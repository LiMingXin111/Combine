#!/usr/bin/env python3
"""内置演示 MCP 服务器（stdio 传输，换行分隔的 JSON-RPC 2.0）。

用途：
- 用户零配置验证「连接外部 Agent → 任务调用 → 结果回传」的完整链路（不需要联网、不需要 npx）；
- 测试夹具：tests/test_agents.py 用它做端到端连接/调用验证。

协议子集：initialize / notifications/initialized / tools/list / tools/call / ping。
仅标准库，任何 Python 3.10+ 均可运行：python mcp_demo_server.py
"""
from __future__ import annotations

import ast
import json
import operator
import sys
from datetime import datetime, timedelta, timezone

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "combine-demo", "version": "1.0.0"}

TOOLS = [
    {
        "name": "now",
        "description": "返回当前日期与时间（可指定 IANA 时区偏移，默认本机时区）。演示「时效事实」类调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"tz_offset_hours": {"type": "number", "description": "相对 UTC 的小时偏移，例如 8 表示北京时间"}},
        },
    },
    {
        "name": "calc",
        "description": "对一段四则运算表达式求值（+ - * / // % ** 与括号）。",
        "inputSchema": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "例如 (12.5 * 3 + 7) / 2"}},
            "required": ["expression"],
        },
    },
    {
        "name": "echo",
        "description": "原样回显输入文本（连通性验证用）。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "recent_days",
        "description": "返回最近 N 天的日期列表（用于日程/时间线类任务的演示数据）。",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "1–30，默认 7"}},
        },
    },
]

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}


def _calc(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError("表达式包含不支持的语法")
    return ev(ast.parse(expr, mode="eval"))


def _call(name: str, args: dict) -> dict:
    if name == "now":
        off = args.get("tz_offset_hours")
        tz = timezone(timedelta(hours=float(off))) if isinstance(off, (int, float)) else None
        t = datetime.now(tz) if tz else datetime.now().astimezone()
        return {"text": t.strftime("%Y-%m-%d %H:%M:%S %z")}
    if name == "calc":
        return {"text": str(_calc(str(args.get("expression") or "0")))}
    if name == "echo":
        return {"text": str(args.get("text") or "")}
    if name == "recent_days":
        n = max(1, min(int(args.get("days") or 7), 30))
        today = datetime.now().date()
        return {"text": json.dumps([(today - timedelta(days=i)).isoformat() for i in range(n)], ensure_ascii=False)}
    raise KeyError(name)


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                                   # 通知（如 notifications/initialized）不需要回包
        return None
    if method == "initialize":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            out = _call(str(params.get("name") or ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": out["text"]}]}}
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": f"未知工具：{params.get('name')}"}]}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": f"执行失败：{e}"}]}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = handle(msg)
        except json.JSONDecodeError:
            continue
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
