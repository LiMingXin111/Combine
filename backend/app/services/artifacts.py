"""成果（F13 / F18）：章节由声明了 produces_sections 的节点产出，章节与节点一一对应。

成果由系统按章节顺序拼装，不再调用模型：因此重写某一章时，其他章节内容逐字不变（INV-17）。

成果同时打包为「成果卡片」（v1.5）：任务完成后按 地图（map）→ 图（chart）→ 文（doc）→ 文件（file）
四类卡片交付，前端据此分组展示。卡片只是索引：正文仍在 sections、图表/地图数据与文件以引用方式保存。

v1.8 人工编辑：用户可以在画布右侧面板直接修改某一节成果（`save_edit`）。编辑按节记录在
`section_edits`（不改计划、不建计划版本）；组装成果时「最后写入者胜出」——编辑晚于步骤最后一次
产出则以用户编辑稿为准，步骤之后重跑产出新稿则以新稿为准。编辑稿会在该步骤（及其下游）重跑时
作为上下文注入模型，让「修改之后再进行的新对话」也读得懂用户的修改。

v1.9 地图成果：旅行 / 路线类任务可以产出「攻略地图」——节点输出可选 `map` 字段
（`markers` 为 GCJ-02 坐标的地点列表，按天分组、按数组顺序连线），经 `clean_map` 校验后成为
地图卡；前端渲染为 SVG 攻略图（离线可用），真实在线底图由用户自配地图服务（见 PRD 06 §地图能力）。
"""
from __future__ import annotations

import re

from .. import config, db
from ..core import graph
from ..util import ApiError, clip, new_id, now_ms, stable_hash
from . import kb

MAX_MAPS = 2
MAX_CHARTS = 3
MAX_FILES = 8
MAX_EDIT_CHARS = 20000          # 单节人工编辑长度上限（足够长的交付章节）

# 用户编辑稿注入模型上下文的说明（v1.8）：重跑对应步骤时，模型须在用户修改稿的基础上继续
USER_EDIT_INSTRUCTION = "用户已手工修改过这一步的成果，改后的版本如下（请以它为基础继续，保留用户的修改）：\n{content}"

# 交付文件不收录的路径前缀：临时草稿目录是过程中的工作文件，被清理属预期行为
_FILE_SKIP_PREFIXES = ("drafts/",)


def _sanitize_name(s: str, n: int = 36) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(s or "")).strip()
    return s[:n] or "deliverable"


def _pack_result(task: dict, title: str, markdown: str) -> dict | None:
    """收官系统打包：把成果全文写入任务工作区，作为可下载的交付文件（云端无工作区，跳过）。

    不依赖模型行为：即使规划没有打包步骤、或模型没有调用 file.write，成果也总有一个文件形态。
    """
    if config.DEPLOY_MODE != "self_hosted":
        return None
    from . import workspace
    try:
        path = f"交付/{task['id']}/v{task.get('active_version') or 0}/{new_id('file')}/{_sanitize_name(title)}.md"
        r = workspace.write(task["user_id"], path, markdown)
        if isinstance(r, dict) and r.get("written"):
            return {"path": r["written"], "bytes": r.get("bytes", 0)}
    except Exception:      # noqa: BLE001  打包失败不影响成果交付
        pass
    return None


def clean_chart(raw) -> dict | None:
    """校验并清洗节点产出的图表数据；结构不合法时返回 None（卡片宁缺毋滥，前端不做二次防御）。"""
    if not isinstance(raw, dict):
        return None
    ctype = raw.get("type")
    if ctype not in ("bar", "pie", "line"):
        return None
    labels = [clip(str(x).strip(), 20) for x in _as_list(raw.get("labels")) if str(x).strip()][:12]
    if len(labels) < 2:
        return None
    series = []
    for s in _as_list(raw.get("series"))[:4]:
        if not isinstance(s, dict):
            continue
        vals: list[float] = []
        for v in _as_list(s.get("values")):
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = 0.0
            vals.append(round(f if f == f else 0.0, 2))     # NaN → 0
        if len(vals) != len(labels):                        # 与 labels 对齐（截断或补零），防止前端错位
            vals = (vals + [0.0] * len(labels))[:len(labels)]
        series.append({"name": clip(str(s.get("name") or ""), 30), "values": vals})
    if ctype == "pie":
        series = series[:1]                                 # 饼图只取第一组数据
    if not series:
        return None
    return {"type": ctype, "title": clip(str(raw.get("title") or "").strip(), 60), "labels": labels, "series": series}


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def clean_map(raw) -> dict | None:
    """校验并清洗节点产出的地图数据（v1.9）；结构不合法时返回 None（同 clean_chart 的宁缺毋滥）。

    契约：{title, city, markers: [{name, lat, lng, day 1–7, note}]}
    - 坐标 GCJ-02（国测局坐标，适用于腾讯/高德等白名单地图服务）；纬度 ±85、经度 ±180 之外丢弃；
    - 至少 2 个有效地点、至多 24 个；数组顺序即游览顺序，同一天内按顺序连线。
    """
    if not isinstance(raw, dict):
        return None
    markers = []
    for m in _as_list(raw.get("markers"))[:24]:
        if not isinstance(m, dict):
            continue
        name = clip(str(m.get("name") or "").strip(), 24)
        try:
            lat, lng = float(m.get("lat")), float(m.get("lng"))
        except (TypeError, ValueError):
            continue
        if not name or lat != lat or lng != lng or not (-85 <= lat <= 85 and -180 <= lng <= 180):
            continue                                            # NaN / 越界坐标：宁缺毋滥
        try:
            day = int(m.get("day") or 1)
        except (TypeError, ValueError):
            day = 1
        markers.append({"name": name, "lat": round(lat, 5), "lng": round(lng, 5),
                        "day": min(max(day, 1), 7),
                        "note": clip(str(m.get("note") or "").strip(), 60)})
    if len(markers) < 2:
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60),
            "city": clip(str(raw.get("city") or "").strip(), 20),
            "markers": markers}


def _excerpt(content: str, limit: int = 140) -> str:
    """卡片摘要：去掉引用标记与 Markdown 标题符号，压成一行。"""
    t = re.sub(r"\[\[c:[\w-]+\]\]", "", content or "")
    t = re.sub(r"^\s*#{1,4}\s*", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    return clip(t, limit)


# ==================== v3.0 M3/M4：功能性节点的数据契约（prd/08 §6） ====================
# 每个 clean_<kind> 与 clean_chart / clean_map 同规约：结构不合法返回 None（卡片宁缺毋滥，
# 前端不做二次防御）；数值与文本混排保持原样、NaN → null、行列/条目按上限截断。
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"}
CODE_LANGS = {"js", "ts", "jsx", "tsx", "py", "go", "java", "sql", "json", "yaml", "yml", "sh", "bash",
              "diff", "html", "css", "c", "cpp", "rs", "rb", "php", "xml", "md", "plain"}
MAX_TABLE_COLS = 12
MAX_TABLE_ROWS = 50
MAX_CODE_CHARS = 4000
MAX_CODE_LINES = 200
MAX_METRIC_CARDS = 6
MAX_LIST_ITEMS = 50
MAX_KANBAN_COLS = 5
MAX_TIMELINE_ITEMS = 20
MAX_CALENDAR_EVENTS = 21
MAX_STATUS_ITEMS = 12
MAX_CHAT_MESSAGES = 30


def _cell(v):
    """单元格归一：NaN → null；数值四舍五入保留；其余转短文本。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return clip(str(v), 80)
        return None if f != f else (int(v) if isinstance(v, int) else round(f, 4))
    return clip(str(v), 80)


def clean_table(raw) -> dict | None:
    """表格契约：{title, columns:[{key,label,align}], rows:[[cell]], total_rows, note}。

    上限：列 ≤12、行 ≤50（画布内）；total_rows 记录真实行数（完整数据由节点写工作区 CSV）。
    """
    if not isinstance(raw, dict):
        return None
    cols, seen = [], set()
    for c in _as_list(raw.get("columns"))[:MAX_TABLE_COLS]:
        if not isinstance(c, dict):
            continue
        key = clip(str(c.get("key") or "").strip(), 24)
        if not key or key in seen:
            continue
        seen.add(key)
        cols.append({"key": key, "label": clip(str(c.get("label") or key).strip(), 24) or key,
                     "align": "right" if str(c.get("align") or "").lower() == "right" else "left"})
    if not cols:
        return None
    rows = []
    for r in _as_list(raw.get("rows"))[:MAX_TABLE_ROWS]:
        vals = r if isinstance(r, list) else ([r] if isinstance(r, (str, int, float, bool)) else [])
        cells = [_cell(v) for v in vals[:len(cols)]]
        cells += [None] * (len(cols) - len(cells))           # 行对齐：不足补空、超出截断
        rows.append(cells)
    if not rows:
        return None
    try:
        total = int(raw.get("total_rows") or len(rows))
    except (TypeError, ValueError):
        total = len(rows)
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "columns": cols, "rows": rows,
            "total_rows": max(total, len(rows)), "note": clip(str(raw.get("note") or "").strip(), 120)}


def clean_metrics(raw) -> dict | None:
    """指标组契约：{title, items:[{label, value, delta, dir:up|down|flat, status:good|warn|bad}], note}。

    dir 只表达方向，颜色由前端按中国惯例处理（up=红、down=绿）；note 是放大时的口径说明。
    """
    if not isinstance(raw, dict):
        return None
    items = []
    for it in _as_list(raw.get("items"))[:MAX_METRIC_CARDS]:
        if not isinstance(it, dict):
            continue
        label = clip(str(it.get("label") or "").strip(), 24)
        value = clip(str(it.get("value") if it.get("value") is not None else "").strip(), 24)
        if not label or not value:
            continue
        d = str(it.get("dir") or "").lower()
        st = str(it.get("status") or "").lower()
        items.append({"label": label, "value": value, "delta": clip(str(it.get("delta") or "").strip(), 16),
                      "dir": d if d in ("up", "down", "flat") else "",
                      "status": st if st in ("good", "warn", "bad") else "good"})
    if not items:
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "items": items,
            "note": clip(str(raw.get("note") or "").strip(), 200)}


def clean_list(raw) -> dict | None:
    """清单 / 看板契约：{title, mode:checklist|kanban, items:[{text, done, meta, column}]}。

    checklist 上限 50 条；kanban 列 ≤5（超出的列并入前 5 列内的分组）。
    """
    if not isinstance(raw, dict):
        return None
    mode = "kanban" if str(raw.get("mode") or "").lower() == "kanban" else "checklist"
    items = []
    for it in _as_list(raw.get("items"))[:MAX_LIST_ITEMS]:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = clip(str(it.get("text") or "").strip(), 120)
        if not text:
            continue
        row = {"text": text, "done": bool(it.get("done")), "meta": clip(str(it.get("meta") or "").strip(), 40)}
        if mode == "kanban":
            row["column"] = clip(str(it.get("column") or "").strip(), 20) or "未分组"
        items.append(row)
    if not items:
        return None
    out = {"title": clip(str(raw.get("title") or "").strip(), 60), "mode": mode, "items": items}
    if mode == "kanban":
        cols = list(dict.fromkeys(str(i.get("column") or "未分组") for i in items))[:MAX_KANBAN_COLS]
        for i in items:                                      # 超出列上限的分组并入最后一列（不丢条目）
            if i.get("column") not in cols:
                i["column"] = cols[-1]
        out["columns"] = cols
    return out


def clean_code(raw) -> dict | None:
    """代码 / 差异契约：{title, language, mode:code|diff, code, path, note}。上限 4000 字符、200 行。"""
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("code") or "")
    if not code.strip():
        return None
    lines = code.split("\n")
    truncated = len(lines) > MAX_CODE_LINES or len(code) > MAX_CODE_CHARS
    code = "\n".join(lines[:MAX_CODE_LINES])[:MAX_CODE_CHARS]
    lang = str(raw.get("language") or "").strip().lower()
    return {"title": clip(str(raw.get("title") or "").strip(), 60),
            "language": lang if lang in CODE_LANGS else "plain",
            "mode": "diff" if str(raw.get("mode") or "").lower() == "diff" else "code",
            "code": code, "path": clip(str(raw.get("path") or "").strip(), 120),
            "note": clip(str(raw.get("note") or "").strip(), 120), "truncated": truncated}


def clean_image(raw, user_id: str = "", task_id: str = "", version: int = 0) -> dict | None:
    """图片契约：{title, path, caption, source_url}。

    安全：path 必须是该用户工作区内真实存在的图片文件（复用 workspace 沙箱防路径逃逸）；
    外链图片不直连（避免 IP 泄露与 CSP 复杂化）——需要外链场景由 web 节点承担。
    """
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip().replace("\\", "/")[:200]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if not path or ext not in IMAGE_EXTS:
        return None
    if user_id:
        from . import workspace
        if task_id:
            path = workspace.task_path(task_id, version, path)
        if workspace.resolve(user_id, path, must_exist=True) is None:
            return None
    src = clip(str(raw.get("source_url") or "").strip(), 300)
    if src and not src.startswith(("http://", "https://")):
        src = ""
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "path": path,
            "caption": clip(str(raw.get("caption") or "").strip(), 100), "source_url": src}


def clean_timeline(raw) -> dict | None:
    """时间线契约：{title, items:[{time, text, note}]}，≥2 条、≤20 条。"""
    if not isinstance(raw, dict):
        return None
    items = []
    for it in _as_list(raw.get("items"))[:MAX_TIMELINE_ITEMS]:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = clip(str(it.get("text") or "").strip(), 120)
        if not text:
            continue
        items.append({"time": clip(str(it.get("time") or "").strip(), 24), "text": text,
                      "note": clip(str(it.get("note") or "").strip(), 80)})
    if len(items) < 2:
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "items": items}


def clean_calendar(raw) -> dict | None:
    """日历契约：{title, week_start, events:[{day 1-7, start, end, title, note}]}，≤21 个事件。"""
    if not isinstance(raw, dict):
        return None
    events = []
    for e in _as_list(raw.get("events"))[:MAX_CALENDAR_EVENTS]:
        if not isinstance(e, dict):
            continue
        title = clip(str(e.get("title") or "").strip(), 40)
        if not title:
            continue
        try:
            day = int(e.get("day") or 1)
        except (TypeError, ValueError):
            day = 1
        events.append({"day": min(max(day, 1), 7), "start": clip(str(e.get("start") or "").strip(), 8),
                       "end": clip(str(e.get("end") or "").strip(), 8), "title": title,
                       "note": clip(str(e.get("note") or "").strip(), 60)})
    if not events:
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60),
            "week_start": clip(str(raw.get("week_start") or "").strip(), 20), "events": events}


def clean_status(raw) -> dict | None:
    """状态检查契约：{title, items:[{name, state:ok|warn|fail, detail}]}，≤12 项。"""
    if not isinstance(raw, dict):
        return None
    items = []
    for it in _as_list(raw.get("items"))[:MAX_STATUS_ITEMS]:
        if not isinstance(it, dict):
            continue
        name = clip(str(it.get("name") or "").strip(), 40)
        if not name:
            continue
        st = str(it.get("state") or "").lower()
        items.append({"name": name, "state": st if st in ("ok", "warn", "fail") else "warn",
                      "detail": clip(str(it.get("detail") or "").strip(), 100)})
    if not items:
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "items": items}


def clean_chat(raw) -> dict | None:
    """会话线程契约：{title, messages:[{who, role:user|other, text, time}]}，≥2 条、≤30 条。"""
    if not isinstance(raw, dict):
        return None
    msgs = []
    for m in _as_list(raw.get("messages"))[:MAX_CHAT_MESSAGES]:
        if not isinstance(m, dict):
            continue
        text = clip(str(m.get("text") or "").strip(), 400)
        if not text:
            continue
        role = str(m.get("role") or "").lower()
        msgs.append({"who": clip(str(m.get("who") or "").strip(), 30) or "?",
                     "role": "user" if role == "user" else "other",
                     "text": text, "time": clip(str(m.get("time") or "").strip(), 20)})
    if len(msgs) < 2:
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "messages": msgs}


def clean_web(raw) -> dict | None:
    """网页快照契约：{title, url, excerpt, text}。url 必须是 http(s)（只收录工具真实返回过的地址）。"""
    if not isinstance(raw, dict):
        return None
    url = clip(str(raw.get("url") or "").strip(), 500)
    if not url.startswith(("http://", "https://")):
        return None
    return {"title": clip(str(raw.get("title") or "").strip(), 60), "url": url,
            "excerpt": clip(str(raw.get("excerpt") or "").strip(), 400),
            "text": clip(str(raw.get("text") or "").strip(), 2000)}


# 卡片组装顺序（prd/08 §6.4）：空间/数据类在前、叙述类在后、附件兜底
CARD_ORDER = ("map", "table", "metrics", "chart", "list", "timeline", "calendar",
              "image", "code", "web", "status", "chat")
_CARD_CAP = {"map": MAX_MAPS, "table": 2, "metrics": 2, "chart": MAX_CHARTS, "list": 2, "timeline": 1,
             "calendar": 1, "image": 3, "code": 3, "web": 2, "status": 2, "chat": 2}


def assemble_cards(sections: list[dict], nodes: list[dict], files: list[dict]) -> list[dict]:
    """把成果组装为卡片列表（v3.0：14 类，顺序见 CARD_ORDER，doc / file 收尾）。

    每类数据来自节点输出的同名字段（写入前已过 clean_<kind> 校验，见 runner.execute）；卡片只是索引：
    完整数据仍在节点输出 / sections / 工作区文件里，前端据此分组直显。
    """
    cards: list[dict] = []
    for kind in CARD_ORDER:
        cap = _CARD_CAP.get(kind, 1)
        seen: set[str] = set()
        count = 0
        for n in nodes:
            if count >= cap:
                break
            data = (n.get("output") or {}).get(kind)
            if not data or n["node_key"] in seen:
                continue
            seen.add(n["node_key"])
            count += 1
            cards.append({"kind": kind, "title": data.get("title") or n["title"], kind: data,
                          "node_key": n["node_key"]})
    for s in sections:                                       # 文
        cards.append({"kind": "doc", "title": s["title"], "excerpt": _excerpt(s.get("content", "")),
                      "section_key": s["section_key"], "node_key": s.get("node_key"),
                      "chars": len(s.get("content") or ""), "citations": len(s.get("citations") or []),
                      "user_edited": bool(s.get("user_edited"))})    # v1.8：人工修改过的章节在卡片上打标
    for f in files[:MAX_FILES]:                              # 文件
        name = f["path"].rsplit("/", 1)[-1]
        cards.append({"kind": "file", "title": name, "path": f["path"], "bytes": f.get("bytes", 0),
                      "node_key": f.get("node_key"),
                      "ext": name.rsplit(".", 1)[-1].lower() if "." in name else ""})
    return cards


def task_files(task: dict, nodes: list[dict] | None = None) -> list[dict]:
    """本任务经 file.write 写入且当前仍存在的交付文件（按写入顺序去重，保留最后一次）。

    v1.6：记录写入它的节点（node_key）——画布上成果节点据此连线到产出步骤。
    """
    from . import workspace
    condition, params = "", [task["id"]]
    if nodes is not None:
        ids = [n["execution_id"] for n in nodes if n.get("execution_id")]
        if not ids:
            return []
        condition = f" AND id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    rows = db.q("SELECT node_key, tool_calls FROM executions WHERE task_id=? AND tool_calls IS NOT NULL" + condition + " ORDER BY started_at", params)
    out: dict[str, dict] = {}
    for r in rows:
        for c in db.j(r["tool_calls"], []) or []:
            if not isinstance(c, dict) or c.get("tool") != "file.write":
                continue
            res = c.get("result") or {}
            path = res.get("written") if isinstance(res, dict) else None
            if not path or any(str(path).startswith(p) or ("/" + p) in str(path) for p in _FILE_SKIP_PREFIXES):
                continue
            out[str(path)] = {"path": str(path), "bytes": res.get("bytes", 0), "node_key": r.get("node_key")}
    live = []
    for path, f in out.items():
        p = workspace.resolve(task["user_id"], path, must_exist=True)
        if p is not None:
            f["bytes"] = p.stat().st_size                       # 以磁盘为准（可能被后续同名写入覆盖）
            live.append(f)
    return live


def _sink_key(nodes: list[dict]) -> str | None:
    """打包文件挂到最后一个无下游的节点（汇点）上，让它在画布上有清晰的产出归属。"""
    sinks = {n["node_key"] for n in nodes} - {d for n in nodes for d in n["depends_on"]}
    for k in reversed(graph.topo_order(nodes)):
        if k in sinks:
            return k
    return None


def merge_content(new_content: str, edit: dict | None, finished_at: int | None) -> tuple[str, bool]:
    """章节内容合并（v1.8）：用户手工编辑稿 vs 步骤新产出——「最后写入者胜出」。

    - 没有编辑 → 用步骤产出；
    - 编辑晚于该步骤最后一次完成（或步骤没有产出）→ 以用户编辑稿为准（user_edited=True）；
    - 步骤在编辑之后重跑并产出了新稿 → 模型新稿生效（编辑意图已通过上下文注入被新稿吸收）。
    返回 (最终内容, 是否为用户编辑稿)。
    """
    if edit and (not new_content or (edit.get("created_at") or 0) > (finished_at or 0)):
        return (edit.get("content") or ""), True
    return (new_content or ""), False


def active_edits(task_id: str) -> dict[str, dict]:
    """当前生效的人工编辑（每节取最新一条 active；旧编辑在保存时被标 superseded）。"""
    rows = db.q("SELECT * FROM section_edits WHERE task_id=? AND status='active' ORDER BY created_at", (task_id,))
    return {r["section_key"]: r for r in rows}


def save_edit(task: dict, section_key: str, content: str, user_id: str) -> dict:
    """人工编辑成果章节（v1.8）：用户在画布右侧面板直接修改结果并保存。

    - 按节记录在 `section_edits`（最新一条 active，旧编辑退役）；**不改计划、不建计划版本**；
    - 保存后立即以「当前节点输出 + 用户编辑稿」重建成果（新 artifact 行）并重写打包文件，
      画布节点、成果页、导出与下载都立刻反映修改；
    - 该节对应步骤日后重跑并产出新稿时，模型新稿取代编辑稿（见 merge_content）；
    - 重跑该步骤（或它的下游）时，编辑稿作为上下文注入模型——新对话读得懂用户的修改。
    """
    from . import store
    if task.get("lane") == "quick" or task["status"] in ("triaging", "planning", "plan_review"):
        raise ApiError(409, "STATE_CONFLICT", "任务还没有可编辑的成果")
    if task["status"] == "running":
        raise ApiError(409, "STATE_CONFLICT", "任务执行中：等这一步跑完再修改")
    if task["status"] == "cancelled":
        raise ApiError(409, "STATE_CONFLICT", "任务已取消，不能编辑成果")
    if not isinstance(content, str):
        raise ApiError(400, "FIELD_INVALID", "content 应为字符串")
    if len(content) > MAX_EDIT_CHARS:
        raise ApiError(400, "FIELD_INVALID", f"内容过长：单节上限 {MAX_EDIT_CHARS} 字")
    pv = store.active_pv(task)
    if not pv:
        raise ApiError(409, "STATE_CONFLICT", "任务还没有已确认的计划")
    art = latest(task["id"])
    sec = next((s for s in (art or {}).get("sections", []) if s["section_key"] == section_key), None)
    if not sec:
        raise ApiError(404, "NOT_FOUND", "章节不存在")
    if not sec.get("node_key"):
        raise ApiError(409, "STATE_CONFLICT", "这一节没有对应的产出步骤，不能编辑")
    base_hash = sec.get("content_hash") or stable_hash(sec.get("content") or "")
    eid = new_id("sed")
    with db.Tx():
        db.ex("UPDATE section_edits SET status='superseded' WHERE task_id=? AND section_key=? AND status='active'",
              (task["id"], section_key))
        db.insert("section_edits", {"id": eid, "task_id": task["id"], "plan_version": pv["version"], "section_key": section_key,
                                    "user_id": user_id, "content": content, "base_hash": base_hash, "status": "active",
                                    "created_at": now_ms()})
    new_art = build(task, pv, store.load_nodes(pv["id"]))    # 立即重建成果：画布 / 导出 / 打包文件同步更新
    return {"edit_id": eid, "section_key": section_key, "chars": len(content), "artifact_id": new_art["id"]}


def build(task: dict, pv: dict, nodes: list[dict]) -> dict:
    order = graph.topo_order(nodes)
    by = {n["node_key"]: n for n in nodes}
    producers = [by[k] for k in order if by[k]["produces_sections"]]
    if not producers:                                     # 没有声明章节时，用汇点节点的输出
        sinks = {n["node_key"] for n in nodes} - {d for n in nodes for d in n["depends_on"]}
        producers = [by[k] for k in order if k in sinks]
    edits = active_edits(task["id"])                      # v1.8：用户手工编辑稿（最后写入者胜出）
    sections = []
    for n in producers:
        out = n.get("output") or {}
        title = (n["produces_sections"] or [n["title"]])[0]
        content, user_edited = merge_content(out.get("content", ""), edits.get(n["node_key"]), n.get("finished_at"))
        stale = [d for d in n.get("depends_on", []) if max(by[d].get("finished_at") or 0,
                  (edits.get(d) or {}).get("created_at") or 0) > (n.get("started_at") or n.get("finished_at") or 0)]
        sections.append({"section_key": f"{n['node_key']}", "title": title, "node_key": n["node_key"],
                         "execution_id": n.get("execution_id"), "content": content, "content_hash": stable_hash(content),
                         "citations": out.get("citations", []), "sources": out.get("sources", []),
                         "stale_dependencies": stale, "model": out.get("model"),
                         "user_edited": user_edited,
                         "edited_at": edits[n["node_key"]]["created_at"] if user_edited else None})
    title = db.j(pv["deliverable"], {}).get("title") or task["title"]
    missing = [{"node_key": n["node_key"], "title": n["title"], "status": n["status"],
                "reason": n.get("error_code") or "UPSTREAM_FAILED"}
               for n in nodes if n["status"] in ("failed", "skipped", "cancelled")]
    delivered = sum(bool(s["content"].strip()) for s in sections)
    quality = {"level": "empty" if not delivered else ("partial" if missing else "complete"),
               "delivered_sections": delivered, "expected_sections": len(sections), "missing": missing,
               "stale_sections": [s["section_key"] for s in sections if s.get("stale_dependencies")],
               "demo": any((n.get("output") or {}).get("demo") for n in nodes)}
    markdown = to_markdown(title, sections)
    if quality["level"] != "complete":
        en = task.get("output_language") == "en"
        label = ("No usable deliverable" if en else "未形成有效交付") if quality["level"] == "empty" else ("Partial delivery" if en else "部分交付")
        detail = "; ".join(f"{m['title']} ({m['reason']})" for m in missing)
        markdown = f"> {label}: {detail}\n\n" + markdown
    if quality["stale_sections"]:
        notice = "Some upstream results changed; related conclusions need review." if task.get("output_language") == "en" else "部分上游成果已修改，相关结论尚未重算，请核查。"
        markdown = f"> {notice}\n\n" + markdown
    if quality["demo"]:
        notice = "Includes demonstration model output or sample data." if task.get("output_language") == "en" else "包含演示模型输出或演示数据。"
        markdown = f"> {notice}\n\n" + markdown
    files = task_files(task, nodes)
    from . import workspace
    for file in files:
        frozen = workspace.snapshot(task["user_id"], task["id"], pv["version"], file["path"])
        if frozen:
            file["path"] = frozen
    for n in nodes:
        image = (n.get("output") or {}).get("image")
        if image:
            frozen = workspace.snapshot(task["user_id"], task["id"], pv["version"], image["path"])
            if frozen:
                n["output"] = {**n["output"], "image": {**image, "path": frozen}}
    packed = _pack_result({**task, "active_version": pv["version"]}, title, markdown)
    if packed:
        packed["node_key"] = _sink_key(nodes)                # 打包文件属于整份成果：挂在最后的汇点步骤上
    if packed and packed["path"] not in {f["path"] for f in files}:
        files = [packed] + files                             # 放最前：它是内容最完整的成果文件
    cards = assemble_cards(sections, nodes, files)
    art = {"id": new_id("art"), "task_id": task["id"], "plan_version": pv["version"], "title": title,
           "sections": sections, "cards": cards, "markdown": markdown, "quality": quality, "created_at": now_ms()}
    db.insert("artifacts", art)
    return art


def build_quick(task: dict, text: str, model: str, demo: bool = False) -> dict:
    sec = [{"section_key": "answer", "title": "回答" if task["output_language"] == "zh" else "Answer", "node_key": None,
            "execution_id": None, "content": text, "content_hash": stable_hash(text), "citations": [], "model": model,
            "user_edited": False, "edited_at": None}]
    art = {"id": new_id("art"), "task_id": task["id"], "plan_version": 0, "title": task["title"], "sections": sec,
           "cards": assemble_cards(sec, [], []), "markdown": to_markdown(task["title"], sec),
           "quality": {"level": "complete", "demo": demo}, "created_at": now_ms()}
    db.insert("artifacts", art)
    return art


def to_markdown(title: str, sections: list[dict]) -> str:
    """[[c:id]] 引用转为脚注编号，文末附出处列表。"""
    refs: dict[str, int] = {}
    lines = [f"# {title}", ""]
    for s in sections:
        body = s["content"]
        for c in re.findall(r"\[\[c:([\w-]+)\]\]", body):
            refs.setdefault(c, len(refs) + 1)
        body = re.sub(r"\[\[c:([\w-]+)\]\]", lambda m: f"[^{refs[m.group(1)]}]", body)
        lines += [f"## {s['title']}", "", body.strip(), ""]
    if refs:
        info = {c["chunk_id"]: c for s in sections for c in s.get("citations", [])}
        lines += ["---", ""]
        for cid, i in refs.items():
            c = info.get(cid, {})
            lines.append(f"[^{i}]: {c.get('doc_title', '知识库资料')}：{c.get('text', '')[:80]}")
    return "\n".join(lines).strip() + "\n"


def latest(task_id: str, version: int | None = None) -> dict | None:
    sql = "SELECT * FROM artifacts WHERE task_id=?" + (" AND plan_version=?" if version is not None else "") + " ORDER BY created_at DESC LIMIT 1"
    a = db.q1(sql, (task_id, version) if version is not None else (task_id,))
    if a:
        a["sections"] = db.j(a["sections"], [])
        a["cards"] = db.j(a.get("cards"), [])
        a["quality"] = db.j(a.get("quality"), {})
    return a


def archive(task: dict, section_keys: list[str] | None) -> dict:
    """成果经用户确认后沉淀回知识库（双向闭环）。"""
    a = latest(task["id"])
    if not a:
        raise ApiError(409, "STATE_CONFLICT", "任务还没有成果")
    if a.get("quality", {}).get("level") == "empty":
        raise ApiError(409, "EMPTY_DELIVERABLE", "当前没有有效交付内容，不能沉淀到知识库")
    secs = [s for s in a["sections"] if not section_keys or s["section_key"] in section_keys]
    text = "\n\n".join(f"## {s['title']}\n\n{re.sub(r'\[\[c:[\w-]+\]\]', '', s['content'])}" for s in secs if s["content"].strip())
    if a.get("quality", {}).get("level") == "partial":
        text = "> 部分交付 / Partial delivery\n\n" + text
    return kb.add_document(task["user_id"], a["title"], text, "task_result", ["任务成果"], task["id"])
