"""密码散列、会话令牌与 API Key 加密（AES-256-GCM，AAD 绑定用户与服务商，INV-18）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config


# ---------- 密码：scrypt（标准库） ----------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, s, h = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(h))
    except Exception:
        return False


def new_session_token() -> tuple[str, str]:
    tok = secrets.token_urlsafe(32)
    return tok, hashlib.sha256(tok.encode()).hexdigest()


def token_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


# ---------- API Key 加密 ----------
def _master_key() -> bytes:
    raw = config.SECRETS_MASTER_KEY
    if not raw:
        # 开发环境：首次运行生成并保存在数据目录（文件不入库、不进日志）
        p = config.DATA_DIR / "master.key"
        if not p.exists():
            p.write_text(base64.b64encode(os.urandom(32)).decode(), encoding="utf-8")
            try:
                os.chmod(p, 0o600)                           # Windows 上只影响只读位，属尽力而为
            except OSError:
                pass
        raw = p.read_text(encoding="utf-8").strip()
    try:
        key = base64.b64decode(raw)
    except Exception:
        key = b""
    if len(key) != 32:
        key = hashlib.sha256(raw.encode()).digest()
    return key


def encrypt_secret(plain: str, user_id: str, provider_id: str) -> str:
    nonce = os.urandom(12)
    aad = f"{user_id}|{provider_id}".encode()
    ct = AESGCM(_master_key()).encrypt(nonce, plain.encode(), aad)
    return base64.b64encode(nonce + ct).decode()


def decrypt_secret(blob: str, user_id: str, provider_id: str) -> str:
    """AAD 不匹配（例如密文被挪到他人名下）时抛出异常。"""
    data = base64.b64decode(blob)
    aad = f"{user_id}|{provider_id}".encode()
    return AESGCM(_master_key()).decrypt(data[:12], data[12:], aad).decode()


def key_hint(plain: str) -> str:
    return "••••" + plain[-4:] if plain and len(plain) >= 4 else "••••"
