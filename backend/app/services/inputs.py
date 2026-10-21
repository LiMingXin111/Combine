"""Canonical effective inputs shared by prompts, reuse hashes and privacy checks."""
from __future__ import annotations

from .. import db


def effective_upstream(task: dict, upstream: list[dict]) -> list[dict]:
    from . import artifacts
    edits = artifacts.active_edits(task["id"])
    result = []
    for item in upstream:
        current = dict(item)
        edit = edits.get(item.get("node_key"))
        edited = bool(edit and (not item.get("content") or edit["created_at"] > (item.get("finished_at") or 0)))
        if edited:
            current["content"] = edit["content"]
        current["user_edited"] = edited
        result.append(current)
    return result


def private_context(task: dict, node: dict, upstream: list[dict]) -> bool:
    """Conservative: attachments, user edits, KB tools and inherited private data are private."""
    return bool(task.get("attachments") or task.get("private_context")
                or set(node.get("tools") or []) & {"kb.search", "reference.read"}
                or any(u.get("private_context") or u.get("citations") or u.get("user_edited") for u in upstream)
                or any(isinstance(i, dict) and i.get("kind") == "user_edit"
                       for i in node.get("revision_instructions") or []))


def from_nodes(node: dict, by: dict) -> list[dict]:
    result = []
    for key in node["depends_on"]:
        parent = by[key]
        out = parent.get("output") or {}
        result.append({"node_key": key, "title": parent.get("title", key), "finished_at": parent.get("finished_at"),
                       "content": out.get("content", ""), "summary": out.get("summary", ""),
                       "claims": out.get("claims", []), "citations": out.get("citations", []),
                       "sources": out.get("sources", []),
                       "demo": out.get("demo", False),
                       "private_context": out.get("private_context", False) or
                           bool(set(parent.get("tools") or []) & {"kb.search", "reference.read"}),
                       "data": {k: v for k, v in out.items() if k in
                                ("chart", "map", "table", "metrics", "list", "code", "timeline", "calendar", "status")}})
    return result
