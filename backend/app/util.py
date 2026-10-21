"""通用工具：ID、时间、错误、语言检测、分词、哈希与脱敏。"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash(obj: Any) -> str:
    """对任意 JSON 结构计算稳定哈希（键排序），用于 input_hash 与 content_hash。"""
    return sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))[:16]


class ApiError(Exception):
    """业务错误：code 为稳定错误码（前端据此本地化），message 为中文提示。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


# ---------- 语言 ----------
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """成果语言跟随用户输入（R27）：中文字符占比 ≥ 15% 视为中文。"""
    cjk = len(_CJK.findall(text or ""))
    lat = len(_LATIN.findall(text or ""))
    if cjk == 0 and lat == 0:
        return "zh"
    return "zh" if cjk / max(1, cjk + lat / 4) >= 0.15 else "en"


# ---------- 检索分词：拉丁词 + 中文二元组 ----------
_WORD = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
STOP = set("的 了 和 与 及 或 是 在 我 你 他 她 它 们 这 那 一个 一下 帮 帮我 请 给 把 吗 呢 吧 a an the of to and or for in on is are be with".split())


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall((text or "").lower()):
        if _CJK.match(w):
            if len(w) == 1:
                if w not in STOP:
                    out.append(w)
            else:
                out.extend(w[i:i + 2] for i in range(len(w) - 1))
        elif w not in STOP and len(w) > 1:
            out.append(w)
    return out


# ---------- 脱敏（INV-18）：运行期登记出现过的密钥原文，写日志/事件/错误前统一替换 ----------
_SECRETS: set[str] = set()


def register_secret(s: str | None) -> None:
    if s and len(s) >= 6:
        _SECRETS.add(s)


def redact(text: str) -> str:
    if not isinstance(text, str):
        return text
    for s in _SECRETS:
        if s in text:
            text = text.replace(s, "****" + s[-4:])
    # 常见 Key 形态兜底
    text = re.sub(r"\b(sk|ak|key)-[A-Za-z0-9_\-]{12,}", lambda m: m.group(1) + "-****", text)
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1****", text)


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj


# ---------- 安全层：敏感信息检测 ----------
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask_sensitive(text: str) -> tuple[str, list[str]]:
    """对输出中的手机号、身份证号做遮蔽，返回 (遮蔽后文本, 命中类型列表)。"""
    hits: list[str] = []

    def _m(kind):
        def f(m):
            hits.append(kind)
            s = m.group(0)
            return s[:3] + "*" * (len(s) - 7) + s[-4:]
        return f

    text = _IDCARD.sub(_m("id_card"), text)
    text = _PHONE.sub(_m("phone"), text)
    return text, hits


def clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"
