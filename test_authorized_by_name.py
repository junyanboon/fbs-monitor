"""A booking with no FBS row still knows its account's authorized users.

2026-09-25: Avante Dance Company Inc.'s one-off "Extra practice - Fuerza team"
in 693 was keyed in and armed by Ishfaaq Jookhun, an authorized user on the
account, and rendered ⚠ "not the expected person". Only FBS rows carried the
Artist page link, so the authorized-users read never ran for it.
"""
import build

AVANTE = "11a186b30d044d3b9c91b4bb828298a2"


def _row(pid, name, company=None):
    p = {"Name": {"type": "title", "title": [{"plain_text": name}]}}
    if company:
        p["Company"] = {"type": "rich_text", "rich_text": [{"plain_text": company}]}
    return {"id": pid, "properties": p}


def test_account_key_folds_title_to_booker():
    assert build._account_key("Avante Dance  Company Inc. — Extra practice - Fuerza team") \
        == "avante dance company"
    assert build._account_key("Avante Dance Company") == "avante dance company"
    assert build._account_key("Jane Doe [Flex Option]") == "jane doe"


def test_unjoined_booking_gets_allowed_names_by_name():
    e = {"kind": "booking", "who": "Avante Dance Company Inc. — Extra practice - Fuerza team"}
    lookup = {AVANTE: ["Claudia Dzierbicki, Avante Dance Company [Fixed Option]",
                       "Avante Dance Company", "Ishfaaq Jookhun"]}
    out = build.fetch_authorized_names(None, [e], lookup=lookup,
                                       by_name={"avante dance company": [AVANTE]})
    assert "Ishfaaq Jookhun" in out[0]["_allowed_names"]


def test_joined_booking_keeps_its_own_artist():
    e = {"kind": "booking", "who": "Avante Dance Company Inc.", "_artist_id": "x"}
    out = build.fetch_authorized_names(None, [e], lookup={"x": ["X"], AVANTE: ["Y"]},
                                       by_name={})
    assert out[0]["_allowed_names"] == ["X"]


def test_find_artists_by_name_exact_only(monkeypatch):
    rows = [_row("a-1", "Claudia Dzierbicki, Avante Dance Company [Fixed Option]",
                 "Avante Dance Company"),
            _row("b-2", "Someone, Avante Dance Company Juniors", "Avante Dance Company Juniors")]
    monkeypatch.setattr(build, "_notion_query", lambda *a, **k: rows)
    assert build.find_artists_by_name("t", ["avante dance company"]) == \
        {"avante dance company": ["a1"]}


def test_find_artists_by_name_common_name_matches_nothing(monkeypatch):
    rows = [_row(f"id{i}", "Alex Kim") for i in range(4)]
    monkeypatch.setattr(build, "_notion_query", lambda *a, **k: rows)
    assert build.find_artists_by_name("t", ["alex kim"]) == {"alex kim": []}


def test_find_artists_by_name_soft_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("notion down")
    monkeypatch.setattr(build, "_notion_query", boom)
    assert build.find_artists_by_name("t", ["avante dance company"]) == \
        {"avante dance company": []}
