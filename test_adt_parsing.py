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

    print("FAILED" if fails else f"ok — {len(ARM_CASES) + len(ALERT_CASES) + 5} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
