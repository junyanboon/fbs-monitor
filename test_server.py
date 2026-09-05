#!/usr/bin/env python3
"""Claim server guards (server/app.py). No network: the calendar writer is
replaced with a recorder, the database is SQLite in a temp folder (server/db.py
picks Postgres only when DATABASE_URL is set), and the built files are read
from disk the way a laptop would — the same _read_built path Vercel uses,
minus the upload.

What must hold:
  1. Nothing that carries a renter's name is served without a session.
  2. A claim is written to the calendar exactly once and only for an id that
     is still in open-shifts.json — a stale id, an ended shift, or someone
     else's claim are all refused with a reason the page can show.
  3. A second claim on the same id by the same person is a harmless replay
     (the offline outbox re-sends), never a second calendar event.

Run: python -m pytest test_server.py
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("America/Toronto")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FBS_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("FBS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FBS_SITE_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)      # SQLite backend under test
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "sa.json"))
    monkeypatch.setenv("STAFF_CALENDAR_ID", "staff@example")
    (tmp_path / "sa.json").write_text("{}")
    (tmp_path / "index.html").write_text("<!doctype html>BOARD Mark Jennings")
    (tmp_path / "mobile.html").write_text("<!doctype html>MOBILE")
    (tmp_path / "sw.js").write_text("// sw")
    (tmp_path / "version.json").write_text('{"generatedAtISO":"2026-09-05T10:00:00-04:00"}')
    now = datetime.now(TZ)
    soon = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    gone = (now - timedelta(hours=3)).replace(second=0, microsecond=0)
    shifts = [
        {"id": "abc123abc123", "role": "FBS", "studio": "509B", "day_offset": 0,
         "start": 18, "end": 20, "date": soon.date().isoformat(),
         "startISO": soon.isoformat(), "endISO": (soon + timedelta(hours=2)).isoformat()},
        {"id": "ended0ended0", "role": "Monitoring", "studio": "901", "day_offset": 0,
         "start": 9, "end": 10, "date": gone.date().isoformat(),
         "startISO": (gone - timedelta(hours=1)).isoformat(), "endISO": gone.isoformat()},
    ]
    (tmp_path / "open-shifts.json").write_text(json.dumps({"openShifts": shifts}))

    for m in [m for m in sys.modules if m.startswith("server")]:
        del sys.modules[m]
    from server import app as appmod, auth, gcal
    calls = []
    monkeypatch.setattr(gcal, "create_event", lambda name, email, shift: calls.append(("add", name, shift["id"])) or "evt-1")
    monkeypatch.setattr(gcal, "delete_event", lambda eid: calls.append(("del", eid)))
    con = auth.connect()
    auth.add_user(con, "KyJah", "kyjah@example.com", "temporary-pass-123", must_change=False)
    auth.add_user(con, "Ela", "ela@example.com", "another-pass-1234", must_change=False)
    con.close()
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def login(c, email="kyjah@example.com", pw="temporary-pass-123"):
    r = c.post("/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def test_pages_need_a_session(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    r = client.get("/mobile.html", follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/claims").status_code == 401
    # the offline shell stays reachable — the worker installs before anyone signs in
    assert client.get("/sw.js").status_code == 200
    assert client.get("/version.json").status_code == 200
    assert client.get("/healthz").json()["ok"] is True
    # and nothing else leaks through the catch-all
    assert client.get("/build.py").status_code == 404
    assert client.get("/open-shifts.json").status_code == 404


def test_login_then_board_and_identity(client):
    assert "Wrong email" in client.post("/login", data={"email": "kyjah@example.com",
                                                        "password": "nope", "next": "/"}).text
    login(client)
    r = client.get("/")
    assert r.status_code == 200 and "Mark Jennings" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/me").json()["name"] == "KyJah"
    client.post("/logout")
    assert client.get("/api/me").status_code == 401


def test_open_redirect_is_refused(client):
    r = client.post("/login", data={"email": "kyjah@example.com", "password": "temporary-pass-123",
                                    "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_claim_writes_calendar_once_and_is_idempotent(client):
    login(client)
    r = client.post("/api/claim", json={"id": "abc123abc123"})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "KyJah" and r.json()["id"] == "abc123abc123"
    # outbox replay: same person, same id → same answer, no second event
    r = client.post("/api/claim", json={"id": "abc123abc123"})
    assert r.status_code == 200
    assert client.calls == [("add", "KyJah", "abc123abc123")]
    claims = client.get("/api/claims").json()
    assert [c["id"] for c in claims] == ["abc123abc123"]
    assert set(claims[0]) == {"id", "name", "role", "studio", "date", "startISO", "endISO", "claimedAt"}


def test_claim_refusals(client):
    login(client)
    assert client.post("/api/claim", json={"id": "nope"}).status_code == 404
    assert client.post("/api/claim", json={"id": "ended0ended0"}).status_code == 410
    client.post("/api/claim", json={"id": "abc123abc123"})
    client.post("/logout")
    login(client, "ela@example.com", "another-pass-1234")
    r = client.post("/api/claim", json={"id": "abc123abc123"})
    assert r.status_code == 409 and "KyJah" in r.json()["detail"]
    r = client.delete("/api/claim/abc123abc123")
    assert r.status_code == 403
    assert client.calls == [("add", "KyJah", "abc123abc123")]


def test_release_deletes_the_event(client):
    login(client)
    client.post("/api/claim", json={"id": "abc123abc123"})
    assert client.delete("/api/claim/abc123abc123").status_code == 200
    assert client.calls == [("add", "KyJah", "abc123abc123"), ("del", "evt-1")]
    assert client.get("/api/claims").json() == []


def test_calendar_failure_leaves_no_claim(client, monkeypatch):
    from server import gcal
    def boom(*a):
        raise RuntimeError("Calendar insert failed: 403")
    monkeypatch.setattr(gcal, "create_event", boom)
    login(client)
    r = client.post("/api/claim", json={"id": "abc123abc123"})
    assert r.status_code == 502
    assert client.get("/api/claims").json() == []


def test_event_title_matches_the_builders_identity_rule():
    from server import gcal
    sys.path.insert(0, str(Path(__file__).parent))
    import build
    shift = {"id": "x", "role": "FBS", "studio": "509B",
             "startISO": "2026-09-06T18:00:00-04:00", "endISO": "2026-09-06T20:00:00-04:00"}
    body = gcal.event_body("KyJah", "kyjah@example.com", shift)
    assert build._staff_identity(body["summary"]) == ("KyJah", "FBS")
    assert build.RE_SHIFT_STUDIO.search(body["description"]).group(1) == "509B"
    shift["role"] = "Monitoring"
    assert build._staff_identity(gcal.event_body("Ela", "e@x", shift)["summary"]) == ("Ela", "Monitoring")
    shift["role"] = "Viewing"
    assert build._staff_identity(gcal.event_body("Donny", "d@x", shift)["summary"]) == ("Donny", "Viewing")


def test_published_pages_win_over_disk(client):
    """publish.py puts the edition in the database; the app serves that copy
    first and falls back to disk only when the table is empty (a laptop)."""
    from server import auth
    con = auth.connect()
    auth.put_page(con, "index.html", "<!doctype html>FROM DB")
    auth.put_page(con, "version.json", '{"generatedAtISO":"2026-09-06T09:00:00-04:00"}')
    con.close()
    login(client)
    assert client.get("/").text == "<!doctype html>FROM DB"
    assert client.get("/version.json").json()["generatedAtISO"].startswith("2026-09-06")
    assert client.get("/healthz").json()["built"].startswith("2026-09-06")
    # mobile.html was never published → disk copy still answers
    assert client.get("/mobile.html").text == "<!doctype html>MOBILE"


def test_login_throttle_is_stored_not_in_memory(client):
    for _ in range(10):
        client.post("/login", data={"email": "kyjah@example.com", "password": "nope", "next": "/"})
    r = client.post("/login", data={"email": "kyjah@example.com",
                                    "password": "temporary-pass-123", "next": "/"})
    assert r.status_code == 401 and "Too many attempts" in r.text


def test_placeholders_are_rewritten_for_postgres():
    from server import db
    con = db.Connection(raw=None, postgres=True)
    assert con._sql("SELECT * FROM x WHERE a=? AND b=?") == "SELECT * FROM x WHERE a=%s AND b=%s"
    assert db.Connection(raw=None, postgres=False)._sql("a=?") == "a=?"


def test_publish_skips_without_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import publish
    assert publish.main() == 0
    assert "skipped" in capsys.readouterr().out
