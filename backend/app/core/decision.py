"""调用决策引擎（v3.0 M2，prd/08 §5）：「什么时候值得调外部 Agent」的确定性规则。

设计原则与能力匹配（INV-16）同源：不让模型心证。分流（triage）后由这里的规则引擎对任务做一次
判定，产出的命中判据（C1–C5）与建议域写进事件流（用户可见、可解释），并把一段决策提示注入规划
提示词；规划模型据此在节点上声明 `agent_needs`（「我需要做什么」而不是「我要调哪个 server」），
再由 services/agents.match_catalog 的漏斗二做确定性评分，选出 primary + 备选。

边界（N1–N4）同样在此判定：
- N1 纯语言智能：写作 / 翻译 / 总结 / 分析类需求 → 模型直出，不调外部（节点级判定，不是任务级——
  同一个人可以既写又调：写正文用模型、取事实用外部）；
- N2 上下文已含 / N3 内置已覆盖 / N4 用户约束：在执行期由 runner 与 policy 判定（数据在那儿）。
"""
from __future__ import annotations

import re

# ---- 判据 C1–C5：命中任意一条 → 「值得调外部」 ----
_CRITERIA: list[tuple[str, str, str]] = [
    ("C1", "时效事实", r"股价|汇率|油价|天气|气温|航班|机票|库存|票价|现价|实时|最新|今日|今天|本周|"
                       r"近日|新闻|赛程|比分|排名|版本号|发布(?:时间|日期)|上线(?:时间|日期)|"
                       r"(?<![a-z])(price|stock|exchange rate|weather|flight|latest|today|release|version|news|score)(?![a-z])"),
    ("C2", "能力缺口", r"发到|发送到|发给|提交到|提交给|发布到|上传到|推送到|同步到|写回|回复|评论|"
                       r"建(?:一个)?(?:日程|会议|任务|工单|issue)|预约|预订|下单|改期|删掉|关闭(?:issue|工单|任务)|"
                       r"(?<![a-z])(send|submit|publish|upload|push to|post to|reply|create (?:an )?issue|schedule a|book a|merge|close the)(?![a-z])"),
    ("C3", "私域数据", r"我的(?:账号|账户|仓库|邮箱|邮件|日历|日程|云盘|网盘|笔记|订单|账单|项目|数据|文件|资料|交易|持仓)|"
                       r"我们(?:公司|团队|内部)|内部(?:系统|数据|文档)|私有|"
                       r"(?<![a-z])(my |our )(repo|repository|mail|inbox|calendar|drive|notion|account|orders?|files?|notes?)(?![a-z])"),
    ("C4", "真实执行", r"运行(?:一下|代码|脚本|命令)|跑一下|执行(?:一下|代码|脚本|命令)|命令行|沙箱|"
                       r"转码|剪辑|截图|渲染|抓取|爬取|OCR|转写|"
                       r"(?<![a-z])(run the|execute|screenshot|render|transcode|scrape|ocr|transcribe)(?![a-z])"),
    ("C5", "权威可验证", r"法条|法规|条文|原文|论文|文献|财报|年报|官方文档|一手来源|标准|规范|引用来源|"
                       r"(?<![a-z])(paper|documentation|official docs|specification|filing|regulation|cite|source)(?![a-z])"),
]

# ---- 边界 N1：纯语言智能（写作 / 翻译 / 总结 / 分析 / 头脑风暴 / 代码解释） ----
_N1 = (r"写一篇|写一份|写个|起草|撰写|翻译|润色|改写|总结|概括|摘要|头脑风暴|创意|起名|取个名|"
       r"分析一下|解释一下|讲解|科普|怎么说|"
       r"(?<![a-z])(write a|draft|translate|summari[sz]e|polish|rewrite|brainstorm|explain|describe how)(?![a-z])")

# ---- 建议域：命中关键词 → 12 域（供目录摘要排序与漏斗二聚焦） ----
_DOMAIN_HINTS: list[tuple[str, str]] = [
    ("code", r"代码|仓库|提交|分支|合并|issue|pull request|bug|报错|异常|堆栈|git|repo|commit|merge|review|sentry|linear|jira"),
    ("data", r"数据库|SQL|表结构|查询语句|数据集|数据表|统计口径|埋点|postgres|sqlite|duckdb|mysql|mongodb|redis|supabase"),
    ("browser", r"网页快照|浏览器|打开网页|截图网页|抓取网页|页面内容|playwright|browser"),
    ("search", r"检索|搜索|查资料|查一下|调研|文献|论文|最新资料|context7|search|research|lookup|exa|tavily|firecrawl"),
    ("office", r"文档|表格|飞书|腾讯文档|notion|excel|sheets|会议纪要|周报|幻灯片|演示"),
    ("chat", r"消息|聊天记录|邮件|群聊|通知|slack|飞书消息|企业微信|微信|钉钉|email|mail|message|gmail"),
    ("design", r"设计稿|原型|配色|海报|logo|figma|canva|设计系统|素材库|blender"),
    ("finance", r"账单|发票|支付|交易|订单|收入|成本|预算表|报价|股票|基金|行情|金融|stripe|invoice|payment|企查查"),
    ("cloud", r"部署|服务器|云服务|域名|DNS|监控|告警|日志|CI|CD|vercel|cloudflare|docker|k8s|grafana|AWS"),
    ("life", r"日程|会议|提醒|日历|旅行|行程|路线|地图|天气|待办|calendar|schedule|todo|map|todoist|高德"),
    ("media", r"视频|音频|字幕|转写|配音|剪辑|图片处理|ffmpeg|whisper|tts|ocr"),
    ("knowledge", r"知识库|笔记|本地文件|目录文件|资料库|obsidian|chroma|qdrant|filesystem"),
]

_CAP = 3


def _hits(pat: str, text: str) -> list[str]:
    return [m.group(0) for m in re.finditer(pat, text, re.I)][:3]


def assess(goal: str, lang: str = "zh") -> dict:
    """对任务目标做一次确定性判定：命中判据 / 边界提示 / 建议域。纯函数，同样输入同样输出。"""
    g = goal or ""
    criteria = []
    for cid, label, pat in _CRITERIA:
        h = _hits(pat, g)
        if h:
            criteria.append({"id": cid, "label": label, "hits": h})
    boundaries = []
    if _hits(_N1, g) and not criteria:
        boundaries.append({"id": "N1", "label": "纯语言智能", "hits": []})   # 无判据 + 写作类 → 明确不调
    domains = [d for d, pat in _DOMAIN_HINTS if re.search(pat, g, re.I)][:_CAP]
    return {"criteria": criteria, "boundaries": boundaries, "domains": domains}


def summary(a: dict, lang: str = "zh") -> str:
    """事件流里的一句话（「什么时候不调」也对用户透明，见 prd/08 §5.6）。"""
    zh = lang != "en"
    if a.get("criteria"):
        ids = "、".join(f"{c['id']} {c['label']}" if zh else f"{c['id']} {c['label']}" for c in a["criteria"])
        return (f"本任务命中调用判据：{ids}；相关域：{'、'.join(a['domains']) or '通用'}" if zh
                else f"External-agent criteria hit: {ids}; domains: {', '.join(a['domains']) or 'general'}")
    return ("本任务未命中任何调用判据（纯语言智能类）：默认不调用外部 Agent，由内置能力直接完成（N1）" if zh
            else "No external-agent criteria hit (pure language intelligence): built-in capabilities handle it (N1)")


def hint(lang: str, mode: str, a: dict) -> str:
    """注入规划提示词的决策指引（三档模式：conservative / balanced / aggressive）。"""
    zh = lang != "en"
    sig = "、".join(f"{c['id']}" for c in a.get("criteria") or []) or ("无" if zh else "none")
    if zh:
        head = (f"外部 Agent 调用决策（判据信号：{sig}）。为某个步骤声明 agent_needs 之前，先判断它是否真的符合："
                "C1 需要此刻为真的事实（价格/天气/最新版本/今日新闻）；C2 需要作用于外部系统的动作（发送/提交/建日程）；"
                "C3 答案在用户自己的外部账号里（我的邮件/仓库/日历）；C4 需要真实执行环境（跑代码/截图/转码）；"
                "C5 需要可核验的一手来源（论文/官方文档/财报）。"
                "纯写作 / 翻译 / 总结 / 分析 / 头脑风暴类步骤属于 N1：**不要**声明 agent_needs，内置能力直接完成，更快更省；"
                "内置工具已覆盖的不要重复声明（N3）；上游或知识库已有的信息不要重复获取（N2）。"
                "需要外部能力时，agent_needs 写「我需要做什么」的短句（如「读取我的仓库的 open issues」），不要写 server 名。")
        tail = {"conservative": "（保守档）只考虑上方「已连接」列表里的 Agent，且必须命中判据才声明。",
                "aggressive": "（激进档）主动审视每个步骤：能接外部 Agent 明显做得更好的，优先声明 agent_needs。",
                "balanced": "（平衡档）命中判据即可声明；拿不准时宁可不声明。"}.get(mode, "")
    else:
        head = (f"External-agent decision (criteria signals: {sig}). Before adding agent_needs to a step, check whether it really matches: "
                "C1 must-be-current facts (prices/weather/latest versions/today's news); C2 must act on an external system (send/submit/schedule); "
                "C3 the answer lives in the user's own external accounts (my mail/repo/calendar); C4 needs a real execution environment "
                "(run code/screenshot/transcode); C5 needs verifiable first-hand sources (papers/official docs/filings). "
                "Pure writing / translation / summary / analysis / brainstorming steps are N1: do NOT declare agent_needs, built-in capabilities "
                "handle them faster and cheaper. Do not redeclare what built-in tools already cover (N3); never refetch what upstream steps or the "
                "knowledge base already contain (N2). When external capability IS needed, phrase agent_needs as a short statement of what you need "
                "(e.g. \"read the open issues of my repo\"), not a server name.")
        tail = {"conservative": " (conservative mode) Only consider the connected agents listed above, and only when a criterion is hit.",
                "aggressive": " (aggressive mode) Actively review every step: if an external agent would clearly do better, request it.",
                "balanced": " (balanced mode) Declare on a criterion hit; when unsure, prefer not to."}.get(mode, "")
    return head + tail
