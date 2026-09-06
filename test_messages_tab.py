"""Messages tab labels and access-chip label. Pure logic — no network."""
import build


def test_daily_kind_matches_sweep_host_and_hta_shapes():
    assert build._daily_kind("EOB-sweep-0905-1645-527", None) == "EOB"
    assert build._daily_kind("AVA", None) == "AVA"
    assert build._daily_kind("HTA-sweep-0905-0900-527", "hta_studio_access") == "HTA"
    assert build._daily_kind("How to Access 527", None) == "HTA"
    assert build._daily_kind("Quick answer — Kaushik Boga", None) is None


def test_label_messages_names_the_renter_and_strips_the_join_key():
    events = [{"kind": "booking", "_artist_id": "a1",
               "who": "Rebecca Wise (Event Wise) — [Unpaid - Invoiced]"}]
    msgs = [{"code": "EOB", "kind": "EOB", "studio": "527", "_artist": "a1"},
            {"code": "AVA-sweep-•••", "kind": "AVA", "studio": "509A", "_artist": "zz"},
            {"code": "Quick answer — X", "kind": None, "studio": "527", "_artist": "a1"}]
    out = build.label_messages(msgs, events)
    assert out[0]["label"] == "EOB for Rebecca Wise in Studio 527"
    assert out[1]["label"] == "AVA · Studio 509A", "unknown renter: no name invented"
    assert "label" not in out[2], "non-daily rows keep their code"
    assert all("_artist" not in m for m in out), "artist id never publishes"


def test_access_label_is_lockbox_key_for_key_return_rows():
    ev = {"kind": "booking", "studio": "901", "who": "Tufan Bhattarai",
          "_artist_id": "t1", "start": 8.0, "end": 12.0}
    rows = [{"text": "Sep 5 after 12:00 — confirm both Studio 901 lockbox keys "
                     "were returned after Tufan Bhattarai", "artist": "t1"}]
    import datetime
    build.flag_access_gaps([ev], rows, datetime.date(2026, 9, 5))
    assert ev["access_gap"] is True and ev["access_label"] == "Lockbox key"
    ev2 = dict(ev); ev2.pop("access_label", None)
    build.flag_access_gaps([ev2], [{"text": "no Alarm.com user", "artist": "t1"}],
                           datetime.date(2026, 9, 5))
    assert ev2["access_label"] == "Verify access"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
