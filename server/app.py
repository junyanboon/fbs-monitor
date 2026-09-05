"""FBS Monitor claim server — the Vercel edition of the board.

Serves the two built pages behind a staff login and adds what a static page
cannot do: know who is reading, and let them claim an open shift.

    GET  /, /index.html, /mobile.html   the built pages (login required)
    GET  /login  POST /login            email + password → signed cookie
    POST /logout
    GET  /account  POST /account        change password
    GET  /api/me                        {name, email}
    GET  /api/claims                    claims for the open-shift lookahead
    POST /api/claim  {id}               claim an open shift (writes the Staff calendar)
    DELETE /api/claim/{id}              release your own claim
    GET  /healthz

This process does not build. GitHub Actions runs build.py every ~15 minutes
exactly as it always has, then publish.py uploads the built files into the
`pages` table (server/db.py) — a serverless function has no disk that
survives a request, so the database is where the edition lives. A claim shows
on the board at once through the claims overlay both pages carry; the next
Actions run then sees the calendar event and drops the placeholder for good.

On a laptop with no DATABASE_URL the same app reads the files build.py wrote
beside it, so `uvicorn server.app:app` works against a local build.

What is public without a login: sw.js, icons, manifests, version.json (a
timestamp). Everything that carries a renter's name is behind the cookie.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import auth, db as dbmod, gcal

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("FBS_SITE_DIR") or HERE.parent)   # where build.py writes locally
TZ = ZoneInfo("America/Toronto")

# Built by build.py, uploaded by publish.py, read from the `pages` table
# (disk fallback for a laptop). Never served to a reader: open-shifts.json —
# the API reads it, the page never needs it.
DB_PAGES = {"index.html", "mobile.html", "version.json", "open-shifts.json",
            "booking-state.json", "panel-state.json"}
PROTECTED_FILES = {"index.html", "mobile.html", "booking-state.json", "panel-state.json"}
PUBLIC_FILES = {
    "sw.js", "version.json", "favicon.svg", "manifest.webmanifest",
    "manifest-mobile.webmanifest", "icon-192.png", "icon-512.png",
    "icon-maskable-512.png", "mobile-icon-192.png", "mobile-icon-512.png",
    "mobile-icon-maskable-512.png", "apple-touch-icon.png", "apple-touch-icon-mobile.png",
}
NO_STORE = {"index.html", "mobile.html", "version.json", "sw.js",
            "booking-state.json", "panel-state.json"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
                 ".js": "text/javascript; charset=utf-8"}

app = FastAPI(title="FBS Monitor", docs_url=None, redoc_url=None, openapi_url=None)


def db():
    # One connection per request: on Vercel each invocation may be a fresh
    # process, and a pooled Postgres URL makes this cheap.
    con = auth.connect()
    try:
        yield con
    finally:
        con.close()


# ── identity ─────────────────────────────────────────────────────────────────
def current_user(request: Request, con=Depends(db)) -> auth.User | None:
    uid = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    return auth.get_user(con, uid) if uid else None


def require_user(request: Request, user: auth.User | None = Depends(current_user)) -> auth.User:
    if user is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, "Sign in required")
        raise HTTPException(302, headers={"Location": _login_url(request)})
    return user


def _login_url(request: Request) -> str:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"/login?next={quote(nxt, safe='')}"


def _safe_next(nxt: str | None) -> str:
    # Only same-site relative paths. "//evil" and "http://…" both fall through.
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


def _set_session(resp: Response, user: auth.User, request: Request) -> None:
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user),
                    max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https"
                           or request.headers.get("x-forwarded-proto") == "https",
                    path="/")


# ── built files ──────────────────────────────────────────────────────────────
def _read_built(con, name: str) -> str | None:
    """A built file: the database first (what publish.py uploaded), then the
    disk beside build.py (a laptop running against its own build)."""
    body = auth.get_page(con, name) if name in DB_PAGES else None
    if body is None:
        path = ROOT / name
        if path.is_file():
            body = path.read_text(encoding="utf-8")
    return body


def _file(con, name: str) -> Response:
    headers = {"Cache-Control": "no-store"} if name in NO_STORE else {}
    if name == "sw.js":
        headers["Service-Worker-Allowed"] = "/"
    if name in DB_PAGES:
        body = _read_built(con, name)
        if body is None:
            raise HTTPException(404, f"{name} not published yet")
        return Response(body, media_type=CONTENT_TYPES.get(Path(name).suffix, "text/plain"),
                        headers=headers)
    path = ROOT / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers=headers)


@app.get("/")
def home(user: auth.User = Depends(require_user), con=Depends(db)):
    return _file(con, "index.html")


@app.get("/healthz")
def healthz(con=Depends(db)):
    v = _read_built(con, "version.json")
    built = json.loads(v)["generatedAtISO"] if v else None
    return {"ok": True, "built": built, "calendar": gcal.configured(),
            "db": "postgres" if dbmod.is_postgres() else "sqlite"}


# ── login / logout / account ────────────────────────────────────────────────
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light"><title>{title} · FBS Monitor</title>
<link rel="icon" href="favicon.svg"><link rel="apple-touch-icon" href="apple-touch-icon-mobile.png">
<style>
:root{{--bg:#0f1115;--card:#171a21;--line:#2a2f3a;--ink:#e8eaf0;--mut:#9aa3b2;--acc:#f5b400;--bad:#ff6b6b}}
@media(prefers-color-scheme:light){{:root{{--bg:#f4f5f8;--card:#fff;--line:#dfe3ea;--ink:#141821;--mut:#5d6675}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;padding:24px}}
form{{width:min(380px,100%);background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px}}
h1{{margin:0 0 4px;font-size:22px}}h1 em{{font-style:normal;color:var(--acc)}}p{{margin:0 0 18px;color:var(--mut);font-size:14px}}
label{{display:block;font-size:13px;color:var(--mut);margin:12px 0 5px}}
input{{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font-size:16px}}
button{{width:100%;margin-top:18px;padding:12px;border:0;border-radius:9px;background:var(--acc);color:#111;font-weight:700;font-size:16px}}
.err{{color:var(--bad);font-size:14px;margin:12px 0 0}}.ok{{color:#5cd68a;font-size:14px;margin:12px 0 0}}
a{{color:var(--mut);font-size:13px;display:inline-block;margin-top:16px}}
</style></head><body>{body}</body></html>"""


def _login_page(error: str = "", nxt: str = "/", email: str = "") -> HTMLResponse:
    body = f"""<form method="post" action="/login" autocomplete="on">
<h1>FBS <em>Monitor</em></h1><p>Dance Annex staff sign-in</p>
<label for="e">Email</label><input id="e" name="email" type="email" required autofocus value="{_esc(email)}" autocomplete="username">
<label for="p">Password</label><input id="p" name="password" type="password" required autocomplete="current-password">
<input type="hidden" name="next" value="{_esc(nxt)}">
{f'<div class="err">{_esc(error)}</div>' if error else ''}
<button type="submit">Sign in</button></form>"""
    return HTMLResponse(_PAGE.format(title="Sign in", body=body),
                        status_code=401 if error else 200,
                        headers={"Cache-Control": "no-store"})


def _account_page(user: auth.User, error: str = "", ok: str = "") -> HTMLResponse:
    note = ('<div class="err">You are on a temporary password. Set your own now.</div>'
            if user.must_change else "")
    body = f"""<form method="post" action="/account">
<h1>Hi, <em>{_esc(user.name)}</em></h1><p>{_esc(user.email)} · change your password</p>{note}
<label for="c">Current password</label><input id="c" name="current" type="password" required autocomplete="current-password">
<label for="n">New password (12+ characters)</label><input id="n" name="new" type="password" required minlength="12" autocomplete="new-password">
{f'<div class="err">{_esc(error)}</div>' if error else ''}{f'<div class="ok">{_esc(ok)}</div>' if ok else ''}
<button type="submit">Save password</button>
<a href="/">← back to the board</a> &nbsp; <a href="#" onclick="fetch('/logout',{{method:'POST'}}).then(()=>location.href='/login');return false">Sign out</a>
</form>"""
    return HTMLResponse(_PAGE.format(title="Account", body=body),
                        headers={"Cache-Control": "no-store"})


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


@app.get("/login")
def login_form(request: Request, next: str = "/", user: auth.User | None = Depends(current_user)):
    if user:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(nxt=_safe_next(next))


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          next: str = Form("/"), con=Depends(db)):
    addr = request.headers.get("x-forwarded-for", request.client.host if request.client else "?")
    addr = addr.split(",")[0].strip()
    if not auth.login_allowed(con, addr):
        return _login_page("Too many attempts. Wait ten minutes.", _safe_next(next), email)
    user = auth.authenticate(con, email, password)
    if not user:
        auth.login_failed(con, addr)
        return _login_page("Wrong email or password.", _safe_next(next), email)
    target = "/account" if user.must_change else _safe_next(next)
    resp = RedirectResponse(target, status_code=303)
    _set_session(resp, user, request)
    return resp


@app.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/account")
def account_form(user: auth.User = Depends(require_user)):
    return _account_page(user)


@app.post("/account")
def account_change(request: Request, current: str = Form(...), new: str = Form(...),
                   user: auth.User = Depends(require_user), con=Depends(db)):
    if not auth.authenticate(con, user.email, current):
        return _account_page(user, error="Current password is wrong.")
    if len(new) < 12:
        return _account_page(user, error="Use at least 12 characters.")
    auth.set_password(con, user.id, new, must_change=False)
    user.must_change = False
    resp = _account_page(user, ok="Saved. You are signed in on this device for 30 days.")
    _set_session(resp, user, request)
    return resp


# ── API ──────────────────────────────────────────────────────────────────────
@app.get("/api/me")
def api_me(user: auth.User = Depends(require_user)):
    return JSONResponse(user.public(), headers={"Cache-Control": "no-store"})


def _open_shifts(con) -> dict:
    body = _read_built(con, "open-shifts.json")
    if body is None:
        return {"openShifts": [], "shiftBaseDay": None}
    return json.loads(body)


@app.get("/api/claims")
def api_claims(user: auth.User = Depends(require_user), con=Depends(db)):
    since = (date.today() - timedelta(days=1)).isoformat()
    return JSONResponse([auth.public_claim(c) for c in auth.recent_claims(con, since)],
                        headers={"Cache-Control": "no-store"})


@app.post("/api/claim")
async def api_claim(request: Request, user: auth.User = Depends(require_user), con=Depends(db)):
    body = await request.json()
    shift_id = str(body.get("id") or "").strip()
    if not shift_id:
        raise HTTPException(400, "id required")
    existing = auth.get_claim(con, shift_id)
    if existing:
        if existing["user_id"] == user.id:
            return JSONResponse(auth.public_claim(existing))        # idempotent replay
        raise HTTPException(409, f"Already claimed by {existing['name']}")
    shift = next((s for s in _open_shifts(con)["openShifts"] if s.get("id") == shift_id), None)
    if shift is None:
        raise HTTPException(404, "That shift is no longer open")
    if datetime.fromisoformat(shift["endISO"]) <= datetime.now(TZ):
        raise HTTPException(410, "That shift has already ended")

    event_id = None
    if gcal.configured():
        try:
            event_id = gcal.create_event(user.name, user.email, shift)
        except Exception as exc:  # noqa: BLE001 — surface it, never half-claim
            raise HTTPException(502, f"Calendar write failed: {exc}") from exc
    elif os.environ.get("CLAIM_WITHOUT_CALENDAR") != "1":
        raise HTTPException(503, "Calendar write is not configured on this server")

    claim = auth.add_claim(con, user, shift, event_id)
    return JSONResponse(auth.public_claim(claim), status_code=201)


@app.delete("/api/claim/{shift_id}")
def api_unclaim(shift_id: str, user: auth.User = Depends(require_user), con=Depends(db)):
    existing = auth.get_claim(con, shift_id)
    if not existing:
        raise HTTPException(404, "No such claim")
    if existing["user_id"] != user.id:
        raise HTTPException(403, f"That claim belongs to {existing['name']}")
    if existing.get("gcal_event_id") and gcal.configured():
        try:
            gcal.delete_event(existing["gcal_event_id"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Calendar delete failed: {exc}") from exc
    auth.remove_claim(con, shift_id)
    return {"ok": True}


# Declared last: a catch-all for one path segment must sit behind /login,
# /account and /healthz or it swallows them. On Vercel the static files
# (icons, sw.js, manifests) are served by the platform before this runs; the
# branch below is the laptop path and the safety net.
@app.get("/{name}")
def asset(name: str, request: Request, user: auth.User | None = Depends(current_user),
          con=Depends(db)):
    if name in PUBLIC_FILES:
        return _file(con, name)
    if name in PROTECTED_FILES:
        if user is None:
            raise HTTPException(302, headers={"Location": _login_url(request)})
        return _file(con, name)
    raise HTTPException(404)
