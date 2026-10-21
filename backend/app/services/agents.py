"""外部 Agent 接入服务（v3.0 M1 · L1 MCP 工具服务器，prd/08 §4）。

职责：
- 目录：读策展目录 catalog/agents.json（12 域 46 条），安装到用户空间（agents 表）；
- 连接：凭据（AESGCM 加密，AAD=user|agent_key）→ 解析运行参数 → MCP 握手（initialize + tools/list）→ 缓存工具清单；
- 精选工具集：目录策展 ≤12 个工具，与真实 tools/list 求交；全部对不上时回退「优先只读 ≤12 个」并标注 note；
- 执行：tools/call（stdio / HTTP 双传输）→ 结果规范化 + agent_calls 审计账本（不存参数明文）；
- 安全：http 端点走公网 SSRF 校验（复用 websearch._public_host）；写类工具默认不出（allow_write 开关）；社区级写操作强制审批（policy 判定）。

线程规约：所有数据库访问都在事件循环线程；`probe_runtime` 是纯网络步骤（可放 asyncio.to_thread），
`resolve_runtime` 只读 DB + 计算，`apply_*` 只写 DB。`connect()` / `refresh()` 是同步便捷版（测试与脚本用）。
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config, db, security
from ..util import ApiError, clip, new_id, now_ms, stable_hash
from . import workspace
from .process_env import clean_env
from . import tool_args

CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog" / "agents.json"
DEMO_SERVER = Path(__file__).resolve().parent.parent / "mcp_demo_server.py"

CONNECT_TIMEOUT = 180          # 首次连接：npx / uvx 要下载依赖，必须宽裕
CALL_TIMEOUT = 90              # 单次工具调用
MAX_TOOLSET = 12               # 精选工具集上限（设计硬约束）
MAX_RESULT_CHARS = 8000        # 单次调用结果回传模型的字符上限
MAX_AGENTS_FOR_PROMPT = 5      # 规划提示词里的已连接 Agent 上限
MAX_TOOLS_FOR_PROMPT = 8       # 每 Agent 提示词中展示的工具名上限
# v3.0 M2 决策引擎的硬上限（prd/08 §5.4 两级暴露）
MAX_CATALOG_DIGEST_CHARS = 900  # 规划提示词里的目录摘要字符上限（12 域 × ≤3 条，≈600 tokens）
MIN_COVERAGE = 0.12             # 漏斗二噪声控制阈值：能力覆盖率低于此值的条目不提（prd/08 §5.3）
MAX_SERVERS_PER_NODE = 2        # 单节点挂载的外部 server 上限
MAX_TOOLS_PER_NODE = 24         # 单节点挂载的外部工具 schema 上限
MAX_SERVERS_PER_TASK = 5        # 单任务的外部 server 上限（超出的按 primary 评分淘汰）
_TRUST_SCORE = {"official": 1.0, "verified": 0.85, "local": 0.7, "community": 0.6}
_COST_SCORE = {"free": 1.0, "low": 0.7, "mid": 0.7, "medium": 0.7, "high": 0.4, "usage": 0.4}

_UA = "Combine/3.0 (+local productivity agent; MCP client)"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_catalog_cache: dict = {"mtime": 0.0, "data": None}


# ==================== 目录 ====================
def catalog() -> dict:
    try:
        m = CATALOG_PATH.stat().st_mtime
    except OSError:
        m = 0.0
    if _catalog_cache["data"] is None or _catalog_cache["mtime"] != m:
        try:
            data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"version": "", "domains": [], "entries": []}
        _catalog_cache.update(mtime=m, data=data)
    return _catalog_cache["data"]


def entries() -> list[dict]:
    return catalog().get("entries") or []


def domains() -> list[dict]:
    return catalog().get("domains") or []


def index() -> dict[str, dict]:
    return {e["key"]: e for e in entries() if e.get("key")}


# ==================== 行读取 / 展示 ====================
def server_of(agent_key: str) -> str:
    return agent_key.split(".", 1)[1] if "." in agent_key else agent_key


def fn_name(agent_key: str, tool: str) -> str:
    """模型侧函数名：只允许 [A-Za-z0-9_] 且 ≤64 字符；反向映射靠缓存里的 fn 字段，不做字符串还原。"""
    raw = f"mcp__{server_of(agent_key)}__{tool}"
    s = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    if len(s) <= 64:
        return s
    return s[:55] + "_" + hashlib.sha1(s.encode()).hexdigest()[:8]


def _parse_row(row: dict | None) -> dict | None:
    if not row:
        return None
    for k in ("tools", "toolset", "args", "headers"):
        row[k] = db.j(row[k], [] if k != "headers" else {})
    return row


def get(user_id: str, key: str) -> dict | None:
    return _parse_row(db.q1("SELECT * FROM agents WHERE user_id=? AND key=?", (user_id, key)))


def installed(user_id: str) -> list[dict]:
    return [_parse_row(r) for r in db.q("SELECT * FROM agents WHERE user_id=? ORDER BY created_at", (user_id,))]


def public(row: dict) -> dict:
    """接口输出：不含任何凭据、不含工具 schema（schema 只进模型调用，不下发展示层）。"""
    tools = [{k: t.get(k) for k in ("name", "desc", "risk")} for t in row.get("tools") or []]
    return {k: row.get(k) for k in (
        "id", "key", "name", "name_en", "kind", "domain", "description", "description_en",
        "transport", "endpoint", "command", "auth", "trust", "cost", "latency", "docs",
        "tool_count", "status", "error", "note", "cred_hint", "enabled", "allow_write",
        "verified_at", "source", "builtin")} | {"tools": tools, "args": row.get("args") or []}


def _fields_for(row: dict) -> list[dict]:
    if row.get("source") == "catalog":
        e = index().get(row["key"]) or {}
        return e.get("fields") or []
    return []


def _auth_ready(row: dict) -> bool:
    """oauth 类在 M1 尚未开放（M4 做授权流）。"""
    return row.get("auth") != "oauth"


# ==================== 安装 / 卸载 ====================
def install(user_id: str, key: str | None = None, custom: dict | None = None) -> dict:
    if key:
        e = index().get(key)
        if not e:
            raise ApiError(404, "AGENT_UNKNOWN", "目录中没有这个 Agent")
        exist = get(user_id, key)
        if exist:
            return public(exist)
        row = {"id": new_id("agt"), "user_id": user_id, "key": key,
               "name": e.get("name") or key, "name_en": e.get("name_en") or key, "kind": "mcp",
               "domain": e.get("domain") or "knowledge",
               "description": e.get("description") or "", "description_en": e.get("description_en") or "",
               "transport": e.get("transport") or "stdio", "endpoint": e.get("endpoint") or "",
               "command": e.get("command") or "", "args": e.get("args") or [], "headers": {},
               "auth": e.get("auth") or "none", "trust": e.get("trust") or "community",
               "cost": e.get("cost") or "free", "latency": e.get("latency") or "fast",
               "docs": e.get("docs") or "", "toolset": e.get("toolset") or [], "tools": [],
               "tool_count": 0, "status": "disconnected", "error": "", "cred_hint": "", "note": "",
               "enabled": 1, "allow_write": 0, "verified_at": e.get("verified_at") or "",
               "source": "catalog", "builtin": e.get("builtin") or "",
               "created_at": now_ms(), "updated_at": now_ms()}
        db.insert("agents", row)
        return public(_parse_row(row))
    if custom is not None:
        name = str(custom.get("name") or "").strip()[:60]
        if not name:
            raise ApiError(400, "AGENT_INVALID", "自定义 Agent 需要 name")
        kind = str(custom.get("kind") or "mcp").strip().lower()
        if kind not in ("mcp", "a2a", "cli"):
            raise ApiError(400, "AGENT_INVALID", "kind 只支持 mcp / a2a / cli")
        command = str(custom.get("command") or "").strip()[:200]
        endpoint = str(custom.get("endpoint") or "").strip()[:500]
        args = [clip(str(a), 300) for a in (custom.get("args") or []) if str(a).strip()][:20]
        transport = str(custom.get("transport") or "stdio").strip().lower()
        if kind == "mcp":                                    # L1：MCP 工具服务器（stdio / http）
            if transport not in ("stdio", "http"):
                raise ApiError(400, "AGENT_INVALID", "transport 只支持 stdio 或 http")
            if transport == "stdio" and not command:
                raise ApiError(400, "AGENT_INVALID", "stdio 接入需要 command（例如 npx / uvx / python）")
            if transport == "http":
                p = urllib.parse.urlsplit(endpoint)
                if p.scheme not in ("http", "https") or not p.hostname:
                    raise ApiError(400, "AGENT_INVALID", "http 接入需要合法的 http(s) endpoint")
            if config.DEPLOY_MODE != "self_hosted" and transport == "stdio":
                raise ApiError(400, "AGENT_LOCAL_ONLY", "云端版只能接入远程（http）Agent")
            trust, prefix = ("local" if transport == "stdio" else "community"), "mcp.custom-"
        elif kind == "a2a":                                  # L2：A2A 外部 Agent（agent card 发现 + 委派）
            p = urllib.parse.urlsplit(endpoint)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ApiError(400, "AGENT_INVALID", "A2A 接入需要 agent card 的 http(s) 地址")
            transport, trust, prefix = "agent-card", "community", "a2a.custom-"
        else:                                                # L3：本地 CLI Agent（自托管专享，安全最高）
            if not command:
                raise ApiError(400, "AGENT_INVALID", "CLI 接入需要 command（本机可执行命令，例如 node / python）")
            if config.DEPLOY_MODE != "self_hosted":
                raise ApiError(400, "AGENT_LOCAL_ONLY", "云端版不开放本地 CLI 接入")
            transport, trust, prefix = "local", "local", "cli.custom-"
        key = prefix + new_id("c")[-6:]
        row = {"id": new_id("agt"), "user_id": user_id, "key": key, "name": name, "name_en": name,
               "kind": kind, "domain": clip(str(custom.get("domain") or "knowledge"), 20),
               "description": clip(str(custom.get("description") or "自定义接入"), 120), "description_en": "",
               "transport": transport, "endpoint": endpoint, "command": command, "args": args,
               "headers": {str(k)[:80]: str(v)[:500] for k, v in (custom.get("headers") or {}).items()},
               "auth": "none", "trust": trust,
               "cost": "free", "latency": "mid", "docs": "", "toolset": [], "tools": [],
               "tool_count": 0, "status": "disconnected", "error": "", "cred_hint": "", "note": "",
               "enabled": 1, "allow_write": 0, "verified_at": "", "source": "custom", "builtin": "",
               "created_at": now_ms(), "updated_at": now_ms()}
        db.insert("agents", row)
        # 自定义接入可在安装时带 env 凭据（加密保存）
        env = custom.get("env")
        if isinstance(env, dict) and env:
            _save_credentials(user_id, key, {str(k)[:80]: str(v)[:500] for k, v in env.items()})
            db.update("agents", {"user_id": user_id, "key": key},
                      {"cred_hint": _mask_values({str(k): str(v) for k, v in env.items()}), "updated_at": now_ms()})
        return public(get(user_id, key))
    raise ApiError(400, "AGENT_INVALID", "缺少 key 或 custom")


def disconnect(user_id: str, key: str) -> dict:
    row = get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    db.ex("DELETE FROM agent_credentials WHERE user_id=? AND agent_key=?", (user_id, key))
    db.update("agents", {"user_id": user_id, "key": key},
              {"status": "disconnected", "error": "", "note": "", "tools": [], "tool_count": 0,
               "cred_hint": "", "updated_at": now_ms()})
    return public(get(user_id, key))


def remove(user_id: str, key: str) -> None:
    row = get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    db.ex("DELETE FROM agent_credentials WHERE user_id=? AND agent_key=?", (user_id, key))
    db.ex("DELETE FROM agents WHERE user_id=? AND key=?", (user_id, key))


# ==================== 凭据 ====================
def _save_credentials(user_id: str, key: str, values: dict) -> None:
    blob = security.encrypt_secret(json.dumps(values, ensure_ascii=False), user_id, key)
    db.ex("DELETE FROM agent_credentials WHERE user_id=? AND agent_key=?", (user_id, key))
    db.insert("agent_credentials", {"id": new_id("acd"), "user_id": user_id, "agent_key": key,
                                    "payload": blob, "created_at": now_ms()})


def stored_values(user_id: str, key: str) -> dict:
    r = db.q1("SELECT payload FROM agent_credentials WHERE user_id=? AND agent_key=?", (user_id, key))
    if not r:
        return {}
    try:
        return json.loads(security.decrypt_secret(r["payload"], user_id, key))
    except Exception:                                       # 密文损坏 / AAD 不匹配 → 视为无凭据
        return {}


def _mask_values(values: dict) -> str:
    if not values:
        return ""
    k = next(iter(values))
    return f"{k}: {security.key_hint(str(values[k]))}"


# ==================== 运行参数解析 ====================
def resolve_runtime(user_id: str, key: str, values: dict | None = None) -> dict:
    """把（行配置 + 凭据）解析成可直接执行的 argv/env/headers。values=None 表示用已存凭据。"""
    row = get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    if not _auth_ready(row):
        raise ApiError(400, "AGENT_OAUTH_PENDING", "OAuth 授权连接将在后续版本开放，请先用 Token / API Key 型 Agent")
    fields = _fields_for(row)
    if values is None:
        values = stored_values(user_id, key)
    values = {str(k): str(v) for k, v in (values or {}).items() if str(v).strip()}
    for f in fields:
        if not f.get("optional") and not values.get(f.get("key")):
            raise ApiError(400, "AGENT_CRED_MISSING", f"请填写：{f.get('label') or f.get('key')}")

    ws_dir = str(workspace.root_for(user_id))
    substitutions = {"{workspace}": ws_dir, **{f"{{{k}}}": v for k, v in values.items()}}

    def sub(s: str) -> str:
        for ph, v in substitutions.items():
            if ph in s:
                s = s.replace(ph, v)
        return s

    if row["builtin"] == "demo":                            # 内置演示服务器：命令由服务端解析，禁止外部指定
        argv = [sys.executable, str(DEMO_SERVER)]
        return {"row": row, "values": values, "argv": argv, "env": clean_env(), "headers": {}, "endpoint": ""}

    if row["kind"] == "a2a":                                # v3.0 M4：A2A agent card（远程 HTTP，L2 委派）
        endpoint = sub(row["endpoint"] or "")
        headers = {str(k): str(v) for k, v in (row["headers"] or {}).items()}
        for f in fields:
            v = values.get(f.get("key"))
            if v:
                dest = str(f.get("input") or f.get("key"))
                headers[dest] = (str(f.get("prefix") or "") + v) if _looks_header(dest) else v
        return {"row": row, "values": values, "argv": [], "env": {}, "headers": headers, "endpoint": endpoint}

    if row["kind"] == "cli":                                # v3.0 M4：本地 CLI Agent（L3，自托管专享，工作区为 cwd）
        if config.DEPLOY_MODE != "self_hosted":
            raise ApiError(400, "AGENT_LOCAL_ONLY", "云端版不开放本地 CLI 接入")
        argv = [sub(row["command"] or ""), *[sub(a) for a in (row["args"] or [])]]
        env = clean_env()
        arg_text = " ".join(row["args"] or []) + " " + (row["command"] or "")
        for f in fields:
            v = values.get(f.get("key"))
            if v and f"{{{f.get('key')}}}" not in arg_text:
                env[str(f.get("input") or f.get("key"))] = v
        return {"row": row, "values": values, "argv": argv, "env": env, "headers": {},
                "endpoint": "", "cwd": ws_dir}

    if row["transport"] == "stdio":
        if config.DEPLOY_MODE != "self_hosted":
            raise ApiError(400, "AGENT_LOCAL_ONLY", "云端版不开放本地进程接入")
        argv = [sub(row["command"] or ""), *[sub(a) for a in (row["args"] or [])]]
        env = clean_env()
        arg_text = " ".join(row["args"] or []) + " " + (row["command"] or "")
        for f in fields:                                    # 已在参数里替换的字段不再塞环境变量
            v = values.get(f.get("key"))
            if v and f"{{{f.get('key')}}}" not in arg_text:
                env[str(f.get("input") or f.get("key"))] = v
        return {"row": row, "values": values, "argv": argv, "env": env, "headers": {}, "endpoint": ""}

    endpoint = sub(row["endpoint"] or "")
    headers = {str(k): str(v) for k, v in (row["headers"] or {}).items()}
    for f in fields:
        v = values.get(f.get("key"))
        if not v:
            continue
        dest = str(f.get("input") or f.get("key"))
        if f"{{{f.get('key')}}}" in (row["endpoint"] or ""):
            continue                                        # endpoint 里已替换（不常见）
        headers[dest] = (str(f.get("prefix") or "") + v) if _looks_header(dest) else v
    return {"row": row, "values": values, "argv": [], "env": {}, "headers": headers, "endpoint": endpoint}


def _looks_header(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9-]{3,80}", name)) and any(c.isupper() for c in name)


def _public_host(host: str) -> bool:
    from .websearch import _public_host as f                     # 复用 web_fetch 的 SSRF 校验（DNS 逐 IP 公网）
    return f(host)


# ==================== MCP 协议：stdio ====================
def _which(cmd: str) -> str | None:
    if not cmd:
        return None
    p = shutil.which(cmd)
    if p:
        return p
    ext = ".exe" if os.name == "nt" else ""
    if ext and shutil.which(cmd + ext):
        return shutil.which(cmd + ext)
    return None


def _kill(proc) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()                              # 多数 server 收到 EOF 后会自行退出
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=5, creationflags=_NO_WINDOW)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
    for f in (proc.stdout, proc.stderr):                    # 显式关闭管道（drain 线程已收尾，异常自吞）
        try:
            if f and not f.closed:
                f.close()
        except Exception:  # noqa: BLE001
            pass


def _read_matching(proc, mid: int) -> dict | None:
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue                                        # 容忍非 JSON 噪声行
        if isinstance(msg, dict) and msg.get("id") == mid:
            return msg


def _stdio_exchange(argv: list, env: dict, method: str, params: dict, timeout: int) -> dict:
    exe = _which(argv[0])
    if not exe:
        return {"error": "NO_CMD", "message": f"未找到命令「{argv[0]}」：请先安装 Node.js（npx）或 uv（uvx），或改用本机已有命令"}
    try:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = _NO_WINDOW
        proc = subprocess.Popen([exe, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, **kwargs)
    except OSError as e:
        return {"error": "SPAWN_FAILED", "message": clip(str(e), 200)}
    state: dict = {}
    err_buf: list[str] = []

    def drain_err() -> None:
        try:
            for line in proc.stderr:                        # 防 stderr 管满阻塞子进程
                if len(err_buf) < 40:
                    err_buf.append(line.strip())
        except Exception:  # noqa: BLE001
            pass

    def exchange() -> None:
        try:
            def send(obj: dict) -> None:
                proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                proc.stdin.flush()

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "combine", "version": config.VERSION}}})
            init = _read_matching(proc, 1)
            if not init:
                state["error"] = {"error": "BAD_PROTOCOL", "message": clip("；".join(err_buf[-6:]) or "服务未返回 initialize 响应", 300)}
                return
            if init.get("error"):
                state["error"] = {"error": "PROTOCOL_ERROR", "message": clip(str(init["error"]), 300)}
                return
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
            resp = _read_matching(proc, 2)
            if not resp:
                state["error"] = {"error": "BAD_PROTOCOL", "message": clip("；".join(err_buf[-6:]) or f"服务未返回 {method} 响应", 300)}
                return
            state["resp"] = resp
        except Exception as e:  # noqa: BLE001
            state["error"] = {"error": "IO_ERROR", "message": clip(str(e), 200)}

    t_err = threading.Thread(target=drain_err, daemon=True)
    t_err.start()
    t = threading.Thread(target=exchange, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout)
    if t.is_alive():
        _kill(proc)
        return {"error": "TIMEOUT", "message": f"服务在 {timeout}s 内未完成响应（首次运行可能需要下载依赖，稍后重试）"}
    _kill(proc)
    if "error" in state:
        out = state["error"]
        if not out.get("message"):
            out["message"] = clip("；".join(err_buf[-6:]), 300)
        return out
    out = state.get("resp") or {"error": "EMPTY", "message": "服务未返回结果"}
    out["_ms"] = int((time.time() - t0) * 1000)
    return out


# ==================== MCP 协议：HTTP（Streamable HTTP + SSE 兼容） ====================
def _http_post(url: str, headers: dict, payload: dict, timeout: int, session: dict) -> tuple[int, str, str]:
    h = {"User-Agent": _UA, "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    h.update(headers or {})
    if session.get("id"):
        h["Mcp-Session-Id"] = session["id"]
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                session["id"] = sid
            body = r.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                body = body[:4 * 1024 * 1024]
            return r.status, body.decode("utf-8", "replace"), (r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(65536).decode("utf-8", "replace"), (e.headers.get("Content-Type") or "")
        except Exception:  # noqa: BLE001
            return e.code, "", ""
    except Exception as e:  # noqa: BLE001
        return 0, "", f"#net:{clip(str(e), 160)}"


def _parse_rpc_body(status: int, body: str, ctype: str, mid: int) -> dict | None:
    if not body.strip():
        return None
    if "text/event-stream" in (ctype or "") or body.lstrip().startswith("event:") or "\ndata:" in body[:2000] or body.startswith("data:"):
        found = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == mid:
                found = msg
        return found
    try:
        msg = json.loads(body)
        return msg if isinstance(msg, dict) else None
    except ValueError:
        return None


def _http_exchange(endpoint: str, headers: dict, method: str, params: dict, timeout: int) -> dict:
    p = urllib.parse.urlsplit(endpoint)
    if p.scheme not in ("http", "https") or not p.hostname:
        return {"error": "BAD_URL", "message": "只支持 http/https 地址"}
    if not _public_host(p.hostname):
        return {"error": "BLOCKED", "message": "该地址不允许访问（仅限公网地址）"}
    session: dict = {}
    t0 = time.time()
    st, body, ctype = _http_post(endpoint, headers, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                "clientInfo": {"name": "combine", "version": config.VERSION}}},
                                 timeout, session)
    if st == 0:
        return {"error": "NETWORK", "message": clip(ctype, 200)}
    init = _parse_rpc_body(st, body, ctype, 1)
    if init is None:
        return {"error": "BAD_PROTOCOL", "message": f"服务未返回 initialize 响应（HTTP {st}）"}
    if init.get("error"):
        return {"error": "PROTOCOL_ERROR", "message": clip(str(init["error"]), 300)}
    _http_post(endpoint, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout, session)
    st2, body2, ctype2 = _http_post(endpoint, headers, {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
                                    timeout, session)
    if st2 == 0:
        return {"error": "NETWORK", "message": clip(ctype2, 200)}
    resp = _parse_rpc_body(st2, body2, ctype2, 2)
    if resp is None:
        return {"error": "BAD_PROTOCOL", "message": f"服务未返回 {method} 响应（HTTP {st2}）"}
    resp["_ms"] = int((time.time() - t0) * 1000)
    return resp


# ==================== A2A 客户端（v3.0 M4，prd/08 §4.1 L2） ====================
def _plain_get(url: str, headers: dict, timeout: int) -> tuple[int, str, str]:
    h = {"User-Agent": _UA, "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2 * 1024 * 1024).decode("utf-8", "replace"), (r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(65536).decode("utf-8", "replace"), ""
        except Exception:  # noqa: BLE001
            return e.code, "", ""
    except Exception as e:  # noqa: BLE001
        return 0, "", f"#net:{clip(str(e), 160)}"


def _plain_post(url: str, headers: dict, payload: dict, timeout: int) -> tuple[int, str, str]:
    h = {"User-Agent": _UA, "Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4 * 1024 * 1024).decode("utf-8", "replace"), (r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(65536).decode("utf-8", "replace"), ""
        except Exception:  # noqa: BLE001
            return e.code, "", ""
    except Exception as e:  # noqa: BLE001
        return 0, "", f"#net:{clip(str(e), 160)}"


def _a2a_candidates(endpoint: str) -> list[str]:
    """A2A 发现地址候选：用户可直接给 agent card URL，或给 base URL（自动补 well-known 路径）。"""
    ep = (endpoint or "").strip()
    if ep.endswith(".json"):
        return [ep]
    base = ep.rstrip("/")
    return [base + "/.well-known/agent.json", base + "/.well-known/agent-card.json"]


def _a2a_discover(endpoint: str, headers: dict, timeout: int) -> dict:
    """A2A agent card 发现 → 映射为 MCP 风格的 tools/list 结果（skills → pseudo-tools）。"""
    card, last = None, ""
    for u in _a2a_candidates(endpoint)[:2]:
        p = urllib.parse.urlsplit(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            last = "只支持 http/https 地址"
            continue
        if not _public_host(p.hostname):
            return {"error": "BLOCKED", "message": "该地址不允许访问（仅限公网地址）"}
        st, body, ctype = _plain_get(u, headers, timeout)
        if st == 0:
            last = clip(ctype, 200)
            continue
        try:
            obj = json.loads(body)
        except ValueError:
            last = f"agent card 不是合法 JSON（HTTP {st}）"
            continue
        if isinstance(obj, dict) and (obj.get("skills") is not None or obj.get("name")):
            card = obj
            break
        last = f"响应里没有 agent card 结构（HTTP {st}）"
    if card is None:
        return {"error": "AGENT_CARD", "message": last or "未获取到 agent card"}
    tools = []
    for s in (card.get("skills") or [])[:MAX_TOOLSET]:
        if not isinstance(s, dict):
            continue
        tid = re.sub(r"[^A-Za-z0-9_-]", "-", str(s.get("id") or s.get("name") or "")).strip("-")[:40]
        if not tid:
            continue
        tools.append({"name": tid,
                      "description": clip(str(s.get("description") or s.get("name") or ""), 200),
                      "risk": "read",
                      "inputSchema": {"type": "object",
                                      "properties": {"message": {"type": "string", "description": "the task to delegate"}},
                                      "required": ["message"]}})
    if not tools:                                            # 无 skills：暴露一个通用委派入口
        tools = [{"name": "delegate", "description": clip(str(card.get("description") or "Delegate one task to this A2A agent"), 200),
                  "risk": "read",
                  "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}]
    return {"result": {"tools": tools, "_card": {"name": card.get("name") or "", "description": card.get("description") or ""}}}


def _a2a_texts(obj: dict) -> list[str]:
    """从 A2A 响应（Task / Message）里抽取文本部件。"""
    texts: list[str] = []
    for part in (obj.get("parts") or []):
        if isinstance(part, dict) and str(part.get("kind") or part.get("type")) in ("text", "text/plain"):
            texts.append(str(part.get("text") or ""))
    for art in (obj.get("artifacts") or []):
        if isinstance(art, dict):
            texts += _a2a_texts(art)
    st = obj.get("status")
    if isinstance(st, dict) and isinstance(st.get("message"), dict):
        texts += _a2a_texts(st["message"])
    return texts


def _a2a_send(endpoint: str, headers: dict, params: dict, timeout: int) -> dict:
    """A2A message/send：把一次调用委派给外部 agent（文本任务 → 文本结果）。"""
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    text = str(args.get("message") or args.get("prompt") or json.dumps(args, ensure_ascii=False) or "")
    if not text.strip():
        return {"error": "ARGS_INVALID", "message": "A2A 委派需要一个 message（任务文本）"}
    url = (endpoint or "").strip().rstrip("/")
    if url.endswith(".json"):
        url = url.rsplit("/", 1)[0]                          # card URL → 去掉文件名
        if url.endswith("/.well-known"):                     # well-known 约定：RPC 端点在站点根，不在 /.well-known 下
            url = url[: -len("/.well-known")]
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return {"error": "BAD_URL", "message": "只支持 http/https 地址"}
    if not _public_host(p.hostname):
        return {"error": "BLOCKED", "message": "该地址不允许访问（仅限公网地址）"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
               "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": text}],
                                      "messageId": new_id("msg")}}}
    t0 = time.time()
    st, body, ctype = _plain_post(url, headers, payload, timeout)
    ms = int((time.time() - t0) * 1000)
    if st == 0:
        return {"error": "NETWORK", "message": clip(ctype, 200)}
    try:
        msg = json.loads(body)
    except ValueError:
        return {"error": "BAD_PROTOCOL", "message": f"A2A 响应不是 JSON（HTTP {st}）"}
    if isinstance(msg, dict) and msg.get("error"):
        return {"error": "AGENT_ERROR", "message": clip(str(msg["error"]), 300)}
    result = (msg or {}).get("result") if isinstance(msg, dict) else None
    texts = _a2a_texts(result if isinstance(result, dict) else {})
    body_txt = "\n\n".join(t for t in texts if t and t.strip())
    if not body_txt:
        body_txt = clip(json.dumps(result if result is not None else msg, ensure_ascii=False), 2000)
    return {"result": {"content": [{"type": "text", "text": clip(body_txt, MAX_RESULT_CHARS)}]}, "_ms": ms}


# ==================== 本地 CLI Agent（v3.0 M4，prd/08 §4.1 L3，自托管专享） ====================
def _cli_tools(argv: list) -> dict:
    """CLI 接入探测：命令存在即可用；工具集固定为一个「run」（把任务提示写进 stdin，收 stdout）。"""
    exe = _which(argv[0]) if argv else None
    if not exe:
        return {"error": "NO_CMD", "message": f"未找到命令「{argv[0] if argv else ''}」：请确认已安装并在 PATH 中"}
    return {"result": {"tools": [{"name": "run",
                                  "description": "Run this local CLI agent with one task prompt (written to stdin); returns its stdout.",
                                  "risk": "console",
                                  "inputSchema": {"type": "object",
                                                  "properties": {"prompt": {"type": "string", "description": "the task for the CLI agent"}},
                                                  "required": ["prompt"]}}]}}


def _cli_run(argv: list, env: dict, cwd: str, params: dict, timeout: int) -> dict:
    """执行一次 CLI Agent 调用（单次进程，stdin 任务 → stdout 结果）。"""
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    prompt = str(args.get("prompt") or args.get("message") or "")
    if not prompt.strip():
        return {"error": "ARGS_INVALID", "message": "CLI Agent 需要一个 prompt"}
    exe = _which(argv[0]) if argv else None
    if not exe:
        return {"error": "NO_CMD", "message": f"未找到命令「{argv[0] if argv else ''}」"}
    try:
        kwargs: dict = {"cwd": cwd or None, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = _NO_WINDOW
        proc = subprocess.run([exe, *argv[1:]], input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=min(timeout, 120), **kwargs)
    except subprocess.TimeoutExpired:
        return {"error": "TIMEOUT", "message": f"CLI Agent 在 {min(timeout, 120)}s 内未完成"}
    except OSError as e:
        return {"error": "SPAWN_FAILED", "message": clip(str(e), 200)}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return {"error": "CLI_FAILED", "message": clip(err or f"退出码 {proc.returncode}", 300)}
    text = out or err
    return {"result": {"content": [{"type": "text", "text": clip(text, MAX_RESULT_CHARS)}]}}


# ==================== 探测 / 工具集 ====================
def probe_runtime(rt: dict, timeout: int = CONNECT_TIMEOUT, method: str = "tools/list", params: dict | None = None) -> dict:
    """纯网络 / 子进程步骤（可放线程执行）：按接入类型分派，统一返回 MCP 形状（result.tools / result.content）。

    - mcp：初始化握手 + 一次方法调用（stdio / Streamable HTTP）；
    - a2a：tools/list → agent card 发现；tools/call → message/send 委派；
    - cli：tools/list → 命令存在性检查；tools/call → 单次进程执行（stdin 任务 → stdout 结果）。
    """
    kind = (rt.get("row") or {}).get("kind") or "mcp"
    if kind == "a2a":
        if method == "tools/call":
            return _a2a_send(rt["endpoint"], rt.get("headers") or {}, params or {}, timeout)
        return _a2a_discover(rt["endpoint"], rt.get("headers") or {}, timeout)
    if kind == "cli":
        if method == "tools/call":
            return _cli_run(rt.get("argv") or [], rt.get("env") or {}, rt.get("cwd") or "", params or {}, timeout)
        return _cli_tools(rt.get("argv") or [])
    if rt.get("argv"):
        return _stdio_exchange(rt["argv"], rt["env"], method, params or {}, timeout)
    return _http_exchange(rt["endpoint"], rt["headers"], method, params or {}, timeout)





_WRITE_HINTS = re.compile(r"(create|update|write|set_|set-|add_|add-|append|insert|post|send|push|merge|edit|move|apply|"
                          r"execute|run_|run-|import|upload|publish|commit|assign|close|archive|patch|put)", re.I)
_DELETE_HINTS = re.compile(r"(delete|remove|drop|destroy|trash|unlink|purge|revoke)", re.I)


def guess_risk(name: str) -> str:
    if _DELETE_HINTS.search(name or ""):
        return "delete"
    if _WRITE_HINTS.search(name or ""):
        return "write"
    return "read"


_RISKS = ("read", "write", "delete", "console")


def _tool_risk(src: dict, name: str) -> str:
    """工具风险级：**服务端明确标注的优先**，只有没标注时才按名字猜。

    MCP 协议本身不携带风险级，真实 tools/list 一律走 guess_risk；但 A2A 委派与本地 CLI 是我们自己
    在适配层标注的，必须采信——否则 CLI 的 `run`（可执行任意本地命令）会被猜成只读，
    绕过「允许写操作」门控与强制审批（真实缺陷，已修）。
    """
    r = str(src.get("risk") or "")
    return r if r in _RISKS else guess_risk(name)


def build_tools(entry_toolset: list[dict], raw_tools: list[dict], agent_key: str) -> tuple[list[dict], str]:
    """精选工具集 ∩ 真实工具列表。返回 (缓存工具, note)。"""
    raw = [t for t in (raw_tools or []) if isinstance(t, dict) and t.get("name")]
    by_name = {str(t["name"]): t for t in raw}
    out: list[dict] = []
    note = ""
    curated = [c for c in (entry_toolset or []) if isinstance(c, dict) and c.get("name")]
    if curated:
        matched = [c for c in curated if str(c["name"]) in by_name]
        if matched:
            for c in matched[:MAX_TOOLSET]:
                rt = by_name[str(c["name"])]
                out.append({"name": str(c["name"]), "desc": clip(str(c.get("desc") or rt.get("description") or ""), 200),
                            "risk": _tool_risk(c, str(c["name"])), "schema": _schema_of(rt), "fn": fn_name(agent_key, str(c["name"]))})
            missing = len(matched) - len(out)
            if missing > 0:
                note = f"目录策展 {len(curated)} 个工具中 {missing} 个超出上限未挂载"
            return out, note
        note = "该 server 暴露的工具与目录策展版本不一致，已自动改为「优先只读、最多 12 个」"
    # 回退：优先只读，最多 MAX_TOOLSET
    ordered = sorted(raw, key=lambda t: (_tool_risk(t, str(t.get("name"))) != "read", str(t.get("name"))))
    for t in ordered[:MAX_TOOLSET]:
        n = str(t.get("name"))
        out.append({"name": n, "desc": clip(str(t.get("description") or ""), 200), "risk": _tool_risk(t, n),
                    "schema": _schema_of(t), "fn": fn_name(agent_key, n)})
    return out, note


def _schema_of(t: dict) -> dict:
    s = t.get("inputSchema")
    if not isinstance(s, dict):
        return {"type": "object", "properties": {}}
    s = json.loads(json.dumps(s))                           # 深拷贝 + 剔除非法兜底
    s.setdefault("type", "object")
    if "properties" not in s:
        s["properties"] = {}
    return s


# ==================== 连接 / 刷新 / 状态 ====================
def apply_connect(user_id: str, key: str, values: dict, probe: dict) -> dict:
    row = get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    if probe.get("error") or not isinstance(probe.get("result"), dict):
        msg = clip(str(probe.get("message") or probe.get("error") or "连接失败"), 300)
        db.update("agents", {"user_id": user_id, "key": key},
                  {"status": "error", "error": msg, "updated_at": now_ms()})
        return {"ok": False, "message": msg, "agent": public(get(user_id, key))}
    raw = (probe["result"].get("tools") or [])
    tools, note = build_tools(row.get("toolset") or [], raw, key)
    if values:
        _save_credentials(user_id, key, values)
        hint = _mask_values(values)
    else:
        hint = row.get("cred_hint") or ""
    db.update("agents", {"user_id": user_id, "key": key},
              {"status": "connected", "error": "", "note": note, "tools": tools, "tool_count": len(tools),
               "cred_hint": hint, "updated_at": now_ms()})
    return {"ok": True, "message": f"已连接，发现 {len(raw)} 个工具，挂载精选 {len(tools)} 个", "agent": public(get(user_id, key))}


def mark_error(user_id: str, key: str, probe: dict) -> dict:
    msg = clip(str(probe.get("message") or probe.get("error") or "连接失败"), 300)
    db.update("agents", {"user_id": user_id, "key": key},
              {"status": "error", "error": msg, "updated_at": now_ms()})
    return {"ok": False, "message": msg, "agent": public(get(user_id, key))}


def connect(user_id: str, key: str, values: dict | None = None, timeout: int = CONNECT_TIMEOUT) -> dict:
    """同步便捷版（测试 / 脚本）。HTTP 路由请用 resolve_runtime + probe_runtime(to_thread) + apply_connect 三步。"""
    rt = resolve_runtime(user_id, key, values)
    probe = probe_runtime(rt, timeout)
    return apply_connect(user_id, key, rt["values"], probe)


def refresh(user_id: str, key: str, timeout: int = CONNECT_TIMEOUT) -> dict:
    row = get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    rt = resolve_runtime(user_id, key, None)                # 用已存凭据
    probe = probe_runtime(rt, timeout)
    if probe.get("error"):
        return mark_error(user_id, key, probe)
    return apply_connect(user_id, key, {}, probe)


def patch(user_id: str, key: str, changes: dict) -> dict:
    row = get(user_id, key)
    if not row:
        raise ApiError(404, "AGENT_UNKNOWN", "未安装这个 Agent")
    upd = {}
    if "enabled" in changes:
        upd["enabled"] = 1 if changes["enabled"] else 0
    if "allow_write" in changes:
        upd["allow_write"] = 1 if changes["allow_write"] else 0
        if upd["allow_write"] and row["trust"] in ("community", "local") and row["source"] == "custom":
            pass                                             # 允许开启，但每次写操作仍会被审批闸门拦下（policy）
    if not upd:
        raise ApiError(400, "AGENT_INVALID", "没有可更新的字段（enabled / allow_write）")
    upd["updated_at"] = now_ms()
    db.update("agents", {"user_id": user_id, "key": key}, upd)
    return public(get(user_id, key))


def health_targets(user_id: str) -> list[dict]:
    """批检第一步（循环线程，只读）：挑出要检查的 Agent 并解析出运行参数。"""
    rows = [r for r in installed(user_id) if r["status"] in ("connected", "error") and _auth_ready(r)]
    out = []
    for r in rows:
        try:
            out.append({"key": r["key"], "rt": resolve_runtime(user_id, r["key"], None)})
        except ApiError as e:
            out.append({"key": r["key"], "error": {"error": e.code, "message": e.message}})
    return out


def health_probe_all(targets: list[dict], timeout: int = 20) -> list[tuple[str, dict]]:
    """批检第二步（可放线程）：并发连通性探测，纯网络、不碰数据库。"""
    if not targets:
        return []

    def one(t: dict) -> tuple[str, dict]:
        if t.get("rt"):
            return t["key"], probe_runtime(t["rt"], timeout)
        return t["key"], t.get("error") or {"error": "UNKNOWN", "message": "无法解析运行参数"}

    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
        return list(ex.map(one, targets))


def health_apply(user_id: str, results: list[tuple[str, dict]]) -> list[dict]:
    """批检第三步（循环线程，只写）：回写状态并刷新工具清单。"""
    out = []
    for key, probe in results:
        row = get(user_id, key)
        if not row:
            continue
        if probe.get("error") or not isinstance(probe.get("result"), dict):
            msg = clip(str(probe.get("message") or probe.get("error") or "连接失败"), 300)
            db.update("agents", {"user_id": user_id, "key": key},
                      {"status": "error", "error": msg, "updated_at": now_ms()})
        else:
            tools, note = build_tools(row.get("toolset") or [], (probe.get("result") or {}).get("tools") or [], key)
            db.update("agents", {"user_id": user_id, "key": key},
                      {"status": "connected", "error": "", "note": note, "tools": tools,
                       "tool_count": len(tools), "updated_at": now_ms()})
        out.append(public(get(user_id, key)))
    return out


def health(user_id: str, timeout: int = 20) -> list[dict]:
    """同步便捷版（测试 / 脚本）：三段式合并调用。"""
    return health_apply(user_id, health_probe_all(health_targets(user_id), timeout))


# ==================== 工具暴露（供匹配 / 策略 / 运行器使用） ====================
def tool_index(user_id: str) -> dict[str, dict]:
    """已连接且启用的 Agent 的全部工具：全名 mcp.<server>.<tool> → 工具信息。"""
    out: dict[str, dict] = {}
    if not user_id:
        return out
    for r in installed(user_id):
        if not r["enabled"] or r["status"] != "connected":
            continue
        for t in r.get("tools") or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            full = f"mcp.{server_of(r['key'])}.{t['name']}"
            out[full] = {**t, "agent_key": r["key"], "agent_name": r["name"], "trust": r["trust"],
                         "allow_write": bool(r["allow_write"]), "domain": r["domain"]}
    return out


def fn_map(user_id: str) -> dict[str, str]:
    """模型函数名 → 工具全名（运行器用；不做字符串还原，避免歧义）。"""
    return {t.get("fn") or fn_name(v["agent_key"], t["name"]): k
            for k, t in tool_index(user_id).items() for v in [t]}


def risk(user_id: str, full_name: str) -> str:
    t = tool_index(user_id).get(full_name) if user_id else None
    return (t or {}).get("risk") or "write"


def trust_of(user_id: str, full_name: str) -> str:
    t = tool_index(user_id).get(full_name) if user_id else None
    return (t or {}).get("trust") or "community"


def tool_enabled(user_id: str, full_name: str, s: dict) -> bool:
    if not s.get("agents.enabled", True):
        return False
    t = tool_index(user_id).get(full_name)
    if not t:
        return False
    if t["risk"] != "read" and not t.get("allow_write"):
        return False
    return True


def tool_defs(user_id: str, names: list[str]) -> list[dict]:
    """OpenAI 工具定义（仅已连接启用、且被请求的工具）。"""
    idx = tool_index(user_id)
    out = []
    for n in names:
        t = idx.get(n)
        if not t:
            continue
        out.append({"type": "function", "function": {
            "name": t.get("fn") or fn_name(t["agent_key"], t["name"]),
            "description": clip(f"[{t.get('agent_name')}] {t.get('desc') or t['name']}", 400),
            "parameters": t.get("schema") or {"type": "object", "properties": {}}}})
    return out


def prompt_digest(user_id: str, s: dict) -> str:
    """规划提示词的「已连接 Agent」摘要（一级暴露：只给名称 + 工具名，不含 schema）。

    设计见 prd/08 §5.4：无论接入多少 Agent，这一段都被硬上限约束（≤5 个 Agent、每个 ≤8 个工具名）。
    """
    if not user_id or not s.get("agents.enabled", True):
        return ""
    rows = [r for r in installed(user_id) if r["enabled"] and r["status"] == "connected" and _auth_tools_visible(r)]
    if not rows:
        return ""
    parts = []
    for r in rows[:MAX_AGENTS_FOR_PROMPT]:
        tools = [t["name"] for t in (r.get("tools") or []) if t.get("risk") == "read"][:MAX_TOOLS_FOR_PROMPT]
        total = sum(1 for t in (r.get("tools") or []) if t.get("risk") == "read")
        more = f", +{total - len(tools)} more" if total > len(tools) else ""
        parts.append(f"mcp.{server_of(r['key'])}: {', '.join(tools)}{more}")
    return " | ".join(parts)


def _auth_tools_visible(row: dict) -> bool:
    return bool(row.get("tools"))


# ==================== 决策引擎支撑（v3.0 M2，prd/08 §5） ====================
def _tokens(text: str) -> set[str]:
    """轻量分词：英文词（≥2 字符，小写）+ 中文 2-gram。用于能力需求与目录条目的确定性词面匹配。"""
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9_]{2,}", (text or "").lower()):
        out.add(w)
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        if len(seg) <= 4:
            out.add(seg)
        else:
            out.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def _entry_text(e: dict) -> str:
    parts = [str(e.get("name") or ""), str(e.get("name_en") or ""),
             str(e.get("description") or ""), str(e.get("description_en") or "")]
    for t in e.get("toolset") or []:
        if isinstance(t, dict):
            parts.append(str(t.get("name") or ""))
            parts.append(str(t.get("desc") or ""))
    return " ".join(parts)


def _server_of_tool(full: str) -> str:
    parts = str(full).split(".")
    return parts[1] if len(parts) >= 3 else ""


def _score_entry(e: dict, need_tokens: set[str], permission: str, row: dict | None) -> dict | None:
    """漏斗二评分（确定性，prd/08 §5.3）：能力覆盖 0.45 / 信任级 0.20 / 权限契合 0.15 / 成本 0.10 / 稳定性 0.10。"""
    toolset = [t for t in (e.get("toolset") or []) if isinstance(t, dict)]
    has_write = any(str(t.get("risk")) in ("write", "delete") for t in toolset)
    if permission == "write" and not has_write:
        return None                                          # 需要写而条目没有写工具 → 直接排除
    text = _entry_text(e).lower()
    hit = sum(1 for t in need_tokens if t in text)
    coverage = hit / max(1, len(need_tokens))
    trust = _TRUST_SCORE.get(str(e.get("trust") or "community"), 0.6)
    perm = 1.0
    cost = _COST_SCORE.get(str(e.get("cost") or "free").lower(), 0.7)
    if row is None:
        stab = 0.8                                           # 未安装：中性（连接后可用）
    elif row.get("status") == "connected":
        stab = 1.0
    elif row.get("status") == "error":
        stab = 0.2
    else:
        stab = 0.6
    score = 0.45 * coverage + 0.20 * trust + 0.15 * perm + 0.10 * cost + 0.10 * stab
    return {"score": round(score, 4),
            "breakdown": {"coverage": round(coverage, 3), "trust": trust, "perm": perm, "cost": cost, "stability": stab}}


def match_catalog(user_id: str, needs: list[str], req_tools: list[str], permission: str, s: dict,
                  limit: int = 3) -> dict | None:
    """漏斗二：需求 ∩ 目录（含未连接）→ 评分排序 → primary + 备选（确定性，同样输入同样输出）。

    返回 None = 外部调用被用户约束关闭（N4），调用方不必再展示外部信息。
    """
    if not s.get("agents.enabled", True):
        return None
    need_tokens = _tokens(" ".join(needs or []) + " " + " ".join(req_tools or []))
    rows = {r["key"]: r for r in installed(user_id)}
    scored = []
    for e in entries():
        r = _score_entry(e, need_tokens, permission, rows.get(e["key"]))
        # 噪声控制（prd/08 §5.3「低于阈值不提」）：阈值作用在**能力覆盖**上，覆盖率 < MIN_COVERAGE 的条目一律不提。
        # 不能拿总分做阈值——信任/权限/成本/稳定性四项在同一条目录上近乎常量，任何条目都自带约 0.45 的基线分，
        # 用总分判定等于没有阈值（曾观察到「量子纠缠态制备」匹配到 AWS、「把周报发到飞书」匹配到本地文件系统，
        # 覆盖率分别是 6% 与 0%）。能力覆盖是唯一与「这个需求是否相关」有关的维度。
        if r and r["breakdown"]["coverage"] >= MIN_COVERAGE:
            scored.append((e, r, rows.get(e["key"])))
    scored.sort(key=lambda x: (-x[1]["score"], x[0]["key"]))
    picks = []
    for e, r, row in scored[:limit]:
        picks.append({"key": e["key"], "name": e.get("name"), "name_en": e.get("name_en"),
                      "domain": e.get("domain"), "trust": e.get("trust"),
                      "connected": bool(row and row.get("status") == "connected"),
                      "score": r["score"], "breakdown": r["breakdown"]})
    if not picks:
        return {"primary": None, "alternates": [], "reason": ""}
    p = picks[0]
    b = p["breakdown"]
    reason = (f"需要「{clip(' '.join(needs or req_tools), 40)}」→ 匹配 {p['name']}："
              f"能力覆盖 {int(b['coverage'] * 100)}%、信任级 {p['trust']}、成本 {b['cost']}、稳定性 {b['stability']}"
              if any("\u4e00" <= ch <= "\u9fff" for ch in " ".join(needs or req_tools))
              else f"Need \"{clip(' '.join(needs or req_tools), 40)}\" → matched {p.get('name_en') or p['name']}: "
                   f"coverage {int(b['coverage'] * 100)}%, trust {p['trust']}")
    return {"primary": p, "alternates": picks[1:limit], "reason": reason}


def plan_node(user_id: str, n: dict, s: dict) -> dict | None:
    """规划期：给一个节点算出外部 Agent 决策（写进 node.agents）。

    - 节点没有 agent_needs、也没显式请求 mcp.* 工具 → 返回 None（不展示外部信息，保持画布干净）；
    - 有需求 → 漏斗二选 primary + 备选；已连接则把精选只读工具列入 attached（执行期据此挂载）；
    - 命中未连接 → 记 gap（连接引导，非阻塞：该节点照常用内置能力执行）。
    """
    needs = [str(x).strip() for x in (n.get("agent_needs") or []) if str(x).strip()][:5]
    req = n.get("capability_requirements") or {}
    explicit = [t for t in (req.get("tools") or []) if str(t).startswith("mcp.")]
    if not needs and not explicit:
        return None
    m = match_catalog(user_id, needs, explicit, req.get("permission", "read"), s)
    if m is None:                                            # N4：总开关关闭（结构与正常分支保持一致，前端无需分支处理）
        return {"mode": s.get("agents.mode", "balanced"), "used": False, "why": "N4",
                "reason": "外部 Agent 调用总开关已关闭，本节点由内置能力执行",
                "primary": None, "alternates": [], "attached": [], "gap": []}
    idx = tool_index(user_id)
    rows = {r["key"]: r for r in installed(user_id)}
    primary = m.get("primary")
    out: dict = {"mode": s.get("agents.mode", "balanced"), "reason": m.get("reason") or "",
                 "primary": primary, "alternates": m.get("alternates") or [], "attached": [], "gap": []}
    if not primary:
        out["used"] = False
        out["gap"] = sorted({_server_key_of(t) for t in explicit if t not in idx and _server_key_of(t)})
        return out
    # 模型显式点名且已连接的工具优先；否则用 primary 的精选只读工具集
    attached = [t for t in explicit if t in idx]
    pk, row = primary["key"], rows.get(primary["key"])
    if row and row.get("status") == "connected":
        for t in (row.get("tools") or []):
            if t.get("risk") == "read":
                full = f"mcp.{server_of(row['key'])}.{t['name']}"
                if full not in attached:
                    attached.append(full)
    primary["connected"] = bool(row and row.get("status") == "connected")
    gap = sorted({t for t in explicit if t not in idx})
    if not primary["connected"]:
        gap.append(pk)
    out["attached"] = attached[:MAX_TOOLS_PER_NODE]
    out["gap"] = list(dict.fromkeys(gap))
    out["used"] = bool(out["attached"])
    return out


def _server_key_of(full: str) -> str:
    sv = _server_of_tool(full)
    return f"mcp.{sv}" if sv else ""


def cap_task_servers(nodes: list[dict]) -> list[str]:
    """单任务 ≤ MAX_SERVERS_PER_TASK 个外部 server：装不下的节点整组剔除 attached 并标注 capped。"""
    ranked = []
    for n in nodes:
        ap = n.get("agents") or {}
        svs = sorted({s for s in (_server_of_tool(t) for t in (ap.get("attached") or [])) if s})
        if svs:
            ranked.append((((ap.get("primary") or {}).get("score")) or 0, n["node_key"], svs, n))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    used: set[str] = set()
    dropped: list[str] = []
    for _score, key, svs, n in ranked:
        fresh = [s for s in svs if s not in used]
        if len(used) + len(fresh) > MAX_SERVERS_PER_TASK:
            n["agents"]["attached"] = []
            n["agents"]["capped"] = True
            n["agents"]["used"] = False
            dropped.append(key)
            continue
        used.update(fresh)
    return dropped


def node_tools(user_id: str, n: dict, s: dict) -> list[str]:
    """执行期：算出该节点实际挂载的外部工具全名（两级暴露硬上限 + 总开关 + 知识库外发闸门）。

    - 总开关关闭 / 知识库外发被禁且本节点会检索知识库 → 不挂任何外部工具（N4，事件流里标注）；
    - 硬上限：单节点 ≤2 个 server、≤24 个工具（超出按 server 首次出现顺序裁剪）。
    """
    if not s.get("agents.enabled", True):
        return []
    if not s.get("agents.kb_share", True) and (n.get("private_context") or
            set(n.get("tools") or []) & {"kb.search", "reference.read"}):
        return []                                            # 可能携带知识库内容 → 一律不外发
    idx = tool_index(user_id)
    names: list[str] = []
    for t in [*((n.get("agents") or {}).get("attached") or []), *(n.get("tools") or [])]:
        if not str(t).startswith("mcp.") or t in names or t not in idx:
            continue
        names.append(t)
    servers: list[str] = []
    out: list[str] = []
    for t in names:
        sv = _server_of_tool(t)
        if sv not in servers:
            if len(servers) >= MAX_SERVERS_PER_NODE:
                continue
            servers.append(sv)
        out.append(t)
    return out[:MAX_TOOLS_PER_NODE]


def catalog_digest(user_id: str, s: dict, domains_hint: list[str] | None = None, lang: str = "zh") -> str:
    """目录摘要（一级暴露，prd/08 §5.4）：`code: GitHub(仓库/issue/PR), Sentry(错误)…`，✓=已连接。

    无论目录里是 46 条还是 300 条，本段都被 MAX_CATALOG_DIGEST_CHARS 硬约束。
    """
    if not s.get("agents.enabled", True):
        return ""
    en = lang == "en"
    rows = {r["key"]: r for r in installed(user_id)}
    hint = [d for d in (domains_hint or []) if d]
    doms = domains()
    ordered = [d for d in doms if d["key"] in hint] + [d for d in doms if d["key"] not in hint]
    lines = []
    for d in ordered:
        items = [e for e in entries() if e.get("domain") == d["key"]]
        if not items:
            continue
        items.sort(key=lambda e: (0 if rows.get(e["key"], {}).get("status") == "connected" else 1,
                                  0 if e.get("featured") else 1, str(e.get("key") or "")))
        segs = []
        for e in items[:3]:
            mark = "✓" if rows.get(e["key"], {}).get("status") == "connected" else ""
            desc = clip(str((e.get("description_en") if en else e.get("description")) or ""), 16)
            segs.append(f"{e.get('name')}{mark}({desc})")
        lines.append(f"{d['key']}: " + ", ".join(segs))
    return clip("\n".join(lines), MAX_CATALOG_DIGEST_CHARS)


def search(user_id: str, query: str, limit: int = 6) -> dict:
    """agent.search（按需发现，prd/08 §5.4）：按「我还需要什么能力」在目录里检索（含未连接）。

    只回候选清单（名称 / 域 / 一句话 / 是否已连接 / 需要什么准备），不含任何 schema —— schema
    由 agent.describe 按需查看，避免把整个目录的工具定义灌进上下文。
    """
    q = _tokens(query or "")
    rows = {r["key"]: r for r in installed(user_id)}
    out = []
    for e in entries():
        score = len(q & _tokens(_entry_text(e))) if q else 0
        if q and score <= 0:
            continue
        r = rows.get(e["key"])
        auth = str(e.get("auth") or "none")
        out.append((score, {"key": e["key"], "name": e.get("name"), "domain": e.get("domain"),
                            "desc": clip(str(e.get("description") or ""), 60),
                            "installed": bool(r),
                            "connected": bool(r and r.get("status") == "connected"),
                            "setup": ("" if r else ("在「Agent 目录」连接并填写凭据" if auth != "none" else "可在「Agent 目录」一键连接"))}))
    out.sort(key=lambda x: (-x[0], x[1]["key"]))
    return {"items": [i for _, i in out[:limit]],
            "note": "用 agent.describe（key）可查看某个 Agent 的精选工具集；连接请在「Agent 目录」页完成"}


def describe(user_id: str, key: str) -> dict:
    """agent.describe（按需发现）：返回某 Agent 的精选工具集（已连接用真实缓存，未连接用目录策展版本）。"""
    key = str(key or "").strip()
    row = get(user_id, key) if key else None
    if row:
        tools = [{"name": t.get("name"), "desc": t.get("desc"), "risk": t.get("risk")} for t in (row.get("tools") or [])]
        return {"key": key, "name": row["name"], "domain": row["domain"], "connected": row["status"] == "connected",
                "status": row["status"], "trust": row["trust"], "tools": tools, "allow_write": bool(row["allow_write"]),
                "note": "已连接：以上为当前挂载的精选工具集" if row["status"] == "connected" else "已安装但未连接：在「Agent 目录」点「连接」后可用"}
    e = index().get(key)
    if not e:
        return {"error": "AGENT_UNKNOWN", "message": f"目录中没有 {key}（可用 agent.search 检索）"}
    tools = [{"name": t.get("name"), "desc": t.get("desc"), "risk": t.get("risk")}
             for t in (e.get("toolset") or []) if isinstance(t, dict)]
    auth = str(e.get("auth") or "none")
    return {"key": key, "name": e.get("name"), "domain": e.get("domain"), "connected": False, "trust": e.get("trust"),
            "tools": tools,
            "setup": "需要在「Agent 目录」连接并填写凭据" if auth != "none" else "可在「Agent 目录」一键连接",
            "note": "未连接：以上为目录策展的工具集（连接后与真实工具求交）"}


# ==================== 调用 ====================
def _prepare_call(user_id: str, full_name: str, args: dict, task_id: str, node_key: str):
    """执行一次外部工具调用；结果规范化 + 审计入账。异常一律转成 {error}。"""
    idx = tool_index(user_id)
    t = idx.get(full_name)
    if not t:
        return {"error": "TOOL_DENIED", "message": f"{full_name} 不在已连接的 Agent 工具列表里"}
    error = tool_args.validate(args, t.get("schema") or {"type": "object"})
    if error:
        return {"error": "ARGS_INVALID", "message": error}
    if t["risk"] != "read" and not t.get("allow_write"):
        _ledger(user_id, task_id, node_key, t["agent_key"], t["name"], args, "denied", 0, 0, "WRITE_NOT_ALLOWED")
        return {"error": "WRITE_NOT_ALLOWED", "message": "该工具是写操作：需要在 Agent 设置中开启「允许写操作」（且执行时可能要你批准）"}
    agent_key = t["agent_key"]
    tool = t["name"]
    try:
        rt = resolve_runtime(user_id, agent_key)
    except ApiError as e:
        _ledger(user_id, task_id, node_key, agent_key, tool, args, "error", 0, 0, e.code)
        return {"error": e.code, "message": e.message}
    return {"runtime": rt, "tool": t}


def _finish_call(user_id, full_name, args, task_id, node_key, t, probe, t0):
    if not db.q1("SELECT id FROM users WHERE id=?", (user_id,)):
        return {"error": "USER_DELETED", "message": "账号已删除，未保存迟到的调用结果"}
    agent_key, tool = t["agent_key"], t["name"]
    ms = int((time.time() - t0) * 1000)
    if probe.get("error") or not isinstance(probe.get("result"), dict):
        msg = clip(str(probe.get("message") or probe.get("error") or "调用失败"), 300)
        _ledger(user_id, task_id, node_key, agent_key, tool, args, "error", ms, 0, msg[:120])
        return {"error": probe.get("error") or "AGENT_ERROR", "message": msg}
    out = _normalize_result(probe["result"])
    out["agent"] = t.get("agent_name")
    out["tool"] = full_name
    out["ms"] = ms
    _ledger(user_id, task_id, node_key, agent_key, tool, args,
            "ok" if "error" not in out else "error", ms, len(json.dumps(out, ensure_ascii=False)), "" if "error" not in out else out.get("message", "")[:120])
    return out


def call(user_id: str, full_name: str, args: dict, task_id: str = "", node_key: str = "", timeout: int = CALL_TIMEOUT) -> dict:
    prepared = _prepare_call(user_id, full_name, args, task_id, node_key)
    if "error" in prepared:
        return prepared
    t0 = time.time()
    t = prepared["tool"]
    probe = probe_runtime(prepared["runtime"], timeout, method="tools/call", params={"name": t["name"], "arguments": args or {}})
    return _finish_call(user_id, full_name, args, task_id, node_key, t, probe, t0)


async def call_async(user_id: str, full_name: str, args: dict, task_id: str = "", node_key: str = "", timeout: int = CALL_TIMEOUT) -> dict:
    prepared = _prepare_call(user_id, full_name, args, task_id, node_key)
    if "error" in prepared:
        return prepared
    t0 = time.time()
    t = prepared["tool"]
    probe = await asyncio.to_thread(probe_runtime, prepared["runtime"], timeout, method="tools/call",
                                   params={"name": t["name"], "arguments": args or {}})
    return _finish_call(user_id, full_name, args, task_id, node_key, t, probe, t0)


def _normalize_result(result: dict) -> dict:
    texts, other = [], 0
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            texts.append(str(c.get("text") or ""))
        else:
            other += 1
    body = clip("\n\n".join(texts), MAX_RESULT_CHARS)
    if result.get("isError"):
        return {"error": "AGENT_TOOL_ERROR", "message": clip(body or "外部工具返回错误", 400)}
    out: dict = {"content": body}
    if other:
        out["note"] = f"另有 {other} 个非文本内容块（图片 / 资源）未回传"
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and len(json.dumps(sc, ensure_ascii=False)) <= 2000:
        out["data"] = sc
    return out


def _ledger(user_id: str, task_id: str, node_key: str, agent_key: str, tool: str, args, status: str,
            ms: int, result_bytes: int, error: str) -> None:
    db.insert("agent_calls", {"id": new_id("alc"), "user_id": user_id, "task_id": task_id or "",
                              "node_key": node_key or "", "agent_key": agent_key, "tool": tool,
                              "args_hash": stable_hash(args if isinstance(args, dict) else {"v": args})[:16],
                              "status": status, "ms": ms, "result_bytes": result_bytes,
                              "error": error, "created_at": now_ms()})
