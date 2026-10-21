"""Start the hosted service only after storage, configuration and admin initialization."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def prepare_storage():
    data = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
        if not mount or Path(mount).resolve() != data:
            raise ValueError("Railway requires a persistent volume mounted at DATA_DIR")
    # Railway mounts fresh volumes as root. Hand ownership to the image's app user,
    # then drop privileges before importing the app or creating any user records.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        import pwd
        user = pwd.getpwnam("combine")
        data.mkdir(parents=True, exist_ok=True)
        os.chown(data, user.pw_uid, user.pw_gid)
        os.chmod(data, 0o700)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    data.mkdir(parents=True, exist_ok=True)
    if not os.access(data, os.W_OK):
        raise ValueError("DATA_DIR is not writable by the app user")


def main():
    port = int(os.environ.get("PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    prepare_storage()
    sys.path.insert(0, str(ROOT / "backend"))
    from app import db
    from bootstrap_admin import bootstrap
    from production_check import check

    check()
    email = os.environ.pop("BOOTSTRAP_ADMIN_EMAIL", "")
    password = os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", "")
    try:
        created = bootstrap({"email": email, "password": password})
        print("Administrator initialized" if created else "Existing administrator preserved", flush=True)
    finally:
        db.reset_connection()
    os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(ROOT / "backend"),
                             "--host", "0.0.0.0", "--port", str(port), "--workers", "1", "--no-proxy-headers"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
