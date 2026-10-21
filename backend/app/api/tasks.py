"""任务、计划、执行控制、修订、安全防护、评论、关注项、成果、布局、回放与 SSE。"""
from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import db, events
from ..core import graph
from ..llm import gateway
from ..util import ApiError, clip, detect_language, new_id, now_ms
from ..services import artifacts, background, plans, revisions, store, views
from ..services import settings as settings_svc
from ..services.runner import RUNNER
from .common import body, current_user, num, opt_str, req_str

router = APIRouter(prefix="/api")
_bg: set[asyncio.Task] = set()


def _spawn(task_id, coro):
    t = background.start(task_id, coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def _own(request: Request, task_id: str) -> tuple[dict, dict]:
    u = current_user(request)
    return u, store.get_task(task_id, u["id"])


# ---------------- 任务 ----------------
@router.post("/tasks", status_code=202)
async def create_task(request: Request):
    u = current_user(request)
    d = await body(request)
    goal = req_str(d, "goal", 4000)
    s = settings_svc.get(u["id"])
    mode = d.get("mode") or s["execution.default_mode"]
    if mode not in ("auto", "quick", "team"):
        raise ApiError(400, "FIELD_INVALID", "mode 只能是 auto / quick / team")
    atts = [a for a in (d.get("attachments") if isinstance(d.get("attachments"), list) else [])[:20] if isinstance(a, str)]
    if atts:
        ok = {r["id"] for r in db.q(f"SELECT id FROM kb_documents WHERE user_id=? AND id IN ({','.join('?' * len(atts))})", [u["id"], *atts])}
        atts = [a for a in atts if a in ok]                       # INV-08：只能附加自己的资料
    model_choice = opt_str(d, "model_choice", 200) or s["models.default_model"]
    if model_choice != "auto" and not gateway.model_info(model_choice, u["id"]):
        raise ApiError(400, "MODEL_NOT_FOUND", "所选模型不存在或不可用")
    out_lang = d.get("output_language") or (s["general.output_language"] if s["general.output_language"] != "follow_input" else detect_language(goal))
    if out_lang not in ("zh", "en"):
        out_lang = detect_language(goal)
    if not d.get("budget_cny"):                             # 0 / 空 → 使用设置中的默认预算
        d.pop("budget_cny", None)
    budget = num(d, "budget_cny", s["execution.task_budget_cny"], 0.001, 100)
    pb = opt_str(d, "playbook_id", 64)
    if pb and not db.q1("SELECT id FROM playbooks WHERE id=? AND user_id=?", (pb, u["id"])):
        raise ApiError(404, "NOT_FOUND", "工作流不存在")
    t = {"id": new_id("tsk"), "user_id": u["id"], "title": clip(re.sub(r"\[[a-z0-9]+\]", "", goal).strip() or goal, 24), "goal": goal,
         "mode": "team" if pb else mode, "status": "triaging", "output_language": out_lang, "model_choice": model_choice,
         "max_concurrency": int(s["execution.max_concurrency"]), "budget_cny": budget, "attachments": atts, "playbook_id": pb,
         "created_at": now_ms(), "updated_at": now_ms()}
    with db.Tx():
        db.insert("tasks", t)
        events.emit(t["id"], "task.created", {"mode": t["mode"], "output_language": out_lang, "model_choice": model_choice})
    _spawn(t["id"], plans.triage_and_plan(t["id"], RUNNER))
    return {"task": views.task_summary(store.get_task(t["id"]))}


@router.get("/tasks")
async def list_tasks(request: Request, q: str = "", status: str = "", limit: int = 50):
    u = current_user(request)
    sql, p = "SELECT * FROM tasks WHERE user_id=?", [u["id"]]
    if status:
        sql += " AND status=?"
        p.append(status)
    if q:
        sql += " AND (title LIKE ? OR goal LIKE ?)"
        p += [f"%{q}%", f"%{q}%"]
    rows = db.q(sql + " ORDER BY updated_at DESC LIMIT ?", p + [min(200, max(1, limit))])
    out = []
    for r in rows:
        r["attachments"] = db.j(r["attachments"], [])
        s = views.task_summary(r)
        cnt = db.q1("SELECT COUNT(*) AS n, SUM(n.status='succeeded') AS ok, SUM(n.status IN ('failed','skipped')) AS bad, "
                    "SUM(n.status='awaiting_approval') AS ap FROM nodes n "
                    "JOIN plan_versions p ON p.id=n.plan_version_id WHERE p.task_id=? AND p.version=?", (r["id"], r["active_version"] or 1))
        s["progress"] = {"total": cnt["n"] or 0, "done": cnt["ok"] or 0, "issues": cnt["bad"] or 0, "awaiting": cnt["ap"] or 0}
        out.append(s)
    return {"items": out}


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    _, t = _own(request, task_id)
    return {"task": views.task_summary(t)}


@router.get("/tasks/{task_id}/state")
async def task_state(request: Request, task_id: str):
    u = current_user(request)
    return views.snapshot(task_id, u["id"])


@router.delete("/tasks/{task_id}")
async def delete_task(request: Request, task_id: str):
    _, t = _own(request, task_id)
    RUNNER.cancel(t["id"])
    db.ex("DELETE FROM tasks WHERE id=?", (t["id"],))
    return {"ok": True}


# ---------------- 计划草稿 ----------------
@router.get("/tasks/{task_id}/plan/draft")
async def get_draft(request: Request, task_id: str):
    _own(request, task_id)
    return plans.draft_view(task_id)


@router.post("/tasks/{task_id}/plan/draft/ops")
async def draft_ops(request: Request, task_id: str):
    u, _ = _own(request, task_id)
    d = await body(request)
    return plans.edit_draft(task_id, u["id"], d.get("ops") or [])


@router.post("/tasks/{task_id}/plan/optimizations/{opt_id}/apply")
async def apply_opt(request: Request, task_id: str, opt_id: str):
    u, _ = _own(request, task_id)
    return plans.apply_optimization(task_id, u["id"], opt_id)


@router.post("/tasks/{task_id}/plan/replan", status_code=202)
async def replan(request: Request, task_id: str):
    u, t = _own(request, task_id)
    if t["status"] not in ("plan_review", "failed"):
        raise ApiError(409, "STATE_CONFLICT", "只有失败的任务可以重新规划")
    d = await body(request)
    fb = req_str(d, "feedback", 1000, required=False) or None

    async def run():
        try:
            await plans.plan_and_start(task_id, RUNNER, fb)   # 重新规划后直接开跑，不需要再确认
        except (ApiError, gateway.LLMError, gateway.BudgetExceeded) as e:
            plans._fail(task_id, e.code, e.message)
        except Exception as e:     # noqa: BLE001  任何意外都要落为失败，不能让任务卡在「生成计划中」
            plans._fail(task_id, "INTERNAL", clip(str(e), 200))
    _spawn(task_id, run())
    return {"status": "planning"}


@router.post("/tasks/{task_id}/plans/confirm", status_code=202)
async def confirm_plan(request: Request, task_id: str):
    """兜底入口：正常流程由 plan_and_start 自动确认并开跑，这里只用于中断后的手动接续。"""
    u, t = _own(request, task_id)
    _ = await body(request)
    v = plans.confirm(task_id, u["id"])
    RUNNER.kick(task_id)
    return {"version": v}


# ---------------- 执行控制 ----------------
@router.post("/tasks/{task_id}/pause")
async def pause(request: Request, task_id: str):
    _own(request, task_id)
    RUNNER.pause(task_id)
    return {"paused": True}


@router.post("/tasks/{task_id}/resume")
async def resume(request: Request, task_id: str):
    _own(request, task_id)
    RUNNER.resume(task_id)
    return {"paused": False}


@router.post("/tasks/{task_id}/step")
async def step(request: Request, task_id: str):
    _own(request, task_id)
    RUNNER.step(task_id)
    return {"ok": True}


@router.post("/tasks/{task_id}/cancel")
async def cancel(request: Request, task_id: str):
    _, t = _own(request, task_id)
    if t["status"] in ("completed", "cancelled"):
        raise ApiError(409, "STATE_CONFLICT", "任务已结束")
    RUNNER.cancel(task_id)
    return {"status": "cancelled"}


@router.post("/tasks/{task_id}/escalate", status_code=202)
async def escalate(request: Request, task_id: str):
    _, t = _own(request, task_id)
    if t["lane"] != "quick" or t["status"] != "completed":
        raise ApiError(409, "STATE_CONFLICT", "只有已完成的快速回答可以升级为团队任务")
    db.update("tasks", {"id": task_id}, {"lane": "team", "mode": "team", "status": "planning"})
    events.emit(task_id, "task.escalated", {})

    async def run():
        try:
            await plans.plan_and_start(task_id, RUNNER)       # 升级为团队任务后直接规划并开跑
        except (ApiError, gateway.LLMError, gateway.BudgetExceeded) as e:
            plans._fail(task_id, e.code, e.message)
        except Exception as e:     # noqa: BLE001
            plans._fail(task_id, "INTERNAL", clip(str(e), 200))
    _spawn(task_id, run())
    return {"status": "planning"}


@router.patch("/tasks/{task_id}/model-choice")
async def model_choice(request: Request, task_id: str):
    u, t = _own(request, task_id)
    d = await body(request)
    ref = opt_str(d, "model_choice", 200) or "auto"
    if t["status"] in ("completed", "cancelled"):
        raise ApiError(409, "STATE_CONFLICT", "任务已结束")
    if ref != "auto" and not gateway.model_info(ref, u["id"]):
        raise ApiError(400, "MODEL_NOT_FOUND", "所选模型不存在")
    db.update("tasks", {"id": task_id}, {"model_choice": ref})
    pv = store.active_pv(t) or store.draft_pv(task_id)
    pending = [n["node_key"] for n in store.load_nodes(pv["id"]) if n["status"] == "pending" and not n["model_override"]] if pv else []
    events.emit(task_id, "task.model_changed", {"model_choice": ref, "affects_pending": pending})
    return {"affects_pending": pending}


@router.get("/tasks/{task_id}/events")
async def task_events(request: Request, task_id: str, since_seq: int = 0, limit: int = 500):
    _own(request, task_id)
    return {"items": events.history(task_id, since_seq, min(5000, limit))}


@router.get("/tasks/{task_id}/stream")
async def stream(request: Request, task_id: str, since_seq: int = 0):
    _own(request, task_id)
    if events.sub_count(task_id) >= 4:
        raise ApiError(429, "TOO_MANY_STREAMS", "该任务打开的实时连接过多")
    last = request.headers.get("last-event-id")
    since = int(last) if last and last.isdigit() else since_seq
    qu = events.subscribe(task_id)                     # 先订阅，再补发历史，避免漏事件

    def fmt(e):
        return f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"

    async def gen():
        try:
            sent = since
            while True:                                      # 分页补发历史：事件再多也不漏
                page = events.history(task_id, sent, 1000)
                for e in page:
                    sent = e["seq"]
                    yield fmt(e)
                if len(page) < 1000:
                    break
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(qu.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if events.lagging(qu):                       # 队列曾溢出：清空队列，从数据库按序补齐
                    while not qu.empty():
                        qu.get_nowait()
                    for x in events.history(task_id, sent, 5000):
                        sent = x["seq"]
                        yield fmt(x)
                    continue
                if e["seq"] <= sent:
                    continue
                sent = e["seq"]
                yield fmt(e)
        finally:
            events.unsubscribe(task_id, qu)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/tasks/{task_id}/replay")
async def replay(request: Request, task_id: str, seq: int):
    u = current_user(request)
    return views.replay(task_id, u["id"], seq)


@router.get("/tasks/{task_id}/versions")
async def versions(request: Request, task_id: str):
    _own(request, task_id)
    return {"items": db.q("SELECT version, status, reason, created_at FROM plan_versions WHERE task_id=? ORDER BY version", (task_id,))}


@router.get("/tasks/{task_id}/versions/{v}")
async def version_detail(request: Request, task_id: str, v: int):
    _own(request, task_id)
    pv = store.get_pv(task_id, v)
    if not pv:
        raise ApiError(404, "NOT_FOUND", "版本不存在")
    return {"version": v, "status": pv["status"], "readonly": True,
            "nodes": [plans.public_node(n) for n in store.load_nodes(pv["id"])]}


# ---------------- 修订 ----------------
def _change_from(d: dict) -> dict:
    ins = d.get("instructions") or {}
    if isinstance(ins, str) and d.get("node_key"):
        ins = {d["node_key"]: [ins]}
    ins = {k: ([v] if isinstance(v, str) else list(v)) for k, v in ins.items()}
    return {"instructions": ins, "ops": d.get("ops") or [], "kind": d.get("reason") or "manual"}


@router.post("/tasks/{task_id}/revisions/preview")
async def rev_preview(request: Request, task_id: str):
    _, t = _own(request, task_id)
    return revisions.preview(t, _change_from(await body(request)))


@router.post("/tasks/{task_id}/revisions", status_code=202)
async def rev_create(request: Request, task_id: str):
    _, t = _own(request, task_id)
    d = await body(request)
    reason = d.get("reason") if d.get("reason") in ("manual", "canvas_edit", "model_override", "reassign", "steer") else "manual"
    if t["status"] not in ("running", "completed", "failed"):
        raise ApiError(409, "STATE_CONFLICT", "当前状态不能修订")
    return revisions.create(t, _change_from(d), reason)


@router.post("/tasks/{task_id}/sections/{section_key}/rewrite", status_code=202)
async def rewrite(request: Request, task_id: str, section_key: str):
    _, t = _own(request, task_id)
    d = await body(request)
    return revisions.rewrite_section(t, section_key, req_str(d, "instructions", 1000))


@router.patch("/tasks/{task_id}/sections/{section_key}")
async def edit_section(request: Request, task_id: str, section_key: str):
    """人工编辑成果章节（v1.8）：用户在画布右侧面板直接修改结果。

    编辑不改计划、不建版本；保存后立即重建成果（画布 / 导出 / 打包文件同步），
    之后的对话重算该步骤或它的下游时，编辑稿会作为上下文注入模型。
    """
    u, t = _own(request, task_id)
    d = await body(request)
    content = d.get("content")
    if not isinstance(content, str):
        raise ApiError(400, "FIELD_INVALID", "content 应为字符串")
    with db.Tx():
        r = artifacts.save_edit(t, section_key, content, u["id"])
        events.emit(task_id, "artifact.edited", {"section_key": section_key, "chars": r["chars"],
                                                 "artifact_id": r["artifact_id"]}, section_key)
    return r


# ---------------- 节点 ----------------
@router.get("/tasks/{task_id}/nodes/{node_key}")
async def node_detail(request: Request, task_id: str, node_key: str):
    _, t = _own(request, task_id)
    pv = store.active_pv(t) or store.draft_pv(task_id)
    n = store.node_by_key(pv["id"], node_key) if pv else None
    if not n:
        raise ApiError(404, "NOT_FOUND", "步骤不存在")
    exs = db.q("SELECT id, plan_version, attempt, status, speculative, input_hash, model_used, cost_cny, error_code, error, "
               "tool_calls, started_at, finished_at FROM executions WHERE task_id=? AND node_key=? ORDER BY started_at DESC LIMIT 20",
               (task_id, node_key))
    for e in exs:
        e["tool_calls"] = db.j(e["tool_calls"], [])
    upstream = [store.node_by_key(pv["id"], d) for d in n["depends_on"]]
    snap = {"goal": t["goal"], "step": {"title": n["title"], "description": n["description"], "acceptance": n["acceptance"]},
            "upstream": [{"node_key": u["node_key"], "title": u["title"], "summary": (u.get("output") or {}).get("summary")} for u in upstream if u],
            "tools": n["tools"], "instructions": [i.get("text") for i in n["revision_instructions"] if i.get("kind") != "rerun"]}
    evs = db.q("SELECT seq, type, payload, created_at FROM events WHERE task_id=? AND node_key=? ORDER BY seq DESC LIMIT 60", (task_id, node_key))
    for e in evs:
        e["payload"] = db.j(e["payload"], {})
    return {"node": plans.public_node(n), "input_snapshot": snap, "executions": exs, "events": evs,
            "approval": db.q1("SELECT * FROM approvals WHERE node_id=? ORDER BY created_at DESC LIMIT 1", (n["id"],))}


@router.post("/tasks/{task_id}/nodes/{node_key}/retry", status_code=202)
async def retry(request: Request, task_id: str, node_key: str):
    _, t = _own(request, task_id)
    pv = store.active_pv(t)
    n = store.node_by_key(pv["id"], node_key) if pv else None
    if not n or n["status"] != "failed" or t["status"] not in ("running", "completed"):
        raise ApiError(409, "STATE_CONFLICT", "只有失败任务里失败的步骤可以重试")
    nodes = store.load_nodes(pv["id"])
    down = graph.descendants(nodes, [node_key])
    with db.Tx():
        store.set_node(n["id"], status="pending", error=None, error_code=None)
        for m in nodes:                                      # 上游修复后，被连带跳过的下游重新排队
            if m["node_key"] in down and m["status"] == "skipped":
                store.set_node(m["id"], status="pending")
        if t["status"] == "completed":                       # 重新开门：跑完后自动重新收官
            db.update("tasks", {"id": task_id}, {"status": "running", "completed_at": None, "updated_at": now_ms()})
    events.emit(task_id, "node.retry_requested", {}, node_key, pv["version"])
    RUNNER.kick(task_id)
    return {"attempt": n["attempts"] + 1}


@router.post("/tasks/{task_id}/nodes/{node_key}/rerun", status_code=202)
async def rerun(request: Request, task_id: str, node_key: str):
    _, t = _own(request, task_id)
    return revisions.rerun(t, node_key)


@router.post("/tasks/{task_id}/nodes/{node_key}/feedback")
async def feedback(request: Request, task_id: str, node_key: str):
    u, t = _own(request, task_id)
    d = await body(request)
    sig = d.get("signal")
    if sig not in ("thumb_up", "thumb_down"):
        raise ApiError(400, "FIELD_INVALID", "signal 无效")
    events.emit(task_id, "node.feedback", {"signal": sig}, node_key, t["active_version"])
    return {"ok": True}


# ---------------- 审批 ----------------
@router.get("/tasks/{task_id}/approvals")
async def approvals(request: Request, task_id: str):
    _own(request, task_id)
    return {"items": db.q("SELECT * FROM approvals WHERE task_id=? ORDER BY created_at DESC", (task_id,))}


@router.post("/approvals/{approval_id}/approve")
async def approve(request: Request, approval_id: str):
    u = current_user(request)
    return RUNNER.approve(approval_id, u["id"])          # 安全防护的批准：唯一可能停顿等人之处


@router.post("/approvals/{approval_id}/reject", status_code=202)
async def reject(request: Request, approval_id: str):
    u = current_user(request)
    a = db.q1("SELECT task_id FROM approvals WHERE id=?", (approval_id,))
    if not a:
        raise ApiError(404, "NOT_FOUND", "审批不存在")
    t = store.get_task(a["task_id"], u["id"])
    d = await body(request)
    return revisions.reject(t, approval_id, u["id"], opt_str(d, "reason", 1000) or "")


# ---------------- 评论 ----------------
@router.post("/tasks/{task_id}/comments")
async def add_comment(request: Request, task_id: str):
    u, t = _own(request, task_id)
    d = await body(request)
    nk = opt_str(d, "node_key", 64)
    pv = store.active_pv(t)
    if nk and (not pv or not store.node_by_key(pv["id"], nk)):
        raise ApiError(404, "NOT_FOUND", "步骤不存在")
    c = {"id": new_id("cm"), "task_id": task_id, "node_key": nk, "section_key": opt_str(d, "section_key", 64),
         "user_id": u["id"], "body": req_str(d, "body", 1000), "status": "open", "created_at": now_ms()}
    db.insert("comments", c)
    events.emit(task_id, "comment.added", {"body": c["body"]}, c["node_key"], t["active_version"])
    return {"comment": c}


@router.get("/tasks/{task_id}/comments")
async def list_comments(request: Request, task_id: str):
    _own(request, task_id)
    return {"items": db.q("SELECT * FROM comments WHERE task_id=? ORDER BY created_at", (task_id,))}


@router.delete("/comments/{cid}")
async def del_comment(request: Request, cid: str):
    u = current_user(request)
    if not db.ex("DELETE FROM comments WHERE id=? AND user_id=? AND status='open'", (cid, u["id"])):
        raise ApiError(404, "NOT_FOUND", "评论不存在或已应用")
    return {"ok": True}


@router.post("/tasks/{task_id}/comments/apply", status_code=202)
async def apply_comments(request: Request, task_id: str):
    u, t = _own(request, task_id)
    d = await body(request)
    return revisions.apply_comments(t, u["id"], d.get("comment_ids"))


# ---------------- 关注项 ----------------
@router.get("/tasks/{task_id}/attention")
async def attention(request: Request, task_id: str):
    _, t = _own(request, task_id)
    return {"items": db.q("SELECT * FROM attention_items WHERE task_id=? AND plan_version=? ORDER BY created_at", (task_id, t["active_version"] or 0))}


def _att(request, aid):
    u = current_user(request)
    a = db.q1("SELECT * FROM attention_items WHERE id=?", (aid,))
    if not a:
        raise ApiError(404, "NOT_FOUND", "关注项不存在")
    return u, store.get_task(a["task_id"], u["id"]), a


@router.post("/attention/{aid}/resolve")
async def att_resolve(request: Request, aid: str):
    _, t, a = _att(request, aid)
    db.update("attention_items", {"id": aid}, {"status": "resolved"})
    events.emit(t["id"], "attention.resolved", {"kind": a["kind"]}, a["node_key"], a["plan_version"])
    RUNNER.kick(t["id"])
    return {"ok": True}


@router.post("/attention/{aid}/dismiss")
async def att_dismiss(request: Request, aid: str):
    _, t, a = _att(request, aid)
    if a["severity"] == "block":
        raise ApiError(409, "ATTENTION_BLOCKED", "阻断项需要回答或处理，不能直接忽略")
    db.update("attention_items", {"id": aid}, {"status": "dismissed"})
    return {"ok": True}


@router.post("/attention/{aid}/answer", status_code=202)
async def att_answer(request: Request, aid: str):
    _, t, a = _att(request, aid)
    d = await body(request)
    return revisions.answer_attention(t, aid, req_str(d, "answer", 1000))


# ---------------- 成果 ----------------
@router.get("/tasks/{task_id}/artifact")
async def get_artifact(request: Request, task_id: str, version: int | None = None):
    _own(request, task_id)
    a = artifacts.latest(task_id, version)
    if not a:
        raise ApiError(404, "NOT_FOUND", "还没有成果")
    return a


@router.get("/tasks/{task_id}/artifact/export")
async def export_artifact(request: Request, task_id: str, format: str = "md", version: int | None = None):
    _own(request, task_id)
    a = artifacts.latest(task_id, version)
    if not a:
        raise ApiError(404, "NOT_FOUND", "还没有成果")
    if format != "md":
        raise ApiError(400, "FORMAT_UNSUPPORTED", "当前仅支持 Markdown 导出")
    fn = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", a["title"])[:40] or "result"
    from urllib.parse import quote
    return PlainTextResponse(a["markdown"], media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn)}.md"})


@router.post("/tasks/{task_id}/artifact/archive")
async def archive(request: Request, task_id: str):
    _, t = _own(request, task_id)
    d = await body(request)
    doc = artifacts.archive(t, d.get("section_keys"))
    events.emit(task_id, "artifact.archived", {"document_id": doc["id"]})
    return {"document": doc}


# ---------------- 画布布局（INV-22：纯视图数据，不产生事件、不修改计划） ----------------
@router.get("/tasks/{task_id}/layout")
async def get_layout(request: Request, task_id: str):
    u, _ = _own(request, task_id)
    r = db.q1("SELECT * FROM canvas_layouts WHERE user_id=? AND task_id=?", (u["id"], task_id))
    return {"positions": db.j(r["positions"], {}), "viewport": db.j(r["viewport"], {}),
            "collapsed": db.j(r["collapsed"], {}), "art": db.j(r.get("art"), {})} if r else {}


@router.put("/tasks/{task_id}/layout")
async def put_layout(request: Request, task_id: str):
    u, _ = _own(request, task_id)
    d = await body(request)
    def coord(v, lo=-1e6, hi=1e6):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return min(hi, max(lo, f)) if f == f else 0.0
    def keys(v):                                             # 成果节点键列表（hidden / pinned）
        return [str(x)[:64] for x in (v if isinstance(v, list) else [])][:200]
    raw = d.get("positions") if isinstance(d.get("positions"), dict) else {}
    pos = {str(k)[:64]: {"x": coord(v.get("x")), "y": coord(v.get("y"))} for k, v in list(raw.items())[:200] if isinstance(v, dict)}
    rv = d.get("viewport") if isinstance(d.get("viewport"), dict) else {}
    vp = {"x": coord(rv.get("x")), "y": coord(rv.get("y")), "z": coord(rv.get("z", 1), 0.25, 2)} if rv else {}
    rc = d.get("collapsed") if isinstance(d.get("collapsed"), dict) else {}
    ra = d.get("art") if isinstance(d.get("art"), dict) else {}
    # v3.0：L2 本地操作（清单勾选 / 表格排序）同样是纯视图状态，只记在这里、不产生事件（prd/08 §6.3）
    # 清洗规则：空键丢弃；勾选下标只留 [0,500) 的整数（去重由前端保证、此处只做边界）；排序列下标同样限界
    checks = {str(k)[:64]: [int(x) for x in (v if isinstance(v, list) else []) if isinstance(x, (int, float)) and 0 <= x < 500][:200]
              for k, v in (ra.get("checks") if isinstance(ra.get("checks"), dict) else {}).items() if str(k)}
    sorts = {str(k)[:64]: [int(v[0]), 'desc' if str(v[1]) == 'desc' else 'asc']
             for k, v in (ra.get("sorts") if isinstance(ra.get("sorts"), dict) else {}).items()
             if str(k) and isinstance(v, list) and len(v) == 2 and isinstance(v[0], (int, float)) and 0 <= v[0] < 500}
    art = {"hidden": keys(ra.get("hidden")), "pinned": keys(ra.get("pinned")), "focus": bool(ra.get("focus", True)),
           "checks": dict(list(checks.items())[:200]), "sorts": dict(list(sorts.items())[:200])}
    row = {"positions": pos, "viewport": vp, "collapsed": {k: bool(v) for k, v in list(rc.items())[:10]},
           "art": art, "updated_at": now_ms()}
    if db.q1("SELECT 1 FROM canvas_layouts WHERE user_id=? AND task_id=?", (u["id"], task_id)):
        db.update("canvas_layouts", {"user_id": u["id"], "task_id": task_id}, row)
    else:
        db.insert("canvas_layouts", {"user_id": u["id"], "task_id": task_id, **row})
    return {"ok": True}
