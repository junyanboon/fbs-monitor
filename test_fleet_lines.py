"""The five lines on the Robots tab.

Junyan, 2026-09-08: the 30-row roster was "too huge and cumbersome … what
needs to be clear to me is who is doing what and what are the implications
when things are down." So the tab is five lines, each naming a thing the
studio needs and what happens if it stops.

Rules pinned here:
  1. A line is as bad as its worst LIVE robot. Paused, retired, off-hours
     robots never colour a line.
  2. A robot that matches no line never colours any line. A new persona must
     be mapped on purpose, not guessed into "Today's plan".
  3. Specific needles beat general ones: "Watchlist sync" is plan, not watch;
     "desk-loop" is plan, same as The Loop.
  4. The worst robot is named, so a bad line can say what broke.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import build

NOW = datetime(2026, 9, 8, 7, 40, tzinfo=ZoneInfo("America/Toronto"))


def rob(run, status="ok", monitoring="Live", label="On time", last="2026-09-08T07:00:00-04:00"):
    return {"run": run, "status": status, "monitoring": monitoring,
            "statusLabel": label, "lastISO": last}


LEASE = {"holder": "codex", "counts": {"vps": 11, "cloudrun": 2}}


def lines(robots):
    return {l["k"]: l for l in build.fleet_lines(robots, LEASE, NOW)["lines"]}


def test_every_robot_in_the_live_roster_lands_on_a_line():
    """Every name seen in the Run Monitor on 2026-09-06 maps somewhere."""
    seen = ["The Concierge — 2026-09-05 20:10 interrupted", "alarm-watchdog (Cloud Run)",
            "QA release check", "scribe (GitHub Actions)", "sentinel-sweep (Cloud Run)",
            "The Analyst", "The Bookkeeper", "The Doorman", "The Host", "The Mechanic",
            "The Planner", "The Responder", "The Timekeeper", "The Treasurer",
            "The Weatherman", "desk-loop (Cloud Run)", "drop-sweep (Cloud Run)",
            "greeter-sweep (Cloud Run)", "migration-sweep (Cloud Run — alarm-mcp)",
            "The Closer", "The Concierge", "The Custodian", "The Loop", "The Morning Text",
            "The Opener", "The Receptionist", "The Sentinel", "The Watchman",
            "Watchlist sync", "🔑 Code Mirror"]
    missing = [s for s in seen if build.fleet_line_for(s) is None]
    assert missing == [], missing


def test_specific_needles_beat_general_ones():
    assert build.fleet_line_for("Watchlist sync") == "plan"      # not "watch"
    assert build.fleet_line_for("The Watchman") == "watch"
    assert build.fleet_line_for("desk-loop (Cloud Run)") == "plan"
    assert build.fleet_line_for("The Loop") == "plan"


def test_all_fine_is_five_green_lines():
    L = lines([rob("The Doorman"), rob("The Concierge"), rob("The Host"),
               rob("The Bookkeeper"), rob("sentinel-sweep (Cloud Run)")])
    assert [L[k]["status"] for k in ("doors", "mail", "plan", "money", "watch")] == ["ok"] * 5
    assert all(L[k]["worst"] is None for k in L)


def test_a_line_is_as_bad_as_its_worst_live_robot():
    L = lines([rob("The Concierge", "watch", label="Late"),
               rob("The Responder", "crit", label="Overdue"),
               rob("greeter-sweep (Cloud Run)")])
    assert L["mail"]["status"] == "crit"
    assert L["mail"]["worst"]["run"] == "The Responder"
    assert L["mail"]["worst"]["label"] == "Overdue"


def test_one_off_row_suffix_is_trimmed_from_the_name():
    L = lines([rob("The Concierge — 2026-09-05 20:10 interrupted", "crit", label="Overdue")])
    assert L["mail"]["worst"]["run"] == "The Concierge"


def test_paused_and_retired_never_colour_a_line():
    L = lines([rob("The Sentinel", "crit", monitoring="Paused"),
               rob("The Watchman", "crit", monitoring="Not reporting"),
               rob("sentinel-sweep (Cloud Run)")])
    assert L["watch"]["status"] == "ok"
    # but they are still counted as members, so the fold can name them
    assert "The Sentinel" in L["watch"]["robots"]


def test_off_hours_is_not_a_fault():
    L = lines([rob("The Loop", "plain", label="Off-hours")])
    assert L["plan"]["status"] == "ok"


def test_an_unmapped_robot_never_colours_any_line():
    L = lines([rob("The Juggler", "crit")])
    assert all(L[k]["status"] == "ok" for k in L)


def test_footer_names_holder_and_where_most_lanes_run():
    F = build.fleet_lines([rob("The Host")], LEASE, NOW)
    assert F["holder"] == "codex" and F["where"] == "on the VPS"
    assert F["liveCount"] == 1 and F["stamp"] == "07:40"


def test_no_lease_still_renders_lines():
    F = build.fleet_lines([rob("The Host", "crit", label="Overdue")], None, NOW)
    assert F["holder"] == "" and F["where"] == ""
    assert F["lines"][2]["status"] == "crit"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
