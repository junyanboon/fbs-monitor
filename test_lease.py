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


# ── The Hermes provider (which model thinks) is not the holder (who runs) ────
def test_provider_absent_reads_as_unknown_never_the_holder():
    L = build.parse_lease(LIVE, NOW)
    assert L["holder"] == "codex" and L["provider"] is None


def test_provider_recorded_by_hermes_provider_sh_is_projected():
    d = json.loads(LIVE)
    d["provider"] = {"name": "claude", "primary": "anthropic", "model": "claude-opus-5",
                     "fallback": "openai-codex", "fallback_model": "gpt-6-astra",
                     "set_at": "2026-09-15T21:30:00Z", "set_by": "hermes-provider.sh:jboon"}
    P = build.parse_lease(json.dumps(d), NOW)["provider"]
    assert P["name"] == "Claude" and P["model"] == "claude-opus-5"
    assert P["fallbackName"] == "Codex" and P["fallbackModel"] == "gpt-6-astra"
    assert P["setAtISO"].startswith("2026-09-15T17:30")        # Toronto
    assert build.parse_provider({"primary": "openai-codex"})["name"] == "Codex"
    assert build.parse_provider({"primary": "mistral"})["name"] == "Mistral"
    assert build.parse_provider({"garbage": 1}) is None
    assert build.parse_provider("codex") is None


def test_fleet_footer_carries_the_provider():
    d = json.loads(LIVE); d["provider"] = {"primary": "anthropic", "model": "claude-opus-5"}
    L = build.parse_lease(json.dumps(d), NOW)
    F = build.fleet_lines([{"run": "The Host", "status": "ok", "monitoring": "Live",
                            "statusLabel": "On time", "lastISO": None}], L, NOW)
    assert F["provider"]["name"] == "Claude" and F["holder"] == "codex"


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


# ── Token usage under the CLAUDE pill (2026-09-18) ──────────────────────────
# Hermes' usage cron writes a `usage` key into the lease JSON every 15 min.
# The rail shows it; no key → "Usage not recorded", never an estimate.
USAGE = {
    "as_of": "2026-09-07T02:50:00Z",          # 22:50 Toronto, 10 min before NOW
    "alarm": False, "alarm_note": "",
    "today": {"date": "2026-09-06", "fires": 61, "prompt_tokens": 41_200_000,
              "cached_tokens": 38_900_000, "completion_tokens": 310_000,
              "errors": 2, "max_fire_prompt_tokens": 2_900_000, "max_fire_job": "Mechanic"},
    "yesterday": {"date": "2026-09-05", "fires": 88, "prompt_tokens": 120_000_000,
                  "cached_tokens": 110_000_000, "completion_tokens": 600_000, "errors": 0},
}


def test_usage_absent_reads_as_not_recorded():
    L = build.parse_lease(LIVE, NOW)
    assert L["usage"] is None
    assert build.parse_usage({"garbage": 1}, NOW) is None
    assert build.parse_usage("41M", NOW) is None


def test_usage_written_by_hermes_is_projected():
    d = json.loads(LIVE); d["usage"] = USAGE
    U = build.parse_lease(json.dumps(d), NOW)["usage"]
    t = U["today"]
    assert t["fires"] == 61 and t["promptTokens"] == 41_200_000
    assert t["cachedPct"] == 94 and t["completionTokens"] == 310_000
    assert t["maxFireJob"] == "Mechanic" and t["maxFireTokens"] == 2_900_000
    assert U["yesterday"]["promptTokens"] == 120_000_000
    assert U["asOfISO"].startswith("2026-09-06T22:50") and U["stale"] is False
    assert U["alarm"] is False


def test_usage_older_than_three_writes_is_stale():
    d = json.loads(LIVE); d["usage"] = dict(USAGE, as_of="2026-09-07T01:00:00Z")   # 2h before NOW
    assert build.parse_lease(json.dumps(d), NOW)["usage"]["stale"] is True


def test_usage_alarm_and_cache_cap_survive():
    d = json.loads(LIVE)
    d["usage"] = dict(USAGE, alarm=True, alarm_note="Concierge fire 3.4M prompt tokens at 14:10")
    d["usage"]["today"] = dict(USAGE["today"], cached_tokens=99_000_000)   # cannot exceed prompt
    U = build.parse_lease(json.dumps(d), NOW)["usage"]
    assert U["alarm"] is True and "Concierge" in U["alarmNote"]
    assert U["today"]["cachedTokens"] == 41_200_000 and U["today"]["cachedPct"] == 100


def test_usage_per_job_and_per_run_are_projected():
    """2026-09-19: the Usage tab lists the day by agent and by fire."""
    d = json.loads(LIVE)
    d["usage"] = dict(USAGE, limits={"fire_prompt_tokens": 10_000_000, "day_prompt_tokens": 250_000_000})
    d["usage"]["today"] = dict(USAGE["today"],
        jobs=[{"job": "Concierge", "fires": 9, "prompt_tokens": 18_000_000, "cached_tokens": 30_000_000,
               "completion_tokens": 90_000, "errors": 1, "max_fire_prompt_tokens": 2_900_000,
               "last_fire_at": "2026-09-06T22:10:00Z"},
              {"fires": 3, "prompt_tokens": 5},                       # no job name → dropped
              {"job": "Doorman", "fires": 2, "prompt_tokens": 0}],
        runs=[{"at": "2026-09-06T21:30:00Z", "job": "Concierge", "prompt_tokens": 2_900_000,
               "cached_tokens": 2_700_000, "completion_tokens": 12_000, "error": 1},
              {"at": "bad time", "job": "Doorman"},
              {"job": ""}])
    U = build.parse_lease(json.dumps(d), NOW)["usage"]
    assert U["limits"] == {"firePromptTokens": 10_000_000, "dayPromptTokens": 250_000_000}
    J = U["today"]["jobs"]
    assert [j["job"] for j in J] == ["Concierge", "Doorman"]
    assert J[0]["cachedTokens"] == 18_000_000 and J[0]["cachedPct"] == 100   # capped at prompt
    assert J[0]["errors"] == 1 and J[0]["maxFireTokens"] == 2_900_000
    assert J[0]["lastISO"].startswith("2026-09-06T")
    assert J[1] == {"job": "Doorman", "fires": 2, "promptTokens": 0, "cachedTokens": 0, "cachedPct": 0,
                    "completionTokens": 0, "errors": 0, "maxFireTokens": 0, "lastISO": None}
    R = U["today"]["runs"]
    assert [r["job"] for r in R] == ["Concierge", "Doorman"]
    assert R[0]["error"] is True and R[0]["promptTokens"] == 2_900_000 and R[0]["atISO"].startswith("2026-09-06T")
    assert R[1]["atISO"] is None and R[1]["error"] is False
    assert U["yesterday"]["jobs"] == [] and U["yesterday"]["runs"] == []


def test_usage_from_the_older_cron_has_empty_lists_not_missing_keys():
    d = json.loads(LIVE); d["usage"] = USAGE
    U = build.parse_lease(json.dumps(d), NOW)["usage"]
    assert U["today"]["jobs"] == [] and U["today"]["runs"] == []
    assert U["limits"] == {"firePromptTokens": 0, "dayPromptTokens": 0}


def test_usage_runs_are_capped_to_the_newest():
    d = json.loads(LIVE); d["usage"] = USAGE
    d["usage"]["today"] = dict(USAGE["today"], runs=[{"job": "J%d" % i, "prompt_tokens": i} for i in range(250)])
    R = build.parse_lease(json.dumps(d), NOW)["usage"]["today"]["runs"]
    assert len(R) == build.USAGE_RUNS_MAX and R[-1]["job"] == "J249" and R[0]["job"] == "J50"


REPORT = """FLEET USAGE V1 · 2026-09-06 · generated 2026-09-06T22:50:58Z

RUNAWAY ALARM:
  !! The Concierge single fire 18,484,442 prompt tokens (limit 10,000,000)
  !! The Concierge — Closeout single fire 12,514,267 prompt tokens (limit 10,000,000)
  !! fleet day total 327,844,394 prompt tokens (limit 250,000,000)

FLEET TOTAL  fires 77  calls 2754  prompt 327,844,394  completion 1,599,941  cached 309,794,532  uncached 18,049,862  errors 0

  The Responder            fires  32 calls  1043 prompt   112,900,502 cached   104,920,275 (92.9%) err 0
  The Concierge            fires   7 calls   491 prompt    75,751,131 cached    73,591,864 (97.1%) err 2
  The Concierge — Closeout fires   7 calls   379 prompt    44,773,513 cached    42,920,202 (95.9%) err 0
  The Bookkeeper           fires   1 calls     3 prompt       100,685 cached       178,057 (77.5%) err 0
"""


def test_usage_report_block_is_parsed_by_agent():
    R = build.parse_usage_report(REPORT)
    assert R["date"] == "2026-09-06" and R["generatedISO"].startswith("2026-09-06T")
    assert R["limits"] == {"firePromptTokens": 10_000_000, "dayPromptTokens": 250_000_000}
    J = {j["job"]: j for j in R["jobs"]}
    assert list(J) == ["The Responder", "The Concierge", "The Concierge — Closeout", "The Bookkeeper"]
    assert J["The Responder"]["fires"] == 32 and J["The Responder"]["promptTokens"] == 112_900_502
    assert J["The Responder"]["cachedPct"] == 93 and J["The Responder"]["maxFireTokens"] is None
    assert J["The Concierge"]["errors"] == 2 and J["The Concierge"]["maxFireTokens"] == 18_484_442
    assert J["The Concierge — Closeout"]["maxFireTokens"] == 12_514_267
    assert J["The Bookkeeper"]["cachedTokens"] == 100_685 and J["The Bookkeeper"]["cachedPct"] == 100   # capped
    assert all(j["completionTokens"] is None and j["lastISO"] is None for j in R["jobs"])
    assert build.parse_usage_report("## Why this row exists") is None
    assert build.parse_usage_report("") is None and build.parse_usage_report(None) is None


def test_usage_report_fills_jobs_only_when_the_lease_has_none_for_that_day():
    R = build.parse_usage_report(REPORT)
    d = json.loads(LIVE); d["usage"] = USAGE                      # today = 2026-09-06, no jobs
    L = build.parse_lease(json.dumps(d), NOW)
    assert build.merge_usage_report(L, R) == "run-monitor"
    assert [j["job"] for j in L["usage"]["today"]["jobs"]][0] == "The Responder"
    assert L["usage"]["limits"]["firePromptTokens"] == 10_000_000
    assert L["usage"]["jobsSource"] == "run-monitor" and L["usage"]["jobsAsOfISO"].startswith("2026-09-06T")
    # another day's report is not today's split
    L = build.parse_lease(json.dumps(d), NOW)
    assert build.merge_usage_report(L, dict(R, date="2026-09-05")) is None and L["usage"]["today"]["jobs"] == []
    # the lease's own jobs win
    d["usage"] = dict(USAGE, today=dict(USAGE["today"], jobs=[{"job": "Lease Job", "fires": 1, "prompt_tokens": 5}]))
    L = build.parse_lease(json.dumps(d), NOW)
    assert build.merge_usage_report(L, R) == "lease" and L["usage"]["today"]["jobs"][0]["job"] == "Lease Job"
    # no usage at all: nothing to fill
    assert build.merge_usage_report(build.parse_lease(LIVE, NOW), R) is None


def test_provider_account_label_is_projected():
    d = json.loads(LIVE)
    d["provider"] = {"primary": "anthropic", "model": "claude-opus-5", "account": "claude-danceannex"}
    L = build.parse_lease(json.dumps(d), NOW)
    assert L["provider"]["account"] == "claude-danceannex"
    assert build.parse_provider({"primary": "anthropic"})["account"] == ""


def test_fleet_carries_usage_beside_provider():
    d = json.loads(LIVE); d["provider"] = {"primary": "anthropic"}; d["usage"] = USAGE
    L = build.parse_lease(json.dumps(d), NOW)
    F = build.fleet_lines([{"run": "The Host", "status": "ok", "monitoring": "Live",
                            "statusLabel": "On time", "lastISO": None}], L, NOW)
    assert F["usage"]["today"]["fires"] == 61 and F["provider"]["name"] == "Claude"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
