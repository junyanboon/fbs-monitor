"""The red access pill — the app's ONE access surface (Junyan, 2026-08-25).

The Issues tab this file used to test is gone; its history is the reason this
matcher is tested hard. An open Access / PIN row must light up exactly the
booking it is about: a false pill cries wolf on a public wall, a missed pill
is a renter at a locked door. Matching is build-side and publishes a boolean
only — the row titles are internal text.
"""
from datetime import date
from pathlib import Path

import build

DAY = date(2026, 8, 24)


def booking(who, studio="509B"):
    return {"kind": "booking", "who": who, "studio": studio,
            "start": 14.0, "end": 16.0}


def flag(events, requests):
    return build.flag_access_gaps(events, requests, DAY)


def test_sweep_row_lights_exactly_its_booking():
    events = flag([booking("Akira Huang"), booking("Marcy Tran")],
                  ["Booking sweep — access unresolved for Akira Huang at 509B "
                   "Aug 24 14:00 [sweep:509B:2026-08-24T14:00:access]"])
    assert events[0].get("access_gap") is True
    assert "access_gap" not in events[1]


def test_sweep_row_for_another_day_lights_nothing():
    """Akira's Friday gap is the Doorman's lead time, not today's pill."""
    events = flag([booking("Akira Huang")],
                  ["Booking sweep — access unresolved for Akira Huang at 509B "
                   "Aug 28 14:00 [sweep:509B:2026-08-28T14:00:access]"])
    assert "access_gap" not in events[0]


def test_sweep_row_for_another_studio_lights_nothing():
    events = flag([booking("Akira Huang", studio="901")],
                  ["Booking sweep — access unresolved for Akira Huang at 509B "
                   "Aug 24 14:00 [sweep:509B:2026-08-24T14:00:access]"])
    assert "access_gap" not in events[0]


def test_unkeyed_row_matches_on_the_person():
    """"Alarm code needs a person — Aahuti Dave" is about her, not a slot."""
    events = flag([booking("Aahuti Dave — rehearsal", studio="693")],
                  ["Alarm code needs a person — Aahuti Dave [Tagvenue] "
                   "(panel user exists but has no code)"])
    assert events[0].get("access_gap") is True


def test_unrelated_rows_and_staff_blocks_stay_dark():
    events = flag(
        [booking("Marcy Tran"), {"kind": "cleaning", "who": "Stefan", "studio": "527"}],
        ["Retire alarm code — Brady Lang (idle 30d+, no future bookings)",
         "🔑 Rotate Megan Cartwright's compromised alarm code before Aug 30"])
    assert all("access_gap" not in e for e in events)


def test_the_flag_is_a_plain_boolean():
    """Public page: build_data casts this with bool() into the payload — the
    pill is true/false, never the row's text."""
    e = flag([booking("Akira Huang")],
             ["access unresolved for Akira Huang [sweep:509B:2026-08-24T14:00:x]"])
    assert e[0]["access_gap"] is True


def test_access_chip_describes_verification_not_a_missing_code():
    """The boolean means an Access / PIN action is open. It must not claim
    the renter's code is missing: the action may instead be an unverified
    window or a temporarily unavailable Alarm.com directory read."""
    root = Path(__file__).parent
    for template in ("template.html", "template-mobile.html"):
        text = (root / template).read_text()
        assert "Verify access" in text
        assert "Code ✗" not in text


def main():
    test_sweep_row_lights_exactly_its_booking()
    test_sweep_row_for_another_day_lights_nothing()
    test_sweep_row_for_another_studio_lights_nothing()
    test_unkeyed_row_matches_on_the_person()
    test_unrelated_rows_and_staff_blocks_stay_dark()
    test_the_flag_is_a_plain_boolean()
    test_access_chip_describes_verification_not_a_missing_code()
    print("access warning regression tests: OK")


if __name__ == "__main__":
    main()
