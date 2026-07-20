#!/usr/bin/env python3
"""
FBS Studio Monitor — deterministic cloud rebuild (GitHub Actions).

Pure rails, no AI. Fetches three cloud sources, builds the DATA JSON, splices it
into template.html and writes index.html. Commit/push is handled by the workflow.

Sources (all reuse the desk-correspondence secret conventions)
  1. Bookings + staff — Google Calendar secret ICS URLs
        ICS_URL_527 / ICS_URL_509A / ICS_URL_509B / ICS_URL_693 / ICS_URL_901 / ICS_URL_STAFF
  2. Tier / GTG / HTA — Notion API (NOTION_TOKEN), FBS AI Support board
  3. Arrivals / departures — Gmail API via OAuth (GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN),
        label "Artist Care - ADT" (TELUS Secure Business emails). Same OAuth pattern as
        desk-correspondence/scripts/gmail_pull.py (scope gmail.readonly).

Fail-loud policy: any calendar or Notion source failure aborts nonzero and does NOT
write a fabricated/partial page. A revoked/expired Gmail refresh token also fails RED
(decision 023 — never skip-green after setup). Other Gmail failures soft-fall-back to
the Notion board's Armed/Disarmed columns and note it for the commit message.

The workflow gates on NOTION_TOKEN presence (skip-green until secrets exist), so once
build.py actually runs, missing/failing sources are real errors.
"""

import os
import re
import sys
import json
import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import icalendar
import recurring_ical_events

TZ = ZoneInfo("America/Toronto")

STUDIOS = [
    {"id": "509A", "name": "509A", "sub": "Main"},
    {"id": "509B", "name": "509B", "sub": "Main"},
    {"id": "527", "name": "527", "sub": "Loft"},
    {"id": "693", "name": "693", "sub": "Annex"},
    {"id": "901", "name": "901", "sub": "Elements"},
]
STUDIO_IDS = {s["id"] for s in STUDIOS}

NOTION_DATA_SOURCE = "36475032-81c4-80d6-b18a-000b8d6f9421"

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
OUTPUT = os.path.join(HERE, "index.html")

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
ARM_LABEL = "Artist Care - ADT"


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def emit_fallback_note(note):
    """Surface a Gmail-fallback note to the workflow's commit step."""
    print(f"NOTE: {note}")
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a") as fh:
            fh.write(f"FALLBACK_NOTE={note}\n")


def decimal_hours(dt, base_day):
    """Local clock hours from the window's base day; +24 per day past it.
    e.g. 2:15 AM the next day → 26.25."""
    if isinstance(dt, datetime) and dt.tzinfo:
        dt = dt.astimezone(TZ)
    delta_days = (dt.date() - base_day).days
    return delta_days * 24 + dt.hour + dt.minute / 60


def norm_hm(val):
    """Normalize an arm/disarm time to 24h 'HH:MM'. Accepts '13:18', '3:59 PM'."""
    if not val:
        return None
    val = str(val).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*([AaPp][Mm])$", val)
    if m:
        h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mn:02d}"
    m = re.match(r"^(\d{1,2}):(\d{2})$", val)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    return None


def _strip_paren_groups(t, starters):
    """Remove balanced (…) groups whose content starts with one of `starters` —
    regex can't handle nesting like '(Studio 901 (Elements))'."""
    out, i = [], 0
    while i < len(t):
        if t[i] == "(":
            j, depth = i + 1, 1
            while j < len(t) and depth:
                depth += {"(": 1, ")": -1}.get(t[j], 0)
                j += 1
            inner = t[i + 1:j - 1].strip().lower()
            if depth == 0 and any(inner.startswith(s) for s in starters):
                i = j
                continue
        out.append(t[i])
        i += 1
    return "".join(out)


def clean_who(title):
    t = title or ""
    t = re.sub(r"\*moved from[^*]*\*", "", t, flags=re.I)
    t = _strip_paren_groups(t, ("fixed option", "aap", "studio"))
    t = re.sub(r"\[(?:Un)?Paid\]", "", t, flags=re.I)
    t = re.sub(r"\bBooking Extension Request\b", "", t, flags=re.I)
    t = re.sub(r"moved from\s+\S+", "", t, flags=re.I)   # bare form: "moved from 9:30am"
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s+#?\d+/\d+\b", "", t)          # session counters "2/8"
    t = re.sub(r"\s{2,}", " ", t)
    t = t.strip(" -–—:)(")
    # "Name: Description" → "Name — Description"; drop the rhs when it just
    # repeats the name ("Desiree Joy: Desiree Joy", "X (Org): Org").
    if ":" in t:
        lhs, rhs = (s.strip(" -–—:") for s in t.split(":", 1))
        t = lhs if (not rhs or rhs.lower() in lhs.lower()) else f"{lhs} — {rhs}"
    return t


# ─────────────────────────────────────────────────────────────────────────────
# 1. Bookings + staff — Google Calendar secret ICS
# ─────────────────────────────────────────────────────────────────────────────
def ics_map_from_env():
    """Build {studio_or_Staff: url} from the ICS_URL_* secrets."""
    m = {}
    for sid in STUDIO_IDS:
        url = os.environ.get(f"ICS_URL_{sid}")
        if url:
            m[sid] = url
    staff = os.environ.get("ICS_URL_STAFF")
    if staff:
        m["Staff"] = staff
    return m


def fetch_ics(url, win_start, win_end):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        cal = icalendar.Calendar.from_ical(r.text)
        occ = recurring_ical_events.of(cal).between(win_start, win_end)
    except Exception as e:  # noqa: BLE001
        die(f"ICS fetch/parse failed for {url[:60]}…: {e}")
    out = []
    for ev in occ:
        summary = str(ev.get("SUMMARY") or "")
        try:
            dts = ev.get("DTSTART").dt
            dte = ev.get("DTEND").dt if ev.get("DTEND") else dts
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(dts, datetime):     # all-day → skip (unavailable-style block)
            continue
        status = str(ev.get("STATUS") or "").upper()
        out.append({
            "summary": summary,
            "cancelled": status == "CANCELLED",
            "dtstart": dts.astimezone(TZ),
            "dtend": dte.astimezone(TZ),
        })
    return out


CLEANERS = ("stefan", "donny", "ela")


def is_cleaning(summary):
    s = summary.lower()
    return "clean" in s and any(c in s for c in CLEANERS)


def build_calendar_events(ics_map, win_start, win_end, base_day):
    events, staff = [], []
    for key, url in ics_map.items():
        occ = fetch_ics(url, win_start, win_end)
        is_staff = key == "Staff"
        for ev in occ:
            summary = ev["summary"]
            if ev.get("cancelled") or "unavailable" in summary.lower():
                continue
            if is_staff:
                s = parse_staff_row(summary, ev["dtstart"], ev["dtend"], base_day)
                if s:
                    staff.append(s)
                continue
            events.append({
                "studio": key,
                "who": clean_who(summary),
                "kind": "cleaning" if is_cleaning(summary) else "booking",
                "start": decimal_hours(ev["dtstart"], base_day),
                "end": decimal_hours(ev["dtend"], base_day),
                "tier": None, "gtg": True, "hta": None,
                "arrived": None, "departed": None,
            })
    return merge_events(events), staff


def parse_staff_row(summary, dtstart, dtend, base_day):
    low = summary.lower().strip()
    if low.startswith("need ") or "meeting" in low or "payroll" in low or "ela morning" in low:
        return None
    start = decimal_hours(dtstart, base_day)
    end = decimal_hours(dtend, base_day)
    if "open the studio" in low:
        return {"name": "Staff", "role": "Open", "start": start, "end": end}
    if "close the studio" in low:
        return {"name": "Staff", "role": "Close", "start": start, "end": end}
    m = re.search(r"^\s*([A-Za-z][A-Za-z'’-]*)\s+.*?\b(FBS|Monitoring|Monitor|Viewing)\b", summary, re.I)
    if m:
        role = m.group(2)
        role = {"monitor": "Monitoring"}.get(role.lower(), role[0].upper() + role[1:])
        return {"name": m.group(1), "role": role, "start": start, "end": end}
    return None


def merge_events(events):
    """Merge same-renter contiguous / cross-midnight blocks per studio."""
    by_studio = {}
    for e in events:
        by_studio.setdefault(e["studio"], []).append(e)
    merged = []
    for evs in by_studio.values():
        evs.sort(key=lambda x: x["start"])
        cur = None
        for e in evs:
            same = cur and cur["kind"] == e["kind"] and cur["who"] \
                and _renter_key(cur["who"]) == _renter_key(e["who"])
            contiguous = cur and e["start"] <= cur["end"] + 1e-6
            if cur and same and contiguous:
                cur["end"] = max(cur["end"], e["end"])
                if len(e["who"]) > len(cur["who"]):   # keep the more descriptive title
                    cur["who"] = e["who"]
            else:
                if cur:
                    merged.append(cur)
                cur = dict(e)
        if cur:
            merged.append(cur)
    merged.sort(key=lambda x: (x["studio"], x["start"]))
    return _dedupe_same_slot(merged)


def _dedupe_same_slot(events):
    """A studio can only hold one booking at a time, so two 'booking' events in
    the same studio with identical start/end are the same booking under two
    titles (seen with Peerspace: the synced 'Peerspace Booking, <First> <L>.'
    event plus a manually created descriptive event). _renter_key can't link
    them — the names share nothing — so dedupe on studio + exact time slot,
    keeping the more descriptive title."""
    out = []
    for e in events:
        dup = next((o for o in out
                    if o["studio"] == e["studio"] and o["kind"] == e["kind"] == "booking"
                    and abs(o["start"] - e["start"]) < 1e-6
                    and abs(o["end"] - e["end"]) < 1e-6), None)
        if dup:
            if len(e["who"] or "") > len(dup["who"] or ""):
                dup["who"] = e["who"]
        else:
            out.append(e)
    return out


def _renter_key(who):
    """Merge key: the renter's name, not the whole title — an extension event or a
    cross-midnight second event carries extra description ('… — Kizomba social',
    '… additional time at no charge') that must not break the merge."""
    name = re.split(r"\s+—\s+", who or "")[0]
    toks = re.findall(r"[a-z]+", name.lower())
    return "".join(toks[:2])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tier / GTG / HTA / board Armed-Disarmed — Notion API
# ─────────────────────────────────────────────────────────────────────────────
def _prop_text(prop):
    if prop is None:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if v is None:
        return None
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v).strip() or None
    if t in ("select", "status"):
        return v.get("name")
    if t == "date":
        return v.get("start")
    if t == "checkbox":
        return "Yes" if v else "No"
    if t == "number":
        return v
    if t == "formula":
        inner = v.get("type")
        return _prop_text({"type": inner, inner: v.get(inner)})
    if isinstance(v, str):
        return v
    return None


def fetch_notion_rows(token, today_iso):
    body = {
        "filter": {"property": "Booking Date", "date": {"equals": today_iso}},
        "page_size": 100,
    }
    endpoints = [
        (f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE}/query", "2025-09-03"),
        (f"https://api.notion.com/v1/databases/{NOTION_DATA_SOURCE}/query", "2022-06-28"),
    ]
    last_err = None
    for url, ver in endpoints:
        try:
            h = {"Authorization": f"Bearer {token}", "Notion-Version": ver,
                 "Content-Type": "application/json"}
            rows, cursor, ok = [], None, True
            while True:
                b = dict(body)
                if cursor:
                    b["start_cursor"] = cursor
                r = requests.post(url, headers=h, json=b, timeout=30)
                if r.status_code != 200:
                    last_err = f"{r.status_code} {r.text[:200]}"
                    ok = False
                    break
                data = r.json()
                rows.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
            if ok:
                return rows
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    die(f"Notion query failed: {last_err}")


def parse_notion(rows):
    out = []
    for row in rows:
        p = row.get("properties", {})
        status = (_prop_text(p.get("Booking Status")) or "").lower()
        if "cancel" in status or "missed" in status:
            continue
        studio = re.sub(r"\s*\(.*\)\s*", "", (_prop_text(p.get("Studio")) or "")).strip()
        tob = (_prop_text(p.get("Type of Booking")) or "").strip()
        tier = {"fbs": "FBS", "monitor only": "Monitor",
                "studio viewing": "Viewing"}.get(tob.lower())
        gtg = (_prop_text(p.get("GTG")) or "").strip().lower() == "yes"
        out.append({
            "studio": studio,
            "start": _prop_text(p.get("Start Time")),
            "tier": tier,
            "gtg": gtg if tier else True,
            "hta": _prop_text(p.get("HTA")),
            "board_disarmed": norm_hm(_prop_text(p.get("Disarmed"))),
            "board_armed": norm_hm(_prop_text(p.get("Armed"))),
        })
    return out


def _time_to_decimal(val):
    hm = norm_hm(val)
    if not hm:
        return None
    h, m = hm.split(":")
    return int(h) + int(m) / 60


def join_notion(events, notion_rows):
    used = [False] * len(notion_rows)
    for e in events:
        best, best_i, best_gap = None, -1, 1e9
        for i, r in enumerate(notion_rows):
            if used[i] or r["studio"] != e["studio"]:
                continue
            rs = _time_to_decimal(r["start"])
            gap = abs((rs if rs is not None else e["start"]) - e["start"])
            if gap < best_gap:
                best, best_i, best_gap = r, i, gap
        if best and best_gap <= 2.0:
            used[best_i] = True
            e["tier"], e["gtg"], e["hta"] = best["tier"], best["gtg"], best["hta"]
            e["_board_disarmed"] = best["board_disarmed"]
            e["_board_armed"] = best["board_armed"]
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 3. Arrivals / departures — Gmail API (OAuth refresh token)
# ─────────────────────────────────────────────────────────────────────────────
RE_DISARM = re.compile(r"Studio\s+(\d+\w?)[^:]*:\s*Studio\s+(\d+\w?)\s+was\s+Disarmed\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
RE_ARM = re.compile(r"Studio\s+(\d+\w?)[^:]*:\s*Studio\s+(\d+\w?)\s+was\s+Armed\s+Away\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
RE_PANEL_DISARM = re.compile(r"Studio\s+(\d+\w?)\s+Panel\s+was\s+Disarmed\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
# Panel arm comes in two forms: named ("Panel was Armed Away by Himanshi Mehta at
# 5:12 PM" — 901's usual form; missing this dropped every 901 departure on 2026-07-19)
# and nameless with the name in trailing parens ("… Armed Away at 9:16 PM (Shiela)").
RE_PANEL_ARM_BY = re.compile(r"Studio\s+(\d+\w?)\s+Panel\s+was\s+Armed\s+Away\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
RE_PANEL_ARM = re.compile(r"Studio\s+(\d+\w?)[^:]*:.*?Panel\s+was\s+Armed\s+Away\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)\s*\((.+?)\)", re.I)
# Nameless panel disarm, name in parens ("Panel was Disarmed at 11:20 PM (info@danceannex.ca)").
RE_PANEL_DISARM_AT = re.compile(r"Studio\s+(\d+\w?)[^:]*:.*?Panel\s+was\s+Disarmed\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)\s*\((.+?)\)", re.I)
IGNORE = ("motion", "pending", "image", "alarm")
STAFF_REMOTE = "info@danceannex.ca"


def gmail_access_token():
    data = {
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }
    r = requests.post(TOKEN_URL, data=data, timeout=30)
    if r.status_code != 200:
        # revoked/expired refresh token = the silent-death risk → fail RED (decision 023)
        die(f"Gmail OAuth refresh failed ({r.status_code}): {r.text[:200]}\n"
            "If invalid_grant: refresh token revoked/expired — re-mint and update "
            "GMAIL_REFRESH_TOKEN.")
    return r.json()["access_token"]


def _resolve_label_id(h, name):
    """Gmail's label: search operator does NOT match quoted multi-word names
    ('label:"Artist Care - ADT"' returns 0), so we resolve to the exact label ID
    and filter with the labelIds param instead."""
    r = requests.get(f"{GMAIL_API}/labels", headers=h, timeout=30)
    if r.status_code in (401, 403):
        die(f"Gmail API {r.status_code} listing labels — check scope/consent.")
    r.raise_for_status()
    for lab in r.json().get("labels", []):
        if lab.get("name") == name:
            return lab["id"]
    die(f'Gmail label "{name}" not found on this account — arrivals source is misconfigured.')


def fetch_arm_events(win_start):
    """Return [{studio, name, time 'HH:MM', kind}]. Raises on soft (non-auth) failures."""
    tok = gmail_access_token()
    h = {"Authorization": f"Bearer {tok}"}
    label_id = _resolve_label_id(h, ARM_LABEL)
    after = int(win_start.timestamp())
    ids, page = [], None
    for _ in range(20):
        params = {"labelIds": label_id, "q": f"after:{after}", "maxResults": 100}
        if page:
            params["pageToken"] = page
        r = requests.get(f"{GMAIL_API}/messages", headers=h, params=params, timeout=30)
        if r.status_code in (401, 403):
            die(f"Gmail API {r.status_code} listing messages — check scope/consent.")
        r.raise_for_status()
        data = r.json()
        ids.extend(m["id"] for m in data.get("messages", []))
        page = data.get("nextPageToken")
        if not page:
            break
    floor_ms = int(win_start.timestamp()) * 1000
    out = []
    for mid in ids:
        r = requests.get(f"{GMAIL_API}/messages/{mid}", headers=h,
                         params={"format": "metadata", "metadataHeaders": "Subject"},
                         timeout=30)
        r.raise_for_status()
        msg = r.json()
        if int(msg.get("internalDate", "0")) < floor_ms:
            continue
        subject = ""
        for hdr in msg.get("payload", {}).get("headers", []):
            if hdr.get("name", "").lower() == "subject":
                subject = hdr.get("value", "")
                break
        parsed = parse_arm_subject(subject)
        if os.environ.get("DEBUG_ARM") == "1":
            print(f"ARM-DEBUG: {'PARSED ' + str(parsed) if parsed else 'DROPPED'} <- {subject!r}")
        if parsed:
            out.append(parsed)
    return out


def parse_arm_subject(subject):
    low = subject.lower()
    if any(k in low for k in IGNORE):
        return None
    m = RE_DISARM.search(subject)
    if m:
        return _arm_evt(m.group(2), m.group(3).strip(), m.group(4), "arrival")
    m = RE_ARM.search(subject)
    if m:
        return _arm_evt(m.group(2), m.group(3).strip(), m.group(4), "departure")
    m = RE_PANEL_DISARM.search(subject)
    if m:
        return _arm_evt(m.group(1), m.group(2).strip(), m.group(3), "arrival")
    m = RE_PANEL_ARM_BY.search(subject)
    if m:
        return _arm_evt(m.group(1), m.group(2).strip(), m.group(3), "departure")
    m = RE_PANEL_ARM.search(subject)
    if m:
        return _arm_evt(m.group(1), m.group(3).strip(), m.group(2), "departure")
    m = RE_PANEL_DISARM_AT.search(subject)
    if m:
        return _arm_evt(m.group(1), m.group(3).strip(), m.group(2), "arrival")
    return None


def norm_studio_label(raw):
    m = re.match(r"(\d+)([AB])?", (raw or "").strip())
    if not m:
        return None
    base = m.group(1) + (m.group(2) or "")
    return base if base in STUDIO_IDS else None


def _arm_evt(studio_raw, name, time_raw, kind):
    studio = norm_studio_label(studio_raw)
    if not studio:
        return None
    if STAFF_REMOTE in name.lower():
        return None  # staff remote → attribute to no renter
    return {"studio": studio, "name": name, "time": norm_hm(time_raw), "kind": kind}


def _name_match(who, arm_name):
    """Do the renter title and the ADT event name share a name token (≥3 chars)?"""
    a = set(t for t in re.findall(r"[a-z]{3,}", (who or "").lower()))
    b = set(t for t in re.findall(r"[a-z]{3,}", (arm_name or "").lower()))
    return bool(a & b)


def apply_arm_events(events, arm_events):
    """Two-pass match, studio + time window [start-60, end+90].

    Back-to-back bookings in one studio overlap windows, so pure nearest-time
    steals events across bookings (Desiree's 15:09 arm became Mia's departure;
    Laura's 11:04 disarm became Tufan's arrival). Pass 1 assigns each arm event
    to in-window bookings whose title shares a name token with the event's name
    and marks it claimed. Pass 2 gives still-unmatched bookings the unclaimed
    nameless-or-foreign events in their window (panel events often carry a staff
    or plus-one name — e.g. 'Shiela' closing out Quynh's booking).
    Earliest disarm = arrived, last arm = departed."""
    def in_window(e, t):
        return e["start"] - 1.0 <= t <= e["end"] + 1.5

    timed = []
    for a in arm_events:
        t = _time_to_decimal(a["time"]) if a["time"] else None
        if t is not None:
            timed.append({**a, "t": t, "claimed": False})

    # pass 1 — name-matched
    for e in events:
        arrivals, departures = [], []
        for a in timed:
            if a["studio"] == e["studio"] and in_window(e, a["t"]) \
                    and _name_match(e["who"], a["name"]):
                a["claimed"] = True
                (arrivals if a["kind"] == "arrival" else departures).append((a["t"], a["time"]))
        if arrivals:
            e["arrived"] = min(arrivals)[1]
        if departures:
            e["departed"] = max(departures)[1]

    # pass 2 — unclaimed events for still-unmatched bookings
    for e in events:
        if e["arrived"] and e["departed"]:
            continue
        arrivals, departures = [], []
        for a in timed:
            if a["claimed"] or a["studio"] != e["studio"] or not in_window(e, a["t"]):
                continue
            (arrivals if a["kind"] == "arrival" else departures).append((a["t"], a["time"]))
        if not e["arrived"] and arrivals:
            e["arrived"] = min(arrivals)[1]
        if not e["departed"] and departures:
            e["departed"] = max(departures)[1]

    # A departure that precedes the arrival is a mis-claimed neighbour's arm
    # (e.g. the main booking's 13:10 arm landing on its own 13:18 extension row).
    for e in events:
        arr, dep = _time_to_decimal(e.get("arrived")), _time_to_decimal(e.get("departed"))
        if arr is not None and dep is not None and dep < arr:
            e["departed"] = None
    return events


def apply_board_fallback(events):
    for e in events:
        if e.get("_board_disarmed"):
            e["arrived"] = e["_board_disarmed"]
        if e.get("_board_armed"):
            e["departed"] = e["_board_armed"]
    return events


# ─────────────────────────────────────────────────────────────────────────────
# assemble + splice
# ─────────────────────────────────────────────────────────────────────────────
def build_data(now):
    base_day = now.date() if now.hour >= 5 else (now - timedelta(days=1)).date()
    win_start = datetime.combine(base_day, datetime.min.time(), TZ).replace(hour=5)
    win_end = win_start + timedelta(days=1) - timedelta(minutes=1)

    ics_map = ics_map_from_env()
    if not any(k in STUDIO_IDS for k in ics_map):
        die("No ICS_URL_<studio> secrets set — cannot build bookings.")
    events, staff = build_calendar_events(ics_map, win_start, win_end, base_day)

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        die("NOTION_TOKEN missing.")
    events = join_notion(events, parse_notion(fetch_notion_rows(token, base_day.isoformat())))

    used_fallback = False
    if os.environ.get("GMAIL_REFRESH_TOKEN"):
        try:
            events = apply_arm_events(events, fetch_arm_events(win_start))
        except SystemExit:
            raise                       # auth failure already died RED
        except Exception as e:          # noqa: BLE001 — soft: fall back to board
            used_fallback = True
            emit_fallback_note(f"Gmail fetch failed ({e}); used board Armed/Disarmed fallback.")
            events = apply_board_fallback(events)
    else:
        used_fallback = True
        emit_fallback_note("GMAIL_* secrets missing; used board Armed/Disarmed fallback.")
        events = apply_board_fallback(events)

    clean = [{
        "studio": e["studio"], "who": e["who"], "kind": e["kind"],
        "tier": e["tier"], "gtg": e["gtg"], "hta": e["hta"],
        "arrived": e.get("arrived"), "departed": e.get("departed"),
        "start": round(e["start"], 4), "end": round(e["end"], 4),
    } for e in events]

    data = {
        "date": now.strftime("%A, %B %-d, %Y"),
        "generatedAt": now.strftime("%b %-d, %-I:%M %p ET"),
        "generatedAtISO": now.replace(microsecond=0).isoformat(),
        "studios": STUDIOS,
        "events": clean,
        "staff": sorted(staff, key=lambda s: s["start"]),
        "attention": [],
    }
    return data, used_fallback


def splice(data):
    tpl = open(TEMPLATE, encoding="utf-8").read()
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    out = re.sub(r"/\*__DATA__\*/.*?/\*__END_DATA__\*/",
                 lambda _: "/*__DATA__*/" + payload + "/*__END_DATA__*/",
                 tpl, count=1, flags=re.S)
    out = ('<!doctype html>\n<html lang="en">\n<head>\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<meta name="robots" content="noindex,nofollow">\n') + out
    i = out.index("</style>") + len("</style>")
    out = out[:i] + "\n</head>\n<body>" + out[i:] + "\n</body>\n</html>\n"
    return out


def main():
    now = datetime.now(TZ)
    force = os.environ.get("FORCE_BUILD") == "1"
    # Run 07:00–02:59 Toronto: bookings regularly cross midnight (e.g. socials
    # ending 02:15), so the board must keep updating arrivals/departures until
    # the last cross-midnight block is done. Quiet hours: 03:00–06:59 only.
    if not force and 3 <= now.hour < 7:
        print(f"Quiet hours 03:00–07:00 Toronto ({now:%H:%M}); skipping.")
        return
    data, fallback = build_data(now)
    open(OUTPUT, "w", encoding="utf-8").write(splice(data))
    n = len(data["events"])
    arrived = sum(1 for e in data["events"] if e["arrived"])
    print(f"Built index.html — {n} bookings, {arrived} with arrivals"
          + (" [board fallback]" if fallback else ""))


if __name__ == "__main__":
    main()
