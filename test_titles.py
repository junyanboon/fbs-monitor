#!/usr/bin/env python3
"""Regression guards for booking-title cleanup and duplicate handling.

Every case here is a REAL title shape seen in the studio ICS feeds. The feeds
are the board's only source of who is coming and which room they are in, so a
title the cleaner mishandles becomes a renter nobody recognises at the door:

  2026-08-15  "Booking on Giggster.com https://…" — the scheme's colon hit the
              "Name: Description" split, so Welton R. Giggster's 693 booking
              rendered as a renter called "Booking on Giggster.com https"
  2026-08-15  one renter shown in two studios at once (Jessica T. Peerspace on
              509B and 693) after a move, the stale row drawing a red
              "no arrival" for a booking that was running one room over

Run: python test_titles.py   (also runs in CI before the build)
"""
import sys
from datetime import date

import build


def check(title, expected):
    got = build.clean_who(title)
    assert got == expected, f"{title!r} → {got!r}, expected {expected!r}"


def test_platform_titles():
    # The booking URL leaves; the platform stays, because the feed never
    # carries the renter's name for these — Skedda has it, the ICS does not.
    check("Booking on Giggster.com https://giggster.com/bookings/12345",
          "Giggster Booking")
    check("Booking on Giggster.com https://giggster.com/b/1 — Birthday Party",
          "Giggster Booking — Birthday Party")
    # A named booking on the same platform must survive untouched.
    check("Welton R. Giggster", "Welton R. Giggster")
    check("Peerspace Booking, Jessica T.", "Peerspace Booking, Jessica T.")


def test_existing_shapes_unchanged():
    check("Nicole Drury (Nicole Drury) — boxing", "Nicole Drury — boxing")
    check("Desiree Joy: Desiree Joy", "Desiree Joy")
    check("Kate Keenan — Studio Viewing", "Kate Keenan — Studio Viewing")
    check("Stefan (Studio 901 (Elements))", "Stefan")


def test_nameless_titles():
    """Which titles the Skedda name lookup is allowed to overwrite."""
    for nameless in ("Giggster Booking", "Giggster Booking — Birthday Party",
                     "Booking", "Tagvenue Booking"):
        assert build._is_nameless_title(nameless), nameless
    # These carry a person. Overwriting them would be a regression, not a fix.
    for named in ("Peerspace Booking, Jessica T.", "ALVIN W. Peerspace",
                  "Welton R. Giggster", "Kate Keenan — Studio Viewing"):
        assert not build._is_nameless_title(named), named


def test_redaction():
    """The Issues tab publishes Action text to a PUBLIC page. These are real
    open rows from 2026-08-15 — every one of them would have leaked."""
    out = build.redact("ALVIN W. sent his phone number (647 836 3616)")
    assert "647" not in out and "3616" not in out, out
    out = build.redact("His number +51 915 027 018 is PERU")
    assert "915" not in out and "018" not in out, out
    out = build.redact("confirm payment for the $259.90 August invoice")
    assert "259" not in out and "$•••" in out, out
    out = build.redact("alarm code 4821 for Studio 693 on Aug 16 2026")
    assert "4821" not in out, out
    # Years, studios and clock times must survive — they are what makes the
    # line readable, and none of them is private.
    assert "2026" in out and "693" in out
    assert "14:00" in build.redact("TOMORROW 14:00 — Hannah Cho cannot enter 509B")


def test_issue_lead():
    """Cut at a clause, never inside somebody's name."""
    assert build._lead("TOMORROW 14:00 — Hannah Cho cannot enter 509B "
                       "(manual_no_match: no Alarm.com user at all)") \
        == "TOMORROW 14:00 — Hannah Cho cannot enter 509B"
    # "ALVIN W." must not be cut at the initial's dot.
    assert build._lead("🚨 TONIGHT 21:00 — ALVIN W. finally sent his phone "
                       "number. Add it and get his 509B access out.") \
        .endswith("phone number")


# test_near_term_window() removed 2026-08-25 with the Issues tab itself —
# _is_near_term() is gone; the one access surface is the card pill
# (flag_access_gaps, tested in test_issues.py).


def main():
    test_platform_titles()
    test_existing_shapes_unchanged()
    test_nameless_titles()
    test_redaction()
    test_issue_lead()
    build._selftest_dedupe()      # same-studio dedupe + cross-studio marking
    print("title/dedupe regression tests: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
