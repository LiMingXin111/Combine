"""端到端测试：启动真实后端（FakeLLM、临时数据库），通过 HTTP 验证核心流程与系统不变量。

运行：  python -m unittest tests/test_e2e.py -v      （在 combine/ 目录下）
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, data_dir=None, **env):
        self.dir = data_dir or tempfile.mkdtemp(prefix="combine-test-")
        self.connections = []
        self.port = free_port()
        e = {**os.environ, "DATA_DIR": self.dir, "FAKE_LLM": "1", "FAKE_LATENCY_SCALE": "0.05", "PYTHONIOENCODING": "utf-8",
             "WEB_SEARCH_MODE": "fake", "COMBINE_IGNORE_DOTENV": "1", "DB_PATH": os.path.join(self.dir, "combine.db"), **env}
        executable = sys.executable
        if os.name == "nt":
            # Bypass the Windows venv launcher so terminate/wait tracks the actual server.
            executable = getattr(sys, "_base_executable", sys.executable)
            e["PYTHONPATH"] = os.pathsep.join(sys.path)
        self.proc = subprocess.Popen([executable, "-m", "uvicorn", "app.main:app", "--app-dir", os.path.join(ROOT, "backend"),
                                      "--port", str(self.port), "--log-level", "warning"], env=e, cwd=ROOT)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/readyz", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self, keep=False):
        self.proc.terminate()
        self.proc.wait(10)
        for connection in self.connections:
            connection.close()
        if not keep:
            shutil.rmtree(self.dir, ignore_errors=True)

    def db(self):
        connection = sqlite3.connect(os.path.join(self.dir, "combine.db"))
        self.connections.append(connection)
        return connection


class Client:
    def __init__(self, srv: Server):
        self.base = f"http://127.0.0.1:{srv.port}"
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, data=None, expect=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", "X-Combine": "1"})
        try:
            with self.op.open(req, timeout=20) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        out = json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw.decode("utf-8", "ignore")
        if expect is not None:
            assert status == expect, (method, path, status, out)
        return status, out

    def get(self, p, **k):
        return self.call("GET", p, **k)[1]

    def post(self, p, d=None, **k):
        return self.call("POST", p, d if d is not None else {}, **k)[1]

    def wait(self, task_id, pred, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            st = self.get(f"/api/tasks/{task_id}/state")
            if pred(st):
                return st
            time.sleep(0.15)
        raise AssertionError(f"timeout; phase={st['phase']} nodes={[(n['node_key'], n['status']) for n in st['nodes']]}")

    def events(self, task_id):
        return self.get(f"/api/tasks/{task_id}/events?limit=5000")["items"]


def node(st, key):
    return next(n for n in st["nodes"] if n["node_key"] == key)


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()
        cls.c = Client(cls.srv)
        cls.c.post("/api/auth/register", {"email": "a@test.io", "password": "password1"}, expect=200)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def plan(self, goal, c=None, **kw):
        c = c or self.c
        t = c.post("/api/tasks", {"goal": goal, **kw}, expect=202)["task"]
        st = c.wait(t["id"], lambda s: s["task"]["status"] in ("running", "completed", "failed"))
        return t["id"], st

    def test_01_quick_lane(self):
        tid, st = self.plan("你好")
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 30)
        self.assertEqual(st["task"]["lane"], "quick")
        art = self.c.get(f"/api/tasks/{tid}/artifact")
        self.assertIn("快速回答", art["sections"][0]["content"])
        self.assertEqual([c["kind"] for c in art["cards"]], ["doc"])          # 快速回答也以卡片形态交付

    def test_02_full_flow_auto_run_and_revision(self):
        tid, st = self.plan("帮我规划一个杭州两日游，同行有老人，少走路")
        self.assertEqual(st["task"]["status"], "running")                   # 计划生成后自动开跑：无需确认，更无需批准
        est = st["estimate"]
        self.assertLess(est["makespan_s"], est["serial_s"])                 # 并行确实缩短了总时长
        self.assertTrue(node(st, "review")["redundant_edges"])              # gather→review 被传递约简
        self.assertTrue(all(n["agent"].get("name") for n in st["nodes"]))   # 每个步骤都匹配到 Agent
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        confirmed = next(e for e in self.c.events(tid) if e["type"] == "plan.confirmed")
        self.assertTrue(confirmed["payload"]["auto"])                       # 自动确认，而非用户点击
        types = [e["type"] for e in self.c.events(tid)]
        self.assertNotIn("approval.requested", types)                       # 全流程没有任何人工审批点
        self.assertNotIn("node.output_ready", types)
        art = self.c.get(f"/api/tasks/{tid}/artifact")
        self.assertGreaterEqual(len(art["sections"]), 3)
        self.__class__.done_task = tid

        # 修订：评论「行程路线」→ 新版本，只重算它与下游（修订后同样自动跑完）
        self.c.post(f"/api/tasks/{tid}/comments", {"node_key": "part1", "body": "第二天下午太赶了"}, expect=200)
        prev = self.c.post(f"/api/tasks/{tid}/revisions/preview", {"node_key": "part1", "instructions": "x"})
        self.assertEqual(set(prev["affected"]), {"part1", "synth", "deliver", "review"})   # deliver 是 synth 的下游：打包文件随成果重建
        r = self.c.post(f"/api/tasks/{tid}/comments/apply", {}, expect=202)
        self.assertEqual(r["version"], 2)
        self.assertIn("req", r["reused"])
        self.assertIn("part2", r["reused"])
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == 2, 60)
        v2_started = [e["node_key"] for e in self.c.events(tid) if e["type"] == "node.started" and e["plan_version"] == 2]
        self.assertNotIn("req", v2_started)                                # INV-05：未受影响的不重跑
        self.assertNotIn("part2", v2_started)
        self.assertIn("part1", v2_started)

        # 章节重写只改目标章节（INV-17）
        a1 = {s["section_key"]: s["content_hash"] for s in self.c.get(f"/api/tasks/{tid}/artifact")["sections"]}
        self.c.post(f"/api/tasks/{tid}/sections/part2/rewrite", {"instructions": "改成表格"}, expect=202)
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == 3, 60)
        a2 = {s["section_key"]: s["content_hash"] for s in self.c.get(f"/api/tasks/{tid}/artifact")["sections"]}
        self.assertNotEqual(a1["part2"], a2["part2"])
        self.assertEqual({k: v for k, v in a1.items() if k != "part2"}, {k: v for k, v in a2.items() if k != "part2"})

        # 回放只读（INV-06）
        evs = self.c.events(tid)
        n_calls = self.srv.db().execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        rp = self.c.get(f"/api/tasks/{tid}/replay?seq={evs[len(evs) // 2]['seq']}")
        self.assertTrue(rp["readonly"])
        self.assertEqual(n_calls, self.srv.db().execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])

        # 布局不改语义（INV-22）；v1.6：成果节点的视图状态（隐藏 / 固定 / 自动聚焦）也随布局保存
        # v3.0：L2 本地操作（勾选 / 排序）同样是纯视图状态，与 hidden/pinned/focus 同处持久化、同样不产生事件
        n_ev = len(self.c.events(tid))
        self.c.call("PUT", f"/api/tasks/{tid}/layout", {"positions": {"req": {"x": 10, "y": 20}}, "viewport": {"x": 0, "y": 0, "zoom": 0.8},
                                                        "art": {"hidden": ["a:d:part1"], "pinned": ["a:f:xyz"], "focus": False,
                                                                "checks": {"a:l:synth": [0, 2]}, "sorts": {"a:t:item1": [1, "desc"]}}}, expect=200)
        self.assertEqual(n_ev, len(self.c.events(tid)))
        lay = self.c.get(f"/api/tasks/{tid}/layout")
        self.assertEqual(lay["positions"]["req"]["x"], 10)
        self.assertEqual(lay["art"], {"hidden": ["a:d:part1"], "pinned": ["a:f:xyz"], "focus": False,
                                      "checks": {"a:l:synth": [0, 2]}, "sorts": {"a:t:item1": [1, "desc"]}})

    def test_02b_manual_section_edit_and_ai_reads_it(self):
        """v1.8：用户可直接修改成果；修改之后的新对话（重算）以用户的修改为基础继续。"""
        tid, _ = self.plan("帮我规划一个杭州两日游，同行有老人，少走路")
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        art = self.c.get(f"/api/tasks/{tid}/artifact")
        edit_text = "第二天改为西湖慢游 + 灵隐寺，下午四点前结束，全程少台阶。"
        n_calls = self.srv.db().execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        # 人工编辑：只在成果层面生效——不调用模型、不新建计划版本
        r = self.c.call("PATCH", f"/api/tasks/{tid}/sections/part1", {"content": edit_text}, expect=200)
        self.assertEqual(r[1]["chars"], len(edit_text))
        self.assertEqual(self.srv.db().execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], n_calls)
        art2 = self.c.get(f"/api/tasks/{tid}/artifact")
        s1 = next(s for s in art2["sections"] if s["section_key"] == "part1")
        self.assertEqual(s1["content"], edit_text)
        self.assertTrue(s1["user_edited"] and s1["edited_at"])
        self.assertEqual(art2["plan_version"], art["plan_version"])         # 编辑不改计划版本
        self.assertIn(edit_text, art2["markdown"])                          # 导出全文同步更新
        doc = next(c for c in art2["cards"] if c["kind"] == "doc" and c["section_key"] == "part1")
        self.assertTrue(doc["user_edited"])                                 # 卡片带「已修改」标记
        self.assertTrue(any(e["type"] == "artifact.edited" for e in self.c.events(tid)))
        packed = next(c for c in art2["cards"] if c["kind"] == "file" and c["path"].endswith(".md"))
        body = self.c.get(f"/api/workspace/file?path={urllib.parse.quote(packed['path'])}")   # 打包文件重写为修改后内容
        self.assertIn(edit_text, body["content"])
        # 修改之后的新对话：重算 part1 → 用户的修改稿作为上下文注入，模型在此基础上继续
        self.c.post(f"/api/tasks/{tid}/revisions", {"instructions": {"part1": ["再压缩到半天"]}}, expect=202)
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == art["plan_version"] + 1, 60)
        art3 = self.c.get(f"/api/tasks/{tid}/artifact")
        s1b = next(s for s in art3["sections"] if s["section_key"] == "part1")
        self.assertIn("西湖慢游", s1b["content"])                           # 模型读到了用户的修改（FakeLLM 把注入的编辑稿写进正文）
        self.assertIn("再压缩到半天", s1b["content"])                        # 以及新的要求
        self.assertFalse(s1b["user_edited"])                                # 模型新稿已吸收用户的修改
        h2 = {s["section_key"]: s["content_hash"] for s in art2["sections"]}
        h3 = {s["section_key"]: s["content_hash"] for s in art3["sections"]}
        self.assertEqual(h3["part2"], h2["part2"])                          # 未受影响的章节逐字不变
        v2_started = [e["node_key"] for e in self.c.events(tid) if e["type"] == "node.started" and e["plan_version"] == art["plan_version"] + 1]
        self.assertIn("part1", v2_started)                                  # 被编辑的步骤重算
        self.assertNotIn("part2", v2_started)                               # 未受影响的步骤不重跑（INV-05）

    def test_03_concurrency_limit_and_fan_out(self):
        goal = "调研并对比 Notion、飞书、语雀、石墨四款笔记工具"
        # 扇出优化在任务跑完后以「修订」的形式应用：只重算受影响的步骤，其余沿用已有结果
        tid, st = self.plan(goal)
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        fan = [o for o in st["optimizations"] if o["kind"] == "fan_out"]
        self.assertTrue(fan, st["optimizations"])                           # 优化器给出扇出建议
        self.c.post(f"/api/tasks/{tid}/plan/optimizations/{fan[0]['id']}/apply", expect=200)
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == 2
                    and len([n for n in s["nodes"] if n["node_key"].startswith("items_")]) == 4, 60)
        self.assertEqual(self.peak(tid, 2), 4)                              # 默认并发上限 4，4 个并列步骤同时执行
        # 并发上限 2：把同一组扇出 ops 应用到同样计划的新任务，峰值不得超过 2（INV-02）
        # （并发上限低时优化器自己就不再推荐扇出，所以这里直接以修订形式下发同一组 ops）
        self.c.call("PATCH", "/api/settings", {"execution.max_concurrency": 2}, expect=200)
        tid2, st2 = self.plan(goal)
        self.c.wait(tid2, lambda s: s["task"]["status"] == "completed", 60)
        self.c.post(f"/api/tasks/{tid2}/revisions", {"ops": fan[0]["ops"], "reason": "optimization"}, expect=202)
        self.c.wait(tid2, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == 2, 60)
        self.assertEqual(self.peak(tid2, 2), 2)
        self.c.call("PATCH", "/api/settings", {"execution.max_concurrency": 4}, expect=200)
        # 关键路径优先：第一批派发的步骤中包含 rank 最大的就绪步骤
        tid3, _ = self.plan("帮我写一份关于城市骑行的分析报告")
        self.c.wait(tid3, lambda s: s["task"]["status"] == "completed", 60)
        st3 = self.c.get(f"/api/tasks/{tid3}/state")
        first = [e["node_key"] for e in self.c.events(tid3) if e["type"] == "node.started"]
        roots = sorted([n for n in st3["nodes"] if not n["depends_on"]], key=lambda n: -n["rank"])
        self.assertEqual(first[0], roots[0]["node_key"])

    def peak(self, tid, version=None):
        running, peak = 0, 0
        for e in self.c.events(tid):                                       # 按事件重放并发峰值（可限定计划版本）
            if version and e.get("plan_version") != version:
                continue
            if e["type"] == "node.started":
                running += 1
                peak = max(peak, running)
            elif e["type"] in ("node.succeeded", "node.failed"):
                running -= 1
        return peak

    def test_04_isolation_and_locked_settings(self):
        tid = self.c.post("/api/tasks", {"goal": "你好呀"}, expect=202)["task"]["id"]
        other = Client(self.srv)
        other.post("/api/auth/register", {"email": "b@test.io", "password": "password2"}, expect=200)
        other.call("GET", f"/api/tasks/{tid}/state", expect=404)          # INV-08
        s, out = self.c.call("PATCH", "/api/settings", {"safety.replay_readonly": False})
        self.assertEqual((s, out["error"]["code"]), (400, "SETTING_LOCKED"))   # INV-19
        s, out = self.c.call("PATCH", "/api/settings", {"approval.ask_dangerous": "yes"})
        self.assertEqual((s, out["error"]["code"]), (400, "SETTING_INVALID"))  # 安全防护开关只接受布尔值
        cur = self.c.get("/api/settings")["values"]["execution.task_budget_cny"]
        self.assertEqual(self.c.call("PATCH", "/api/settings", {"execution.task_budget_cny": 5})[0], 200)   # 预算可改，但只用于展示
        self.c.call("PATCH", "/api/settings", {"execution.task_budget_cny": cur}, expect=200)               # 复原，避免影响后续用例
        s, out = Client(self.srv).call("POST", "/api/tasks", {"goal": "x"})
        self.assertEqual(s, 401)
        req = urllib.request.Request(self.c.base + "/api/tasks", data=b'{"goal":"x"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.c.op.open(req)
        self.assertEqual(cm.exception.code, 403)                           # CSRF 头缺失

    def test_05_byok_key_never_leaks_and_substitution(self):
        key = "fake-SECRET-abcdef-987654"
        p = self.c.post("/api/providers", {"kind": "fake", "api_key": key}, expect=200)["provider"]
        self.assertNotIn(key, json.dumps(p))
        t = self.c.post(f"/api/providers/{p['id']}/test", {}, expect=200)
        self.assertTrue(t["ok"] and t["tools"])
        no_tools = next(m for m in p["models"] if m["model_name"] == "fake-no-tools")
        tid, st = self.plan("帮我写一份关于远程办公效率的分析报告")
        self.c.post(f"/api/tasks/{tid}/revisions",
                    {"ops": [{"op": "set_model", "node_key": "gather", "model_ref": no_tools["ref"]}], "reason": "model_override"}, expect=202)
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        subs = [e for e in self.c.events(tid) if e["type"] == "model.substituted"]
        self.assertTrue(subs and subs[0]["node_key"] == "gather")          # INV-20：需要工具 → 显式替换
        for path in ("/api/providers", "/api/me/export", f"/api/tasks/{tid}/events?limit=5000", f"/api/tasks/{tid}/state"):
            self.assertNotIn(key, json.dumps(self.c.get(path), ensure_ascii=False))   # INV-18
        con = self.srv.db()
        for (tbl,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in con.execute(f"SELECT * FROM {tbl}").fetchall():
                self.assertNotIn(key, " ".join(map(str, row)), tbl)

    def test_06_question_does_not_block_and_answer_triggers_revision(self):
        """待答问题只随成果提示，不再阻断下游：任务照常跑完；回答后自动重算受影响步骤并跑完。"""
        tid, st = self.plan("帮我规划一次露营活动 [q]")
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        self.assertTrue(all(node(st, k)["status"] == "succeeded" for k in ("req", "part1", "part2", "synth")))
        q = next(a for a in st["attention"] if a["kind"] == "open_question")
        self.assertEqual(q["severity"], "warn")                             # 提示级，不再是 block
        r = self.c.post(f"/api/attention/{q['id']}/answer", {"answer": "500 元"}, expect=202)
        self.assertEqual(r["version"], 2)
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == 2, 60)
        self.assertEqual(node(st, "req")["status"], "succeeded")            # 回答后自动重算并跑完

    def test_07_failure_auto_retry_and_cost_is_display_only(self):
        tid, st = self.plan("帮我规划一个周末学习计划 [fail]")
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        self.assertIn("node.retrying", [e["type"] for e in self.c.events(tid)])   # 失败先自动重试，不打扰用户
        self.assertTrue(all(n["status"] == "succeeded" for n in st["nodes"]))
        # 成本只做展示：预算小到 0.001 元也不会被拦截、暂停或失败（旧的「规划前拦截」「超限前暂停」路径已删除）
        tid2, st = self.plan("帮我规划一次家庭聚会的安排", budget_cny=0.001)
        st = self.c.wait(tid2, lambda s: s["task"]["status"] == "completed", 60)
        self.assertFalse(st["task"]["paused"])
        self.assertNotIn("budget.exceeded", [e["type"] for e in self.c.events(tid2)])

    def test_08_kb_search_and_citation(self):
        self.c.post("/api/kb/documents/note", {"title": "杭州攻略", "text": "## 灵隐寺\n\n灵隐寺建议上午 8 点前到达，门票 75 元。\n\n## 西湖\n\n西湖游船单程约 45 分钟。"}, expect=200)
        r = self.c.post("/api/kb/search", {"query": "灵隐寺 门票"})
        self.assertTrue(r["items"] and "灵隐寺" in r["items"][0]["text"])
        other = Client(self.srv)
        other.post("/api/auth/register", {"email": "c@test.io", "password": "password3"}, expect=200)
        self.assertEqual(other.post("/api/kb/search", {"query": "灵隐寺"})["items"], [])   # 知识库按用户隔离
        tid, st = self.plan("规划杭州灵隐寺和西湖的一日游")
        st = self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        cites = [c for n in st["nodes"] for c in ((n.get("output") or {}).get("citations") or [])]
        self.assertTrue(cites, [(n["node_key"], n["tools"], (n.get("output") or {}).get("content", "")[:200]) for n in st["nodes"]])  # INV-11

    def test_09_cards_and_packaged_file_download(self):
        """v1.5：成果以「地图 / 图 / 文 / 文件」四类卡片交付；系统保底打包的成果全文可下载，路径逃逸被拒。"""
        tid, st = self.plan("帮我规划一个杭州两日游，同行有老人，少走路")
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        art = self.c.get(f"/api/tasks/{tid}/artifact")
        cards = art["cards"]
        kinds = [c["kind"] for c in cards]
        self.assertTrue({"map", "chart", "doc", "file"} <= set(kinds), kinds)   # 四类卡片齐全（v1.9 起含地图）
        self.assertEqual(kinds, sorted(kinds, key={"map": -1, "chart": 0, "doc": 1, "file": 2}.get))   # 顺序：地图 → 图 → 文 → 文件
        # v1.9：旅行任务产出的攻略地图（坐标来自内置地标库，两天行程、按顺序连线）
        mp = next(c for c in cards if c["kind"] == "map")
        self.assertEqual(mp["node_key"], "part1")                             # 地图挂在「行程路线」步骤
        marks = mp["map"]["markers"]
        self.assertEqual(len(marks), 8)
        self.assertTrue(all(isinstance(m["lat"], (int, float)) and isinstance(m["lng"], (int, float)) for m in marks))
        self.assertTrue(all(29.9 < m["lat"] < 30.5 and 119.9 < m["lng"] < 120.5 for m in marks))   # 杭州范围内（GCJ-02）
        self.assertEqual({m["day"] for m in marks}, {1, 2})                   # 两天行程分组
        self.assertEqual(marks[0]["name"], "断桥残雪")                        # 数组顺序即游览顺序
        self.assertFalse([c for c in cards if c["kind"] == "map" and c.get("node_key") != "part1"])   # 只有行程步骤出地图
        docs = [c for c in cards if c["kind"] == "doc"]
        self.assertEqual([c["section_key"] for c in docs], [s["section_key"] for s in art["sections"]])   # 文卡与章节一一对应
        self.assertTrue(all(c["chars"] > 0 and c["excerpt"] for c in docs))   # 文卡带摘要与字数
        charts = [c for c in cards if c["kind"] == "chart"]
        self.assertIn("pie", [c["chart"]["type"] for c in charts])            # travel domain：预算核对 → 饼图
        self.assertTrue(all(c["chart"]["labels"] and c["chart"]["series"] for c in charts))
        fc = next(c for c in cards if c["kind"] == "file")
        self.assertTrue(fc["path"].startswith("交付/") and fc["ext"] == "md")  # 系统保底打包的成果全文（不依赖模型行为）
        self.assertGreater(fc["bytes"], 0)
        self.assertTrue(fc.get("node_key"))                                   # v1.6：文件卡带产出节点（打包件挂在最后汇点），画布成果节点据此连线
        self.assertNotIn("drafts/", str(cards))                               # 草稿目录不进文件卡
        s, body = self.c.call("GET", "/api/workspace/file/download?path=" + urllib.parse.quote(fc["path"]))
        self.assertEqual((s, art["title"] in body), (200, True))              # 下载到的就是成果全文
        s, _ = self.c.call("GET", "/api/workspace/file/download?path=" + urllib.parse.quote("../combine.db"))
        self.assertEqual(s, 404)                                              # 路径逃逸被拒
        # v1.6：历史版本的成果可单独导出（节点管理模块的「历史工作流」用）；不存在的版本 404
        s, body1 = self.c.call("GET", f"/api/tasks/{tid}/artifact/export?format=md&version={art['plan_version']}")
        self.assertEqual((s, art["title"] in body1), (200, True))
        self.assertEqual(self.c.call("GET", f"/api/tasks/{tid}/artifact/export?format=md&version=999")[0], 404)
        # 成果版本迭代后卡片同步更新（修订会重建 artifact）
        self.c.post(f"/api/tasks/{tid}/sections/{docs[0]['section_key']}/rewrite", {"instructions": "精简"}, expect=202)
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed" and s["plan_version"] == 2, 60)
        art2 = self.c.get(f"/api/tasks/{tid}/artifact")
        self.assertNotEqual(art["id"], art2["id"])
        self.assertTrue({"chart", "doc", "file"} <= {c["kind"] for c in art2["cards"]})

    def test_10_web_search_multi_engine(self):
        """v2.1：外网检索工具链——默认开启；gather 步骤调用多引擎 web.search（含查询变体）；
        结果以带链接的「网络来源」写入成果正文；关闭设置后模型侧不再拿到 web.* 工具。
        本用例在 WEB_SEARCH_MODE=fake 下运行（确定性夹具，不产生真实网络请求）。"""
        self.assertIs(self.c.get("/api/settings")["values"]["tools.web_search"], True)     # 默认开启
        tid, st = self.plan("调研并对比 Notion 与 Obsidian 两款笔记工具的优缺点")
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 90)
        ev = self.c.events(tid)
        called = [e for e in ev if e["type"] == "tool.called" and e["payload"]["tool"] == "web.search"]
        self.assertTrue(called, [(e["type"], e["payload"]) for e in ev if e["type"].startswith("tool.")][:8])
        self.assertTrue(called[0]["payload"]["args"].get("queries"))          # 查询变体（中英双语并行检索）被使用
        results = [e for e in ev if e["type"] == "tool.result" and e["payload"]["tool"] == "web.search"]
        self.assertTrue(results and results[0]["payload"]["items"] >= 3)      # 多引擎融合返回条目
        gather = node(self.c.get(f"/api/tasks/{tid}/state"), "gather")
        body = (gather.get("output") or {}).get("content") or ""
        self.assertIn("网络来源", body)                                        # 来源行进入步骤正文
        self.assertIn("http", body)                                           # 且带可点击链接
        # 关闭开关 → web.* 从模型侧白名单消失（不会出现「工具被禁用」的空转）
        self.c.call("PATCH", "/api/settings", {"tools.web_search": False}, expect=200)
        try:
            tid2, _ = self.plan("写一份三条远程办公效率建议")
            self.c.wait(tid2, lambda s: s["task"]["status"] == "completed", 90)
            ev2 = self.c.events(tid2)
            web_events = [e for e in ev2 if e["type"] == "tool.called" and str(e["payload"]["tool"]).startswith("web.")]
            self.assertFalse(web_events, web_events)                          # 关闭后不给模型，也不会撞禁用
        finally:
            self.c.call("PATCH", "/api/settings", {"tools.web_search": True}, expect=200)   # 复原，避免影响后续用例


    def test_11_functional_artifact_nodes(self):
        """v3.0 M3/M4：十类功能性成果节点端到端——节点输出经 clean_<kind> 校验后组装成卡片，
        画布据此直显（表格 / 指标 / 清单 / 代码 / 图片 / 时间线 / 日历 / 状态 / 会话 / 网页）。"""
        tid, _ = self.plan("分析华东与华南两个区域的运营数据。整理成周报 [artifacts]")
        self.c.wait(tid, lambda s: s["task"]["status"] == "completed", 90)
        art = self.c.get(f"/api/tasks/{tid}/artifact")
        kinds = [c["kind"] for c in art["cards"]]
        want = ["table", "metrics", "list", "timeline", "calendar", "image", "code", "web", "status", "chat"]
        self.assertTrue(set(want) <= set(kinds), kinds)                       # 十类齐全
        order = [k for k in kinds if k in want]
        self.assertEqual(order, sorted(order, key=want.index))                # 顺序与 CARD_ORDER 一致（数据类在前、叙述类在后）
        by = {c["kind"]: c for c in art["cards"]}
        # 表格：列定义 / 行数 / 真实总量 / 数字右对齐
        tb = by["table"]["table"]
        self.assertEqual([x["key"] for x in tb["columns"]], ["date", "orders", "amount", "rate"])
        self.assertEqual((len(tb["rows"]), tb["total_rows"]), (7, 132))
        self.assertEqual(tb["columns"][1]["align"], "right")
        self.assertEqual(tb["rows"][0][1], 128)
        # 指标：涨跌方向只表达方向（颜色由前端按中国惯例处理：涨红 / 跌绿）
        dirs = [x["dir"] for x in by["metrics"]["metrics"]["items"]]
        self.assertIn("up", dirs); self.assertIn("down", dirs)
        self.assertTrue(all(x["status"] in ("good", "warn", "bad") for x in by["metrics"]["metrics"]["items"]))
        # 清单 / 时间线 / 日历 / 状态 / 会话 / 网页：结构齐备且条目数满足契约
        self.assertEqual(by["list"]["list"]["mode"], "checklist")
        self.assertGreaterEqual(len(by["timeline"]["timeline"]["items"]), 2)
        self.assertTrue(by["calendar"]["calendar"]["events"])
        self.assertTrue(by["status"]["status"]["items"])
        self.assertGreaterEqual(len(by["chat"]["chat"]["messages"]), 2)
        self.assertTrue(by["web"]["web"]["url"].startswith("https://"))
        # 代码（diff 形态）
        self.assertEqual(by["code"]["code"]["mode"], "diff")
        self.assertIn("+", by["code"]["code"]["code"])
        # 图片：路径必须真实存在于工作区（防伪造路径）；inline 下载直显、普通下载走附件
        img = by["image"]["image"]
        self.assertTrue(img["path"].endswith(".svg"))
        q = "/api/workspace/file/download?path=" + urllib.parse.quote(img["path"])
        self.assertEqual(self.c.call("GET", q)[0], 200)
        with self.c.op.open(urllib.request.Request(self.c.base + q + "&inline=1", headers={"X-Combine": "1"}), timeout=20) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")   # 内联展示必须禁嗅探
            self.assertIn("image/svg+xml", r.headers.get("Content-Type", ""))
            self.assertNotIn("attachment", (r.headers.get("Content-Disposition") or ""))
        with self.c.op.open(urllib.request.Request(self.c.base + q, headers={"X-Combine": "1"}), timeout=20) as r:
            self.assertIn("attachment", (r.headers.get("Content-Disposition") or ""))   # 普通下载仍为附件
            self.assertIn("svg", r.headers.get("Content-Type", ""))                     # 类型由扩展名决定
            # 禁嗅探由全局安全中间件统一施加（hardening.py setdefault），所有响应都必须带
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        # 数据全部来自节点输出字段（卡片只是索引）：图 / 文 / 文件兜底仍照常组装
        self.assertTrue({"doc", "file"} <= set(kinds))


class AccountCleanup(unittest.TestCase):
    def test_account_deletion_removes_connected_data_and_files(self):
        srv = Server()
        try:
            client = Client(srv)
            uid = client.post("/api/auth/register", {"email": "cleanup@test.io", "password": "password1"}, expect=200)["user"]["id"]
            client.post("/api/agents", {"key": "mcp.demo"}, expect=200)
            client.post("/api/agents/mcp.demo/connect", {"values": {"AUDIT_TOKEN": "audit-only"}}, expect=200)
            tid = client.post("/api/tasks", {"goal": "帮我写一份学习计划"}, expect=202)["task"]["id"]
            client.wait(tid, lambda state: state["task"]["status"] == "completed")
            directory = os.path.join(srv.dir, "workspace", uid)
            self.assertTrue(os.path.isdir(directory))
            connection = srv.db()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agents WHERE user_id=?", (uid,)).fetchone()[0], 1)
            token = client.get("/api/me/delete-token")["confirm_token"]
            client.post("/api/me/delete", {"confirm_token": token}, expect=200)
            for table in ("users", "agents", "agent_credentials", "agent_calls", "llm_calls", "tasks"):
                column = "id" if table == "users" else "user_id"
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (uid,)).fetchone()[0], 0, table)
            self.assertFalse(os.path.exists(directory))
        finally:
            srv.stop()


class CloudAndRecovery(unittest.TestCase):
    def test_cloud_blocks_private_addresses(self):
        srv = Server(DEPLOY_MODE="cloud")
        try:
            c = Client(srv)
            c.post("/api/auth/register", {"email": "x@test.io", "password": "password1"}, expect=200)
            s, out = c.call("POST", "/api/providers", {"kind": "ollama"})
            self.assertEqual((s, out["error"]["code"]), (400, "LOCAL_DISABLED"))
            s, out = c.call("POST", "/api/providers", {"kind": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1", "api_key": "sk-abcdefgh12345"})
            self.assertEqual((s, out["error"]["code"]), (400, "PRIVATE_ADDRESS_FORBIDDEN"))
            self.assertNotIn("local", [g["key"] for g in c.get("/api/models")["groups"]])
            self.assertIsNone(c.get("/api/workspace")["root"])            # 云端版不暴露本机工作区路径
        finally:
            srv.stop()

    def test_restart_recovers_running_task(self):
        """INV-15：执行中途重启服务，未完成的步骤重新排队并最终跑完（无需任何人工接续）。"""
        srv = Server(FAKE_LATENCY_SCALE="0.6")
        c = Client(srv)
        c.post("/api/auth/register", {"email": "r@test.io", "password": "password1"}, expect=200)
        tid = c.post("/api/tasks", {"goal": "帮我写一份关于城市骑行的分析报告"}, expect=202)["task"]["id"]
        c.wait(tid, lambda s: any(n["status"] == "running" for n in s["nodes"]), 60)   # 规划完自动开跑
        cookies = c.op
        srv.stop(keep=True)
        srv2 = Server(data_dir=srv.dir, FAKE_LATENCY_SCALE="0.05")
        try:
            c2 = Client(srv2)
            c2.op = cookies
            c2.base = f"http://127.0.0.1:{srv2.port}"
            st = c2.wait(tid, lambda s: s["task"]["status"] == "completed", 60)        # 重启后自动接着跑完
            self.assertIn("run.recovered", [e["type"] for e in c2.events(tid)])
            con = srv2.db()
            self.assertGreaterEqual(con.execute("SELECT COUNT(*) FROM executions WHERE task_id=? AND status='orphaned'", (tid,)).fetchone()[0], 1)
            self.assertTrue(all(n["status"] == "succeeded" for n in st["nodes"]))
        finally:
            srv2.stop()


class Hardening(unittest.TestCase):
    """本轮修复的回归：安全头、输入校验、状态检查、多版本沿用执行的取消。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = Server(FAKE_LATENCY_SCALE="0.6")
        cls.c = Client(cls.srv)
        cls.c.post("/api/auth/register", {"email": "h@test.io", "password": "password1"}, expect=200)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_security_headers(self):
        with urllib.request.urlopen(self.c.base + "/") as r:
            csp = r.headers.get("Content-Security-Policy", "")
            self.assertIn("script-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")

    def test_invalid_input_is_400_not_500(self):
        c = self.c
        self.assertEqual(c.call("POST", "/api/tasks", {"goal": "写一份报告", "budget_cny": "abc"})[0], 400)
        self.assertEqual(c.call("POST", "/api/tasks", {"goal": "写一份报告", "budget_cny": 1e9})[0], 400)
        self.assertEqual(c.call("POST", "/api/kb/search", {"query": "测试", "top_k": "x"})[0], 400)
        self.assertEqual(c.call("PATCH", "/api/settings", {"models.default_model": {"x": 1}})[0], 400)
        self.assertEqual(c.call("POST", "/api/providers", {"kind": "fake", "api_key": "fake-1234567", "price_in_per_1m": -5})[0], 400)
        tid = c.post("/api/tasks", {"goal": "你好"}, expect=202)["task"]["id"]
        c.wait(tid, lambda s: s["task"]["status"] == "completed")
        s, _ = self.c.call("PUT", f"/api/tasks/{tid}/layout", {"positions": {"a": {"x": "abc", "y": 1e30}}, "viewport": {"z": 99},
                                                               "art": {"hidden": "x", "pinned": [1, "k"], "focus": 1,
                                                                       "checks": {"a:l:x": ["bad", 1e9, 3], "": [1]},
                                                                       "sorts": {"a:t:x": [1, "weird"], "": []}}})
        self.assertEqual(s, 200)
        lay = c.get(f"/api/tasks/{tid}/layout")
        self.assertEqual(lay["positions"]["a"], {"x": 0.0, "y": 1e6})            # 非法坐标被清洗而不是写入数据库
        self.assertEqual(lay["viewport"]["z"], 2)
        self.assertEqual(lay["art"], {"hidden": [], "pinned": ["1", "k"], "focus": True,
                                      # 勾选下标只留合法整数（负数/超限/非数字丢弃）；排序方向非 desc 一律归 asc
                                      "checks": {"a:l:x": [3]}, "sorts": {"a:t:x": [1, "asc"]}})
        self.assertIn(c.call("POST", f"/api/tasks/{tid}/budget", {"add_cny": 1})[0], (404, 405))   # 追加预算端点已删除（成本只做展示）

    def test_cancelled_task_cannot_be_revived(self):
        c = self.c
        tid = c.post("/api/tasks", {"goal": "帮我写一份关于城市骑行的分析报告"}, expect=202)["task"]["id"]
        c.wait(tid, lambda s: any(n["status"] == "running" for n in s["nodes"]), 60)   # 自动开跑后即可取消
        c.post(f"/api/tasks/{tid}/cancel", expect=200)
        st = c.get(f"/api/tasks/{tid}/state")
        k = st["nodes"][0]["node_key"]
        self.assertEqual(c.call("POST", f"/api/tasks/{tid}/nodes/{k}/retry")[0], 409)
        self.assertEqual(c.call("POST", f"/api/tasks/{tid}/nodes/{k}/rerun")[0], 409)
        self.assertEqual(c.call("POST", f"/api/tasks/{tid}/comments", {"node_key": "no_such_step", "body": "x"})[0], 404)

    def test_superseded_carried_execution_is_cancelled(self):
        """v1 中执行中的步骤 X 被 v2 沿用；v3 修改了 X → v1 的那次执行必须被取消（修复前会一直跑完）。"""
        c = self.c
        tid = c.post("/api/tasks", {"goal": "帮我写一份关于城市骑行的分析报告"}, expect=202)["task"]["id"]
        st = c.wait(tid, lambda s: any(n["status"] == "running" and n["node_key"] in ("part1", "part2") for n in s["nodes"]), 60)
        x = next(n["node_key"] for n in st["nodes"] if n["status"] == "running" and n["node_key"] in ("part1", "part2"))
        sink = next(n["node_key"] for n in st["nodes"] if n["node_key"] == "review")
        r1 = c.post(f"/api/tasks/{tid}/revisions", {"instructions": {sink: ["更严格地核对引用"]}}, expect=202)
        self.assertNotIn(x, r1["affected"])
        self.assertEqual(node(c.get(f"/api/tasks/{tid}/state"), x)["status"], "running")      # v2 沿用进行中的执行
        c.post(f"/api/tasks/{tid}/revisions", {"instructions": {x: ["换一个角度"]}}, expect=202)
        time.sleep(0.5)
        con = self.srv.db()
        rows = con.execute("SELECT status FROM executions WHERE task_id=? AND node_key=? AND plan_version=1", (tid, x)).fetchall()
        self.assertTrue(rows and all(r[0] == "cancelled" for r in rows), rows)


class ToolSafetyGuard(unittest.TestCase):
    """可选的执行前安全防护（F32 工具风险分级 + 设置项 approval.ask_dangerous）。

    默认一路直跑：删除与控制台操作同样不打扰用户。只有主动打开「危险操作先问我」后，
    这两类操作才会在执行前逐次停下等待批准，批准按「工具 + 参数」精确匹配。
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = Server()
        cls.c = Client(cls.srv)
        cls.c.post("/api/auth/register", {"email": "g@test.io", "password": "password1"}, expect=200)
        # 打开删除与控制台能力（默认关闭），并打开「危险操作先问我」
        cls.c.call("PATCH", "/api/settings", {"tools.file_delete": True, "tools.shell_exec": True,
                                              "approval.ask_dangerous": True}, expect=200)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _start_cleanup(self, c):
        """[del] 场景：cleanup 节点会依次写文件 → 删除 → 执行命令（后两步在安全防护后面）。"""
        return c.post("/api/tasks", {"goal": "整理一下学习资料 [del]"}, expect=202)["task"]["id"]

    def _await_node(self, c, tid, key, want, timeout=60):
        """等待某个步骤到达指定状态；计划还没生成时节点为空，不能直接用 node()（会 StopIteration）。"""
        return c.wait(tid, lambda s: next((n["status"] for n in s["nodes"] if n["node_key"] == key), None) == want, timeout)

    def test_guard_on_delete_and_console_need_approval_then_run(self):
        c = self.c
        tid = self._start_cleanup(c)
        st = self._await_node(c, tid, "cleanup", "awaiting_approval")
        ap = next(a for a in st["approvals"] if a["kind"] == "tool")
        self.assertEqual(ap["tool"], "file.delete")                              # 先撞上删除这一步
        self.assertEqual(ap["args"]["path"], "drafts/note.md")                   # 待批准的是具体操作
        req = next(e for e in c.events(tid) if e["type"] == "tool.approval_required")
        self.assertEqual((req["payload"]["risk"], req["payload"]["tool"]), ("delete", "file.delete"))
        self.assertEqual(st["task"]["status"], "running")                        # 只有这一步停下，任务不暂停
        # 闸门之前的只读/写入已经真实执行：草稿文件确实落在工作区里
        self.assertIn(f"任务/{tid}/v1/drafts/note.md", [f["path"] for f in c.get("/api/workspace")["items"]])
        # 批准 → 该步骤重新排队；控制台命令随后同样需要批准
        c.post(f"/api/approvals/{ap['id']}/approve", expect=200)
        self._await_node(c, tid, "cleanup", "awaiting_approval")
        st = c.wait(tid, lambda s: any(a["kind"] == "tool" and a["tool"] == "shell.run" for a in s["approvals"]), 60)
        ap2 = next(a for a in st["approvals"] if a["kind"] == "tool" and a["tool"] == "shell.run")
        c.post(f"/api/approvals/{ap2['id']}/approve", expect=200)
        st = c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        self.assertEqual(node(st, "cleanup")["status"], "succeeded")
        self.assertNotIn(f"任务/{tid}/v1/drafts/note.md", [f["path"] for f in c.get("/api/workspace")["items"]])
        rows = self.srv.db().execute("SELECT tool, status FROM approvals WHERE task_id=? AND kind='tool' ORDER BY created_at",
                                     (tid,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["file.delete", "shell.run"])
        self.assertTrue(all(r[1] == "approved" for r in rows))
        # 批准记录按「工具 + 参数」精确匹配：同一件被批准的操作只问一次
        self.assertNotIn("TOOL_REJECTED", [e["payload"].get("code") for e in c.events(tid)])
        done = next(e for e in c.events(tid) if e["type"] == "task.completed")
        self.assertEqual(done["payload"]["issues"], 0)

    def test_guard_on_reject_marks_step_failed_without_rerun(self):
        c = self.c
        tid = self._start_cleanup(c)
        st = self._await_node(c, tid, "cleanup", "awaiting_approval")
        ap = next(a for a in st["approvals"] if a["kind"] == "tool")
        before = self.srv.db().execute("SELECT COUNT(*) FROM executions WHERE task_id=? AND node_key='cleanup'", (tid,)).fetchone()[0]
        c.post(f"/api/approvals/{ap['id']}/reject", {}, expect=202)              # 拒绝不需要理由
        st = c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)       # 拒绝不停机：其余部分照常跑完
        self.assertEqual(node(st, "cleanup")["status"], "failed")
        self.assertEqual(node(st, "cleanup")["error_code"], "TOOL_REJECTED")
        types = [e["type"] for e in c.events(tid)]
        self.assertIn("tool.approval_denied", types)
        self.assertNotIn("plan.version_created", types)                          # 拒绝 ≠ 要求修改：不创建新版本、不自动重跑
        after = self.srv.db().execute("SELECT COUNT(*) FROM executions WHERE task_id=? AND node_key='cleanup'", (tid,)).fetchone()[0]
        self.assertEqual(before, after)
        done = next(e for e in c.events(tid) if e["type"] == "task.completed")
        self.assertEqual(done["payload"]["issues"], 1)                          # 问题清单随成果交付
        self.assertIn(f"任务/{tid}/v1/drafts/note.md", [f["path"] for f in c.get("/api/workspace")["items"]])   # 删除没有被执行
        self.assertEqual(st["approvals"], [])

    def test_guard_off_by_default_runs_dangerous_tools_directly(self):
        """默认（未开启防护）：删除与控制台一路直跑，不产生任何批准点。"""
        c = Client(self.srv)
        c.post("/api/auth/register", {"email": "g3@test.io", "password": "password1"}, expect=200)
        c.call("PATCH", "/api/settings", {"tools.file_delete": True, "tools.shell_exec": True}, expect=200)
        tid = self._start_cleanup(c)
        st = c.wait(tid, lambda s: s["task"]["status"] == "completed", 60)
        self.assertEqual(node(st, "cleanup")["status"], "succeeded")
        self.assertEqual([f["path"] for f in c.get("/api/workspace")["items"] if f["path"].startswith("drafts/")], [])   # 草稿被直跑删除；交付文件保留
        self.assertNotIn("tool.approval_required", [e["type"] for e in c.events(tid)])
        self.assertEqual(st["approvals"], [])


class WechatE2E(unittest.TestCase):
    """F30：无资质时用 fake 模式跑通「出码 → 手机授权 → PC 轮询登录」全链路。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = Server(WECHAT_MODE="fake", WECHAT_APPID="wx_e2e_appid")
        cls.c = Client(cls.srv)                          # PC 端浏览器（带 Cookie 罐）

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def approve(self, state, ident):
        """模拟手机扫码：打开模拟授权页 → 提交表单到回调。返回回调页 HTML。"""
        phone = Client(self.srv)
        page = phone.get(f"/api/auth/wechat/mock/authorize?state={state}&as={ident}", expect=200)
        self.assertIn("mock_" + ident, page)
        return phone.get(f"/api/auth/wechat/callback?state={state}&code=mock_{ident}", expect=200)

    def test_01_qr_flow_establishes_session(self):
        info = self.c.get("/api/auth/wechat/start?next=/", expect=200)
        self.assertTrue(info["enabled"])
        self.assertEqual(info["mode"], "fake")
        self.assertIn("/api/auth/wechat/mock/authorize", info["qr_url"])
        self.assertEqual(self.c.get(f"/api/auth/wechat/poll?id={info['state']}", expect=200)["status"], "pending")
        self.assertIn("授权成功", self.approve(info["state"], "e2euser"))
        r = self.c.get(f"/api/auth/wechat/poll?id={info['state']}", expect=200)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["user"]["email"].endswith("@wechat.local"))
        self.assertEqual(self.c.get("/api/me", expect=200)["user"]["id"], r["user"]["id"])   # 会话已建立
        self.assertEqual(self.c.get(f"/api/auth/wechat/poll?id={info['state']}", expect=200)["status"], "consumed")

    def test_02_same_wechat_reuses_account(self):
        ids = []
        for _ in range(2):
            info = self.c.get("/api/auth/wechat/start", expect=200)
            self.approve(info["state"], "reuse")
            ids.append(self.c.get(f"/api/auth/wechat/poll?id={info['state']}", expect=200)["user"]["id"])
        self.assertEqual(ids[0], ids[1])

    def test_03_poll_must_come_from_same_browser(self):
        """防会话固定：换另一个浏览器轮询已确认的票据，拿不到会话。"""
        info = self.c.get("/api/auth/wechat/start", expect=200)
        self.approve(info["state"], "hijack")
        other = Client(self.srv)                         # 没有 cb_wx_state 的浏览器
        self.assertEqual(other.get(f"/api/auth/wechat/poll?id={info['state']}")["status"], "expired")
        self.assertEqual(self.c.get(f"/api/auth/wechat/poll?id={info['state']}", expect=200)["status"], "ok")

    def test_04_unknown_state_is_expired(self):
        self.assertEqual(self.c.get("/api/auth/wechat/poll?id=nope", expect=200)["status"], "expired")

    def test_05_callback_rejects_reused_state(self):
        info = self.c.get("/api/auth/wechat/start", expect=200)
        self.approve(info["state"], "once")
        phone = Client(self.srv)
        self.assertIn("已失效", phone.get(f"/api/auth/wechat/callback?state={info['state']}&code=mock_once", expect=200))

    def test_06_disabled_without_config(self):
        srv = Server(WECHAT_MODE="off", WECHAT_APPID="", WECHAT_SECRET="", WECHAT_REDIRECT_URI="")
        try:
            c = Client(srv)
            info = c.get("/api/auth/wechat/start", expect=200)
            self.assertFalse(info["enabled"])
            self.assertEqual(info["mode"], "off")
            c.call("GET", "/api/auth/wechat/mock/authorize?state=x", expect=404)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
