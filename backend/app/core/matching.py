"""Agent 能力库与自适应匹配（F06）。

模型只负责给出结构化的「能力需求」（标签 / 工具 / 权限），选择哪个 Agent 由这里的
确定性评分完成（INV-16）：同样的输入一定得到同样的匹配结果，且评分构成对用户可见。
"""
from __future__ import annotations

from .. import db

# key, 中文名, 英文名, 说明, 标签, 工具, 权限, 成本档(1-3), 默认模型档
SEED = [
    ("extract", "资料解析 Agent", "Extraction Agent", "读取用户上传与知识库资料，抽取可引用的结构化信息",
     ["extract", "read_docs", "organize"], ["reference.read", "kb.search"], "read", 1, "fast"),
    ("retrieve", "资料检索 Agent", "Retrieval Agent", "围绕问题在知识库、附件与公开网络中检索并交叉比对，标记存疑内容",
     ["retrieve", "research", "evidence"], ["kb.search", "reference.read", "web.search", "web.news", "web.fetch"], "read", 1, "fast"),
    ("requirements", "需求分析 Agent", "Requirement Analyst", "把模糊目标拆成可检查的约束、偏好与验收标准",
     ["requirements", "constraints", "preference"], [], "read", 1, "fast"),
    ("plan", "方案编排 Agent", "Planner Agent", "安排步骤、日程与资源，输出可执行方案",
     ["plan", "schedule", "organize"], ["kb.search", "map.resolve"], "read", 2, "standard"),
    ("analyze", "分析 Agent", "Analyst Agent", "基于结构化输入做比较、推理与数据解读",
     ["analyze", "compare", "reason", "data"], ["calc.eval", "kb.search"], "read", 2, "standard"),
    ("research", "调研 Agent", "Research Agent", "围绕单个对象做深入调研并给出证据（多引擎检索 + 读全文）",
     ["research", "compare", "market", "evidence"], ["kb.search", "web.search", "web.news", "web.scholar", "web.fetch"], "read", 2, "standard"),
    ("write", "写作 Agent", "Writer Agent", "把结构化结果写成连贯、可直接使用的文字",
     ["write", "draft", "explain"], ["kb.search"], "read", 2, "standard"),
    ("summarize", "摘要 Agent", "Summarizer Agent", "压缩长材料，提炼要点", ["summarize", "condense"], [], "read", 1, "fast"),
    ("translate", "翻译 Agent", "Translator Agent", "翻译与本地化，保持术语一致", ["translate", "localize"], [], "read", 1, "fast"),
    ("code", "编程 Agent", "Coding Agent", "编写、解释与调试代码（不执行代码）",
     ["code", "debug", "technical"], ["calc.eval", "web.code", "web.search"], "read", 3, "strong"),
    ("verify", "校验 Agent", "Review Agent", "核验事实、引用与安全风险，给出待核实清单",
     ["verify", "review", "citation", "safety"], ["kb.search", "web.fetch"], "read", 2, "standard"),
    ("synthesize", "整合 Agent", "Synthesis Agent", "整合多个上游结果，形成结论与建议",
     ["synthesize", "integrate", "conclude"], [], "read", 2, "strong"),
    ("brainstorm", "创意 Agent", "Ideation Agent", "发散创意、给出多个备选方向", ["brainstorm", "creative", "ideas"], [], "read", 1, "standard"),
    ("teach", "辅导 Agent", "Tutor Agent", "讲解概念、出题与学习规划", ["teach", "explain", "learn"], ["kb.search"], "read", 2, "standard"),
    ("budget", "预算核算 Agent", "Budget Agent", "核对费用、预算与数量计算", ["budget", "finance", "calculate"], ["calc.eval", "kb.search"], "read", 1, "fast"),
    ("workspace", "文件与命令 Agent", "Workspace Agent", "在你自己的任务工作区里读写文件、执行命令（删除与控制台操作需你批准）",
     ["file", "organize", "cleanup", "automate"], ["file.list", "file.write", "file.delete", "shell.run"], "read", 3, "standard"),
]

# 工具注册表：权限（read/write 决定能力匹配）、风险类别（决定要不要先问你，见 core/policy.py）、说明
TOOL_REGISTRY = {
    "kb.search": {"perm": "read", "risk": "read", "desc": "检索当前用户的个人知识库"},
    "reference.read": {"perm": "read", "risk": "read", "desc": "读取本任务附带的资料"},
    "web.search": {"perm": "read", "risk": "read", "desc": "公开网络检索（多引擎聚合：搜狗/360/Bing 融合去重）"},
    "web.news": {"perm": "read", "risk": "read", "desc": "新闻与公众号文章检索（Google News / Bing News / 微信）"},
    "web.scholar": {"perm": "read", "risk": "read", "desc": "学术文献检索（OpenAlex / arXiv / Crossref）"},
    "web.code": {"perm": "read", "risk": "read", "desc": "代码与技术社区检索（GitHub / Stack Overflow / Hacker News）"},
    "web.fetch": {"perm": "read", "risk": "read", "desc": "打开一个公开网页并读取全文（HTML 转纯文本）"},
    "calc.eval": {"perm": "read", "risk": "read", "desc": "安全的四则运算计算器"},
    "map.resolve": {"perm": "read", "risk": "read", "desc": "地名解析为坐标（GCJ-02，用于旅行/路线地图）"},
    "file.list": {"perm": "read", "risk": "read", "desc": "列出任务工作区中的文件"},
    "file.write": {"perm": "write", "risk": "write", "desc": "在任务工作区新建或覆盖文件"},
    "file.delete": {"perm": "write", "risk": "delete", "desc": "删除任务工作区中的文件（执行前需你批准）"},
    "shell.run": {"perm": "write", "risk": "console", "desc": "在任务工作区执行一条控制台命令（执行前需你批准）"},
    # v3.0 M2 按需发现：执行期常驻的只读工具（agent.search 单节点限 2 次）
    "agent.search": {"perm": "read", "risk": "read", "desc": "按需在 Agent 目录中检索能提供某类外部能力的 Agent（含未连接）"},
    "agent.describe": {"perm": "read", "risk": "read", "desc": "查看某个外部 Agent 的精选工具集（只读预览）"},
}

DEFAULT_WEIGHTS = {"w_tag": 0.55, "w_tool": 0.25, "w_perm": 0.10, "w_cost": 0.10}
COMPOSE_THRESHOLD = 0.5            # 标签覆盖率低于此值时组合生成临时 Agent
PERM_ORDER = {"read": 0, "write": 1}


def external_tools(user_id: str) -> set[str]:
    """已连接外部 Agent 的可用工具全名（v3.0 M1，形如 mcp.github.search_repositories）。

    外部层任何异常都不得拖垮内置匹配，因此整体兜底为空集；延迟导入避免核心层反向依赖。
    """
    if not user_id:
        return set()
    try:
        from ..services import agents
        return set(agents.tool_index(user_id).keys())
    except Exception:  # noqa: BLE001
        return set()


def seed_capabilities() -> None:
    """写入内置能力。已存在的能力做「只增不减」的工具列表合并——
    版本升级新增的工具（如 v2.1 的 web.* 家族）会补进旧库，
    但绝不删除任何已有工具（避免覆盖历史数据的自定义）。"""
    for (key, name, en, desc, tags, tools, perm, cost, tier) in SEED:
        row = db.q1("SELECT key, tools, description, name_en FROM capabilities WHERE key=?", (key,))
        if not row:
            db.insert("capabilities", {"key": key, "name": name, "name_en": en, "description": desc,
                                       "tags": tags, "tools": tools, "permission": perm, "cost_tier": cost,
                                       "default_tier": tier,
                                       "system_prompt": f"你是{name}：{desc}。只完成本步骤，不越界。"})
            continue
        cur = db.j(row["tools"], [])
        merged = list(dict.fromkeys([*cur, *(t for t in tools if t not in cur)]))
        patch = {}
        if merged != cur:
            patch["tools"] = merged
        if row.get("description") and row["description"] != desc and row["name_en"] == en:
            patch["description"] = desc                      # 仅刷新内置能力的描述（同名同源才更新）
        if patch:
            db.update("capabilities", {"key": key}, patch)


def load_capabilities() -> list[dict]:
    rows = db.q("SELECT * FROM capabilities WHERE enabled=1 ORDER BY rowid")
    for r in rows:
        r["tags"], r["tools"] = db.j(r["tags"], []), db.j(r["tools"], [])
    return rows


def weights_for(user_id: str) -> dict:
    w = db.q1("SELECT * FROM match_weights WHERE user_id=?", (user_id,))
    if not w:
        w = {"user_id": user_id, **DEFAULT_WEIGHTS, "sample_count": 0, "version": 1}
        db.insert("match_weights", w)
    return w


def score_one(cap: dict, req: dict, w: dict) -> dict:
    tags = set(req.get("tags") or [])
    # 工具覆盖率只按内置工具口径计算：外部工具是「资源」而非角色能力（v3.0），
    # 若把它们算进覆盖率会无谓压低内置角色的得分、逼出临时 Agent。
    tools = {t for t in (req.get("tools") or []) if t in TOOL_REGISTRY}
    tag = len(tags & set(cap["tags"])) / len(tags) if tags else 0.5
    tool = len(tools & set(cap["tools"])) / len(tools) if tools else 1.0
    perm = 1.0 if PERM_ORDER[cap["permission"]] <= PERM_ORDER.get(req.get("permission", "read"), 0) else 0.0
    cost = (cap["cost_tier"] - 1) / 2
    total = w["w_tag"] * tag + w["w_tool"] * tool + w["w_perm"] * perm - w["w_cost"] * cost
    return {"key": cap["key"], "score": round(total, 3),
            "breakdown": {"tag": round(tag, 3), "tool": round(tool, 3), "perm": perm, "cost": round(cost, 3)}}


def match(req: dict, user_id: str, caps: list[dict] | None = None, forced: list[str] | None = None) -> dict:
    """返回 agent 描述：选中的能力、分数构成、理由、备选；必要时组合临时 Agent。"""
    caps = caps or load_capabilities()
    by_key = {c["key"]: c for c in caps}
    w = weights_for(user_id)
    scored = [score_one(c, req, w) for c in caps]
    scored = [s for s in scored if s["breakdown"]["perm"] > 0]          # 权限是硬约束（INV-10）
    scored.sort(key=lambda s: (-s["score"], s["key"]))                 # 确定性排序
    alts = [{"key": s["key"], "name": by_key[s["key"]]["name"], "name_en": by_key[s["key"]]["name_en"],
             "score": s["score"]} for s in scored[:4]]
    ext = external_tools(user_id)
    req_tools = [t for t in (req.get("tools") or []) if t in TOOL_REGISTRY or t in ext]

    if forced:                                                          # 用户手动指定（set_agent）
        chosen = [by_key[k] for k in forced if k in by_key]
        if chosen:
            s = score_one(chosen[0], req, w)
            return _agent("library" if len(chosen) == 1 else "composed", chosen, s, req_tools, alts, manual=True, ext=ext)

    best = scored[0] if scored else None
    if best and best["breakdown"]["tag"] >= COMPOSE_THRESHOLD:
        caps_sel = [by_key[best["key"]]]
        # v2.1：需求声明的工具若未被该能力覆盖，从能力库补上能提供缺口工具的能力（工具级组合），
        # 避免「规划需要的工具被白名单静默裁掉」（例如地图 / 外网检索工具）。缺口为空时保持单能力。
        # v3.0：外部工具（mcp.*）不算缺口 —— 它们不依附于任何内置能力，直接随节点白名单下传。
        have = set(caps_sel[0]["tools"])
        missing = [t for t in req_tools if t not in have and t in TOOL_REGISTRY]
        if missing:
            extra = sorted([c for c in caps if c["key"] != best["key"] and set(c["tools"]) & set(missing)],
                           key=lambda c: (-len(set(c["tools"]) & set(missing)), c["cost_tier"], c["key"]))
            if extra:
                return _agent("composed", caps_sel + extra[:2], best, req_tools, alts, ext=ext)
        return _agent("library", caps_sel, best, req_tools, alts, ext=ext)

    # 组合：贪心选能覆盖剩余标签最多的能力，最多 3 个，生成一次性临时实例（任务结束即失效）
    need = set(req.get("tags") or [])
    picked: list[dict] = []
    while need and len(picked) < 3:
        cand = max(caps, key=lambda c: (len(need & set(c["tags"])), -c["cost_tier"], c["key"] == "write"))
        gain = need & set(cand["tags"])
        if not gain:
            break
        picked.append(cand)
        need -= gain
    if not picked:
        picked = [by_key.get("write") or caps[0]]
    s = score_one(picked[0], req, w)
    covered = set().union(*[set(c["tags"]) for c in picked]) & set(req.get("tags") or [])
    s["breakdown"]["tag"] = round(len(covered) / max(1, len(req.get("tags") or [])), 3)
    s["score"] = round(w["w_tag"] * s["breakdown"]["tag"] + w["w_tool"] * s["breakdown"]["tool"]
                       + w["w_perm"] - w["w_cost"] * s["breakdown"]["cost"], 3)
    return _agent("composed", picked, s, req_tools, alts, ext=ext)


def _agent(source: str, caps: list[dict], s: dict, req_tools: list[str], alts: list[dict],
           manual=False, ext: set[str] | None = None) -> dict:
    allowed = sorted(set().union(*[set(c["tools"]) for c in caps]))
    tools = [t for t in req_tools if t in allowed]                    # 工具 = 需求 ∩ 能力白名单
    ext_tools = [t for t in req_tools if ext and t in ext]            # v3.0：外部工具直接随节点下传
    tools = [*tools, *[t for t in ext_tools if t not in tools]]
    if source == "composed":
        name = "临时 Agent · " + " + ".join(c["name"].replace(" Agent", "") for c in caps)
        en = "Temporary Agent · " + " + ".join(c["name_en"].replace(" Agent", "") for c in caps)
        why = f"能力库中没有单个条目覆盖全部需求，组合 {len(caps)} 项基础能力生成临时实例，任务结束即释放。"
        why_en = f"No single library entry covers the requirement; composed {len(caps)} base capabilities into a temporary instance."
    else:
        c = caps[0]
        name, en = c["name"], c["name_en"]
        b = s["breakdown"]
        why = f"标签覆盖 {int(b['tag'] * 100)}%，工具覆盖 {int(b['tool'] * 100)}%，权限只读，成本档 {c['cost_tier']}。"
        why_en = f"Tag coverage {int(b['tag'] * 100)}%, tool coverage {int(b['tool'] * 100)}%, read-only, cost tier {c['cost_tier']}."
    if manual:
        why, why_en = "由你手动指定。" + why, "Chosen by you. " + why_en
    return {"source": source, "capability_keys": [c["key"] for c in caps], "name": name, "name_en": en,
            "score": s["score"], "breakdown": s["breakdown"], "reason": why, "reason_en": why_en,
            "alternatives": alts, "tools": tools, "permission": "read",
            "default_tier": max((c["default_tier"] for c in caps), key=["fast", "standard", "strong"].index),
            "system_prompt": " ".join(c["system_prompt"] for c in caps)}


def learn_from_choice(user_id: str, original: dict, chosen: dict) -> None:
    """F21：用户手动更换 Agent 时，把权重朝「被选中项更占优的维度」微调（不训练模型）。"""
    w = weights_for(user_id)
    ob, cb = original.get("breakdown", {}), chosen.get("breakdown", {})
    step = 0.02
    for dim, wk in (("tag", "w_tag"), ("tool", "w_tool")):
        if cb.get(dim, 0) > ob.get(dim, 0):
            w[wk] += step
        elif cb.get(dim, 0) < ob.get(dim, 0):
            w[wk] = max(0.05, w[wk] - step)
    if cb.get("cost", 0) < ob.get("cost", 0):
        w["w_cost"] += step
    s = w["w_tag"] + w["w_tool"] + w["w_perm"] + w["w_cost"]
    vals = {k: round(w[k] / s, 4) for k in ("w_tag", "w_tool", "w_perm", "w_cost")}
    db.update("match_weights", {"user_id": user_id},
              {**vals, "sample_count": w["sample_count"] + 1, "version": w["version"] + 1})
