"""账户、知识库、工作流、偏好、能力库、通知、设置、模型服务与管理接口。"""
from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
import time
import urllib.request
from urllib.parse import quote

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .. import config, db, events
from ..core import matching
from ..llm import gateway
from ..security import encrypt_secret, hash_password, key_hint, token_hash, verify_password
from ..services import kb, plans, store, workspace
from ..services import settings as settings_svc
from ..services.runner import RUNNER
from ..util import ApiError, clip, new_id, now_ms, register_secret, tokenize
from .common import COOKIE, body, current_user, num, opt_str, public_user, req_str, set_session_cookie

router = APIRouter(prefix="/api")
_delete_tokens: dict[str, tuple[str, float]] = {}
_DUMMY_HASH = hash_password(secrets.token_hex(8))          # 用于登录时的等时比较


# ======================= 账户（F01） =======================
@router.get("/meta")
async def meta():
    return {"deploy_mode": config.DEPLOY_MODE, "byok_enabled": True, "local_models_enabled": config.DEPLOY_MODE == "self_hosted",
            "registration_mode": config.REGISTRATION_MODE, "languages": ["zh-CN", "en-US"], "version": config.VERSION,
            "fake_llm": config.FAKE_LLM, "team_spaces": {"version": 1, "template_schema": "combine.workflow-template/v1"}}


@router.post("/auth/register")
async def register(request: Request):
    d = await body(request)
    if config.REGISTRATION_MODE == "closed" and db.q1("SELECT id FROM users LIMIT 1"):
        raise ApiError(403, "REGISTRATION_CLOSED", "当前不开放注册")
    email = req_str(d, "email", 120).lower()
    pw = req_str(d, "password", 128)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError(400, "EMAIL_INVALID", "邮箱格式不正确")
    if len(pw) < 8:
        raise ApiError(400, "PASSWORD_WEAK", "密码至少 8 位")
    if db.q1("SELECT id FROM users WHERE email=?", (email,)):
        raise ApiError(409, "EMAIL_TAKEN", "该邮箱已注册")
    first = db.q1("SELECT id FROM users LIMIT 1") is None
    u = {"id": new_id("usr"), "email": email, "password_hash": hash_password(pw),
         "display_name": clip(opt_str(d, "display_name", 40) or email.split("@")[0], 40), "is_admin": int(first),
         "quota_monthly_cny": config.DEFAULT_MONTHLY_QUOTA_CNY, "created_at": now_ms()}
    db.insert("users", u)
    resp = JSONResponse({"user": public_user(u)})
    set_session_cookie(resp, u["id"])
    return resp


@router.post("/auth/login")
async def login(request: Request):
    d = await body(request)
    email = req_str(d, "email", 120).lower()
    pw = req_str(d, "password", 128)
    window = now_ms() - 10 * 60_000
    if db.q1("SELECT COUNT(*) AS n FROM login_failures WHERE email=? AND ts>?", (email, window))["n"] >= 5:
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "尝试次数过多，请 10 分钟后再试")
    u = db.q1("SELECT * FROM users WHERE email=?", (email,))
    ok = verify_password(pw, u["password_hash"] if u else _DUMMY_HASH)
    if not u or not ok:
        db.insert("login_failures", {"email": email, "ts": now_ms()})
        raise ApiError(401, "LOGIN_FAILED", "邮箱或密码不正确")
    db.ex("DELETE FROM login_failures WHERE email=?", (email,))
    resp = JSONResponse({"user": public_user(u)})
    set_session_cookie(resp, u["id"])
    return resp


@router.post("/auth/logout")
async def logout(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok:
        db.ex("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/me")
async def me(request: Request):
    u = current_user(request)
    w = matching.weights_for(u["id"])
    return {"user": public_user(u), "quota": {"monthly": u["quota_monthly_cny"], "used": round(gateway.month_used(u["id"]), 4)},
            "weights": {k: w[k] for k in ("w_tag", "w_tool", "w_perm", "w_cost", "sample_count", "version")}}


@router.patch("/me")
async def patch_me(request: Request):
    u = current_user(request)
    d = await body(request)
    db.update("users", {"id": u["id"]}, {"display_name": req_str(d, "display_name", 40)})
    return {"ok": True}


@router.post("/me/password")
async def change_pw(request: Request):
    u = current_user(request)
    d = await body(request)
    row = db.q1("SELECT password_hash FROM users WHERE id=?", (u["id"],))
    if not verify_password(req_str(d, "current", 128), row["password_hash"]):
        raise ApiError(400, "LOGIN_FAILED", "当前密码不正确")
    nxt = req_str(d, "next", 128)
    if len(nxt) < 8:
        raise ApiError(400, "PASSWORD_WEAK", "密码至少 8 位")
    db.update("users", {"id": u["id"]}, {"password_hash": hash_password(nxt)})
    return {"ok": True}


@router.get("/me/data-summary")
async def data_summary(request: Request):
    u = current_user(request)
    cnt = lambda t, c="user_id": db.q1(f"SELECT COUNT(*) AS n FROM {t} WHERE {c}=?", (u["id"],))["n"]  # noqa: E731
    return {"tasks": cnt("tasks"), "kb_documents": cnt("kb_documents"), "kb_chunks": cnt("kb_chunks"), "playbooks": cnt("playbooks"),
            "preferences": cnt("preferences"), "providers": cnt("provider_configs"), "llm_calls": cnt("llm_calls")}


@router.get("/me/delete-token")
async def delete_token(request: Request):
    u = current_user(request)
    tok = secrets.token_hex(4)
    for k in [k for k, (_, exp) in _delete_tokens.items() if exp < time.time()]:
        _delete_tokens.pop(k, None)                          # 清理过期确认码
    _delete_tokens[u["id"]] = (tok, time.time() + 300)
    return {"confirm_token": tok, "expires_in": 300}


@router.post("/me/delete")
async def delete_me(request: Request):
    u = current_user(request)
    d = await body(request)
    tok, exp = _delete_tokens.pop(u["id"], ("", 0))
    if not tok or d.get("confirm_token") != tok or time.time() > exp:
        raise ApiError(400, "CONFIRM_REQUIRED", "确认令牌无效或已过期")
    for t in db.q("SELECT id FROM tasks WHERE user_id=?", (u["id"],)):
        RUNNER.cancel(t["id"])
    workspace.delete_user_files(u["id"])
    with db.Tx():
        for table in ("agents", "agent_credentials", "agent_calls", "llm_calls", "canvas_layouts"):
            db.ex(f"DELETE FROM {table} WHERE user_id=?", (u["id"],))
        db.ex("DELETE FROM auth_tickets WHERE user_id=?", (u["id"],))
        db.ex("DELETE FROM login_failures WHERE email=?", (u["email"],))
        db.ex("DELETE FROM users WHERE id=?", (u["id"],))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/me/export")
async def export_me(request: Request):
    """导出全部个人数据。服务商只导出名称与地址，不含任何 Key 原文或密文（INV-18）。"""
    u = current_user(request)
    uid = u["id"]
    data = {"user": public_user(u), "settings": settings_svc.get(uid),
            "tasks": db.q("SELECT * FROM tasks WHERE user_id=?", (uid,)),
            "artifacts": db.q("SELECT a.* FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE t.user_id=?", (uid,)),
            "kb_documents": db.q("SELECT id,title,source,tags,content,created_at FROM kb_documents WHERE user_id=?", (uid,)),
            "playbooks": db.q("SELECT * FROM playbooks WHERE user_id=?", (uid,)),
            "preferences": db.q("SELECT * FROM preferences WHERE user_id=?", (uid,)),
            "providers": db.q("SELECT id,kind,display_name,base_url,status,created_at FROM provider_configs WHERE user_id=?", (uid,))}
    return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=combine-export.json"})


# ======================= 知识库（F17） =======================
ALLOWED_EXT = (".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".log")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ApiError(400, "UPLOAD_DECODE", "无法识别文件编码，请转为 UTF-8")


def _strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n\n", t)
    return html.unescape(re.sub(r"<[^>]+>", " ", t))


async def _upload(request: Request, file: UploadFile, source: str):
    u = current_user(request)
    name = (file.filename or "upload.txt").strip()
    if not name.lower().endswith(ALLOWED_EXT):
        raise ApiError(400, "UPLOAD_TYPE", "首版支持 txt / md / csv / json / html 文本文件")
    raw = await file.read(config.UPLOAD_MAX_BYTES + 1)
    if len(raw) > config.UPLOAD_MAX_BYTES:
        raise ApiError(413, "UPLOAD_TOO_LARGE", f"文件超过 {config.UPLOAD_MAX_BYTES // 1024 // 1024} MB")
    text = _decode(raw)
    if name.lower().endswith((".html", ".htm")):
        text = _strip_html(text)
    doc = kb.add_document(u["id"], re.sub(r"\.\w+$", "", name), text, source)
    return {"document": doc, "attachment_id": doc["id"], "filename": name, "size_bytes": len(raw), "text_chars": len(text)}


@router.post("/kb/documents/upload")
async def kb_upload(request: Request, file: UploadFile):
    return await _upload(request, file, "upload")


@router.post("/uploads")
async def task_upload(request: Request, file: UploadFile):
    """任务附件：同时存入知识库（与原型一致），并作为 reference.read 的读取范围。"""
    return await _upload(request, file, "attachment")


@router.post("/kb/documents/note")
async def kb_note(request: Request):
    u = current_user(request)
    d = await body(request)
    tags = d.get("tags") if isinstance(d.get("tags"), list) else []
    return {"document": kb.add_document(u["id"], req_str(d, "title", 120), req_str(d, "text", 200_000), "note",
                                        [clip(str(t), 20) for t in tags][:10])}


@router.post("/kb/documents/clip")
async def kb_clip(request: Request):
    u = current_user(request)
    d = await body(request)
    url = req_str(d, "url", 2000)
    text = opt_str(d, "text", 500_000)
    if not text:
        gateway.check_base_url(url, "byok")              # 云端版禁止抓取内网地址（SSRF 防护）

        def fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "CombineClipper/1.0"})
            with gateway.safe_urlopen(req, 10) as r:
                return r.read(1_000_000).decode("utf-8", "ignore")
        try:
            raw = await asyncio.to_thread(fetch)
        except Exception:
            raise ApiError(400, "CLIP_FAILED", "无法抓取该网页，可直接粘贴正文")
        m = re.search(r"(?is)<title>(.*?)</title>", raw)
        title = opt_str(d, "title", 120) or (html.unescape(m.group(1)).strip()[:120] if m else url[:120])
        text = _strip_html(raw)
    else:
        title = opt_str(d, "title", 120) or url[:120]
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
    return {"document": kb.add_document(u["id"], title, f"来源：{url}\n\n{text}", "clip")}


@router.get("/kb/documents")
async def kb_list(request: Request, q: str = "", limit: int = 200):
    u = current_user(request)
    sql, p = "SELECT * FROM kb_documents WHERE user_id=?", [u["id"]]
    if q:
        sql += " AND (title LIKE ? OR content LIKE ?)"
        p += [f"%{q}%", f"%{q}%"]
    return {"items": [kb.public_doc(d) for d in db.q(sql + " ORDER BY updated_at DESC LIMIT ?", p + [min(500, max(1, limit))])]}


@router.get("/kb/documents/{did}")
async def kb_get(request: Request, did: str):
    u = current_user(request)
    d = db.q1("SELECT * FROM kb_documents WHERE id=? AND user_id=?", (did, u["id"]))
    if not d:
        raise ApiError(404, "NOT_FOUND", "资料不存在")
    chunks = db.q("SELECT id, idx, heading, text FROM kb_chunks WHERE doc_id=? ORDER BY idx LIMIT 50", (did,))
    return {"document": kb.public_doc(d) | {"content": d["content"][:20000]}, "chunks": chunks}


@router.patch("/kb/documents/{did}")
async def kb_patch(request: Request, did: str):
    u = current_user(request)
    d = await body(request)
    vals = {}
    if "title" in d:
        vals["title"] = req_str(d, "title", 120)
    if "tags" in d and isinstance(d["tags"], list):
        vals["tags"] = [clip(str(t), 20) for t in d["tags"]][:10]
    if not vals or not db.update("kb_documents", {"id": did, "user_id": u["id"]}, vals | {"updated_at": now_ms()}):
        raise ApiError(404, "NOT_FOUND", "资料不存在")
    return {"ok": True}


@router.delete("/kb/documents/{did}")
async def kb_delete(request: Request, did: str):
    u = current_user(request)
    if not db.ex("DELETE FROM kb_documents WHERE id=? AND user_id=?", (did, u["id"])):   # 分块级联删除
        raise ApiError(404, "NOT_FOUND", "资料不存在")
    return {"ok": True}


@router.post("/kb/documents/{did}/reindex")
async def kb_reindex(request: Request, did: str):
    u = current_user(request)
    return {"chunks": kb.reindex(u["id"], did)}


@router.post("/kb/search")
async def kb_search(request: Request):
    u = current_user(request)
    d = await body(request)
    return {"items": kb.search(u["id"], req_str(d, "query", 500), num(d, "top_k", 8, 1, 20, integer=True))}


@router.get("/kb/stats")
async def kb_stats(request: Request):
    u = current_user(request)
    r = db.q1("SELECT COUNT(*) AS docs, COALESCE(SUM(chunk_count),0) AS chunks, COALESCE(SUM(LENGTH(content)),0) AS chars, "
              "COALESCE(SUM(cited_count),0) AS cited FROM kb_documents WHERE user_id=?", (u["id"],))
    return {"documents": r["docs"], "chunks": r["chunks"], "chars": r["chars"], "cited": r["cited"],
            "embed_model": "bm25 (offline)", "vector_enabled": False}


# ======================= 个人工作流（F19） =======================
TEMPLATE_KEYS = ("node_key", "title", "description", "acceptance", "depends_on", "input_refs", "produces_sections",
                 "capability_requirements", "est_complexity", "model_tier", "requires_approval")


@router.post("/tasks/{task_id}/playbook/draft")
async def playbook_draft(request: Request, task_id: str):
    u = current_user(request)
    t = store.get_task(task_id, u["id"])
    pv = store.active_pv(t)
    if not pv:
        raise ApiError(409, "STATE_CONFLICT", "任务还没有已确认的计划")
    nodes = [{k: n[k] for k in TEMPLATE_KEYS} for n in store.load_nodes(pv["id"])]
    return {"name": t["title"], "description": t["goal"][:200], "sample_goal": t["goal"], "template_nodes": nodes}


@router.post("/playbooks")
async def playbook_save(request: Request):
    u = current_user(request)
    d = await body(request)
    nodes = d.get("template_nodes")
    if not d.get("task_id") and not nodes:
        raise ApiError(400, "FIELD_REQUIRED", "缺少任务图")
    if d.get("task_id"):
        t = store.get_task(str(d["task_id"]), u["id"])
        pv = store.active_pv(t)
        if not pv:
            raise ApiError(409, "STATE_CONFLICT", "任务还没有已确认的计划")
        nodes = store.load_nodes(pv["id"])
        sample = t["goal"]
    else:
        sample = req_str(d, "sample_goal", 4000)
    nodes = [{k: n.get(k) for k in TEMPLATE_KEYS} for n in plans.normalize(nodes, u["id"], "zh")]   # 结构校验 + 清洗
    pb = {"id": new_id("pb"), "user_id": u["id"], "name": req_str(d, "name", 60), "description": clip(d.get("description") or "", 300),
          "sample_goal": sample, "template_nodes": nodes, "source_task_id": d.get("task_id"), "created_at": now_ms(), "updated_at": now_ms()}
    db.insert("playbooks", pb)
    return {"playbook": {k: v for k, v in pb.items() if k != "template_nodes"} | {"steps": len(nodes)}}


@router.get("/playbooks")
async def playbook_list(request: Request):
    u = current_user(request)
    rows = db.q("SELECT * FROM playbooks WHERE user_id=? ORDER BY updated_at DESC", (u["id"],))
    for r in rows:
        nodes = db.j(r.pop("template_nodes"), [])
        r["steps"] = len(nodes)
        r["step_titles"] = [n["title"] for n in nodes]
    return {"items": rows}


@router.get("/playbooks/{pid}")
async def playbook_get(request: Request, pid: str):
    u = current_user(request)
    r = db.q1("SELECT * FROM playbooks WHERE id=? AND user_id=?", (pid, u["id"]))
    if not r:
        raise ApiError(404, "NOT_FOUND", "工作流不存在")
    r["template_nodes"] = db.j(r["template_nodes"], [])
    return r


@router.patch("/playbooks/{pid}")
async def playbook_patch(request: Request, pid: str):
    u = current_user(request)
    d = await body(request)
    vals = {k: clip(str(d[k]), 300) for k in ("name", "description") if k in d}
    if not vals or not db.update("playbooks", {"id": pid, "user_id": u["id"]}, vals | {"updated_at": now_ms()}):
        raise ApiError(404, "NOT_FOUND", "工作流不存在")
    return {"ok": True}


@router.delete("/playbooks/{pid}")
async def playbook_delete(request: Request, pid: str):
    u = current_user(request)
    if not db.ex("DELETE FROM playbooks WHERE id=? AND user_id=?", (pid, u["id"])):
        raise ApiError(404, "NOT_FOUND", "工作流不存在")
    return {"ok": True}


@router.post("/playbooks/match")
async def playbook_match(request: Request):
    """新目标与已保存工作流的相似度（词项 Jaccard），超过阈值时在首页提示复用。"""
    u = current_user(request)
    d = await body(request)
    qt = set(tokenize(req_str(d, "text", 4000)))
    best = None
    for r in db.q("SELECT id, name, description, sample_goal FROM playbooks WHERE user_id=?", (u["id"],)):
        st = set(tokenize(r["sample_goal"] + " " + r["name"]))
        sc = len(qt & st) / max(1, len(qt | st))
        if sc >= 0.3 and (not best or sc > best["score"]):
            best = {"id": r["id"], "name": r["name"], "description": r["description"], "score": round(sc, 3)}
    return {"match": best}


# ======================= 偏好（F20，INV-14） =======================
@router.get("/preferences")
async def pref_list(request: Request):
    u = current_user(request)
    return {"items": db.q("SELECT * FROM preferences WHERE user_id=? ORDER BY created_at DESC", (u["id"],))}


@router.post("/preferences")
async def pref_add(request: Request):
    u = current_user(request)
    d = await body(request)
    p = {"id": new_id("pref"), "user_id": u["id"], "statement": req_str(d, "statement", 200), "scope": "global",
         "status": "active", "source": "manual", "created_at": now_ms()}
    db.insert("preferences", p)
    return {"preference": p}


@router.post("/preferences/{pid}/{action}")
async def pref_action(request: Request, pid: str, action: str):
    u = current_user(request)
    st = {"activate": "active", "dismiss": "dismissed"}.get(action)
    if not st or not db.update("preferences", {"id": pid, "user_id": u["id"]}, {"status": st}):
        raise ApiError(404, "NOT_FOUND", "偏好不存在")
    return {"ok": True}


@router.delete("/preferences/{pid}")
async def pref_delete(request: Request, pid: str):
    u = current_user(request)
    db.ex("DELETE FROM preferences WHERE id=? AND user_id=?", (pid, u["id"]))
    return {"ok": True}


# ======================= 能力库（F06） =======================
@router.get("/capabilities")
async def caps(request: Request):
    current_user(request)
    rows = matching.load_capabilities()
    for r in rows:
        r.pop("system_prompt", None)
    return {"items": rows, "tools": {k: {"permission": v["perm"], "risk": v["risk"], "description": v["desc"]}
                                     for k, v in matching.TOOL_REGISTRY.items()}}


@router.post("/capabilities/match")
async def caps_match(request: Request):
    u = current_user(request)
    d = await body(request)
    a = matching.match(plans.clean_requirements(d.get("capability_requirements")), u["id"])
    a.pop("system_prompt", None)
    return a


# ======================= 任务工作区（F32） =======================
@router.get("/workspace")
async def ws_list(request: Request):
    """列出我的任务工作区文件（Agent 只能在这里读写；删除与控制台操作需我批准）。"""
    u = current_user(request)
    out = workspace.list_files(u["id"])
    out["root"] = str(workspace.root_for(u["id"]) / "files") if config.DEPLOY_MODE == "self_hosted" else None
    return out


@router.get("/workspace/file")
async def ws_file(request: Request, path: str):
    u = current_user(request)
    r = workspace.read(u["id"], path)
    if "error" in r:
        raise ApiError(404, "NOT_FOUND", r.get("message") or "文件不存在")
    return r


_MEDIA_TYPES = {"md": "text/markdown", "txt": "text/plain", "csv": "text/csv", "json": "application/json",
                "html": "text/html", "htm": "text/html", "svg": "image/svg+xml", "png": "image/png",
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
                "pdf": "application/pdf", "py": "text/x-python", "js": "text/javascript", "css": "text/css"}


@router.get("/workspace/file/download")
async def ws_download(request: Request, path: str, inline: int = 0):
    """下载工作区文件（成果中的「文件」卡）。

    v3.0：`inline=1` 且文件是图片时内联展示（画布「图片」节点用 `<img>` 直接显示）；
    非图片一律走附件下载，避免任何非图片内容被浏览器当作页面渲染。
    """
    u = current_user(request)
    p = workspace.resolve(u["id"], path, must_exist=True)
    if p is None:
        raise ApiError(404, "NOT_FOUND", "文件不存在或路径不合法")
    ext = p.name.rsplit(".", 1)[-1].lower() if "." in p.name else ""
    inline_ok = bool(inline) and ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp")
    resp = FileResponse(p, media_type=_MEDIA_TYPES.get(ext, "application/octet-stream"),
                        filename=None if inline_ok else p.name)
    if inline_ok:
        resp.headers["X-Content-Type-Options"] = "nosniff"      # 图片内联：禁止浏览器嗅探类型
    return resp


@router.delete("/workspace/file")
async def ws_delete(request: Request, path: str):
    u = current_user(request)
    r = workspace.delete(u["id"], path)
    if "error" in r:
        raise ApiError(404, "NOT_FOUND", r.get("message") or "文件不存在")
    return r


# ======================= 通知 =======================
@router.get("/notifications")
async def notif_list(request: Request):
    u = current_user(request)
    return {"items": db.q("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (u["id"],)),
            "unread": db.q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND read=0", (u["id"],))["n"]}


@router.post("/notifications/read-all")
async def notif_read_all(request: Request):
    u = current_user(request)
    db.ex("UPDATE notifications SET read=1 WHERE user_id=?", (u["id"],))
    return {"ok": True}


@router.get("/stream/notifications")
async def notif_stream(request: Request):
    u = current_user(request)
    if events.user_sub_count(u["id"]) >= 8:
        raise ApiError(429, "TOO_MANY_STREAMS", "打开的通知连接过多，请关闭多余的标签页")
    qu = events.subscribe_user(u["id"])

    async def gen():
        try:
            while not await request.is_disconnected():
                try:
                    n = await asyncio.wait_for(qu.get(), timeout=15)
                    yield f"event: notification\ndata: {json.dumps(n, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe_user(u["id"], qu)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ======================= 设置（F26，INV-19） =======================
@router.get("/settings")
async def get_settings(request: Request):
    u = current_user(request)
    return {"values": settings_svc.get(u["id"]), "locked": sorted(settings_svc.LOCKED), "defaults": settings_svc.DEFAULTS,
            "schema_version": 1}


@router.patch("/settings")
async def patch_settings(request: Request):
    u = current_user(request)
    d = await body(request)
    for k, v in d.items():
        if k.startswith("models.") and k != "models.allow_builtin_fallback" and v != "auto":
            if not isinstance(v, str) or not gateway.model_info(v, u["id"]):
                raise ApiError(400, "MODEL_NOT_FOUND", "所选模型不存在")
    return {"values": settings_svc.patch(u["id"], d)}


@router.post("/settings/reset")
async def reset_settings(request: Request):
    u = current_user(request)
    d = await body(request)
    return {"values": settings_svc.reset(u["id"], req_str(d, "section", 40))}


# ======================= 模型服务（F28 / F29，INV-18） =======================
def _pub_provider(p: dict) -> dict:
    models = db.q("SELECT id, model_name, display_name, capabilities, price_in_per_1m, price_out_per_1m, visible "
                  "FROM provider_models WHERE provider_id=? ORDER BY rowid", (p["id"],))
    for m in models:
        m["capabilities"] = db.j(m["capabilities"], [])
        m["ref"] = f"{p['source']}:{m['id']}"
    return {k: p.get(k) for k in ("id", "kind", "source", "display_name", "base_url", "status", "latency_ms", "probed_at",
                                  "concurrency", "created_at")} | {"api_key_hint": p.get("key_hint"), "models": models}


@router.get("/providers/catalog")
async def provider_catalog(request: Request):
    current_user(request)
    return {"items": [{"kind": k, "name": n, "default_base_url": url, "needs_key": nk, "source": src, "suggested_models": ms,
                       "available": src != "local" or config.DEPLOY_MODE == "self_hosted"}
                      for (k, n, url, nk, src, ms) in gateway.PROVIDER_CATALOG]}


@router.get("/providers")
async def providers(request: Request):
    u = current_user(request)
    return {"items": [_pub_provider(p) for p in db.q("SELECT * FROM provider_configs WHERE user_id=? ORDER BY created_at", (u["id"],))]}


@router.post("/providers")
async def add_provider(request: Request):
    u = current_user(request)
    d = await body(request)
    kind = d.get("kind")
    if kind not in gateway.CATALOG or (kind == "fake" and not config.FAKE_LLM):
        raise ApiError(400, "PROVIDER_KIND_INVALID", "不支持的服务商类型")
    _, name, url, needs_key, source, suggested = gateway.CATALOG[kind]
    if source == "local" and config.DEPLOY_MODE != "self_hosted":
        raise ApiError(400, "LOCAL_DISABLED", "云端版不支持本地模型，请使用自托管版")
    if db.q1("SELECT COUNT(*) AS n FROM provider_configs WHERE user_id=?", (u["id"],))["n"] >= 20:
        raise ApiError(400, "PROVIDER_LIMIT", "最多添加 20 个服务商")
    base = opt_str(d, "base_url", 500) or url or ""
    if not base:
        raise ApiError(400, "FIELD_REQUIRED", "请填写服务地址")
    gateway.check_base_url(base, source)
    key = opt_str(d, "api_key", 500) or ""
    if needs_key and not key:
        raise ApiError(400, "FIELD_REQUIRED", "请填写 API Key")
    register_secret(key)
    pid = new_id("prv")
    pin, pout = num(d, "price_in_per_1m", 0, 0, 10_000), num(d, "price_out_per_1m", 0, 0, 10_000)
    p = {"id": pid, "user_id": u["id"], "kind": kind, "source": source, "display_name": clip(opt_str(d, "display_name", 40) or name, 40),
         "base_url": base, "key_ciphertext": encrypt_secret(key, u["id"], pid) if key else None,
         "key_hint": key_hint(key) if key else None, "status": "untested",
         "concurrency": 1 if source == "local" else config.DEFAULT_PROVIDER_CONCURRENCY, "created_at": now_ms()}
    db.insert("provider_configs", p)
    models = [str(m).strip() for m in d.get("models")] if isinstance(d.get("models"), list) else []
    models = [m for m in models if m] or suggested or []
    for m in models[:10]:
        caps = ["json", "tools"] if kind == "fake" and "no-tools" not in m else (["json"] if kind == "fake" else ["json"])
        db.insert("provider_models", {"id": new_id("mdl"), "provider_id": pid, "user_id": u["id"], "model_name": clip(m, 80),
                                      "display_name": f"{p['display_name']} · {m}", "capabilities": caps,
                                      "price_in_per_1m": pin, "price_out_per_1m": pout})
    return {"provider": _pub_provider(p)}


def _own_provider(u, pid):
    p = db.q1("SELECT * FROM provider_configs WHERE id=? AND user_id=?", (pid, u["id"]))
    if not p:
        raise ApiError(404, "NOT_FOUND", "服务商不存在")
    return p


@router.patch("/providers/{pid}")
async def patch_provider(request: Request, pid: str):
    u = current_user(request)
    p = _own_provider(u, pid)
    d = await body(request)
    vals = {}
    if opt_str(d, "display_name", 40):
        vals["display_name"] = opt_str(d, "display_name", 40)
    base = opt_str(d, "base_url", 500)
    if base:
        gateway.check_base_url(base, p["source"])
        vals.update(base_url=base, status="untested")          # 地址变了，之前的测试结果不再可信
    key = opt_str(d, "api_key", 500)
    if key:
        register_secret(key)
        vals.update(key_ciphertext=encrypt_secret(key, u["id"], pid), key_hint=key_hint(key), status="untested")
    if "concurrency" in d:
        vals["concurrency"] = num(d, "concurrency", None, 1, 8, integer=True)
        lim = gateway._limiters.get(pid)
        if lim:                                              # 就地调整，保留进行中的占用计数
            lim.permits = lim.cap = vals["concurrency"]
    if vals:
        db.update("provider_configs", {"id": pid}, vals)
    return {"provider": _pub_provider(_own_provider(u, pid))}


@router.delete("/providers/{pid}")
async def delete_provider(request: Request, pid: str):
    u = current_user(request)
    p = _own_provider(u, pid)
    refs = [f"{p['source']}:{m['id']}" for m in db.q("SELECT id FROM provider_models WHERE provider_id=?", (pid,))]
    db.ex("DELETE FROM provider_configs WHERE id=?", (pid,))           # 密文随之销毁
    s = settings_svc.get(u["id"])
    fix = {k: ("auto" if k == "models.default_model" else f"builtin:{k.split('_')[-1]}") for k, v in s.items()
           if k.startswith("models.") and v in refs}
    if fix:
        settings_svc.patch(u["id"], fix)
    affected = [t["id"] for t in db.q("SELECT id FROM tasks WHERE user_id=? AND model_choice IN (%s)" % ",".join("?" * len(refs)), [u["id"], *refs])] if refs else []
    return {"affected_tasks": affected}


@router.post("/providers/{pid}/test")
async def test_provider(request: Request, pid: str):
    u = current_user(request)
    p = _own_provider(u, pid)
    from ..security import decrypt_secret
    try:
        key = decrypt_secret(p["key_ciphertext"], u["id"], pid) if p["key_ciphertext"] else ""
    except Exception:          # noqa: BLE001  主密钥变更或密文损坏：按 Key 无效处理，提示用户重新填写
        db.update("provider_configs", {"id": pid}, {"status": "key_invalid", "probed_at": now_ms()})
        return {"ok": False, "reachable": False, "auth_ok": False, "json": False, "tools": False,
                "error": "KEY_UNREADABLE", "status": "key_invalid"}
    register_secret(key)
    models = db.q("SELECT * FROM provider_models WHERE provider_id=? ORDER BY rowid", (pid,))
    want = (await body(request)).get("model_id")
    target = next((m for m in models if m["id"] == want), models[0] if models else None)
    res = await gateway.probe(p["kind"], p["base_url"], key, target["model_name"] if target else "")
    status = "ok" if res["ok"] else ("key_invalid" if res.get("error") == "AUTH_FAILED" else
                                     "unreachable" if not res["reachable"] else "limited")
    db.update("provider_configs", {"id": pid}, {"status": status, "latency_ms": res.get("latency_ms"), "probed_at": now_ms()})
    if target and res["auth_ok"]:
        caps = [c for c in ("json", "tools") if res.get(c)]
        db.update("provider_models", {"id": target["id"]}, {"capabilities": caps})
    if status == "key_invalid":
        events.notify(u["id"], "provider_key_invalid", f"{p['display_name']} 的 Key 无效，请更换")
    return res | {"status": status}


@router.post("/providers/{pid}/models")
async def add_model(request: Request, pid: str):
    u = current_user(request)
    p = _own_provider(u, pid)
    d = await body(request)
    name = req_str(d, "model_name", 80)
    m = {"id": new_id("mdl"), "provider_id": pid, "user_id": u["id"], "model_name": name,
         "display_name": clip(d.get("display_name") or f"{p['display_name']} · {name}", 60),
         "capabilities": ["json", "tools"] if p["kind"] == "fake" and "no-tools" not in name else ["json"],
         "price_in_per_1m": num(d, "price_in_per_1m", 0, 0, 10_000), "price_out_per_1m": num(d, "price_out_per_1m", 0, 0, 10_000)}
    db.insert("provider_models", m)
    return {"model": m}


@router.patch("/models/{mid}")
async def patch_model(request: Request, mid: str):
    u = current_user(request)
    d = await body(request)
    vals = {}
    for k in ("display_name",):
        if k in d:
            vals[k] = clip(str(d[k]), 60)
    for k in ("price_in_per_1m", "price_out_per_1m"):
        if k in d:
            vals[k] = num(d, k, None, 0, 10_000)
    if "visible" in d:
        vals["visible"] = int(bool(d["visible"]))
    if not vals or not db.update("provider_models", {"id": mid, "user_id": u["id"]}, vals):
        raise ApiError(404, "NOT_FOUND", "模型不存在")
    return {"ok": True}


@router.delete("/models/{mid}")
async def delete_model(request: Request, mid: str):
    u = current_user(request)
    if not db.ex("DELETE FROM provider_models WHERE id=? AND user_id=?", (mid, u["id"])):
        raise ApiError(404, "NOT_FOUND", "模型不存在")
    return {"ok": True}


@router.get("/models")
async def models(request: Request):
    u = current_user(request)
    return gateway.list_models(u["id"])


# ======================= 管理（F25） =======================
def _admin(request):
    u = current_user(request)
    if not u["is_admin"]:
        raise ApiError(403, "FORBIDDEN", "需要管理员权限")
    return u


@router.get("/admin/usage")
async def admin_usage(request: Request):
    _admin(request)
    return {"by_day": db.q("SELECT date(created_at/1000,'unixepoch','localtime') AS day, COUNT(*) AS calls, ROUND(SUM(cost_cny),4) AS cost, "
                           "SUM(ok=0) AS errors FROM llm_calls GROUP BY day ORDER BY day DESC LIMIT 30"),
            "by_source": db.q("SELECT source, COUNT(*) AS calls, ROUND(SUM(cost_cny),4) AS cost FROM llm_calls GROUP BY source")}


@router.get("/admin/users")
async def admin_users(request: Request):
    _admin(request)
    return {"items": db.q("SELECT u.id, u.email, u.created_at, u.quota_monthly_cny, (SELECT COUNT(*) FROM tasks t WHERE t.user_id=u.id) AS tasks "
                          "FROM users u ORDER BY u.created_at")}


@router.patch("/admin/users/{uid}")
async def admin_patch_user(request: Request, uid: str):
    _admin(request)
    d = await body(request)
    if not db.update("users", {"id": uid}, {"quota_monthly_cny": num(d, "quota_monthly_cny", None, 0, 100_000)}):
        raise ApiError(404, "NOT_FOUND", "用户不存在")
    return {"ok": True}


@router.post("/admin/housekeeping")
async def admin_housekeeping(request: Request):
    _admin(request)
    from ..hardening import housekeeping
    return housekeeping()


@router.get("/admin/health")
async def admin_health(request: Request):
    _admin(request)
    return {"inflight": len(RUNNER.inflight), "global_running": RUNNER.global_running,
            "task_streams": sum(len(v) for v in events._task_subs.values()),      # 便于发现前端未关闭的实时连接
            "user_streams": sum(len(v) for v in events._user_subs.values()),
            "limiters": {k: {"permits": v.permits, "in_use": v.in_use} for k, v in gateway._limiters.items()},
            "fake_llm": config.FAKE_LLM, "deploy_mode": config.DEPLOY_MODE}


_ = quote
