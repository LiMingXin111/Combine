"""外部 Agent 接口（v3.0 M1，prd/08 §4.2）。

契约要点：
- 凭据只在 connect 时上行一次，落库即加密；任何响应都不回明文（只回掩码 cred_hint）；
- 长网络动作（握手 / 批检）放工作线程，数据库读写始终留在事件循环线程；
- 试用调用（/call）在 M1 只放行只读工具 —— 写 / 删除类请走任务执行（那里有审批闸门与审计）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .. import db
from ..services import agents
from ..util import ApiError, clip
from .common import body, current_user

router = APIRouter(prefix="/api/agents")


def _require(user_id: str, key: str) -> dict:
    row = agents.get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    return row


@router.get("/catalog")
async def catalog(request: Request, domain: str = "", q: str = ""):
    u = current_user(request)
    mine = {r["key"]: r for r in agents.installed(u["id"])}
    q = clip(q.strip().lower(), 60)
    items = []
    for e in agents.entries():
        if domain and e.get("domain") != domain:
            continue
        if q and q not in (e.get("name", "") + e.get("name_en", "") + e.get("key", "") + e.get("description", "")).lower():
            continue
        row = mine.get(e["key"])
        items.append({**e,
                      "installed": bool(row),
                      "status": row["status"] if row else "none",
                      "enabled": bool(row["enabled"]) if row else False,
                      "tool_count": row["tool_count"] if row else 0})
    return {"version": agents.catalog().get("version", ""), "domains": agents.domains(), "items": items,
            "count": len(items), "installed_count": len(mine)}


@router.get("")
async def list_installed(request: Request):
    u = current_user(request)
    return {"items": [agents.public(r) for r in agents.installed(u["id"])]}


@router.post("")
async def install(request: Request):
    u = current_user(request)
    d = await body(request)
    key = d.get("key") if isinstance(d.get("key"), str) else None
    custom = d.get("custom") if isinstance(d.get("custom"), dict) else None
    row = agents.install(u["id"], key, custom)
    return {"agent": row}


@router.post("/{key}/connect")
async def connect(request: Request, key: str):
    u = current_user(request)
    d = await body(request)
    values = d.get("values") if isinstance(d.get("values"), dict) else {}
    rt = agents.resolve_runtime(u["id"], key, values)          # 只读 + 校验（缺字段在这里 400）
    probe = await asyncio.to_thread(agents.probe_runtime, rt)
    return agents.apply_connect(u["id"], key, rt["values"], probe)


@router.post("/{key}/refresh")
async def refresh(request: Request, key: str):
    u = current_user(request)
    _require(u["id"], key)
    rt = agents.resolve_runtime(u["id"], key, None)
    probe = await asyncio.to_thread(agents.probe_runtime, rt)
    if probe.get("error") or not isinstance(probe.get("result"), dict):
        return agents.mark_error(u["id"], key, probe)
    return agents.apply_connect(u["id"], key, {}, probe)


@router.patch("/{key}")
async def patch(request: Request, key: str):
    u = current_user(request)
    d = await body(request)
    changes = {k: d[k] for k in ("enabled", "allow_write") if k in d}
    return {"agent": agents.patch(u["id"], key, changes)}


@router.delete("/{key}")
async def remove(request: Request, key: str):
    u = current_user(request)
    agents.remove(u["id"], key)
    return {"ok": True}


@router.post("/health")
async def health(request: Request):
    u = current_user(request)
    targets = agents.health_targets(u["id"])
    results = await asyncio.to_thread(agents.health_probe_all, targets)
    return {"items": agents.health_apply(u["id"], results)}


@router.post("/{key}/call")
async def call_tool(request: Request, key: str):
    """试用调用（M1 只放行只读工具）：验证凭据可用、结果即时可见。"""
    u = current_user(request)
    _require(u["id"], key)
    d = await body(request)
    tool = str(d.get("tool") or "").strip()[:120]
    if not tool:
        raise ApiError(400, "FIELD_REQUIRED", "缺少字段 tool")
    full = tool if tool.startswith("mcp.") else f"mcp.{agents.server_of(key)}.{tool}"
    t = agents.tool_index(u["id"]).get(full)
    if not t:
        raise ApiError(404, "TOOL_UNKNOWN", "该工具不在已连接列表里（试试先刷新）")
    if t["risk"] != "read":
        raise ApiError(400, "AGENT_WRITE_BLOCKED", "写 / 删除类工具不能用试用通道：请在任务里使用（执行时走审批闸门）")
    args = d.get("args") if isinstance(d.get("args"), dict) else {}
    out = await agents.call_async(u["id"], full, args)
    return {"result": out}


@router.get("/calls")
async def recent_calls(request: Request, limit: int = 20):
    u = current_user(request)
    limit = max(1, min(int(limit or 20), 100))
    rows = db.q("SELECT agent_key, tool, status, ms, result_bytes, error, created_at FROM agent_calls "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (u["id"], limit))
    return {"items": rows}
