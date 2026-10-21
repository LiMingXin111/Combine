"""修订（F11）：运行期的任何修改都创建新的计划版本（INV-05）。

受影响集合 = 直接改动的节点 ∪ 它们的全部下游（章节重写只影响产出该章节的节点）；
其余节点沿用旧结果（含进行中的执行，结果会同时写回新版本节点）；等待批准的危险操作随新版本延续（待批准的具体操作一并复制）。

v1.8：用户手工编辑过的成果章节，其编辑稿会作为该步骤的修订上下文（revision_instructions，kind=user_edit）
注入——重跑时模型以「用户改后的版本」为基础继续，「修改之后再进行新的对话」也能读得懂用户的修改。
"""
from __future__ import annotations

import copy

from .. import db, events
from ..core import graph
from ..util import ApiError, clip, new_id, now_ms
from . import artifacts, plans, store
from .runner import RUNNER


def _compute(task: dict, change: dict) -> dict:
    pv = store.active_pv(task)
    if not pv:
        raise ApiError(409, "STATE_CONFLICT", "任务还没有已确认的计划")
    old = store.load_nodes(pv["id"])
    new = copy.deepcopy(old)
    ops = change.get("ops") or []
    new, rejected = plans.apply_ops(new, ops, task["user_id"])
    if rejected:                                              # 修订是原子的：任一操作非法则整组拒绝
        raise ApiError(400, rejected[0]["reason"], rejected[0]["message"], {"rejected_ops": rejected})
    idx_new = graph.index(new)
    old_keys = {n["node_key"] for n in old}
    direct: set[str] = set()
    for k, texts in (change.get("instructions") or {}).items():
        if k not in idx_new:
            raise ApiError(400, "NODE_NOT_FOUND", f"步骤 {k} 不存在")
        for t in texts:
            idx_new[k]["revision_instructions"] = list(idx_new[k].get("revision_instructions") or []) + [
                {"text": t, "kind": change.get("kind", "comment"), "at": now_ms()}]
        direct.add(k)
    for op in ops:
        o = op.get("op")
        if o == "add_node":
            direct.add(next(n["node_key"] for n in new if n["node_key"] not in old_keys))
        elif o in ("update_node", "set_agent", "set_model_tier", "set_model"):
            direct.add(op["node_key"])
        elif o in ("add_edge", "remove_edge"):
            direct.add(op["to"])
        elif o == "remove_node":
            direct |= {n["node_key"] for n in old if op["node_key"] in n["depends_on"]} & set(idx_new)
    direct &= set(idx_new)
    affected = set(direct) if change.get("scope") == "node_only" else direct | graph.descendants(new, direct)
    # v1.8：用户手工编辑过成果的步骤，重跑时以用户编辑稿为基础（清理已被更新编辑取代的旧指令，保证模型
    # 始终看到用户当前的修改）——「修改之后再进行新的对话，AI 也读得懂」。
    edits = artifacts.active_edits(task["id"])
    for nd in new:
        kept = [i for i in (nd.get("revision_instructions") or [])
                if not (isinstance(i, dict) and i.get("kind") == "user_edit")]
        e = edits.get(nd["node_key"])
        if e:
            kept.append({"text": artifacts.USER_EDIT_INSTRUCTION.format(content=clip(e["content"], 3000)),
                         "kind": "user_edit", "at": e["created_at"]})
        nd["revision_instructions"] = kept
    old_by = graph.index(old)
    reused = [k for k in idx_new if k not in affected and k in old_by and old_by[k]["status"] == "succeeded"]
    carried = [k for k in idx_new if k not in affected and k in old_by and old_by[k]["status"] in ("running", "awaiting_approval")]
    est = sum(plans.node_cost(idx_new[k]) for k in affected)
    return {"pv": pv, "old": old, "new": new, "affected": sorted(affected, key=[n["node_key"] for n in new].index),
            "reused": reused, "carried": carried,
            "added": sorted(set(idx_new) - old_keys), "removed": sorted(old_keys - set(idx_new)),
            "estimate_cost_cny": round(est, 4)}


def preview(task: dict, change: dict) -> dict:
    r = _compute(task, change)
    titles = {n["node_key"]: n["title"] for n in r["new"]} | {n["node_key"]: n["title"] for n in r["old"]}
    return {k: r[k] for k in ("affected", "reused", "carried", "added", "removed", "estimate_cost_cny")} | {"titles": titles}


@db.atomic
def create(task: dict, change: dict, reason: str) -> dict:
    # 所有修订入口（评论、退回、回答关注项、重写章节、重跑）都经过这里，统一做状态检查
    if task["status"] not in ("running", "completed", "failed"):
        raise ApiError(409, "STATE_CONFLICT", "当前状态不能修订（任务已取消或计划尚未确认）")
    r = _compute(task, change)
    pv, old_by, aff = r["pv"], graph.index(r["old"]), set(r["affected"])
    est = plans.annotate(r["new"], task["max_concurrency"], task)
    newv = pv["version"] + 1
    cancel_ids: set[str] = set()
    adopt: list[tuple[str, str]] = []
    with db.Tx():
        db.update("plan_versions", {"id": pv["id"]}, {"status": "superseded"})
        npv = {"id": new_id("pv"), "task_id": task["id"], "version": newv, "status": "active", "reason": reason,
               "deliverable": db.j(pv["deliverable"], {}), "optimizations": [], "created_at": now_ms()}
        db.insert("plan_versions", npv)
        for n in r["new"]:
            nid = new_id("nd")
            o = old_by.get(n["node_key"])
            if n["node_key"] in aff or not o:
                n.update(status="pending", output=None, execution_id=None, input_hash=None, attempts=0, spec_state=None,
                         approval_status="none", error=None, error_code=None, started_at=None, finished_at=None)
                if o and o["status"] == "running":
                    cancel_ids.add(o["id"])
            else:                                             # 沿用：状态、结果、审批一并带入新版本
                for f in ("status", "output", "execution_id", "input_hash", "attempts", "approval_status",
                          "started_at", "finished_at", "error", "error_code"):
                    n[f] = o.get(f)
                n["spec_state"] = None
                if o["status"] == "running":
                    adopt.append((o["id"], nid))
            store.insert_node(npv, n, nid)
            if n.get("status") == "awaiting_approval":
                # 安全防护的等待随新版本延续：待批准的具体操作（工具/参数/哈希）一并复制，
                # 否则新版本里的批准记录对不上工具，批准后仍会被拦。
                old_ap = db.q1("SELECT * FROM approvals WHERE node_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                               (o["id"],)) if o else None
                if old_ap:
                    db.insert("approvals", {"id": new_id("apv"), "task_id": task["id"], "node_id": nid, "node_key": n["node_key"],
                                            "plan_version": newv, "status": "pending", "kind": old_ap["kind"], "created_at": now_ms(),
                                            "tool": old_ap["tool"], "args": old_ap["args"], "args_hash": old_ap["args_hash"]})
                else:                                         # 找不到待批准操作：让这一步重新排队，不留僵尸等待
                    store.set_node(nid, status="pending", approval_status="none")
        db.ex("UPDATE approvals SET status='superseded' WHERE task_id=? AND plan_version=? AND status='pending'", (task["id"], pv["version"]))
        upd = {"active_version": newv, "updated_at": now_ms(), "est_makespan_s": est["makespan_s"], "est_serial_s": est["serial_s"]}
        if task["status"] in ("completed", "failed"):
            upd.update(status="running", completed_at=None, run_started_at=now_ms())
        db.update("tasks", {"id": task["id"]}, upd)
    # 进行中执行：被沿用的继续，结果同时写回新节点；受影响的取消
    for old_id, nid in adopt:
        for eid, ad in list(RUNNER.adopters.items()):
            if old_id in ad:
                RUNNER.attach_adopter(eid, nid)
    RUNNER.cancel_executions_for(cancel_ids)
    RUNNER.discard_all_speculative(task["id"], pv["version"])
    events.emit(task["id"], "plan.version_created", {"version": newv, "reason": reason, "affected": r["affected"],
                                                     "reused": r["reused"], "added": r["added"], "removed": r["removed"]},
                plan_version=newv)
    RUNNER.kick(task["id"])
    return {"version": newv, "affected": r["affected"], "reused": r["reused"], "added": r["added"], "removed": r["removed"]}


# ---------------- 上层入口 ----------------
def apply_comments(task: dict, user_id: str, comment_ids: list[str] | None) -> dict:
    if comment_ids is not None and not isinstance(comment_ids, list):
        raise ApiError(400, "FIELD_INVALID", "comment_ids 应为数组")
    rows = db.q("SELECT * FROM comments WHERE task_id=? AND status='open'", (task["id"],))
    pv = store.active_pv(task)
    live = {n["node_key"] for n in store.load_nodes(pv["id"])} if pv else set()
    # 只应用仍存在于当前计划中的步骤上的评论；步骤已被删除的评论保持 open，由用户自行删除
    rows = [c for c in rows if (not comment_ids or c["id"] in comment_ids) and c["node_key"] in live]
    if not rows:
        raise ApiError(400, "NOTHING_TO_APPLY", "没有可应用的评论")
    ins: dict[str, list[str]] = {}
    for c in rows:
        ins.setdefault(c["node_key"], []).append(c["body"])
    res = create(task, {"instructions": ins, "kind": "comment"}, "comment")
    for c in rows:
        db.update("comments", {"id": c["id"]}, {"status": "applied", "applied_version": res["version"]})
        _maybe_suggest_preference(user_id, c["body"])
    return res


def _maybe_suggest_preference(user_id: str, body: str) -> None:
    """F20：评论里出现长期性的表述时生成「建议偏好」，必须由用户确认才生效（INV-14）。"""
    import re
    if re.search(r"以后|每次|总是|一律|always|prefer|from now on", body, re.I):
        db.insert("preferences", {"id": new_id("pref"), "user_id": user_id, "statement": body[:200], "scope": "global",
                                  "status": "suggested", "source": "comment", "created_at": now_ms()})


def reject(task: dict, approval_id: str, user_id: str, reason: str) -> dict:
    a = db.q1("SELECT * FROM approvals WHERE id=? AND task_id=?", (approval_id, task["id"]))
    if not a or a["status"] != "pending":
        raise ApiError(409, "STATE_CONFLICT", "该审批已处理或已失效")
    if a.get("kind") != "tool":
        raise ApiError(409, "STATE_CONFLICT", "该审批类型已退出流程，无需处理")
    # 安全防护的拒绝：不创建新版本、不重跑这个步骤 —— 直接把这个危险操作挡掉并让步骤失败
    if a["plan_version"] != task["active_version"]:
        raise ApiError(409, "STATE_CONFLICT", "该审批属于旧版本，已失效")
    return RUNNER.deny_tool(task, a, user_id, reason)


def rewrite_section(task: dict, section_key: str, instructions: str) -> dict:
    pv = store.active_pv(task)
    n = store.node_by_key(pv["id"], section_key) if pv else None
    if not n or not n["produces_sections"]:
        raise ApiError(404, "NOT_FOUND", "章节不存在")
    res = create(task, {"instructions": {section_key: [instructions or "请重写这一节"]}, "kind": "section", "scope": "node_only"},
                 "section_rewrite")
    return res


def answer_attention(task: dict, aid: str, answer: str) -> dict:
    a = db.q1("SELECT * FROM attention_items WHERE id=? AND task_id=?", (aid, task["id"]))
    if not a or a["kind"] != "open_question" or a["status"] != "open":
        raise ApiError(409, "STATE_CONFLICT", "该关注项不能回答")
    db.update("attention_items", {"id": aid}, {"status": "answered", "answer": answer})
    return create(task, {"instructions": {a["node_key"]: [f"用户回答「{a['title']}」：{answer}"]}, "kind": "answer"}, "attention_answer")


def rerun(task: dict, node_key: str) -> dict:
    return create(task, {"instructions": {node_key: ["重新生成"]}, "kind": "rerun"}, "manual")
