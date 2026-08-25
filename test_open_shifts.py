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
from datetime import date, datetime

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
    assert set(o) == {"role", "studio", "day_offset", "start", "end"}, o
    for v in o.values():
        s = str(v)
        assert "Mark" not in s and "Paid" not in s, o


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


def main():
    test_real_placeholder_shapes()
    test_description_never_leaks()
    test_assigned_and_noise_are_not_open()
    test_day_offset_and_times()
    print("open-shift regression tests: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
