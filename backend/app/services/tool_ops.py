"""Durable receipts for side effects. An uncertain remote outcome is never blindly replayed."""
from __future__ import annotations

import asyncio

from .. import db
from ..util import now_ms, stable_hash


async def perform(task_id, version, node_key, tool, args, invoke):
    key = stable_hash([task_id, version, node_key, tool, args])
    old = db.q1("SELECT * FROM tool_operations WHERE id=?", (key,))
    if old:
        if old["status"] == "succeeded":
            return db.j(old["result"], {})
        return {"error": "OPERATION_UNCERTAIN", "message": "该操作先前已发起，结果尚未确认；为避免重复执行，本次未重放"}
    db.insert("tool_operations", {"id": key, "task_id": task_id, "node_key": node_key,
                                  "tool": tool, "status": "running", "created_at": now_ms()})
    try:
        result = await invoke()
    except BaseException:
        db.update("tool_operations", {"id": key}, {"status": "uncertain"})
        raise
    status = "uncertain" if isinstance(result, dict) and result.get("error") else "succeeded"
    db.update("tool_operations", {"id": key}, {"status": status, "result": result})
    return result
