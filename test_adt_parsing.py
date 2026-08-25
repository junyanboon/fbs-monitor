#!/usr/bin/env python3
"""Regression guards for ADT subject parsing and panel-state derivation.

Every case here is a REAL subject shape seen in the "Artist Care - ADT" label.
TELUS/ADT has changed wording more than once and each change silently dropped
events until someone noticed a studio reading wrong on the board:

  2026-07-19  "Panel was Armed Away by <name> at" — every 901 departure dropped
  2026-08-01  "Studio 509B was Armed Away at 6:04 PM (info@…)" — the nameless
              form matched nothing, so 509B read "Open" all evening
  2026-08-01  info@danceannex.ca events were discarded outright, so a remote
              arm never reached the page at all

Run: python test_adt_parsing.py   (also runs in CI before the build)
"""
import sys
from datetime import datetime

import build

TS = int(datetime(2026, 8, 1, 18, 4, tzinfo=build.TZ).timestamp() * 1000)

# (subject, expected) — expected None means "must not parse as an arm/disarm"
ARM_CASES = [
    # by-name forms
    ("Studio 509: Studio 509A was Disarmed by Keerthana Vijay at 2:08 PM",
     {"studio": "509A", "name": "Keerthana Vijay", "time": "14:08", "kind": "arrival", "remote": False}),
    ("Studio 509: Studio 509A was Armed Away by Keerthana Vijay at 4:29 PM",
     {"studio": "509A", "name": "Keerthana Vijay", "time": "16:29", "kind": "departure", "remote": False}),
    # panel forms (901's usual shape)
    ("Studio 901: Studio 901 Panel was Armed Away by Himanshi Mehta at 5:12 PM",
     {"studio": "901", "name": "Himanshi Mehta", "time": "17:12", "kind": "departure", "remote": False}),
    ("Studio 693: Studio 693 Panel was Disarmed at 11:20 PM (Shiela)",
     {"studio": "693", "name": "Shiela", "time": "23:20", "kind": "arrival", "remote": False}),
    # nameless forms, name in trailing parens (509's usual shape)
    ("Studio 509: Studio 509B was Armed Away at 6:04 PM (info@danceannex.ca)",
     {"studio": "509B", "name": "Studio (remote)", "time": "18:04", "kind": "departure", "remote": True}),
    ("Studio 509: Studio 509B was Disarmed at 4:29 PM (Panel User)",
     {"studio": "509B", "name": "Panel User", "time": "16:29", "kind": "arrival", "remote": False}),
    # must NOT be read as arm/disarm
    ("Studio 693: Studio 693 reported an Alarm", None),
    ("Studio 901: Motion detected at Studio 901", None),
    ("Studio 527: Image captured at Studio 527", None),
    ("Newsletter from someone else entirely", None),
]

ALERT_CASES = [
    ("Studio 693: Studio 693 reported an Alarm", "ALARM", None),
    ("Studio 693: PENDING Alarm at Studio 693", "PENDING", None),
    # site prefix is NOT the partition — "Studio 509:" must not win over "509B"
    ("Studio 509: Studio 509B reported an Alarm", "ALARM", None),
    ("Studio 509: Studio 509B was Armed Away with sensors Bypassed", "BYPASS", None),
    ("Studio 509: Studio 509A Panel Tamper", "TROUBLE", "Tamper"),
    ("Studio 527: Studio 527 Front Door malfunction", "TROUBLE", "Malfunction"),
    ("Studio 901: Studio 901 Panel low battery", "TROUBLE", "Low Battery"),
    ("Studio 509: Studio 509A was Disarmed by Jake Maresca at 2:00 PM", None, None),
    # Alarm.com System Event Notifications, pointed at this label 2026-08-08.
    # ADT never mailed about these four, so before that day the board could show
    # a panel that could not arm as simply "Armed" and nobody would know.
    ("Studio 693: Studio 693 System is unable to arm", "TROUBLE", "Unable To Arm"),
    ("Studio 901: Studio 901 Panel is not communicating", "TROUBLE", "Not Communicating"),
    ("Studio 527: Studio 527 Panel has been shut down", "TROUBLE", "Shut Down"),
    ("Studio 509: Studio 509B lost power", "TROUBLE", "Lost Power"),
    # Must NEVER surface. Junyan asked for credentials-in-conflict out by name:
    # Alarm.com raises one per duplicate PIN across the 168 rotating codes, so
    # several stand permanently (4 live on 2026-08-08) and a pill for them would
    # be on every day forever — which is how the pill that matters gets ignored.
    ("Studio 693: Studio 693 Credentials In Conflict", None, None),
    ("Studio 901: Credential Conflict on Studio 901 Panel", None, None),
    # Routine mail that names a studio and reads like trouble but is not.
    ("Your Security System User Codes Have Been Changed for Studio 901", None, None),
    ("Studio 901: Post-Disarm images uploaded by Panel Camera from 3:10 PM", None, None),
]


def check(label, got, want):
    if got != want:
        print(f"FAIL {label}\n     got  {got!r}\n     want {want!r}")
        return 1
    return 0


def main():
    fails = 0

    for subject, want in ARM_CASES:
        got = build.parse_arm_subject(subject)
        if want is None:
            fails += check(f"arm/none: {subject[:60]}", got, None)
        else:
            got = {k: got.get(k) for k in want} if got else None
            fails += check(f"arm: {subject[:60]}", got, want)

    for subject, stage, detail in ALERT_CASES:
        got = build.parse_alarm_subject(subject, TS)
        if stage is None:
            fails += check(f"alert/none: {subject[:60]}", got, None)
        else:
            fails += check(f"alert stage: {subject[:60]}", got and got.get("stage"), stage)
            if detail:
                fails += check(f"alert detail: {subject[:60]}", got and got.get("detail"), detail)

    # Same-minute ordering: a disarm and an arm stamped 19:36 must resolve by the
    # email receipt time, not by list order (this is what made 509A read Open).
    evs = [
        {"studio": "509A", "kind": "arrival", "time": "19:36", "ts": 1000, "name": "x"},
        {"studio": "509A", "kind": "departure", "time": "19:36", "ts": 2000, "name": "x"},
    ]
    ordered = sorted(evs, key=lambda a: (a["time"], a["ts"]))
    fails += check("same-minute ordering", ordered[-1]["kind"], "departure")

    # Durable state: a studio silent today keeps its last known event, and a newer
    # event supersedes the stored one.
    prev = {"527": {"studio": "527", "kind": "departure", "time": "23:40", "ts": 100, "when": "Fri 23:40"}}
    merged = build.merge_panel_state(prev, [], {})
    fails += check("state kept when silent", merged["527"]["time"], "23:40")
    merged = build.merge_panel_state(
        prev, [{"studio": "527", "kind": "arrival", "time": "09:15", "ts": 200, "name": "n"}], {})
    fails += check("state advances on newer event", merged["527"]["kind"], "arrival")

    # Remote events set panel state but are never attributed to a renter.
    booking = [{"studio": "509B", "who": "Someone", "kind": "booking", "start": 18.0, "end": 20.0,
                "tier": None, "gtg": True, "hta": None, "arrived": None, "departed": None}]
    out = build.apply_arm_events(booking, [
        {"studio": "509B", "kind": "arrival", "time": "18:04", "remote": True, "name": "Studio (remote)"}])
    fails += check("remote never becomes an arrival", out[0]["arrived"], None)

    # Wrong studio: the renter is in the building, in a room they didn't book.
    # Real case 2026-08-07 — Ayden Mauro booked 527 18:00-19:15 and disarmed 693
    # at 17:47, ran the session there, and armed 693 at 18:52, one minute before
    # the class that HAD booked 693 arrived. The board showed only "no arrival",
    # which reads as a no-show and sends nobody to move him.
    def booking(studio, who, start, end):
        return {"studio": studio, "who": who, "kind": "booking", "start": start, "end": end,
                "tier": None, "gtg": True, "hta": None, "arrived": None, "departed": None}

    out = build.apply_arm_events([booking("527", "Ayden Mauro", 18.0, 19.25)], [
        {"studio": "693", "kind": "arrival", "time": "17:47", "remote": False, "name": "Ayden Mauro"},
        {"studio": "693", "kind": "departure", "time": "18:52", "remote": False, "name": "Ayden Mauro"}])
    fails += check("wrong studio is flagged", (out[0].get("wrong_studio") or {}).get("studio"), "693")
    fails += check("wrong studio carries the time", (out[0].get("wrong_studio") or {}).get("at"), "17:47")
    fails += check("wrong studio is not an arrival", out[0]["arrived"], None)

    # An arrival in the booked studio outranks any foreign event — someone who
    # showed up where they belong is never "in the wrong studio", whatever else
    # their name touched that hour.
    out = build.apply_arm_events([booking("527", "Ayden Mauro", 18.0, 19.25)], [
        {"studio": "527", "kind": "arrival", "time": "18:02", "remote": False, "name": "Ayden Mauro"},
        {"studio": "693", "kind": "arrival", "time": "17:47", "remote": False, "name": "Ayden Mauro"}])
    fails += check("right studio wins", out[0].get("wrong_studio"), None)
    fails += check("right studio still records arrival", out[0]["arrived"], "18:02")

    # A name token shared with someone who legitimately booked that other studio
    # must not manufacture a wrong-studio flag. Pass 1 claims the event for the
    # booking that owns the room; pass 3 only ever considers unclaimed events.
    out = build.apply_arm_events(
        [booking("527", "Ayden Mauro", 18.0, 19.25), booking("693", "Ayden Smith", 17.5, 19.0)],
        [{"studio": "693", "kind": "arrival", "time": "17:47", "remote": False, "name": "Ayden Smith"}])
    fails += check("claimed event is not a wrong studio", out[0].get("wrong_studio"), None)
    fails += check("the real booking keeps its arrival", out[1]["arrived"], "17:47")

    # Out of window is out of scope — a disarm hours from the booking is a
    # different visit, not a misplaced renter.
    out = build.apply_arm_events([booking("527", "Ayden Mauro", 18.0, 19.25)], [
        {"studio": "693", "kind": "arrival", "time": "09:15", "remote": False, "name": "Ayden Mauro"}])
    fails += check("far-off event is not a wrong studio", out[0].get("wrong_studio"), None)

    # The missing-alarm-code chip was REMOVED 2026-08-25 — it never once fired
    # in 17 days of production, and it duplicated the Doorman's live-panel check
    # against a weaker source. Its cases lived here; see the tombstone above
    # join_notion() in build.py for why they are not coming back.

    print("FAILED" if fails else f"ok — {len(ARM_CASES) + len(ALERT_CASES) + 13} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
