"""微信开放平台「网站应用」扫码登录（F30）。

真机链路（WECHAT_MODE=live）：
  1. PC 端 `GET /api/auth/wechat/start` 取 state 与授权链接，前端把链接画成二维码；
  2. 用户用手机微信「扫一扫」→ 微信授权页 → 微信 302 到 WECHAT_REDIRECT_URI；
  3. 手机浏览器请求 `GET /api/auth/wechat/callback?code=&state=` → 本模块用 code 换 openid
     （+ unionid），绑定或新建本地账号，并把该 state 票据置为 confirmed；
  4. PC 端轮询 `GET /api/auth/wechat/poll?id=<state>` → 取到用户并建立会话（票据一次性消费）。

无资质自证（WECHAT_MODE=fake）：二维码指向本服务内置的模拟授权页，code 形如 `mock_xxx`，
全程不发起任何外部请求；state 校验、建号、轮询登录等链路与真机完全一致，
因此自动化测试与无 AppID 的演示都能端到端跑通。

安全：state 是一次性随机票据（默认 30 分钟有效、用后即焚）；登录后的跳转只允许站内路径。
"""
from __future__ import annotations

import json
import secrets
import urllib.parse
import urllib.request

from .. import config, db
from ..security import hash_password
from ..util import ApiError, clip, new_id, now_ms, sha256

WECHAT_QR_URL = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"
HTTP_TIMEOUT_S = 10


def enabled() -> bool:
    """是否启用扫码登录。判断一律走当前配置值，不缓存快照（配置可在运行时被改写）。
    fake 是本地模拟授权页，无需任何凭据；live 必须齐备 AppID / Secret / 回调地址。"""
    if config.WECHAT_MODE == "off":
        return False
    if config.WECHAT_MODE == "live":
        return bool(config.WECHAT_APPID and config.WECHAT_SECRET and config.WECHAT_REDIRECT_URI)
    return True


def appid() -> str:
    """当前生效的 AppID。fake 模式用占位值，让身份表始终有稳定的「应用」维度键。"""
    return config.WECHAT_APPID or ("fake_appid" if config.WECHAT_MODE == "fake" else "")


# ---------------- 授权地址 ----------------
def _redirect_uri(request_base: str) -> str:
    """回调地址：真机必须是开放平台后台已登记的域名（配置值优先）；
    fake 模式或未配置时退回当前站点自身的回调。"""
    if config.WECHAT_MODE == "live":
        return config.WECHAT_REDIRECT_URI
    return (config.WECHAT_REDIRECT_URI or request_base.rstrip("/") + "/api/auth/wechat/callback")


def authorize_url(state: str, redirect_uri: str) -> str:
    q = urllib.parse.urlencode({
        "appid": config.WECHAT_APPID, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": "snsapi_login", "state": state,
    })
    return f"{WECHAT_QR_URL}?{q}#wechat_redirect"


def qr_target(state: str, request_base: str) -> str:
    """二维码内容：真机 = 微信授权页；fake = 本服务模拟授权页。前端不区分二者。"""
    if config.WECHAT_MODE == "fake":
        return f"{request_base.rstrip('/')}/api/auth/wechat/mock/authorize?state={urllib.parse.quote(state)}"
    return authorize_url(state, _redirect_uri(request_base))


def safe_next(next_path: str | None) -> str:
    """只允许站内相对路径，防止开放重定向（`//evil.com` 与绝对 URL 一律丢弃）。"""
    p = (next_path or "").strip()
    if not p.startswith("/") or p.startswith("//") or p.startswith("/\\"):
        return "/"
    return p[:400]


# ---------------- 与微信服务器交互（均在 to_thread 中调用，不阻塞事件循环） ----------------
def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Combine/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        raw = r.read(256 * 1024)
    return json.loads(raw.decode("utf-8", "replace"))


def exchange_code(code: str) -> dict:
    """code → {openid, unionid, access_token}。失败抛 WECHAT_CODE_INVALID。"""
    if config.WECHAT_MODE == "fake":
        # mock_<身份> → 确定性 openid：同一身份多次登录会命中同一账号，便于测试与演示。
        ident = code[5:] if code.startswith("mock_") else code
        if not ident:
            raise ApiError(400, "WECHAT_CODE_INVALID", "微信授权失败：缺少 code")
        return {"openid": f"mock_openid_{ident}", "unionid": f"mock_union_{ident}", "access_token": "mock_token"}
    url = WECHAT_TOKEN_URL + "?" + urllib.parse.urlencode({
        "appid": config.WECHAT_APPID, "secret": config.WECHAT_SECRET,
        "code": code, "grant_type": "authorization_code",
    })
    try:
        d = _get_json(url)
    except Exception as e:                                      # noqa: BLE001
        raise ApiError(502, "WECHAT_UNREACHABLE", f"无法连接微信服务器：{e}")
    if d.get("errcode") or not d.get("openid"):
        raise ApiError(400, "WECHAT_CODE_INVALID", f"微信授权失败：{d.get('errmsg') or 'code 无效或已过期'}")
    return {"openid": d["openid"], "unionid": d.get("unionid") or "", "access_token": d.get("access_token") or ""}


def fetch_profile(access_token: str, openid: str) -> dict:
    """拉取昵称与头像；失败不影响登录（返回空资料）。"""
    if config.WECHAT_MODE == "fake":
        return {"nickname": "", "avatar": ""}
    url = WECHAT_USERINFO_URL + "?" + urllib.parse.urlencode(
        {"access_token": access_token, "openid": openid, "lang": "zh_CN"})
    try:
        d = _get_json(url)
    except Exception:                                           # noqa: BLE001
        return {"nickname": "", "avatar": ""}
    if d.get("errcode"):
        return {"nickname": "", "avatar": ""}
    return {"nickname": clip(d.get("nickname") or "", 40), "avatar": d.get("headimgurl") or ""}


# ---------------- 扫码票据（state，防 CSRF + PC 端取件） ----------------
def new_ticket(next_path: str) -> dict:
    purge()
    t = {"id": secrets.token_urlsafe(24), "provider": "wechat", "status": "pending",
         "user_id": None, "next": safe_next(next_path), "created_at": now_ms(),
         "expires_at": now_ms() + config.WECHAT_TICKET_TTL_S * 1000}
    db.insert("auth_tickets", t)
    return t


def get_ticket(tid: str) -> dict | None:
    return db.q1("SELECT * FROM auth_tickets WHERE id=?", (tid,))


def is_expired(t: dict) -> bool:
    return int(t["expires_at"]) < now_ms()


def confirm(tid: str, user_id: str) -> bool:
    """手机端授权成功：把票据置为待取件。已过期 / 已用过的票据一律拒绝。"""
    return db.ex("UPDATE auth_tickets SET status='confirmed', user_id=? "
                 "WHERE id=? AND status='pending' AND expires_at>=?",
                 (user_id, tid, now_ms())) > 0


def consume(tid: str) -> str | None:
    """PC 端取走登录态（一次性）：成功返回 user_id；已被取走或已过期返回 None。"""
    t = db.q1("SELECT user_id, status, expires_at FROM auth_tickets WHERE id=?", (tid,))
    if not t or t["status"] != "confirmed" or int(t["expires_at"]) < now_ms():
        return None
    if db.ex("UPDATE auth_tickets SET status='consumed' WHERE id=? AND status='confirmed'", (tid,)) <= 0:
        return None
    return t["user_id"]


def purge() -> int:
    """清理过期票据（由 hardening.housekeeping 周期调用，也可在开新票据时顺手清）。"""
    return db.ex("DELETE FROM auth_tickets WHERE expires_at<?", (now_ms(),))


# ---------------- 账号：绑定或新建 ----------------
def login_or_create(appid: str, openid: str, unionid: str, profile: dict) -> dict:
    ident = db.q1("SELECT * FROM wechat_identities WHERE appid=? AND openid=?", (appid, openid))
    if not ident and unionid:
        # unionid 优先：同一开放平台账号下，用户换应用也能落到同一个本地账号
        ident = db.q1("SELECT * FROM wechat_identities WHERE unionid=? LIMIT 1", (unionid,))
    if ident:
        u = db.q1("SELECT * FROM users WHERE id=?", (ident["user_id"],))
        if u:
            if profile.get("nickname") or profile.get("avatar"):
                db.update("wechat_identities", {"id": ident["id"]},
                          {"nickname": profile.get("nickname") or "", "avatar": profile.get("avatar") or ""})
            return u
    if config.REGISTRATION_MODE == "closed" and db.q1("SELECT id FROM users LIMIT 1"):
        raise ApiError(403, "REGISTRATION_CLOSED", "当前不开放注册")
    first = db.q1("SELECT id FROM users LIMIT 1") is None
    uid = new_id("usr")
    # 微信账号没有邮箱：用 openid 派生一个稳定且唯一的占位邮箱；密码为随机值（该账号默认不可用密码登录）
    email = f"wx_{sha256(appid + '|' + openid)[:16]}@wechat.local"
    db.insert("users", {"id": uid, "email": email, "password_hash": hash_password(secrets.token_urlsafe(32)),
                        "display_name": clip(profile.get("nickname") or "微信用户", 40), "is_admin": int(first),
                        "quota_monthly_cny": config.DEFAULT_MONTHLY_QUOTA_CNY, "created_at": now_ms()})
    db.insert("wechat_identities", {"id": new_id("wxi"), "user_id": uid, "appid": appid, "openid": openid,
                                    "unionid": unionid or "", "nickname": profile.get("nickname") or "",
                                    "avatar": profile.get("avatar") or "", "created_at": now_ms()})
    return db.q1("SELECT * FROM users WHERE id=?", (uid,))
