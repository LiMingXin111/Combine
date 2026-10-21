"""外网检索服务测试（v2.1）：解析器 / 融合去重 / 缓存 / SSRF 防护 / 工具契约。

全部离线：解析器用 tests/fixtures/web/ 中的人工编写响应样本，网络层被替换为读文件，
不产生任何真实网络请求；端到端夹具（WEB_SEARCH_MODE=fake）返回确定性数据。

运行：  python -m unittest tests/test_websearch.py -v      （在 combine/ 目录下）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="combine-ws-")
os.environ.update(DATA_DIR=_TMP, DB_PATH=os.path.join(_TMP, "combine.db"), COMBINE_IGNORE_DOTENV="1", FAKE_LLM="1", FAKE_LATENCY_SCALE="0.05",
                  DEPLOY_MODE="self_hosted", WEB_SEARCH_MODE="fake")
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.core import matching, policy  # noqa: E402
from app.services import kb, websearch as ws  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "web")


class _Fix(unittest.TestCase):
    """把模块的 HTTP 层替换为「读 fixture 文件」，并保证每个用例后恢复。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_get = ws._http_get

    def tearDown(self):
        ws._http_get = self._orig_get

    def use(self, fixture: str, status: int = 200):
        path = os.path.join(FIX, fixture)

        def _get(url, timeout=8, max_bytes=0, headers=None):
            with open(path, "rb") as f:
                return status, f.read().decode("utf-8", "replace"), url
        ws._http_get = _get


class Parsers(_Fix):
    def test_sogou_html(self):
        self.use("sogou.html")
        items = ws._eng_sogou("大模型")
        self.assertGreaterEqual(len(items), 5)
        self.assertTrue(all(it["url"].startswith("http") for it in items))
        self.assertTrue(any("sogou.com/link" in it["url"] for it in items))   # 跳转链被补全为绝对地址
        self.assertTrue(any(it["snippet"] for it in items))                  # 摘要被解析出来
        self.assertFalse(any("javascript:" in it["url"] for it in items))    # 伪链接被过滤

    def test_so360_html(self):
        self.use("so360.html")
        items = ws._eng_so360("大模型")
        self.assertGreaterEqual(len(items), 3)
        self.assertTrue(any(it["url"].startswith("http") for it in items))
        self.assertTrue(any(it["snippet"] for it in items))

    def test_weixin_html(self):
        self.use("weixin.html")
        items = ws._eng_weixin("大模型")
        self.assertGreaterEqual(len(items), 5)
        it = items[0]
        self.assertIn("weixin.sogou.com", it["url"])
        self.assertTrue(it.get("source_name"))                               # 公众号名
        self.assertRegex(it.get("published") or "", r"^\d{4}-\d{2}-\d{2}$")  # 时间戳解码为日期

    def test_bing_rss(self):
        self.use("bing_rss.xml")
        items = ws._eng_bing("python asyncio")
        self.assertGreaterEqual(len(items), 5)
        self.assertTrue(all(it["title"] and it["url"].startswith("http") for it in items))

    def test_gnews_rss(self):
        self.use("gnews.xml")
        items = ws._eng_gnews("AI regulation")
        self.assertGreaterEqual(len(items), 5)
        self.assertTrue(items[0].get("source_name"))
        self.assertTrue(items[0].get("published"))

    def test_bing_news_rss(self):
        self.use("bing_news.xml")
        items = ws._eng_bing_news("artificial intelligence")
        self.assertGreaterEqual(len(items), 5)

    def test_openalex_json(self):
        self.use("openalex.json")
        items = ws._eng_openalex("retrieval augmented generation")
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(items[0]["url"].startswith("http"))
        self.assertIn("被引", items[0]["snippet"])                           # 引用数进入摘要

    def test_arxiv_atom(self):
        self.use("arxiv.xml")
        items = ws._eng_arxiv("retrieval augmented generation")
        self.assertGreaterEqual(len(items), 2)
        self.assertIn("arxiv.org/abs/", items[0]["url"])
        self.assertRegex(items[0].get("published") or "", r"^\d{4}-\d{2}-\d{2}$")

    def test_crossref_json(self):
        self.use("crossref.json")
        items = ws._eng_crossref("retrieval augmented generation")
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(items[0]["title"])

    def test_github_json(self):
        self.use("github.json")
        items = ws._eng_github("rag")
        self.assertGreaterEqual(len(items), 2)
        self.assertIn("★", items[0]["snippet"])                              # star 数进入摘要

    def test_hn_json(self):
        self.use("hn.json")
        items = ws._eng_hn("rag")
        self.assertGreaterEqual(len(items), 3)
        self.assertTrue(all(it["url"].startswith("http") for it in items))

    def test_stackexchange_json(self):
        self.use("stackexchange.json")
        items = ws._eng_so("rag")
        self.assertGreaterEqual(len(items), 3)
        self.assertIn("answers", items[0]["snippet"])

    def test_engine_failure_is_raised(self):
        self.use("bing_rss.xml", status=502)
        with self.assertRaises(RuntimeError):
            ws._eng_bing("x")


class Fusion(unittest.TestCase):
    def test_url_normalization(self):
        a = ws._url_key("https://www.Example.com/Path/?utm_source=wx&b=2&a=1#frag")
        b = ws._url_key("https://example.com/Path?a=1&b=2")
        self.assertEqual(a, b)                                               # utm / fragment / 尾斜杠 / www 归一
        self.assertNotEqual(ws._url_key("https://a.com/x"), ws._url_key("https://b.com/x"))

    def test_rrf_merge_dedupe_and_engines(self):
        lists = [
            [{"title": "同一篇文章标题足够长", "url": "https://a.com/t?utm_source=x", "snippet": "短", "engine": "sogou"},
             {"title": "B 文章标题", "url": "https://b.com/1", "snippet": "s2", "engine": "sogou"}],
            [{"title": "同一篇文章标题足够长", "url": "https://a.com/t/", "snippet": "更长的摘要内容", "engine": "so360"},
             {"title": "C 文章标题", "url": "https://c.com/2", "snippet": "s3", "engine": "so360"}],
        ]
        out = ws._fuse(lists, 10)
        self.assertEqual(len(out), 3)                                        # 跨引擎重复合并
        top = out[0]
        self.assertEqual(sorted(top["engines"]), ["so360", "sogou"])         # 来源引擎合并
        self.assertEqual(top["snippet"], "更长的摘要内容")                    # 取更长摘要
        self.assertGreater(top["score"], out[1]["score"])                    # 双引擎命中排更前

    def test_rrf_title_level_dedupe(self):
        lists = [[{"title": "完全一样的长标题新闻", "url": "https://x.com/a", "snippet": "", "engine": "gnews"}],
                 [{"title": "完全一样的长标题新闻", "url": "https://y.com/b", "snippet": "", "engine": "bing_news"}]]
        self.assertEqual(len(ws._fuse(lists, 10)), 1)

    def test_variants_dedupe_and_cap(self):
        v = ws._variants({"query": "A", "queries": ["a", "B", "C", "D"]})
        self.assertEqual(v, ["A", "B", "C"])                                 # 去重 + 上限 3

    def test_engine_selection_by_language(self):
        self.assertEqual(ws._general_engines("大语言模型"), ["sogou", "so360"])
        self.assertTrue("bing" in ws._general_engines("large language model"))


class HttpAndSafety(unittest.TestCase):
    def test_html_to_text(self):
        h = ("<html><head><title>T</title><script>bad()</script></head><body>"
             "<h1>标题</h1><p>第一段&nbsp;文本</p><p>第二段</p></body></html>")
        t = ws._html_to_text(h)
        self.assertIn("标题", t)
        self.assertIn("第一段 文本", t)
        self.assertNotIn("bad()", t)                                         # script 被剥离

    def test_ssrf_blocks_private_hosts(self):
        for host in ("127.0.0.1", "localhost", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", ""):
            self.assertFalse(ws._public_host(host), host)
        self.assertTrue(ws._public_host("example.com"))

    def test_fetch_rejects_private_and_bad_scheme(self):
        # 直测抓取器本身（绕过 fake 模式的短路），不发任何真实请求：私网地址在校验阶段即被拒
        r = ws._run_fetch({"url": "http://127.0.0.1:8765/"})
        self.assertEqual(r["error"], "BLOCKED")
        r2 = ws._run_fetch({"url": "file:///etc/passwd"})
        self.assertEqual(r2["error"], "BAD_URL")
        r3 = ws._run_fetch({"url": "http://169.254.169.254/latest/meta-data/"})
        self.assertEqual(r3["error"], "BLOCKED")                             # 云元数据地址

    def test_strip_tags_removes_em_highlight(self):
        self.assertEqual(ws._strip_tags("<em>大模型</em>是什么"), "大模型是什么")   # 高亮标签不留空格


class RunAndMode(unittest.TestCase):
    def test_fake_mode_returns_fixtures_and_never_networks(self):
        self.assertEqual(ws.mode(), "fake")
        r = ws.run("web.search", {"query": "任意"}, enabled=True)
        self.assertGreaterEqual(len(r["items"]), 3)
        self.assertEqual(r.get("note"), "offline demo data")
        f = ws.run("web.fetch", {"url": "https://example.com"}, enabled=True)
        self.assertEqual(f["error"], "TOOL_DISABLED")                        # fake 模式不出网

    def test_disabled_switch(self):
        r = ws.run("web.search", {"query": "x"}, enabled=False)
        self.assertEqual(r["error"], "TOOL_DISABLED")

    def test_unknown_tool_denied(self):
        self.assertEqual(ws.run("web.unknown", {}, enabled=True)["error"], "TOOL_DENIED")

    def test_search_pipeline_with_mocked_engines(self):
        """mock 三个引擎：验证任务构造（按语言选引擎）、失败引擎记录、融合输出与缓存。"""
        calls = []

        def mk(name, n):
            def fn(q):
                calls.append((name, q))
                return [{"title": f"{name} 关于 {q} 的结果标题", "url": f"https://{name}.example/{n(q)}",
                         "snippet": "s", "engine": name}]
            return fn
        orig = dict(ws._ENGINES)
        ws._ENGINES.update({"sogou": mk("sogou", lambda q: len(q)),
                            "so360": mk("so360", lambda q: len(q) + 7),
                            "bing": mk("bing", lambda q: 1)})
        ws._CACHE.clear()
        try:
            r = ws._run_search("web.search", {"query": "大模型 调研", "queries": ["large model survey"]})
            self.assertIn("sogou", r["engines"])
            self.assertTrue(all(c[0] in ("sogou", "so360") for c in calls if "大模型" in c[1]))   # 中文 → 不加 bing
            self.assertTrue(any(c[0] == "bing" for c in calls))              # 英文变体 → bing 参与
            self.assertTrue(r["items"])
            r2 = ws._run_search("web.search", {"query": "大模型 调研", "queries": ["large model survey"]})
            self.assertTrue(r2.get("cached"))                                # 二次调用命中缓存
        finally:
            ws._ENGINES.clear()
            ws._ENGINES.update(orig)
            ws._CACHE.clear()

    def test_all_engines_failed_reports_gracefully(self):
        def boom(q):
            raise RuntimeError("blocked")
        orig = dict(ws._ENGINES)
        ws._ENGINES.update({"sogou": boom, "so360": boom, "bing": boom})
        ws._CACHE.clear()
        try:
            r = ws._run_search("web.search", {"query": "中文查询"})
            self.assertEqual(r["error"], "NO_RESULT")
            self.assertTrue(r["engines_failed"])                             # 失败引擎被记录
        finally:
            ws._ENGINES.clear()
            ws._ENGINES.update(orig)
            ws._CACHE.clear()


class ToolContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 与 test_units.py 同进程跑时共用一个 config：显式改写 DB 路径到本文件的临时目录，
        # 避免 test_units 清理其 _TMP 之后这里的连接指向已删除目录。
        from app import config, db
        config.DB_PATH = os.path.join(_TMP, "websearch.db")
        db.reset_connection()

    def test_schemas_and_registry_cover_web_tools(self):
        for t in ws.TOOLS:
            self.assertIn(t, kb.TOOL_SCHEMAS, t)
            self.assertIn(t, matching.TOOL_REGISTRY, t)
            self.assertEqual(matching.TOOL_REGISTRY[t]["risk"], "read")
        self.assertIn("queries", kb.TOOL_SCHEMAS["web.search"]["parameters"]["properties"])
        self.assertIn("freshness", kb.TOOL_SCHEMAS["web.news"]["parameters"]["properties"])

    def test_prompt_tools_mentions_web_channels(self):
        s = {"knowledge.auto_search": True}
        line = policy.prompt_tools(s)
        self.assertIn("web.search", line)
        self.assertIn("web.fetch", line)

    def test_seed_capabilities_merge_adds_new_tools(self):
        """升级合并：老库中 research 能力缺 web.scholar/web.fetch 时会被补齐，但不删除已有工具。"""
        from app import db
        matching.seed_capabilities()                                         # 首次播种
        db.update("capabilities", {"key": "research"}, {"tools": ["kb.search", "custom.keep"]})
        matching.seed_capabilities()                                         # 再次播种 → 只增不减地合并
        row = db.q1("SELECT tools FROM capabilities WHERE key='research'")
        tools = db.j(row["tools"], [])
        self.assertIn("custom.keep", tools)                                  # 自定义工具不被删除
        self.assertIn("web.scholar", tools)
        self.assertIn("web.fetch", tools)


if __name__ == "__main__":
    unittest.main()
