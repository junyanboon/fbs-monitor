#!/usr/bin/env python3
"""Regression guards for the PANEL arrival source and the feed-down guard.

Why these exist: on 2026-08-18 the ADT email feed had been dead since 23:31 the
night before, and the board rendered Kiah Francis' finished 07:30-09:30 booking
in 509A as a red "no arrival". A dead pipe and a real no-show produced the same
output — `arrived: null` — so the board accused a renter on no evidence.

Two things had to become true, and both are guarded here:
  1. arrivals no longer depend on one feed (fetch_arm_history + enrich_arm_names)
  2. an unanswered feed is VISIBLE, never silently rendered as a no-show

Run: python test_arm_history.py   (also runs in CI before the build)
"""
import sys
from datetime import datetime, timedelta

import build
from test_adt_parsing import check

TZ = build.TZ


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def with_history(payload, fn):
    """Run fn() with requests.get stubbed to return `payload`."""
    real = build.requests.get
    build.requests.get = lambda *a, **k: FakeResponse(payload)
    try:
        return fn()
    finally:
        build.requests.get = real


def main():
    fails = 0
    win_start = datetime(2026, 8, 18, 5, 0, tzinfo=TZ)
    build.ARM_HISTORY_URL = "https://alarm.example/arm-history"
    build.ARM_HISTORY_TOKEN = "t"

    # ---- fetch_arm_history ------------------------------------------------
    payload = {
        "updated_at": "2026-08-18T13:40:00+00:00",
        "events": [
            # 07:31 and 09:28 Toronto — Kiah's real booking, as the panel saw it.
            {"studio": "509A", "kind": "arrival", "at": "2026-08-18T11:31:00+00:00"},
            {"studio": "509A", "kind": "departure", "at": "2026-08-18T13:28:00+00:00"},
        ],
    }
    evs, updated = with_history(payload, lambda: build.fetch_arm_history(win_start))
    fails += check("panel events parsed", len(evs), 2)
    fails += check("UTC converted to Toronto clock", evs[0]["time"], "07:31")
    fails += check("departure converted too", evs[1]["time"], "09:28")
    fails += check("panel carries no actor", evs[0]["name"], "")
    fails += check("panel events are never remote", evs[0]["remote"], False)
    fails += check("updated_at returned for staleness judging", updated,
                   "2026-08-18T13:40:00+00:00")

    # Events before the window belong to yesterday's board, not today's.
    old = {"events": [{"studio": "527", "kind": "arrival",
                       "at": "2026-08-17T11:00:00+00:00"}]}
    evs_old, _ = with_history(old, lambda: build.fetch_arm_history(win_start))
    fails += check("pre-window events dropped", evs_old, [])

    # Junk must be skipped, not crash the build — this feeds a page, and a
    # malformed row is not a reason for the studios to lose their board.
    junk = {"events": [
        {"studio": "509A", "kind": "arrival", "at": "not-a-date"},
        {"studio": "777", "kind": "arrival", "at": "2026-08-18T11:31:00+00:00"},
        {"studio": "509A", "kind": "wandered-off", "at": "2026-08-18T11:31:00+00:00"},
        {"studio": "509A", "kind": "arrival", "at": "2026-08-18T12:00:00+00:00"},
    ]}
    evs_junk, _ = with_history(junk, lambda: build.fetch_arm_history(win_start))
    fails += check("only the sound row survives", len(evs_junk), 1)
    fails += check("and it is the right one", evs_junk[0]["time"], "08:00")

    # ---- enrich_arm_names -------------------------------------------------
    def panel(studio, hhmm, kind="arrival"):
        h, m = (int(x) for x in hhmm.split(":"))
        ts = int(datetime(2026, 8, 18, h, m, tzinfo=TZ).timestamp() * 1000)
        return {"studio": studio, "name": "", "time": hhmm, "kind": kind,
                "remote": False, "ts": ts, "source": "panel"}

    def mail(studio, hhmm, name, kind="arrival", remote=False):
        h, m = (int(x) for x in hhmm.split(":"))
        ts = int(datetime(2026, 8, 18, h, m, tzinfo=TZ).timestamp() * 1000)
        return {"studio": studio, "name": name, "time": hhmm, "kind": kind,
                "remote": remote, "ts": ts}

    merged = build.enrich_arm_names([panel("509A", "07:31")],
                                    [mail("509A", "07:32", "Kiah Francis")])
    fails += check("one event out, not two", len(merged), 1)
    fails += check("the name is attached", merged[0]["name"], "Kiah Francis")
    fails += check("the panel keeps the timeline", merged[0]["time"], "07:31")

    # A remote studio-account disarm is a real panel change but never a renter's
    # arrival. Losing that flag would attribute it to whoever was booked.
    merged = build.enrich_arm_names(
        [panel("693", "22:57")],
        [mail("693", "22:57", "Studio (remote)", remote=True)])
    fails += check("remote flag rides along", merged[0]["remote"], True)

    # Different studio, different kind, or too far apart: no match, no name.
    merged = build.enrich_arm_names([panel("509A", "07:31")],
                                    [mail("509B", "07:31", "Someone Else")])
    fails += check("a neighbour's event is not borrowed", merged[0]["name"], "")
    merged = build.enrich_arm_names([panel("509A", "07:31")],
                                    [mail("509A", "07:31", "Someone", kind="departure")])
    fails += check("an arm is not a disarm", merged[0]["name"], "")
    merged = build.enrich_arm_names([panel("509A", "07:31")],
                                    [mail("509A", "07:45", "Too Late")])
    fails += check("beyond two minutes is not a match", merged[0]["name"], "")

    # One mail event must not name two panel events.
    merged = build.enrich_arm_names([panel("509A", "07:31"), panel("509A", "07:32")],
                                    [mail("509A", "07:31", "Kiah Francis")])
    named = [e for e in merged if e["name"]]
    fails += check("a name is claimed once", len(named), 1)

    # A mail event with no panel twin is kept — a disarm and re-arm inside one
    # tick is invisible to state polling, and dropping it would lose a real
    # arrival the emails did see.
    merged = build.enrich_arm_names([panel("509A", "07:31")],
                                    [mail("527", "14:00", "Ayden Mauro")])
    fails += check("unmatched mail events survive", len(merged), 2)

    # ---- the guard --------------------------------------------------------
    late = win_start + timedelta(hours=6)
    early = win_start + timedelta(hours=1)
    down = {"panel": "failed", "mail": "failed"}

    # The actual Kiah case: nothing answered, hours into the day.
    fails += check("nothing answered + late = outage",
                   build.feed_is_down([], down, late, win_start), True)

    # Early in the day an empty stream is ordinary — the studios open at 07:00
    # and nobody has touched a panel. Firing then cries wolf every morning.
    fails += check("empty and early is not an outage",
                   build.feed_is_down([], down, early, win_start), False)

    # One event anywhere means something is watching.
    fails += check("any event at all clears the guard",
                   build.feed_is_down([panel("509A", "07:31")], down, late, win_start),
                   False)

    # A healthy panel ledger returning nothing is the one trustworthy empty —
    # it genuinely holds no events before the day's first disarm. Suppressing
    # flags here would hide real no-shows on a quiet day.
    fails += check("a healthy panel feed is believed when it says nothing",
                   build.feed_is_down([], {"panel": "ok", "mail": "failed"},
                                      late, win_start),
                   False)

    # Panel never configured + dead mail is exactly the pre-fix world.
    fails += check("unconfigured panel does not count as healthy",
                   build.feed_is_down([], {"panel": "unconfigured", "mail": "failed"},
                                      late, win_start),
                   True)

    print("FAILED" if fails else "ok — 25 checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
