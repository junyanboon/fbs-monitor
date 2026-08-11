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
