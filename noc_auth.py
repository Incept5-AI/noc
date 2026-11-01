"""Magic-link auth for the NOC dashboard — SQLite + SES, stdlib apart from boto3.

Magic link + 6-digit code (either signs you in), SQLite-backed sessions, SES
email, single allowed domain. Restricts sign-in to ALLOWED_EMAIL_DOMAINS
(no default: nobody can sign in until it is set).
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

# ---- config (env-overridable; systemd EnvironmentFile in prod) ----
DB_PATH = os.getenv("NOC_DB", os.path.join(os.path.dirname(__file__), "noc.db"))
ALLOWED_EMAIL_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()
}
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
}
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8788")
APP_NAME = os.getenv("APP_NAME", "Fleet NOC")
SES_SENDER = os.getenv("SES_SENDER", "noc@example.com")
SES_REGION = os.getenv("AWS_REGION", "eu-west-2")
MAGIC_LINK_TTL_MIN = int(os.getenv("MAGIC_LINK_TTL_MIN", "15"))
MAX_CODE_ATTEMPTS = int(os.getenv("MAX_CODE_ATTEMPTS", "8"))  # guesses per email's live codes
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
LOGIN_EMAIL_COOLDOWN_S = int(os.getenv("LOGIN_EMAIL_COOLDOWN_S", "45"))
SESSION_COOKIE = os.getenv("SESSION_COOKIE", "noc_session")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") not in ("0", "false", "")

# ---- SQLite (single connection + lock; low concurrency) ----
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS magic_links(
                token TEXT PRIMARY KEY, email TEXT NOT NULL,
                code TEXT, attempts INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL, used_at TEXT);
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY, email TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL, last_seen_at TEXT);
            CREATE TABLE IF NOT EXISTS machines(
                name TEXT PRIMARY KEY, owner_email TEXT NOT NULL,
                token TEXT UNIQUE, os_type TEXT, shared INTEGER DEFAULT 0,
                added_at TEXT DEFAULT (datetime('now')), last_seen TEXT);
            """
        )
        # defensive migrations (idempotent) for DBs created before these columns existed
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(machines)").fetchall()}
        if "shared" not in cols:
            _conn.execute("ALTER TABLE machines ADD COLUMN shared INTEGER DEFAULT 0")
        mlcols = {r[1] for r in _conn.execute("PRAGMA table_info(magic_links)").fetchall()}
        if "code" not in mlcols:
            _conn.execute("ALTER TABLE magic_links ADD COLUMN code TEXT")
        if "attempts" not in mlcols:
            _conn.execute("ALTER TABLE magic_links ADD COLUMN attempts INTEGER DEFAULT 0")
        _conn.commit()
    return _conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def email_domain_allowed(email: str) -> bool:
    email = (email or "").strip().lower()
    return "@" in email and email.rsplit("@", 1)[1] in ALLOWED_EMAIL_DOMAINS


def recently_sent(email: str) -> bool:
    cutoff = _iso(_now() - timedelta(seconds=LOGIN_EMAIL_COOLDOWN_S))
    with _lock:
        row = _db().execute(
            "SELECT 1 FROM magic_links WHERE email=? AND created_at>? LIMIT 1",
            (email.strip().lower(), cutoff),
        ).fetchone()
    return row is not None


def issue_login(email: str) -> tuple[str, str]:
    """Create a sign-in token + 6-digit code (both valid); return (magic URL, code)."""
    token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = _iso(_now() + timedelta(minutes=MAGIC_LINK_TTL_MIN))
    with _lock:
        c = _db()
        c.execute(
            "INSERT INTO magic_links(token, email, code, expires_at) VALUES(?,?,?,?)",
            (token, email.strip().lower(), code, expires),
        )
        c.commit()
    return f"{APP_BASE_URL.rstrip('/')}/magic?token={token}", code


def send_login_email(email: str, url: str, code: str) -> None:
    """Send the magic link + 6-digit code via AWS SES (default boto3 chain / EC2 role)."""
    import boto3

    ses = boto3.Session().client("ses", region_name=SES_REGION)
    subject = f"Your {APP_NAME} sign-in code: {code}"
    intro = (f"Enter this code to sign in to {APP_NAME}, or use the button below. "
             f"The code and link expire in {MAGIC_LINK_TTL_MIN} minutes.")
    ignore = "If you did not request this, you can ignore this email."
    spaced = " ".join(code)
    text_body = f"{intro}\n\nCode: {code}\n\n{url}\n\n{ignore}"
    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;max-width:520px">
      <p>{intro}</p>
      <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#888;margin-bottom:6px">
         Your sign-in code</div>
      <div style="font-size:34px;font-weight:700;letter-spacing:.18em;color:#1a7f37;
         font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;margin-bottom:18px">{spaced}</div>
      <p><a href="{url}" style="display:inline-block;padding:12px 20px;background:#1a7f37;
         color:#fff;border-radius:6px;text-decoration:none;font-weight:600">Sign in</a></p>
      <p style="color:#555;font-size:13px">Or paste this URL into your browser:<br>
         <code style="word-break:break-all">{url}</code></p>
      <p style="color:#999;font-size:12px">{ignore}</p>
    </div>"""
    ses.send_email(
        Source=SES_SENDER,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )


def redeem_magic_link(token: str) -> str:
    """Redeem a URL token → new session token. Raises ValueError on failure."""
    with _lock:
        c = _db()
        row = c.execute(
            "SELECT email, expires_at, used_at FROM magic_links WHERE token=?", (token,)
        ).fetchone()
        if not row:
            raise ValueError("invalid")
        if row["used_at"] is not None:
            raise ValueError("used")
        if row["expires_at"] < _iso(_now()):
            raise ValueError("expired")
        c.execute("UPDATE magic_links SET used_at=datetime('now') WHERE token=?", (token,))
        sess = secrets.token_urlsafe(32)
        c.execute(
            "INSERT INTO sessions(token, email, expires_at) VALUES(?,?,?)",
            (sess, row["email"], _iso(_now() + timedelta(days=SESSION_TTL_DAYS))),
        )
        c.commit()
    return sess


def redeem_code(email: str, code: str) -> str:
    """Redeem a 6-digit code for an email → new session token. Raises ValueError on
    failure. Wrong guesses burn an attempt against the email's live codes (capped)."""
    email = (email or "").strip().lower()
    # accept the code however it was pasted — the email prints it spaced ("1 2 3 4 5 6"),
    # and phone keyboards add stray spaces/dashes; keep the digits only.
    code = "".join(ch for ch in (code or "") if ch in "0123456789")
    now = _iso(_now())
    with _lock:
        c = _db()
        row = c.execute(
            "SELECT token FROM magic_links WHERE email=? AND code=? AND used_at IS NULL "
            "AND expires_at > ? AND attempts < ? ORDER BY created_at DESC LIMIT 1",
            (email, code, now, MAX_CODE_ATTEMPTS),
        ).fetchone()
        if not row:
            # wrong/expired: burn an attempt on this email's still-live challenges
            c.execute("UPDATE magic_links SET attempts = attempts + 1 "
                      "WHERE email=? AND used_at IS NULL AND expires_at > ?", (email, now))
            c.commit()
            raise ValueError("invalid")
        c.execute("UPDATE magic_links SET used_at=datetime('now') WHERE token=?", (row["token"],))
        sess = secrets.token_urlsafe(32)
        c.execute(
            "INSERT INTO sessions(token, email, expires_at) VALUES(?,?,?)",
            (sess, email, _iso(_now() + timedelta(days=SESSION_TTL_DAYS))),
        )
        c.commit()
    return sess


def session_email(token: str | None) -> str | None:
    if not token:
        return None
    with _lock:
        row = _db().execute(
            "SELECT email, expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()
    if not row or row["expires_at"] < _iso(_now()):
        return None
    return row["email"]


def logout(token: str) -> None:
    with _lock:
        c = _db()
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()


def current_user(request) -> str | None:
    """Return the signed-in email, or None. `request` is a Starlette/FastAPI Request."""
    return session_email(request.cookies.get(SESSION_COOKIE))


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_DAYS * 86400,
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/",
    )


# ---- machine enrolment / ownership ----
import re

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}$")


def valid_machine_name(name: str) -> bool:
    return bool(_NAME_RE.match((name or "").strip()))


def is_admin(email: str | None) -> bool:
    return bool(email) and email.lower() in ADMIN_EMAILS


def register_machine(owner_email: str, name: str, os_type: str,
                     shared: bool = False) -> tuple[str | None, str | None]:
    """Register a machine to an owner. Returns (token, error). error is a slug or None."""
    name = (name or "").strip()
    if not valid_machine_name(name):
        return None, "bad_name"
    if os_type not in ("linux", "macos"):
        return None, "bad_os"
    token = secrets.token_urlsafe(24)
    sh = 1 if shared else 0
    with _lock:
        c = _db()
        exists = c.execute("SELECT owner_email, token FROM machines WHERE name=?", (name,)).fetchone()
        if exists and exists["token"]:            # already a self-registered machine
            return None, "name_taken"
        if exists:                                # a seeded/legacy row — attach a token to it
            c.execute("UPDATE machines SET owner_email=?, token=?, os_type=?, shared=? WHERE name=?",
                      (owner_email.lower(), token, os_type, sh, name))
        else:
            c.execute("INSERT INTO machines(name, owner_email, token, os_type, shared) VALUES(?,?,?,?,?)",
                      (name, owner_email.lower(), token, os_type, sh))
        c.commit()
    return token, None


def set_shared(name: str, shared: bool, requester_email: str) -> bool:
    """Toggle a machine's shared flag. Allowed for the owner or an admin. Returns success."""
    with _lock:
        c = _db()
        row = c.execute("SELECT owner_email FROM machines WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        if not (is_admin(requester_email) or row["owner_email"] == requester_email.lower()):
            return False
        c.execute("UPDATE machines SET shared=? WHERE name=?", (1 if shared else 0, name))
        c.commit()
    return True


def machines_meta() -> dict[str, dict]:
    """name -> {owner, shared} for visibility decisions."""
    with _lock:
        rows = _db().execute("SELECT name, owner_email, shared FROM machines").fetchall()
    return {r["name"]: {"owner": r["owner_email"], "shared": bool(r["shared"])} for r in rows}


def can_see(name: str, meta: dict, email: str) -> bool:
    """A machine is visible if you're an admin, you own it, or it's shared.
    Machines with no ownership record are visible to admins only."""
    if is_admin(email):
        return True
    m = meta.get(name)
    return bool(m and (m["shared"] or m["owner"] == email.lower()))


def machine_by_token(token: str | None):
    if not token:
        return None
    with _lock:
        return _db().execute("SELECT * FROM machines WHERE token=?", (token,)).fetchone()


def touch_machine(name: str) -> None:
    with _lock:
        c = _db()
        c.execute("UPDATE machines SET last_seen=datetime('now') WHERE name=?", (name,))
        c.commit()


def owners_map() -> dict[str, str]:
    with _lock:
        rows = _db().execute("SELECT name, owner_email FROM machines").fetchall()
    return {r["name"]: r["owner_email"] for r in rows}


def list_machines() -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT name, owner_email, os_type, shared, added_at, last_seen, "
            "(token IS NOT NULL) AS enrolled FROM machines ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def seed_machine(name: str, owner_email: str, os_type: str, shared: bool = False) -> None:
    """Insert a pre-existing machine (legacy shared-token) if not already present."""
    with _lock:
        c = _db()
        c.execute("INSERT OR IGNORE INTO machines(name, owner_email, os_type, shared) VALUES(?,?,?,?)",
                  (name, owner_email.lower(), os_type, 1 if shared else 0))
        c.commit()
