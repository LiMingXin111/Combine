"""外网搜索服务（v2.1）：多引擎聚合、垂直渠道与正文抓取。

设计目标（用户要求：把外网搜索加到最强，用各种方法增强，深入各种渠道）：

1. **多引擎并行**：同一查询同时发往多个相互独立的检索源，单点失败 / 被反爬拦截不影响整体
   （失败引擎记入 engines_failed，其余结果照常返回）；
2. **查询变体融合**：模型可为困难主题一次给出 2–3 个变体（换语言 / 换措辞 / 换视角），
   全部结果经 RRF（Reciprocal Rank Fusion, k=60）融合排名；
3. **跨引擎去重**：URL 归一化（去 utm 等跟踪参数与 fragment）+ 标题归一化双重去重，
   合并来源引擎列表；
4. **垂直渠道**：新闻资讯（Google News / Bing News / 微信公众号文章）、
   学术（OpenAlex / arXiv / Crossref）、代码与技术社区（GitHub / Stack Overflow / Hacker News）；
5. **正文抓取**（web.fetch）：从「只读摘要」升级为「读全文」——HTML 去噪转纯文本；
   SSRF 防护：仅 http/https、解析后 IP 必须是公网地址、重定向逐跳校验、响应体限量；
6. **结果缓存**：进程内 TTL（10 分钟）+ LRU 上限，同一查询不重复出网；
7. **可离线**：WEB_SEARCH_MODE=auto|fake|off。fake 用于测试与无网演示（返回夹具并标注），
   off 时工具不提供给模型（runner / policy 由此判断）。

合规与礼数：只访问公开页面（不登录、不绕过反爬）；被拦截（403/429/验证页）即算该引擎失败并降级；
带可识别 UA 与语言头；单引擎超时 8 秒；不做高频重试。

引擎可用性是在目标网络环境实测后确定的（2026-10）：
- 稳定：搜狗、360、Google News RSS、Bing News RSS、搜狗微信、OpenAlex、arXiv、Crossref、
  GitHub API、Hacker News（Algolia）、Stack Exchange API；
- 尽力而为：Bing RSS（英文较佳、偶发空响应）；
- 实测不可用（被反爬或屏蔽，未纳入）：DuckDuckGo、Wikipedia、Reddit、Baidu、Brave、Qwant、Mojeek。
"""
from __future__ import annotations

import gzip
import html as _html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from .. import config

# ---------------- 常量 ----------------
SEARCH_TIMEOUT = 8                 # 单引擎超时（秒）
FETCH_TIMEOUT = 12                 # 正文抓取超时（秒）
RESP_MAX_BYTES = 3 * 1024 * 1024   # 搜索响应体上限
FETCH_MAX_BYTES = 2 * 1024 * 1024  # 抓取响应体上限
FETCH_MAX_CHARS = 12000            # 正文返回给模型的字符上限
CACHE_TTL = 600                    # 结果缓存有效期（秒）
CACHE_MAX = 200                    # 缓存条数上限
RRF_K = 60                         # RRF 融合常数
PER_ENGINE = 10                    # 每引擎每查询取前 N 条
SNIPPET_MAX = 200                  # 摘要截断
MAX_VARIANTS = 3                   # 查询变体上限

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 CombineBot/2.1 (local research agent)")

TOOLS = ("web.search", "web.news", "web.scholar", "web.code", "web.fetch")

# 搜索工具按查询语言选择引擎集合（实测：Bing 的中文排序质量差，不参与中文查询）
_GENERAL_ZH = ("sogou", "so360")
_GENERAL_OTHER = ("bing", "sogou", "so360")


# ---------------- 基础 HTTP ----------------
def _decode(body: bytes, ctype: str = "") -> str:
    m = re.search(r"charset=[\"']?([\w-]+)", ctype or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "gbk"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def _http_get(url: str, timeout: int = SEARCH_TIMEOUT, max_bytes: int = RESP_MAX_BYTES,
              headers: dict | None = None) -> tuple[int, str, str]:
    """GET 一个 URL，返回 (status, 文本, final_url)。自动解 gzip、跟随 3xx（搜索场景用）。"""
    h = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
         "Accept-Encoding": "gzip", "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                body = body[:max_bytes]
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError):
                    pass
            return r.status, _decode(body, r.headers.get("Content-Type") or ""), r.geturl()
    except urllib.error.HTTPError as e:            # 4xx / 5xx（urllib 会抛）
        return e.code, "", url
    except Exception:                              # noqa: BLE001 —— 网络层任何异常都算该请求失败
        return 0, "", url


def _strip_tags(s: str) -> str:
    s = re.sub(r"(?i)</?em\b[^>]*>", "", s or "")       # 搜索高亮 <em> 直接去除（不留空格）
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _has_cjk(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", s or ""))


# ---------------- 引擎解析器 ----------------
def _item(title: str, url: str, snippet: str, engine: str, **extra) -> dict:
    it = {"title": _clip(_strip_tags(title), 110), "url": (url or "").strip(),
          "snippet": _clip(_strip_tags(snippet), SNIPPET_MAX), "engine": engine}
    it.update({k: v for k, v in extra.items() if v not in (None, "")})
    return it


def _eng_bing(q: str) -> list[dict]:
    """Bing RSS（best-effort：英文查询较佳，偶发空响应）。"""
    lang = "zh-CN" if _has_cjk(q) else "en-US"
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "mkt": lang, "count": 10})
    status, body, _ = _http_get(url)
    if status != 200 or "<item>" not in body:
        raise RuntimeError(f"bing status={status}")
    root = ET.fromstring(body.encode("utf-8"))
    out = []
    for it in root.findall(".//item")[:PER_ENGINE]:
        link = (it.findtext("link") or "").strip()
        if link and "bing.com" not in urllib.parse.urlsplit(link).netloc:
            out.append(_item(it.findtext("title") or "", link, it.findtext("description") or "", "bing"))
    return out


def _html_blocks(html: str, h3_pat: str) -> list[str]:
    """把结果页按 <h3 ...> 切块（块 = 当前 h3 到下一个 h3 之间），供解析器在块内找标题与摘要。"""
    starts = [m.start() for m in re.finditer(h3_pat, html)]
    return [html[s: starts[i + 1] if i + 1 < len(starts) else len(html)] for i, s in enumerate(starts)]


def _eng_sogou(q: str) -> list[dict]:
    url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": q})
    status, body, _ = _http_get(url)
    if status != 200 or "vr-title" not in body:
        raise RuntimeError(f"sogou status={status}")
    out = []
    for block in _html_blocks(body, r'<h3[^>]*class="[^"]*vr-title'):
        a = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue                                    # 无链接的结构化卡片（专业/学校卡片等）跳过
        href = _html.unescape(a.group(1))
        if not _strip_tags(a.group(2)) or href.startswith(("javascript:", "#")):
            continue                                    # 伪链接（相关搜索等）跳过
        if href.startswith("/"):
            href = "https://www.sogou.com" + href
        sn = re.search(r'class="fz-mid[^"]*"[^>]*>(.*?)</div>', block, re.S) \
            or re.search(r'id="cacheresult_summary[^"]*"[^>]*>(.*?)</div>', block, re.S)
        out.append(_item(a.group(2), href, sn.group(1) if sn else "", "sogou"))
        if len(out) >= PER_ENGINE:
            break
    return out


def _eng_so360(q: str) -> list[dict]:
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": q})
    status, body, _ = _http_get(url)
    if status != 200 or "res-title" not in body:
        raise RuntimeError(f"so360 status={status}")
    out = []
    for block in _html_blocks(body, r'<h3[^>]*class="res-title"'):
        a = re.search(r'<a[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        tag = block[: a.end()]
        md = re.search(r'data-mdurl="([^"]+)"', tag)
        href = _html.unescape(md.group(1)) if md else ""
        if not href:
            h = re.search(r'href="([^"]+)"', tag)
            href = _html.unescape(h.group(1)) if h else ""
        if not _strip_tags(a.group(1)) or href.startswith(("javascript:", "#")):
            continue
        if href.startswith("/"):
            href = "https://www.so.com" + href
        sn = re.search(r'class="res-desc"[^>]*>(.*?)</p>', block, re.S) \
            or re.search(r'class="res-comm-con[^"]*"[^>]*>(.*?)</div>', block, re.S)
        out.append(_item(a.group(1), href, sn.group(1) if sn else "", "so360"))
        if len(out) >= PER_ENGINE:
            break
    return out


def _eng_gnews(q: str) -> list[dict]:
    zh = _has_cjk(q)
    params = {"q": q, "hl": "zh-CN" if zh else "en-US", "gl": "CN" if zh else "US",
              "ceid": "CN:zh-Hans" if zh else "US:en"}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    status, body, _ = _http_get(url)
    if status != 200 or "<item>" not in body:
        raise RuntimeError(f"gnews status={status}")
    root = ET.fromstring(body.encode("utf-8"))
    out = []
    for it in root.findall(".//item")[:PER_ENGINE]:
        src = it.find("source")
        extra = {"source_name": (src.text or "").strip() if src is not None else ""}
        pd = it.findtext("pubDate") or ""
        if pd:
            extra["published"] = pd[:16]
        out.append(_item(it.findtext("title") or "", it.findtext("link") or "",
                         it.findtext("description") or "", "gnews", **extra))
    return out


def _eng_bing_news(q: str) -> list[dict]:
    params = {"q": q, "format": "rss"}
    if not _has_cjk(q):
        params["mkt"] = "en-US"                      # 中文查询带 mkt=zh-CN 会退化为网页，默认参数反而正确
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode(params)
    status, body, _ = _http_get(url)
    if status != 200 or "<item>" not in body:
        raise RuntimeError(f"bing_news status={status}")
    root = ET.fromstring(body.encode("utf-8"))
    out = []
    for it in root.findall(".//item")[:PER_ENGINE]:
        out.append(_item(it.findtext("title") or "", it.findtext("link") or "",
                         it.findtext("description") or "", "bing_news",
                         published=(it.findtext("pubDate") or "")[:16] or None))
    return out


def _eng_weixin(q: str) -> list[dict]:
    """搜狗微信：公众号文章（中文深度长文渠道）。

    注：实测 tsn 时效参数会返回空壳页（需登录态），不采用；时效过滤交给 gnews 的 when: 语法。
    """
    url = "https://weixin.sogou.com/weixin?" + urllib.parse.urlencode({"type": "2", "query": q})
    status, body, _ = _http_get(url)
    if status != 200 or "txt-box" not in body:
        raise RuntimeError(f"weixin status={status}")
    out = []
    for m in list(re.finditer(r'<div class="txt-box">(.*?)</li>', body, re.S))[: PER_ENGINE]:
        blk = m.group(1)
        a = re.search(r'<h3>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        href = _html.unescape(a.group(1))
        if href.startswith("/"):
            href = "https://weixin.sogou.com" + href
        sn = re.search(r'class="txt-info"[^>]*>(.*?)</p>', blk, re.S)
        acc = re.search(r'class="all-time-y2"[^>]*>(.*?)</span>', blk, re.S)
        ts = re.search(r"timeConvert\('(\d+)'\)", blk)
        extra = {"source_name": _strip_tags(acc.group(1)) if acc else ""}
        if ts:
            extra["published"] = time.strftime("%Y-%m-%d", time.localtime(int(ts.group(1))))
        out.append(_item(a.group(2), href, sn.group(1) if sn else "", "weixin", **extra))
    return out


def _openalex_abstract(r: dict) -> str:
    inv = r.get("abstract_inverted_index")
    if not isinstance(inv, dict):
        return ""
    pos = {}
    for w, idxs in list(inv.items())[:600]:
        for i in idxs:
            if isinstance(i, int):
                pos[i] = w
    return " ".join(pos[i] for i in sorted(pos)[:60])


def _eng_openalex(q: str) -> list[dict]:
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode({"search": q, "per-page": PER_ENGINE})
    status, body, _ = _http_get(url, headers={"Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"openalex status={status}")
    d = json.loads(body)
    out = []
    for r in d.get("results", [])[:PER_ENGINE]:
        loc = (r.get("primary_location") or {}).get("landing_page_url") or r.get("doi") or r.get("id") or ""
        authors = [a.get("author", {}).get("display_name", "") for a in (r.get("authorships") or [])[:2]]
        sn = " · ".join(x for x in [" ".join(authors), str(r.get("publication_year") or ""),
                                    f"被引 {r.get('cited_by_count')}" if r.get("cited_by_count") else "",
                                    _openalex_abstract(r)] if x)
        out.append(_item(r.get("title") or "", loc, sn, "openalex",
                         published=str(r.get("publication_year") or "") or None))
    return out


def _eng_arxiv(q: str) -> list[dict]:
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": f"all:{q}", "max_results": PER_ENGINE})
    status, body, _ = _http_get(url)
    if status != 200 or "<entry>" not in body:
        raise RuntimeError(f"arxiv status={status}")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(body.encode("utf-8"))
    out = []
    for e in root.findall("a:entry", ns)[:PER_ENGINE]:
        link = (e.findtext("a:id", namespaces=ns) or "").replace("http://", "https://")
        api = [a.findtext("a:name", namespaces=ns) or "" for a in e.findall("a:author", ns)[:2]]
        out.append(_item(e.findtext("a:title", namespaces=ns) or "", link,
                         " ".join(api) + " · " + (e.findtext("a:summary", namespaces=ns) or ""), "arxiv",
                         published=(e.findtext("a:published", namespaces=ns) or "")[:10] or None))
    return out


def _eng_crossref(q: str) -> list[dict]:
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query": q, "rows": PER_ENGINE})
    status, body, _ = _http_get(url)
    if status != 200:
        raise RuntimeError(f"crossref status={status}")
    d = json.loads(body)
    out = []
    for w in (d.get("message", {}).get("items") or [])[:PER_ENGINE]:
        title = (w.get("title") or [""])[0]
        year = ""
        try:
            year = str(w.get("issued", {}).get("date-parts", [[""]])[0][0] or "")
        except (IndexError, TypeError):
            pass
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in (w.get("author") or [])[:2]]
        sn = " · ".join(x for x in [" ".join(authors), year, w.get("type") or ""] if x)
        out.append(_item(title, w.get("URL") or "", sn, "crossref", published=year or None))
    return out


def _eng_github(q: str) -> list[dict]:
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": PER_ENGINE})
    headers = {"Accept": "application/vnd.github+json"}
    token = config.env("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body, _ = _http_get(url, headers=headers)
    if status != 200:
        raise RuntimeError(f"github status={status}")
    d = json.loads(body)
    out = []
    for r in (d.get("items") or [])[:PER_ENGINE]:
        sn = " · ".join(x for x in [f"★{r.get('stargazers_count')}", r.get("language") or "",
                                    r.get("description") or ""] if x)
        out.append(_item(r.get("full_name") or "", r.get("html_url") or "", sn, "github",
                         published=(r.get("updated_at") or "")[:10] or None))
    return out


def _eng_hn(q: str) -> list[dict]:
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"query": q, "tags": "story", "hitsPerPage": PER_ENGINE})
    status, body, _ = _http_get(url)
    if status != 200:
        raise RuntimeError(f"hn status={status}")
    d = json.loads(body)
    out = []
    for h in (d.get("hits") or [])[:PER_ENGINE]:
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        sn = " · ".join(x for x in [f"{h.get('points') or 0} points" if h.get("points") is not None else "",
                                    f"{h.get('num_comments') or 0} comments",
                                    (h.get("created_at") or "")[:10]] if x)
        out.append(_item(h.get("title") or "", link, sn, "hn", published=(h.get("created_at") or "")[:10] or None))
    return out


def _eng_so(q: str) -> list[dict]:
    url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode(
        {"order": "desc", "sort": "relevance", "q": q, "site": "stackoverflow", "pagesize": PER_ENGINE})
    status, body, _ = _http_get(url)
    if status != 200:
        raise RuntimeError(f"stackexchange status={status}")
    d = json.loads(body)
    out = []
    for qq in (d.get("items") or [])[:PER_ENGINE]:
        sn = " · ".join(x for x in ["answered" if qq.get("is_answered") else "unanswered",
                                    f"{qq.get('answer_count') or 0} answers",
                                    "tags: " + ", ".join(qq.get("tags") or [])] if x)
        out.append(_item(_html.unescape(qq.get("title") or ""), qq.get("link") or "", sn, "stackexchange"))
    return out


_ENGINES = {"bing": _eng_bing, "sogou": _eng_sogou, "so360": _eng_so360, "gnews": _eng_gnews,
            "bing_news": _eng_bing_news, "weixin": _eng_weixin, "openalex": _eng_openalex,
            "arxiv": _eng_arxiv, "crossref": _eng_crossref, "github": _eng_github,
            "hn": _eng_hn, "stackexchange": _eng_so}


# ---------------- 融合与去重 ----------------
_TRACK_RE = re.compile(r"^(utm_|spm|ref$|ref_|fr$|from$|src$|source$|share_|scm|vd_source)")


def _url_key(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u or "")
    except ValueError:
        return (u or "").lower()
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not _TRACK_RE.match(k.lower())]
    host = (p.netloc or "").lower().removeprefix("www.")
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path}" + (f"?{urllib.parse.urlencode(sorted(q))}" if q else "")


def _title_key(t: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (t or "").lower())[:60]


def _fuse(lists: list[list[dict]], limit: int) -> list[dict]:
    """RRF 融合多份排名列表：去重（URL + 标题）、合并来源、按融合分排序。"""
    merged: dict[str, dict] = {}
    scores: dict[str, float] = {}
    by_url: dict[str, str] = {}
    by_title: dict[str, str] = {}
    n = 0
    for lst in lists:
        for rank, it in enumerate(lst, 1):
            uk = _url_key(it["url"])
            tk = _title_key(it["title"])
            tk = tk if len(tk) >= 8 else ""
            key = by_url.get(uk) if uk else None
            if key is None and tk:
                key = by_title.get(tk)
            if key is None:
                n += 1
                key = f"k{n}"
                merged[key] = {"title": it["title"], "url": it["url"], "snippet": it["snippet"],
                               "engines": [it["engine"]],
                               **{f: it[f] for f in ("source_name", "published") if it.get(f)}}
            else:
                cur = merged[key]
                if it["engine"] not in cur["engines"]:
                    cur["engines"].append(it["engine"])
                if len(it.get("snippet") or "") > len(cur.get("snippet") or ""):
                    cur["snippet"] = it["snippet"]
                for f in ("source_name", "published"):
                    if it.get(f) and not cur.get(f):
                        cur[f] = it[f]
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            if uk:
                by_url.setdefault(uk, key)
            if tk:
                by_title.setdefault(tk, key)
    out = sorted(merged.items(), key=lambda kv: (-scores[kv[0]], kv[0]))
    return [dict(it, score=round(scores[k], 4)) for k, it in out[:limit]]


# ---------------- 缓存 ----------------
_CACHE: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    if hit:
        _CACHE.pop(key, None)
    return None


def _cache_put(key: str, val: dict) -> None:
    _CACHE[key] = (time.time(), val)
    while len(_CACHE) > CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)


# ---------------- 模式 ----------------
def mode() -> str:
    m = (config.WEB_SEARCH_MODE or "auto").lower()
    return m if m in ("auto", "fake", "off") else "auto"


def available() -> bool:
    """搜索工具在当前部署下是否可用（runner / policy 用来决定是否提供给模型）。"""
    return mode() != "off"


# ---------------- 工具入口 ----------------
def run(name: str, args: dict, *, enabled: bool = True, mode_override: str | None = None) -> dict:
    """执行一个搜索工具，返回可序列化结果。异常转换为 {error}，不拖垮节点。"""
    if name not in TOOLS:
        return {"error": "TOOL_DENIED"}
    if not enabled:
        return {"error": "TOOL_DISABLED", "message": "外网检索未开启（设置 → 工具与权限）"}
    selected_mode = mode_override or mode()
    if mode() == "off" or selected_mode == "off":
        return {"error": "TOOL_DISABLED", "message": "外网检索已被部署配置关闭（WEB_SEARCH_MODE=off）"}
    try:
        if selected_mode == "fake":
            return _fake(name, args)                     # 离线夹具：绝不出网（含 web.fetch）
        if name == "web.fetch":
            return _run_fetch(args)
        return _run_search(name, args)
    except Exception as e:                              # noqa: BLE001
        return {"error": "TOOL_ERROR", "message": str(e)[:200]}


def _variants(args: dict) -> list[str]:
    q = str(args.get("query") or "").strip()[:200]
    extra = [str(x).strip()[:200] for x in (args.get("queries") or []) if str(x).strip()]
    seen, out = set(), []
    for v in [q, *extra]:
        k = v.lower()
        if v and k not in seen:
            seen.add(k)
            out.append(v)
    return out[:MAX_VARIANTS]


def _general_engines(q: str) -> list[str]:
    return list(_GENERAL_ZH) if _has_cjk(q) else list(_GENERAL_OTHER)


def _run_tasks(tasks: list[tuple[str, str]], fn_map: dict) -> tuple[list[list[dict]], dict, list[str]]:
    """并行执行 (engine, query) 任务；返回（成功列表集合、各引擎条数、失败引擎名）。"""
    lists, counts, failed = [], {}, []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tasks)))) as ex:
        futs = {ex.submit(fn_map[eng], q): eng for eng, q in tasks}
        for fu in futs:
            eng = futs[fu]
            try:
                items = fu.result(timeout=SEARCH_TIMEOUT + 6)
                if items:
                    lists.append(items)
                    counts[eng] = counts.get(eng, 0) + len(items)
                else:
                    failed.append(eng)
            except Exception:                            # noqa: BLE001 —— 单引擎失败不影响整体
                failed.append(eng)
    return lists, counts, sorted(set(failed))


def _run_search(name: str, args: dict) -> dict:
    queries = _variants(args)
    if not queries:
        return {"error": "BAD_ARGS", "message": "缺少 query"}
    ck = f"{name}|{json.dumps(sorted(queries))}|{json.dumps({k: args.get(k) for k in ('freshness', 'kind') if args.get(k)}, sort_keys=True)}"
    if (hit := _cache_get(ck)) is not None:
        return dict(hit, cached=True)

    freshness = str(args.get("freshness") or "").lower()
    kind = str(args.get("kind") or "all").lower()
    tasks: list[tuple[str, str]] = []
    if name == "web.search":
        for q in queries:
            for eng in _general_engines(q):
                tasks.append((eng, q))
    elif name == "web.news":
        for q in queries:
            tasks.append(("gnews", q + (f" when:{'1d' if freshness == 'day' else '7d' if freshness == 'week' else '30d'}" if freshness in ("day", "week", "month") else "")))
            tasks.append(("bing_news", q))
            if _has_cjk(q):
                tasks.append(("weixin", q))
    elif name == "web.scholar":
        for q in queries:
            tasks += [("openalex", q), ("arxiv", q), ("crossref", q)]
    elif name == "web.code":
        for q in queries:
            if kind in ("all", "repos"):
                tasks.append(("github", q))
            if kind in ("all", "qa"):
                tasks.append(("stackexchange", q))
            if kind in ("all", "discuss"):
                tasks.append(("hn", q))

    lists, counts, failed = _run_tasks(tasks, dict(_ENGINES))
    limit = {"web.search": 8, "web.news": 8, "web.scholar": 6, "web.code": 6}.get(name, 8)
    items = _fuse(lists, limit)
    if not items:
        return {"error": "NO_RESULT", "message": "本次检索没有返回结果（相关渠道均失败或被拦截，可稍后重试）",
                "engines_failed": failed}
    out = {"items": items, "engines": counts, "engines_failed": failed,
           "queries": queries, "cached": False}
    _cache_put(ck, out)
    return out


# ---------------- 正文抓取（SSRF 防护） ----------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):                 # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _public_host(host: str) -> bool:
    """host 解析出的所有 IP 必须都是公网地址（防 SSRF：禁私网 / 环回 / 链路本地 / 保留段）。"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not addr.is_global:
            return False
    return True


def _html_to_text(page: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|iframe|head|nav|footer)[^>]*>.*?</\1>", " ", page)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article|blockquote)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _run_fetch(args: dict) -> dict:
    url = str(args.get("url") or "").strip()[:1000]
    if not url:
        return {"error": "BAD_ARGS", "message": "缺少 url"}
    try:
        max_chars = min(int(args.get("max_chars") or FETCH_MAX_CHARS), FETCH_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = FETCH_MAX_CHARS
    cur = url
    for _hop in range(5):
        p = urllib.parse.urlsplit(cur)
        if p.scheme not in ("http", "https"):
            return {"error": "BAD_URL", "message": "只支持 http/https 地址"}
        if not _public_host(p.hostname or ""):
            return {"error": "BLOCKED", "message": "该地址不允许访问（仅限公网公开页面）"}
        try:
            req = urllib.request.Request(cur, headers={"User-Agent": _UA, "Accept": "*/*",
                                                       "Accept-Encoding": "gzip"})
            with _OPENER.open(req, timeout=FETCH_TIMEOUT) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                ce = (r.headers.get("Content-Encoding") or "").lower()
                body = r.read(FETCH_MAX_BYTES + 1)
                if ce == "gzip":
                    try:
                        body = gzip.decompress(body)
                    except (OSError, EOFError):
                        return {"error": "FETCH_FAILED", "message": "响应体 gzip 解压失败"}
                elif ce and ce != "identity":
                    return {"error": "FETCH_FAILED", "message": f"不支持的压缩编码：{ce}"}
                is_html = "html" in ctype or "xml" in ctype or not ctype
                text = _html_to_text(_decode(body, ctype)) if is_html else _decode(body, ctype)
                title = ""
                if is_html:
                    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", _decode(body[:200000], ctype))
                    title = _strip_tags(mt.group(1)) if mt else ""
                return {"url": r.geturl(), "title": _clip(title, 120), "content": _clip(text, max_chars),
                        "chars": len(text), "truncated": len(text) > max_chars}
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                cur = urllib.parse.urljoin(cur, loc)
                continue
            return {"error": "FETCH_FAILED", "message": f"HTTP {e.code}"}
        except Exception as e:                           # noqa: BLE001
            return {"error": "FETCH_FAILED", "message": str(e)[:160]}
    return {"error": "TOO_MANY_REDIRECTS", "message": "重定向次数过多"}


# ---------------- 离线夹具（WEB_SEARCH_MODE=fake） ----------------
_FAKE: dict[str, list[dict]] = {
    "web.search": [
        {"title": "检索增强生成（RAG）在个人助理中的应用综述", "url": "https://example.org/survey-rag-agents",
         "snippet": "离线演示数据：本条目用于在无网络环境下展示多引擎检索结果的融合形态（RRF 排名、来源去重）。", "engines": ["sogou", "so360"], "score": 0.0325},
        {"title": "Retrieval-Augmented Generation for Knowledge-Intensive Tasks", "url": "https://example.org/rag-paper",
         "snippet": "Offline demo data: shows how ranked results from multiple engines are fused and de-duplicated.", "engines": ["bing"], "score": 0.0161},
        {"title": "从零构建个人知识库助手：架构与落地要点", "url": "https://example.org/kb-assistant",
         "snippet": "离线演示数据：包含「检索 → 融合 → 引用」的工程实践要点，可在配置真实检索后获得在线内容。", "engines": ["so360"], "score": 0.0154},
    ],
    "web.news": [
        {"title": "多智能体协作成为生产力工具新趋势", "url": "https://example.org/news-agents",
         "snippet": "离线演示数据：新闻渠道（Google News / Bing News / 微信公众号）的条目形态。", "engines": ["gnews"], "source_name": "示例科技", "score": 0.0325},
        {"title": "AI research roundup: agents, retrieval and evaluation", "url": "https://example.org/news-roundup",
         "snippet": "Offline demo data for the news channel.", "engines": ["bing_news"], "source_name": "Demo Wire", "score": 0.0161},
    ],
    "web.scholar": [
        {"title": "Active Retrieval Augmented Generation", "url": "https://doi.org/10.18653/v1/2023.emnlp-main.495",
         "snippet": "Jiang et al. · 2023 · 被引 457 · 离线演示数据（真实来源为 OpenAlex 样例）。", "engines": ["openalex"], "score": 0.0325},
        {"title": "AR-RAG: Autoregressive Retrieval Augmentation for Image Generation", "url": "https://arxiv.org/abs/2506.06962",
         "snippet": "离线演示数据（真实来源为 arXiv 样例）。", "engines": ["arxiv"], "score": 0.0161},
    ],
    "web.code": [
        {"title": "infiniflow/ragflow", "url": "https://github.com/infiniflow/ragflow",
         "snippet": "★91732 · Go · 离线演示数据（真实来源为 GitHub 样例）。", "engines": ["github"], "score": 0.0325},
        {"title": "Retrieval augmented generation (RAG) for text classification", "url": "https://stackoverflow.com/questions/77190366",
         "snippet": "answered · 1 answers · tags: python, langchain", "engines": ["stackexchange"], "score": 0.0161},
        {"title": "How do domain-specific chatbots work?", "url": "https://news.ycombinator.com/item?id=37261198",
         "snippet": "136 points · 37 comments", "engines": ["hn"], "score": 0.0154},
    ],
}


def _fake(name: str, args: dict) -> dict:
    if name == "web.fetch":
        return {"error": "TOOL_DISABLED", "message": "离线演示（fake）模式下不抓取网页"}
    items = [dict(it) for it in _FAKE.get(name, [])]
    return {"items": items, "engines": {it["engines"][0]: 1 for it in items}, "engines_failed": [],
            "queries": _variants(args), "cached": False, "note": "offline demo data"}
