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


def test_mail_doors_reconciliation():
    """The ADT email as a SECOND WITNESS to the websocket — added 2026-08-25
    when the 'permanently dead' feed turned out to be delivering again.

    Only one of the three outcomes is a finding, and the two that are not
    matter just as much: a gap-covered miss is the coverage machinery being
    honest, and a pre-ledger event is unanswerable rather than clean."""
    import build
    MS = 1000

    def ev(studio, kind, ts, name="Ivanka Moskaliuk", remote=False):
        return {"studio": studio, "kind": kind, "ts": ts, "time": "19:05",
                "name": name, "remote": remote}

    base = 1787612722 * MS          # 2026-08-24T23:05:22Z, a real disarm
    covers = "2026-08-22T19:32:00Z"

    # Both saw it — the expected case. Seconds apart still matches.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base)], [ev("509A", "arrival", base + 3 * MS)],
        [], covers)
    assert r == {"matched": 1, "inGap": 0, "missed": [], "missedCount": 0}, r

    # Mail saw it, socket did not, and a recorded gap covers that second.
    # Not a defect — this is the proof the gap machinery is telling the truth.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base)], [],
        [{"since": "2026-08-24T23:05:00Z", "until": "2026-08-24T23:05:40Z"}],
        covers)
    assert r["inGap"] == 1 and r["missedCount"] == 0, r

    # Mail saw it, socket did not, and the socket claimed to be listening.
    # The one loud case: a hole inside a window reported as clean.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base)], [], [], covers)
    assert r["missedCount"] == 1 and "509A arrival" in r["missed"][0], r
    assert "Ivanka Moskaliuk" in r["missed"][0], r

    # A different studio is not a twin, however close in time.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base)], [ev("693", "arrival", base)], [], covers)
    assert r["missedCount"] == 1, r

    # Neither is the opposite kind — an arm never satisfies a disarm.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base)], [ev("509A", "departure", base)], [],
        covers)
    assert r["missedCount"] == 1, r

    # Before the ledger existed: unanswerable, so silent. The ledger prunes at
    # 72h and must never be read as "clean" for time it never held.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", 1787000000 * MS)], [], [], covers)
    assert r == {"matched": 0, "inGap": 0, "missed": [], "missedCount": 0}, r

    # Remote and nameless events are not human-attributable — never judged.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base, name="Studio (remote)", remote=True),
         ev("509A", "arrival", base, name="")], [], [], covers)
    assert r["missedCount"] == 0, r

    # Errs quiet: 4 minutes drift still matches, so a slow mail delivery does
    # not manufacture a hole. A false alarm here costs an investigation.
    r = build.reconcile_mail_against_doors(
        [ev("509A", "arrival", base)], [ev("509A", "arrival", base + 240 * MS)],
        [], covers)
    assert r["matched"] == 1, r
