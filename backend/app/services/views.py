"""读模型：任务快照（前端首屏与重连校准）与只读回放（INV-06：纯计算，不触发任何执行）。"""
from __future__ import annotations

from .. import db, events
from . import artifacts, plans, store
from .runner import RUNNER


def task_summary(t: dict) -> dict:
    keys = ("id", "title", "goal", "mode", "lane", "status", "paused", "output_language", "model_choice", "max_concurrency",
            "budget_cny", "cost_used", "active_version", "est_makespan_s", "est_serial_s", "actual_makespan_s", "error_code",
            "created_at", "updated_at", "completed_at", "run_started_at", "playbook_id", "attachments", "completion_quality")
    out = {k: t.get(k) for k in keys}
    out["paused"] = bool(out["paused"])
    return out


def phase(t: dict, nodes: list[dict]) -> str:
    if t["status"] != "running":
        return t["status"]
    if t["paused"]:
        return "paused"
    if any(n["status"] == "awaiting_approval" for n in nodes):
        return "awaiting_approval"                           # 唯一可能等人的位置：用户开启的安全防护
    return "running"


def snapshot(task_id: str, user_id: str) -> dict:
    t = store.get_task(task_id, user_id)
    pv = store.active_pv(t) or store.draft_pv(task_id)
    nodes = store.load_nodes(pv["id"]) if pv else []
    est = plans.estimate(nodes, t["max_concurrency"]) if nodes else None
    if est:
        est.pop("rank")
    approvals = db.q("SELECT id, node_key, plan_version, status, kind, tool, args, created_at FROM approvals "
                     "WHERE task_id=? AND status='pending' AND plan_version=?", (task_id, pv["version"] if pv else -1))
    for a in approvals:                                      # kind=tool 时 args 是危险操作的具体参数（供审批界面展示）
        a["args"] = db.j(a.get("args"), {})
    att = db.q("SELECT * FROM attention_items WHERE task_id=? AND status='open' AND plan_version=? ORDER BY created_at",
               (task_id, pv["version"] if pv else -1))
    comments = db.q("SELECT * FROM comments WHERE task_id=? ORDER BY created_at", (task_id,))
    art = artifacts.latest(task_id)
    last = db.q1("SELECT COALESCE(MAX(seq),0) AS s FROM events WHERE task_id=?", (task_id,))["s"]
    versions = db.q("SELECT version, status, reason, created_at FROM plan_versions WHERE task_id=? ORDER BY version", (task_id,))
    cost_rows = db.q("SELECT node_key, model_id, source, tokens_in, tokens_out, cost_cny, latency_ms, ok, error_code, created_at "
                     "FROM llm_calls WHERE task_id=? ORDER BY created_at DESC LIMIT 200", (task_id,))
    return {"task": task_summary(t), "phase": phase(t, nodes), "plan_version": pv["version"] if pv else None,
            "plan_status": pv["status"] if pv else None, "deliverable": db.j(pv["deliverable"], {}) if pv else {},
            "optimizations": db.j(pv["optimizations"], []) if pv else [],
            "nodes": [plans.public_node(n) for n in nodes], "estimate": est, "approvals": approvals,
            "attention": att, "comments": comments, "versions": versions,
            "artifact": {"id": art["id"], "plan_version": art["plan_version"], "sections": len(art["sections"]),
                         "cards": len(art.get("cards") or [])} if art else None,
            "running": RUNNER.running_count(task_id), "effective_limit": t["max_concurrency"],
            "last_seq": last, "costs": cost_rows}


def replay(task_id: str, user_id: str, upto_seq: int) -> dict:
    """按事件重建某一时刻的节点状态。只读：不访问调度器、不调用模型或工具。"""
    store.get_task(task_id, user_id)
    evs = events.history(task_id, 0, 100000)
    status: dict[str, str] = {}
    edges: list = []
    version, cost, at = None, 0.0, None
    for e in evs:
        if e["seq"] > upto_seq:
            break
        at = e["created_at"]
        p, k = e["payload"], e["node_key"]
        if e["type"] == "plan.confirmed":
            version = p.get("version")
            status = {nk: "pending" for nk in p.get("nodes", [])}
            edges = p.get("edges", [])
        elif e["type"] == "plan.version_created":
            version = p["version"]
            for nk in p.get("removed", []):
                status.pop(nk, None)
            for nk in p.get("affected", []) + p.get("added", []):
                status[nk] = "pending"
        elif k and e["plan_version"] == version:
            st = {"node.started": "running", "node.succeeded": "succeeded", "node.reused": "succeeded",
                  "node.failed": "failed", "node.skipped": "skipped", "tool.approval_required": "awaiting_approval",
                  "node.speculative_committed": "succeeded"}.get(e["type"])
            if st:
                status[k] = st
    return {"seq": upto_seq, "at": at, "plan_version": version, "status": status, "edges": edges, "readonly": True, "cost": cost}
