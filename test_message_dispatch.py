"""Per-booking AVA/EOB dispatch pills.

Only reduced queue state may cross onto the public board: message kind, a small
state vocabulary, and a Toronto-local time. Bodies, recipients, queue ids and
Notion relation ids stay internal.
"""

from pathlib import Path
from datetime import date

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


def main():
    test_scheduled_queue_rows_publish_toronto_times_only()
    test_existing_row_without_time_is_queued_not_missing()
    test_expected_row_that_does_not_exist_is_missing()
    test_will_not_send_is_intentional_absence_not_missing()
    test_terminal_rows_do_not_render_queue_pills()
    test_missing_artist_relation_is_unknown_not_missing()
    test_public_projection_contains_no_private_join_fields()
    test_newest_duplicate_replacement_wins_over_stale_error()
    test_timed_rows_attach_to_their_exact_booking_only()
    test_untimed_row_is_not_guessed_between_two_bookings()
    test_only_fbs_and_monitor_rows_receive_dispatch_pills()
    test_templates_render_the_approved_right_side_cluster_and_missing_state()
    print("message dispatch regression tests: OK")


if __name__ == "__main__":
    main()
