"""The fleet lease panel on the Robots tab.

The lease answers "who owns this lane, and where does its body run" — the
question the watched-runs list underneath cannot answer. A lane that is not
held is SUPPOSED to be silent, so a silent lane only reads as a fault once you
know who was meant to run it.

Two rules these tests exist to hold:
  1. A lane with no `executors` entry inherits mac. That is the lease page's
     own rule; guessing "vps" because most lanes are there would say a lane is
     safely on the server when it is actually waiting on a laptop being awake.
  2. An unreadable lease shows a notice. It never falls back to the roster, and
     it never takes the tab down.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import build

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 6, 23, 0, tzinfo=TZ)

LIVE = json.dumps({
    "holder": "codex",
    "since": "2026-09-04T14:10:00Z",
    "reason": "Manual cutover 2026-09-04: Claude tokens exhausted.",
    "lease_id": "lease-2026-09-04-cutover-codex",
    "flipped_by": "human:Junyan via claude-session-2026-09-04",
    "executors": {
        "mechanic": "vps", "responder": "vps", "concierge": "vps",
        "custodian": "vps", "doorman": "vps", "analyst": "vps", "host": "vps",
        "bookkeeper": "cloudrun", "treasurer": "vps", "planner": "vps",
        "timekeeper": "vps", "morning-text": "cloudrun", "loop": "vps",
    },
})


def test_holder_and_identity_survive():
    L = build.parse_lease(LIVE, NOW)
    assert L["holder"] == "codex"
    assert L["leaseId"] == "lease-2026-09-04-cutover-codex"
    assert L["sinceISO"].startswith("2026-09-04")


def test_every_lane_is_named_and_placed():
    L = build.parse_lease(LIVE, NOW)
    assert len(L["lanes"]) == 13
    by = {l["lane"]: l for l in L["lanes"]}
    assert by["concierge"]["name"] == "The Concierge"
    assert by["concierge"]["how"] == "Hermes cron · VPS"
    assert by["bookkeeper"]["executor"] == "cloudrun"
    assert by["bookkeeper"]["how"] == "Deterministic · Cloud Run"
    assert L["counts"] == {"vps": 11, "cloudrun": 2}


def test_a_missing_entry_inherits_mac_not_the_majority():
    """The lease page's rule: absent means mac. The Mac needs the Codex app
    open and the machine awake, so this is the fragile mode — it must not be
    quietly reported as the VPS just because eleven siblings are there."""
    d = json.loads(LIVE)
    del d["executors"]["loop"]
    L = build.parse_lease(json.dumps(d), NOW)
    loop = next(l for l in L["lanes"] if l["lane"] == "loop")
    assert loop["executor"] == "mac"
    assert loop["default"] is True
    assert loop["status"] == "watch"      # surfaced, not treated as healthy
    # and a lane that IS written down is never marked inherited
    assert next(l for l in L["lanes"] if l["lane"] == "host")["default"] is False


def test_fragile_lanes_sort_above_settled_ones():
    d = json.loads(LIVE)
    d["executors"]["planner"] = "mac"
    L = build.parse_lease(json.dumps(d), NOW)
    assert L["lanes"][0]["lane"] == "planner"


def test_retired_lane_is_shown_greyed_not_hidden():
    """A retired lane still appears. Silence from a lane you cannot see is the
    thing this panel exists to prevent."""
    d = json.loads(LIVE)
    d["executors"]["morning-text"] = "retired"
    L = build.parse_lease(json.dumps(d), NOW)
    mt = next(l for l in L["lanes"] if l["lane"] == "morning-text")
    assert mt["how"] == "Retired"
    assert mt["status"] == "plain"


def test_unknown_executor_value_renders_verbatim():
    """A value this build has never seen must not be dropped or renamed."""
    d = json.loads(LIVE)
    d["executors"]["host"] = "fly-io"
    L = build.parse_lease(json.dumps(d), NOW)
    host = next(l for l in L["lanes"] if l["lane"] == "host")
    assert host["executor"] == "fly-io" and host["how"] == "fly-io"


def test_a_lane_absent_from_the_name_map_still_renders():
    d = json.loads(LIVE)
    d["executors"]["greeter"] = "vps"
    L = build.parse_lease(json.dumps(d), NOW)
    assert next(l for l in L["lanes"] if l["lane"] == "greeter")["name"] == "Greeter"


def test_lease_py_wrapper_shape_is_accepted():
    """`lease.py show` wraps the same object under a "lease" key; the page's
    code block does not. Both must parse."""
    L = build.parse_lease(json.dumps({"lease": json.loads(LIVE)}), NOW)
    assert L["holder"] == "codex" and len(L["lanes"]) == 13


def test_garbage_raises_so_the_caller_shows_its_notice():
    for bad in ("not json", "[1,2,3]"):
        try:
            build.parse_lease(bad, NOW)
        except Exception:
            continue
        raise AssertionError(f"{bad!r} should not have parsed")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
