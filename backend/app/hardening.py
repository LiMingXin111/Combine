"""上线加固：接口限流、安全响应头与定期数据维护。

- 限流：进程内滑动窗口，按「客户端 IP × 规则」计数。单实例部署足够；多实例需换成 Redis 等共享存储。
  在反向代理之后运行时，IP 取自 X-Forwarded-For 的第一个地址（仅当 TRUST_PROXY=1）。
- 安全头：CSP 禁止内联脚本与外部脚本，即使前端出现注入点也无法执行脚本；frame-ancestors 防点击劫持。
- 数据维护：清理过期会话与登录失败记录；按每个用户的「执行记录保留天数」删除已结束任务的事件（F26 隐私设置）。
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from . import config, db, events
from .util import now_ms

# (规则名, 路径前缀, 方法集合, 窗口秒, 次数上限)。按顺序匹配第一条。
RULES = [
    # 扫码轮询是每 2 秒一次的高频只读请求，必须排在微信规则之前给它单独额度
    ("wechat_poll", ("/api/auth/wechat/poll",), {"GET"}, 60, 240),
    ("wechat_qr", ("/api/auth/wechat/",), {"GET", "POST"}, 60, 60),          # 出码 / 回调 / 模拟授权页
    ("auth", ("/api/auth/login", "/api/auth/register"), {"POST"}, 60, 20),
    ("expensive", ("/api/tasks", "/api/uploads", "/api/kb/documents/upload", "/api/kb/documents/clip",
                   "/api/providers"), {"POST"}, 60, 60),
    ("write", ("/api/",), {"POST", "PUT", "PATCH", "DELETE"}, 60, 600),
]
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def client_ip(request) -> str:
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rule_for(path: str, method: str):
    for name, prefixes, methods, win, cap in RULES:
        if method not in methods or not path.startswith(prefixes):
            continue
        # expensive 规则只针对「创建」类入口：/api/tasks 精确匹配 + 任务下的重新规划 / 升级 / 服务商测试
        if name == "expensive" and not (path in prefixes or path.endswith(("/replan", "/escalate", "/test"))):
            continue
        return name, win, cap
    return None


def check_rate(request) -> float | None:
    """命中限流时返回建议的重试秒数，否则记录本次请求并返回 None。"""
    if not config.RATE_LIMIT:
        return None
    r = _rule_for(request.url.path, request.method)
    if not r:
        return None
    name, win, cap = r
    key, now = (client_ip(request), name), time.monotonic()
    q = _hits[key]
    while q and q[0] <= now - win:
        q.popleft()
    if len(q) >= cap:
        return max(1.0, win - (now - q[0]))
    q.append(now)
    return None


def security_headers(resp) -> None:
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                 "img-src 'self' data: https://*.is.autonavi.com https://tile.openstreetmap.org; "
                 "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    if config.COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")


# ---------------- 定期维护 ----------------
def housekeeping() -> dict:
    """可重复执行、幂等。返回各项清理数量，便于管理接口与测试观察。"""
    now = now_ms()
    out = {"sessions": db.ex("DELETE FROM sessions WHERE expires_at<?", (now,)),
           "login_failures": db.ex("DELETE FROM login_failures WHERE ts<?", (now - 86400_000,)),
           "events": 0}
    from .services import settings as settings_svc
    from .services import wechat as wechat_svc
    for u in db.q("SELECT id FROM users"):
        days = settings_svc.get(u["id"]).get("privacy.event_retention_days", 180)
        cutoff = now - int(days) * 86400_000
        out["events"] += db.ex(
            "DELETE FROM events WHERE task_id IN (SELECT id FROM tasks WHERE user_id=? AND status IN "
            "('completed','cancelled','failed') AND updated_at<?)", (u["id"], cutoff))
    for k in [k for k, q in _hits.items() if not q or q[-1] < time.monotonic() - 3600]:
        _hits.pop(k, None)                                    # 限流计数器不无限增长
    out["auth_tickets"] = wechat_svc.purge()                   # 过期扫码票据（F30）
    events.prune_empty()
    return out


async def housekeeping_loop(interval_s: float = 6 * 3600) -> None:
    while True:
        try:
            housekeeping()
        except Exception:      # noqa: BLE001  维护失败不影响服务，下一轮再试
            pass
        await asyncio.sleep(interval_s)
