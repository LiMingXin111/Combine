"""进程内单元测试：覆盖边界条件与确定性策略（不启动 HTTP 服务，速度快）。

覆盖：额度并发预留、安全防护策略（core/policy 默认直跑 + 可选「危险操作先问我」）、任务工作区沙箱（services/workspace）、
模型输出容错、PlanOp 回滚、云端地址校验、限流、事件保留期与队列上限、思考模式契约、空输出护栏。

运行：  python -m unittest tests/test_units.py -v      （在 combine/ 目录下）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="combine-unit-")
os.environ.update(DATA_DIR=_TMP, DB_PATH=os.path.join(_TMP, "combine.db"), COMBINE_IGNORE_DOTENV="1",
                  FAKE_LLM="1", FAKE_LATENCY_SCALE="0.05", DEPLOY_MODE="self_hosted", WEB_SEARCH_MODE="fake")
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app import config, db, events, hardening  # noqa: E402
from app.core import policy  # noqa: E402
from app.llm import gateway  # noqa: E402
from app.services import artifacts, kb, plans, wechat, workspace  # noqa: E402
from app.services.runner import _as_float, _as_list, _assistant_msg, _blank  # noqa: E402
from app.security import verify_password  # noqa: E402
from app.util import ApiError, new_id, now_ms  # noqa: E402


def make_user_task(quota: float = 50.0) -> tuple[str, str]:
    uid, tid = new_id("usr"), new_id("tsk")
    db.insert("users", {"id": uid, "email": uid + "@t.io", "password_hash": "x", "display_name": "u", "quota_monthly_cny": quota,
                        "created_at": now_ms()})
    db.insert("tasks", {"id": tid, "user_id": uid, "title": "t", "goal": "你好", "mode": "auto", "status": "running",
                        "budget_cny": 1.0, "created_at": now_ms(), "updated_at": now_ms()})
    return uid, tid


class Units(unittest.TestCase):
    # 临时数据目录由模块级 tearDownModule 统一清理（本模块有多个测试类共用同一份数据库）

    # ---- INV-12（改造后）：并发调用不能一起穿过「平台内置模型月度额度」 ----
    def test_quota_reservation_under_concurrency(self):
        info = gateway.builtin_info("standard")
        msgs = [{"role": "user", "content": "你好"}]
        est = gateway.estimate_cost(info, gateway._approx_tokens(msgs), 1500 // 2)
        uid, tid = make_user_task(quota=est * 2.5)             # 额度只够两次调用

        async def one():
            return await gateway.chat(user_id=uid, task_id=tid, node_key=None, kind="quick", info=info, messages=msgs,
                                      json_mode=False, meta={"goal": "你好", "lang": "zh", "task_id": tid}, max_out=1500)

        async def run():
            return await asyncio.gather(*[one() for _ in range(4)], return_exceptions=True)
        res = asyncio.run(run())
        blocked = [r for r in res if isinstance(r, gateway.BudgetExceeded)]
        self.assertEqual(len(blocked), 2, res)                  # 修复前 4 个都会通过预检
        self.assertTrue(all(b.code == "QUOTA_EXCEEDED" for b in blocked))
        self.assertEqual(gateway._reserved_user.get(uid), None)  # 调用结束后预留全部释放

    # ---- 成本只做展示：任务预算不拦截调用，只有平台月度额度是硬约束 ----
    def test_task_budget_never_blocks_calls(self):
        info = gateway.builtin_info("standard")
        msgs = [{"role": "user", "content": "你好"}]
        uid, tid = make_user_task(quota=50.0)
        db.update("tasks", {"id": tid}, {"budget_cny": 0.0})    # 任务预算为零也不影响执行

        async def run():
            return await gateway.chat(user_id=uid, task_id=tid, node_key=None, kind="quick", info=info, messages=msgs,
                                      json_mode=False, meta={"goal": "你好", "lang": "zh", "task_id": tid}, max_out=1500)
        self.assertIn("content", asyncio.run(run()))

    def test_quota_exhausted_blocks(self):
        info = gateway.builtin_info("standard")
        msgs = [{"role": "user", "content": "你好"}]
        uid, tid = make_user_task(quota=0.0)

        async def run():
            return await gateway.chat(user_id=uid, task_id=tid, node_key=None, kind="quick", info=info, messages=msgs,
                                      json_mode=False, meta={"goal": "你好", "lang": "zh", "task_id": tid}, max_out=1500)
        with self.assertRaises(gateway.BudgetExceeded) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.code, "QUOTA_EXCEEDED")

    # ---- 安全防护策略：只读/写入永远直通；删除与控制台默认直跑，开启「危险操作先问我」后逐次批准 ----
    def test_policy_risk_and_approval_matrix(self):
        default = {}
        for t in ("kb.search", "reference.read", "calc.eval", "file.list"):
            self.assertEqual(policy.risk_of(t), "read")
            self.assertFalse(policy.approval_required(t, default))              # 只读操作永远不打扰用户
        self.assertEqual((policy.risk_of("file.write"), policy.risk_of("file.delete"), policy.risk_of("shell.run")),
                         ("write", "delete", "console"))
        self.assertFalse(policy.approval_required("file.write", default))
        self.assertFalse(policy.approval_required("file.delete", default))      # 默认直跑：任务不因审批停顿
        self.assertFalse(policy.approval_required("shell.run", default))
        guard = {"approval.ask_dangerous": True}
        self.assertFalse(policy.approval_required("file.write", guard))         # 写文件永远不打扰
        self.assertTrue(policy.approval_required("file.delete", guard))         # 开启防护后逐次批准
        self.assertTrue(policy.approval_required("shell.run", guard))
        self.assertFalse(policy.approval_required("kb.search", guard))
        self.assertNotEqual(policy.args_hash({"path": "a"}), policy.args_hash({"path": "b"}))   # 批准按「工具+参数」精确匹配
        self.assertEqual(policy.risk_of("unknown.tool"), "write")               # 未注册工具按最保守处理

    # ---- 闸门异常必须原样保留参数：曾误用 self.args，被 BaseException 转成键名元组 ----
    def test_tool_approval_exception_keeps_args(self):
        e = policy.ToolApprovalRequired("file.delete", {"path": "drafts/note.md"})
        self.assertEqual(e.tool_args, {"path": "drafts/note.md"})
        self.assertEqual(policy.summarize(e.tool, e.tool_args), "删除文件：drafts/note.md")
        self.assertEqual(e.hash, policy.args_hash({"path": "drafts/note.md"}))  # 「批准 → 重跑」要能命中同一条记录
        self.assertEqual(policy.ToolApprovalRequired("shell.run", None).tool_args, {})   # 非 dict 参数按空处理

    def test_policy_tool_switches_and_cloud(self):
        on = {"tools.file_write": True, "tools.file_delete": True, "tools.shell_exec": True}
        self.assertEqual(policy.allowed(["kb.search", "file.write", "file.delete", "shell.run", "nope"], {}), ["kb.search"])
        self.assertEqual(policy.allowed(["kb.search", "file.write", "file.delete", "shell.run"], on),
                         ["kb.search", "file.write", "file.delete", "shell.run"])
        old = config.DEPLOY_MODE
        config.DEPLOY_MODE = "cloud"
        try:
            self.assertFalse(policy.enabled("file.write", on))                  # 云端版不开放本机文件与命令
            self.assertEqual(policy.allowed(["kb.search", "file.write", "shell.run"], on), ["kb.search"])
        finally:
            config.DEPLOY_MODE = old

    # ---- 任务工作区（F32）：路径逃逸一律拒绝、按用户隔离、命令在工作区内执行 ----
    def test_workspace_sandbox(self):
        uid, uid2 = new_id("usr"), new_id("usr")
        for bad in ("", "  ", "../x", "a/../../x", "/etc/passwd", "C:/Windows/win.ini", "a/b/c/d/e/f/g/h/i/j"):
            self.assertIsNone(workspace.resolve(uid, bad), bad)
        self.assertEqual(workspace.write(uid, "../escape.md", "x")["error"], "PATH_DENIED")
        self.assertEqual(workspace.write(uid, "drafts/note.md", "hello")["written"], "drafts/note.md")
        self.assertEqual(workspace.read(uid, "drafts/note.md")["content"], "hello")
        self.assertEqual([f["path"] for f in workspace.list_files(uid)["items"]], ["drafts/note.md"])
        self.assertEqual(workspace.list_files(uid2)["items"], [])               # 按用户隔离（INV-08）
        self.assertEqual(workspace.read(uid2, "drafts/note.md")["error"], "NOT_FOUND")
        self.assertEqual(workspace.write(uid, "big.md", "x" * (workspace.MAX_WRITE_BYTES + 1))["error"], "TOO_LARGE")
        self.assertEqual(workspace.delete(uid, "drafts")["error"], "NOT_FOUND")  # 目录不能被当作文件删除
        self.assertEqual(workspace.delete(uid, "drafts/note.md")["deleted"], "drafts/note.md")
        self.assertEqual(workspace.list_files(uid)["items"], [])
        r = workspace.run_command(uid, "echo combine-workspace-ready")
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("combine-workspace-ready", r["stdout"])
        self.assertEqual(workspace.run_command(uid, "   ")["error"], "EMPTY_COMMAND")

    # ---- 计算器工具不能被构造的表达式拖垮 ----
    def test_calc_bounds(self):
        run = lambda e: kb.run_tool("calc.eval", {"expression": e}, user_id="u", attachments=[], web_enabled=False)  # noqa: E731
        self.assertEqual(run("1200+260+130")["result"], 1590)
        t0 = time.time()
        self.assertEqual(run("((9**9)**9)**9")["error"], "TOOL_ERROR")
        self.assertEqual(run("(-8)**0.5")["error"], "TOOL_ERROR")
        self.assertLess(time.time() - t0, 1)

    # ---- 模型输出容错 ----
    def test_output_coercion(self):
        self.assertEqual(_as_list("一个问题"), ["一个问题"])    # 修复前会被拆成 4 个单字问题
        self.assertEqual(_as_list(None), [])
        self.assertEqual(_as_float("high", 0.7), 0.7)
        self.assertEqual(_as_float(float("nan"), 0.7), 0.7)
        self.assertEqual(_as_float(3, 0.7), 1.0)

    # ---- 成果卡片（v1.5）：图表数据校验，结构不合法一律丢弃（宁缺毋滥，前端不做二次防御）----
    def test_clean_chart_validation(self):
        for bad in (None, "x", [], {}, {"type": "radar", "labels": ["a", "b"], "series": [{"values": [1, 2]}]},
                    {"type": "bar", "labels": ["a"], "series": [{"values": [1]}]},
                    {"type": "bar", "labels": ["a", "b"], "series": []},
                    {"type": "bar", "labels": ["a", "b"], "series": ["nope"]}):
            self.assertIsNone(artifacts.clean_chart(bad), bad)
        c = artifacts.clean_chart({"type": "line", "title": " 走势 ", "labels": ["a", "b", "c"],
                                   "series": [{"name": "s", "values": [1, "2", float("nan")]}, {"name": "t", "values": ["x"]}]})
        self.assertEqual((c["type"], c["title"]), ("line", "走势"))
        self.assertEqual(c["series"][0]["values"], [1.0, 2.0, 0.0])           # 字符串数字可转，NaN → 0
        self.assertEqual(c["series"][1]["values"], [0.0, 0.0, 0.0])           # 无法转换 → 0，且长度与 labels 对齐
        p = artifacts.clean_chart({"type": "pie", "labels": ["a", "b"], "series": [{"values": [1, 2]}, {"values": [3, 4]}]})
        self.assertEqual(len(p["series"]), 1)                                  # 饼图只看第一组数据
        many = artifacts.clean_chart({"type": "bar", "labels": [f"l{i}" for i in range(20)],
                                      "series": [{"values": list(range(20))} for _ in range(6)]})
        self.assertEqual((len(many["labels"]), len(many["series"])), (12, 4))  # 上限：12 个标签、4 组数据

    # ---- 成果卡片：图 → 文 → 文件三类组装；卡片只是索引，正文仍在 sections ----
    def test_assemble_cards(self):
        ch = artifacts.clean_chart({"type": "bar", "labels": ["a", "b"], "series": [{"values": [1, 2]}]})
        nodes = [{"node_key": f"n{i}", "title": f"步骤 {i}", "output": ({"chart": ch} if i < 4 else {})} for i in range(5)]
        content = "## 小标题\n\n正文 [[c:abc]] 续写"
        sections = [{"section_key": "n0", "title": "第一章", "node_key": "n0", "content": content, "citations": [{"chunk_id": "abc"}]}]
        cards = artifacts.assemble_cards(sections, nodes, [{"path": "交付/报告.md", "bytes": 128, "node_key": "n4"}])
        self.assertEqual([c["kind"] for c in cards], ["chart", "chart", "chart", "doc", "file"])   # 顺序：图 → 文 → 文件；图表上限 3 张
        self.assertEqual((cards[0]["node_key"], cards[0]["title"]), ("n0", "步骤 0"))              # 图表无标题时回落到步骤名
        doc = cards[3]
        self.assertEqual((doc["section_key"], doc["chars"], doc["citations"]), ("n0", len(content), 1))
        self.assertEqual(doc["excerpt"], "小标题 正文 续写")                    # 引用标记与标题符号去除、压成一行
        self.assertEqual(cards[4]["node_key"], "n4")                          # v1.6：文件卡带产出节点，画布成果节点据此连线
        self.assertEqual(artifacts.assemble_cards([], [], []) , [])            # 空成果没有卡片
        # 无产出节点的旧文件（如外部写入）：node_key 为 None，不强行归属
        self.assertIsNone(artifacts.assemble_cards([], [], [{"path": "a.md", "bytes": 1}])[0]["node_key"])

    # ---- 交付文件扫描：只收录仍存在的 file.write 产物，草稿目录与其它工具调用不看 ----
    def test_task_files_scan(self):
        uid, tid = make_user_task()
        workspace.write(uid, "交付/最终.md", "hello world")
        workspace.write(uid, "drafts/tmp.md", "temp")
        db.insert("executions", {"id": new_id("ex"), "task_id": tid, "node_key": "n1", "plan_version": 1, "attempt": 1,
                                 "status": "succeeded", "input_hash": "h", "started_at": now_ms(),
                                 "tool_calls": json.dumps([
                                     {"tool": "file.write", "result": {"written": "交付/最终.md", "bytes": 999}},
                                     {"tool": "file.write", "result": {"written": "drafts/tmp.md", "bytes": 4}},
                                     {"tool": "file.read", "result": {"content": "x"}},
                                 ], ensure_ascii=False)})
        files = artifacts.task_files({"id": tid, "user_id": uid})
        self.assertEqual([f["path"] for f in files], ["交付/最终.md"])          # 草稿不收录；非 file.write 调用不看
        self.assertEqual(files[0]["bytes"], 11)                                # 字节数按磁盘复核，不信任模型回执
        self.assertEqual(files[0]["node_key"], "n1")                           # v1.6：记录写入节点（画布连线用）
        workspace.delete(uid, "交付/最终.md")
        self.assertEqual(artifacts.task_files({"id": tid, "user_id": uid}), [])  # 文件被删后卡片不再出现

    # ---- 前端 ES 模块图静态校验：具名导入必须真实存在（曾把 `t` 从 core.js 导入，整页空白只在浏览器里才暴露）----
    def test_frontend_modules_parse_as_esm(self):
        """每个前端模块都必须能被真实 ESM 解析器解析（v3.0 回归：语言包里含全角符号的键名未加引号
        会让整个模块语法错误 → 页面上所有 t() 都失效；正则式的导入检查拦不住这类问题）。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node")
        js_dir = os.path.join(ROOT, "frontend", "js")
        tmp = tempfile.mkdtemp(prefix="combine-esm-")
        for f in sorted(os.listdir(js_dir)):
            if not f.endswith(".js"):
                continue
            dst = os.path.join(tmp, f[:-3] + ".mjs")            # .mjs 强制按 ES 模块解析
            shutil.copy(os.path.join(js_dir, f), dst)
            r = subprocess.run([node, "--check", dst], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, f"{f} 不是合法的 ES 模块：{r.stderr.strip()[:400]}")

    def test_frontend_module_imports_resolve(self):
        js_dir = os.path.join(ROOT, "frontend", "js")
        files = [f for f in os.listdir(js_dir) if f.endswith(".js")]
        exports: dict[str, set[str]] = {}
        for f in files:
            with open(os.path.join(js_dir, f), encoding="utf-8") as fh:
                s = fh.read()
            names = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)", s))
            for grp in re.findall(r"export\s*\{([^}]+)\}", s):
                for n in grp.split(","):
                    n = n.strip().split(" as ")[-1].strip()
                    if n:
                        names.add(n)
            exports[f] = names
        for f in files:
            with open(os.path.join(js_dir, f), encoding="utf-8") as fh:
                s = fh.read()
            for names_s, target in re.findall(r"import\s*\{([^}]+)\}\s*from\s*['\"]\./([\w.-]+\.js)['\"]", s):
                self.assertIn(target, exports, f"{f} 导入了不存在的模块 {target}")
                for n in [x.strip().split(" as ")[0] for x in names_s.split(",") if x.strip()]:
                    self.assertIn(n, exports[target], f"{f} 从 {target} 导入了不存在的导出 {n}")

    # ---- PlanOp：非法操作整条回滚，字段被清洗 ----
    def test_plan_ops_validation_and_rollback(self):
        nodes = [{"node_key": "a", "title": "A", "description": "", "acceptance": [], "depends_on": [], "input_refs": [],
                  "produces_sections": [], "capability_requirements": {"tags": ["write"], "tools": [], "permission": "read"},
                  "est_complexity": 3, "requires_approval": False, "revision_instructions": []}]
        out, rej = plans.apply_ops([dict(n) for n in nodes], [
            {"op": "update_node"},                                                    # 缺 node_key
            {"op": "update_node", "node_key": "a", "patch": {"est_complexity": "abc", "title": 123, "acceptance": "一条"}},
            {"op": "add_node", "node": {"node_key": "b", "est_complexity": "x",
                                        "capability_requirements": {"tags": "write", "tools": ["rm -rf", "kb.search"], "permission": "write"}}},
            "not-a-dict", {"op": "add_edge", "from": "b", "to": "a"}, {"op": "add_edge", "from": "a", "to": "b"},
        ], None, rematch=False)
        self.assertEqual([r["reason"] for r in rej], ["OP_INVALID", "OP_INVALID", "PLAN_CYCLE"])
        a, b = out[0], out[1]
        self.assertEqual((a["est_complexity"], a["title"], a["acceptance"]), (3, "123", ["一条"]))
        self.assertEqual(b["capability_requirements"]["tools"], ["kb.search"])      # 只保留白名单工具
        self.assertEqual(b["capability_requirements"]["permission"], "read")
        self.assertEqual(a["depends_on"], ["b"])
        self.assertEqual(b["depends_on"], [])                                       # 成环的那条被回滚

    # ---- 云端版地址校验 ----
    def test_cloud_address_guard(self):
        old = config.DEPLOY_MODE
        config.DEPLOY_MODE = "cloud"
        try:
            for u in ("http://127.0.0.1:11434/v1", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://169.254.169.254/",
                      "http://10.1.2.3/", "http://localhost:8000"):
                with self.assertRaises(ApiError, msg=u):
                    gateway.check_base_url(u, "byok")
            with self.assertRaises(gateway.LLMError):                       # 调用时再校验一次（防止配置后改 DNS）
                gateway._http_json("http://127.0.0.1:9/v1/models", "", None, 2)
        finally:
            config.DEPLOY_MODE = old

    # ---- 接口限流 ----
    def test_rate_limit(self):
        req = SimpleNamespace(url=SimpleNamespace(path="/api/auth/login"), method="POST", headers={},
                              client=SimpleNamespace(host="10.9.9.9"))
        hits = [hardening.check_rate(req) for _ in range(25)]
        self.assertTrue(all(h is None for h in hits[:20]))
        self.assertTrue(all(h is not None for h in hits[20:]))
        other = SimpleNamespace(url=SimpleNamespace(path="/api/auth/login"), method="POST", headers={},
                                client=SimpleNamespace(host="10.9.9.10"))
        self.assertIsNone(hardening.check_rate(other))                   # 按 IP 隔离
        get = SimpleNamespace(url=SimpleNamespace(path="/api/tasks"), method="GET", headers={}, client=req.client)
        self.assertIsNone(hardening.check_rate(get))                     # 读请求不限流

    # ---- 执行记录保留期（F26 隐私设置）真正生效 ----
    def test_event_retention(self):
        uid, tid = make_user_task()
        db.update("tasks", {"id": tid}, {"status": "completed", "updated_at": now_ms() - 10 * 86400_000})
        events.emit(tid, "task.completed", {})
        db.insert("user_settings", {"user_id": uid, "data": {"privacy.event_retention_days": 7}})
        uid2, tid2 = make_user_task()                                   # 进行中的任务不清理
        events.emit(tid2, "task.created", {})
        db.update("tasks", {"id": tid2}, {"updated_at": now_ms() - 400 * 86400_000})
        db.insert("sessions", {"token_hash": new_id("s"), "user_id": uid, "expires_at": now_ms() - 1, "created_at": now_ms()})
        out = hardening.housekeeping()
        self.assertGreaterEqual(out["events"], 1)
        self.assertGreaterEqual(out["sessions"], 1)
        self.assertEqual(len(events.history(tid)), 0)
        self.assertEqual(len(events.history(tid2)), 1)

    # ---- SSE 慢消费者：队列有上限，溢出后标记为落后而不是抛异常 ----
    def test_event_queue_bounded(self):
        old = config.SSE_QUEUE_MAX
        config.SSE_QUEUE_MAX = 3
        try:
            _, tid = make_user_task()

            async def run():
                qu = events.subscribe(tid)
                for i in range(10):
                    events.emit(tid, "x.y", {"i": i})                     # 修复前第 4 条起会因 QueueFull 抛异常
                lag = events.lagging(qu)
                events.unsubscribe(tid, qu)
                return qu.qsize(), lag
            size, lag = asyncio.run(run())
            self.assertEqual((size, lag), (3, True))
            self.assertEqual(events.sub_count(tid), 0)
            self.assertNotIn(tid, events._task_subs)                      # 断开后不残留空集合
        finally:
            config.SSE_QUEUE_MAX = old


    # ---- DeepSeek 思考模式：reasoning_content 被捕获并可回传（多轮工具调用契约）----
    def test_openai_call_captures_reasoning(self):
        info = gateway.ModelInfo("probe", "byok", "d", "deepseek-chat", "openai", {"json"},
                                 base_url="https://api.example.com/v1", api_key="k")
        canned = {"choices": [{"finish_reason": "length",
                               "message": {"content": "", "reasoning_content": "先想一下",
                                           "tool_calls": [{"id": "c1", "function": {"name": "ping", "arguments": '{"x":1}'}}]}}],
                  "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
        old = gateway._http_json
        gateway._http_json = lambda *a, **k: canned
        try:
            out, tin, tout = gateway._openai_call(info, [{"role": "user", "content": "x"}], None, False, 10)
        finally:
            gateway._http_json = old
        self.assertEqual(out["reasoning"], "先想一下")
        self.assertEqual(out["finish_reason"], "length")     # 截断标志透传给调用方
        self.assertEqual(out["tool_calls"][0]["name"], "ping")
        self.assertEqual((tin, tout), (5, 7))

    def test_assistant_msg_carries_reasoning(self):
        msg = _assistant_msg({"content": "", "tool_calls": [{"id": "c1", "name": "kb.search", "args": {"query": "x"}}],
                              "reasoning": "推演内容"})
        self.assertEqual(msg["reasoning_content"], "推演内容")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "kb.search")
        bare = _assistant_msg({"content": "ok", "tool_calls": [], "reasoning": ""})   # FakeLLM 等无该字段 → 不携带
        self.assertNotIn("reasoning_content", bare)
        self.assertNotIn("tool_calls", bare)                 # 空数组字段不回传（部分服务商不接受）
        blank = _assistant_msg({"content": "", "tool_calls": [], "reasoning": ""})     # 纠正空回复时走这条路径
        self.assertEqual(blank["content"], "")               # 不能是 None：DeepSeek 校验「content 或 tool_calls 必须存在」，None 会整个请求 400

    # ---- 空输出护栏：空白正文不算交付（曾把纯空格正文判为成功）----
    def test_blank_detection(self):
        for s in ("", None, "   ", "\n\n", "\t \n"):
            self.assertTrue(_blank(s), repr(s))
        self.assertFalse(_blank("## 有内容"))
        self.assertFalse(_blank(" 0 "))                       # 有字符即非空

    def test_empty_output_is_nudged_then_fails(self):
        from app.services import runner as runner_mod
        R = runner_mod.RUNNER
        tid, eid, uid = new_id("tsk"), new_id("ex"), new_id("u")
        db.insert("users", {"id": uid, "email": uid + "@t.io", "password_hash": "x", "display_name": "t",
                            "quota_monthly_cny": 10.0, "created_at": now_ms()})
        db.insert("tasks", {"id": tid, "user_id": uid, "title": "调研", "goal": "写一份调研", "mode": "auto",
                            "status": "running", "output_language": "zh", "budget_cny": 1.0,
                            "created_at": now_ms(), "updated_at": now_ms()})
        task = {"id": tid, "user_id": uid, "goal": "写一份调研", "output_language": "zh", "attachments": []}
        n = {"id": "nd_x", "node_key": "step1", "title": "调研", "description": "d", "acceptance": "a", "tools": [],
             "produces_sections": ["s1"], "model_tier": "standard", "est_complexity": 2, "agent": {},
             "revision_instructions": []}
        info = gateway.ModelInfo("probe", "byok", "d", "m", "openai", set())
        seen = []

        async def fake_chat(**kw):
            seen.append(kw["messages"])
            return {"content": "", "tool_calls": [], "reasoning": "", "cost": 0.0, "finish_reason": "stop"}

        old = gateway.chat
        gateway.chat = fake_chat
        R.exec_meta[eid] = {"speculative": False}
        try:
            with self.assertRaises(gateway.LLMError) as cm:
                asyncio.run(R.execute(task, 1, n, info, [], 1, eid))
            self.assertEqual(cm.exception.code, "EMPTY_OUTPUT")
            self.assertEqual(len(seen), 2)                     # 空白 → 纠正提示后再试一次
            self.assertIn("no body text", seen[1][-1]["content"])
        finally:
            gateway.chat = old
            R.exec_meta.pop(eid, None)

    # ---- 截断护栏：思考模型把预算烧在推理上（正文全空 + finish_reason=length）时加大预算重试 ----
    def test_length_truncation_raises_budget(self):
        from app.services import runner as runner_mod
        R = runner_mod.RUNNER
        tid, eid, uid = new_id("tsk"), new_id("ex"), new_id("u")
        db.insert("users", {"id": uid, "email": uid + "@t.io", "password_hash": "x", "display_name": "t",
                            "quota_monthly_cny": 10.0, "created_at": now_ms()})
        db.insert("tasks", {"id": tid, "user_id": uid, "title": "复习计划", "goal": "写复习计划", "mode": "auto",
                            "status": "running", "output_language": "zh", "budget_cny": 1.0,
                            "created_at": now_ms(), "updated_at": now_ms()})
        task = {"id": tid, "user_id": uid, "goal": "写复习计划", "output_language": "zh", "attachments": []}
        n = {"id": "nd_x", "node_key": "step1", "title": "复习计划", "description": "d", "acceptance": "a", "tools": [],
             "produces_sections": ["s1"], "model_tier": "standard", "est_complexity": 2, "agent": {},
             "revision_instructions": []}
        info = gateway.ModelInfo("probe", "byok", "d", "m", "openai", set())
        calls = []
        good = {"summary": "ok", "content": "## 每日安排\n完整正文",
                "sections": [{"section_key": "s1", "title": "每日安排", "content_markdown": "完整正文"}]}

        async def fake_chat(**kw):
            calls.append(kw)
            if len(calls) == 1:                                # 复现生产事故：推理烧光预算 → 正文空、被截断
                return {"content": "", "tool_calls": [], "reasoning": "推理烧光预算", "cost": 0.0, "finish_reason": "length"}
            return {"content": json.dumps(good, ensure_ascii=False), "tool_calls": [], "reasoning": "", "cost": 0.0, "finish_reason": "stop"}

        old = gateway.chat
        gateway.chat = fake_chat
        R.exec_meta[eid] = {"speculative": False}
        try:
            out, _, _ = asyncio.run(R.execute(task, 1, n, info, [], 1, eid))
            hint = R.out_budget_hint.get(tid + "|step1")       # finally 会清理，先取值
        finally:
            gateway.chat = old
            R.exec_meta.pop(eid, None)
            R.out_budget_hint.pop(tid + "|step1", None)
        self.assertEqual(calls[0]["max_out"], 6000)
        self.assertEqual(calls[1]["max_out"], 12000)           # 截断 → 预算翻倍后再试
        nudge = calls[1]["messages"][-1]
        self.assertEqual(nudge["role"], "system")
        self.assertIn("budget has been raised", nudge["content"])       # 明确告知已扩容、要求收敛
        sent_back = [m for m in calls[1]["messages"] if m.get("role") == "assistant"][-1]
        self.assertEqual(sent_back["content"], "")             # 回传的 assistant 不能是 null（否则 DeepSeek 400）
        self.assertEqual(sent_back["reasoning_content"], "推理烧光预算")
        self.assertTrue(out["content"].startswith("## 每日安排"))        # 第二轮拿到完整正文，节点成功
        self.assertEqual(hint, 12000)                          # 预算记忆留存，供下一次尝试沿用


    # ---- v1.8：人工编辑成果的合并规则（最后写入者胜出） ----
    def test_section_edit_merge_last_writer_wins(self):
        self.assertEqual(artifacts.merge_content("AI 稿", None, 100), ("AI 稿", False))
        self.assertEqual(artifacts.merge_content("AI 稿", {"content": "用户稿", "created_at": 200}, 100), ("用户稿", True))
        self.assertEqual(artifacts.merge_content("AI 新稿", {"content": "用户稿", "created_at": 100}, 200), ("AI 新稿", False))
        self.assertEqual(artifacts.merge_content("", {"content": "用户稿", "created_at": 100}, 200), ("用户稿", True))   # 步骤无产出：保留用户稿

    # ---- v1.8：用户手工修改的成果会进入模型上下文（「修改之后再进行新的对话」读得懂） ----
    def test_user_edit_feeds_model_context(self):
        from app.services.runner import RUNNER
        uid, tid = make_user_task()
        task = {"id": tid, "user_id": uid, "goal": "g", "output_language": "zh"}
        n = {"node_key": "down", "title": "预算", "description": "d", "acceptance": [], "produces_sections": ["预算"],
             "revision_instructions": [], "tools": [], "agent": {}}
        up = [{"node_key": "up", "finished_at": 100, "title": "行程", "summary": "", "content": "AI 旧稿", "claims": []}]

        def upstream_seen():
            msgs = RUNNER.messages_for(task, n, up)
            data = json.loads(re.search(r"<data>(.*)</data>", msgs[2]["content"], re.S).group(1))
            return data["upstream"][0]

        self.assertEqual(upstream_seen()["content"], "AI 旧稿")            # 没有编辑：用步骤产出
        db.insert("section_edits", {"id": new_id("sed"), "task_id": tid, "plan_version": 1, "section_key": "up", "user_id": uid,
                                    "content": "用户改的稿", "base_hash": "", "status": "active", "created_at": 200})
        seen = upstream_seen()
        self.assertEqual(seen["content"], "用户改的稿")                    # 编辑晚于上游完成：以用户编辑稿为准
        self.assertIn("已手工修改", seen["title"])                        # 并明确标注给模型（不是伪装成 AI 产物）
        up[0]["content"] = "AI 新稿"; up[0]["finished_at"] = 300          # 上游在编辑之后重跑：模型新稿生效
        seen = upstream_seen()
        self.assertEqual(seen["content"], "AI 新稿")
        self.assertNotIn("已手工修改", seen["title"])

    # ---- v1.9：地图数据校验（clean_map）与地图卡组装 ----
    def test_map_clean_and_cards(self):
        ok = {"title": "杭州两日游路线图", "city": "杭州", "markers": [
            {"name": "断桥残雪", "lat": 30.2585, "lng": 120.1495, "day": 1, "note": "起点"},
            {"name": "灵隐寺", "lat": 30.241, "lng": 120.098, "day": 2}]}
        cm = artifacts.clean_map(ok)
        self.assertEqual([m["name"] for m in cm["markers"]], ["断桥残雪", "灵隐寺"])
        self.assertEqual((cm["markers"][0]["day"], cm["markers"][1]["day"]), (1, 2))
        # 边界：点数不足 / 坐标越界 / 非数字 / 非 dict → 丢弃；剩不足 2 个 → None（宁缺毋滥）
        self.assertIsNone(artifacts.clean_map({"markers": [{"name": "A", "lat": 30, "lng": 120}]}))
        self.assertIsNone(artifacts.clean_map({"markers": [
            {"name": "A", "lat": 999, "lng": 120}, {"name": "B", "lat": "x", "lng": 120}]}))
        self.assertIsNone(artifacts.clean_map(None))
        self.assertIsNone(artifacts.clean_map({"markers": "not-a-list"}))
        # day 越界收敛到 1..7；空 name 丢弃
        cm = artifacts.clean_map({"markers": [
            {"name": "A", "lat": 30, "lng": 120, "day": 99}, {"name": "B", "lat": 30.1, "lng": 120.1, "day": 0},
            {"name": "", "lat": 30, "lng": 120}]})
        self.assertEqual([m["day"] for m in cm["markers"]], [7, 1])
        # 卡片组装：地图卡排最前（图 → 文 → 文件之前），标题缺省时用节点标题
        nodes = [
            {"node_key": "part1", "title": "行程路线", "output": {"map": cm}},
            {"node_key": "part2", "title": "预算", "output": {"chart": {"type": "pie", "title": "t",
                                                                        "labels": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}]}}},
        ]
        cards = artifacts.assemble_cards([], nodes, [])
        self.assertEqual([c["kind"] for c in cards], ["map", "chart"])      # 顺序：地图 → 图
        self.assertEqual((cards[0]["node_key"], cards[0]["title"]), ("part1", "行程路线"))

    # ---- v3.0 M3/M4：十类功能性节点数据契约（clean_*）与卡片顺序 ----
    def test_functional_node_cleaning(self):
        A = artifacts
        # 表格：列去重、行对齐（不足补 null、超出截断）、上限（列 ≤12 / 行 ≤50）、total_rows 兜底
        t = A.clean_table({"title": " 对比 ", "columns": [{"key": "name", "label": "名称"}, {"key": "name"}, {"key": "v", "align": "right"}],
                           "rows": [["A", 1, 2, "多出的列"], ["B"], "C"], "total_rows": "180", "note": "口径"})
        self.assertEqual([c["key"] for c in t["columns"]], ["name", "v"])          # 重复列丢弃
        self.assertEqual(t["columns"][1]["align"], "right")
        self.assertEqual(t["rows"][0], ["A", 1])                                   # 去重后只剩 2 列：多余单元格截断
        self.assertEqual(t["rows"][1], ["B", None])                                # 不足补空（补到列数）
        self.assertEqual(t["rows"][2][0], "C")                                     # 标量行按单列包成一行
        self.assertEqual(t["total_rows"], 180)
        self.assertIsNone(A.clean_table({"columns": [], "rows": []}))
        self.assertIsNone(A.clean_table({"columns": [{"key": "a"}], "rows": []}))
        self.assertIsNone(A.clean_table(None))
        wide = A.clean_table({"columns": [{"key": f"c{i}"} for i in range(20)], "rows": [[1] * 20]})
        self.assertEqual(len(wide["columns"]), A.MAX_TABLE_COLS)
        many = A.clean_table({"columns": [{"key": "a"}], "rows": [[i] for i in range(80)]})
        self.assertEqual(len(many["rows"]), A.MAX_TABLE_ROWS)
        # 指标：label/value 必填；dir / status 白名单（非法值回落）；NaN 值由 _cell 归 null
        m = A.clean_metrics({"items": [{"label": "营收", "value": "¥1.2M", "delta": "+12%", "dir": "up", "status": "warn"},
                                       {"label": "", "value": "x"}, {"label": "坏方向", "value": "1", "dir": "sideways", "status": "?"}]})
        self.assertEqual((m["items"][0]["dir"], m["items"][0]["status"]), ("up", "warn"))
        self.assertEqual(len(m["items"]), 2)
        self.assertEqual((m["items"][1]["dir"], m["items"][1]["status"]), ("", "good"))
        self.assertIsNone(A.clean_metrics({"items": []}))
        # 清单：默认 checklist；kanban 列 ≤5 且超出的分组并入最后一列（不丢条目）
        l = A.clean_list({"items": ["甲", {"text": "乙", "done": 1, "meta": "备注"}]})
        self.assertEqual((l["mode"], l["items"][1]["done"], l["items"][1]["text"]), ("checklist", True, "乙"))
        self.assertNotIn("columns", l)
        kb = A.clean_list({"mode": "kanban", "items": [{"text": f"t{i}", "column": f"列{i}"} for i in range(7)]})
        self.assertEqual(len(kb["columns"]), A.MAX_KANBAN_COLS)                    # 列上限 5
        self.assertEqual([i["text"] for i in kb["items"]], [f"t{i}" for i in range(7)])   # 条目一条不少
        self.assertTrue(all(i["column"] in kb["columns"] for i in kb["items"]))
        self.assertIsNone(A.clean_list({"items": []}))
        # 代码：语言白名单（非法回落 plain）、行数与字符数截断标记、空内容 None
        c = A.clean_code({"language": "PYTHON", "code": "print(1)\nprint(2)", "path": "a.py"})
        self.assertEqual(c["language"], "plain")                                    # 大小写不匹配白名单 → plain
        self.assertFalse(c["truncated"])
        big = A.clean_code({"language": "py", "code": "\n".join(f"l{i}" for i in range(500))})
        self.assertTrue(big["truncated"])
        self.assertEqual(len(big["code"].split("\n")), A.MAX_CODE_LINES)
        self.assertIsNone(A.clean_code({"code": "   "}))
        self.assertIsNone(A.clean_code({}))
        # 图片：必须是该用户工作区内真实存在的图片文件（沙箱解析，防路径逃逸）
        uid = new_id("usr")
        workspace.write(uid, "交付/封面.svg", "<svg/>")
        ok_img = A.clean_image({"path": "交付/封面.svg", "caption": "封面"}, uid)
        self.assertEqual(ok_img["path"], "交付/封面.svg")
        self.assertIsNone(A.clean_image({"path": "交付/不存在.png"}, uid))            # 文件不存在 → 丢弃
        self.assertIsNone(A.clean_image({"path": "../escape.png"}, uid))              # 越界路径 → 丢弃
        self.assertIsNone(A.clean_image({"path": "交付/封面.exe"}, uid))              # 非图片扩展名 → 丢弃
        self.assertEqual(A.clean_image({"path": "远程/图.png", "source_url": "ftp://x"})["source_url"], "")   # 非 http(s) 外链清空
        # 时间线：<2 条丢弃；字符串条目归一为 {text}
        t2 = A.clean_timeline({"items": ["开工", {"time": "2026-10", "text": "上线", "note": "里程碑"}]})
        self.assertEqual([x["text"] for x in t2["items"]], ["开工", "上线"])
        self.assertIsNone(A.clean_timeline({"items": ["只有一条"]}))
        # 日历：day 收敛 1..7；无标题事件丢弃
        cal = A.clean_calendar({"week_start": "2026-10-05", "events": [{"day": 99, "title": "评审", "start": "09:00"},
                                                                      {"day": 0, "title": "复盘"}, {"day": 2, "title": ""}]})
        self.assertEqual([e["day"] for e in cal["events"]], [7, 1])
        self.assertIsNone(A.clean_calendar({"events": []}))
        # 状态检查：state 白名单（非法回落 warn）
        st = A.clean_status({"items": [{"name": "接口", "state": "ok", "detail": "200"},
                                       {"name": "磁盘", "state": "什么"}, {"name": ""}]})
        self.assertEqual([(x["name"], x["state"]) for x in st["items"]], [("接口", "ok"), ("磁盘", "warn")])
        self.assertIsNone(A.clean_status({"items": []}))
        # 会话：<2 条丢弃；role 归一（只有 user 是用户，其余是对方）
        ch = A.clean_chat({"messages": [{"who": "我", "role": "user", "text": "你好"},
                                        {"who": "客服", "role": "agent", "text": "收到"}, {"who": "x", "text": ""}]})
        self.assertEqual([m["role"] for m in ch["messages"]], ["user", "other"])
        self.assertIsNone(A.clean_chat({"messages": [{"text": "只有一条"}]}))
        # 网页快照：url 必须 http(s)
        w = A.clean_web({"title": "T", "url": "https://example.com/a", "excerpt": "摘要"})
        self.assertEqual(w["url"], "https://example.com/a")
        self.assertIsNone(A.clean_web({"title": "T", "url": "javascript:alert(1)"}))
        self.assertIsNone(A.clean_web({"title": "T"}))

    def test_functional_cards_order_and_caps(self):
        """卡片顺序：空间/数据类在前、叙述类在后、附件兜底；同类按上限截断（每步只出一份）。"""
        def node(k, kind, key=None):
            return {"node_key": key or k, "title": k, "output": {kind: {"title": k, "url": "https://e.com" if kind == "web" else None}}}
        nodes = [
            node("n_chat", "chat"), node("n_chart", "chart"), node("n_code", "code"), node("n_cal", "calendar"),
            node("n_tbl", "table"), node("n_img", "image"), node("n_list", "list"), node("n_map", "map"),
            node("n_met", "metrics"), node("n_st", "status"), node("n_tl", "timeline"), node("n_web", "web"),
        ]
        kinds = [c["kind"] for c in artifacts.assemble_cards([], nodes, [])]
        self.assertEqual(kinds, ["map", "table", "metrics", "chart", "list", "timeline", "calendar",
                                 "image", "code", "web", "status", "chat"])          # 顺序 = CARD_ORDER（doc/file 之外）
        # 上限：table 同类第 3 个不提；每类按 node_key 去重
        many = [{"node_key": f"t{i}", "title": f"t{i}", "output": {"table": {"title": "T", "columns": [{"key": "a"}], "rows": [[1]]}}} for i in range(4)]
        self.assertEqual(len([c for c in artifacts.assemble_cards([], many, []) if c["kind"] == "table"]), 2)
        dup = [{"node_key": "same", "title": "A", "output": {"list": {"items": ["x"]}}},
               {"node_key": "same", "title": "A", "output": {"list": {"items": ["x"]}}}]
        self.assertEqual(len([c for c in artifacts.assemble_cards([], dup, []) if c["kind"] == "list"]), 1)


    def test_places_resolve_and_tool(self):
        from app.core import places
        from app.services import kb
        self.assertEqual(places.resolve("断桥残雪"), (30.2585, 120.1495))
        self.assertEqual(places.resolve("杭州西湖"), (30.247, 120.147))     # 复合名取具体地点（位置最靠后的命中项）
        self.assertIsNone(places.resolve("不存在的地方xyz"))
        r = places.resolve_many(["灵隐寺", "不存在的地方xyz"])
        self.assertEqual((r[0]["found"], r[0]["lat"], r[0]["city"]), (True, 30.241, "杭州"))
        self.assertEqual(r[1]["found"], False)
        self.assertIn("map.resolve", kb.TOOL_SCHEMAS)
        out = kb.run_tool("map.resolve", {"places": ["断桥残雪", "灵隐寺"]}, user_id="u", attachments=[], web_enabled=False)
        self.assertEqual([i["name"] for i in out["items"]], ["断桥残雪", "灵隐寺"])
        self.assertTrue(all(i["found"] and i["lat"] and i["lng"] for i in out["items"]))


class WechatUnits(unittest.TestCase):
    """F30 微信扫码登录：票据生命周期、身份绑定与配置降级（fake 模式，不连外网）。"""

    def setUp(self):
        self._saved = (config.WECHAT_MODE, config.WECHAT_APPID, config.WECHAT_SECRET, config.WECHAT_REDIRECT_URI)
        config.WECHAT_MODE, config.WECHAT_APPID = "fake", "wx_unit_appid"
        config.WECHAT_SECRET = config.WECHAT_REDIRECT_URI = ""

    def tearDown(self):
        config.WECHAT_MODE, config.WECHAT_APPID, config.WECHAT_SECRET, config.WECHAT_REDIRECT_URI = self._saved

    # ---- 登录后的跳转只允许站内路径（防开放重定向） ----
    def test_safe_next_rejects_external(self):
        self.assertEqual(wechat.safe_next("/task/abc"), "/task/abc")
        for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "", None, "task/abc"):
            self.assertEqual(wechat.safe_next(bad), "/", bad)

    # ---- 票据：未确认不能取件；取件一次性；确认后不可二次确认 ----
    def test_ticket_lifecycle_is_one_shot(self):
        t = wechat.new_ticket("/task/x")
        self.assertEqual(wechat.get_ticket(t["id"])["status"], "pending")
        self.assertEqual(wechat.get_ticket(t["id"])["next"], "/task/x")
        self.assertIsNone(wechat.consume(t["id"]))
        self.assertTrue(wechat.confirm(t["id"], "usr_demo"))
        self.assertEqual(wechat.consume(t["id"]), "usr_demo")
        self.assertIsNone(wechat.consume(t["id"]))
        self.assertFalse(wechat.confirm(t["id"], "usr_other"))

    def test_expired_ticket_rejected_and_purged(self):
        t = wechat.new_ticket("/")
        db.update("auth_tickets", {"id": t["id"]}, {"expires_at": now_ms() - 1})
        self.assertTrue(wechat.is_expired(wechat.get_ticket(t["id"])))
        self.assertFalse(wechat.confirm(t["id"], "usr_demo"))
        self.assertGreaterEqual(wechat.purge(), 1)
        self.assertIsNone(wechat.get_ticket(t["id"]))

    # ---- 身份绑定：同一微信复用账号；unionid 相同则跨应用归并到同一账号 ----
    def test_identity_binding_reuses_account(self):
        a = wechat.login_or_create("appid1", "openid_u1", "union_u1", {"nickname": "小明", "avatar": ""})
        b = wechat.login_or_create("appid1", "openid_u1", "union_u1", {"nickname": "小明", "avatar": ""})
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["display_name"], "小明")
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM wechat_identities "
                              "WHERE appid='appid1' AND openid='openid_u1'")["n"], 1)
        c = wechat.login_or_create("appid2", "openid_u1b", "union_u1", {})       # 同 unionid、另一应用
        self.assertEqual(c["id"], a["id"])
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM users WHERE id=?", (a["id"],))["n"], 1)

    # ---- 微信账号没有邮箱、也没有可用密码 ----
    def test_wechat_account_has_no_usable_password(self):
        u = wechat.login_or_create("appid1", "openid_u2", "", {})
        self.assertTrue(u["email"].endswith("@wechat.local"))
        self.assertEqual(u["display_name"], "微信用户")
        self.assertFalse(verify_password("", u["password_hash"]))
        self.assertFalse(verify_password("password1", u["password_hash"]))

    # ---- 配置矩阵：live 下缺 secret 或回调地址一律视为未启用 ----
    def test_enabled_matrix(self):
        config.WECHAT_MODE = "off"
        self.assertFalse(wechat.enabled())
        config.WECHAT_MODE = "fake"
        self.assertTrue(wechat.enabled())
        config.WECHAT_APPID = ""
        self.assertTrue(wechat.enabled())                 # fake 模式无需任何凭据
        config.WECHAT_APPID = "wx_unit_appid"
        config.WECHAT_MODE = "live"
        self.assertFalse(wechat.enabled())
        config.WECHAT_SECRET, config.WECHAT_REDIRECT_URI = "secret", "https://x.io/api/auth/wechat/callback"
        self.assertTrue(wechat.enabled())
        config.WECHAT_APPID = ""
        self.assertFalse(wechat.enabled())

    def test_qr_target_per_mode(self):
        url = wechat.qr_target("st1", "http://127.0.0.1:8765/")
        self.assertIn("/api/auth/wechat/mock/authorize", url)
        self.assertIn("state=st1", url)
        config.WECHAT_MODE = "live"
        config.WECHAT_REDIRECT_URI = "https://x.io/api/auth/wechat/callback"
        url = wechat.qr_target("st2", "http://127.0.0.1:8765/")
        self.assertTrue(url.startswith("https://open.weixin.qq.com/connect/qrconnect"))
        self.assertIn("scope=snsapi_login", url)
        self.assertIn("state=st2", url)

    # ---- fake 模式：code 换 openid 是确定性的，同一身份重登落同一账号 ----
    def test_exchange_code_deterministic_in_fake_mode(self):
        a, b = wechat.exchange_code("mock_alice"), wechat.exchange_code("mock_alice")
        self.assertEqual(a["openid"], b["openid"])
        self.assertNotEqual(a["openid"], wechat.exchange_code("mock_bob")["openid"])
        with self.assertRaises(ApiError):
            wechat.exchange_code("mock_")


def tearDownModule():
    db.reset_connection()
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
