"""The red dot: a renter's latest message newer than our latest reply."""
from datetime import datetime, timezone
import build

T = lambda h, m=0: datetime(2026, 9, 5, h, m, tzinfo=timezone.utc)


def _ev(aid="a1"):
    return {"kind": "booking", "tier": "FBS", "studio": "527", "_artist_id": aid}


def test_inbound_with_no_reply_is_unread():
    ev = _ev()
    build.apply_unread([ev], {"a1": T(14)}, {})
    assert ev["unread"] is True


def test_a_later_reply_clears_it():
    ev = _ev()
    build.apply_unread([ev], {"a1": T(14)}, {"a1": T(14, 5)})
    assert ev["unread"] is False


def test_an_earlier_reply_does_not_clear_a_newer_message():
    ev = _ev()
    build.apply_unread([ev], {"a1": T(15)}, {"a1": T(14, 5)})
    assert ev["unread"] is True


def test_no_artist_or_no_message_means_no_dot():
    ev1, ev2 = _ev(aid=None), _ev(aid="zz")
    build.apply_unread([ev1, ev2], {"a1": T(14)}, {})
    assert ev1["unread"] is False and ev2["unread"] is False


def test_fetch_reply_state_reads_both_directions_and_queue_sent_at(monkeypatch=None):
    calls = []
    def fake_query(token, ds, body):
        calls.append(ds)
        if ds == build.CORRESPONDENCE_DS:
            return [
                {"properties": {"Artist": {"type": "relation", "relation": [{"id": "a1"}]},
                                "Direction": {"type": "select", "select": {"name": "→ Us"}},
                                "Date & Time": {"type": "date", "date": {"start": "2026-09-05T14:00:00.000+00:00"}}}},
                {"properties": {"Artist": {"type": "relation", "relation": [{"id": "a1"}]},
                                "Direction": {"type": "select", "select": {"name": "→ Them"}},
                                "Date & Time": {"type": "date", "date": {"start": "2026-09-05T13:00:00.000+00:00"}}}},
            ]
        return [{"properties": {"Artist": {"type": "relation", "relation": [{"id": "a1"}]},
                                "Sent At": {"type": "date", "date": {"start": "2026-09-05T14:30:00.000+00:00"}}}}]
    orig = build._notion_query
    build._notion_query = fake_query
    try:
        last_in, last_out = build.fetch_reply_state("tok", T(5))
    finally:
        build._notion_query = orig
    assert last_in["a1"] == T(14)
    assert last_out["a1"] == T(14, 30), "queue Sent At beats the older log reply"
    assert set(calls) == {build.CORRESPONDENCE_DS, build.MESSAGES_DS}


def test_public_projection_carries_only_the_boolean():
    ev = dict(_ev(), who="R", gtg=True, hta="Sent", unread=True, dispatch=[],
              start=16.75, end=21.75)
    out = build.prepare_board_events([ev])[0]
    assert out["unread"] is True and isinstance(out["unread"], bool)
    assert "_artist_id" not in out, "the join key never publishes"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
