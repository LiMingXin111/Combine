"""Minimal environment for explicitly connected local agents."""
import os

_ALLOWED = ("PATH", "SystemRoot", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")


def clean_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ALLOWED if k in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env
