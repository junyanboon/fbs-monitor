"""Per-booking AVA/EOB dispatch pills.

Only reduced queue state may cross onto the public board: message kind, a small
state vocabulary, and a Toronto-local time. Bodies, recipients, queue ids and
Notion relation ids stay internal.
"""

from pathlib import Path
from datetime import date
import json
import re
import subprocess

import build


def booking(*, ava="Scheduled", eob="Scheduled"):
    return {
        "kind": "booking",
        "who": "Kosi Eze",
        "studio": "693",
        "tier": "FBS",
        "gtg": True,
        "hta": "Sent",
        "_artist_id": "artist-kosi",
        "_ava_status": ava,
        "_eob_status": eob,
        "start": 18.5,
        "end": 24.0,
    }


def message(kind, status, *, send_after=None, sent_at=None, created="2026-08-30T10:00:00Z"):
    return {
        "kind": kind,
        "status": status,
        "studio": "693",
        "artist": "artist-kosi",
        "send_after": send_after,
        "sent_at": sent_at,
        "created": created,
    }


def dispatch(events, rows):
    return build.apply_message_dispatch(events, rows, date(2026, 8, 30))[0]["dispatch"]


def test_scheduled_queue_rows_publish_toronto_times_only():
    pills = dispatch([booking()], [
        message("AVA", "Ready to Send", send_after="2026-08-30T20:30:00Z"),
        message("EOB", "Pending Review", send_after="2026-08-31T03:45:00Z"),
    ])
    assert pills == [
        {"kind": "AVA", "state": "scheduled", "time": "16:30"},
        {"kind": "EOB", "state": "scheduled", "time": "23:45"},
    ]
    assert "artist-kosi" not in repr(pills)


def test_existing_row_without_time_is_queued_not_missing():
    pills = dispatch([booking(eob="Will Not Send")], [
        message("AVA", "Pending Review"),
    ])
    assert pills == [{"kind": "AVA", "state": "queued", "time": None}]


def test_expected_row_that_does_not_exist_is_missing():
    pills = dispatch([booking()], [
        message("EOB", "Ready to Send", send_after="2026-08-31T03:45:00Z"),
    ])
    assert pills == [
        {"kind": "AVA", "state": "missing", "time": None},
        {"kind": "EOB", "state": "scheduled", "time": "23:45"},
    ]


def test_extended_booking_does_not_call_its_sent_message_missing():
    """Stella Dada, 2026-09-04, Studio 509A — the regression this guards.

    She booked 12:00–13:00 and asked to extend to 13:30 at 12:31, mid-session.
    `EOB-sweep-0904-1200-509A` had already been scheduled off the original end
    and went out on time at 12:45 (`Sent`, 16:45Z). The card's end had become
    13:30, so the expected EOB moved to 13:15 — a 30-minute gap against the
    5-minute join window. The sent row fell out of the match and the board
    published MISSING for a message the renter had already received.
    """
    extended = booking()
    extended["studio"] = "509A"
    extended["tier"] = "Monitor"
    extended["start"], extended["end"] = 12.0, 13.5

    sent_before_the_extension = message(
        "EOB", "Sent",
        send_after="2026-09-04T16:45:00Z", sent_at="2026-09-04T16:45:00Z")
    sent_before_the_extension["studio"] = "509A"

    out = build.apply_message_dispatch(
        [extended], [sent_before_the_extension], date(2026, 9, 4))
    assert not any(p["kind"] == "EOB" for p in out[0]["dispatch"])


def test_terminal_row_suppresses_missing_only_when_nothing_else_joins():
    """A finished row must not mute a pill the queue still owes.

    An `Error` row replaced by a live one is the ordinary case: the replacement
    joins, so the terminal row never gets to speak for the booking.
    """
    pills = dispatch([booking()], [
        message("AVA", "Error", created="2026-08-30T08:00:00Z"),
        message("AVA", "Ready to Send", send_after="2026-08-30T20:30:00Z",
                created="2026-08-30T09:00:00Z"),
    ])
    assert {"kind": "AVA", "state": "scheduled", "time": "16:30"} in pills


def test_missing_still_fires_when_no_row_of_that_kind_ever_finished():
    """The alarm has to survive the fix — a live row that misses its slot by
    half an hour is a real scheduling fault, not a moved booking."""
    pills = dispatch([booking()], [
        message("AVA", "Ready to Send", send_after="2026-08-30T21:00:00Z"),
        message("EOB", "Ready to Send", send_after="2026-08-31T03:45:00Z"),
    ])
    assert {"kind": "AVA", "state": "missing", "time": None} in pills


def test_will_not_send_is_intentional_absence_not_missing():
    assert dispatch([booking(ava="Will Not Send", eob="Will Not Send")], []) == []


def test_terminal_rows_do_not_render_queue_pills():
    pills = dispatch([booking()], [
        message("AVA", "Sent", sent_at="2026-08-30T15:02:00Z"),
        message("EOB", "Error"),
    ])
    assert pills == []


def test_missing_artist_relation_is_unknown_not_missing():
    event = booking()
    event["_artist_id"] = None
    assert dispatch([event], []) == []


def test_public_projection_contains_no_private_join_fields():
    event = booking()
    build.apply_message_dispatch(
        [event], [message("AVA", "Pending Review")], date(2026, 8, 30))
    public = build.prepare_board_events([event])[0]
    assert public["dispatch"] == [{"kind": "AVA", "state": "queued", "time": None},
                                  {"kind": "EOB", "state": "missing", "time": None}]
    serialized = repr(public)
    assert "artist-kosi" not in serialized
    assert "send_after" not in serialized
    assert "sent_at" not in serialized


def test_newest_duplicate_replacement_wins_over_stale_error():
    pills = dispatch([booking()], [
        message("AVA", "Error", created="2026-08-30T08:00:00Z"),
        message("AVA", "Pending Review", created="2026-08-30T09:00:00Z"),
    ])
    assert pills[0] == {"kind": "AVA", "state": "queued", "time": None}


def test_timed_rows_attach_to_their_exact_booking_only():
    early = booking()
    early["start"], early["end"] = 14.0, 17.0
    late = booking()
    late["start"], late["end"] = 19.75, 20.5
    rows = [
        message("AVA", "Ready to Send", send_after="2026-08-30T16:00:00Z"),
        message("EOB", "Ready to Send", send_after="2026-08-30T20:45:00Z"),
    ]
    out = build.apply_message_dispatch([early, late], rows, date(2026, 8, 30))
    assert out[0]["dispatch"] == [
        {"kind": "AVA", "state": "scheduled", "time": "12:00"},
        {"kind": "EOB", "state": "scheduled", "time": "16:45"},
    ]
    assert out[1]["dispatch"] == [
        {"kind": "AVA", "state": "missing", "time": None},
        {"kind": "EOB", "state": "missing", "time": None},
    ]


def test_untimed_row_is_not_guessed_between_two_bookings():
    early, late = booking(), booking()
    early["start"], early["end"] = 14.0, 17.0
    late["start"], late["end"] = 19.75, 20.5
    out = build.apply_message_dispatch(
        [early, late], [message("AVA", "Pending Review")], date(2026, 8, 30))
    assert all(not any(p["kind"] == "AVA" for p in event["dispatch"]) for event in out)


def test_only_fbs_and_monitor_rows_receive_dispatch_pills():
    plain = booking()
    plain["tier"] = None
    viewing = booking()
    viewing["tier"] = "Viewing"
    rows = [message("AVA", "Pending Review"), message("EOB", "Pending Review")]
    out = build.apply_message_dispatch([plain, viewing], rows, date(2026, 8, 30))
    assert all(e.get("dispatch") == [] for e in out)


def test_templates_render_the_approved_right_side_cluster_and_missing_state():
    root = Path(__file__).parent
    for template in ("template.html", "template-mobile.html"):
        text = (root / template).read_text()
        assert "dispatch-cluster" in text
        assert "MISSING" in text


def test_no_gtg_renders_beside_the_booking_type_not_in_the_warning_column():
    root = Path(__file__).parent
    desktop = (root / "template.html").read_text()
    mobile = (root / "template-mobile.html").read_text()

    assert '<span class="tags">${tag}${gtgChip}${verdictChip(verdictOf(e,n))}</span>' in desktop
    assert ('<div class="who"><span class="nm">${e.facilitator?esc(e.facilitator):shortWho(e.who)}</span>'
            '${typeChip}${gtgChip}${verdictChip(verdictOf(e,n))}</div>') in mobile
    assert 'warns += `<span class="chip ${live?"crit":"watch"}">No GTG</span>`' not in desktop
    assert 't+=`<span class="chip ${st.live?"crit":"watch"}">No GTG</span>`' not in mobile


def test_no_gtg_visibility_rules_match_on_desktop_and_mobile():
    cases = [
        ({"tier": "FBS", "gtg": False, "heard": False}, {"live": False, "done": False}, "watch"),
        ({"tier": "Monitor", "gtg": False, "heard": False}, {"live": True, "done": False}, "crit"),
        ({"tier": "FBS", "gtg": True, "heard": False}, {"live": False, "done": False}, None),
        ({"tier": "FBS", "gtg": False, "heard": True}, {"live": False, "done": False}, None),
        ({"tier": "Viewing", "gtg": False, "heard": False}, {"live": False, "done": False}, None),
        ({"tier": None, "gtg": False, "heard": False}, {"live": False, "done": False}, None),
        ({"tier": "FBS", "gtg": False, "heard": False}, {"live": False, "done": True}, None),
    ]
    payload = json.dumps([{"event": event, "state": state} for event, state, _ in cases])

    for template in ("template.html", "template-mobile.html"):
        text = (Path(__file__).parent / template).read_text()
        helper = re.search(r"function noGtgChip\(e,st\)\{.*?\n\}", text, re.S)
        assert helper, f"{template} must expose the pure No GTG renderer"
        script = helper.group(0) + f"\nconst cases={payload};\n" + (
            "console.log(JSON.stringify(cases.map(c=>noGtgChip(c.event,c.state))));")
        result = subprocess.run(
            ["node", "-e", script], check=True, capture_output=True, text=True)
        rendered = json.loads(result.stdout)
        for html, (_, _, expected_class) in zip(rendered, cases):
            assert html.count("No GTG") == (1 if expected_class else 0)
            if expected_class:
                assert f'class="chip {expected_class}"' in html


def test_legacy_host_rows_without_template_still_have_a_dispatch_kind():
    assert build._dispatch_kind({
        "Template": {"type": "select", "select": None},
        "Message Code": {"type": "title", "title": [{"plain_text": "AVA-sweep-0830-1400-527"}]},
    }) == "AVA"
    assert build._dispatch_kind({
        "Message Code": {"type": "title", "title": [{"plain_text": "EOB-sweep-0830-1400-527"}]},
    }) == "EOB"


def main():
    test_scheduled_queue_rows_publish_toronto_times_only()
    test_existing_row_without_time_is_queued_not_missing()
    test_expected_row_that_does_not_exist_is_missing()
    test_extended_booking_does_not_call_its_sent_message_missing()
    test_terminal_row_suppresses_missing_only_when_nothing_else_joins()
    test_missing_still_fires_when_no_row_of_that_kind_ever_finished()
    test_will_not_send_is_intentional_absence_not_missing()
    test_terminal_rows_do_not_render_queue_pills()
    test_missing_artist_relation_is_unknown_not_missing()
    test_public_projection_contains_no_private_join_fields()
    test_newest_duplicate_replacement_wins_over_stale_error()
    test_timed_rows_attach_to_their_exact_booking_only()
    test_untimed_row_is_not_guessed_between_two_bookings()
    test_only_fbs_and_monitor_rows_receive_dispatch_pills()
    test_templates_render_the_approved_right_side_cluster_and_missing_state()
    test_no_gtg_renders_beside_the_booking_type_not_in_the_warning_column()
    test_no_gtg_visibility_rules_match_on_desktop_and_mobile()
    test_legacy_host_rows_without_template_still_have_a_dispatch_kind()
    print("message dispatch regression tests: OK")


if __name__ == "__main__":
    main()
