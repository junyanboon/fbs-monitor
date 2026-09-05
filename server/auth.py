"""Staff accounts, sessions and the claims ledger for the claim server.

One database (server/db.py: Postgres on Vercel, SQLite on a laptop). Tables:

  users   the five rostered staff — email + scrypt password hash. Created by
          `python -m server.users add`, never by the web app (no self-signup;
          the roster is closed, see README rule 9).
  claims  one row per claimed open shift, keyed on the builder's shift id
          (build.open_shift_id). This is the local truth the pages overlay on
          DATA.openShifts, because the Staff calendar's ICS feed can lag a
          claim by minutes to hours and the board must not re-offer a shift
          someone has already taken.
  pages   the built editions, uploaded by publish.py at the end of every
          GitHub Actions build. A serverless host has no disk of its own.
  login_attempts  the sign-in throttle, kept here because each request may
          run in a fresh process.

Sessions are a signed cookie (itsdangerous) carrying only the user id — no
server-side session table, so a restart logs nobody out. FBS_SECRET_KEY signs
it; rotate that key and every session ends.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import db

SESSION_COOKIE = "fbs_session"
SESSION_MAX_AGE = 30 * 24 * 3600          # 30 days; the staff live on their phones

def connect() -> db.Connection:
    return db.connect()


# ── passwords ────────────────────────────────────────────────────────────────
# scrypt from the stdlib: no extra dependency, and the roster is five people,
# so the cost parameters can sit well above the usual defaults.
_SCRYPT = dict(n=2 ** 15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT), salt


def check_password(password: str, pw_hash: bytes, salt: bytes) -> bool:
    got, _ = hash_password(password, salt)
    return secrets.compare_digest(got, pw_hash)


# ── users ────────────────────────────────────────────────────────────────────
@dataclass
class User:
    id: int
    name: str
    email: str
    must_change: bool

    def public(self) -> dict:
        return {"name": self.name, "email": self.email, "mustChange": self.must_change}


def _row_to_user(row) -> User:
    return User(id=row["id"], name=row["name"], email=row["email"],
                must_change=bool(row["must_change"]))


def add_user(con, name: str, email: str, password: str, must_change: bool = True) -> User:
    pw_hash, salt = hash_password(password)
    row = con.execute(
        "INSERT INTO users (name, email, pw_hash, salt, must_change, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (name.strip(), email.strip().lower(), pw_hash, salt, int(must_change), time.time())
    ).fetchone()
    con.commit()
    return get_user(con, row["id"])


def set_password(con, user_id: int, password: str, must_change: bool = False) -> None:
    pw_hash, salt = hash_password(password)
    con.execute("UPDATE users SET pw_hash=?, salt=?, must_change=? WHERE id=?",
                (pw_hash, salt, int(must_change), user_id))
    con.commit()


def remove_user(con, email: str) -> bool:
    cur = con.execute("DELETE FROM users WHERE email=?", (email.strip().lower(),))
    con.commit()
    return cur.rowcount > 0


def list_users(con) -> list[User]:
    return [_row_to_user(r) for r in con.execute("SELECT * FROM users ORDER BY name").fetchall()]


def get_user(con, user_id: int) -> User | None:
    row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_email(con, email: str) -> User | None:
    row = con.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return _row_to_user(row) if row else None


def authenticate(con, email: str, password: str) -> User | None:
    row = con.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row:
        # Burn the same time as a real check so a probe can't tell an unknown
        # address from a wrong password by the clock.
        hash_password(password, b"\0" * 16)
        return None
    if not check_password(password, bytes(row["pw_hash"]), bytes(row["salt"])):
        return None
    return _row_to_user(row)


# ── sessions ─────────────────────────────────────────────────────────────────
def _serializer() -> URLSafeTimedSerializer:
    key = os.environ.get("FBS_SECRET_KEY")
    if not key or len(key) < 32:
        raise RuntimeError("FBS_SECRET_KEY must be set (32+ random characters); "
                           "see deploy/README.md")
    return URLSafeTimedSerializer(key, salt="fbs-session")


def make_session(user: User) -> str:
    return _serializer().dumps({"uid": user.id})


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data.get("uid") if isinstance(data, dict) else None


# ── login throttling ─────────────────────────────────────────────────────────
# Per client address: 10 attempts per 10 minutes, kept in the database because
# on Vercel each request may land in a fresh process. Enough to stop a
# dictionary run against five accounts; not a substitute for the long secret.
LOGIN_WINDOW, LOGIN_MAX = 600, 10


def login_allowed(con, addr: str) -> bool:
    cutoff = time.time() - LOGIN_WINDOW
    con.execute("DELETE FROM login_attempts WHERE ts < ?", (cutoff,))
    con.commit()
    row = con.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE addr=? AND ts >= ?",
                      (addr, cutoff)).fetchone()
    return int(row["n"]) < LOGIN_MAX


def login_failed(con, addr: str) -> None:
    con.execute("INSERT INTO login_attempts (addr, ts) VALUES (?, ?)", (addr, time.time()))
    con.commit()


# ── claims ───────────────────────────────────────────────────────────────────
def get_claim(con, shift_id: str):
    return con.execute("SELECT * FROM claims WHERE shift_id=?", (shift_id,)).fetchone()


def add_claim(con, user: User, shift: dict, gcal_event_id: str | None) -> dict:
    con.execute(
        "INSERT INTO claims (shift_id, user_id, name, role, studio, date, start_iso, "
        "end_iso, gcal_event_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (shift["id"], user.id, user.name, shift["role"], shift.get("studio"),
         shift["date"], shift["startISO"], shift["endISO"], gcal_event_id, time.time()))
    con.commit()
    return get_claim(con, shift["id"])


def remove_claim(con, shift_id: str) -> None:
    con.execute("DELETE FROM claims WHERE shift_id=?", (shift_id,))
    con.commit()


def recent_claims(con, since_date: str) -> list[dict]:
    """Claims whose shift date is on/after since_date (ISO yyyy-mm-dd)."""
    return con.execute("SELECT * FROM claims WHERE date >= ? ORDER BY start_iso",
                       (since_date,)).fetchall()


def public_claim(c: dict) -> dict:
    """What the pages get: who took it and when. No ids that aren't the shift's."""
    return {"id": c["shift_id"], "name": c["name"], "role": c["role"],
            "studio": c["studio"], "date": c["date"],
            "startISO": c["start_iso"], "endISO": c["end_iso"],
            "claimedAt": c["created_at"]}


# ── built pages ──────────────────────────────────────────────────────────────
def put_page(con, name: str, body: str) -> None:
    """Upsert one built file. Plain DELETE+INSERT: the one upsert form both
    engines share without dialect."""
    con.execute("DELETE FROM pages WHERE name=?", (name,))
    con.execute("INSERT INTO pages (name, body, updated_at) VALUES (?, ?, ?)",
                (name, body, time.time()))
    con.commit()


def get_page(con, name: str) -> str | None:
    row = con.execute("SELECT body FROM pages WHERE name=?", (name,)).fetchone()
    return row["body"] if row else None


def page_updated_at(con, name: str) -> float | None:
    row = con.execute("SELECT updated_at FROM pages WHERE name=?", (name,)).fetchone()
    return row["updated_at"] if row else None
