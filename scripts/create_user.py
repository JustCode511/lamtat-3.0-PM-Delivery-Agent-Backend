"""
Create or reset a user in data/users.json.

Usage (from the backend root):
    python scripts/create_user.py                      # creates admin / admin123
    python scripts/create_user.py myname mypassword    # creates custom user

Run this once after a fresh checkout so you can log in.
"""
import json
import sys
from pathlib import Path

# Allow running from the repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.auth import hash_password

DATA_FILE = Path(__file__).parent.parent / "data" / "users.json"


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    password = sys.argv[2] if len(sys.argv) > 2 else "admin123"

    if len(username) < 3:
        print("Username must be at least 3 characters.")
        sys.exit(1)
    if len(password) < 6:
        print("Password must be at least 6 characters.")
        sys.exit(1)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    users: dict = {}
    if DATA_FILE.exists():
        try:
            users = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    from datetime import datetime, timezone
    pw_hash, salt = hash_password(password)
    users[username] = {
        "username": username,
        "password_hash": pw_hash,
        "salt": salt,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    DATA_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    print(f"✓ User '{username}' created/updated in {DATA_FILE}")
    print(f"  Login at the app with username='{username}' password='{password}'")


if __name__ == "__main__":
    main()
