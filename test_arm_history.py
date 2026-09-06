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

    # ---- pass 2 must not spend one event on two bookings ------------------
    # The 2026-08-20 case verbatim: Vanessa's 13:00-13:15 viewing in 509A went
    # unseen by the panel tick (2m24s visit, inside one tick), Anneka's 13:30
    # booking followed. Her nameless 13:29 disarm must land on Anneka ONLY —
    # it sits 1 min before Anneka's start but 14 min after Vanessa's end —
    # and her 15:34 arm likewise. Vanessa's row stays honestly blank.
    def ev(studio, who, start, end):
        return {"studio": studio, "who": who, "start": start, "end": end,
                "kind": "booking", "hta": None, "arrived": None,
                "departed": None, "lane": "unknown"}
    vanessa = ev("509A", "Vanessa Zavatti — Studio Viewing", 13.0, 13.25)
    anneka = ev("509A", "Anneka K. Peerspace — 509B", 13.5, 15.5)
    arm = [{"studio": "509A", "name": "", "time": "13:29", "kind": "arrival"},
           {"studio": "509A", "name": "", "time": "15:34", "kind": "departure"}]
    build.apply_arm_events([vanessa, anneka], arm)
    fails += check("phantom arrival not written", vanessa["arrived"], None)
    fails += check("phantom departure not written", vanessa["departed"], None)
    fails += check("real booking gets the arrival", anneka["arrived"], "13:29")
    fails += check("real booking gets the departure", anneka["departed"], "15:34")

    # A mid-booking arm/disarm pair must not read as leaving: last arm wins.
    solo = ev("693", "Angus Dirnbeck", 18.0, 20.0)
    arm = [{"studio": "693", "name": "", "time": "18:02", "kind": "arrival"},
           {"studio": "693", "name": "", "time": "19:00", "kind": "departure"},
           {"studio": "693", "name": "", "time": "19:05", "kind": "arrival"},
           {"studio": "693", "name": "", "time": "20:01", "kind": "departure"}]
    build.apply_arm_events([solo], arm)
    fails += check("earliest disarm is the arrival", solo["arrived"], "18:02")
    fails += check("last arm is the departure", solo["departed"], "20:01")

    # ---- facilitator + keypad actor (2026-09-02) ----------------------------
    # `[Facilitator: Name]` names who we expect at the keypad; the booker is
    # not that person. Krista's disarm must (a) match her booking by name,
    # (b) carry her name on the card, (c) NOT read as foreign. Nina's own
    # disarm on that booking is foreign — she is not the facilitator.
    nina = ev("901", "Nina Li — *bookings covered* [Facilitator: Krista Flynn]", 20.5, 22.5)
    nina["facilitator"] = build.facilitator_of(
        "Nina Li: *bookings covered* [Facilitator: Krista Flynn]")
    fails += check("facilitator parsed", nina["facilitator"], "Krista Flynn")
    arm = [{"studio": "901", "name": "Krista Flynn", "time": "20:28", "kind": "arrival"},
           {"studio": "901", "name": "Stefan", "time": "22:31", "kind": "departure"}]
    build.apply_arm_events([nina], arm)
    fails += check("facilitator disarm is the arrival", nina["arrived"], "20:28")
    fails += check("actor carried", nina["arrived_by"], "Krista Flynn")
    fails += check("facilitator is not foreign", nina["arrived_foreign"], False)
    fails += check("staff arm carried", nina["departed_by"], "Stefan")
    fails += check("staff arm is foreign", nina["departed_foreign"], True)

    plain = ev("901", "Nina Li — Heels", 20.5, 22.5)
    plain["facilitator"] = None
    build.apply_arm_events([plain], [
        {"studio": "901", "name": "Akira Huang", "time": "20:29", "kind": "arrival"}])
    fails += check("unnamed guest still lands (pass 2)", plain["arrived"], "20:29")
    fails += check("unnamed guest is foreign", plain["arrived_foreign"], True)
    fails += check("no facilitator → booker expected", build.expected_name(plain), "Nina Li — Heels")

    nameless = ev("693", "Angus Dirnbeck", 18.0, 20.0)
    build.apply_arm_events([nameless], [
        {"studio": "693", "name": "", "time": "18:02", "kind": "arrival"}])
    fails += check("nameless event is not foreign", nameless["arrived_foreign"], False)


    # ---- panel_backstop: the phantom of 2026-09-05 ------------------------
    #
    # The board printed a second, nameless arrival on 509A at 18:53 while
    # Simona Horova was alone in the studio. Alarm.com's own PastEvents export
    # has no 18:53 event at all — only her 18:40:07 disarm and 19:52:36 arm.
    # The builder made it: the listener was down 18:45:53-18:52:52 and the
    # panel ledger's tick stalled with it, so the ledger wrote her disarm at
    # 18:53:46 — 13m38s late, past the 8-minute same-kind window that was
    # sized off a measured 1-6 minute lag. These are the real timestamps.
    MIN = 60 * 1000
    door_509a = [
        {"studio": "509A", "kind": "arrival", "ts": 1788648007997},   # 18:40:07
        {"studio": "509A", "kind": "departure", "ts": 1788652356861},  # 19:52:36
    ]
    phantom = {"studio": "509A", "kind": "arrival", "name": "",
               "ts": 1788648826000}                                   # 18:53:46
    fails += check("13m38s-late restatement is dropped",
                   build.panel_backstop([phantom], door_509a), [])

    # The ordinary case the old window already handled must not regress.
    fails += check("3-minute-late restatement is dropped",
                   build.panel_backstop(
                       [{"studio": "509A", "kind": "arrival",
                         "ts": 1788648007997 + 3 * MIN}], door_509a), [])

    # An hour late and still a restatement — no window would have caught this.
    fails += check("60-minute-late restatement is dropped",
                   build.panel_backstop(
                       [{"studio": "509A", "kind": "arrival",
                         "ts": 1788648007997 + 60 * MIN}], door_509a), [])

    # THE BACKSTOP MUST STILL BACKSTOP. An event the socket genuinely lost
    # changes the state, so it survives however late it lands.
    missed = {"studio": "509A", "kind": "departure",
              "ts": 1788648007997 + 40 * MIN}
    fails += check("a real missed departure survives",
                   build.panel_backstop([missed], door_509a[:1]), [missed])

    # A disarm and re-arm that BOTH fell inside one gap: each flips the state,
    # so each survives. This is the case a naive last-door-event check loses.
    out = build.panel_backstop(
        [{"studio": "509A", "kind": "departure", "ts": 1788648007997 + 20 * MIN},
         {"studio": "509A", "kind": "arrival", "ts": 1788648007997 + 25 * MIN}],
        door_509a[:1])
    fails += check("both halves of an in-gap round trip survive", len(out), 2)

    # A studio the door feed never saw has no known state — keep it, an
    # unknown state is not evidence of a duplicate.
    lone = {"studio": "693", "kind": "arrival", "ts": 1788648826000}
    fails += check("panel event for an unseen studio survives",
                   build.panel_backstop([lone], door_509a), [lone])

    # Another studio's events must not mask this one's.
    fails += check("dedup does not cross studios",
                   build.panel_backstop(
                       [{"studio": "693", "kind": "arrival",
                         "ts": 1788648007997 + MIN}], door_509a),
                   [{"studio": "693", "kind": "arrival",
                     "ts": 1788648007997 + MIN}])

    # A door twin stamped just AFTER the panel copy — the one shape state
    # cannot see, which is why the same-kind window survives as rule two.
    fails += check("same-kind twin arriving later is still dropped",
                   build.panel_backstop(
                       [{"studio": "527", "kind": "arrival", "ts": 1788648007997}],
                       [{"studio": "527", "kind": "arrival",
                         "ts": 1788648007997 + 4 * MIN}]), [])

    print("FAILED" if fails else "ok — 50 checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
