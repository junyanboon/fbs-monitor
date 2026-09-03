#!/usr/bin/env python3
"""Regression guards for the 2026-09-03 Matterport bug.

WHAT HAPPENED. Studio 509B held a Skedda UNAVAILABLE block (booking 119939698,
type 2, no venueuser) titled "Matterport 360° panorama capture — Studios 509A &
509B", 13:00–14:00. The board reads the Google mirror, which carries only the
title, so the block rendered as a booking. join_notion then matched it — on the
START only, within a 2-hour window — to Akira Huang's 14:15–15:45 `Monitor Only`
row, 1.25 h away. The published payload showed:

    Matterport …  "tier": "Monitor", "hta": "Sent", AVA MISSING, EOB MISSING
    Akira Huang   "tier": null,      "hta": null,   no dispatch pills

Both wrong, in opposite directions: a camera on a tripod wore the renter's tier,
and the renter — whose AVA and EOB were genuinely Scheduled in Notion — read as
having neither. Two fixes, guarded here:

  1. Skedda's own `type == 2` marks the card staff (mark_skedda_holds).
  2. join_notion requires BOTH ends within 30 min, not one end within 2 hours.

Either alone stops this case. Both are kept because each covers a hole the other
does not: a RECURRING hold never reaches the Skedda hold feed, and a Skedda
outage takes the hold flag away entirely.

Run: python -m pytest test_staff_blocks.py
"""
from datetime import date, datetime

import build

BASE_DAY = date(2026, 9, 3)


def _card(who, studio, start, end, kind="booking"):
    return {"kind": kind, "who": who, "studio": studio,
            "start": start, "end": end, "tier": None, "gtg": True, "hta": None}


def _hold(studio, start, end, title):
    return {"studio": studio, "title": title, "user_name": None, "is_hold": True,
            "start": datetime.combine(BASE_DAY, datetime.min.time()).replace(
                hour=int(start), minute=round((start % 1) * 60)),
            "end": datetime.combine(BASE_DAY, datetime.min.time()).replace(
                hour=int(end), minute=round((end % 1) * 60))}


def _row(studio, start, end, tier="Monitor Only"):
    return {"id": f"row-{studio}-{start}", "studio": studio,
            "start": start, "end": end, "artist": "artist-1",
            "tier": {"monitor only": "Monitor"}.get(tier.lower(), tier),
            "gtg": True, "hta": "Sent", "ava": "Scheduled", "eob": "Scheduled",
            "board_disarmed": None, "board_armed": None, "status": "Upcoming"}


def _notion_hold(studio, start, end, title):
    """The shape fetch_studio_holds returns — wall-clock HH:MM, not datetimes."""
    return {"studio": studio, "title": title, "user_name": None,
            "is_hold": True, "_hm": (start, end)}


# ── 0. The Notion-carried rows mark exactly like the Skedda ones ─────────────
# 🚧 Studio Holds is the PRIMARY source: the board's GitHub Actions runner has
# no Skedda credential, so the direct read answers only on a laptop.

def test_a_notion_hold_row_marks_the_same_card():
    events = [_card("Matterport 360° panorama capture", "509B", 13.0, 14.0)]
    holds = [_notion_hold("509B", "13:00", "14:00", "Matterport")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 1
    assert events[0]["kind"] == "staff"


def test_a_notion_hold_does_not_touch_the_next_booking():
    events = [_card("Akira Huang", "509B", 14.25, 15.75)]
    holds = [_notion_hold("509B", "13:00", "14:00", "Matterport")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 0


def test_a_notion_hold_after_midnight_lands_on_the_operating_day():
    """The board's day runs 05:00 → 05:00, so 02:00 sits at 26.0 on the grid."""
    events = [_card("Late clean", "901", 26.0, 27.0)]
    holds = [_notion_hold("901", "02:00", "03:00", "Stefan")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 1


def test_a_notion_hold_crossing_midnight_still_matches():
    events = [_card("Overnight hold", "693", 23.0, 25.0)]
    holds = [_notion_hold("693", "23:00", "01:00", "Maintenance")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 1


# ── 1. Skedda's UNAVAILABLE type is what identifies a staff block ────────────

def test_the_matterport_hold_is_marked_staff():
    events = [_card("Matterport 360° panorama capture — Studios 509A & 509B",
                    "509B", 13.0, 14.0)]
    holds = [_hold("509B", 13.0, 14.0, "Matterport 360° panorama capture")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 1
    assert events[0]["kind"] == "staff"


def test_a_real_booking_in_the_same_room_is_untouched():
    events = [_card("Akira Huang — Dance practice", "509B", 14.25, 15.75)]
    holds = [_hold("509B", 13.0, 14.0, "Matterport 360° panorama capture")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 0
    assert events[0]["kind"] == "booking"


def test_an_inexact_slot_is_left_a_booking():
    """Over-marking hides a paying renter. Ambiguity must fail towards booking."""
    events = [_card("Someone", "509B", 13.0, 15.0)]      # same start, later end
    holds = [_hold("509B", 13.0, 14.0, "Matterport")]
    assert build.mark_skedda_holds(events, holds, BASE_DAY) == 0
    assert events[0]["kind"] == "booking"


def test_no_holds_means_no_change():
    events = [_card("Akira Huang", "509B", 14.25, 15.75)]
    rows = [dict(_hold("509B", 13.0, 14.0, "x"), is_hold=False)]
    assert build.mark_skedda_holds(events, rows, BASE_DAY) == 0
    assert events[0]["kind"] == "booking"


# ── 2. join_notion needs both ends, so the hold cannot steal the row ─────────

def test_a_neighbouring_slot_can_no_longer_steal_a_tier():
    """The exact 2026-09-03 pairing, with the hold flag unavailable."""
    matterport = _card("Matterport 360° panorama capture", "509B", 13.0, 14.0)
    akira = _card("Akira Huang — Dance practice", "509B", 14.25, 15.75)
    build.join_notion([matterport, akira], [_row("509B", "14:15", "15:45")])
    assert matterport["tier"] is None, "a 1.25 h gap is not the same booking"
    assert akira["tier"] == "Monitor"
    assert akira["hta"] == "Sent"


def test_a_staff_card_is_skipped_outright():
    hold = _card("Matterport", "509B", 13.0, 14.0, kind="staff")
    akira = _card("Akira Huang", "509B", 14.25, 15.75)
    build.join_notion([hold, akira], [_row("509B", "14:15", "15:45")])
    assert hold["tier"] is None
    assert akira["tier"] == "Monitor"


def test_an_exact_slot_still_matches():
    kyle = _card("Kyle FitzGerald", "509B", 18.0, 21.0)
    build.join_notion([kyle], [_row("509B", "18:00", "21:00")])
    assert kyle["tier"] == "Monitor"


def test_a_cross_midnight_booking_still_matches():
    """Card 22:00–01:00 lives at 22.0–25.0 on the operating day; Notion says 01:00."""
    late = _card("Night Owl", "693", 22.0, 25.0)
    build.join_notion([late], [_row("693", "22:00", "01:00")])
    assert late["tier"] == "Monitor"


def test_a_row_with_no_end_time_is_not_forced_onto_a_card():
    card = _card("Someone", "527", 18.0, 21.0)
    build.join_notion([card], [dict(_row("527", "18:00", "21:00"), end=None)])
    assert card["tier"] is None


def test_two_bookings_one_room_each_keep_their_own_row():
    a = _card("Kyle FitzGerald", "509B", 18.0, 21.0)
    b = _card("Kyle FitzGerald", "509B", 21.0, 22.0)
    build.join_notion([a, b], [_row("509B", "21:00", "22:00", "Studio Viewing"),
                               _row("509B", "18:00", "21:00")])
    assert a["tier"] == "Monitor"
    assert b["tier"] == "Studio Viewing"


# ── 3. The staff-name title rule (Junyan, 2026-09-03) ───────────────────────
# "If any of our staff names appear on there then it will be known that it is a
# staff booking." Implemented as FIRST WHOLE WORD + no holder colon — a plain
# substring rule is unsafe against the live Artist DB, and these tests pin why.

def test_our_own_names_lead_a_staff_block():
    for title in ("Caney", "Stefan ", "Donny (Studio 527)",
                  "Junyan (Studio 901 (Elements))", "Stefan clean 693",
                  "Ela (Studio 509A)", "Kyjah 901"):
        assert build.is_staff_block(title), title


def test_a_renter_whose_SURNAME_is_a_staff_name_is_not_staff():
    """'Rita Stefan [Skedda]' is a live One-Off renter. A substring rule would
    have hidden her from the board and from every FBS message lane."""
    assert not build.is_staff_block("Rita Stefan: Practice (Studio 527) [Paid]")
    assert not build.is_staff_block("Rita Stefan (Studio 527)")


def test_names_that_merely_CONTAIN_a_staff_name_are_not_staff():
    """Seven live renters contain 'ela': Gabriela, Mihaela, Mariadela,
    Daniela, Pamela, Kaela, Elaine."""
    for who in ("Gabriela Meza-Yanes", "Mihaela Dirlea", "Mariadela Ruiz",
                "Nanu Daniela Bragagnolo", "Pamela Xu", "Kaela Faloon",
                "Elaine Yang"):
        assert not build.is_staff_block(f"{who} (Studio 693)"), who


def test_a_holder_colon_means_a_real_booking_whatever_the_name():
    """Skedda writes a booking as '<Holder>: <activity> (Studio NNN)'; a hold
    has no holder and gets the bare title. Verified live 2026-09-03."""
    assert not build.is_staff_block("Stefan Lovecchio: Practice (Studio 527) [Paid]")
    assert build.is_staff_block("Stefan (Studio 901 (Elements))")


def test_a_stranger_is_never_staff():
    assert not build.is_staff_block("Matterport 360° panorama capture")
    assert not build.is_staff_block("Akira Huang: Dance practice (Studio 509B)")
    assert not build.is_staff_block("")


def test_the_old_name_still_answers():
    assert build.is_cleaning("Caney") is True
