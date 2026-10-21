"""应用入口：路由装配、统一错误格式、CSRF 防护、静态前端与启动恢复。

运行：  python -m uvicorn app.main:app --app-dir backend --port 8765
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, hardening
from .api import agents, other, tasks, wechat, teams
from .core import matching
from .services.runner import RUNNER
from .services import background
from .util import ApiError, redact


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    matching.seed_capabilities()
    RUNNER.recover()                     # INV-15：重启后把中断的执行重新排队
    if config.DEPLOY_MODE == "cloud" and not config.SECRETS_MASTER_KEY:
        logging.getLogger("combine").warning(
            "SECRETS_MASTER_KEY 未设置：API Key 将用数据目录中的 master.key 加密。生产环境请用 scripts/gen_master_key.py 生成并配置。")
    hk = asyncio.create_task(hardening.housekeeping_loop())     # 过期会话、登录失败、执行记录保留期
    try:
        yield
    finally:
        hk.cancel()
        await background.shutdown()
        if RUNNER._wake_timer:
            RUNNER._wake_timer.cancel()
        jobs = list(RUNNER.inflight.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(hk, *jobs, return_exceptions=True)


app = FastAPI(title="Combine 康拜因", version=config.VERSION, lifespan=lifespan)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """修改类请求必须带自定义头：浏览器跨站表单无法伪造，配合 SameSite=Lax Cookie 防 CSRF。
    同时做接口限流，并给所有响应加上安全头（CSP / 防点击劫持）。"""
    if request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and request.headers.get("x-combine") != "1":
        resp = JSONResponse({"error": {"code": "CSRF", "message": "缺少请求头 X-Combine"}}, status_code=403)
    elif (wait := hardening.check_rate(request)) is not None:
        resp = JSONResponse({"error": {"code": "RATE_LIMITED", "message": "操作过于频繁，请稍后再试",
                                       "details": {"retry_after": round(wait)}}},
                            status_code=429, headers={"Retry-After": str(round(wait))})
    else:
        resp = await call_next(request)
    hardening.security_headers(resp)
    return resp


@app.exception_handler(ApiError)
async def api_error(_: Request, e: ApiError):
    return JSONResponse({"error": {"code": e.code, "message": redact(e.message), "details": e.details}}, status_code=e.status)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, e: RequestValidationError):
    return JSONResponse({"error": {"code": "VALIDATION", "message": "请求参数不正确", "details": {"errors": str(e.errors())[:500]}}},
                        status_code=422)


@app.exception_handler(Exception)
async def internal_error(_: Request, e: Exception):
    logging.getLogger("combine").exception("unhandled error")
    # 云端版不把异常细节（可能含 SQL、路径）返回给浏览器；自托管版保留提示便于排查
    details = {} if config.DEPLOY_MODE == "cloud" else {"hint": redact(str(e))[:200]}
    return JSONResponse({"error": {"code": "INTERNAL", "message": "服务器内部错误", "details": details}}, status_code=500)


app.include_router(tasks.router)
app.include_router(other.router)
app.include_router(agents.router)
app.include_router(wechat.router)
app.include_router(teams.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    try:
        db.q1("SELECT 1")
        return {"ok": True, "fake_llm": config.FAKE_LLM}
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)


app.mount("/static", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")


@app.get("/{path:path}")
async def spa(path: str):
    """单页应用：除 /api 与 /static 外的路径都返回 index.html，由前端路由处理。"""
    if path.startswith("api/"):
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "接口不存在"}}, status_code=404)
    return FileResponse(str(config.FRONTEND_DIR / "index.html"))
