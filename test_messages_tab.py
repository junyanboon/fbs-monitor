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
            {"code": "SAL-REBECCA-PAID", "kind": None, "studio": "527", "_artist": "a1"}]
    out = build.label_messages(msgs, events)
    assert out[0]["label"] == "EOB for Rebecca Wise in Studio 527"
    assert out[1]["label"] == "AVA · Studio 509A", "unknown renter: no name invented"
    assert "label" not in out[2], "an unknown lane prefix keeps its code"
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


def test_lane_labels_read_as_plain_english():
    L = build._lane_label
    assert L("Holding reply — Janet Zamora ••• [gmail-1a071f55683e2310]", None, None) \
        == "Pending Reply for Janet Zamora"
    assert L("Post-HTA go-ahead confirmation — Jia L. [Peerspace] — 509B [gtg:ffb5067d-42c1]",
             None, "509B") == "GTG Confirmation for Jia L. in Studio 509B"
    assert L("PBF — Harleen C — availability reply [responder:1a07208cf7b8d0c3]", None, "901") \
        == "PBF for Harleen C in Studio 901"
    assert L("Quick answer — Kaushik Boga Studio 527 mats and cleaning", "Kaushik Boga", "527") \
        == "Quick answer for Kaushik Boga in Studio 527 · mats and cleaning"
    assert L("Quick answer — Rebecca Wise Studio 527 cleaning supplies 2026-09-04", None, "527") \
        == "Quick answer for Rebecca Wise in Studio 527 · cleaning supplies"
    assert L("HTR acknowledgement — Kaushik Boga 2026-09-05", None, None) \
        == "Acknowledgement for Kaushik Boga"
    assert L("Courtesy — Rebecca Wise booking ack 2026-09-03", None, None) \
        == "Courtesy reply for Rebecca Wise"
    assert L("Booking extension confirmed (527 Sep 5) — Rebecca Wise [extender:380f0719]", None, "527") \
        == "Extension confirmed for Rebecca Wise in Studio 527"
    assert L("PLATFORM booking (gate 1) — Lauren Taylor Scott FBS via Tagvenue: 509B Sep 04 16:00", None, None) \
        == "Booking confirmation for Lauren Taylor Scott · Tagvenue"
    assert L("SAL-REBECCA-PAID-20260805", None, None) is None, "unknown prefix keeps its code"


def test_greeter_and_booking_change_read_as_dotted_labels():
    L = build._lane_label
    assert L("Greeter — Peerspace instant + phone ask — KerriAnn M. — 901 Fri Oct 16 6:00 PM–7:30 PM [greeter:1a07265fb123e3d4]",
             None, "901") == "Booking Confirmation (Platform) · KerriAnn M. · Oct 16 booking"
    assert L("Booking change — KerriAnn M. — Oct 23 alternative [responder:1a07244005bcc4e8]",
             None, "901") == "Booking Change Request · KerriAnn M. · Oct 23 alternative"


def test_label_messages_never_publishes_the_raw_code():
    msgs = [{"code": "Holding reply — X ••• [gmail-1]", "_raw_code": "Holding reply — X +1 416 555 0100 [gmail-1]",
             "kind": None, "studio": None, "_artist": None}]
    out = build.label_messages(msgs, [])
    assert "_raw_code" not in out[0] and "_artist" not in out[0]
    assert "416" not in out[0]["label"], "label goes through redact()"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
