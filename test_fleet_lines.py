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


def test_all_fine_is_six_green_lines():
    L = lines([rob("The Doorman"), rob("The Concierge"), rob("The Host"),
               rob("The Bookkeeper"), rob("sentinel-sweep (Cloud Run)"), rob("The Weatherman")])
    assert [L[k]["status"] for k in ("doors", "mail", "plan", "money", "watch", "climate")] == ["ok"] * 6
    assert all(L[k]["worst"] is None for k in L)


# ── The Weatherman line (Junyan, 2026-09-15: its own call-out, with the
#    studio temperatures and what each thermostat is set to) ──────────────────
FRESH = {"studios": [
    {"studio": "509", "name": "509 Bloor", "inside": 23.1, "setpoint": "COOL 22.95",
     "mode": "COOL", "hvac": "OFF", "humidity": 49, "whenISO": "2026-09-08T07:05:00-04:00",
     "ageMin": 35, "stale": False},
    {"studio": "901", "name": "901 Yonge", "inside": 23.3, "setpoint": "",
     "mode": "OFF", "hvac": "OFF", "humidity": 54, "whenISO": "2026-09-08T07:05:00-04:00",
     "ageMin": 35, "stale": False}],
    "blind": False, "note": None, "newestISO": "2026-09-08T07:05:00-04:00"}


def test_weatherman_is_its_own_line_not_the_plan():
    assert build.fleet_line_for("The Weatherman") == "climate"
    assert build.fleet_line_for("climate-sweep (Cloud Run)") == "climate"
    assert build.fleet_line_for("The Planner") == "plan"


def test_weatherman_line_carries_a_reading_per_studio():
    F = build.fleet_lines([rob("The Weatherman")], LEASE, NOW, FRESH)
    c = next(l for l in F["lines"] if l["k"] == "climate")
    assert c["status"] == "ok" and c["climateNote"] is None
    assert [s["name"] for s in c["studios"]] == ["509 Bloor", "901 Yonge"]
    assert c["studios"][0]["inside"] == 23.1 and c["studios"][0]["setpoint"] == "COOL 22.95"
    assert c["studios"][1]["setpoint"] == ""          # 901: no cool setpoint = monitor only


def test_a_stale_reading_turns_the_weatherman_red_even_if_the_robot_is_on_time():
    """climate-controller.md: no reading ≤2h old means the Weatherman is BLIND."""
    blind = dict(FRESH, blind=True, note="Newest reading is older than 2 h — the Weatherman is blind")
    F = build.fleet_lines([rob("The Weatherman")], LEASE, NOW, blind)
    c = next(l for l in F["lines"] if l["k"] == "climate")
    assert c["status"] == "crit"
    assert c["worst"]["run"] == "Thermal Log"


def test_no_thermal_log_shows_a_note_never_a_guess():
    F = build.fleet_lines([rob("The Weatherman")], LEASE, NOW, None)
    c = next(l for l in F["lines"] if l["k"] == "climate")
    assert c["studios"] is None and c["climateNote"] == "Thermal Log unreadable"
    assert c["status"] == "ok"        # the roster still speaks for the robot itself


def _obs(sid, inside, setpoint, notes, when, kind="observation"):
    return {"created_time": when, "properties": {
        "Kind": {"type": "select", "select": {"name": kind}},
        "Studio": {"type": "select", "select": {"name": sid}},
        "Inside °C": {"type": "number", "number": inside},
        "Setpoint": {"type": "rich_text", "rich_text": [{"plain_text": setpoint}] if setpoint else []},
        "Notes": {"type": "rich_text", "rich_text": [{"plain_text": notes}]},
        "When": {"type": "date", "date": {"start": when}},
    }}


def test_parse_climate_takes_the_newest_row_per_studio_and_reads_the_notes():
    rows = [_obs("509", 23.1, "COOL 22.95", "mode COOL · HVAC OFF · humidity 49% · via gh-actions", "2026-09-08T07:05:00-04:00"),
            _obs("901", 23.3, "", "mode OFF · HVAC OFF · humidity 54%", "2026-09-08T07:05:00-04:00"),
            _obs("509", 22.25, "COOL 22.1", "mode COOL · HVAC COOLING · humidity 49%", "2026-09-08T06:05:00-04:00"),
            _obs("509", 30.0, "COOL 20", "an action row, not an observation", "2026-09-08T07:30:00-04:00", kind="action")]
    C = build.parse_climate(rows, NOW)
    assert not C["blind"] and C["note"] is None
    s509, s901 = C["studios"]
    assert (s509["inside"], s509["setpoint"], s509["mode"], s509["hvac"], s509["humidity"]) == (23.1, "COOL 22.95", "COOL", "OFF", 49)
    assert s901["setpoint"] == "" and s901["mode"] == "OFF"
    assert s509["ageMin"] == 35 and not s509["stale"]


def test_parse_climate_flags_blind_after_two_hours_and_missing_studios():
    old = [_obs("509", 23.1, "COOL 22.95", "mode COOL · HVAC OFF", "2026-09-08T05:00:00-04:00"),
           _obs("901", 23.3, "", "mode OFF · HVAC OFF", "2026-09-08T07:05:00-04:00")]
    C = build.parse_climate(old, NOW)
    assert C["blind"] and "older than 2 h" in C["note"]
    only = build.parse_climate(old[1:], NOW)
    assert only["note"] == "No reading on file for 509 Bloor" and only["blind"]
    assert build.parse_climate([], NOW)["blind"]


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


# ── Weatherman rules fold ───────────────────────────────────────────────────
# Plain-text lines as _block_lines returns them for the live 📐 Thermal Model
# page (2026-08-30 edition, bold markers already stripped by the API).
MODEL_LINES = [
    "cool_pull_down_rate: ~1.5°C/hour — fitted 2026-07-26 from SDM ground-truth",
    "warm_up_rate (masonry, summer): ~0.56°C/hour — re-fitted 2026-08-02",
    "Outdoor peak | Lead time | Last changed",
    "Below 24°C | None needed unless inside is already above 24°C | —",
    "24–28°C | 75 min | Tightened 2026-07-26 (was 60 min)",
    "28°C and above | 165 min | Tightened 2026-07-19 (was 135 min)",
    "Comfort ceiling during occupancy: 24°C inside. A complaint is a violation.",
    "Cool setpoint baseline: 26°C (staff-adjusted 2026-07-09, was 27°C)",
    "Heat setpoint baseline: 20°C",
    "Frost floor: 10°C",
    "Gap rule: booking gap longer than 90 min → relax/off between, re-cool with full lead time.",
    "Last re-fit: 2026-08-30 (see the Thermal Log model-refit row for details)",
]


def test_parse_thermal_model_reads_every_number_the_fold_shows():
    m = build.parse_thermal_model(MODEL_LINES)
    assert (m["ceiling"], m["coolBaseline"], m["heatBaseline"], m["frostFloor"]) == (24.0, 26.0, 20.0, 10.0)
    assert (m["gapMin"], m["leadMid"], m["leadHigh"]) == (90.0, 75.0, 165.0)
    assert (m["pullDown"], m["warmUp"]) == (1.5, 0.56)
    assert m["lastRefit"] == "2026-08-30" and m["missing"] == []


def test_a_number_the_page_drops_is_named_missing_never_defaulted():
    m = build.parse_thermal_model([l for l in MODEL_LINES if not l.startswith("Comfort ceiling")])
    assert m["ceiling"] is None and m["missing"] == ["ceiling"]


def test_weatherman_line_carries_rules_and_says_when_the_page_is_unreadable():
    model = build.parse_thermal_model(MODEL_LINES)
    c = next(l for l in build.fleet_lines([rob("The Weatherman")], LEASE, NOW, FRESH, model)["lines"]
             if l["k"] == "climate")
    assert c["rules"]["model"]["ceiling"] == 24.0 and c["rules"]["note"] is None
    assert c["rules"]["code"]["heats"] is False and c["rules"]["code"]["clearsEco"] is False
    c = next(l for l in build.fleet_lines([rob("The Weatherman")], LEASE, NOW, FRESH)["lines"]
             if l["k"] == "climate")
    assert c["rules"]["model"] is None and "unreadable" in c["rules"]["note"]
