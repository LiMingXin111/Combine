"""Generate production configuration and a separate bootstrap credential file."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.split(" #", 1)[0].strip().strip('\"\'')
    return result


def write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
        target.write(content)


def initialize(domain, email, output, use_local_model=False):
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}", domain):
        raise ValueError("domain must be a hostname without a scheme, port or path")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("invalid administrator email")
    output = Path(output).resolve()
    credential_file = output.parent / ".admin-bootstrap.json"
    if output.exists() or credential_file.exists():
        raise ValueError("configuration or bootstrap file already exists; refusing to replace credentials")
    values = read_env(ROOT / "deploy" / "production.env.example")
    values.update(COMBINE_DOMAIN=domain.lower(), ACME_EMAIL=email, COMBINE_RELEASE=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
                  SECRETS_MASTER_KEY=base64.b64encode(secrets.token_bytes(32)).decode())
    if use_local_model:
        local = read_env(ROOT / ".env")
        for key in ("PLATFORM_BASE_URL", "PLATFORM_API_KEY", "PLATFORM_MODEL_FAST", "PLATFORM_MODEL_STANDARD", "PLATFORM_MODEL_STRONG", "LLM_TIMEOUT_S"):
            if local.get(key):
                values[key] = local[key]
    for value in values.values():
        if any(char in str(value) for char in "\r\n\x00"):
            raise ValueError("multiline environment values are not supported")
    # Compose interpolates dollars even inside env files; single quotes keep credentials literal.
    text = "".join(f"{key}='{value}'\n" for key, value in values.items())
    if any("'" in str(value) for value in values.values()):
        raise ValueError("single quotes in environment values are not supported")
    write_private(output, text)
    write_private(credential_file, json.dumps({"email": email, "password": secrets.token_urlsafe(24)}, ensure_ascii=False) + "\n")
    return output, credential_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--output", default=str(ROOT / ".env.production"))
    parser.add_argument("--use-local-model", action="store_true")
    args = parser.parse_args()
    try:
        paths = initialize(args.domain, args.email, args.output, args.use_local_model)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
    for path in paths:
        print(path)
