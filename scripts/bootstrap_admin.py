"""Initialize the first administrator from JSON on stdin, without exposing a public registration window."""
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import db
from app.security import hash_password
from app.util import new_id, now_ms


def bootstrap(data):
    if db.q1("SELECT id FROM users LIMIT 1"):
        if not db.q1("SELECT id FROM users WHERE is_admin=1 LIMIT 1"):
            raise ValueError("existing accounts have no administrator; manual recovery required")
        return False
    email, password = data.get("email", ""), data.get("password", "")
    if not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("invalid administrator email")
    if not isinstance(password, str) or len(password) < 16:
        raise ValueError("bootstrap password must have at least 16 characters")
    with db.Tx():
        db.insert("users", {"id": new_id("usr"), "email": email.lower(), "password_hash": hash_password(password),
                            "display_name": email.split("@")[0], "is_admin": 1, "quota_monthly_cny": 50,
                            "created_at": now_ms()})
    return True


if __name__ == "__main__":
    try:
        created = bootstrap(json.load(sys.stdin))
        print("Administrator initialized" if created else "Existing administrator preserved")
    except (ValueError, TypeError) as error:
        raise SystemExit(str(error))
    finally:
        db.reset_connection()
