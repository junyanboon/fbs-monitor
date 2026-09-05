#!/usr/bin/env python3
"""Regression guards for open-shift (claimable placeholder) parsing.

The Shifts tab advertises unassigned placeholders from the Staff Scheduling
calendar on a PUBLIC page. Two things must never break:

  1. A placeholder must never be mistaken for a person's shift (or vice versa) —
     "Need FBS" as staff would count as monitored-booking coverage on the
     desktop panel, hiding a real gap.
  2. Nothing from the event DESCRIPTION except the studio number may reach the
     output — descriptions carry renter names and [Paid] markers.

Every title here is a real shape from the Staff Scheduling calendar.

Run: python test_open_shifts.py   (also runs in CI before the build)
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import build

TZ = build.TZ
BASE = date(2026, 8, 25)


def _dt(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def parse(summary, desc="", day=BASE, h1=12, m1=45, h2=13, m2=15):
    return build.parse_open_shift(summary, desc, _dt(day, h1, m1), _dt(day, h2, m2), BASE)


def test_real_placeholder_shapes():
    o = parse("Need FBS", "Mark Jennings (Midnight Seven): Mark Jennings  (Studio 901 (Elements)) [Paid]")
    assert o and o["role"] == "FBS" and o["studio"] == "901", o
    o = parse("Need Monitoring", "Tamina Pollack-Paris: (Studio 509B) [Paid]")
    assert o and o["role"] == "Monitoring" and o["studio"] == "509B", o
    assert parse("Need Monitor")["role"] == "Monitoring"
    assert parse("Studio Viewing Support")["role"] == "Viewing"
    assert parse("Open/Close the Studio")["role"] == "Open/Close"


def test_description_never_leaks():
    # Only the studio number may come out of a description — renter names and
    # money markers stay behind. The output is a closed set of fields.
    o = parse("Need FBS", "Mark Jennings (Midnight Seven): (Studio 901) [Paid]")
    assert set(o) == {"role", "studio", "day_offset", "start", "end",
                      "id", "date", "startISO", "endISO"}, o
    for v in o.values():
        s = str(v)
        assert "Mark" not in s and "Paid" not in s, o


def test_claim_id_is_stable_and_public_only():
    # The claim server keys on `id`. It must come out the same on every build
    # (a claim posted against one build must still resolve on the next) and be
    # built from the public fields only — never the calendar UID.
    a = parse("Need FBS", "Renter A: (Studio 901) [Paid]")
    b = parse("Need FBS", "Renter B: (Studio 901) [Paid]")
    assert a["id"] == b["id"] and len(a["id"]) == 12
    c = parse("Need FBS", "Renter A: (Studio 509B) [Paid]")
    assert c["id"] != a["id"]
    assert a["startISO"].startswith("2026-08-25T12:45") and a["date"] == "2026-08-25"


def test_assigned_and_noise_are_not_open():
    assert parse("Ela Krystin FBS", "booking line") is None
    assert parse("Junyan Accounting") is None
    assert parse("Junyan Kyjah meeting") is None
    # and the staff parser keeps rejecting placeholders (rule 1 above)
    assert build.parse_staff_row("Need FBS", _dt(BASE, 12), _dt(BASE, 13), BASE) is None
    assert build.parse_staff_row("Need Monitoring", _dt(BASE, 12), _dt(BASE, 13), BASE) is None


def test_day_offset_and_times():
    o = parse("Need Monitoring", day=date(2026, 8, 26), h1=14, m1=45, h2=15, m2=15)
    assert o["day_offset"] == 1, o
    assert abs(o["start"] - 14.75) < 1e-6 and abs(o["end"] - 15.25) < 1e-6, o
    assert parse("Need FBS")["day_offset"] == 0


def assignment(summary="Ela FBS", desc="Studio 901", day=BASE,
               h1=12, m1=45, h2=13, m2=15):
    return build.parse_staff_assignment(
        summary, desc, _dt(day, h1, m1), _dt(day, h2, m2), BASE)


def test_assignment_projection_is_private_and_minimal():
    a = assignment(desc="Renter Name (Studio 901) [Paid $500]")
    assert a["name"] == "Ela" and a["role"] == "FBS" and a["studio"] == "901", a
    assert set(a) == {"name", "role", "studio", "day_offset", "start", "end"}, a
    assert all("Renter" not in str(v) and "Paid" not in str(v) for v in a.values())


def test_reconciliation_removes_only_proven_claims():
    now = _dt(BASE, 10)
    claimed = parse("Need FBS", "Studio 901")
    other_studio = parse("Need FBS", "Studio 527")
    monitoring = parse("Need Monitoring", "Studio 509A")
    out = build.reconcile_open_shifts(
        [claimed, other_studio, monitoring],
        [assignment(desc="Studio 901")], now, BASE)
    assert claimed not in out
    assert other_studio in out and monitoring in out


def test_ambiguous_assignment_without_studio_fails_open():
    now = _dt(BASE, 10)
    studio_901 = parse("Need FBS", "Studio 901")
    studio_527 = parse("Need FBS", "Studio 527")
    out = build.reconcile_open_shifts(
        [studio_901, studio_527], [assignment(desc="")], now, BASE)
    assert out == [studio_901, studio_527]


def test_unique_assignment_without_studio_can_clear_one_gap():
    now = _dt(BASE, 10)
    only = parse("Need FBS", "Studio 901")
    assert build.reconcile_open_shifts([only], [assignment(desc="")], now, BASE) == []


def test_expired_and_cross_midnight_shifts():
    expired = parse("Need FBS", "Studio 527", h1=9, h2=10)
    overnight = parse("Need FBS", "Studio 693", h1=23, m1=45,
                      h2=0, m2=15, day=BASE)
    # A real cross-midnight event's end belongs to the following date.
    overnight["end"] = 24.25
    before_midnight = _dt(BASE, 21)
    assert build.reconcile_open_shifts([expired, overnight], [], before_midnight, BASE) == [overnight]
    after_end = _dt(BASE + timedelta(days=1), 0, 16)
    assert build.reconcile_open_shifts([overnight], [], after_end, BASE) == []


def test_templates_age_out_open_rows_between_rebuilds():
    root = Path(__file__).parent
    for template in ("template.html", "template-mobile.html"):
        text = (root / template).read_text()
        assert "shiftNowHour" in text
        assert "o.day_offset*24+o.end>shiftNow" in text


def test_fetch_reconciles_the_same_lookahead_feed():
    def event(summary, desc, start, end):
        return {"summary": summary, "description": desc,
                "dtstart": start, "dtend": end, "cancelled": False}

    tomorrow = BASE + timedelta(days=1)
    occurrences = [
        event("Need FBS", "Studio 901", _dt(BASE, 12, 45), _dt(BASE, 13, 15)),
        event("Ela FBS", "Studio 901", _dt(BASE, 12, 45), _dt(BASE, 13, 15)),
        event("Need Monitoring", "Studio 509A", _dt(tomorrow, 16, 45),
              _dt(tomorrow, 17, 15)),
    ]
    original = build.fetch_ics
    build.fetch_ics = lambda *_: occurrences
    try:
        out = build.fetch_open_shifts(
            {"Staff": "private-feed"}, _dt(BASE, 5), BASE, _dt(BASE, 10))
    finally:
        build.fetch_ics = original
    assert len(out) == 1, out
    assert out[0]["day"] == "Tomorrow" and out[0]["role"] == "Monitoring", out


def main():
    test_real_placeholder_shapes()
    test_description_never_leaks()
    test_assigned_and_noise_are_not_open()
    test_day_offset_and_times()
    test_assignment_projection_is_private_and_minimal()
    test_reconciliation_removes_only_proven_claims()
    test_ambiguous_assignment_without_studio_fails_open()
    test_unique_assignment_without_studio_can_clear_one_gap()
    test_expired_and_cross_midnight_shifts()
    test_templates_age_out_open_rows_between_rebuilds()
    test_fetch_reconciles_the_same_lookahead_feed()
    print("open-shift regression tests: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
