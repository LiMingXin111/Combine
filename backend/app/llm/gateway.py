"""模型网关：模型目录与解析、能力校验与替换、自适应限流、额度守卫、调用账本。

- 所有模型调用都经过这里（INV-18 脱敏、INV-20 记录实际模型）；
- 成本只做展示：任务级预算不再拦截调用（estimate + cost_used 只用于界面展示），
  唯一会拦住调用的是**平台内置模型的月度额度**（平台自己的资源上限）；
- 平台内置 / 外部 Key / 本地模型都按 OpenAI 兼容接口调用（Gemini、Ollama、LM Studio、vLLM 均提供该接口）；
- 未配置平台 Key 时，内置模型由 FakeLLM 提供。
"""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .. import config, db, events
from ..security import decrypt_secret
from ..util import ApiError, new_id, now_ms, redact, register_secret
from . import fake

TIERS = ("fast", "standard", "strong")
# 内置模型单价（元 / 百万 token）。FakeLLM 下为演示用的名义价格。
BUILTIN_PRICES = {"fast": (1.0, 2.0), "standard": (4.0, 12.0), "strong": (12.0, 36.0)}

PROVIDER_CATALOG = [
    # kind, 名称, 默认地址, 需要 Key, 来源, 推荐模型
    ("openai", "OpenAI", "https://api.openai.com/v1", True, "byok", ["gpt-4o-mini", "gpt-4o"]),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1", True, "byok", ["deepseek-chat"]),
    ("qwen", "通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", True, "byok", ["qwen-plus", "qwen-turbo"]),
    ("zhipu", "智谱", "https://open.bigmodel.cn/api/paas/v4", True, "byok", ["glm-4-flash"]),
    ("moonshot", "Kimi", "https://api.moonshot.cn/v1", True, "byok", ["moonshot-v1-8k"]),
    ("doubao", "豆包", "https://ark.cn-beijing.volces.com/api/v3", True, "byok", []),
    ("gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", True, "byok", ["gemini-2.0-flash"]),
    ("openai_compatible", "OpenAI 兼容服务", "", True, "byok", []),
    ("ollama", "Ollama（本机）", "http://127.0.0.1:11434/v1", False, "local", ["llama3.1:8b", "qwen2.5:7b"]),
    ("lmstudio", "LM Studio（本机）", "http://127.0.0.1:1234/v1", False, "local", []),
    ("vllm", "vLLM（本机）", "http://127.0.0.1:8000/v1", False, "local", []),
    ("fake", "模拟服务商（测试用）", "fake://local", True, "byok", ["fake-full", "fake-no-tools"]),
]
CATALOG = {c[0]: c for c in PROVIDER_CATALOG}


class LLMError(Exception):
    def __init__(self, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, redact(message), retry_after


class BudgetExceeded(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ModelInfo:
    ref: str
    source: str                  # builtin | byok | local
    display: str
    model_name: str
    engine: str                  # fake | openai
    caps: set = field(default_factory=set)
    price_in: float = 0.0        # 元 / 百万 token
    price_out: float = 0.0
    provider_id: str | None = None
    provider_kind: str | None = None
    base_url: str = ""
    api_key: str = ""
    status: str = "ok"
    limiter_key: str = ""

    def public(self) -> dict:
        return {"ref": self.ref, "source": self.source, "display_name": self.display, "provider_kind": self.provider_kind,
                "capabilities": sorted(self.caps), "price_in_per_1m": self.price_in, "price_out_per_1m": self.price_out,
                "status": self.status}


# ---------------- 模型目录 ----------------
def builtin_info(tier: str) -> ModelInfo:
    pin, pout = BUILTIN_PRICES[tier]
    if config.FAKE_LLM:
        return ModelInfo(f"builtin:{tier}", "builtin", {"fast": "内置 · 快速（演示）", "standard": "内置 · 标准（演示）",
                                                      "strong": "内置 · 强力（演示）"}[tier],
                         f"fake-{tier}", "fake", {"json", "tools", "long_ctx"}, pin, pout, limiter_key=f"builtin:{tier}")
    name = config.PLATFORM_MODELS.get(tier) or config.PLATFORM_MODELS.get("standard") or "gpt-4o-mini"
    register_secret(config.PLATFORM_API_KEY)
    return ModelInfo(f"builtin:{tier}", "builtin", f"内置 · {name}", name, "openai", {"json", "tools", "long_ctx"},
                     pin, pout, base_url=config.PLATFORM_BASE_URL, api_key=config.PLATFORM_API_KEY,
                     limiter_key=f"builtin:{tier}")


def model_info(ref: str, user_id: str) -> ModelInfo | None:
    if not ref or ref == "auto":
        return None
    src, _, rest = ref.partition(":")          # 只按第一个冒号拆分：本地模型名可能自带冒号
    if src == "builtin":
        return builtin_info(rest) if rest in TIERS else None
    if src not in ("byok", "local"):
        return None
    m = db.q1("SELECT m.*, p.kind, p.source, p.base_url, p.key_ciphertext, p.status AS pstatus, p.display_name AS pname "
              "FROM provider_models m JOIN provider_configs p ON p.id=m.provider_id "
              "WHERE m.id=? AND m.user_id=?", (rest, user_id))           # INV-08：只能用自己的服务商
    if not m:
        return None
    if m["source"] == "local" and config.DEPLOY_MODE != "self_hosted":
        status = "unavailable"
    else:
        status = {"key_invalid": "key_invalid", "unreachable": "unreachable"}.get(m["pstatus"], "ok")
    key = ""
    if m["key_ciphertext"]:
        try:
            key = decrypt_secret(m["key_ciphertext"], user_id, m["provider_id"])
            register_secret(key)
        except Exception:
            status = "key_invalid"
    return ModelInfo(ref, m["source"], m["display_name"], m["model_name"], "fake" if m["kind"] == "fake" else "openai",
                     set(db.j(m["capabilities"], [])), m["price_in_per_1m"], m["price_out_per_1m"],
                     m["provider_id"], m["kind"], m["base_url"], key, status, limiter_key=m["provider_id"])


def list_models(user_id: str) -> dict:
    """模型切换器数据源（F28）。"""
    groups = [{"key": "auto", "items": [{"ref": "auto", "display_name": "自动（按步骤分档）", "capabilities": ["json", "tools"],
                                         "source": "auto", "status": "ok"}]},
              {"key": "builtin", "items": [builtin_info(t).public() for t in TIERS]}]
    rows = db.q("SELECT m.id, p.source FROM provider_models m JOIN provider_configs p ON p.id=m.provider_id "
                "WHERE m.user_id=? AND m.visible=1 ORDER BY p.created_at, m.rowid", (user_id,))
    by = {"byok": [], "local": []}
    for r in rows:
        if r["source"] == "local" and config.DEPLOY_MODE != "self_hosted":
            continue
        mi = model_info(f"{r['source']}:{r['id']}", user_id)
        if mi:
            by[r["source"]].append(mi.public())
    groups.append({"key": "byok", "items": by["byok"]})
    if config.DEPLOY_MODE == "self_hosted":
        groups.append({"key": "local", "items": by["local"]})
    return {"groups": groups, "fake_llm": config.FAKE_LLM}


def resolve_for_node(user_id: str, task: dict, node: dict, settings: dict) -> tuple[ModelInfo | None, dict | None]:
    """按「节点覆盖 > 任务选择 > 自动分档」解析模型，并做能力校验（R28）。
    返回 (实际模型, 替换信息)；替换信息非空表示发生了 model.substituted 或无可用模型。"""
    tier = node.get("model_tier") or "standard"
    choice = node.get("model_override") or task.get("model_choice") or "auto"
    ref = settings.get(f"models.tier_{tier}", f"builtin:{tier}") if choice == "auto" else choice
    tools = node.get("tools") or []
    if isinstance(tools, str):
        tools = db.j(tools, [])
    needs = {"json"} | ({"tools"} if tools else set())
    info = model_info(ref, user_id)
    reason = None
    if info is None:
        reason = "not_found"
    elif info.status != "ok":
        reason = info.status
    elif not needs <= info.caps:
        reason = "missing_" + "_".join(sorted(needs - info.caps))
    if reason is None:
        return info, None
    requested = info.display if info else ref
    if settings.get("models.allow_builtin_fallback", True):
        fb = builtin_info(tier)
        return fb, {"requested": requested, "used": fb.display, "reason": reason}
    return None, {"requested": requested, "used": None, "reason": reason}


# ---------------- 自适应限流（R30.5） ----------------
class Limiter:
    def __init__(self, permits: int, cap: int):
        self.permits, self.cap, self.in_use, self.ok_streak, self.cool_until = permits, cap, 0, 0, 0.0

    def try_acquire(self) -> bool:
        if time.time() < self.cool_until or self.in_use >= self.permits:
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)


_limiters: dict[str, Limiter] = {}


def limiter_for(info: ModelInfo) -> Limiter:
    lim = _limiters.get(info.limiter_key)
    if lim is None:
        if info.source == "builtin":
            lim = Limiter(8, 16)
        elif info.source == "local":
            lim = Limiter(1, 4)                     # 单 GPU 串行通常更快
        else:
            p = db.q1("SELECT concurrency FROM provider_configs WHERE id=?", (info.provider_id,))
            n = (p or {}).get("concurrency") or config.DEFAULT_PROVIDER_CONCURRENCY
            lim = Limiter(n, max(n, 8))
        _limiters[info.limiter_key] = lim
    return lim


def on_rate_limited(info: ModelInfo, task_id: str | None, retry_after: float | None) -> None:
    lim = limiter_for(info)
    lim.permits = max(1, lim.permits // 2)
    lim.ok_streak = 0
    lim.cool_until = time.time() + (retry_after or 2.0) * (config.FAKE_LATENCY_SCALE if info.engine == "fake" else 1)
    if task_id:
        events.emit(task_id, "scheduler.concurrency_changed",
                    {"provider": info.display, "permits": lim.permits, "reason": "rate_limited"})


def on_success(info: ModelInfo, task_id: str | None) -> None:
    lim = limiter_for(info)
    lim.ok_streak += 1
    if lim.ok_streak >= 10 and lim.permits < lim.cap:
        lim.permits += 1
        lim.ok_streak = 0
        if task_id:
            events.emit(task_id, "scheduler.concurrency_changed",
                        {"provider": info.display, "permits": lim.permits, "reason": "recovered"})


# ---------------- 预算（展示为主，额度为硬约束） ----------------
def estimate_cost(info: ModelInfo, tokens_in: int, tokens_out: int) -> float:
    return (info.price_in * tokens_in + info.price_out * tokens_out) / 1_000_000


def month_used(user_id: str) -> float:
    lt = time.localtime()
    start = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)) * 1000)
    r = db.q1("SELECT COALESCE(SUM(cost_cny),0) AS c FROM llm_calls WHERE user_id=? AND source='builtin' AND created_at>=?",
              (user_id, start))
    return r["c"]


# 进行中调用的预留额度。预检发生在调用之前、记账发生在调用之后；多个步骤并发时，
# 如果不把「已发出但未记账」的调用算进去，N 个并发调用会各自通过预检，合计超出额度。
_reserved_user: dict[str, float] = {}


def quota_guard(user_id: str, task_id: str | None, info: ModelInfo, est: float) -> None:
    """只有平台内置模型的月度额度会拦住调用；任务预算纯粹是展示值（成本只做展示，用户无需确认）。"""
    _ = task_id
    if info.source == "builtin":
        u = db.q1("SELECT quota_monthly_cny FROM users WHERE id=?", (user_id,))
        if u and month_used(user_id) + _reserved_user.get(user_id, 0.0) + est > u["quota_monthly_cny"]:
            raise BudgetExceeded("QUOTA_EXCEEDED", "本月平台内置模型额度已用完")


def _reserve(user_id: str, task_id: str | None, info: ModelInfo, amount: float) -> None:
    """amount 为正表示预留，为负表示释放。"""
    _ = task_id
    if info.source == "builtin":
        v = _reserved_user.get(user_id, 0.0) + amount
        if v > 1e-12:
            _reserved_user[user_id] = v
        else:
            _reserved_user.pop(user_id, None)


# ---------------- 调用 ----------------
def _approx_tokens(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages) // 2 + 50


async def chat(*, user_id: str, task_id: str | None, node_key: str | None, kind: str, info: ModelInfo,
               messages: list[dict], tools: list[dict] | None = None, json_mode: bool = True,
               meta: dict | None = None, max_out: int = 4000) -> dict:
    """单次模型调用：额度预检 → 调用（限流重试）→ 记账。返回 {content, tool_calls, cost}。"""
    tin_est = _approx_tokens(messages)
    est = estimate_cost(info, tin_est, max_out // 2)
    quota_guard(user_id, task_id, info, est)
    _reserve(user_id, task_id, info, est)                  # 预检与预留在同一个同步段内完成，期间不会被其他协程插入
    try:
        return await _chat_inner(user_id, task_id, node_key, kind, info, messages, tools, json_mode, meta, max_out, tin_est)
    finally:
        _reserve(user_id, task_id, info, -est)


async def _chat_inner(user_id, task_id, node_key, kind, info, messages, tools, json_mode, meta, max_out, tin_est) -> dict:
    attempt, t0 = 0, time.time()
    while True:
        attempt += 1
        try:
            if info.engine == "fake":
                out = await fake.respond(kind, meta or {}, messages, tools, info.model_name)
                tin, tout = tin_est, len(out.get("content") or "") // 2 + 20 * len(out.get("tool_calls") or [])
            else:
                out, tin, tout = await asyncio.to_thread(_openai_call, info, messages, tools, json_mode, max_out)
            break
        except (fake.FakeRateLimited, LLMError) as e:
            rl = isinstance(e, fake.FakeRateLimited) or e.code == "RATE_LIMITED"
            _ledger(user_id, task_id, node_key, kind, info, 0, 0, 0.0, t0, False, "RATE_LIMITED" if rl else e.code)
            if rl and attempt <= 3:
                on_rate_limited(info, task_id, getattr(e, "retry_after", None))
                await asyncio.sleep(max(0.05, limiter_for(info).cool_until - time.time()))
                continue
            raise e if isinstance(e, LLMError) else LLMError("RATE_LIMITED", "服务商持续限流")
        except fake.FakeFailure as e:
            _ledger(user_id, task_id, node_key, kind, info, 0, 0, 0.0, t0, False, "LLM_ERROR")
            raise LLMError("LLM_ERROR", str(e))
    cost = estimate_cost(info, tin, tout)
    _ledger(user_id, task_id, node_key, kind, info, tin, tout, cost, t0, True, None)
    on_success(info, task_id)
    return {"content": out.get("content") or "", "tool_calls": out.get("tool_calls") or [],
            "reasoning": out.get("reasoning") or "", "cost": cost,
            "finish_reason": out.get("finish_reason") or ""}   # length = 撞输出上限；调用方据此加大预算重试（思考模型常把预算烧在推理上）


def _ledger(user_id, task_id, node_key, kind, info: ModelInfo, tin, tout, cost, t0, ok, err):
    db.insert("llm_calls", {"id": new_id("llm"), "user_id": user_id, "task_id": task_id, "node_key": node_key,
                            "kind": kind, "source": info.source, "provider_id": info.provider_id,
                            "model_id": info.ref + "|" + info.model_name, "tokens_in": tin, "tokens_out": tout,
                            "cost_cny": round(cost, 6), "latency_ms": int((time.time() - t0) * 1000), "ok": int(ok),
                            "error_code": err, "created_at": now_ms()})
    if task_id and cost:
        db.ex("UPDATE tasks SET cost_used=cost_used+? WHERE id=?", (cost, task_id))


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新做地址校验：防止公网地址 302 到内网 / 云元数据地址（SSRF）。"""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_base_url(newurl, "byok")
        except ApiError as e:
            raise urllib.error.URLError(e.message)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirect())


def safe_urlopen(req: urllib.request.Request, timeout: float):
    """所有出站 HTTP 都走这里：云端版在真正连接前再解析一次域名并校验（缓解配置后改 DNS 的情况）。"""
    try:
        check_base_url(req.full_url, "byok")
    except ApiError as e:
        raise urllib.error.URLError(e.message)
    return _opener.open(req, timeout=timeout)


def _http_json(url: str, key: str, body: dict | None, timeout: int) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with safe_urlopen(req, timeout) as r:
            out = json.loads(r.read().decode("utf-8"))
            if not isinstance(out, dict):
                raise ValueError("not an object")
            return out
    except urllib.error.HTTPError as e:
        detail = redact(e.read().decode("utf-8", "ignore")[:300])
        if e.code in (401, 403):
            raise LLMError("AUTH_FAILED", f"鉴权失败（HTTP {e.code}）")
        if e.code == 429:
            ra = e.headers.get("Retry-After")
            raise LLMError("RATE_LIMITED", "服务商限流", float(ra) if ra and ra.isdigit() else None)
        if e.code == 404:
            raise LLMError("MODEL_NOT_FOUND", f"接口或模型不存在：{detail}")
        raise LLMError("LLM_ERROR", f"HTTP {e.code}：{detail}")
    except (OSError, http.client.HTTPException) as e:      # URLError / 超时 / 连接重置 / 不完整响应
        raise LLMError("UNREACHABLE", f"无法连接服务：{redact(str(e))[:200]}")
    except ValueError:
        raise LLMError("LLM_ERROR", "服务返回的不是 JSON")


def _openai_call(info: ModelInfo, messages, tools, json_mode, max_out):
    body = {"model": info.model_name, "messages": messages, "max_tokens": max_out, "temperature": 0.3}
    if tools:
        body["tools"] = tools
    elif json_mode and "json" in info.caps:
        body["response_format"] = {"type": "json_object"}
    data = _http_json(info.base_url.rstrip("/") + "/chat/completions", info.api_key, body, config.LLM_TIMEOUT_S)
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append({"id": c.get("id") or new_id("call"), "name": fn.get("name"), "args": args})
    u = data.get("usage") or {}
    return ({"content": msg.get("content") or "", "tool_calls": calls,
             "reasoning": msg.get("reasoning_content") or "",     # 思考模式：带 tools 的多轮调用需回传该字段（DeepSeek 契约）
             "finish_reason": choice.get("finish_reason") or ""},  # length = 撞输出上限被截断，调用方据此要求模型压缩输出
            int(u.get("prompt_tokens") or _approx_tokens(messages)), int(u.get("completion_tokens") or 0))


# ---------------- 服务商：地址校验与测试连接（F29） ----------------
def check_base_url(url: str, source: str) -> None:
    if url.startswith("fake://"):
        return
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ApiError(400, "INVALID_URL", "服务地址需以 http:// 或 https:// 开头")
    if config.DEPLOY_MODE != "cloud":
        return
    host = p.hostname
    private = host in ("localhost",) or host.endswith(".local")
    try:
        for fam, *_, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0].split("%")[0])
            if getattr(ip, "ipv4_mapped", None):
                ip = ip.ipv4_mapped                          # ::ffff:127.0.0.1 这类写法
            private = private or ip.is_private or ip.is_loopback or ip.is_link_local \
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    except (socket.gaierror, ValueError, UnicodeError):
        pass
    if private:
        raise ApiError(400, "PRIVATE_ADDRESS_FORBIDDEN", "云端版无法访问你电脑或内网上的地址，请使用自托管版")


async def probe(kind: str, base_url: str, key: str, model_name: str) -> dict:
    """测试连接四项：连通、鉴权、JSON 输出、工具调用。能力以实测为准。"""
    t0 = time.time()
    res = {"ok": False, "reachable": False, "auth_ok": False, "json": False, "tools": False, "error": None}
    if kind == "fake":
        await asyncio.sleep(0.2 * config.FAKE_LATENCY_SCALE)
        good = key.startswith("fake-") or key.startswith("sk-")
        res.update(reachable=True, auth_ok=good, json=good, tools=good and "no-tools" not in model_name,
                   ok=good, error=None if good else "AUTH_FAILED")
        res["latency_ms"] = int((time.time() - t0) * 1000)
        return res
    info = ModelInfo("probe", "byok", model_name, model_name, "openai", {"json"}, base_url=base_url, api_key=key)
    try:
        await asyncio.to_thread(_http_json, base_url.rstrip("/") + "/models", key, None, 15)
        res.update(reachable=True, auth_ok=True)
    except LLMError as e:
        res["reachable"] = e.code != "UNREACHABLE"
        if e.code in ("UNREACHABLE", "AUTH_FAILED"):
            res["error"] = e.code
            res["latency_ms"] = int((time.time() - t0) * 1000)
            return res
        res["auth_ok"] = True                      # 部分服务没有 /models，继续实测
    try:
        out, _, _ = await asyncio.to_thread(_openai_call, info, [
            {"role": "user", "content": 'Reply with JSON {"ok": true} only.'}], None, True, 30)
        json.loads(out["content"].strip().strip("`").removeprefix("json").strip())
        res["json"] = True
    except Exception as e:
        res["error"] = getattr(e, "code", "JSON_UNSUPPORTED")
    try:
        tool = [{"type": "function", "function": {"name": "ping", "description": "ping", "parameters": {
            "type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}}]
        out, _, _ = await asyncio.to_thread(_openai_call, info, [
            {"role": "user", "content": "Call the ping tool with x='1'."}], tool, False, 60)
        res["tools"] = bool(out["tool_calls"])
    except Exception:
        res["tools"] = False
    res["ok"] = res["auth_ok"] and res["json"]
    res["latency_ms"] = int((time.time() - t0) * 1000)
    return res
