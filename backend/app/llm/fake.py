"""FakeLLM：离线、确定性的模型替身（未配置平台 Key 时自动启用，也用于全部测试）。

它不是规则模板引擎：只模拟真实模型在各类调用中的「输出形状」，让调度、安全防护、版本、
知识库与引用校验等全部机制在没有网络和费用的情况下可被完整演示和测试。
目标文本中的场景标记可触发异常路径：
  [fail]  第一个分析步骤首次调用失败      [429]  首次调用返回限流
  [q]     需求步骤提出待你回答的问题      [phone] 输出中含手机号（触发安全层遮蔽）
  [cycle] 首次规划返回循环依赖（触发校验与重规划）
  [del]   规划一个包含写/删/命令的清理步骤（开启「危险操作先问我」时会停在批准）
  [artifacts] 各步骤额外产出十类功能性节点数据（表格 / 指标 / 清单 / 代码 / 图片 / 时间线 / 日历 / 状态 / 会话 / 网页快照），
             用于 v3.0 画布节点扩容的离线演示与回归；同时写出一张 SVG 封面图验证「图片节点只认工作区文件」。

成果形态：分析节点按领域产出确定性图表数据（chart 字段，前端渲染成图）；旅行 / 路线类任务
的行程节点额外产出地图数据（map 字段，坐标取自内置地标库 `core/places.py`，前端渲染成攻略图）；
计划固定包含「整理交付文件」步骤，经文件与命令 Agent 在工作区写出「交付/执行摘要.md」供下载。
v2.1：检索类步骤会调用 web.search（多引擎 + 查询变体），结果以带链接的「网络来源」写入正文；
离线（fake）模式返回夹具并标注，联网模式为真实检索。
"""
from __future__ import annotations

import asyncio
import json
import re

from .. import config
from ..core import places
from ..util import clip

_seen_flags: set[str] = set()      # 一次性场景（如首次 429）只触发一次

# [artifacts] 场景的封面图（SVG 是文本，可以直接用 file.write 写入工作区 → 图片节点读取）
_ARTIFACT_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
                 '<rect width="640" height="360" fill="#141a26"/>'
                 '<rect x="24" y="24" width="592" height="312" rx="16" fill="none" stroke="#5b8cff" stroke-width="3"/>'
                 '<text x="320" y="170" text-anchor="middle" font-family="system-ui,sans-serif" font-size="32" fill="#e8ecf5">'
                 'Combine &#183; demo cover</text>'
                 '<text x="320" y="214" text-anchor="middle" font-family="system-ui,sans-serif" font-size="17" fill="#8ea0c0">'
                 'image node &#183; reads a real workspace file</text>'
                 '</svg>')


class FakeRateLimited(Exception):
    pass


class FakeFailure(Exception):
    pass


def _once(flag: str) -> bool:
    if flag in _seen_flags:
        return False
    _seen_flags.add(flag)
    return True


DOMAINS = [
    (r"旅|游|行程|景点|trip|travel|itinerary", [("行程路线", "Itinerary", ["plan", "schedule"], 3),
                                                  ("预算核对", "Budget check", ["budget", "calculate"], 2)]),
    (r"复习|学习|考试|课程|study|exam|learn", [("知识点梳理", "Key topics", ["teach", "organize"], 3),
                                              ("复习日程", "Study schedule", ["plan", "schedule"], 2)]),
    (r"选购|购买|买|入手|buy|purchase", [("候选对比", "Candidate comparison", ["compare", "analyze"], 3),
                                         ("预算与取舍", "Budget and trade-offs", ["budget", "compare"], 2)]),
    (r"报告|调研|分析|方案|report|analysis|proposal", [("现状与问题", "Current state and issues", ["analyze", "research"], 3),
                                                      ("对策建议", "Recommendations", ["analyze", "write"], 3)]),
    (r"代码|程序|bug|code|script", [("问题定位", "Problem analysis", ["code", "debug"], 3),
                                    ("实现说明", "Implementation notes", ["code", "explain"], 3)]),
]
GENERIC = [("核心分析", "Core analysis", ["analyze", "reason"], 3), ("方案草拟", "Draft plan", ["plan", "write"], 3)]


def extract_items(goal: str) -> list[str]:
    """从目标中找出可并列处理的对象，例如「对比 A、B 和 C」。"""
    m = re.search(r"(?:调研并对比|调研并比较|调研和对比|对比|比较|调研|分析|评估|compare|research|evaluate)\s*[:：]?\s*(.+)", goal, re.I)
    if not m:
        return []
    seg = re.split(r"[。；;!?！？\n]|的(?:优缺点|差异|区别)|(?:\s+for\s+)", m.group(1))[0]
    parts = re.split(r"\s*(?:、|,|，|/|和|与|及|以及|\band\b|\bvs\.?\b)\s*", seg)
    items = [p.strip(" 　\"'“”") for p in parts if 0 < len(p.strip()) <= 20]
    if items:   # 去掉末项的量词尾巴，如「石墨四款笔记工具」→「石墨」
        items[-1] = re.sub(r"\s*[一二两三四五六七八九十\d]+\s*(?:款|个|家|种|所|项|座).*$", "", items[-1]) or items[-1]
    return items[:8] if len(items) >= 2 else []


def _latency(complexity: int, tier: str) -> float:
    base = {1: 2.0, 2: 3.0, 3: 4.5, 4: 6.0, 5: 8.0}.get(complexity, 4.0)
    return base * {"fast": 0.6, "standard": 1.0, "strong": 1.4}.get(tier, 1.0) * config.FAKE_LATENCY_SCALE


def _domain(goal: str) -> str | None:
    """目标命中的领域（与 DOMAINS 同序：travel / study / buy / report / code）。"""
    for i, (pat, _) in enumerate(DOMAINS):
        if re.search(pat, goal, re.I):
            return ["travel", "study", "buy", "report", "code"][i]
    return None


def _chart_for(meta: dict, node: dict) -> dict | None:
    """确定性图表数据（成果中的「图」）：预算类分析节点 → 饼图；对比任务的综合节点 → 柱状图。"""
    en = meta.get("lang") == "en"
    L = (lambda zh, e: e if en else zh)
    key, g = node["node_key"], meta.get("goal", "")
    if key == "synth":
        items = extract_items(g)
        if len(items) >= 2:
            vals = [86.0, 78.0, 91.0, 83.0, 88.0, 81.0, 84.0, 79.0]
            return {"type": "bar", "title": L("候选综合评分", "Overall scores"),
                    "labels": [clip(i, 10) for i in items],
                    "series": [{"name": L("评分", "Score"), "values": vals[:len(items)]}]}
        return None
    if key == "part2":
        dom = _domain(g)
        if dom in ("travel", "buy"):
            return {"type": "pie", "title": L("预算分配", "Budget breakdown"),
                    "labels": [L("交通", "Transport"), L("住宿", "Lodging"), L("餐饮", "Meals"), L("门票", "Tickets"), L("机动", "Contingency")],
                    "series": [{"name": L("金额（元）", "Amount (CNY)"), "values": [1200.0, 2600.0, 1500.0, 700.0, 400.0]}]}
        if dom == "study":
            return {"type": "bar", "title": L("各科时间分配（分钟）", "Study time (min)"),
                    "labels": [L("数学", "Math"), L("英语", "English"), L("专业课", "Major"), L("错题复盘", "Review"), L("机动", "Buffer")],
                    "series": [{"name": L("时长", "Minutes"), "values": [90.0, 60.0, 120.0, 45.0, 30.0]}]}
        if dom == "report":
            return {"type": "bar", "title": L("建议优先级（1–5）", "Priority (1–5)"),
                    "labels": [L("快赢项", "Quick wins"), L("中期项", "Mid-term"), L("长期项", "Long-term"), L("风险项", "Risks")],
                    "series": [{"name": L("优先级", "Priority"), "values": [4.0, 3.0, 2.0, 3.0]}]}
    return None


def _map_for(meta: dict, node: dict) -> dict | None:
    """确定性地图数据（成果中的「地图」，v1.9）：旅行 / 路线类任务的攻略图。

    坐标取自内置地标库（GCJ-02，`core/places.py`），离线演示不产生幻觉坐标；
    数组顺序即游览顺序，同一天内按顺序连线。FakeLLM 的旅行夹具为杭州演示场景。
    """
    en = meta.get("lang") == "en"
    L = (lambda zh, e: e if en else zh)
    g = meta.get("goal", "")
    if node["node_key"] != "part1" or _domain(g) != "travel" or not re.search(r"杭州|西湖", g):
        return None
    plan = [("断桥残雪", 1, L("漫步白堤起点", "Start of the Bai Causeway walk")),
            ("平湖秋月", 1, L("沿湖步行约 1.1 km", "About 1.1 km along the lake")),
            ("苏堤春晓", 1, L("环湖骑行或换乘接驳", "Cycle or shuttle around the lake")),
            ("花港观鱼", 1, L("午后歇脚", "Mid-afternoon break")),
            ("雷峰塔", 1, L("傍晚上塔看夕照", "Sunset from the pagoda")),
            ("灵隐寺", 2, L("上午礼佛，建议 8:30 前到", "Morning visit, arrive before 8:30")),
            ("龙井村", 2, L("午后茶山漫步", "Tea-hill walk in the afternoon")),
            ("河坊街", 2, L("老街晚餐与夜间闲逛", "Dinner and evening stroll"))]
    markers = []
    for name, day, note in plan:
        hit = places.resolve(name)
        if hit:
            markers.append({"name": name, "lat": hit[0], "lng": hit[1], "day": day, "note": note})
    if len(markers) < 2:
        return None
    return {"title": L("杭州两日游路线图", "Hangzhou 2-day route map"), "city": L("杭州", "Hangzhou"), "markers": markers}


def _artifacts_for(meta: dict, node: dict) -> dict:
    """[artifacts] 场景：十类功能性节点的确定性数据（v3.0 M3/M4 的离线演示与回归）。

    分配：gather→web；item1/part1→table+code；item2/part2→metrics+calendar；
    synth→list+timeline；review→status+chat；deliver→image（配合第一轮写入的 交付/封面.svg）。
    """
    en = meta.get("lang") == "en"
    L = (lambda zh, e: e if en else zh)
    k = node["node_key"]
    table = {"title": L("近 7 日关键数据", "Key figures — last 7 days"),
             "columns": [{"key": "date", "label": L("日期", "Date"), "align": "left"},
                         {"key": "orders", "label": L("订单", "Orders"), "align": "right"},
                         {"key": "amount", "label": L("金额（元）", "Amount (CNY)"), "align": "right"},
                         {"key": "rate", "label": L("转化率", "Conversion"), "align": "right"}],
             "rows": [["2026-10-01", 128, 12850.5, "3.2%"], ["2026-10-02", 96, 9640.0, "2.8%"],
                      ["2026-10-03", 142, 15230.0, "3.6%"], ["2026-10-04", 88, 8210.5, "2.4%"],
                      ["2026-10-05", 156, 17480.0, "3.9%"], ["2026-10-06", 134, 14020.0, "3.1%"],
                      ["2026-10-07", 121, 12980.0, "2.9%"]],
             "total_rows": 132, "note": L("完整 132 行已导出为 CSV（见文件卡）", "Full 132 rows exported as CSV (see the file card)")}
    code = {"title": L("登录跳转修复（diff）", "Login redirect fix (diff)"), "language": "diff", "mode": "diff",
            "code": "--- a/src/login.ts\n+++ b/src/login.ts\n@@ -10,3 +10,3 @@\n-  return redirect('/old-dashboard')\n+  return redirect('/home')\n   // keep the query string",
            "path": "src/login.ts", "note": L("来自代码评审示例", "From a code-review sample")}
    if k == "gather":
        return {"web": {"title": L("Python 官方介绍页", "Python.org — about"),
                        "url": "https://www.python.org/about/",
                        "excerpt": L("Python 是一门广泛使用的通用编程语言，官方页面介绍其历史、应用领域与社区生态。",
                                     "Python is a widely used general-purpose programming language; the official page covers its history, uses and community."),
                        "text": L("官方页面要点：解释器与标准库、跨平台支持、社区治理（PSF）与入门资源。",
                                  "Highlights: interpreter and standard library, cross-platform support, PSF governance and getting-started resources.")}}
    if k in ("item1", "part1", "items"):
        return {"table": table, "code": code}
    if k in ("item2", "part2"):
        return {"metrics": {"title": L("本月收入概览", "Revenue overview — this month"),
                            "items": [{"label": L("总收入", "Total revenue"), "value": "¥896.5万", "delta": "+39.8%", "dir": "up", "status": "good"},
                                      {"label": L("退款率", "Refund rate"), "value": "1.2%", "delta": "-0.3pt", "dir": "down", "status": "good"},
                                      {"label": L("客单价", "Avg. order"), "value": "¥118", "delta": "+2.1%", "dir": "up", "status": "good"},
                                      {"label": L("异常订单", "Flagged orders"), "value": "27", "delta": "+9", "dir": "up", "status": "warn"}],
                            "note": L("口径：折扣后金额，剔除通道费；来源：账单分析示例", "Basis: post-discount, channel fees excluded; from a billing-analysis sample")},
                "calendar": {"title": L("本周排期", "This week's schedule"), "week_start": "2026-10-05",
                             "events": [{"day": 1, "start": "10:00", "end": "11:00", "title": L("周会", "Weekly sync"), "note": ""},
                                        {"day": 2, "start": "14:00", "end": "15:30", "title": L("方案评审", "Design review"), "note": L("带上原型", "Bring the prototype")},
                                        {"day": 4, "start": "09:30", "end": "10:00", "title": L("数据核对", "Data check"), "note": ""},
                                        {"day": 5, "start": "16:00", "end": "17:00", "title": L("复盘", "Retro"), "note": ""}]}}
    if k == "synth":
        return {"list": {"title": L("上线前检查", "Pre-launch checklist"), "mode": "checklist",
                         "items": [{"text": L("回归全绿", "All regressions green"), "done": True, "meta": "10 月 6 日"},
                                   {"text": L("灰度发布", "Canary release"), "done": False, "meta": L("负责人：A", "Owner: A")},
                                   {"text": L("监控告警核对", "Verify alerts"), "done": False, "meta": ""}]},
                "timeline": {"title": L("项目里程碑", "Milestones"),
                             "items": [{"time": "W1", "text": L("需求冻结", "Scope frozen"), "note": ""},
                                       {"time": "W2–W3", "text": L("开发与联调", "Build & integrate"), "note": ""},
                                       {"time": "W4", "text": L("灰度与回归", "Canary & regression"), "note": ""},
                                       {"time": "W5", "text": L("全量上线", "Full rollout"), "note": ""}]}}
    if k == "review":
        return {"status": {"title": L("核验结果", "Verification results"),
                           "items": [{"name": L("引用可追溯", "Citations traceable"), "state": "ok", "detail": L("全部来源可回查", "All sources resolvable")},
                                     {"name": L("数据一致性", "Data consistency"), "state": "warn", "detail": L("1 处口径待确认", "1 basis needs confirmation")},
                                     {"name": L("易变信息", "Volatile facts"), "state": "warn", "detail": L("价格类信息建议执行前复查", "Re-check prices before acting")}]},
                "chat": {"title": L("沟通记录摘录", "Conversation excerpt"),
                         "messages": [{"who": L("你", "You"), "role": "user", "text": L("这版方案什么时候能评审？", "When can we review this draft?"), "time": "09:12"},
                                      {"who": L("协作方", "Collaborator"), "role": "other", "text": L("周四下午可以，带上数据附录。", "Thursday afternoon works — bring the data appendix."), "time": "09:20"},
                                      {"who": L("你", "You"), "role": "user", "text": L("好，我周三先发预览版。", "OK, I'll send a preview on Wednesday."), "time": "09:24"}]}}
    if k == "deliver":
        return {"image": {"title": L("封面图", "Cover image"), "path": "交付/封面.svg",
                          "caption": L("任务演示封面（工作区真实文件）", "Demo cover stored as a real workspace file")}}
    return {}


async def respond(kind: str, meta: dict, messages: list[dict], tools: list[dict] | None, model_name: str) -> dict:
    goal = meta.get("goal", "")
    if "[429]" in goal and _once("429:" + meta.get("task_id", "")):
        raise FakeRateLimited()
    fn = {"triage": _triage, "plan": _plan, "node": _node, "quick": _quick}[kind]
    out = fn(meta, messages, tools if "no-tools" not in model_name else None)
    await asyncio.sleep(out.pop("_latency", 0.05 * config.FAKE_LATENCY_SCALE))
    return out


def _triage(meta, messages, tools):
    g = meta["goal"]
    team = len(g) >= 24 or bool(re.search(
        r"规划|计划|报告|分析|对比|比较|方案|调研|整理|写一份|安排|plan|report|compare|analy|research|itinerary|proposal", g, re.I))
    return {"content": json.dumps({"lane": "team" if team else "quick",
                                   "reason": "需要多步骤协作" if team else "单轮即可回答"}, ensure_ascii=False)}


def _plan(meta, messages, tools):
    g, en = meta["goal"], meta.get("lang") == "en"
    L = (lambda zh, e: e if en else zh)
    nodes = [
        {"node_key": "req", "title": L("梳理目标与约束", "Clarify goal and constraints"),
         "description": L("提取目标中的硬约束、偏好与验收标准", "Extract hard constraints, preferences and acceptance criteria"),
         "acceptance": [L("约束可逐条检查", "Constraints are checkable")], "depends_on": [], "input_refs": [],
         "capability_requirements": {"tags": ["requirements", "constraints"], "tools": [], "permission": "read"},
         "est_complexity": 1, "model_tier": "fast", "produces_sections": [], "requires_approval": False},
        {"node_key": "gather", "title": L("检索相关资料", "Gather references"),
         "description": L("在你的知识库、附件与公开网络中检索与目标相关的资料", "Search your knowledge base, attachments and the public web"),
         "acceptance": [L("每条资料保留出处", "Every item keeps its source")], "depends_on": [], "input_refs": [],
         "capability_requirements": {"tags": ["retrieve", "research"], "tools": ["kb.search", "web.search", "reference.read"], "permission": "read"},
         "est_complexity": 2, "model_tier": "fast", "produces_sections": [], "requires_approval": False},
    ]
    items = extract_items(g)
    aspect_keys = []
    if len(items) == 2:
        for i, it in enumerate(items):
            k = f"item{i + 1}"
            aspect_keys.append(k)
            nodes.append(_aspect(k, L(f"分析：{it}", f"Analyze: {it}"), it, ["research", "compare"], 3,
                                 ["kb.search", "web.search", "web.fetch"]))
    elif len(items) >= 3:            # 故意合成一个大步骤，由计划优化器给出「扇出」建议（R30.2-3）
        aspect_keys.append("items")
        nodes.append(_aspect("items", L("逐个分析：" + "、".join(items), "Analyze each: " + ", ".join(items)),
                             L("逐个分析：" + "、".join(items), "Each item: " + ", ".join(items)),
                             ["research", "compare"], 5, ["kb.search", "web.search", "web.fetch"]))
    else:
        aspects = GENERIC
        for pat, asp in DOMAINS:
            if re.search(pat, g, re.I):
                aspects = asp
                break
        for i, (zh, e, tags, cx) in enumerate(aspects):
            k = f"part{i + 1}"
            aspect_keys.append(k)
            tls = ["map.resolve", "kb.search"] if (_domain(g) == "travel" and "plan" in tags) \
                else (["calc.eval", "kb.search"] if "budget" in tags else ["kb.search"])   # 旅行行程节点带地图工具（v1.9）
            # v3.0 M2 夹具：行程节点声明外部能力需求——天气是「此刻为真的事实」（判据 C1），
            # 用于离线演示里覆盖「漏斗二匹配 → 未连接 → 非阻塞连接引导」的完整决策链路
            needs = [L("查询目的地当地当前的天气与地图路线", "Check the local weather and map route for the destination")] \
                if (_domain(g) == "travel" and "plan" in tags) else None
            nodes.append(_aspect(k, L(zh, e), L(zh, e), tags, cx, tls, needs))
    for n in nodes[2:]:
        n["depends_on"] = ["req", "gather"]
        n["input_refs"] = ["req.output", "gather.output"]
    nodes.append({"node_key": "synth", "title": L("形成结论与建议", "Conclusions and recommendations"),
                  "description": L("整合各部分结果，给出明确结论", "Integrate all parts; state clear conclusions"),
                  "acceptance": [L("结论与上游一致", "Consistent with upstream results")],
                  "depends_on": aspect_keys, "input_refs": [k + ".output" for k in aspect_keys],
                  "capability_requirements": {"tags": ["synthesize", "conclude"], "tools": [], "permission": "read"},
                  "est_complexity": 3, "model_tier": "strong",
                  "produces_sections": [L("结论与建议", "Conclusions")],
                  "requires_approval": False})            # 节点审批已退出流程：保留字段仅为数据模型兼容
    if "[del]" in g:                       # 安全防护场景：清理工作区草稿（开启「危险操作先问我」时删除与控制台先等批准）
        nodes.append({"node_key": "cleanup", "title": L("清理工作区草稿文件", "Clean up workspace drafts"),
                      "description": L("把不再需要的临时草稿写入并删除，保持工作区整洁",
                                       "Write then delete a temporary draft to keep the workspace tidy"),
                      "acceptance": [L("临时草稿已被移除", "The temporary draft is removed")],
                      "depends_on": ["req", "gather"], "input_refs": ["req.output", "gather.output"],
                      "capability_requirements": {"tags": ["organize", "cleanup"],
                                                  "tools": ["file.write", "file.delete", "shell.run"],
                                                  "permission": "read"},
                      "est_complexity": 2, "model_tier": "standard",
                      "produces_sections": [], "requires_approval": False})
    nodes.append({"node_key": "deliver", "title": L("整理交付文件", "Package deliverable files"),
                  "description": L("把本次成果整理成可下载的文件，放在任务工作区",
                                   "Package the results into downloadable files in the task workspace"),
                  "acceptance": [L("交付文件出现在任务工作区", "Deliverable files appear in the workspace")],
                  "depends_on": ["synth"], "input_refs": ["synth.output"],
                  "capability_requirements": {"tags": ["file", "organize"], "tools": ["file.write"], "permission": "read"},
                  "est_complexity": 1, "model_tier": "fast",
                  "produces_sections": [], "requires_approval": False})
    nodes.append({"node_key": "review", "title": L("核验引用与风险", "Verify citations and risks"),
                  "description": L("核对引用可追溯性，列出依据不足与需核实的内容", "Check citations and list unsupported claims"),
                  "acceptance": [L("每条结论有出处或被标注待核实", "Every claim is cited or flagged")],
                  "depends_on": ["synth", "gather"], "input_refs": ["synth.output"],   # gather→review 为可推出的冗余依赖
                  "capability_requirements": {"tags": ["verify", "citation"], "tools": ["kb.search"], "permission": "read"},
                  "est_complexity": 2, "model_tier": "standard",
                  "produces_sections": [L("待核实事项", "Items to verify")], "requires_approval": False})
    if "[cycle]" in g and _once("cycle:" + meta.get("task_id", "")):
        nodes[0]["depends_on"] = ["review"]
    title = clip(re.sub(r"\[[a-z0-9]+\]", "", g).strip(), 24)
    return {"content": json.dumps({"title": title, "nodes": nodes}, ensure_ascii=False), "_latency": 1.2 * config.FAKE_LATENCY_SCALE}


def _aspect(key, title, focus, tags, cx, tools, needs=None):
    n = {"node_key": key, "title": title, "description": focus, "acceptance": [],
         "depends_on": [], "input_refs": [], "produces_sections": [title],
         "capability_requirements": {"tags": tags, "tools": tools, "permission": "read"},
         "est_complexity": cx, "model_tier": "standard", "requires_approval": False}
    if needs:                                  # v3.0 M2：节点级外部能力需求（「做什么」而非「用谁」）
        n["agent_needs"] = needs
    return n


def _tool_args(name: str, meta: dict, node: dict) -> dict:
    """每个工具在夹具里的固定参数（安全闸门测试要求参数稳定，便于「批准 → 重跑」命中同一条批准记录）。"""
    if name == "kb_search":
        return {"query": f"{meta['goal']} {node['title']}"}
    if name == "web_search":
        # 多查询变体：中文主题补一个英文变体（论文 / 开源内容的覆盖）——演示「变体并行 + 融合」
        q = f"{meta['goal']} {node['title']}"
        return {"query": q, "queries": [f"{node['title']} overview"]}
    if name == "web_fetch":
        # 固定抓取一个稳定公开页（离线 fake 模式不出网、返回提示；联网模式读取全文）
        return {"url": "https://www.python.org/about/", "max_chars": 1500}
    if name == "web_news":
        return {"query": f"{node['title']}", "freshness": "month"}
    if name == "web_scholar":
        return {"query": f"{node['title']}"}
    if name == "web_code":
        return {"query": f"{node['title']}", "kind": "all"}
    if name == "calc_eval":
        return {"expression": "1200+260+130"}
    if name == "map_resolve":
        return {"places": ["断桥残雪", "平湖秋月", "苏堤春晓", "花港观鱼", "雷峰塔", "灵隐寺", "龙井村", "河坊街"]}
    if name == "file_write":
        if node["node_key"] == "deliver":            # 交付文件：任务成果的文件形态（成果面板的「文件」卡）
            title = clip(re.sub(r"\[[a-z0-9]+\]", "", meta.get("goal", "")).strip(), 24)
            body = (f"# {title}\n\n任务目标：{meta['goal']}\n\n"
                    "本文件由「整理交付文件」步骤生成。完整成果可在「查看成果」中阅读，"
                    "或从成果页导出 Markdown 全文。\n")
            return {"path": "交付/执行摘要.md", "content": body}
        return {"path": "drafts/note.md", "content": "# 临时草稿\n\n写完即删，用于演示删除操作的安全闸门。"}
    if name == "file_delete":
        return {"path": "drafts/note.md"}
    if name == "shell_run":
        return {"command": "echo combine-workspace-ready"}
    return {}


def _node(meta, messages, tools):
    node, en = meta["node"], meta.get("lang") == "en"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if tools and not tool_msgs:                                   # 第一轮：并行发起工具调用（与调度器同轮 3 个的上限一致）
        # 按需发现（agent.search / agent.describe）由真实模型按需使用；夹具不主动调用，保持行为可预测
        use_tools = [t for t in tools if not str(t["function"]["name"]).startswith("agent_")]
        calls = []
        for i, t in enumerate(use_tools[:3]):
            name = t["function"]["name"]                     # 模型侧函数名为 kb_search 形式
            calls.append({"id": f"call_{i}", "name": name, "args": _tool_args(name, meta, node)})
        if "[artifacts]" in meta["goal"] and node["node_key"] == "deliver" and len(calls) < 3 \
                and any(c["name"] == "file_write" for c in calls):
            calls.append({"id": f"call_{len(calls)}", "name": "file_write",       # 封面图：图片节点只认工作区真实文件
                          "args": {"path": "交付/封面.svg", "content": _ARTIFACT_SVG}})
        # v3.0 守卫：若本步骤的可调用工具只剩按需发现类（agent.search / agent.describe 是 v3.0 给每个节点
        # 附加的兜底工具），夹具不主动调用它们，直接进入最终回答——否则会返回「无工具调用的空正文」并被判 EMPTY_OUTPUT。
        if calls:
            return {"content": "", "tool_calls": calls, "_latency": 0.4 * config.FAKE_LATENCY_SCALE}
    if "[fail]" in meta["goal"] and node["node_key"] in ("part1", "item1", "items") and meta.get("attempt", 1) == 1 \
            and _once("fail:" + meta.get("task_id", "") + node["node_key"] + str(meta.get("version"))):
        raise FakeFailure("模拟的模型服务错误（[fail] 场景）")
    hits, web_src = [], []
    for m in tool_msgs:
        try:
            data = json.loads(m["content"])
        except (ValueError, TypeError):
            continue
        for it in data.get("items", []) if isinstance(data, dict) else []:
            if it.get("chunk_id"):
                hits.append(it)
            elif it.get("url") and it.get("title"):              # v2.1：web.* 检索结果（带 URL 的公开来源）
                web_src.append(it)
    L = (lambda zh, e: e if en else zh)
    lines, claims = [], []
    for up in meta.get("upstream", [])[:4]:
        s = clip(up.get("summary", ""), 80)
        lines.append(L(f"- 基于「{up['title']}」：{s}", f"- From \"{up['title']}\": {s}"))
    for h in hits[:3]:
        t = clip(h["text"].replace("\n", " "), 70)
        lines.append(f"- {t} [[c:{h['chunk_id']}]]")
        claims.append({"text": t, "citations": [h["chunk_id"]]})
    for w in web_src[:3]:                                        # 网络来源：带 Markdown 链接（可点击、可追溯）
        eng = "/".join(w.get("engines") or ["web"])
        lines.append(L(f"- 网络来源「{clip(w['title'], 42)}」：{clip((w.get('snippet') or '').replace(chr(10), ' '), 60)}"
                       f" [原文链接]({w['url']})（{eng}）",
                       f"- Web source \"{clip(w['title'], 42)}\": {clip((w.get('snippet') or '').replace(chr(10), ' '), 60)}"
                       f" [link]({w['url']}) ({eng})"))
    if node["node_key"] == "review":
        flagged = [c["text"] for up in meta.get("upstream", []) for c in up.get("claims", []) if not c.get("citations")]
        flagged = flagged or [L("未发现依据不足的结论", "No unsupported claims found")]
        lines += [L(f"- 待核实：{f}", f"- To verify: {f}") for f in flagged[:5]]
    else:
        extra = L("执行前请再次确认时间、价格等易变信息。", "Re-check volatile details such as times and prices before acting.")
        lines.append(f"- {extra}")
        claims.append({"text": extra, "citations": []})               # 无出处 → 关注项
    for ins in meta.get("instructions", [])[-3:]:
        lines.append(L(f"- 已按你的要求调整：{ins}", f"- Adjusted per your request: {ins}"))
    if "[phone]" in meta["goal"]:
        lines.append(L("- 联系人电话：13812345678", "- Contact: 13812345678"))
    head = L(f"围绕「{clip(meta['goal'], 30)}」，{node['description'] or node['title']}。",
             f"For \"{clip(meta['goal'], 40)}\": {node['description'] or node['title']}.")
    questions = []
    if "[q]" in meta["goal"] and node["node_key"] == "req" and not meta.get("instructions"):
        questions.append(L("请确认预算上限是多少？", "What is the budget cap?"))
    body = head + "\n\n" + "\n".join(lines)
    out = {"summary": clip(head, 90), "content": body, "claims": claims, "questions": questions,
           "confidence": 0.62 if not hits else 0.84}
    chart = _chart_for(meta, node)
    if chart:
        out["chart"] = chart
    mp = _map_for(meta, node)
    if mp:
        out["map"] = mp
    if "[artifacts]" in meta["goal"]:                        # v3.0：十类功能性节点数据（[artifacts] 场景）
        out.update(_artifacts_for(meta, node))
    return {"content": json.dumps(out, ensure_ascii=False),
            "_latency": _latency(node.get("est_complexity", 3), node.get("model_tier", "standard"))}


def _quick(meta, messages, tools):
    en = meta.get("lang") == "en"
    g = meta["goal"]
    txt = (f"(Offline demo model) About \"{g}\": here is a short, direct answer. "
           "Configure a platform or personal model in Settings to get real answers."
           if en else f"（离线演示模型）关于「{g}」：这是一次快速回答，没有启动任务图。"
                      "在「设置 → 模型服务」配置平台或个人模型后即可获得真实回答。")
    return {"content": txt, "_latency": 0.8 * config.FAKE_LATENCY_SCALE}
