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


def row(text, artist=None):
    return {"text": text, "artist": artist}


def flag(events, requests):
    """`requests` may be plain titles (no Artist relation) or row dicts."""
    rows = [r if isinstance(r, dict) else row(r) for r in requests]
    return build.flag_access_gaps(events, rows, DAY)


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
        [booking("Marcy Tran"), {"kind": "staff", "who": "Stefan", "studio": "527"}],
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


def test_assumed_arrival_reason_does_not_cramp_timeline_endpoints():
    """The inferred-arrival reason is the compact rectangular middle column,
    between the arrival and departure endpoints."""
    root = Path(__file__).parent
    cramped = '${e.arrived} in · ${e.assumed==="bypass"?"door bypassed":"forgot to arm"}'
    for template in ("template.html", "template-mobile.html"):
        text = (root / template).read_text()
        assert cramped not in text
        assert 'assumed-row' in text
        assert 'class="signal"' in text
        assert "grid-template-columns:auto 1fr auto" in text
        assert "${L}${signal}${R}" in text
        assert 'border-radius:3px' in text
        assert 'class="assumption"' not in text
        assert 'class="assumed-in"' not in text


def main():
    test_sweep_row_lights_exactly_its_booking()
    test_sweep_row_for_another_day_lights_nothing()
    test_sweep_row_for_another_studio_lights_nothing()
    test_unkeyed_row_matches_on_the_person()
    test_unrelated_rows_and_staff_blocks_stay_dark()
    test_the_flag_is_a_plain_boolean()
    test_access_chip_describes_verification_not_a_missing_code()
    test_assumed_arrival_reason_does_not_cramp_timeline_endpoints()
    print("access warning regression tests: OK")


if __name__ == "__main__":
    main()


# ── The artist relation, added 2026-09-03 ───────────────────────────────────
# An unkeyed row that CARRIES an Artist relation is matched on that id, not on
# a name substring. The row that forced this: "🔑 Studio 901 authorized users —
# Krista Flynn: … (Akira Huang done)" is Nina Li's row about Sep 9 in 901, and
# it painted VERIFY ACCESS on Akira Huang's 509B booking on Sep 3.

NINA = "2b575032-81c4-8005-b8a3-e8ff494480af"
AKIRA = "3ce75032-81c4-8184-85e6-e8fc1f49e63e"

KRISTA_ROW = ("🔑 Studio 901 authorized users — Krista Flynn: release T3b "
              "follow-up, add her code when she replies (Akira Huang done)")


def test_another_artists_row_no_longer_lights_a_named_bystander():
    e = dict(booking("Akira Huang — Dance practice"), _artist_id=AKIRA)
    flag([e], [row(KRISTA_ROW, artist=NINA)])
    assert "access_gap" not in e


def test_that_same_row_still_lights_the_artist_it_belongs_to():
    e = dict(booking("Nina Li", studio="901"), _artist_id=NINA)
    flag([e], [row(KRISTA_ROW, artist=NINA)])
    assert e.get("access_gap") is True


def test_an_unkeyed_row_with_no_artist_still_falls_back_to_the_name():
    e = dict(booking("Aahuti Dave — rehearsal", studio="693"), _artist_id=AKIRA)
    flag([e], [row("Alarm code needs a person — Aahuti Dave [Tagvenue]")])
    assert e.get("access_gap") is True


def test_an_artist_keyed_sweep_row_lights_every_room_that_day():
    """Day-scoped by design: one text covers every room they hold that day."""
    key = f"[sweep:access:{AKIRA}:2026-08-24:access]"
    a = dict(booking("Akira Huang", studio="509B"), _artist_id=AKIRA)
    b = dict(booking("Akira Huang", studio="693"), _artist_id=AKIRA)
    flag([a, b], [row(f"Booking sweep — access unresolved for Akira Huang {key}",
                      artist=AKIRA)])
    assert a.get("access_gap") is True and b.get("access_gap") is True


def test_an_artist_keyed_sweep_row_for_another_day_lights_nothing():
    key = f"[sweep:access:{AKIRA}:2026-08-31:access]"
    e = dict(booking("Akira Huang"), _artist_id=AKIRA)
    flag([e], [row(f"Booking sweep — access unresolved for Akira Huang {key}",
                   artist=AKIRA)])
    assert "access_gap" not in e
