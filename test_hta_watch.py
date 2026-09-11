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


def test_merged_bc_row_counts_as_a_sent_access_message():
    # 2026-09-06 Hannah Cho / 509B: the sweep merged the booking-change
    # confirmation and the Returning Access text into BC-sweep-0905-2000-509B,
    # Sent 01:32 with the door and alarm codes. It must verify.
    bc = row("Sent", studio="509B", rid="BC-sweep-0905-2000-509B",
             sent_at="2026-09-06T01:32:00.000Z")
    v = verdict(booking(studio="509B"), [bc])
    assert v["state"] == "verified" and v["row"]["id"].startswith("BC-")


def test_hta_row_filter_accepts_a_sent_bc_row_only():
    # The BC clause is a proof-of-send shape, never a promise-to-send one.
    # Since 2026-09-08 the Sent test lives in Python, not the filter: nesting
    # it inside the OR inside the AND made three levels and Notion allows two
    # (every build from 2026-09-07 03:02Z failed the HTA watch on it). The
    # filter fetches BC rows; fetch_hta_rows drops the unsent ones — see
    # test_unsent_bc_rows_are_dropped_after_the_query.
    seen = {}

    def fake_query(token, ds, payload):
        seen["filter"] = payload["filter"]
        return []

    real = build._notion_query
    build._notion_query = fake_query
    try:
        build.fetch_hta_rows("tok", NOW)
    finally:
        build._notion_query = real
    clauses = seen["filter"]["and"][1]["or"]
    assert {"property": "Message Code", "title": {"starts_with": "BC"}} in clauses
    assert not any("and" in c or "or" in c for c in clauses), "no third level"


def test_shape_names_the_evidence():
    def props(code):
        return {"Message Code": {"type": "title",
                                 "title": [{"plain_text": code}]}}
    assert build._hta_shape(props("BC-sweep-0905-2000-509B")) == "BC"
    assert build._hta_shape(props("HTA-sweep-0906-1400-509A")) == "HTA"
    assert build._hta_shape(props("How to Access 527")) == "HTA"
    assert build._hta_shape(props("Returning Access 527 RA-sweep-0911-1600-527")) == "RA"


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
    # Defined below this function, so main() runs LAST in the file. It used to
    # run here, at which point none of them existed yet: every test after this
    # point was dead code that never once executed, including both BC ones.
    test_merged_bc_row_counts_as_a_sent_access_message()
    test_hta_row_filter_accepts_a_sent_bc_row_only()
    test_shape_names_the_evidence()
    test_hta_filter_never_exceeds_notion_two_level_limit()
    test_unsent_bc_rows_are_dropped_after_the_query()
    test_bc_row_without_codes_is_not_access()
    test_carries_access_reads_the_label_not_the_digits()
    test_filter_depth_counts_like_notion()
    test_returning_access_query_and_verdict()
    test_self_serve_is_outside_hta_watch()
    test_returning_access_verifies_and_is_held_to_the_bc_margin()
    test_returning_access_is_in_the_filter_at_two_levels()
    print("HTA watch regression tests: OK")


# ── filter shape ──────────────────────────────────────────────────────────
# 2026-09-06 nested "BC AND Sent" inside the OR inside the AND: three levels.
# Notion allows two. Every build from 03:02Z on 2026-09-07 failed the HTA
# watch with a misleading "Could not find database" and dropped the pills.
def test_hta_filter_never_exceeds_notion_two_level_limit():
    import datetime
    captured = {}
    def fake_query(token, ds, body):
        captured["filter"] = body["filter"]; return []
    orig = build._notion_query; build._notion_query = fake_query
    try:
        build.fetch_hta_rows("t", datetime.datetime(2026, 9, 6, tzinfo=datetime.timezone.utc))
    finally:
        build._notion_query = orig
    assert build._filter_depth(captured["filter"]) <= 2


def test_unsent_bc_rows_are_dropped_after_the_query():
    """The Sent test moved from the filter into Python; the safety margin
    (a queued or errored BC row is not proof) must survive the move."""
    import datetime
    def mk(code, status, body="Door Code: 5123"):
        return {"id": code, "properties": {
            "Message Code": {"type": "title", "title": [{"plain_text": code}]},
            "Message Body": {"type": "rich_text",
                             "rich_text": [{"plain_text": body}]},
            "Status": {"type": "status", "status": {"name": status}},
            "Artist": {"type": "relation", "relation": []},
            "Studio": {"type": "select", "select": None},
            "Booking": {"type": "relation", "relation": []},
            "Send After": {"type": "date", "date": None},
            "Sent At": {"type": "date", "date": None},
            "Created time": {"type": "created_time", "created_time": "2026-09-06T00:00:00.000Z"},
        }}
    rows = [mk("BC-sweep-1", "Sent"), mk("BC-sweep-2", "Pending Review"),
            mk("BC-sweep-3", "Error"), mk("HTA-1", "Pending Review")]
    orig = build._notion_query; build._notion_query = lambda t, d, b: rows
    try:
        out = build.fetch_hta_rows("t", datetime.datetime(2026, 9, 6, tzinfo=datetime.timezone.utc))
    finally:
        build._notion_query = orig
    assert sorted(r["id"] for r in out) == ["BC-sweep-1", "HTA-1"]


def test_bc_row_without_codes_is_not_access():
    """2026-09-09. A Sent BC row is only proof if it actually carries a code.

    Laurie-Eve Bastiani / 509A / 2026-09-08 read verified off BC-sweep-0908-
    1700-509A, whose whole body was "you're all set for Tuesday, September 8 in
    Studio 509A". No door code, no alarm code. The bare confirmation is the
    COMMON shape of a BC send and the merged access variant is the exception,
    so the title prefix alone quietly verified bookings nobody had been told
    how to enter. An HTA-shaped row is unaffected: it is a How-to-Access by
    construction and is judged on status and clocks alone.
    """
    import datetime
    def mk(code, body):
        return {"id": code, "properties": {
            "Message Code": {"type": "title", "title": [{"plain_text": code}]},
            "Message Body": {"type": "rich_text",
                             "rich_text": [{"plain_text": body}]},
            "Status": {"type": "status", "status": {"name": "Sent"}},
            "Artist": {"type": "relation", "relation": []},
            "Studio": {"type": "select", "select": None},
            "Booking": {"type": "relation", "relation": []},
            "Send After": {"type": "date", "date": None},
            "Sent At": {"type": "date", "date": None},
            "Created time": {"type": "created_time", "created_time": "2026-09-06T00:00:00.000Z"},
        }}
    rows = [
        mk("BC-with-codes", "Please use the same codes as before:\nDoor Code: 5123"),
        mk("BC-alarm-only", "Alarm Code: 6284"),
        mk("BC-bare", "Hi Laurie-Eve, you're all set for Tuesday, September 8."),
        mk("HTA-no-body-marker", "Watch our quick video walkthrough."),
    ]
    orig = build._notion_query; build._notion_query = lambda t, d, b: rows
    try:
        out = build.fetch_hta_rows("t", datetime.datetime(2026, 9, 6, tzinfo=datetime.timezone.utc))
    finally:
        build._notion_query = orig
    assert sorted(r["id"] for r in out) == [
        "BC-alarm-only", "BC-with-codes", "HTA-no-body-marker"]


def test_carries_access_reads_the_label_not_the_digits():
    def props(body):
        return {"Message Body": {"type": "rich_text",
                                 "rich_text": [{"plain_text": body}]}}
    assert build._carries_access(props("door code: 5123"))
    assert build._carries_access(props("∙ Alarm Code: 4509"))
    assert not build._carries_access(props("You're all set for Tuesday."))
    assert not build._carries_access(props(""))
    assert not build._carries_access({})


def test_returning_access_verifies_and_is_held_to_the_bc_margin():
    """2026-09-11. A Sent `Returning Access …` row is proof of access.

    Best O. / 527 / 2026-09-11 16:00 and Anita Shack / 901 / 11:00 both drew a
    false "🔔 HTA not sent" from the Gap audit on 2026-09-10, hours AFTER their
    codes had gone out. A returning renter keeps permanent codes, so the sweep
    sends the short `RA-sweep-…` text instead of a second walkthrough (the
    xavier rule, 2026-09-06). That row carries no Template and a code starting
    with neither HTA nor BC, so the filter never fetched it: the watchdog read
    `missing`, alarmed, and never wrote the `Booking` relation the board's
    `HTA Verified` rollup counts. Same failure class as the BC gap.

    The BC safety margin applies unchanged — Sent, and codes in the body.
    """
    import datetime
    def mk(code, status, body="Door Code: 5123\nAlarm Code: 4463",
           receipt="phonecom:260086375"):
        return {"id": code, "properties": {
            "Message Code": {"type": "title", "title": [{"plain_text": code}]},
            "Message Body": {"type": "rich_text",
                             "rich_text": [{"plain_text": body}]},
            "Status": {"type": "status", "status": {"name": status}},
            "Artist": {"type": "relation", "relation": []},
            "Studio": {"type": "select", "select": None},
            "Booking": {"type": "relation", "relation": []},
            "Dispatch Receipt": {"type": "rich_text", "rich_text": (
                [{"plain_text": receipt}] if receipt else [])},
            "Send After": {"type": "date", "date": None},
            "Sent At": {"type": "date", "date": None},
            "Created time": {"type": "created_time", "created_time": "2026-09-06T00:00:00.000Z"},
        }}
    rows = [
        mk("Returning Access 527 RA-sweep-0911-1600-527", "Sent"),
        mk("Returning Access 901 RA-sweep-0911-1100-901", "Pending Review"),
        mk("Returning Access 693 RA-sweep-0911-1800-693", "Sent",
           body="For your upcoming booking, see you Thursday."),
        # Sent, codes in the body, but the provider never acknowledged it.
        # Status alone is a promise; the receipt is the proof.
        mk("Returning Access 509B RA-sweep-0911-1900-509B", "Sent", receipt=""),
    ]
    orig = build._notion_query; build._notion_query = lambda t, d, b: rows
    try:
        out = build.fetch_hta_rows("t", datetime.datetime(2026, 9, 6, tzinfo=datetime.timezone.utc))
    finally:
        build._notion_query = orig
    assert [r["id"] for r in out] == ["Returning Access 527 RA-sweep-0911-1600-527"]
    assert out[0]["shape"] == "RA"

    # …and the surviving row verifies the booking, so no alarm is raised.
    ra = row("Sent", studio="527", rid="Returning Access 527 RA-sweep-0911-1600-527",
             sent_at="2026-09-06T01:00:00.000Z")
    v = verdict(booking(studio="527"), [ra])
    assert v["state"] == "verified"


def test_returning_access_is_in_the_filter_at_two_levels():
    seen = {}

    def fake_query(token, ds, payload):
        seen["filter"] = payload["filter"]
        return []

    real = build._notion_query
    build._notion_query = fake_query
    try:
        build.fetch_hta_rows("tok", NOW)
    finally:
        build._notion_query = real
    clauses = seen["filter"]["and"][1]["or"]
    assert {"property": "Message Code",
            "title": {"starts_with": "Returning Access"}} in clauses
    assert not any("and" in c or "or" in c for c in clauses), "no third level"
    assert build._filter_depth(seen["filter"]) <= 2


def test_filter_depth_counts_like_notion():
    leaf = {"property": "x", "title": {"starts_with": "a"}}
    assert build._filter_depth(leaf) == 0
    assert build._filter_depth({"and": [leaf]}) == 1
    assert build._filter_depth({"and": [leaf, {"or": [leaf]}]}) == 2
    assert build._filter_depth({"and": [{"or": [leaf, {"and": [leaf]}]}]}) == 3


def test_returning_access_query_and_verdict():
    """Replay the query boundary that hid Anita's delivered reminder."""
    now = datetime(2026, 9, 10, 17, 2, tzinfo=TZ)
    code = "Returning Access 901 RA-sweep-0911-1100-901"
    def prop(kind, value):
        if kind in ("title", "rich_text"):
            return {"type": kind, kind: [{"plain_text": value}]}
        return {"type": kind, kind: value}
    p = {
        "Message Code": prop("title", code),
        "Message Body": prop("rich_text", "Use the same Door Code: [redacted]"),
        "Status": prop("status", {"name": "Sent"}),
        "Artist": prop("relation", [{"id": "artistanita"}]),
        "Studio": prop("select", {"name": "901"}),
        "Sent At": prop("date", {"start": "2026-09-10T13:00:00Z"}),
        "Dispatch Receipt": prop("rich_text", "phonecom:fixture"),
    }
    raw = {"id": "ra-anita", "created_time": "2026-09-10T04:02:08Z", "properties": p}
    def query(token, ds, payload):
        clauses = payload["filter"]["and"][1]["or"]
        selected = any(code.startswith(c.get("title", {}).get("starts_with", "\0"))
                       for c in clauses)
        return [raw] if selected else []
    original = build._notion_query
    build._notion_query = query
    try:
        b = booking(artist="artistanita", studio="901", date="2026-09-11",
                    start="11:00", tier="Monitor")
        rows = build.fetch_hta_rows("fixture", now - timedelta(days=7))
        assert len(rows) == 1, "Returning Access is invisible to the query"
        assert verdict(b, rows, now)["state"] == "verified"
        # A repeat-renter reminder does not replace a first-visit walkthrough.
        assert verdict(dict(b, tier="FBS"), rows, now)["state"] == "missing"
        assert verdict(dict(b, studio="527"), rows, now)["state"] == "missing"
        assert verdict(dict(b, artist="other"), rows, now)["state"] == "missing"
        assert verdict(dict(b, date="2026-09-20"), rows, now)["state"] == "missing"
        for status in ("Pending Review", "Ready to Send", "Error", "Will Not Send"):
            p["Status"] = prop("status", {"name": status})
            assert build.fetch_hta_rows("fixture", now) == []
        p["Status"] = prop("status", {"name": "Sent"})
        p["Dispatch Receipt"] = prop("rich_text", "")
        assert build.fetch_hta_rows("fixture", now) == []
        p["Dispatch Receipt"] = prop("rich_text", "phonecom:fixture")
        p["Message Body"] = prop("rich_text", "Your booking is confirmed.")
        assert build.fetch_hta_rows("fixture", now) == []
    finally:
        build._notion_query = original


def test_self_serve_is_outside_hta_watch():
    original = build._notion_query
    build._notion_query = lambda *args: [{"id": "anita", "properties": {
        "Type of Booking": {"type": "select", "select": {"name": "Self Serve"}},
        "Booking Status": {"type": "select", "select": {"name": "Upcoming"}},
    }}]
    try:
        assert build.fetch_hta_watch_bookings("fixture", NOW.date()) == []
    finally:
        build._notion_query = original


if __name__ == "__main__":
    main()
