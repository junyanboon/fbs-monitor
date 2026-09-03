"""Selection rules for the Booking Status write-back.

This is the only place this repo writes to Notion, and it writes to a field an
agent also owns. The predicate is therefore the whole safety story: flip the
unambiguous case, never touch anything requiring judgment. These tests pin that
boundary — a change that makes one of them pass differently is a change to what
the desk will silently mark Complete.
"""
import contextlib
import io
import os
from datetime import datetime

import pytest

import build

NOW = datetime(2026, 8, 11, 14, 22, tzinfo=build.TZ)   # 14:22 Toronto


def booking(**kw):
    """A booking that SHOULD flip; override one field per case."""
    e = {
        "kind": "booking", "who": "Test Renter", "studio": "901",
        "start": 12.0, "end": 13.0, "arrived": "11:48", "departed": "13:07",
        "_notion_id": "page-id", "_board_status": "In Studio",
    }
    e.update(kw)
    return e


def would_flip(event, fallback=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        build.sync_booking_status({"events": [event]}, fallback, NOW)
    return "would set Complete" in buf.getvalue()


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch):
    """Never let these tests reach the network."""
    monkeypatch.setenv("NOTION_TOKEN", "fake")
    monkeypatch.setenv("BOOKING_STATUS_SYNC_DRYRUN", "1")
    monkeypatch.delenv("BOOKING_STATUS_SYNC_DISABLED", raising=False)


@pytest.mark.parametrize("label,event", [
    # The case this was built for: Kristel San Jose, 2026-08-11. Armed and left
    # at 13:07; the board still read In Studio 75 minutes later, holding her PBF
    # hostage to an agent pass.
    ("departed after a completed booking", booking()),
    ("an Upcoming row with the same facts", booking(_board_status="Upcoming")),
])
def test_unambiguously_finished_bookings_flip(label, event):
    assert would_flip(event) is True


@pytest.mark.parametrize("label,event", [
    # Judgment cases — all of these stay the Concierge's to classify.
    ("no-show: never arrived", booking(arrived=None, departed=None)),
    ("arrived but has not left yet", booking(departed=None)),
    ("still inside the booking window", booking(start=14.0, end=15.0)),
    # `end` counts decimal hours from the window's base day, so a 2:15 AM finish
    # is 26.25. Comparing against a naive clock hour would flip this one early.
    ("cross-midnight booking still running", booking(start=22.0, end=26.25)),
    # Never re-write a settled row.
    ("already Complete", booking(_board_status="Complete")),
    # No Notion row matched, so there is nothing to write and no way to be sure.
    ("unmatched to a Notion row", booking(_notion_id=None)),
])
def test_anything_requiring_judgment_is_left_alone(label, event):
    assert would_flip(event) is False


def test_never_writes_back_board_fallback_data():
    """On fallback, arrival/departure came from the board's own Armed/Disarmed
    columns rather than the panel. Writing that back would launder the board's
    guess into a fact."""
    assert would_flip(booking(), fallback=True) is False


def test_kill_switch_stops_all_writes(monkeypatch):
    monkeypatch.setenv("BOOKING_STATUS_SYNC_DISABLED", "1")
    assert would_flip(booking()) is False


# ────────────────────────────────────────────────────────────────────────────
# The pipeline seam
# ───────────────────────────────────────────────────────────────────────────────
# Every test above hands sync_booking_status a hand-built event, so all of them
# passed while the real board flipped nothing for weeks: build_data projected
# its events through a clean dict that dropped `_notion_id`, and the sync's
# first guard skips — without logging — when it is missing. Reproduced
# 2026-08-16 with Hannah Cho in 509B: arrived 14:00, departed 16:09, row still
# In Studio, rebuild printed no sync attempt at all. These tests walk the real
# path: join_notion → prepare_board_events → sync.

def _joined_event(status="In Studio"):
    """One booking, matched to a Notion row the way build_data matches it."""
    events = [{
        "kind": "booking", "who": "Hannah Cho", "studio": "509B",
        "start": 14.0, "end": 16.0, "arrived": "14:00", "departed": "16:09",
        "tier": None, "gtg": None, "hta": None,
    }]
    rows = [{
        "id": "notion-row-509b", "studio": "509B",
        "start": "2:00 PM", "end": "4:00 PM",
        "tier": None, "gtg": None, "hta": None, "has_code": True,
        "board_disarmed": None, "board_armed": None, "status": status,
    }]
    return build.prepare_board_events(build.join_notion(events, rows))[0]


def test_prepared_board_event_still_carries_its_notion_row_for_sync():
    e = _joined_event()
    assert e["_notion_id"] == "notion-row-509b"
    assert e["_board_status"] == "In Studio"


def test_an_arrived_and_departed_past_end_row_attempts_exactly_one_patch(monkeypatch):
    """The end-to-end claim: one PATCH to Complete, for that row, and only it."""
    monkeypatch.delenv("BOOKING_STATUS_SYNC_DRYRUN", raising=False)
    calls = []

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        return type("R", (), {"status_code": 200, "text": ""})()

    monkeypatch.setattr(build.requests, "patch", fake_patch)
    data = {"events": [_joined_event(), {"kind": "staff", "who": "Cleaner"}]}
    with contextlib.redirect_stdout(io.StringIO()):
        build.sync_booking_status(data, False, datetime(2026, 8, 16, 16, 20, tzinfo=build.TZ))

    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/notion-row-509b")
    assert payload["properties"]["Booking Status"]["status"]["name"] == "Complete"


def test_internal_fields_never_reach_the_published_page():
    data = {"events": [_joined_event()], "date": "Sunday, August 16, 2026"}
    published = build.public_data(data)["events"][0]
    assert "_notion_id" not in published and "_board_status" not in published
    assert published["who"] == "Hannah Cho"      # the real fields survive
    assert data["events"][0]["_notion_id"]       # and the sync's copy is untouched
