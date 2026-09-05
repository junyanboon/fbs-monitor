"""The session verdict glyph — the one-glyph answer to "did this booking go
normally?" that rides at the end of the name row on both editions.

The rules live in the templates, in JavaScript, so these tests extract the pure
`verdictOf` helper and run it under node — the same trick
`test_message_dispatch.py` uses for `noGtgChip`. Running BOTH templates through
the identical case table is the point: the desktop board and the mobile PWA
must never disagree about whether a booking was clean.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

TEMPLATES = ("template.html", "template-mobile.html")
ROOT = Path(__file__).parent


def _verdicts(cases, feed_down=False):
    """Run the template's own verdictOf over `cases`, one template at a time."""
    out = {}
    for template in TEMPLATES:
        text = (ROOT / template).read_text()
        helper = re.search(r"function verdictOf\(e, ?now\)\{.*?\n\}", text, re.S)
        assert helper, f"{template} must expose the pure session-verdict helper"
        script = (
            "const parseHM=s=>{if(!s)return null;"
            "const[a,b]=s.split(':').map(Number);return a+b/60;};\n"
            "const FLAG_MIN=16/60, OVER_BIG=1;\n"
            f"const FEED_DOWN={json.dumps(bool(feed_down))};\n"
            + helper.group(0) + "\n"
            f"const cases={json.dumps(cases)};\n"
            "console.log(JSON.stringify(cases.map(c=>verdictOf(c.e,c.now))));"
        )
        result = subprocess.run(
            ["node", "-e", script], check=True, capture_output=True, text=True)
        out[template] = json.loads(result.stdout)
    return out


def booking(**over):
    e = {"kind": "booking", "start": 10.0, "end": 12.0,
         "arrived": "10:02", "departed": "11:58"}
    e.update(over)
    return e


# (label, event, now, expected class or None)
CASES = [
    ("in and out inside the window", booking(), 13.0, "ok"),
    ("still running", booking(departed=None), 11.0, None),
    ("out 30m late", booking(departed="12:30"), 13.0, "odd"),
    # 16 minutes IS the threshold and the comparison is strict, so this case
    # deliberately sits inside it rather than on it.
    ("out 15m late is still on time", booking(departed="12:15"), 13.0, "ok"),
    ("out 90m late", booking(departed="13:30"), 14.0, "bad"),
    ("in 30m early", booking(arrived="09:30"), 13.0, "odd"),
    ("arrival inferred from an open studio",
     booking(arrived="10:00", assumed="open"), 13.0, "odd"),
    ("arrival inferred from a bypassed door",
     booking(arrived="10:00", assumed="bypass"), 13.0, "odd"),
    ("someone else keyed in",
     booking(arrived_foreign=True, arrived_by="Shiela"), 13.0, "odd"),
    ("someone else armed",
     booking(departed_foreign=True, departed_by="Shiela"), 13.0, "odd"),
    ("nobody disarmed at all",
     booking(arrived=None, departed=None), 13.0, "bad"),
    ("renter was in another room",
     booking(arrived=None, departed=None,
             wrong_studio={"studio": "509A", "at": "10:04"}), 13.0, "bad"),
    # The back-to-back case: one panel, no arm between two renters, so the
    # earlier booking honestly has no departure. Junyan asked for it flagged
    # anyway (2026-09-05) — amber, never red, because it is usually routine.
    ("came in, departure never observed", booking(departed=None), 13.0, "odd"),
    ("a second calendar row for the same renter",
     booking(arrived=None, departed=None, dup_studios=True), 13.0, None),
    ("staff block, not a renter",
     booking(kind="staff", arrived=None, departed=None), 13.0, None),
]


def test_both_editions_return_the_same_verdict_for_every_case():
    rendered = _verdicts([{"e": e, "now": now} for _, e, now, _ in CASES])
    desktop, mobile = (rendered[t] for t in TEMPLATES)
    for (label, _, _, expected), d, m in zip(CASES, desktop, mobile):
        assert (d or {}).get("cls") == expected, f"desktop: {label} → {d}"
        assert d == m, f"editions disagree on {label}: {d} vs {m}"


def test_a_serious_verdict_says_why_in_its_tooltip():
    rendered = _verdicts([
        {"e": booking(departed="13:30"), "now": 14.0},
        {"e": booking(arrived=None, departed=None), "now": 13.0},
    ])
    for template, (late, missing) in rendered.items():
        assert late["glyph"] == "!" and "90m late" in late["why"], template
        assert missing["glyph"] == "!" and "No arrival" in missing["why"], template


def test_an_unclosed_booking_is_amber_and_says_so():
    """A finished booking nobody armed after. Almost always the back-to-back
    case, so it must stay amber — red here would accuse a large share of every
    ordinary day of something that is usually nothing."""
    rendered = _verdicts([{"e": booking(departed=None), "now": 13.0}])
    for template, (v,) in rendered.items():
        assert v["cls"] == "odd" and v["glyph"] == "\u26a0", template
        assert "No departure seen" in v["why"], template


def test_a_clean_verdict_is_a_check_mark_and_an_odd_one_a_warning_sign():
    rendered = _verdicts([
        {"e": booking(), "now": 13.0},
        {"e": booking(departed="12:30"), "now": 13.0},
    ])
    for template, (clean, odd) in rendered.items():
        assert clean["glyph"] == "✓", template
        assert odd["glyph"] == "⚠", template


def test_a_dead_arrival_feed_produces_no_verdict_at_all():
    """FEED_DOWN means `arrived` is null for EVERY booking because nothing was
    watching. A verdict then is manufactured out of an outage — the same reason
    stateOf() suppresses the Missing flag (README rule 0)."""
    cases = [{"e": e, "now": now} for _, e, now, _ in CASES]
    for template, verdicts in _verdicts(cases, feed_down=True).items():
        assert all(v is None for v in verdicts), template


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_verdict_rides_at_the_end_of_the_name_row(template):
    """Right of the tier pill; beside the name when there is no pill."""
    text = (ROOT / template).read_text()
    assert "${verdictChip(verdictOf(e,n))}" in text
    assert ".verdict.ok{" in text and ".verdict.odd{" in text and ".verdict.bad{" in text


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_two_editions_share_one_lateness_threshold(template):
    text = (ROOT / template).read_text()
    assert re.search(r"const FLAG_MIN\s*=\s*16/60", text)
    assert re.search(r"const OVER_BIG\s*=\s*1\b", text)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
