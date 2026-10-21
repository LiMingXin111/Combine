"""工具风险分级与「执行前安全防护」（可选，默认关闭）。

风险类别（在能力库的工具注册表里逐一声明）：
- read    只读：检索知识库、读取附件、四则运算、列出工作区文件 —— 永远直接执行；
- write   写入：在任务工作区里新建 / 覆盖文件 —— 永远直接执行；
- delete  删除：删除工作区文件 —— 开启安全防护后，执行前由你批准（不可逆）；
- console 控制台：执行一条命令 —— 开启安全防护后，执行前由你批准（影响面大）。

执行策略（设置 → 工具与权限）：
- approval.ask_dangerous = false（默认）：全部直接执行，流程不因审批停顿，一路跑出最终结果；
- approval.ask_dangerous = true       ：删除与控制台操作逐次停下、等你在任务页批准（写文件不打扰）。

两条不会因为设置而消失的约束：
- 云端版（DEPLOY_MODE=cloud）不开放本机文件与命令，file.* 与 shell.run 一律不可用；
- 危险操作无论是否免批，都会写入事件流、执行日志与模型调用账本（可追溯）。

模型只负责「想做什么」，能不能做、要不要先问人，全部由这里确定性判定。
"""
from __future__ import annotations

from .. import config
from ..util import clip, stable_hash
from . import matching

# 类别 → 能力开关
ENABLE_SETTING = {"write": "tools.file_write", "delete": "tools.file_delete", "console": "tools.shell_exec"}
NOTE = {"file.write": "create or overwrite a file in the workspace",
        "file.delete": "delete a file from the workspace (irreversible)",
        "shell.run": "run one console command in the workspace"}


def _is_ext(tool: str) -> bool:
    return isinstance(tool, str) and tool.startswith("mcp.")


def risk_of(tool: str, user_id: str = "") -> str:
    """工具的风险类别；未注册的工具按最保守的写入类处理。

    v3.0：外部 Agent 工具（mcp.*）的风险来自连接时缓存到 agents 表的逐工具标注；
    没有用户上下文时按最保守的写入类处理（预执行等安全判定不受影响）。
    """
    if _is_ext(tool):
        from ..services import agents
        return agents.risk(user_id, tool)
    return (matching.TOOL_REGISTRY.get(tool) or {}).get("risk", "write")


def enabled(tool: str, s: dict, user_id: str = "") -> bool:
    """该工具在当前设置与部署形态下是否可用（能力开关）。"""
    if _is_ext(tool):                                    # 外部工具：已连接 + 已启用 + 写开关 + 全局开关
        from ..services import agents
        return agents.tool_enabled(user_id, tool, s)
    r = risk_of(tool)
    if r == "read":
        return True
    if config.DEPLOY_MODE != "self_hosted":
        return False                          # 云端版不开放本机文件与命令
    return bool(s.get(ENABLE_SETTING.get(r, ""), False))


def allowed(names, s: dict, user_id: str = "") -> list[str]:
    """节点工具白名单 ∩ 当前可用工具。"""
    return [t for t in names if (t in matching.TOOL_REGISTRY or _is_ext(t)) and enabled(t, s, user_id)]


def approval_required(tool: str, s: dict, user_id: str = "") -> bool:
    """这次执行该工具前是否需要用户批准。

    默认全部直接执行；只有用户主动开启「危险操作先问我」时，删除与控制台才逐次停下。
    v3.0：外部 Agent 的写 / 删除类操作——社区级强制审批（不受全局开关豁免），
    其余信任级随「危险操作先问我」；只读外部工具永不打断。
    """
    if _is_ext(tool):
        from ..services import agents
        r = agents.risk(user_id, tool)
        if r not in ("write", "delete", "console"):
            return False
        if agents.trust_of(user_id, tool) in ("community", "local"):
            return True
        return bool(s.get("approval.ask_dangerous", False))
    if risk_of(tool) not in ("delete", "console"):
        return False
    return bool(s.get("approval.ask_dangerous", False))


def args_hash(args) -> str:
    return stable_hash({"args": args if isinstance(args, dict) else {"value": args}})


def summarize(tool: str, args: dict, user_id: str = "") -> str:
    """给审批记录用的单行描述（人也看得懂）。"""
    a = args if isinstance(args, dict) else {}
    if _is_ext(tool):
        from ..services import agents
        try:
            name = (agents.tool_index(user_id).get(tool) or {}).get("agent_name") or tool
        except Exception:  # noqa: BLE001
            name = tool
        return clip(f"调用外部 Agent「{name}」：{tool}（{clip(str(a), 100)}）", 160)
    if tool == "file.delete":
        return f"删除文件：{clip(str(a.get('path') or '?'), 120)}"
    if tool == "file.write":
        return f"写入文件：{clip(str(a.get('path') or '?'), 120)}（{len(str(a.get('content') or ''))} 字符）"
    if tool == "shell.run":
        return f"执行命令：{clip(str(a.get('command') or '?'), 160)}"
    if tool == "file.list":
        return "列出工作区文件"
    return clip(f"{tool}", 120)


def prompt_tools(s: dict, user_id: str = "") -> str:
    """规划提示词里的「允许的工具」一行：只列出当前真正可用的工具。

    v3.0：末尾附「已连接外部 Agent」摘要（一级暴露：只给 Agent 名 + 只读工具名，不含 schema；
    硬上限 5 个 Agent × 8 个工具名，见 services/agents.prompt_digest 与 prd/08 §5.4）。
    """
    parts = []
    if s.get("knowledge.auto_search", True):
        parts.append("kb.search (the user's personal knowledge base)")
    parts.append("reference.read (files attached to this task)")
    parts.append("calc.eval (arithmetic)")
    parts.append("map.resolve (resolve place names to GCJ-02 coordinates — use it for itinerary / route / map steps)")
    if config.WEB_SEARCH_MODE != "off":                  # v2.1：外网检索可用时告知规划模型
        parts.append("web.search / web.news / web.scholar / web.code (public web: multi-engine general search, "
                     "news, academic papers, developer channels — pick the right channel and use query variants)")
        parts.append("web.fetch (open one search result URL and read the full text before citing it)")
    parts.append("file.list (list files already in the workspace)")
    for t in ("file.write", "file.delete", "shell.run"):
        if enabled(t, s):
            gate = " — the user must approve EACH use, so only use it when the goal truly requires it" if approval_required(t, s) else ""
            parts.append(f"{t} ({NOTE[t]}{gate})")
    line = ", ".join(parts)
    try:                                                  # 外部 Agent 摘要（失败不影响规划）
        from ..services import agents
        digest = agents.prompt_digest(user_id, s)
    except Exception:  # noqa: BLE001
        digest = ""
    if digest:
        line += ("\nConnected external agents (live, external data/actions — add the exact tool id like "
                 "\"mcp.demo.now\" to a node's capability_requirements.tools ONLY when that step truly needs live "
                 f"external data or a real-world action; otherwise do not request them): {digest}")
    return line


class ToolApprovalRequired(Exception):
    """闸门触发：危险操作尚未获得用户批准。节点会挂起为「等待你确认」，批准后重新执行。

    注意：参数必须存进 `tool_args`，不能叫 `args` —— `BaseException.args` 是内置属性，
    赋值时 CPython 会把 dict 迭代成键名元组（`{"path": "x"}` → `("path",)`），
    结果是审批界面与「批准 → 重跑」的参数哈希全部对不上。
    """

    def __init__(self, tool: str, args: dict):
        super().__init__(f"{tool} 需要你批准后才能执行")
        self.tool = tool
        self.tool_args = args if isinstance(args, dict) else {}
        self.risk = risk_of(tool)
        self.hash = args_hash(self.tool_args)
