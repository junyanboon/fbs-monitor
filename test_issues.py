"""The Issues tab is the human interface — burial here is the failure.

Pinned by the 2026-08-24 board: five "Retire alarm code" housekeeping rows
rendered while the only row about a renter who actually could not get in
(Akira Huang, NO Alarm.com user, 509B Fri Aug 28 — raised with 5 days of lead)
was filtered out by the today+tomorrow horizon, because it carried a date.
These tests assert the inversion of that: dated door gaps always show, with
their fuse; housekeeping collapses to one line that sorts after them.
"""
from datetime import datetime

import build

NOW = datetime(2026, 8, 24, 21, 15, tzinfo=build.TZ)      # the real board's clock


def _row(request, rtype="Access / PIN", by="The Doorman", requested="2026-08-23T10:00:00Z"):
    return {
        "created_time": requested,
        "properties": {
            "Request": {"type": "title", "title": [{"plain_text": request}]},
            "Type": {"type": "select", "select": {"name": rtype}},
            "Raised by": {"type": "select", "select": {"name": by}},
            "Requested": {"type": "created_time", "created_time": requested},
        },
    }


def issues_for(rows, monkeypatch, reports=None):
    monkeypatch.setattr(build, "_notion_query", lambda token, ds, body: rows)
    return build.fetch_issues("fake-token", NOW, reports)


# ── the regression itself ────────────────────────────────────────────────────

def test_a_dated_forward_gap_is_never_buried(monkeypatch):
    """Akira Huang, Aug 28, raised Aug 23 — the row the 2026-08-24 board hid."""
    issues, total = issues_for(
        [_row("Booking sweep — access unresolved for Akira Huang at 509B Aug 28 14:00")],
        monkeypatch)
    assert total == 1
    (i,) = issues
    assert "Akira Huang" in i["text"]
    assert "4d" in i["text"]                  # the fuse is on the wall
    assert i["level"] == "watch"              # four days out is not yet a siren


def test_housekeeping_collapses_and_sorts_after_real_gaps(monkeypatch):
    rows = [_row(f"Retire alarm code — Renter {n} (idle 30d+, no future bookings)")
            for n in range(5)]
    rows.append(_row("Booking sweep — access unresolved for Akira Huang at 509B Aug 28 14:00"))
    issues, total = issues_for(rows, monkeypatch)
    assert [i["label"] for i in issues] == ["Access", "Housekeeping"]
    assert "×5" in issues[1]["text"]
    assert total == 6                         # "+N more" still counts rows


# ── the fuse burning down ────────────────────────────────────────────────────

def test_gap_due_tomorrow_is_crit(monkeypatch):
    issues, _ = issues_for(
        [_row("Access unresolved for A. Renter at 693 Aug 25 19:00")], monkeypatch)
    assert issues[0]["level"] == "crit"
    assert "tomorrow" in issues[0]["text"]


def test_overdue_gap_stays_crit_and_says_so(monkeypatch):
    """Megan Cartwright's compromised code does not go quiet past Aug 30."""
    issues, _ = issues_for(
        [_row("🔑 Rotate compromised alarm code before Aug 23; do not decommission")],
        monkeypatch)
    assert issues[0]["level"] == "crit"
    assert "OVERDUE 1d" in issues[0]["text"]


def test_soonest_fuse_sorts_first(monkeypatch):
    issues, _ = issues_for([
        _row("Access unresolved for Far Gap at 901 Sep 06 13:30"),
        _row("Access unresolved for Near Gap at 509B Aug 27 14:00"),
    ], monkeypatch)
    assert "Near Gap" in issues[0]["text"]
    assert "Far Gap" in issues[1]["text"]


# ── what must not change ─────────────────────────────────────────────────────

def test_undated_access_row_still_shows(monkeypatch):
    """Silence must never hide a live problem — no date, still on the wall."""
    issues, _ = issues_for(
        [_row("Alarm code needs a person — Aahuti Dave [Tagvenue]")], monkeypatch)
    assert len(issues) == 1
    assert issues[0]["level"] == "watch"


def test_money_shaped_rows_never_reach_the_public_page(monkeypatch):
    issues, total = issues_for(
        [_row("Charge Kristel San Jose $86.75 for overtime", rtype="Charge")],
        monkeypatch)
    assert issues == [] and total == 0
