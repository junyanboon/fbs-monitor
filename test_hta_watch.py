"""HTA watchdog: the queue is the evidence, never the board's HTA flag.

Pure tests — no network. Covers the verdict table, the alert window, the
public pill projection, and the Actions write-back in dry run.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import build

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 5, 21, 20, tzinfo=TZ)          # Sat 21:20, the night before


def booking(**kw):
    b = {"id": "bk-thea", "artist": "artist-thea", "studio": "509A",
         "date": "2026-09-06", "start": "14:00", "tier": "FBS", "hta": "Sent",
         "who": "Thea Giggster (Studio 509A)"}
    b.update(kw)
    return b


def row(status, *, artist="artist-thea", studio="509A", send_after=None,
        sent_at=None, created="2026-09-06T00:31:39.000Z", linked=False, rid="q1"):
    return {"id": rid, "artist": artist, "studio": studio, "status": status,
            "send_after": send_after, "sent_at": sent_at, "created": created,
            "receipt": status == "Sent", "linked": linked}


def verdict(bk, rows, now=NOW):
    out = build.hta_verdicts([bk], rows, now)
    assert len(out) == 1, out
    return out[0]


def test_board_flag_alone_is_never_verification():
    # Board says Sent, queue has nothing of the HTA shape → missing.
    v = verdict(booking(hta="Sent"), [])
    assert v["state"] == "missing", v


def test_sent_row_inside_lookback_verifies():
    v = verdict(booking(), [row("Sent", sent_at="2026-09-06T01:21:00.000Z")])
    assert v["state"] == "verified" and v["row"]["id"] == "q1"


def test_sent_row_from_an_old_booking_does_not_count():
    old = row("Sent", sent_at="2026-08-20T01:21:00.000Z", created="2026-08-20T01:00:00.000Z")
    assert verdict(booking(), [old])["state"] == "missing"


def test_other_studio_row_does_not_count_but_unstudioed_row_does():
    other = row("Sent", studio="901", sent_at="2026-09-06T01:21:00.000Z")
    assert verdict(booking(), [other])["state"] == "missing"
    bare = row("Sent", studio="", sent_at="2026-09-06T01:21:00.000Z")
    assert verdict(booking(), [bare])["state"] == "verified"


def test_ready_row_past_its_send_time_is_stuck():
    late = row("Ready to Send", send_after="2026-09-05T20:00:00-04:00")
    assert verdict(booking(), [late])["state"] == "stuck"


def test_ready_row_timed_after_the_booking_starts_is_stuck():
    after = row("Ready to Send", send_after="2026-09-06T15:00:00-04:00")
    assert verdict(booking(), [after])["state"] == "stuck"


def test_ready_row_with_a_future_send_time_is_scheduled():
    # Thea's actual row: Send After 09:00 the booking morning.
    sched = row("Ready to Send", send_after="2026-09-06T13:00:00.000Z")
    v = verdict(booking(), [sched])
    assert v["state"] == "scheduled"
    assert build._hta_pill(v) == {"kind": "HTA", "state": "scheduled", "time": "09:00"}


def test_untimed_ready_row_is_queued_briefly_then_stuck():
    fresh = row("Ready to Send", created=(NOW - timedelta(minutes=3)).isoformat())
    v = verdict(booking(), [fresh])
    assert v["state"] == "scheduled"
    assert build._hta_pill(v) == {"kind": "HTA", "state": "queued", "time": None}
    stale = row("Ready to Send", created=(NOW - timedelta(minutes=30)).isoformat())
    assert verdict(booking(), [stale])["state"] == "stuck"


def test_review_only_rows_are_awaiting():
    assert verdict(booking(), [row("Pending Review")])["state"] == "awaiting"
    assert verdict(booking(), [row("Error")])["state"] == "awaiting"


def test_will_not_send_is_intentional_absence():
    assert verdict(booking(hta="Will Not Send"), [])["state"] == "intentional"
    assert verdict(booking(), [row("Will Not Send")])["state"] == "intentional"
    assert build._hta_pill(verdict(booking(), [row("Will Not Send")])) is None


def test_newest_row_wins_when_a_replacement_was_sent():
    stale = row("Error", rid="q-old", created="2026-09-05T20:00:00.000Z")
    fresh = row("Sent", rid="q-new", sent_at="2026-09-06T01:21:00.000Z",
                created="2026-09-06T00:31:00.000Z")
    v = verdict(booking(), [stale, fresh])
    assert v["state"] == "verified" and v["row"]["id"] == "q-new"


def test_no_artist_or_no_clock_is_unknown_not_missing():
    assert build.hta_verdicts([booking(artist=None)], [], NOW) == []
    assert build.hta_verdicts([booking(start=None)], [], NOW) == []


def test_pill_appears_only_inside_the_alert_window():
    ev = {"kind": "booking", "_notion_id": "bk-thea", "dispatch": []}
    far = verdict(booking(date="2026-09-08"), [])          # ~64 h out
    assert build.apply_hta_watch([dict(ev)], [far])[0]["dispatch"] == []
    near = verdict(booking(), [])                         # ~16.7 h out
    out = build.apply_hta_watch([dict(ev)], [near])[0]["dispatch"]
    assert out == [{"kind": "HTA", "state": "missing", "time": None}]


def test_verified_booking_draws_no_pill():
    ev = {"kind": "booking", "_notion_id": "bk-thea", "dispatch": []}
    ok = verdict(booking(), [row("Sent", sent_at="2026-09-06T01:21:00.000Z")])
    assert build.apply_hta_watch([dict(ev)], [ok])[0]["dispatch"] == []


def test_public_pill_carries_no_private_fields():
    for state in ("missing", "stuck", "awaiting"):
        pill = build._hta_pill({"state": state, "row": row("Ready to Send")})
        assert set(pill) == {"kind", "state", "time"}, pill
        assert pill["kind"] == "HTA"


def test_action_title_is_stable_for_dedup():
    t = build._hta_action_title(booking())
    assert t == "🔔 HTA not sent — Thea Giggster · Studio 509A · 2026-09-06 14:00"


def test_sync_dry_run_raises_once_and_links_verified(monkeypatch=None):
    calls = []
    orig_query = build._notion_query
    build._notion_query = lambda token, ds, body: calls.append(("query", ds)) or []
    env = dict(os.environ)
    os.environ["NOTION_TOKEN"] = "x"
    os.environ["HTA_WATCH_DRYRUN"] = "1"
    try:
        v_missing = verdict(booking(), [])
        v_ok = verdict(booking(id="bk-2"), [row("Sent", sent_at="2026-09-06T01:21:00.000Z")])
        v_far = verdict(booking(id="bk-3", date="2026-09-08"), [])
        # Must not raise and must not touch requests: dry run only logs.
        build.requests = None
        build.sync_hta_watch([v_missing, v_ok, v_far], NOW)
        assert calls == [("query", build.ACTIONS_DS)]   # one dedup read, no writes
    finally:
        os.environ.clear(); os.environ.update(env)
        build._notion_query = orig_query
        import requests
        build.requests = requests


def test_kill_switch_writes_nothing():
    env = dict(os.environ)
    os.environ["HTA_WATCH_DISABLED"] = "1"
    os.environ["NOTION_TOKEN"] = "x"
    try:
        build.requests = None
        build.sync_hta_watch([verdict(booking(), [])], NOW)   # would need requests otherwise
    finally:
        os.environ.clear(); os.environ.update(env)
        import requests
        build.requests = requests


def test_templates_render_the_new_states():
    for tpl in ("template.html", "template-mobile.html"):
        src = open(tpl, encoding="utf-8").read()
        assert '"STUCK"' in src and '"UNSENT"' in src, tpl
        assert ".dispatch-seg.stuck" in src and ".dispatch-seg.awaiting" in src, tpl


def main():
    test_board_flag_alone_is_never_verification()
    test_sent_row_inside_lookback_verifies()
    test_sent_row_from_an_old_booking_does_not_count()
    test_other_studio_row_does_not_count_but_unstudioed_row_does()
    test_ready_row_past_its_send_time_is_stuck()
    test_ready_row_timed_after_the_booking_starts_is_stuck()
    test_ready_row_with_a_future_send_time_is_scheduled()
    test_untimed_ready_row_is_queued_briefly_then_stuck()
    test_review_only_rows_are_awaiting()
    test_will_not_send_is_intentional_absence()
    test_newest_row_wins_when_a_replacement_was_sent()
    test_no_artist_or_no_clock_is_unknown_not_missing()
    test_pill_appears_only_inside_the_alert_window()
    test_verified_booking_draws_no_pill()
    test_public_pill_carries_no_private_fields()
    test_action_title_is_stable_for_dedup()
    test_sync_dry_run_raises_once_and_links_verified()
    test_kill_switch_writes_nothing()
    test_templates_render_the_new_states()
    print("HTA watch regression tests: OK")


if __name__ == "__main__":
    main()
