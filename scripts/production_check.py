"""Reject incomplete production configuration without printing credential values."""
import base64
import binascii
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import config


def check():
    errors = []
    for name, valid in (
        ("DEPLOY_MODE", config.DEPLOY_MODE == "cloud"),
        ("COOKIE_SECURE", config.COOKIE_SECURE),
        ("TRUST_PROXY", config.TRUST_PROXY),
        ("RATE_LIMIT", config.RATE_LIMIT),
        ("PLATFORM_BASE_URL / PLATFORM_API_KEY / FAKE_LLM", not config.FAKE_LLM),
        ("PLATFORM_MODEL_FAST / STANDARD / STRONG", all(config.PLATFORM_MODELS.values())),
    ):
        if not valid:
            errors.append(name)
    try:
        valid_key = len(base64.b64decode(config.SECRETS_MASTER_KEY, validate=True)) == 32
    except (ValueError, binascii.Error):
        valid_key = False
    if not valid_key:
        errors.append("SECRETS_MASTER_KEY (base64, 32 bytes)")
    if errors:
        raise ValueError("Production configuration requires: " + "; ".join(errors))


if __name__ == "__main__":
    try:
        check()
    except ValueError as error:
        raise SystemExit(str(error))
    print("Production configuration checked; no model request was sent")
