"""设置中心（F26）：默认值、取值校验与锁定项。

审批已退出主流程：任务自输入起自动规划、自动执行，不等待任何人工确认。
唯一的可选安全防护是 approval.ask_dangerous —— 删除与控制台等危险操作默认直接执行，
用户想保守时可在设置里打开「危险操作先问我」，逐次执行前停下等待批准（core/policy.py 负责判定）。
锁定项（INV-19）只剩三条真正不随设置变化的行为约束：回放只读、上传内容只当数据、危险操作全程留痕。
"""
from __future__ import annotations

from .. import config, db
from ..util import ApiError

# key: (默认值, 校验)  校验为 ("enum", [...]) / ("bool",) / ("int", lo, hi) / ("float", lo, hi) / ("str",) / ("list",)
SCHEMA: dict[str, tuple] = {
    "general.ui_language": ("en-US", ("enum", ["zh-CN", "en-US", "system"])),
    "general.output_language": ("follow_input", ("enum", ["follow_input", "zh", "en"])),
    "general.theme": ("dark", ("enum", ["dark", "light", "system"])),
    "general.density": ("comfortable", ("enum", ["comfortable", "compact"])),
    "general.start_page": ("home", ("enum", ["home", "last_task"])),
    "models.default_model": ("auto", ("str",)),
    "models.tier_fast": ("builtin:fast", ("str",)),
    "models.tier_standard": ("builtin:standard", ("str",)),
    "models.tier_strong": ("builtin:strong", ("str",)),
    "models.allow_builtin_fallback": (True, ("bool",)),
    "execution.default_mode": ("auto", ("enum", ["auto", "quick", "team"])),
    "execution.max_concurrency": (4, ("int", 1, 8)),
    "execution.max_retries": (1, ("int", 0, 3)),
    "execution.task_budget_cny": (config.DEFAULT_TASK_BUDGET_CNY, ("float", 0.05, 100)),
    "execution.speculative": (True, ("bool",)),
    "execution.speculative_max_cny": (0.10, ("float", 0, 2)),
    "knowledge.auto_search": (True, ("bool",)),
    "knowledge.show_citations": (True, ("bool",)),
    "knowledge.archive_mode": ("ask", ("enum", ["ask", "never"])),
    "tools.web_search": (True, ("bool",)),
    "tools.file_write": (True, ("bool",)),
    "tools.file_delete": (False, ("bool",)),
    "tools.shell_exec": (False, ("bool",)),
    # v3.0 外部 Agent：总开关（用户约束 N4 的第一道闸）；单个 Agent 的启停与写权限在「Agent 目录」页管理
    "agents.enabled": (True, ("bool",)),
    # v3.0 M2 决策引擎：三档调用模式（保守 / 平衡 / 激进）与知识库外发守卫（默认允许、出网留痕）
    "agents.mode": ("balanced", ("enum", ["conservative", "balanced", "aggressive"])),
    "agents.kb_share": (True, ("bool",)),
    # 可选安全防护：开启后删除与控制台操作逐次停下等用户批准；默认关闭，流程一路直跑
    "approval.ask_dangerous": (False, ("bool",)),
    "notifications.approval": (True, ("bool",)),
    "notifications.completed": (True, ("bool",)),
    "notifications.failed": (True, ("bool",)),
    "notifications.budget": (True, ("bool",)),
    "privacy.event_retention_days": (180, ("int", 7, 3650)),
    "privacy.learn_from_feedback": (True, ("bool",)),
    "advanced.show_raw_events": (False, ("bool",)),
    "canvas.show_minimap": ("auto", ("enum", ["auto", "on", "off"])),
    "canvas.highlight_critical": (False, ("bool",)),
    # —— 锁定项：只展示说明，不可修改（INV-19）——
    "safety.replay_readonly": (True, ("bool",)),
    "safety.uploads_are_data": (True, ("bool",)),
    "safety.danger_ops_logged": (True, ("bool",)),
}
LOCKED = {"safety.replay_readonly", "safety.uploads_are_data", "safety.danger_ops_logged",
          "notifications.approval", "notifications.budget"}
DEFAULTS = {k: v[0] for k, v in SCHEMA.items()}


def get(user_id: str) -> dict:
    row = db.q1("SELECT data FROM user_settings WHERE user_id=?", (user_id,))
    vals = dict(DEFAULTS)
    vals.update({k: v for k, v in db.j(row["data"] if row else "{}", {}).items() if k in SCHEMA and k not in LOCKED})
    return vals


def _validate(key: str, value):
    if key not in SCHEMA:
        raise ApiError(400, "SETTING_UNKNOWN", f"未知设置项 {key}")
    if key in LOCKED:
        raise ApiError(400, "SETTING_LOCKED", "该项受系统保护规则约束，不能修改", {"key": key})
    rule = SCHEMA[key][1]
    kind = rule[0]
    ok = True
    if kind == "enum":
        ok = value in rule[1]
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool) and rule[1] <= value <= rule[2]
    elif kind == "float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and rule[1] <= value <= rule[2]
        value = float(value) if ok else value
    elif kind == "str":
        ok = isinstance(value, str) and len(value) <= 200
    if not ok:
        raise ApiError(400, "SETTING_INVALID", f"设置项 {key} 的取值无效", {"key": key})
    return value


def patch(user_id: str, changes: dict) -> dict:
    clean = {k: _validate(k, v) for k, v in changes.items()}     # 任一非法则整体拒绝
    row = db.q1("SELECT data FROM user_settings WHERE user_id=?", (user_id,))
    data = db.j(row["data"] if row else "{}", {})
    data.update(clean)
    if row:
        db.update("user_settings", {"user_id": user_id}, {"data": data})
    else:
        db.insert("user_settings", {"user_id": user_id, "data": data})
    return get(user_id)


def reset(user_id: str, section: str) -> dict:
    row = db.q1("SELECT data FROM user_settings WHERE user_id=?", (user_id,))
    data = {k: v for k, v in db.j(row["data"] if row else "{}", {}).items() if not k.startswith(section + ".")}
    if row:
        db.update("user_settings", {"user_id": user_id}, {"data": data})
    return get(user_id)
