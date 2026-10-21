"""外部 Agent 接入测试（v3.0 M1–M4 · prd/08 §4/§5/§9 AC-A）。

覆盖：
- 目录完整性（12 域 / 46 条 / 首批 5+1 / 精选工具集 ≤12）；
- 连接生命周期（stdio 真实握手：安装 → 连接 → 调用 → 审计 → 断开）；
- 凭据加密（密文落库、明文只在内存、接口只回掩码）；
- SSRF 拒绝（私网端点）、OAuth 未开放提示、缺凭据提示、坏命令友好报错；
- 写工具门控与审批（社区级强制审批、official 随全局开关）；
- 匹配 / 需求清洗 / 提示词摘要（一级暴露硬上限）/ 工具定义 / 函数名反查；
- 真实 HTTP 层：目录、连接、调用、密钥不回显。
- M2 调用决策引擎：判据 C1–C5 / 边界 N1 / 三档模式 / 漏斗二评分（噪声阈值 + 权重公式）/ 目录摘要上限 /
  两级暴露（单节点 ≤2 server ≤24 工具、单任务 ≤5 server）/ `agent_needs` 契约 / 执行期外发闸门；
- M4：A2A agent card 发现 + message/send 委派（本地 HTTP 真服务）、本地 CLI 接入（真实子进程往返、自托管专享）。

运行：  python -m unittest tests/test_agents.py -v      （在 combine/ 目录下）
"""
from __future__ import annotations

import atexit
import http.cookiejar
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="combine-agents-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)   # 本文件多个测试类共用数据目录，进程退出时统一清理
os.environ.update(DATA_DIR=_TMP, DB_PATH=os.path.join(_TMP, "combine.db"), COMBINE_IGNORE_DOTENV="1",
                  FAKE_LLM="1", FAKE_LATENCY_SCALE="0.05", DEPLOY_MODE="self_hosted", WEB_SEARCH_MODE="fake")
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app import config, db  # noqa: E402
from app.core import decision, matching, policy  # noqa: E402
from app.services import agents, kb, plans, runner  # noqa: E402
from app.util import ApiError, new_id, now_ms  # noqa: E402

SETTINGS = {"agents.enabled": True, "approval.ask_dangerous": False}


def make_user() -> str:
    uid = new_id("usr")
    db.insert("users", {"id": uid, "email": uid + "@t.io", "password_hash": "x", "display_name": "u",
                        "quota_monthly_cny": 50.0, "created_at": now_ms()})
    return uid


class AgentsCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        matching.seed_capabilities()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        # 数据目录由模块级 atexit 统一清理：本文件里多个测试类共用 _TMP，在这里删会让后续类连不上库

    def setUp(self):
        self.uid = make_user()

    # ---------- 目录 ----------
    def test_catalog_integrity(self):
        es = agents.entries()
        self.assertGreaterEqual(len(es), 40)
        self.assertEqual(len(agents.domains()), 12)
        keys = [e["key"] for e in es]
        self.assertEqual(len(keys), len(set(keys)))
        for e in es:
            self.assertTrue(e["key"].startswith("mcp."))
            self.assertIn(e["transport"], ("stdio", "http"), e["key"])
            self.assertIn(e["auth"], ("none", "env", "oauth"), e["key"])
            self.assertIn(e["trust"], ("official", "verified", "community", "local"), e["key"])
            self.assertTrue(e.get("description"), e["key"])
            self.assertLessEqual(len(e.get("toolset") or []), 12, e["key"])
            if e["transport"] == "stdio":
                self.assertTrue(e.get("command") or e.get("builtin"), e["key"])
            else:
                self.assertTrue((e.get("endpoint") or "").startswith("https://"), e["key"])
        featured = {e["key"] for e in es if e.get("featured")}
        for k in ("mcp.context7", "mcp.github", "mcp.playwright", "mcp.sqlite", "mcp.filesystem"):
            self.assertIn(k, featured)
        feat = [e for e in es if e.get("featured")]
        self.assertGreaterEqual(len(feat), 30)                                            # 首批 ≥30（M4 扩面目标）
        self.assertEqual({e["domain"] for e in feat}, {d["key"] for d in agents.domains()})   # 12 域全覆盖
        self.assertTrue(all("risk" in t for e in es for t in (e.get("toolset") or [])))

    # ---------- 生命周期（真实 stdio 握手） ----------
    def test_lifecycle_install_connect_call_disconnect(self):
        row = agents.install(self.uid, "mcp.demo")
        self.assertEqual(row["status"], "disconnected")
        r = agents.connect(self.uid, "mcp.demo")
        self.assertTrue(r["ok"], r)
        names = [t["name"] for t in r["agent"]["tools"]]
        self.assertEqual(names, ["now", "calc", "echo", "recent_days"])
        self.assertTrue(all(t["risk"] == "read" for t in r["agent"]["tools"]))

        out = agents.call(self.uid, "mcp.demo.calc", {"expression": "(12.5*3+7)/2"}, task_id="t1", node_key="n1")
        self.assertEqual(out.get("content"), "22.25")
        out2 = agents.call(self.uid, "mcp.demo.echo", {"text": "你好"})
        self.assertEqual(out2.get("content"), "你好")

        rows = db.q("SELECT tool, status, result_bytes FROM agent_calls WHERE user_id=?", (self.uid,))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(x["status"] == "ok" for x in rows))
        self.assertTrue(all("args" not in x for x in rows))          # 审计不存参数明文

        d = agents.disconnect(self.uid, "mcp.demo")
        self.assertEqual(d["status"], "disconnected")
        self.assertEqual(agents.tool_index(self.uid), {})
        self.assertEqual(agents.stored_values(self.uid, "mcp.demo"), {})
        self.assertEqual(agents.call(self.uid, "mcp.demo.now", {}).get("error"), "TOOL_DENIED")

    def test_bad_command_friendly_error(self):
        agents.install(self.uid, custom={"name": "bad", "transport": "stdio", "command": "definitely-not-a-cmd-xyz", "args": []})
        key = [r["key"] for r in agents.installed(self.uid)][0]
        r = agents.connect(self.uid, key)
        self.assertFalse(r["ok"])
        self.assertIn("未找到命令", r["message"])
        self.assertEqual(agents.get(self.uid, key)["status"], "error")

    # ---------- 凭据 ----------
    def test_credentials_encrypted_masked_never_echoed(self):
        r = agents.install(self.uid, custom={"name": "sec", "transport": "stdio", "command": "python", "args": [],
                                             "env": {"API_TOKEN": "sk-secret-123456"}})
        key = r["key"]
        payload = db.q1("SELECT payload FROM agent_credentials WHERE user_id=? AND agent_key=?", (self.uid, key))
        self.assertNotIn("sk-secret", payload["payload"])            # 密文落库
        self.assertEqual(agents.stored_values(self.uid, key), {"API_TOKEN": "sk-secret-123456"})
        pub = agents.public(agents.get(self.uid, key))
        self.assertNotIn("sk-secret", json.dumps(pub, ensure_ascii=False))
        self.assertIn("••••", pub["cred_hint"])

    def test_missing_credential_raises(self):
        agents.install(self.uid, "mcp.exa")
        with self.assertRaises(ApiError) as cm:
            agents.resolve_runtime(self.uid, "mcp.exa", {})
        self.assertEqual(cm.exception.code, "AGENT_CRED_MISSING")

    def test_oauth_deferred_with_clear_message(self):
        agents.install(self.uid, "mcp.linear")
        with self.assertRaises(ApiError) as cm:
            agents.connect(self.uid, "mcp.linear", {})
        self.assertEqual(cm.exception.code, "AGENT_OAUTH_PENDING")

    # ---------- 安全 ----------
    def test_ssrf_private_endpoint_blocked(self):
        r = agents.install(self.uid, custom={"name": "evil", "transport": "http", "endpoint": "http://127.0.0.1:9999/mcp"})
        key = r["key"]
        rt = agents.resolve_runtime(self.uid, key, {})
        probe = agents.probe_runtime(rt, timeout=3)
        self.assertEqual(probe.get("error"), "BLOCKED")

    def test_custom_http_requires_valid_endpoint(self):
        with self.assertRaises(ApiError):
            agents.install(self.uid, custom={"name": "x", "transport": "http", "endpoint": "ftp://a/b"})

    # ---------- 写工具门控与审批 ----------
    def _connect_demo_with_write_tool(self, trust="community"):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        row = agents.get(self.uid, "mcp.demo")
        tool = {"name": "create_note", "desc": "写一条笔记", "risk": "write",
                "schema": {"type": "object", "properties": {}}, "fn": agents.fn_name("mcp.demo", "create_note")}
        db.update("agents", {"user_id": self.uid, "key": "mcp.demo"},
                  {"tools": [*row["tools"], tool], "tool_count": len(row["tools"]) + 1, "trust": trust})
        return "mcp.demo.create_note"

    def test_write_tool_is_gated_by_allow_write(self):
        full = self._connect_demo_with_write_tool()
        self.assertEqual(policy.allowed([full], SETTINGS, self.uid), [])          # 默认不出写工具
        out = agents.call(self.uid, full, {})
        self.assertEqual(out.get("error"), "WRITE_NOT_ALLOWED")
        agents.patch(self.uid, "mcp.demo", {"allow_write": True})
        self.assertEqual(policy.allowed([full], SETTINGS, self.uid), [full])       # 开启后允许执行…

    def test_write_approval_rules(self):
        full = self._connect_demo_with_write_tool(trust="community")
        agents.patch(self.uid, "mcp.demo", {"allow_write": True})
        self.assertTrue(policy.approval_required(full, SETTINGS, self.uid))        # 社区级：强制审批（不看全局开关）
        db.update("agents", {"user_id": self.uid, "key": "mcp.demo"}, {"trust": "official"})
        self.assertFalse(policy.approval_required(full, SETTINGS, self.uid))       # 官方级：默认不打断
        self.assertTrue(policy.approval_required(full, {**SETTINGS, "approval.ask_dangerous": True}, self.uid))
        self.assertFalse(policy.approval_required("mcp.demo.now", SETTINGS, self.uid))   # 只读永不打断

    def test_global_switch_off_blocks_all_external(self):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        off = {**SETTINGS, "agents.enabled": False}
        self.assertEqual(policy.allowed(["mcp.demo.now"], off, self.uid), [])
        self.assertIn("mcp.demo.now", policy.allowed(["mcp.demo.now"], SETTINGS, self.uid))

    # ---------- 匹配 / 计划 / 提示词 ----------
    def test_requirements_and_matching_keep_external_tools(self):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        req = plans.clean_requirements({"tags": ["research"], "tools": ["mcp.demo.now", "mcp.zz.nope", "kb.search"]}, self.uid)
        self.assertEqual(req["tools"], ["mcp.demo.now", "kb.search"])
        a = matching.match({"tags": ["research"], "tools": ["mcp.demo.now"], "permission": "read"}, self.uid)
        self.assertIn("mcp.demo.now", a["tools"])                    # 外部工具随节点白名单下传
        self.assertGreater(a["breakdown"]["tool"], 0.9)              # 覆盖率只按内置口径算，不被外部工具拉低

    def test_prompt_digest_hard_caps(self):
        def mk(i):                                                   # 造第 i 个已连接 Agent，各 10 个只读工具
            db.insert("agents", {"id": new_id("agt"), "user_id": self.uid, "key": f"mcp.s{i}", "name": f"S{i}", "name_en": f"S{i}",
                                 "kind": "mcp", "domain": "knowledge", "args": [], "headers": {}, "toolset": [],
                                 "tools": [{"name": f"t{j}", "desc": "", "risk": "read", "schema": {}} for j in range(10)],
                                 "tool_count": 10, "status": "connected", "enabled": 1, "trust": "community",
                                 "created_at": now_ms(), "updated_at": now_ms()})
        for i in range(6):
            mk(i)
        d = agents.prompt_digest(self.uid, SETTINGS)
        parts = d.split(" | ")
        self.assertEqual(len(parts), 5)                              # ≤5 个 Agent
        for p in parts:
            seg = p.split(": ", 1)[1].split(", +")[0]                # 去 "mcp.sN: " 前缀与 "+N more" 尾巴
            self.assertLessEqual(len(seg.split(", ")), 8)            # 每个 ≤8 个工具名
        self.assertIn("+2 more", parts[0])                           # 被截断的余量要显式标出
        base = len(policy.prompt_tools(SETTINGS, "u_nobody"))        # 未接任何 Agent 时的基线
        line = policy.prompt_tools(SETTINGS, self.uid)
        self.assertIn("Connected external agents", line)
        self.assertLess(len(line) - base, 520)                       # 一级暴露固定开销可控（引导语 + 5×8 工具名）
        for i in range(6, 12):                                       # 再接入 6 个 Agent 也不增长（硬上限）
            mk(i)
        self.assertEqual(len(policy.prompt_tools(SETTINGS, self.uid)) - base, len(line) - base)

    def test_tool_defs_and_fn_map_roundtrip(self):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        defs = kb.tool_defs(["mcp.demo.now", "calc.eval"], self.uid)
        names = [d["function"]["name"] for d in defs]
        self.assertIn("mcp__demo__now", names)
        self.assertIn("calc_eval", names)
        fm = agents.fn_map(self.uid)
        self.assertEqual(fm.get("mcp__demo__now"), "mcp.demo.now")
        now_def = next(d for d in defs if d["function"]["name"] == "mcp__demo__now")
        self.assertEqual(now_def["function"]["parameters"].get("type"), "object")

    def test_health_refreshes_status(self):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        rows = agents.health(self.uid)
        self.assertEqual([(x["key"], x["status"]) for x in rows], [("mcp.demo", "connected")])


# ==================== M2 调用决策引擎：判据 / 边界 / 模式（prd/08 §5.1–§5.2） ====================
class DecisionCriteria(unittest.TestCase):
    """「什么时候值得调外部 Agent」的规则引擎：确定性纯函数，同样输入同样输出。"""

    @classmethod
    def setUpClass(cls):
        matching.seed_capabilities()

    #: 每条判据配一句只命中它、不命中其他的真实说法
    CASES = [
        ("C1", "今天上海的天气和英伟达的股价"),
        ("C2", "把这份周报发到群里"),
        ("C3", "看看我的仓库里有哪些未关闭的 issue"),
        ("C4", "运行一下这个脚本，把结果截图给我"),
        ("C5", "帮我找这篇论文的原文和官方文档"),
    ]

    def test_each_criterion_has_evidence(self):
        for cid, goal in self.CASES:
            a = decision.assess(goal)
            hit = [c for c in a["criteria"] if c["id"] == cid]
            self.assertTrue(hit, goal)
            self.assertTrue(hit[0]["hits"], goal)                 # 命中必须附证据词（用户可核对，不是黑箱）
            self.assertEqual(a["boundaries"], [], goal)

    def test_pure_language_is_n1_and_explained(self):
        a = decision.assess("写一篇关于春天的短文，语言优美一些")
        self.assertEqual(a["criteria"], [])
        self.assertEqual([b["id"] for b in a["boundaries"]], ["N1"])
        self.assertIn("N1", decision.summary(a))                  # 「什么时候不调」同样对用户透明

    def test_criteria_win_over_n1(self):
        # 一句话里既有写作、又需要此刻为真的事实：判据优先。「不调」是节点级的，不是整任务级的
        a = decision.assess("写一段分析，需要包含今天的英伟达股价")
        self.assertEqual([c["id"] for c in a["criteria"]], ["C1"])
        self.assertEqual(a["boundaries"], [])

    def test_deterministic_and_domain_order(self):
        g = "查一下我的仓库的 issue，并运行测试脚本"
        self.assertEqual(decision.assess(g), decision.assess(g))
        a = decision.assess(g)
        self.assertEqual(a["domains"], ["code", "search"])        # 建议域固定顺序
        self.assertIn("C3", [c["id"] for c in a["criteria"]])

    def test_hint_covers_three_modes_bilingually(self):
        a = decision.assess("今天天气")
        h = {m: decision.hint("zh", m, a) for m in ("conservative", "balanced", "aggressive")}
        self.assertEqual(len(set(h.values())), 3)                 # 三档产出不同指引
        self.assertIn("保守档", h["conservative"])
        self.assertIn("平衡档", h["balanced"])
        self.assertIn("激进档", h["aggressive"])
        for txt in h.values():
            self.assertIn("agent_needs", txt)                     # 契约字段名不翻译
            self.assertIn("C1", txt)
        en = decision.hint("en", "balanced", a)
        self.assertIn("balanced mode", en)
        self.assertNotIn("平衡档", en)


# ==================== M2 两级暴露硬上限（prd/08 §5.4） ====================
class ExposureCaps(unittest.TestCase):
    """目录摘要（一级暴露）字符上限 + 单节点 / 单任务挂载上限（两级暴露的硬约束）。"""

    @classmethod
    def setUpClass(cls):
        matching.seed_capabilities()

    def setUp(self):
        self.uid = make_user()

    def _fake_connected(self, key: str, tools: list[str]) -> None:
        """直接造「已连接」行（不真实握手），用于验证挂载上限。"""
        db.insert("agents", {"id": new_id("agt"), "user_id": self.uid, "key": key, "name": key, "name_en": key,
                             "kind": "mcp", "domain": "knowledge", "args": [], "headers": {}, "toolset": [],
                             "tools": [{"name": t, "desc": "", "risk": "read", "schema": {}} for t in tools],
                             "tool_count": len(tools), "status": "connected", "enabled": 1, "trust": "community",
                             "created_at": now_ms(), "updated_at": now_ms()})

    def test_catalog_digest_is_hard_bounded(self):
        d = agents.catalog_digest(self.uid, SETTINGS, ["code"])
        self.assertLessEqual(len(d), agents.MAX_CATALOG_DIGEST_CHARS)     # 目录涨到 300 条也不变
        self.assertTrue(d.startswith("code: "))                           # 提示域排最前
        self.assertEqual(agents.catalog_digest(self.uid, {**SETTINGS, "agents.enabled": False}), "")

    def test_digest_marks_connected(self):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        d = agents.catalog_digest(self.uid, SETTINGS, ["knowledge"])       # 提示域排最前，确保落在字符上限之内
        self.assertIn("内置演示 Agent✓", d)

    def test_node_tools_caps_servers_and_tools(self):
        for i in range(3):
            self._fake_connected(f"mcp.srv{i}", [f"t{j}" for j in range(10)])
        out = agents.node_tools(self.uid, {"agents": {"attached": [f"mcp.srv{i}.t{j}" for i in range(3) for j in range(10)]}}, SETTINGS)
        self.assertEqual(len({t.split(".")[1] for t in out}), agents.MAX_SERVERS_PER_NODE)   # 单节点 ≤2 server（用满）
        self.assertLessEqual(len(out), agents.MAX_TOOLS_PER_NODE)                            # 单节点 ≤24 工具
        self._fake_connected("mcp.big", [f"x{j}" for j in range(30)])
        out2 = agents.node_tools(self.uid, {"agents": {"attached": [f"mcp.big.x{j}" for j in range(30)]}}, SETTINGS)
        self.assertEqual(len(out2), agents.MAX_TOOLS_PER_NODE)                               # 工具数上限独立生效

    def test_node_tools_gated_by_connected_index(self):
        self._fake_connected("mcp.srv0", ["t0"])
        # 不在已连接索引里的工具一律不挂（防伪造 / 断连后残留）
        out = agents.node_tools(self.uid, {"agents": {"attached": ["mcp.srv0.t0", "mcp.nope.x"]}, "tools": ["mcp.srv0.t0"]}, SETTINGS)
        self.assertEqual(out, ["mcp.srv0.t0"])
        self.assertEqual(agents.node_tools(self.uid, {"agents": {"attached": ["mcp.srv0.t0"]}}, {**SETTINGS, "agents.enabled": False}), [])

    def test_task_server_cap_drops_lowest_scored_node(self):
        ns = [{"node_key": f"n{i}", "agents": {"primary": {"score": 0.9 - i * 0.05}, "attached": [f"mcp.s{i}.t0"]}}
              for i in range(6)]
        dropped = agents.cap_task_servers(ns)
        self.assertEqual(dropped, ["n5"])                                     # 单任务 ≤5 server：低分节点整组剔除
        self.assertEqual(ns[5]["agents"]["attached"], [])
        self.assertTrue(ns[5]["agents"]["capped"])
        self.assertFalse(ns[5]["agents"]["used"])
        for n in ns[:5]:
            self.assertFalse(n["agents"].get("capped"))
            self.assertTrue(n["agents"]["attached"])


# ==================== M2 漏斗二评分与节点决策（prd/08 §5.3） ====================
class FunnelTwo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        matching.seed_capabilities()

    def setUp(self):
        self.uid = make_user()

    def test_noise_threshold_on_coverage(self):
        # 阈值必须真正生效：曾用总分判定，而信任/权限/成本/稳定性在任何条目上近乎常量（自带约 0.45 基线），
        # 导致「量子纠缠态制备」匹配到 AWS（覆盖 6%）、「把周报发到飞书」匹配到本地文件系统（覆盖 0%）。
        for needs, perm in ([["制备量子纠缠态的实验方案与误差分析"], "read"], [["把周报发到飞书"], "write"]):
            m = agents.match_catalog(self.uid, needs, [], perm, SETTINGS)
            self.assertIsNone(m["primary"], needs)
            self.assertEqual(m["alternates"], [])

    def test_pick_primary_alternates_and_weight_math(self):
        m = agents.match_catalog(self.uid, ["读取我的仓库的 open issues"], ["mcp.github"], "read", SETTINGS)
        p = m["primary"]
        self.assertEqual(p["key"], "mcp.github")
        self.assertLessEqual(len(m["alternates"]), 2)               # primary + ≤2 备选
        b = p["breakdown"]
        expect = (0.45 * b["coverage"] + 0.20 * b["trust"] + 0.15 * b["perm"]
                  + 0.10 * b["cost"] + 0.10 * b["stability"])       # 权重公式可核对
        self.assertAlmostEqual(p["score"], round(expect, 4), places=4)
        self.assertIn("能力覆盖", m["reason"])                       # 匹配理由对用户可解释
        prev = 1.1
        for x in [p, *m["alternates"]]:
            self.assertGreaterEqual(x["breakdown"]["coverage"], agents.MIN_COVERAGE)   # 低覆盖不提
            self.assertLessEqual(x["score"], prev)                                      # 按分数降序（含并列稳定序）
            prev = x["score"]

    def test_same_input_same_output(self):
        a = agents.match_catalog(self.uid, ["读取我的仓库的 open issues"], ["mcp.github"], "read", SETTINGS)
        b = agents.match_catalog(self.uid, ["读取我的仓库的 open issues"], ["mcp.github"], "read", SETTINGS)
        self.assertEqual(a, b)

    def test_write_permission_requires_write_tools(self):
        m = agents.match_catalog(self.uid, ["把内容写入本地文件系统目录"], [], "write", SETTINGS)
        picks = [x for x in [m["primary"], *m["alternates"]] if x]
        self.assertTrue(picks, "write 权限下应仍有具备写工具的条目")
        for x in picks:
            ts = agents.index()[x["key"]].get("toolset") or []
            self.assertTrue(any(str(t.get("risk")) in ("write", "delete") for t in ts), x["key"])

    def test_n4_global_switch_off(self):
        off = {**SETTINGS, "agents.enabled": False}
        self.assertIsNone(agents.match_catalog(self.uid, ["今天天气"], [], "read", off))     # 漏斗二整体关闭
        ap = agents.plan_node(self.uid, {"node_key": "n", "agent_needs": ["查今天天气"]}, off)
        self.assertFalse(ap["used"])
        self.assertEqual(ap["why"], "N4")
        self.assertEqual(ap["attached"], [])

    def test_plan_node_contract(self):
        self.assertIsNone(agents.plan_node(self.uid, {"node_key": "n0"}, SETTINGS))          # 无需求 → 不展示外部信息
        ap = agents.plan_node(self.uid, {"node_key": "n1", "agent_needs": ["读取我的仓库的 open issues"]}, SETTINGS)
        self.assertEqual(ap["mode"], "balanced")                                             # 默认平衡档
        self.assertEqual(ap["primary"]["key"], "mcp.github")
        self.assertFalse(ap["primary"]["connected"])
        self.assertFalse(ap["used"])
        self.assertEqual(ap["attached"], [])
        self.assertIn("mcp.github", ap["gap"])                                               # 非阻塞连接引导

    def test_plan_node_attaches_readonly_tools_only(self):
        agents.install(self.uid, "mcp.demo")
        agents.connect(self.uid, "mcp.demo")
        ap = agents.plan_node(self.uid, {"node_key": "n2", "capability_requirements":
                                         {"tools": ["mcp.demo.now"], "permission": "read"}}, SETTINGS)
        self.assertTrue(ap["used"])
        self.assertEqual(ap["attached"][0], "mcp.demo.now")             # 显式点名的已连接工具排最前
        row = agents.get(self.uid, "mcp.demo")
        reads = {f"mcp.demo.{t['name']}" for t in row["tools"] if t["risk"] == "read"}
        self.assertTrue(reads <= set(ap["attached"]))                   # 精选只读工具自动并入
        self.assertNotIn("mcp.github", ap["attached"])                  # 未连接的绝不挂载

    def test_agent_needs_contract(self):
        nodes = plans.normalize([{"node_key": "s1", "title": "t", "agent_needs":
                                  ["  读我的邮件  ", "", "x" * 200, "三条", "四条", "五条"]}], self.uid, "zh")
        needs = nodes[0]["agent_needs"]
        self.assertEqual(len(needs), 3)                                  # 最多 3 条
        self.assertEqual(needs[0], "读我的邮件")                          # 去首尾空白
        self.assertLessEqual(max(len(x) for x in needs), 80)             # 每条 ≤80 字符


# ==================== 执行期外发闸门（prd/08 §4.4） ====================
class EgressGate(unittest.TestCase):
    def test_block_reason_matrix(self):
        n = {"node_key": "n", "tools": ["kb.search", "mcp.github.list_issues"], "agents": {"attached": []}}
        self.assertEqual(runner.external_block_reason(n, SETTINGS), "")
        self.assertEqual(runner.external_block_reason(n, {**SETTINGS, "agents.enabled": False}), "GLOBAL_OFF")
        # 数据出境守卫：关掉「知识库随外部调用共享」后，会检索知识库的节点一律不发外部工具
        self.assertEqual(runner.external_block_reason(n, {**SETTINGS, "agents.kb_share": False}), "KB_SHARE_OFF")
        n2 = {"node_key": "n2", "tools": ["mcp.github.list_issues"], "agents": {"attached": []}}
        self.assertEqual(runner.external_block_reason(n2, {**SETTINGS, "agents.kb_share": False}), "")   # 不涉知识库 → 不受影响
        n3 = {"node_key": "n3", "tools": ["kb.search"], "agents": {}}
        self.assertEqual(runner.external_block_reason(n3, {**SETTINGS, "agents.enabled": False}), "")    # 不用外部 → 不提示
        n4 = {"node_key": "n4", "tools": [], "agents": {"attached": ["mcp.github.list_issues"]}}
        self.assertEqual(runner.external_block_reason(n4, {**SETTINGS, "agents.enabled": False}), "GLOBAL_OFF")


# ==================== M4：A2A 委派与本地 CLI（prd/08 §4.1 L2/L3） ====================
class A2AAndCLI(unittest.TestCase):
    """A2A 与 CLI 的探测/调用结果统一映射成 MCP 结果形状，复用同一条 build_tools / 审计 / 审批链路。"""

    @classmethod
    def setUpClass(cls):
        matching.seed_capabilities()
        os.environ.setdefault("no_proxy", "127.0.0.1,localhost")     # 本地测试服务不走系统代理
        got: list[dict] = []
        cls.got = got
        card = {"name": "Test A2A Agent", "description": "a2a test card",
                "skills": [{"id": "summarize_text", "name": "Summarize", "description": "summarize a text"},
                           {"id": "bad id!", "name": "Translate", "description": "translate a text"}]}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self._send(200, json.dumps(card).encode()) if "agent" in self.path else self._send(404, b"{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    payload = {}
                got.append({"path": self.path, "payload": payload})
                result = {"kind": "task", "status": {"state": "completed"},
                          "artifacts": [{"parts": [{"kind": "text", "text": "A2A 结果正文"}]}]}
                self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode())

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):                                # 静音（避免污染测试输出）
                pass

        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.uid = make_user()
        self.base = f"http://127.0.0.1:{self.port}"

    def _script(self) -> str:
        p = os.path.join(_TMP, "cli_agent.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("import sys\nprint('CLI 收到：' + sys.stdin.read().strip())\n")
        return p

    # ---------- A2A ----------
    def test_card_discovery_maps_skills_to_pseudo_tools(self):
        with unittest.mock.patch.object(agents, "_public_host", return_value=True):
            d = agents._a2a_discover(self.base, {}, 10)
        tools = d["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["summarize_text", "bad-id"])   # 非法字符规整为 -
        self.assertTrue(all(t["risk"] == "read" for t in tools))
        self.assertTrue(all(t["inputSchema"]["required"] == ["message"] for t in tools))
        self.assertEqual(d["result"]["_card"]["name"], "Test A2A Agent")

    def test_card_without_skills_falls_back_to_delegate(self):
        with unittest.mock.patch.object(agents, "_public_host", return_value=True), \
             unittest.mock.patch.object(agents, "_plain_get", return_value=(200, '{"name":"Bare"}', "application/json")):
            d = agents._a2a_discover(self.base, {}, 10)
        self.assertEqual([t["name"] for t in d["result"]["tools"]], ["delegate"])

    def test_delegate_sends_jsonrpc_message(self):
        with unittest.mock.patch.object(agents, "_public_host", return_value=True):
            r = agents._a2a_send(self.base + "/.well-known/agent.json", {}, {"arguments": {"message": "帮我总结这段文字"}}, 10)
        self.assertIn("A2A 结果正文", r["result"]["content"][0]["text"])
        sent = self.got[-1]
        self.assertEqual(sent["path"], "/")                                          # card URL → 站点根 RPC 端点
        self.assertEqual(sent["payload"]["method"], "message/send")
        self.assertEqual(sent["payload"]["jsonrpc"], "2.0")
        self.assertEqual(sent["payload"]["params"]["message"]["parts"][0]["text"], "帮我总结这段文字")

    def test_a2a_install_fields_and_ssrf(self):
        r = agents.install(self.uid, custom={"name": "远程助手", "kind": "a2a", "endpoint": self.base})
        row = agents.get(self.uid, r["key"])
        self.assertEqual((row["kind"], row["transport"], row["trust"]), ("a2a", "agent-card", "community"))
        with self.assertRaises(ApiError):
            agents.install(self.uid, custom={"name": "bad", "kind": "a2a", "endpoint": "ftp://x/y"})
        pr = agents.probe_runtime(agents.resolve_runtime(self.uid, r["key"], {}), timeout=3)
        self.assertEqual(pr.get("error"), "BLOCKED")                                 # 私网地址拒连（SSRF）

    # ---------- 本地 CLI ----------
    def test_cli_tools_shape(self):
        t = agents._cli_tools([sys.executable])
        self.assertEqual([x["name"] for x in t["result"]["tools"]], ["run"])         # 固定一个 run 工具
        self.assertEqual(t["result"]["tools"][0]["risk"], "console")                 # 危险级：走安全闸门
        self.assertEqual(t["result"]["tools"][0]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(agents._cli_tools(["definitely-not-a-cmd-xyz"]).get("error"), "NO_CMD")

    def test_cli_connect_and_call_end_to_end(self):
        r = agents.install(self.uid, custom={"name": "本地助手", "kind": "cli",
                                             "command": sys.executable, "args": [self._script()]})
        row = agents.get(self.uid, r["key"])
        self.assertEqual((row["kind"], row["transport"], row["trust"]), ("cli", "local", "local"))
        self.assertTrue(row["key"].startswith("cli.custom-"))
        c = agents.connect(self.uid, row["key"])
        self.assertTrue(c["ok"], c)
        self.assertEqual([t["name"] for t in c["agent"]["tools"]], ["run"])
        full = next(k for k in agents.tool_index(self.uid) if k.endswith(".run"))
        out = agents.call(self.uid, full, {"prompt": "写一首关于春天的诗"})
        self.assertEqual(out.get("error"), "WRITE_NOT_ALLOWED")                      # console 级：未开启写权限先拦
        agents.patch(self.uid, row["key"], {"allow_write": True})
        out = agents.call(self.uid, full, {"prompt": "写一首关于春天的诗"}, task_id="t-cli", node_key="n-cli")
        self.assertIn("CLI 收到：写一首关于春天的诗", out.get("content") or "")
        self.assertEqual(agents.risk(self.uid, full), "console")
        self.assertTrue(policy.approval_required(full, SETTINGS, self.uid))          # 本地级写操作：强制审批

    def test_cli_missing_prompt_and_bad_command(self):
        script = self._script()
        self.assertEqual(agents._cli_run([sys.executable, script], {}, _TMP, {}, 20).get("error"), "ARGS_INVALID")
        self.assertEqual(agents._cli_run(["definitely-not-a-cmd-xyz"], {}, _TMP,
                                         {"arguments": {"prompt": "x"}}, 20).get("error"), "NO_CMD")

    def test_cli_is_self_hosted_only(self):
        old = config.DEPLOY_MODE
        try:
            config.DEPLOY_MODE = "cloud"
            with self.assertRaises(ApiError) as cm:
                agents.install(self.uid, custom={"name": "本地脚本", "kind": "cli", "command": sys.executable})
            self.assertEqual(cm.exception.code, "AGENT_LOCAL_ONLY")
        finally:
            config.DEPLOY_MODE = old


# ==================== 真实 HTTP 层 ====================
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class AgentsHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="combine-agt-http-")
        cls.port = free_port()
        env = {**os.environ, "DATA_DIR": cls.dir, "DB_PATH": os.path.join(cls.dir, "combine.db"), "FAKE_LLM": "1", "FAKE_LATENCY_SCALE": "0.05",
               "PYTHONIOENCODING": "utf-8", "WEB_SEARCH_MODE": "fake"}
        cls.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", os.path.join(ROOT, "backend"),
                                     "--port", str(cls.port), "--log-level", "warning"], env=env, cwd=ROOT)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/readyz", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        cls.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def call(self, method, path, data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", "X-Combine": "1"})
        try:
            with self.op.open(req, timeout=240) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_http_full_loop_and_no_secret_echo(self):
        st, _ = self.call("POST", "/api/auth/register", {"email": "agt@test.io", "password": "password1"})
        self.assertEqual(st, 200)
        st, cat = self.call("GET", "/api/agents/catalog")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(cat["count"], 40)
        self.assertEqual(len(cat["domains"]), 12)
        self.assertTrue(cat["items"][0]["key"].startswith("mcp."))

        st, r = self.call("POST", "/api/agents", {"key": "mcp.demo"})
        self.assertEqual(st, 200)
        st, r = self.call("POST", "/api/agents/mcp.demo/connect", {})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["agent"]["tool_count"], 4)

        st, r = self.call("POST", "/api/agents/mcp.demo/call", {"tool": "calc", "args": {"expression": "6*7"}})
        self.assertEqual(st, 200)
        self.assertEqual(r["result"]["content"], "42")

        st, r = self.call("GET", "/api/agents/calls")
        self.assertEqual(st, 200)
        self.assertTrue(len(r["items"]) >= 1)

        st, r = self.call("POST", "/api/agents/health")
        self.assertEqual(st, 200)
        self.assertEqual(r["items"][0]["status"], "connected")

        # 自定义接入带密钥：列表接口绝不回明文，只回掩码
        st, r = self.call("POST", "/api/agents", {"custom": {"name": "sec", "transport": "stdio", "command": "python",
                                                             "args": [], "env": {"API_TOKEN": "sk-http-secret-9876"}}})
        self.assertEqual(st, 200)
        st, mine = self.call("GET", "/api/agents")
        blob = json.dumps(mine, ensure_ascii=False)
        self.assertNotIn("sk-http-secret", blob)
        self.assertIn("••••", blob)

        # SSRF：私网端点被拒（ok=false + 明确原因）
        st, r = self.call("POST", "/api/agents", {"custom": {"name": "evil", "transport": "http",
                                                             "endpoint": "http://127.0.0.1:9999/mcp"}})
        key = r["agent"]["key"]
        st, r = self.call("POST", f"/api/agents/{key}/connect", {})
        self.assertEqual(st, 200)
        self.assertFalse(r["ok"])
        self.assertIn("不允许访问", r["message"])
