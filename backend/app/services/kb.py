"""个人知识库（F17）与内置工具。

- 每个用户独立（INV-08）：所有查询都带 user_id 条件；
- 检索为 BM25（中文二元组 + 英文词），无需向量模型即可离线运行；
- 资料只作为数据进入提示词，永远不会被当作指令执行（INV-09）；
- 工具分风险类别（core/policy.py）：read 直接执行；write/delete/console 由用户设置决定是否先批准，
  file.* 与 shell.run 只操作该用户的任务工作区（services/workspace.py）。
"""
from __future__ import annotations

import ast
import json
import math
import operator
import re
from collections import Counter

from .. import db
from ..util import ApiError, clip, new_id, now_ms, tokenize
from . import workspace

CHUNK_CHARS = 420


def chunk_text(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题与段落切块，返回 [(标题路径, 文本)]。"""
    out: list[tuple[str, str]] = []
    heading, buf = "", ""
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        p = para.strip()
        if not p:
            continue
        m = re.match(r"^(#{1,6})\s+(.+)", p)
        if m and "\n" not in p:
            if buf:
                out.append((heading, buf))
                buf = ""
            heading = m.group(2).strip()
            continue
        if len(buf) + len(p) > CHUNK_CHARS and buf:
            out.append((heading, buf))
            buf = ""
        while len(p) > CHUNK_CHARS * 1.5:
            out.append((heading, p[:CHUNK_CHARS]))
            p = p[CHUNK_CHARS - 40:]
        buf = (buf + "\n\n" + p).strip()
    if buf:
        out.append((heading, buf))
    return out


def add_document(user_id: str, title: str, content: str, source: str, tags=None, task_id=None) -> dict:
    content = (content or "").strip()
    if not content:
        raise ApiError(400, "KB_EMPTY", "资料内容为空")
    doc = {"id": new_id("doc"), "user_id": user_id, "title": clip(title.strip() or "未命名资料", 120), "source": source,
           "tags": tags or [], "content": content, "chunk_count": 0, "cited_count": 0, "source_task_id": task_id,
           "created_at": now_ms(), "updated_at": now_ms()}
    with db.Tx():
        db.insert("kb_documents", doc)
        n = _index(doc)
    doc["chunk_count"] = n
    return public_doc(doc)


def _index(doc: dict) -> int:
    db.ex("DELETE FROM kb_chunks WHERE doc_id=?", (doc["id"],))
    chunks = chunk_text(doc["content"])
    for i, (h, t) in enumerate(chunks):
        terms = Counter(tokenize(doc["title"] + " " + h + " " + t))
        db.insert("kb_chunks", {"id": new_id("kbc"), "doc_id": doc["id"], "user_id": doc["user_id"], "idx": i,
                                "heading": h, "text": t, "terms": dict(terms)})
    db.update("kb_documents", {"id": doc["id"]}, {"chunk_count": len(chunks), "updated_at": now_ms()})
    return len(chunks)


def reindex(user_id: str, doc_id: str) -> int:
    d = db.q1("SELECT * FROM kb_documents WHERE id=? AND user_id=?", (doc_id, user_id))
    if not d:
        raise ApiError(404, "NOT_FOUND", "资料不存在")
    return _index(d)


def public_doc(d: dict) -> dict:
    return {k: (db.j(d[k], []) if k == "tags" and isinstance(d[k], str) else d[k])
            for k in ("id", "title", "source", "tags", "chunk_count", "cited_count", "source_task_id",
                      "created_at", "updated_at") if k in d} | {"chars": len(d.get("content", ""))}


def search(user_id: str, query: str, top_k: int = 5, doc_ids: list[str] | None = None) -> list[dict]:
    """BM25 检索，只在当前用户的分块中进行。"""
    qt = tokenize(query)
    if not qt:
        return []
    sql = "SELECT c.id, c.doc_id, c.heading, c.text, c.terms, d.title FROM kb_chunks c JOIN kb_documents d ON d.id=c.doc_id WHERE c.user_id=?"
    params: list = [user_id]
    if doc_ids is not None:
        if not doc_ids:
            return []
        sql += f" AND c.doc_id IN ({','.join('?' * len(doc_ids))})"
        params += doc_ids
    rows = db.q(sql, params)
    if not rows:
        return []
    docs = [db.j(r["terms"], {}) for r in rows]
    N = len(docs)
    avgdl = sum(sum(d.values()) for d in docs) / N or 1
    df = Counter(t for d in docs for t in set(d))
    k1, b = 1.4, 0.75
    scored = []
    for r, d in zip(rows, docs):
        dl = sum(d.values()) or 1
        s = 0.0
        for t in set(qt):
            f = d.get(t, 0)
            if f:
                idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    return [{"chunk_id": r["id"], "doc_id": r["doc_id"], "doc_title": r["title"], "heading": r["heading"],
             "score": round(s, 3), "text": r["text"]} for s, r in scored[:top_k]]


# ---------------- 内置工具（全部只读，INV-10 白名单） ----------------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.Mod: operator.mod}


def _bound(v):
    if isinstance(v, complex) or abs(v) > 1e18:              # 负数开小数次方得到复数、结果过大都拒绝
        raise ValueError("结果超出范围")
    return v


def _calc(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(right) > 10 or abs(left) > 1e6):
                raise ValueError("指数或底数过大")
            return _bound(_OPS[type(n.op)](left, right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("只支持数字与 + - * / % **")
    if len(expr) > 200:
        raise ValueError("表达式过长")
    return ev(ast.parse(expr, mode="eval"))


TOOL_SCHEMAS = {
    "kb.search": {"description": "Search the user's personal knowledge base. Returns cited chunks.",
                  "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    "reference.read": {"description": "Read the documents attached to this task.",
                       "parameters": {"type": "object", "properties": {}}},
    "calc.eval": {"description": "Evaluate an arithmetic expression.",
                  "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
    "map.resolve": {"description": "Resolve place names to GCJ-02 coordinates (mainland-China cities and sights). Use it to place itinerary stops on the map.",
                    "parameters": {"type": "object", "properties": {"places": {"type": "array", "items": {"type": "string"}}}, "required": ["places"]}},
    "web.search": {"description": "Search the public web across multiple engines (Sogou, 360, Bing); results are fused and de-duplicated. For hard topics pass 1-2 extra query variants in `queries` (other wording or the other language) — Chinese and English queries use different engine sets.",
                   "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                                   "queries": {"type": "array", "items": {"type": "string"},
                                                                               "description": "optional extra query variants (max 2)"}},
                                  "required": ["query"]}},
    "web.news": {"description": "Search recent news and articles (Google News, Bing News, WeChat public accounts for Chinese). Use for fresh events, releases and announcements.",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                                 "freshness": {"type": "string", "enum": ["day", "week", "month", "any"]}},
                                "required": ["query"]}},
    "web.scholar": {"description": "Search academic literature (OpenAlex, arXiv, Crossref). Returns papers with authors, year and citation counts — use it for theory, evidence and state-of-the-art.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    "web.code": {"description": "Search developer channels: GitHub repositories, Stack Overflow Q&A and Hacker News discussions. Use it for libraries, tools and implementation questions.",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                                 "kind": {"type": "string", "enum": ["all", "repos", "qa", "discuss"]}},
                                "required": ["query"]}},
    "web.fetch": {"description": "Open one public URL and read its full text (HTML is converted to clean text). Go beyond snippets: fetch the most promising pages found by web.search, read them, and cite the specifics.",
                  "parameters": {"type": "object", "properties": {"url": {"type": "string"},
                                                                  "max_chars": {"type": "integer", "description": "optional, default 12000"}},
                                 "required": ["url"]}},
    "file.list": {"description": "List the files currently in the user's task workspace.",
                  "parameters": {"type": "object", "properties": {}}},
    "file.write": {"description": "Create or overwrite one file in the task workspace.",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                                  "content": {"type": "string"}}, "required": ["path", "content"]}},
    "file.delete": {"description": "Delete one file from the task workspace. The user must approve each use.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    "shell.run": {"description": "Run one console command inside the task workspace. The user must approve each use.",
                  "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # v3.0 M2 按需发现（prd/08 §5.4）：执行中查找「还有谁能干这个」，只给候选清单 / 工具集，不含 schema
    "agent.search": {"description": "On-demand discovery: search the external-agent catalog when you need a capability this step does not have. "
                                    "Returns candidate agents (installed or not) with what they cover and what setup they need. "
                                    "At most 2 uses per step.",
                     "parameters": {"type": "object", "properties": {"query": {"type": "string",
                                                                               "description": "what capability you need, e.g. \"read my repository issues\""}},
                                    "required": ["query"]}},
    "agent.describe": {"description": "Show one external agent's curated toolset (read-only preview; installed agents show the real mounted tools). "
                                      "Use after agent.search to decide which agent fits.",
                       "parameters": {"type": "object", "properties": {"key": {"type": "string",
                                                                              "description": "agent key from agent.search, e.g. mcp.github"}},
                                      "required": ["key"]}},
}


def tool_defs(names: list[str], user_id: str = "") -> list[dict]:
    # 模型侧函数名不允许出现点号，用下划线代替
    out = [{"type": "function", "function": {"name": n.replace(".", "_"), **TOOL_SCHEMAS[n]}} for n in names if n in TOOL_SCHEMAS]
    if user_id and any(str(n).startswith("mcp.") for n in names):     # v3.0：外部 Agent 工具（schema 来自连接时缓存）
        from . import agents
        out += agents.tool_defs(user_id, [n for n in names if str(n).startswith("mcp.")])
    return out


def run_tool(name: str, args: dict, *, user_id: str, attachments: list[str], web_enabled: bool,
             task_id: str = "", version: int = 0) -> dict:
    """执行一个工具，返回可序列化结果。异常转换为 {error}，不让单个工具拖垮节点。

    注意：这里**不做**审批判定 —— 「该不该先问用户」由调度器在执行前用 core/policy.py 判定，
    能走到这里的危险操作都是已获批准的。
    """
    try:
        if task_id and name in ("file.write", "file.delete"):
            path = str(args.get("path") or "")
            if workspace.resolve(user_id, path) is None:
                return {"error": "PATH_DENIED"}
            args = {**args, "path": workspace.task_path(task_id, version, path)}
        if name.startswith("web."):                      # v2.1：外网检索全家桶（多引擎 / 垂直渠道 / 正文抓取）
            from . import websearch
            return websearch.run(name, args or {}, enabled=web_enabled)
        if name == "kb.search":
            q = str(args.get("query") or "")[:300]
            items = search(user_id, q, 5)
            return {"items": [{"chunk_id": i["chunk_id"], "doc_title": i["doc_title"], "text": clip(i["text"], 360)} for i in items]}
        if name == "reference.read":
            if not attachments:
                return {"items": [], "note": "本任务没有附件"}
            items = search(user_id, " ".join(args.values()) if args else "", 5, attachments) or [
                {"chunk_id": c["id"], "doc_title": c["title"], "text": c["text"]} for c in db.q(
                    f"SELECT c.id, c.text, d.title FROM kb_chunks c JOIN kb_documents d ON d.id=c.doc_id "
                    f"WHERE c.user_id=? AND c.doc_id IN ({','.join('?' * len(attachments))}) ORDER BY c.idx LIMIT 5",
                    [user_id, *attachments])]
            return {"items": [{"chunk_id": i["chunk_id"], "doc_title": i["doc_title"], "text": clip(i["text"], 360)} for i in items]}
        if name == "calc.eval":
            return {"result": _calc(str(args.get("expression") or ""))}
        if name == "map.resolve":                            # v1.9：地名 → GCJ-02 坐标（内置地标库，无 Key 依赖）
            from ..core import places
            names = [str(x)[:40] for x in (args.get("places") or []) if str(x).strip()][:12]
            return {"items": places.resolve_many(names)}
        if name == "file.list":
            return workspace.list_files(user_id)
        if name == "file.write":
            return workspace.write(user_id, args.get("path"), args.get("content"))
        if name == "file.delete":
            return workspace.delete(user_id, args.get("path"))
        if name == "shell.run":
            return workspace.run_command(user_id, args.get("command"))
        if name == "agent.search":                           # v3.0 M2：目录检索（含未连接候选）
            from . import agents
            return agents.search(user_id, str(args.get("query") or ""))
        if name == "agent.describe":
            from . import agents
            return agents.describe(user_id, str(args.get("key") or ""))
        return {"error": "TOOL_DENIED"}
    except Exception as e:     # noqa: BLE001
        return {"error": "TOOL_ERROR", "message": str(e)[:200]}


def mark_cited(chunk_ids: list[str]) -> None:
    for cid in set(chunk_ids):
        db.ex("UPDATE kb_documents SET cited_count=cited_count+1 WHERE id=(SELECT doc_id FROM kb_chunks WHERE id=?)", (cid,))


def chunk_refs(chunk_ids: list[str], user_id: str) -> dict[str, dict]:
    if not chunk_ids:
        return {}
    rows = db.q(f"SELECT c.id, c.doc_id, c.text, d.title FROM kb_chunks c JOIN kb_documents d ON d.id=c.doc_id "
                f"WHERE c.user_id=? AND c.id IN ({','.join('?' * len(chunk_ids))})", [user_id, *chunk_ids])
    return {r["id"]: {"chunk_id": r["id"], "doc_id": r["doc_id"], "doc_title": r["title"], "text": clip(r["text"], 200)} for r in rows}


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
