"""任务 / 计划版本 / 节点的读写助手。节点在内存中以「已解码的 dict」表示。"""
from __future__ import annotations

from .. import db
from ..util import ApiError

JSON_COLS = ("acceptance", "depends_on", "input_refs", "produces_sections", "capability_requirements", "agent",
             "agents", "agent_needs", "tools", "redundant_edges", "revision_instructions", "output")
NODE_COPY_COLS = ("node_key", "title", "description", "acceptance", "depends_on", "input_refs", "produces_sections",
                  "capability_requirements", "agent", "agents", "agent_needs", "tools", "model_tier", "model_override",
                  "requires_approval", "est_complexity", "est_duration_s", "est_cost_cny", "rank", "on_critical_path",
                  "redundant_edges", "revision_instructions")


def decode_node(r: dict) -> dict:
    for c in JSON_COLS:
        default = {} if c in ("capability_requirements", "agent") else ([] if c != "output" else None)
        if c == "agents":
            default = None                                   # NULL 表示该节点无外部 Agent 需求（v3.0 M2）
        r[c] = db.j(r.get(c), default)
    r["requires_approval"] = bool(r["requires_approval"])
    return r


def get_task(task_id: str, user_id: str | None = None) -> dict:
    t = db.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t or (user_id is not None and t["user_id"] != user_id):     # INV-08：他人任务一律 404
        raise ApiError(404, "NOT_FOUND", "任务不存在")
    t["attachments"] = db.j(t["attachments"], [])
    t["settings_snapshot"] = db.j(t["settings_snapshot"], {})
    return t


def get_pv(task_id: str, version: int) -> dict | None:
    return db.q1("SELECT * FROM plan_versions WHERE task_id=? AND version=?", (task_id, version))


def draft_pv(task_id: str) -> dict | None:
    return db.q1("SELECT * FROM plan_versions WHERE task_id=? AND status='draft' ORDER BY version DESC LIMIT 1", (task_id,))


def active_pv(task: dict) -> dict | None:
    if task.get("active_version") is None:
        return None
    return get_pv(task["id"], task["active_version"])


def load_nodes(pv_id: str) -> list[dict]:
    return [decode_node(r) for r in db.q("SELECT * FROM nodes WHERE plan_version_id=? ORDER BY rowid", (pv_id,))]


def node_by_key(pv_id: str, key: str) -> dict | None:
    r = db.q1("SELECT * FROM nodes WHERE plan_version_id=? AND node_key=?", (pv_id, key))
    return decode_node(r) if r else None


def node_by_id(nid: str) -> dict | None:
    r = db.q1("SELECT * FROM nodes WHERE id=?", (nid,))
    return decode_node(r) if r else None


def set_node(nid: str, **values) -> None:
    db.update("nodes", {"id": nid}, values)


def insert_node(pv: dict, n: dict, node_id: str) -> None:
    row = {"id": node_id, "plan_version_id": pv["id"], "task_id": pv["task_id"]}
    for c in NODE_COPY_COLS:
        if c in n:
            row[c] = n[c]
    for c in ("status", "input_hash", "execution_id", "output", "attempts", "approval_status", "error_code",
              "error", "spec_state", "started_at", "finished_at"):
        if c in n and n[c] is not None:
            row[c] = n[c]
    db.insert("nodes", row)
