"""微信扫码登录接口（F30）：出码 → 手机授权 → PC 轮询取登录态。

四个入口：
  GET /api/auth/wechat/start          取二维码链接与 state（把 state 同时写入 HttpOnly Cookie，
                                      使票据绑定到发起扫码的那个浏览器，防会话固定）
  GET /api/auth/wechat/poll            PC 端轮询；票据 confirmed 时建立会话并一次性消费
  GET /api/auth/wechat/callback        手机端回调（微信 302 到这里），换 openid 并绑定/建号
  GET /api/auth/wechat/mock/authorize  仅 fake 模式的本地模拟授权页（无资质也能跑通全链路）

回调与模拟页都返回自包含的静态 HTML（无内联脚本，CSP 下可正常渲染），手机端看完即可离开。
"""
from __future__ import annotations

import asyncio
import html
import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import config, db
from ..services import wechat
from ..util import ApiError, now_ms
from .common import public_user, set_session_cookie

router = APIRouter(prefix="/api")

WX_STATE_COOKIE = "cb_wx_state"

_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 康拜因</title><style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:#0e1016;color:#e8eaf0;
font:15px/1.6 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}}
.card{{width:min(340px,100%);background:#161923;border:1px solid #262b38;border-radius:14px;padding:24px 20px;text-align:center}}
.mark{{width:44px;height:44px;margin:0 auto 14px;border-radius:12px;display:grid;place-items:center;font-size:20px;font-weight:700}}
h1{{font-size:17px;margin:0 0 8px}} p{{margin:0;color:#99a0b0;font-size:13.5px}}
.btn{{display:inline-block;margin-top:18px;padding:10px 22px;border:0;border-radius:9px;background:#5b5bd6;color:#fff;
font-size:14px;cursor:pointer}} .btn:active{{opacity:.85}}
.tip{{margin-top:14px;font-size:12px;color:#6b7383}}
</style></head><body><div class="card"><div class="mark" style="background:{markbg};color:{markfg}">{mark}</div>
<h1>{title}</h1><p>{msg}</p>{extra}</div></body></html>"""

_MARK = {"ok": ("✓", "#1c3b2a", "#4ade80"), "err": ("×", "#3b1c22", "#f87171"), "info": ("i", "#1e2740", "#8ab4ff")}


def _page(title: str, msg: str, kind: str = "info", extra: str = "") -> HTMLResponse:
    mark, bg, fg = _MARK.get(kind, _MARK["info"])
    return HTMLResponse(_PAGE.format(title=html.escape(title), msg=html.escape(msg), extra=extra,
                                     mark=mark, markbg=bg, markfg=fg))


@router.get("/auth/wechat/start")
async def start(request: Request):
    """出码：未配置开放平台参数时返回 enabled=false，前端据此隐藏扫码入口。"""
    if not wechat.enabled():
        return {"enabled": False, "mode": config.WECHAT_MODE}
    t = wechat.new_ticket(request.query_params.get("next"))
    resp = JSONResponse({"enabled": True, "mode": config.WECHAT_MODE, "state": t["id"],
                         "qr_url": wechat.qr_target(t["id"], str(request.base_url)),
                         "expires_in": config.WECHAT_TICKET_TTL_S, "next": t["next"]})
    resp.set_cookie(WX_STATE_COOKIE, t["id"], httponly=True, samesite="lax",
                    secure=config.COOKIE_SECURE, max_age=config.WECHAT_TICKET_TTL_S)
    return resp


@router.get("/auth/wechat/poll")
async def poll(request: Request):
    """PC 端轮询。status：pending | ok（已建立会话）| expired | consumed。"""
    tid = request.query_params.get("id") or ""
    t = wechat.get_ticket(tid)
    if not t or wechat.is_expired(t):
        return {"status": "expired"}
    if t["status"] == "consumed":
        return {"status": "consumed"}
    if t["status"] != "confirmed":
        return {"status": "pending", "expires_in": max(0, (int(t["expires_at"]) - now_ms()) // 1000)}
    if request.cookies.get(WX_STATE_COOKIE) != tid:
        # 票据不是本次浏览器发起的：不发放会话（防会话固定），按过期处理让用户重新扫码
        return {"status": "expired"}
    uid = wechat.consume(tid)
    u = db.q1("SELECT * FROM users WHERE id=?", (uid,)) if uid else None
    if not u:
        return {"status": "consumed"}
    resp = JSONResponse({"status": "ok", "user": public_user(u), "next": t["next"]})
    set_session_cookie(resp, u["id"])
    return resp


@router.get("/auth/wechat/callback", response_class=HTMLResponse)
async def callback(request: Request):
    """手机端回调：用 code 换 openid，绑定或新建本地账号，并把票据置为可取件。"""
    qp = request.query_params
    code, state = qp.get("code", ""), qp.get("state", "")
    t = wechat.get_ticket(state)
    if not t or wechat.is_expired(t) or t["status"] != "pending":
        return _page("授权链接已失效", "请回到电脑上刷新二维码后重新扫码。", "err")
    if not code:
        return _page("未获得授权", "微信没有返回授权码，请重新扫码。", "err")
    try:
        tok = await asyncio.to_thread(wechat.exchange_code, code)
        prof = await asyncio.to_thread(wechat.fetch_profile, tok["access_token"], tok["openid"])
        u = wechat.login_or_create(wechat.appid(), tok["openid"], tok["unionid"], prof)
    except ApiError as e:
        return _page("登录失败", e.message, "err")
    if not wechat.confirm(state, u["id"]):
        return _page("授权链接已失效", "请回到电脑上刷新二维码后重新扫码。", "err")
    return _page("授权成功", "已登录。请回到电脑上的康拜因页面，无需其他操作。", "ok")


@router.get("/auth/wechat/mock/authorize", response_class=HTMLResponse)
async def mock_authorize(request: Request):
    """仅 fake 模式的本地模拟授权页：等价于真机上用户扫码后看到的微信授权页。
    可选 `as=<身份名>` 固定模拟身份，便于复现「同一微信多次登录落在同一账号」。"""
    if config.WECHAT_MODE != "fake":
        raise ApiError(404, "NOT_FOUND", "接口不存在")
    qp = request.query_params
    t = wechat.get_ticket(qp.get("state", ""))
    if not t or wechat.is_expired(t) or t["status"] != "pending":
        return _page("二维码已失效", "请回到电脑上刷新二维码后重试。", "err")
    ident = re.sub(r"[^0-9A-Za-z_-]", "", qp.get("as", ""))[:32] or secrets.token_hex(4)
    action = html.escape(str(request.base_url).rstrip("/") + "/api/auth/wechat/callback")
    form = (f'<form method="get" action="{action}">'
            f'<input type="hidden" name="state" value="{html.escape(t["id"])}">'
            f'<input type="hidden" name="code" value="{html.escape("mock_" + ident)}">'
            f'<button class="btn" type="submit">授权并登录</button></form>'
            f'<div class="tip">本地模拟授权页（WECHAT_MODE=fake），不连接微信服务器</div>')
    return _page("模拟微信授权", "真机上这一步由微信提供；此处用于无 AppID 时演示与自动化测试。", "info", form)
