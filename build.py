#!/usr/bin/env python3
"""
FBS Studio Monitor — deterministic cloud rebuild (GitHub Actions).

Pure rails, no AI. Fetches three cloud sources, builds the DATA JSON, splices it
into template.html and writes index.html. Commit/push is handled by the workflow.

Sources
  1. Bookings   — Google Calendar secret ICS URLs (ICS_URLS env, JSON map)
  2. Arrivals   — Gmail IMAP, label "Artist Care - ADT" (TELUS Secure Business emails)
  3. Tier/GTG   — Notion API, FBS AI Support board

Fail-loud policy: any calendar or Notion source failure aborts with a nonzero exit
and does NOT write a fabricated/partial page. IMAP failure is the one soft path —
we fall back to the Notion board's Armed/Disarmed columns and note it for the commit
message (writes FALLBACK_NOTE to $GITHUB_ENV / stdout).

Environment
  ICS_URLS            JSON: {"527":"https://…ical…/basic.ics", "509A":..., "Staff":...}
  NOTION_TOKEN        Notion internal integration token
  GMAIL_USER          default thedanceannex@gmail.com
  GMAIL_APP_PASSWORD  Gmail app password (16 chars, no spaces)
  FORCE_BUILD         "1" to bypass the 07:00–24:00 Toronto time gate (manual dispatch)
  GITHUB_ENV          (set by Actions) — we append FALLBACK_NOTE here for the commit step
"""

import os
import re
import sys
import json
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import icalendar
import recurring_ical_events

TZ = ZoneInfo("America/Toronto")

# Fixed board studios (order matters — matches the template rails).
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


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def emit_fallback_note(note):
    """Surface an IMAP-fallback note to the workflow's commit step."""
    print(f"NOTE: {note}")
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a") as fh:
            fh.write(f"FALLBACK_NOTE={note}\n")


def decimal_hours(dt, base_day):
    """Local clock hours from the window's base day; +24 per day past it.
    e.g. 2:15 AM the next day → 26.25."""
    dt = dt.astimezone(TZ) if isinstance(dt, datetime) and dt.tzinfo else dt
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


def clean_who(title):
    """Strip source tags / paid markers / session counters / studio notes from a title."""
    t = title or ""
    t = re.sub(r"\*moved from[^*]*\*", "", t, flags=re.I)
    t = re.sub(r"\((?:Fixed Option|AAP|Studio [^)]*)\)", "", t, flags=re.I)
    t = re.sub(r"\[(?:Un)?Paid\]", "", t, flags=re.I)
    t = re.sub(r"\bBooking Extension Request\b", "", t, flags=re.I)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s+#?\d+/\d+\b", "", t)          # session counters "2/8"
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -–—")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Bookings + staff — Google Calendar secret ICS
# ─────────────────────────────────────────────────────────────────────────────
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
        # all-day (date, not datetime) → treat as full-day unavailable block, skip below
        if not isinstance(dts, datetime):
            out.append({"summary": summary, "allday": True, "start": None, "end": None})
            continue
        status = str(ev.get("STATUS") or "").upper()
        out.append({
            "summary": summary,
            "allday": False,
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
    """Return (events, staff) from studio + staff calendars."""
    events = []
    staff = []
    for key, url in ics_map.items():
        occ = fetch_ics(url, win_start, win_end)
        is_staff = "staff" in key.lower()
        studio_id = key if key in STUDIO_IDS else None
        for ev in occ:
            summary = ev["summary"]
            if ev.get("allday"):
                continue
            if ev.get("cancelled"):
                continue
            low = summary.lower()
            if "unavailable" in low:
                continue

            if is_staff:
                s = parse_staff_row(summary, ev["dtstart"], ev["dtend"], base_day)
                if s:
                    staff.append(s)
                continue

            if studio_id is None:
                # Unknown non-studio calendar key — ignore rather than mis-map.
                continue

            events.append({
                "studio": studio_id,
                "raw": summary,
                "who": clean_who(summary),
                "kind": "cleaning" if is_cleaning(summary) else "booking",
                "start": decimal_hours(ev["dtstart"], base_day),
                "end": decimal_hours(ev["dtend"], base_day),
                # tier/gtg/hta/arrived/departed filled by the Notion join
                "tier": None, "gtg": True, "hta": None,
                "arrived": None, "departed": None,
            })

    events = merge_events(events)
    return events, staff


def parse_staff_row(summary, dtstart, dtend, base_day):
    low = summary.lower().strip()
    if low.startswith("need ") or "meeting" in low or "payroll" in low:
        return None
    if "ela morning" in low:
        return None
    start = decimal_hours(dtstart, base_day)
    end = decimal_hours(dtend, base_day)
    # Open/Close the Studio → name "Staff"
    if re.search(r"open the studio", low):
        return {"name": "Staff", "role": "Open", "start": start, "end": end}
    if re.search(r"close the studio", low):
        return {"name": "Staff", "role": "Close", "start": start, "end": end}
    # "<Name> FBS|Monitoring|Monitor|Viewing"
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
    for studio, evs in by_studio.items():
        evs.sort(key=lambda x: x["start"])
        cur = None
        for e in evs:
            same_renter = cur and cur["kind"] == e["kind"] and \
                _renter_key(cur["who"]) == _renter_key(e["who"]) and cur["who"]
            contiguous = cur and e["start"] <= cur["end"] + 1e-6
            if cur and same_renter and contiguous:
                cur["end"] = max(cur["end"], e["end"])
            else:
                if cur:
                    merged.append(cur)
                cur = dict(e)
        if cur:
            merged.append(cur)
    merged.sort(key=lambda x: (x["studio"], x["start"]))
    return merged


def _renter_key(who):
    return re.sub(r"[^a-z]", "", (who or "").lower())


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
    if t == "select":
        return v.get("name")
    if t == "status":
        return v.get("name")
    if t == "date":
        return v.get("start")
    if t == "checkbox":
        return "Yes" if v else "No"
    if t == "formula":
        return _prop_text({"type": v.get("type"), v.get("type"): v.get(v.get("type"))})
    if t == "number":
        return v
    if isinstance(v, str):
        return v
    return None


def fetch_notion_rows(token, today_iso):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    body = {
        "filter": {"property": "Booking Date", "date": {"equals": today_iso}},
        "page_size": 100,
    }
    # Prefer the data-source endpoint (2025-09 API); fall back to the database endpoint.
    urls = [
        (f"https://api.notion.com/v1/data_sources/{NOTION_DATA_SOURCE}/query", "2025-09-03"),
        (f"https://api.notion.com/v1/databases/{NOTION_DATA_SOURCE}/query", "2022-06-28"),
    ]
    last_err = None
    for url, ver in urls:
        try:
            h = dict(headers, **{"Notion-Version": ver})
            rows, cursor = [], None
            while True:
                b = dict(body)
                if cursor:
                    b["start_cursor"] = cursor
                r = requests.post(url, headers=h, json=b, timeout=30)
                if r.status_code != 200:
                    last_err = f"{r.status_code} {r.text[:200]}"
                    break
                data = r.json()
                rows.extend(data.get("results", []))
                if not data.get("has_more"):
                    return rows
                cursor = data.get("next_cursor")
            # non-200 → try next endpoint
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    die(f"Notion query failed: {last_err}")


def parse_notion(rows):
    """Return list of dicts with studio, title, start/end decimal-ish, tier, gtg, hta, board arm/disarm."""
    out = []
    for row in rows:
        p = row.get("properties", {})
        status = (_prop_text(p.get("Booking Status")) or "").lower()
        if "cancel" in status or "missed" in status:
            continue
        studio = (_prop_text(p.get("Studio")) or "").strip()
        # "901 (Elements)" → "901"
        studio = re.sub(r"\s*\(.*\)\s*", "", studio).strip()
        tob = (_prop_text(p.get("Type of Booking")) or "").strip()
        tier = {
            "fbs": "FBS", "monitor only": "Monitor", "studio viewing": "Viewing",
        }.get(tob.lower())
        gtg = (_prop_text(p.get("GTG")) or "").strip().lower() == "yes"
        out.append({
            "studio": studio,
            "title": _prop_text(p.get("Skedda Booking Title")) or "",
            "start": _prop_text(p.get("Start Time")),
            "end": _prop_text(p.get("End Time")),
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
    """Attach tier/gtg/hta (and board arm/disarm as fallback) by studio + overlapping time."""
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
            e["tier"] = best["tier"]
            e["gtg"] = best["gtg"]
            e["hta"] = best["hta"]
            e["_board_disarmed"] = best["board_disarmed"]
            e["_board_armed"] = best["board_armed"]
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 3. Arrivals / departures — Gmail IMAP
# ─────────────────────────────────────────────────────────────────────────────
ARM_LABEL = "Artist Care - ADT"

RE_DISARM = re.compile(r"Studio\s+(\d+\w?)[^:]*:\s*Studio\s+(\d+\w?)\s+was\s+Disarmed\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
RE_ARM = re.compile(r"Studio\s+(\d+\w?)[^:]*:\s*Studio\s+(\d+\w?)\s+was\s+Armed\s+Away\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
RE_PANEL_DISARM = re.compile(r"Studio\s+(\d+\w?)\s+Panel\s+was\s+Disarmed\s+by\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.I)
RE_PANEL_ARM = re.compile(r"Panel\s+was\s+Armed\s+Away\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)\s*\((.+?)\)", re.I)
IGNORE = ("motion", "pending", "image", "alarm")
STAFF_REMOTE = "info@danceannex.ca"


def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    return "".join(
        (b.decode(enc or "utf-8", "ignore") if isinstance(b, bytes) else b)
        for b, enc in parts
    )


def norm_studio_label(raw):
    raw = (raw or "").strip()
    m = re.match(r"(\d+)([AB])?", raw)
    if not m:
        return None
    base = m.group(1) + (m.group(2) or "")
    if base in STUDIO_IDS:
        return base
    # bare "509" never occurs for arm/disarm; map "901" family
    if base == "901":
        return "901"
    return base if base in STUDIO_IDS else None


def fetch_arm_events(user, password, since_dt):
    """Return list of {studio, name, time 'HH:MM', kind 'arrival'|'departure'}."""
    out = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, password)
        M.select(f'"{ARM_LABEL}"')
        since = since_dt.strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE "{since}")')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        for num in data[0].split():
            typ, msg_data = M.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject"))
            date_hdr = msg.get("Date")
            try:
                msg_dt = email.utils.parsedate_to_datetime(date_hdr).astimezone(TZ)
            except Exception:  # noqa: BLE001
                msg_dt = None
            if msg_dt and msg_dt < since_dt:
                continue
            parsed = parse_arm_subject(subject)
            if parsed:
                out.append(parsed)
        M.logout()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"IMAP error: {e}")
    return out


def parse_arm_subject(subject):
    low = subject.lower()
    if any(k in low for k in IGNORE):
        return None

    m = RE_DISARM.search(subject)
    if m:
        who = m.group(3).strip()
        return _arm_evt(m.group(2), who, m.group(4), "arrival")
    m = RE_ARM.search(subject)
    if m:
        return _arm_evt(m.group(2), m.group(3).strip(), m.group(4), "departure")
    m = RE_PANEL_DISARM.search(subject)
    if m:
        return _arm_evt(m.group(1), m.group(2).strip(), m.group(3), "arrival")
    m = RE_PANEL_ARM.search(subject)
    if m:
        # panel-arm variant: studio not in subject — best effort, drop (no studio)
        return None
    return None


def _arm_evt(studio_raw, name, time_raw, kind):
    studio = norm_studio_label(studio_raw)
    if not studio:
        return None
    if STAFF_REMOTE in name.lower():
        return None  # staff remote → attribute to no renter
    return {"studio": studio, "name": name, "time": norm_hm(time_raw), "kind": kind}


def apply_arm_events(events, arm_events):
    """Match arm/disarm to bookings: studio + nearest time in [start-60, end+90].
    earliest disarm = arrived, last arm = departed."""
    for e in events:
        window_lo = e["start"] - 1.0
        window_hi = e["end"] + 1.5
        arrivals, departures = [], []
        for a in arm_events:
            if a["studio"] != e["studio"] or not a["time"]:
                continue
            t = _time_to_decimal(a["time"])
            if t is None or not (window_lo <= t <= window_hi):
                continue
            (arrivals if a["kind"] == "arrival" else departures).append((t, a["time"]))
        if arrivals:
            e["arrived"] = min(arrivals)[1]
        if departures:
            e["departed"] = max(departures)[1]
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
    win_end = win_start + timedelta(days=1) - timedelta(minutes=1)  # next day 04:59

    ics_map = json.loads(os.environ.get("ICS_URLS") or "{}")
    if not ics_map:
        die("ICS_URLS env is empty — cannot build bookings.")

    events, staff = build_calendar_events(ics_map, win_start, win_end, base_day)

    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token:
        die("NOTION_TOKEN missing.")
    notion_rows = parse_notion(fetch_notion_rows(notion_token, base_day.isoformat()))
    events = join_notion(events, notion_rows)

    # arrivals — IMAP, soft-fail to board columns
    user = os.environ.get("GMAIL_USER", "thedanceannex@gmail.com")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    used_fallback = False
    if pw:
        try:
            arm_events = fetch_arm_events(user, pw, win_start)
            events = apply_arm_events(events, arm_events)
        except Exception as e:  # noqa: BLE001
            used_fallback = True
            emit_fallback_note(f"IMAP failed ({e}); used board Armed/Disarmed fallback.")
            events = apply_board_fallback(events)
    else:
        used_fallback = True
        emit_fallback_note("GMAIL_APP_PASSWORD missing; used board Armed/Disarmed fallback.")
        events = apply_board_fallback(events)

    # strip internal keys
    clean_events = []
    for e in events:
        clean_events.append({
            "studio": e["studio"], "who": e["who"], "kind": e["kind"],
            "tier": e["tier"], "gtg": e["gtg"], "hta": e["hta"],
            "arrived": e.get("arrived"), "departed": e.get("departed"),
            "start": round(e["start"], 4), "end": round(e["end"], 4),
        })

    data = {
        "date": now.strftime("%A, %B %-d, %Y"),
        "generatedAt": now.strftime("%b %-d, %-I:%M %p ET"),
        "generatedAtISO": now.replace(microsecond=0).isoformat(),
        "studios": STUDIOS,
        "events": clean_events,
        "staff": sorted(staff, key=lambda s: s["start"]),
        "attention": [],
    }
    return data, used_fallback


def splice(data):
    tpl = open(TEMPLATE, encoding="utf-8").read()
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    out = re.sub(
        r"/\*__DATA__\*/.*?/\*__END_DATA__\*/",
        lambda _: "/*__DATA__*/" + payload + "/*__END_DATA__*/",
        tpl, count=1, flags=re.S,
    )
    # wrap as full document (per RUNBOOK)
    out = ('<!doctype html>\n<html lang="en">\n<head>\n'
           '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
           '<meta name="robots" content="noindex,nofollow">\n') + out
    i = out.index("</style>") + len("</style>")
    out = out[:i] + "\n</head>\n<body>" + out[i:] + "\n</body>\n</html>\n"
    return out


def main():
    now = datetime.now(TZ)
    force = os.environ.get("FORCE_BUILD") == "1"
    if not force and not (7 <= now.hour < 24):
        print(f"Outside 07:00–24:00 Toronto window ({now:%H:%M}); skipping.")
        return
    data, fallback = build_data(now)
    html = splice(data)
    open(OUTPUT, "w", encoding="utf-8").write(html)
    n = len(data["events"])
    arrived = sum(1 for e in data["events"] if e["arrived"])
    print(f"Built index.html — {n} bookings, {arrived} with arrivals"
          + (" [IMAP fallback]" if fallback else ""))


if __name__ == "__main__":
    main()
