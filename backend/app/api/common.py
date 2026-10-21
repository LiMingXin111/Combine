"""鉴权依赖与请求体助手。"""
from __future__ import annotations

from fastapi import Request

from .. import config, db
from ..security import new_session_token, token_hash
from ..util import ApiError, now_ms

COOKIE = "cb_session"


def set_session_cookie(resp, user_id: str) -> None:
    """建立登录会话并下发 Cookie（密码登录与微信扫码登录共用）。"""
    tok, th = new_session_token()
    db.insert("sessions", {"token_hash": th, "user_id": user_id,
                           "expires_at": now_ms() + config.SESSION_DAYS * 86400_000, "created_at": now_ms()})
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                    max_age=config.SESSION_DAYS * 86400)


def public_user(u: dict) -> dict:
    """对外可见的用户字段（不含 password_hash 等内部字段）。"""
    return {k: u[k] for k in ("id", "email", "display_name", "is_admin", "created_at")} | {"is_admin": bool(u["is_admin"])}


def current_user(request: Request) -> dict:
    tok = request.cookies.get(COOKIE)
    if not tok:
        raise ApiError(401, "UNAUTHENTICATED", "请先登录")
    s = db.q1("SELECT user_id, expires_at FROM sessions WHERE token_hash=?", (token_hash(tok),))
    if not s or s["expires_at"] < now_ms():
        raise ApiError(401, "UNAUTHENTICATED", "登录已过期，请重新登录")
    u = db.q1("SELECT id, email, display_name, is_admin, quota_monthly_cny, created_at FROM users WHERE id=?", (s["user_id"],))
    if not u:
        raise ApiError(401, "UNAUTHENTICATED", "请先登录")
    return u


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def num(d: dict, key: str, default: float | None = None, lo: float | None = None, hi: float | None = None,
        integer: bool = False) -> float:
    """解析数值字段：缺省用 default；类型错误、NaN/Inf、越界一律 400 FIELD_INVALID。"""
    v = d.get(key)
    if v is None or v == "":
        if default is None:
            raise ApiError(400, "FIELD_REQUIRED", f"缺少字段 {key}", {"field": key})
        v = default
    if isinstance(v, bool):
        raise ApiError(400, "FIELD_INVALID", f"字段 {key} 应为数字", {"field": key})
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, "FIELD_INVALID", f"字段 {key} 应为数字", {"field": key})
    if f != f or f in (float("inf"), float("-inf")) or (lo is not None and f < lo) or (hi is not None and f > hi):
        raise ApiError(400, "FIELD_INVALID", f"字段 {key} 超出允许范围", {"field": key, "min": lo, "max": hi})
    return int(f) if integer else f


def opt_str(d: dict, key: str, max_len: int) -> str | None:
    """可选文本字段：非文本 → 400；超长截断。"""
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ApiError(400, "FIELD_INVALID", f"字段 {key} 应为文本", {"field": key})
    v = v.strip()
    return v[:max_len] if v else None


def req_str(d: dict, key: str, max_len: int = 4000, required: bool = True) -> str:
    v = d.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ApiError(400, "FIELD_REQUIRED", f"缺少字段 {key}", {"field": key})
        return ""
    if not isinstance(v, str):
        raise ApiError(400, "FIELD_INVALID", f"字段 {key} 应为文本", {"field": key})
    if len(v) > max_len:
        raise ApiError(400, "FIELD_TOO_LONG", f"字段 {key} 过长（上限 {max_len} 字）", {"field": key})
    return v.strip()
