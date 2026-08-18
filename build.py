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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import icalendar
import recurring_ical_events

import skedda_names   # read-only renter-name lookup; soft-fails to no enrichment

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
# 🚥 Run Monitor DB (Staff Console) — robot heartbeat roster for the Robots tab.
RUN_MONITOR_DS = "caca3d50-b7b9-4f2a-b172-4fdcfce96cac"
# 📊 Workflow Reports — one row per fleet run, rendered as the Reports tab.
# Read title-only; see fetch_reports() for why bodies must stay off this board.
WORKFLOW_REPORTS_DS = "469a877b-83fa-4387-ac97-94aa656481dd"
# ✅ Actions to Perform — the fleet's human worklist, source of the Issues tab.
# Only the FBS-shaped classes are published; see fetch_issues().
ACTIONS_DS = "20df225d-382f-4bb8-9c15-c31571c9f4e0"

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
OUTPUT = os.path.join(HERE, "index.html")
TEMPLATE_MOBILE = os.path.join(HERE, "template-mobile.html")   # sister PWA (agenda view)
OUTPUT_MOBILE = os.path.join(HERE, "mobile.html")
# Durable last-known panel state, committed with the page. The email lookback can
# only see a few days back and dies with any Gmail hiccup; this file remembers
# each studio's last arm/disarm indefinitely, so the board never regresses to
# "Unknown" for a studio that simply had a quiet week.
PANEL_STATE = os.path.join(HERE, "panel-state.json")
# Poll target for the open pages. Both editions used to detect a new edition by
# refetching the whole page (59 KB desktop / 39 KB mobile) every 5 minutes just
# to compare one timestamp. This file carries the same timestamp in ~60 bytes,
# so clients can poll every 30 s for a fraction of the bandwidth — faster AND
# cheaper. Keep it byte-small; it is fetched far more often than the pages.
VERSION = os.path.join(HERE, "version.json")
# Per-booking arrival/departure, published for machines rather than for the two
# pages. The event gate reads it to answer one question before it sends a canned
# how-to reply: is this renter still in the studio? panel-state.json cannot
# answer that — it holds one row per STUDIO, so a back-to-back renter's arrival
# overwrites the previous renter's departure, and a text arriving after someone
# has gone home reads as "occupied". These rows are per BOOKING and already
# carry the builder's attribution (remote events excluded, wrong-studio handled),
# so a consumer needs no name matching of its own. Same fields the pages already
# publish — no PINs, no codes (see parse_notion()).
BOOKING_STATE = os.path.join(HERE, "booking-state.json")

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
ARM_LABEL = "Artist Care - ADT"
# alarm-mcp's /arm-history — the PRIMARY arrival/departure source since
# 2026-08-18. See fetch_arm_history() for why it displaced the Gmail feed and
# what the Gmail feed is still for.
ARM_HISTORY_URL = (os.environ.get("ALARM_HISTORY_URL") or "").strip()
ARM_HISTORY_TOKEN = (os.environ.get("ALARM_HISTORY_TOKEN") or "").strip()
EPS = 1e-6


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
    # Platform-synced events carry their booking URL in the summary
    # ("Booking on Giggster.com https://giggster.com/…"). Strip it before the
    # "Name: Description" split below, or the scheme's own colon splits the
    # title there and the board shows a renter called "… .com https".
    t = re.sub(r"\bhttps?://\S+", "", t, flags=re.I)
    t = re.sub(r"\bbooking on ([a-z0-9-]+)\.(?:com|co|io|net)\b",
               lambda m: f"{m.group(1).title()} Booking", t, flags=re.I)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s+#?\d+/\d+\b", "", t)          # session counters "2/8"
    t = re.sub(r"\s{2,}", " ", t)
    t = t.strip(" -–—:)(")
    # "Name: Description" → "Name — Description"; drop the rhs when it just
    # repeats the name ("Desiree Joy: Desiree Joy", "X (Org): Org").
    if ":" in t:
        lhs, rhs = (s.strip(" -–—:") for s in t.split(":", 1))
        t = lhs if (not rhs or rhs.lower() in lhs.lower()) else f"{lhs} — {rhs}"
    # "Name (Name)" → "Name" (Skedda duplicates the renter name in parens),
    # including with a trailing description ("Nicole Drury (Nicole Drury) — boxing")
    # or with the closing paren already stripped.
    t = re.sub(r"^([^(]+?)\s*\(\s*\1\s*\)", r"\1", t, flags=re.I)
    t = re.sub(r"^([^(]+?)\s*\(\s*\1\s*$", r"\1", t, flags=re.I)
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


def _bust_cache(url):
    """Skedda's per-space ICS feeds sit behind caches that can serve a booking's
    OLD space for a while after it is moved — the board then shows the renter in
    two studios at once (2026-08-15: Jessica T. Peerspace on 509B and 693).
    Studio identity here comes only from which feed an event arrived in, so a
    stale feed is a wrong studio, not just a late one. Ask for a fresh copy."""
    stamp = int(datetime.now(TZ).timestamp())
    return f"{url}{'&' if '?' in url else '?'}_cb={stamp}"


def fetch_ics(url, win_start, win_end):
    try:
        r = requests.get(_bust_cache(url), timeout=30,
                         headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
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
            # Read for provenance only — which sync lane wrote this event (see
            # lane_of). Never rendered; descriptions carry prices and notes.
            "description": str(ev.get("DESCRIPTION") or ""),
            "cancelled": status == "CANCELLED",
            "recurring": bool(ev.get("RRULE")),
            "dtstart": dts.astimezone(TZ),
            "dtend": dte.astimezone(TZ),
        })
    return out


CLEANERS = ("stefan", "donny", "ela")


def is_cleaning(summary):
    """Staff blocks on studio calendars: '<Cleaner> ... clean ...' or just the
    cleaner's name alone — after dropping '(Studio …)' parentheticals, e.g.
    'Stefan (Studio 901 (Elements))'."""
    s = _strip_paren_groups(summary, ("studio",)).lower().strip()
    toks = s.split()
    if not toks or toks[0] not in CLEANERS:
        return False
    return "clean" in s or len(toks) <= 2


def plan_of(summary, recurring):
    """Skedda's colour convention, reconstructed from the booking title.

    blue  = recurring renter (contract / Fixed Option)
    green = Artist Plan (AAP / 1AP)
    yellow = one-off booking, not on contract
    (red is a runtime state — missed booking — decided in the page, not here.)
    """
    s = (summary or "").lower()
    if re.search(r"\b(aap|1ap|artist plan)\b", s):
        return "aap"
    if recurring or "fixed option" in s:
        return "contract"
    return "oneoff"


MARKETPLACE_HOSTS = ("peerspace.com", "giggster.com", "tagvenue.com", "splacer.co")


def lane_of(summary, description):
    """Which sync lane wrote this calendar event.

    Each studio's Google calendar is written by TWO independent syncs, and the
    difference decides which one is right about the room:

      "skedda"      — the Skedda→Google mirror. Description starts "Spaces:
                      Studio NNN"; the studio is Skedda's own field, so this
                      lane is the system of record for WHERE a booking is.
      "marketplace" — Peerspace/Giggster's own feed. Carries a confirmation
                      number and a marketplace link, and lands on whichever
                      studio the LISTING names. It never learns about a move,
                      because moves happen in Skedda.

    That asymmetry is the whole cross-studio ghost: on 2026-08-15 Jessica T.'s
    booking was moved to 693 in Skedda at 11:58Z, the Skedda lane wrote 693,
    and the Peerspace event created on 07-24 sat on 509B untouched. Both are
    real calendar entries; only one knows the current room."""
    d = (description or "").lower()
    s = (summary or "").lower()
    if re.search(r"^\s*spaces:\s*studio\b", d, re.M):
        return "skedda"
    if any(h in d or h in s for h in MARKETPLACE_HOSTS):
        return "marketplace"
    return "unknown"


def build_calendar_events(ics_map, win_start, win_end, base_day):
    events, staff = [], []
    for key, url in ics_map.items():
        occ = fetch_ics(url, win_start, win_end)
        is_staff = key == "Staff"
        for ev in occ:
            summary = ev["summary"]
            if ev.get("cancelled") or "unavailable" in summary.lower():
                continue
            if os.environ.get("DEBUG_ARM") == "1" and not is_staff:
                print(f"CAL-DEBUG: {key} {summary!r} cleaning={is_cleaning(summary)}")
            if is_staff:
                s = parse_staff_row(summary, ev["dtstart"], ev["dtend"], base_day)
                if s:
                    staff.append(s)
                continue
            events.append({
                "studio": key,
                "who": clean_who(summary),
                "kind": "cleaning" if is_cleaning(summary) else "booking",
                "plan": plan_of(summary, ev.get("recurring")),
                "start": decimal_hours(ev["dtstart"], base_day),
                "end": decimal_hours(ev["dtend"], base_day),
                "tier": None, "gtg": True, "hta": None,
                "arrived": None, "departed": None,
                "lane": lane_of(summary, ev.get("description")),
            })
    events, mirrored, orphans = drop_marketplace_mirrors(events)
    for ghost, twin in mirrored:
        moved = " — MOVED" if ghost["studio"] != twin["studio"] else ""
        print(f"NOTE: {ghost['lane']} mirror of {twin['who']!r} on {ghost['studio']} "
              f"ignored; Skedda has it in {twin['studio']}{moved}.")
    for o in orphans:
        # Loud on purpose: a marketplace booking with no Skedda record is a hole
        # in the system of record, not a rendering detail. It stays on the board.
        print(f"NOTE: ⚠ {o['who']!r} on {o['studio']} came from {o['lane']} with NO "
              f"Skedda booking — kept on the board; check it exists in Skedda.")
    return merge_events(events), staff


# The staff rail shows PEOPLE only. Anything not led by a rostered staff name —
# unassigned placeholders (Need FBS / Need Monitoring / Studio Viewing Support,
# Open/Close the Studio) and any future placeholder title — never renders.
STAFF_ROSTER = ("junyan", "kyjah", "ela", "stefan", "donny")


def parse_staff_row(summary, dtstart, dtend, base_day):
    low = summary.lower().strip()
    if "meeting" in low or "payroll" in low or "ela morning" in low:
        return None
    m = re.search(r"^\s*([A-Za-z][A-Za-z'’-]*)\s+.*?\b(FBS|Monitoring|Monitor|Viewing)\b", summary, re.I)
    if not m or m.group(1).lower() not in STAFF_ROSTER:
        return None
    role = m.group(2)
    role = {"monitor": "Monitoring"}.get(role.lower(), role[0].upper() + role[1:])
    return {"name": m.group(1), "role": role,
            "start": decimal_hours(dtstart, base_day),
            "end": decimal_hours(dtend, base_day)}


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
    return _mark_cross_studio_dupes(_dedupe_same_slot(merged))


def _is_nameless_title(who):
    """True when a cleaned title names a platform instead of a person.

    Marketplace-synced ICS summaries carry no renter at all — Giggster sends
    "Booking on Giggster.com https://…", which clean_who reduces to "Giggster
    Booking". Skedda knows the person; the feed never did. Titles that DO carry
    a name ("Peerspace Booking, Jessica T.", "ALVIN W. Peerspace") are not this
    and must never be overwritten."""
    name_part = re.split(r"\s+—\s+", who or "")[0].strip()
    return bool(re.fullmatch(r"(?:[A-Za-z0-9.-]+\s+)?[Bb]ooking[,.]?", name_part))


def enrich_names_from_skedda(events, win_start, win_end):
    """Fill in renter names the ICS feeds omit, using Skedda as the name source.

    Touches ONLY nameless platform titles, and only on an unambiguous match:
    one studio, one overlapping Skedda booking. Two candidates means the read
    cannot tell them apart, and a confidently wrong name at the door is worse
    than an honest "Giggster Booking". Any failure is soft — the board is not
    worth losing over a nicety."""
    targets = [e for e in events
               if e["kind"] == "booking" and _is_nameless_title(e.get("who"))]
    if not targets:
        return events, None
    try:
        rows = skedda_names.fetch_named_bookings(win_start, win_end)
    except skedda_names.SkeddaUnavailable as e:
        return events, f"Skedda name lookup skipped ({e}); platform titles left as-is."
    except Exception as e:  # noqa: BLE001 — never let a nicety fail the build
        return events, f"Skedda name lookup failed ({type(e).__name__}: {e})."

    base_day = win_start.date()
    for r in rows:
        r["_start"] = decimal_hours(r["start"], base_day)
        r["_end"] = decimal_hours(r["end"], base_day)

    filled = 0
    for e in targets:
        hits = [r for r in rows
                if r["studio"] == e["studio"]
                and e["start"] < r["_end"] - EPS and r["_start"] < e["end"] - EPS]
        if len(hits) != 1:
            continue
        # The booking's own Skedda title is the renter for marketplace bookings
        # (no venue user exists); a registered holder's name wins when present.
        name = hits[0]["user_name"] or hits[0]["title"]
        if not name or _is_nameless_title(name):
            continue
        rest = re.split(r"\s+—\s+", e["who"] or "", 1)
        e["who"] = f"{name} — {rest[1]}" if len(rest) > 1 else name
        filled += 1
    if filled:
        # A renamed row can now be recognised as its own ghost in another
        # studio — the placeholder title matched nothing.
        _mark_cross_studio_dupes(events)
    return events, (f"Skedda supplied {filled} renter name(s) the ICS feeds omitted."
                    if filled else None)


def drop_marketplace_mirrors(events):
    """Skedda is the board's only booking source (Junyan, 2026-08-15).

    A marketplace booking is written to the studio calendars TWICE — once by
    Peerspace/Giggster's own feed and once by the Skedda→Google mirror (see
    lane_of). The Skedda copy carries the room, the price and the paid status,
    and it tracks moves; the marketplace copy carries a confirmation number and
    cannot move. Taking both is what produced a renter in two studios at once
    and a renter named "Booking on Giggster.com https". So take Skedda's.

    ONE guard, and it is the important part: a marketplace row is dropped only
    when a Skedda row for the same renter overlaps it SOMEWHERE that day. A
    marketplace booking with no Skedda counterpart at all is kept and reported
    loudly — that is a booking missing from the system of record, and the door
    is the worst possible place to discover it. Silence would turn a sync gap
    into an empty studio on the board.

    Returns (kept, mirrored, orphans) — the caller logs both lists, so any row
    that leaves the board is traceable to a reason."""
    skedda = [e for e in events if e.get("lane") == "skedda" and e["kind"] == "booking"]
    kept, mirrored, orphans = [], [], []
    for e in events:
        if e.get("lane") != "marketplace" or e["kind"] != "booking":
            kept.append(e)
            continue
        # Two ways to recognise the same booking, and both are needed. By NAME,
        # because a moved booking's mirror sits in the wrong room ("Peerspace
        # Booking, Jessica T." on 509B ↔ "Jessica T. Peerspace" on 693). By
        # ROOM, because a marketplace title often carries no name at all
        # ("Giggster Booking") and a studio holds one booking at a time.
        twin = next((s for s in skedda
                     if e["start"] < s["end"] - EPS and s["start"] < e["end"] - EPS
                     and (_same_renter(s.get("who"), e.get("who"))
                          or s["studio"] == e["studio"])), None)
        if twin:
            mirrored.append((e, twin))
        else:
            orphans.append(e)
            kept.append(e)
    return kept, mirrored, orphans


def _mark_cross_studio_dupes(events):
    """One renter cannot be in two studios at once. With the marketplace mirrors
    already gone (see drop_marketplace_mirrors), a pair that still shows here is
    something the builder genuinely cannot adjudicate: two Skedda rows, or a
    hand-made event that means something to staff.

    So mark, never drop. Each row learns its siblings' studios; the page renders
    that instead of a red 'no arrival' on one of them. The alert stays on screen
    and stays counted — it just stops claiming a no-show that isn't one."""
    for group in _cross_studio_groups(events):
        for e in group:
            e["dup_studios"] = sorted({o["studio"] for o in group
                                       if o["studio"] != e["studio"]})
    return events


def _cross_studio_groups(events):
    """Groups of rows that are the same renter, overlapping, in ≥2 studios."""
    for e in events:
        e.pop("dup_studios", None)      # idempotent: safe to re-run after enrichment
    groups, claimed = [], set()
    for i, e in enumerate(events):
        if e["kind"] != "booking" or not e.get("who") or id(e) in claimed:
            continue
        group = [e]
        for o in events[i + 1:]:
            if (o["kind"] != "booking" or not o.get("who") or id(o) in claimed
                    or not _same_renter(e["who"], o["who"])):
                continue
            if e["start"] < o["end"] - EPS and o["start"] < e["end"] - EPS:
                group.append(o)
        if len({g["studio"] for g in group}) > 1:
            claimed.update(id(g) for g in group)
            groups.append(group)
    return groups


def _dedupe_same_slot(events):
    """A studio can only hold one booking at a time, so overlapping 'booking'
    events in the same studio are the same booking under two titles (seen with
    Peerspace: the synced 'Peerspace Booking, <First> <L>.' event plus a manually
    created descriptive event). _renter_key can't link them — the names share
    nothing — so dedupe on studio + overlapping time window, unioning the window
    and preferring a non-generic Peerspace title, otherwise the longer title."""
    out = []
    for e in events:
        dup = next((o for o in out
                    if o["studio"] == e["studio"] and o["kind"] == e["kind"] == "booking"
                    and e["start"] < o["end"] - EPS
                    and o["start"] < e["end"] - EPS), None)
        if dup:
            dup["start"] = min(dup["start"], e["start"])
            dup["end"] = max(dup["end"], e["end"])
            dup_generic = re.match(r"^\s*(peerspace\s+)?booking,", dup["who"] or "", re.I)
            e_generic = re.match(r"^\s*(peerspace\s+)?booking,", e["who"] or "", re.I)
            if (dup_generic and not e_generic) or (
                    bool(dup_generic) == bool(e_generic)
                    and len(e["who"] or "") > len(dup["who"] or "")):
                dup["who"] = e["who"]
            for key in ("arrived", "departed"):
                if not dup.get(key) and e.get(key):
                    dup[key] = e[key]
            # Both lanes wrote this slot and they AGREE on the room — the
            # normal case for a marketplace booking nobody moved. The merged
            # row inherits the Skedda lane, because a Skedda record does exist
            # for it and the survivor must not look like a marketplace orphan.
            if "skedda" in (dup.get("lane"), e.get("lane")):
                dup["lane"] = "skedda"
        else:
            out.append(e)
    return out


def _selftest_dedupe():
    def event(studio, who, start, end, arrived=None, lane="unknown"):
        return {
            "studio": studio, "who": who, "kind": "booking",
            "start": start, "end": end, "tier": None, "gtg": None,
            "hta": None, "arrived": arrived, "departed": None, "lane": lane,
        }

    qtrang = _dedupe_same_slot([
        event("901", "Booking, QTrang T.", 18.0, 21.0),
        event("901", "EVENT FBS QTrang Tran", 18.0, 19.25, arrived="18:32"),
    ])
    assert len(qtrang) == 1
    assert qtrang[0]["start"] == 18.0 and qtrang[0]["end"] == 21.0
    assert qtrang[0]["who"] == "EVENT FBS QTrang Tran"
    assert qtrang[0]["arrived"] == "18:32"

    adjacent = _dedupe_same_slot([
        event("901", "Alice", 18.0, 19.0),
        event("901", "Bob", 19.0, 20.0),
    ])
    assert len(adjacent) == 2

    gapped = _dedupe_same_slot([
        event("901", "Alice", 18.0, 19.0),
        event("901", "Bob", 21.0, 22.0),
    ])
    assert len(gapped) == 2

    different_studios = _dedupe_same_slot([
        event("901", "Alice", 18.0, 20.0),
        event("509A", "Bob", 18.0, 20.0),
    ])
    assert len(different_studios) == 2

    # A moved Peerspace booking still alive in the old studio's feed: two rows,
    # each pointing at the other. Both survive — the page shows the conflict.
    # ── Skedda-only policy (Junyan, 2026-08-15) ──────────────────────────────
    # The real 2026-08-15 shape: Skedda moved Jessica T. to 693; the Peerspace
    # feed still says 509B. The mirror is ignored, wherever it landed.
    kept, mirrored, orphans = drop_marketplace_mirrors([
        event("509B", "Peerspace Booking, Jessica T.", 9.0, 11.0, lane="marketplace"),
        event("693", "Jessica T. Peerspace", 9.0, 11.0, lane="skedda"),
    ])
    assert [e["studio"] for e in kept] == ["693"]
    assert len(mirrored) == 1 and mirrored[0][0]["studio"] == "509B" and not orphans

    # Same booking, same room, both lanes — the ordinary marketplace case. The
    # mirror goes too, so the Skedda title is what renders.
    kept, mirrored, orphans = drop_marketplace_mirrors([
        event("693", "Giggster Booking", 19.5, 22.5, lane="marketplace"),
        event("693", "Welton R. Giggster", 19.5, 22.5, lane="skedda"),
    ])
    assert [e["who"] for e in kept] == ["Welton R. Giggster"] and not orphans

    # NO Skedda counterpart: the booking exists only on the marketplace. Keep it
    # and shout — an empty studio on the board is how that becomes a door
    # incident.
    kept, mirrored, orphans = drop_marketplace_mirrors([
        event("527", "Peerspace Booking, Nadia K.", 14.0, 16.0, lane="marketplace"),
        event("693", "Someone Else", 14.0, 16.0, lane="skedda"),
    ])
    assert len(kept) == 2 and not mirrored
    assert len(orphans) == 1 and orphans[0]["who"] == "Peerspace Booking, Nadia K."

    # A Skedda booking for the same renter at a DIFFERENT hour is not a twin.
    kept, mirrored, orphans = drop_marketplace_mirrors([
        event("509B", "Peerspace Booking, Jessica T.", 9.0, 11.0, lane="marketplace"),
        event("693", "Jessica T. Peerspace", 14.0, 16.0, lane="skedda"),
    ])
    assert len(kept) == 2 and len(orphans) == 1 and not mirrored

    # ── What still reaches the reader ────────────────────────────────────────
    # Two Skedda rows disagreeing is a genuine double-booking, not a mirror.
    both_skedda = _mark_cross_studio_dupes([
        event("509B", "Jessica Tran", 9.0, 11.0, lane="skedda"),
        event("693", "Jessica Tran", 9.0, 11.0, lane="skedda"),
    ])
    assert len(both_skedda) == 2
    assert both_skedda[0]["dup_studios"] == ["693"]
    assert both_skedda[1]["dup_studios"] == ["509B"]

    # One shared first name is not a person match — a real no-show keeps its red
    # flag.
    namesake = _mark_cross_studio_dupes([
        event("509B", "Jessica Tran", 9.0, 11.0),
        event("693", "Jessica Okonkwo", 9.0, 11.0),
    ])
    assert not any(e.get("dup_studios") for e in namesake)

    # Same renter, two studios, no overlap — back-to-back rooms are legal.
    sequential = _mark_cross_studio_dupes([
        event("509B", "Jessica Tran", 9.0, 11.0),
        event("693", "Jessica Tran", 11.0, 13.0),
    ])
    assert not any(e.get("dup_studios") for e in sequential)

    # lane_of, on the exact strings the two syncs write.
    assert lane_of("Jessica T. Peerspace (Studio 693) [Paid]",
                   "Spaces: Studio 693\n\nPrice: $48.00") == "skedda"
    assert lane_of("Peerspace Booking, Jessica T.",
                   "Peerspace Booking, Jessica T.509 Bloor St West\n\n"
                   "Manage booking: https://www.peerspace.com/signin") == "marketplace"
    assert lane_of("Booking on Giggster.com https://giggster.com/bookings/428f",
                   "") == "marketplace"
    assert lane_of("Kateryna Zozulia", "") == "unknown"


GENERIC_TITLE_TOKENS = {
    "peerspace", "booking", "bookings", "giggster", "splacer", "event", "fbs",
    "studio", "viewing", "rehearsal", "class", "session", "the", "and",
}


def _same_renter(a, b):
    """Cross-studio person match. _renter_key is positional — it takes the first
    two tokens — so it cannot link 'Peerspace Booking, Jessica T.' (the synced
    title) to 'Jessica T. Peerspace' (the manual one) even though both name the
    same person. Compare token SETS instead, minus the platform/activity words
    every such title carries.

    Two shared name tokens are required, not one: a lone shared first name would
    quietly recast a real no-show by another Jessica as a moved booking, and a
    missed no-show costs more than a missed ghost."""
    def toks(who):
        name = re.split(r"\s+—\s+", who or "")[0]
        return {t for t in re.findall(r"[a-z]+", name.lower())
                if t not in GENERIC_TITLE_TOKENS}
    return len(toks(a) & toks(b)) >= 2


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
    if t == "rollup":
        inner = v.get("type")
        if inner == "array":
            arr = v.get("array") or []
            return _prop_text(arr[0]) if arr else None
        return _prop_text({"type": inner, inner: v.get(inner)})
    if isinstance(v, str):
        return v
    return None


def _notion_query(token, ds_id, body):
    """Query a Notion data source, trying the new then the legacy endpoint.
    Raises RuntimeError on failure — callers decide fatal vs soft."""
    endpoints = [
        (f"https://api.notion.com/v1/data_sources/{ds_id}/query", "2025-09-03"),
        (f"https://api.notion.com/v1/databases/{ds_id}/query", "2022-06-28"),
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
    raise RuntimeError(f"Notion query failed for {ds_id}: {last_err}")


def fetch_notion_rows(token, today_iso):
    body = {
        "filter": {"property": "Booking Date", "date": {"equals": today_iso}},
        "page_size": 100,
    }
    try:
        return _notion_query(token, NOTION_DATA_SOURCE, body)
    except RuntimeError as e:
        die(str(e))


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
        # ⚠ NEVER put the code itself anywhere near the payload. `Alarm Code` is
        # a rollup of the renter's real PIN and this board is a PUBLIC GitHub
        # Pages site. Read it here, collapse it to a boolean on this line, and
        # let the string go. Canon (capture-sop-update) masks PINs for the same
        # reason: the display name and access window are AI-readable, the PIN is
        # [STAFF]. A future edit that "helpfully" surfaces the value to save a
        # lookup publishes every renter's door code to the internet.
        has_code = bool((_prop_text(p.get("Alarm Code")) or "").strip())
        out.append({
            "id": row.get("id"),
            "status": (_prop_text(p.get("Booking Status")) or "").strip(),
            "studio": studio,
            "start": _prop_text(p.get("Start Time")),
            "tier": tier,
            "gtg": gtg if tier else True,
            "hta": _prop_text(p.get("HTA")),
            "has_code": has_code,
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
        if e["kind"] != "booking":
            continue   # a staff cleaning block must never absorb a booking's tier row
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
            e["_has_code"] = best["has_code"]
            e["_board_disarmed"] = best["board_disarmed"]
            e["_board_armed"] = best["board_armed"]
            e["_notion_id"] = best["id"]
            e["_board_status"] = best["status"]
    return events


def apply_missing_codes(events):
    """Flag bookings whose renter has no alarm code on file.

    A renter with no code cannot get in, and nobody finds out until they are
    standing at the door. The Doorman raises these each morning as `Access / PIN
    — <name> … — no alarm code on file` rows, but that is a Notion queue nobody
    reads mid-shift; the board is what is actually open when the door call comes.

    Only bookings that matched a Notion row are eligible — an unmatched booking
    has no code information either way, and "unknown" must never render as
    "missing". Self-serve/untiered bookings are skipped for the same reason the
    GTG chip skips them: they are not ours to let in.

    Suppression guard: if EVERY eligible booking reads as codeless, that is far
    more likely a renamed property, a broken rollup, or a permissions change than
    a day where nobody can get in. Flagging all of them would be a page full of
    false alarms, which is how a real one gets ignored. Emit nothing and say so.
    """
    eligible = [e for e in events
                if e["kind"] == "booking" and e["tier"] and "_has_code" in e]
    if not eligible:
        return events
    missing = [e for e in eligible if not e["_has_code"]]
    if len(missing) == len(eligible) and len(eligible) > 1:
        emit_fallback_note(
            f"Alarm-code check suppressed — all {len(eligible)} tiered bookings read "
            "as codeless, which is a schema/permissions failure far more often than "
            "a real one. Check the 'Alarm Code' rollup on 🛎️ FBS AI Support.")
        return events
    for e in missing:
        e["no_code"] = True
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
# Same nameless form but WITHOUT the word "Panel" — 509's usual shape:
#   "Studio 509: Studio 509B was Armed Away at 6:04 PM (info@danceannex.ca)"
#   "Studio 509: Studio 509B was Disarmed at 4:29 PM (Panel User)"
# Missing these left 509B stuck reading Open after it had been armed (2026-08-01).
RE_ARM_AT = re.compile(r"Studio\s+(\d+\w?)[^:]*:\s*Studio\s+(\d+\w?)\s+was\s+Armed\s+Away\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)\s*\((.+?)\)", re.I)
RE_DISARM_AT = re.compile(r"Studio\s+(\d+\w?)[^:]*:\s*Studio\s+(\d+\w?)\s+was\s+Disarmed\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)\s*\((.+?)\)", re.I)
IGNORE = ("motion", "pending", "image", "alarm")
STAFF_REMOTE = "info@danceannex.ca"
# Alarm-trigger subjects (JOB 1 Step 3.5 pattern): a PENDING alarm email, then a
# real Alarm email for the same studio. Parsed BEFORE the IGNORE list drops them.
RE_ALARM_PENDING = re.compile(r"Studio\s+(\d+\w?).{0,80}?\bPENDING\s+Alarm\b", re.I | re.S)
RE_ALARM_REAL = re.compile(r"Studio\s+(\d+\w?).{0,80}?\breported\s+an?\s+Alarm\b", re.I | re.S)
# Sensor bypass notices ("… was Armed Away with sensors Bypassed", "Bypass on Front
# Door …"). Loose on purpose: any ADT subject naming a studio + "bypass".
RE_BYPASS = re.compile(r"Studio\s+(\d+\w?).{0,120}?\bbypass", re.I | re.S)
# Panel trouble conditions ADT emails about: tamper, malfunction, low battery,
# AC/power loss, comms failure. These are "someone must go look at the panel"
# states that the arm/disarm stream cannot express.
#
# 2026-08-08: Junyan turned on Alarm.com's own System Event Notifications and
# pointed them at this same label, which adds four conditions ADT never mailed
# about — "System is unable to arm", "My panel is not communicating", "My panel
# has been shut down", "My property loses power". Their wording is Alarm.com's,
# not TELUS's, so the alternation carries both.
RE_TROUBLE = re.compile(
    r"Studio\s+(\d+\w?).{0,120}?\b(tamper|malfunction|trouble|low\s+battery|"
    r"power\s+(?:loss|failure)|ac\s+(?:loss|failure)|communication\s+failure|"
    r"unable\s+to\s+arm|not\s+communicating|no\s+communication|"
    r"(?:panel\s+)?shut\s*down|lost\s+power|offline)", re.I | re.S)

# Subjects that name a studio and read like a problem but must NEVER reach the
# board. Matched case-insensitively as substrings against the whole subject.
#
#   credentials in conflict — Alarm.com raises one per duplicate PIN across the
#     168 rotating codes. Four are standing right now. It is a housekeeping
#     notice, not a fault; Junyan asked for it excluded by name, and the
#     alarm-monitor's own filer has skipped it since 2026-06 for the same reason.
#   user codes have been changed — every code we issue mails one of these.
#   post-disarm / images uploaded — camera traffic, already covered by the
#     "image" guard, listed here so the intent survives a refactor.
TROUBLE_IGNORE = (
    "credentials in conflict",
    "credential conflict",
    "user codes have been changed",
    "post-disarm",
    "images uploaded",
)


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


def _msg_subject_and_ts(h, mid):
    r = requests.get(f"{GMAIL_API}/messages/{mid}", headers=h,
                     params={"format": "metadata", "metadataHeaders": "Subject"},
                     timeout=30)
    r.raise_for_status()
    msg = r.json()
    subject = ""
    for hdr in msg.get("payload", {}).get("headers", []):
        if hdr.get("name", "").lower() == "subject":
            subject = hdr.get("value", "")
            break
    return subject, int(msg.get("internalDate", "0"))


def _gmail_ids(h, label_id, q):
    ids, page = [], None
    for _ in range(20):
        params = {"labelIds": label_id, "q": q, "maxResults": 100}
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
    return ids


def load_panel_state():
    try:
        with open(PANEL_STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — first run, or a corrupt file: start clean
        return {}


def save_panel_state(state):
    with open(PANEL_STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def merge_panel_state(prev, arm_events, prior):
    """Newest known event per studio: this build's events win, then the email
    lookback, then whatever we already had on file. Never forgets a studio."""
    state = {k: dict(v) for k, v in (prev or {}).items() if isinstance(v, dict)}
    for src in (prior or {}), {}:
        for sid, ev in src.items():
            if ev.get("ts", 0) >= state.get(sid, {}).get("ts", -1):
                state[sid] = dict(ev)
    for ev in arm_events:
        sid = ev.get("studio")
        if not sid:
            continue
        if ev.get("ts", 0) >= state.get(sid, {}).get("ts", -1):
            when = datetime.fromtimestamp(ev["ts"] / 1000, TZ) if ev.get("ts") else None
            state[sid] = {**ev, "when": f"{when:%a} {ev['time']}" if when else ev.get("time")}
    return state


def fetch_prior_state(h, label_id, missing, win_start, days=5):
    """Last known arm/disarm for studios with NO event in today's window.

    A panel does not reset at 05:00 — a studio armed last night is still armed
    this morning, and reporting "No events yet" throws away state we can know.
    Gmail lists newest-first, so we walk back and stop as soon as every missing
    studio is resolved (usually a handful of messages)."""
    missing = set(missing)
    if not missing:
        return {}
    q = (f"after:{int((win_start - timedelta(days=days)).timestamp())} "
         f"before:{int(win_start.timestamp())}")
    out = {}
    for mid in _gmail_ids(h, label_id, q):
        if not missing:
            break
        try:
            subject, ts = _msg_subject_and_ts(h, mid)
        except Exception:  # noqa: BLE001 — a single unreadable message must not kill the build
            continue
        parsed = parse_arm_subject(subject)
        if not parsed or parsed["studio"] not in missing:
            continue
        when = datetime.fromtimestamp(ts / 1000, TZ)
        out[parsed["studio"]] = {**parsed, "ts": ts,
                                 "when": f"{when:%a} {parsed['time']}"}
        missing.discard(parsed["studio"])
    return out


def fetch_arm_events(win_start):
    """Return (arm_events, alarm_alerts, panel_prior).
    arm_events: [{studio, name, time 'HH:MM', kind arrival|departure}]
    alarm_alerts: [{studio, time 'HH:MM', stage 'PENDING'|'ALARM'}] — alarm-trigger
    emails; time comes from the email's internalDate (their subjects carry none).
    panel_prior: {studio: last event BEFORE today's window} for studios silent today.
    Raises on soft (non-auth) failures."""
    tok = gmail_access_token()
    h = {"Authorization": f"Bearer {tok}"}
    label_id = _resolve_label_id(h, ARM_LABEL)
    ids = _gmail_ids(h, label_id, f"after:{int(win_start.timestamp())}")
    floor_ms = int(win_start.timestamp()) * 1000
    out, alerts = [], []
    for mid in ids:
        r = requests.get(f"{GMAIL_API}/messages/{mid}", headers=h,
                         params={"format": "metadata", "metadataHeaders": "Subject"},
                         timeout=30)
        r.raise_for_status()
        msg = r.json()
        internal_ms = int(msg.get("internalDate", "0"))
        if internal_ms < floor_ms:
            continue
        subject = ""
        for hdr in msg.get("payload", {}).get("headers", []):
            if hdr.get("name", "").lower() == "subject":
                subject = hdr.get("value", "")
                break
        alert = parse_alarm_subject(subject, internal_ms)
        if alert:
            alert["ts"] = internal_ms
            alerts.append(alert)
            continue
        parsed = parse_arm_subject(subject)
        if parsed:
            # Gmail lists newest-first and the subject only carries HH:MM, so two
            # events in the same minute (a disarm and an arm) cannot be ordered by
            # the subject alone. The email receipt time breaks the tie.
            parsed["ts"] = internal_ms
        if os.environ.get("DEBUG_ARM") == "1":
            print(f"ARM-DEBUG: {'PARSED ' + str(parsed) if parsed else 'DROPPED'} <- {subject!r}")
        if parsed:
            out.append(parsed)
    prior = fetch_prior_state(h, label_id,
                              STUDIO_IDS - {e["studio"] for e in out}, win_start)
    return out, alerts, prior


def fetch_arm_history(win_start):
    """Arrival/departure events from the PANEL, via alarm-mcp's /arm-history.

    Returns (arm_events, updated_at) in the same shape fetch_arm_events()
    returns, so apply_arm_events() cannot tell the two apart.

    WHY THIS IS THE PRIMARY SOURCE. Until 2026-08-18 arrivals came only from
    TELUS notification emails. That feed died twice, and when it dies the board
    does not go blank — every ended booking renders a red "no arrival", which
    reads as a renter no-show. Kiah Francis' 07:30-09:30 in 509A was flagged
    that way while the feed had been silent since 23:31 the night before. The
    panel ledger is the same fact one hop upstream, written every minute by the
    watchdog tick off a cross-panel read that has never been the thing to break.

    WHAT IT CANNOT DO. Panel state has no ACTOR, so `name` comes back empty and
    `remote` is unknowable here. enrich_arm_names() re-attaches both from the
    email feed when that feed is alive. An empty name is not a lie the matcher
    trips over: _name_match() returns False for it, so pass 1 skips the event
    and pass 2's time-window matching takes it — which is exactly how the
    builder has always handled ADT's own nameless panel notices.

    Raises on any failure. The caller decides what a failure means; this
    function never returns a partial answer that could read as "nobody came".
    """
    r = requests.get(ARM_HISTORY_URL,
                     headers={"Authorization": f"Bearer {ARM_HISTORY_TOKEN}"},
                     params={"since": win_start.astimezone(timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%S+00:00")},
                     timeout=30)
    r.raise_for_status()
    payload = r.json()
    out = []
    for ev in payload.get("events") or []:
        studio = norm_studio_label(ev.get("studio"))
        kind = ev.get("kind")
        if not studio or kind not in ("arrival", "departure"):
            continue
        try:
            at = datetime.fromisoformat(str(ev["at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        local = at.astimezone(TZ)
        if local < win_start:
            continue
        out.append({
            "studio": studio,
            "name": "",                 # the panel does not say who
            "time": f"{local.hour:02d}:{local.minute:02d}",
            "kind": kind,
            "remote": False,            # unknowable from state alone
            "ts": int(at.timestamp() * 1000),
            "source": "panel",
        })
    return out, payload.get("updated_at")


def feed_is_down(arm_events, arm_feed, now, win_start, quiet_hours=5):
    """Is the silence an outage rather than a quiet morning?

    Zero arm/disarm events across ALL FIVE studios, hours into the operating
    day, is not something the studios do — it is what a broken pipe looks like.
    Saying so is the whole point: on 2026-08-18 the board had no events at all
    and rendered that as a no-show against Kiah Francis, because a dead feed and
    an absent renter produce the identical `arrived: null`.

    Three conditions, and each one is load-bearing:

    `not arm_events` — one event anywhere means something is watching. Studios
    do not all sit idle while the feed works.

    `panel != "ok"` — a healthy panel ledger that legitimately returns nothing
    is the ONE case where empty is trustworthy, and it is not rare: the ledger
    genuinely holds no events before the first booking of the day disarms
    anything. Suppressing flags then would hide real no-shows on quiet days.

    `quiet_hours` past the window — the board's window opens at 05:00 and the
    studios open at 07:00. Firing before then would cry wolf every morning, and
    a banner that appears daily is a banner nobody reads by the second week.
    """
    if arm_events:
        return False
    if arm_feed.get("panel") == "ok":
        return False
    return now >= win_start + timedelta(hours=quiet_hours)


def enrich_arm_names(panel_events, mail_events):
    """Put the ADT feed's actor names onto the panel's timeline.

    The panel owns the TIMELINE (it is the source that stays up); the emails own
    the NAMES (the only source that has them). A mail event is matched to a
    panel event of the same studio and kind within two minutes — the same
    tolerance the Alarms tab already uses to collapse ADT's duplicate notices,
    and comfortably wider than the tick interval that bounds panel resolution.

    `remote` rides along with the name. A studio-account arm/disarm is a real
    panel change but never a renter's arrival, and without the email there is
    nothing in arm state that could tell the difference — so an unmatched panel
    event stays attributed to nobody rather than to whoever was booked.
    """
    used = set()
    for p in panel_events:
        best, best_gap = None, None
        for i, m in enumerate(mail_events):
            if i in used or m["studio"] != p["studio"] or m["kind"] != p["kind"]:
                continue
            gap = abs((m.get("ts") or 0) - (p.get("ts") or 0))
            if gap <= 2 * 60 * 1000 and (best_gap is None or gap < best_gap):
                best, best_gap = i, gap
        if best is not None:
            used.add(best)
            p["name"] = mail_events[best].get("name") or ""
            p["remote"] = bool(mail_events[best].get("remote"))
    # A mail event with no panel counterpart is still real — a disarm and
    # re-arm inside one tick is invisible to state polling, and the ledger is
    # blind to anything before its first write. Keep them.
    extra = [m for i, m in enumerate(mail_events) if i not in used]
    return panel_events + extra


def studio_from_subject(text):
    """The studio a subject refers to.

    ADT prefixes every subject with the SITE ("Studio 509: Studio 509B was …")
    and 509 alone is not a partition, so taking the first "Studio <n>" match
    silently dropped 509B bypass/tamper/alarm mails. Scan every token and keep
    the last valid one — the partition always follows the site prefix."""
    found = [norm_studio_label(m.group(1))
             for m in re.finditer(r"Studio\s+(\d+[AB]?)", text or "", re.I)]
    found = [f for f in found if f]
    return found[-1] if found else None


def parse_alarm_subject(subject, internal_ms):
    """Non-arm/disarm panel conditions: alarms, sensor bypasses, trouble states.
    Checked BEFORE the arm/disarm parse — the IGNORE list would drop them (they
    contain 'pending'/'alarm'). Time comes from the email's internalDate; these
    subjects carry none."""
    low = subject.lower()
    if "image" in low or "motion" in low:
        return None
    if any(phrase in low for phrase in TROUBLE_IGNORE):
        return None

    stage, detail = None, None
    if RE_ALARM_PENDING.search(subject):
        stage = "PENDING"
    elif RE_ALARM_REAL.search(subject):
        stage = "ALARM"
    elif RE_BYPASS.search(subject):
        stage, detail = "BYPASS", "Sensor bypass"
    else:
        m = RE_TROUBLE.search(subject)
        if m:
            stage, detail = "TROUBLE", m.group(2).strip().title()
    if not stage:
        return None

    studio = studio_from_subject(subject)
    if not studio:
        return None
    t = datetime.fromtimestamp(internal_ms / 1000, TZ)
    out = {"studio": studio, "time": f"{t.hour:02d}:{t.minute:02d}", "stage": stage}
    if detail:
        out["detail"] = detail
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
    m = RE_DISARM_AT.search(subject)
    if m:
        return _arm_evt(m.group(2), m.group(4).strip(), m.group(3), "arrival")
    m = RE_ARM_AT.search(subject)
    if m:
        return _arm_evt(m.group(2), m.group(4).strip(), m.group(3), "departure")
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
    # A remote arm/disarm by the studio account is still a real panel state change —
    # keep it for the Alarms panel, but flag it so it is never attributed to a renter.
    remote = STAFF_REMOTE in name.lower()
    return {"studio": studio, "name": "Studio (remote)" if remote else name,
            "time": norm_hm(time_raw), "kind": kind, "remote": remote}


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
        if a.get("remote"):
            continue                 # panel-state only, never a renter's arrival
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

    # pass 3 — wrong studio.
    #
    # A booking with no arrival in its OWN studio, whose renter disarmed a
    # DIFFERENT studio inside the same window, is not a no-show: the person is
    # in the building, in the wrong room. Those two situations need opposite
    # responses — a no-show is a billing question, a wrong studio is someone to
    # go move right now, often before the room's real booking walks in.
    #
    # 2026-08-07: Ayden Mauro booked 527 18:00-19:15 (30 attendees, paid) and
    # ran the session in 693, which nobody had booked. The board showed only
    # "no arrival" on 527, so it read as a no-show. He armed 693 at 18:52, one
    # minute before Amandeep Kaur's class disarmed the same room.
    #
    # Only unclaimed events qualify. If a booking in that other studio already
    # name-matched the event in pass 1, the person belongs there and this is a
    # coincidence of shared name tokens, not a misplaced renter.
    for e in events:
        if e["kind"] != "booking" or e.get("arrived"):
            continue
        for a in timed:
            if a["claimed"] or a["kind"] != "arrival" or a["studio"] == e["studio"]:
                continue
            if in_window(e, a["t"]) and _name_match(e["who"], a["name"]):
                e["wrong_studio"] = {"studio": a["studio"], "at": a["time"]}
                a["claimed"] = True
                break
    return events


def apply_board_fallback(events):
    for e in events:
        if e.get("_board_disarmed"):
            e["arrived"] = e["_board_disarmed"]
        if e.get("_board_armed"):
            e["departed"] = e["_board_armed"]
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 4. Robot heartbeats — 🚥 Run Monitor DB (Notion)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_notion_ts(iso):
    """Notion date → aware datetime (Toronto). Date-only values become midnight."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _last_checkin(r):
    """Newest of every pulse the Run Monitor's Health formula considers."""
    return max((t for t in (r.get("_self_hb"), r.get("_last_seen"),
                            r.get("_last_completed"), r.get("_last_run")) if t),
               default=None)


def robot_status(r, now):
    """Mirror the Run Monitor DB's Health formula semantics (see the DB's own
    property descriptions): Paused/Not-reporting are un-watched; a Window
    start/end pair means off-hours outside it; Daily-and-faster cadences go
    yellow at 75% of Stale-after and red past it; Weekly reds past 7 days +
    grace; Monthly must check in each calendar month."""
    if r["monitoring"] == "Paused":
        return "plain", "Paused"
    if r["monitoring"] == "Not reporting":
        return "plain", "Not reporting"
    ws, we = r["window_start"], r["window_end"]
    if ws is not None and we is not None and not (ws <= now.hour < we):
        return "plain", "Off-hours"
    last = _last_checkin(r)
    if last is None:
        return "crit", "Never checked in"
    age_min = (now - last).total_seconds() / 60
    stale = r["stale_after"] or 120
    cadence = (r["cadence"] or "").lower()
    if cadence == "monthly":
        if last.year == now.year and last.month == now.month:
            return "ok", "On time"
        month_min = (now.day - 1) * 1440 + now.hour * 60 + now.minute
        return ("crit", "Overdue") if month_min > stale else ("ok", "Due this month")
    if cadence == "weekly":
        limit = 7 * 1440 + stale
        if age_min > limit:
            return "crit", "Overdue"
        return ("watch", "Due") if age_min > 0.75 * limit else ("ok", "On time")
    if age_min > stale:
        return "crit", "Overdue"
    return ("watch", "Due") if age_min > 0.75 * stale else ("ok", "On time")


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def redact(text):
    """Strip the things that must never reach a public page.

    THIS BOARD IS PUBLIC — public repo, public Pages, index.html carries its
    whole DATA blob. Renter names and booking times are already published by
    design; phone numbers, money and door codes are not, and Action titles are
    internal text written with no thought for publication ("… his number
    +51 915 027 018 is PERU", "… the $259.90 August invoice").

    Order matters: phone patterns first (they are the longest), then money,
    then any remaining 4-8 digit run, which is the shape of an alarm code.
    Years are exempt so dates survive; studio numbers are three digits and are
    never touched. Redaction is a second line of defence, not the first — the
    first is fetch_issues() refusing to publish money-shaped classes at all."""
    t = text or ""
    t = re.sub(r"\+?\d[\d\s().\-]{7,}\d", "•••", t)             # phone numbers
    t = re.sub(r"\$\s?\d[\d,]*(?:\.\d+)?", "$•••", t)           # amounts
    t = re.sub(r"\b(?!(?:19|20)\d{2}\b)\d{4,8}\b", "••••", t)   # codes / PINs
    return t


def _lead(text, limit=88):
    """First clause of an issue title — the part that says what it is.

    Action titles run to 200+ characters with the reasoning attached. The tab
    exists to be read at a glance, so cut at the first natural break and let
    Notion hold the rest."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    # A sentence break, but NOT the dot in an initial — half these titles are
    # about people called "ALVIN W." or "Jessica T.", and splitting there cuts
    # the name in half.
    cuts = [t.split(sep)[0] for sep in (" — ", " – ", ": ", " (")]
    m = re.search(r"(?<![A-Z])\.\s", t)
    if m:
        cuts.append(t[:m.start()])
    usable = [c for c in cuts if 20 <= len(c) < len(t)]
    if usable:
        t = min(usable, key=len)
    return t if len(t) <= limit else t[:limit - 1].rstrip(" ,;–—") + "…"


def _dates_in(text, year):
    """Every 'Aug 22' / 'October' style date mentioned in a title."""
    out = []
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})\b", text or ""):
        mon = MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                out.append(datetime(year, mon, int(m.group(2)), tzinfo=TZ).date())
            except ValueError:
                pass
    for m in re.finditer(r"\b([A-Za-z]{3,9})\b", text or ""):
        mon = MONTHS.get(m.group(1)[:3].lower())
        if mon and len(m.group(1)) > 3:      # spelled out: "(October)"
            out.append(datetime(year, mon, 28, tzinfo=TZ).date())
    return out


def _is_near_term(row, today, horizon):
    """Today and tomorrow only (Junyan, 2026-08-15) — a door problem three
    weeks out is not something to read off the wall this evening.

    A row is far-off if it says so: a `Process on/after` date past the horizon,
    or every date named in its title past the horizon. A row that names no date
    at all is near-term by default — silence must never hide a live problem."""
    if row["process_after"] and row["process_after"] > horizon:
        return False
    dates = _dates_in(row["request"], today.year)
    if dates and all(d > horizon for d in dates):
        return False
    return True


FBS_ACTION_TYPES = ("Access / PIN", "Run Error")


def fetch_issues(token, now, reports, limit=12):
    """The Issues tab — what needs Junyan, today or tomorrow, FBS only.

    Sources, in the order they matter:
      1. Open ✅ Actions to Perform rows of an FBS shape — a door someone cannot
         open (`Access / PIN`) or a robot that broke (`Run Error`).
      2. Runs that ended early, from the reports already fetched. A run that
         dies mid-pass cannot file its own Action row, so nothing else would
         ever surface it.
      3. `⚠` report headlines — an agent saying, in a field written to be
         public, that something needs a human.

    DELIBERATELY EXCLUDED, and this is the load-bearing decision: money. Charge,
    Invoice and Platform-charge rows never reach this page. They are the rows
    whose titles carry amounts and who-owes-what, they are not FBS, and the
    board is public. Scope and privacy happen to point the same way here.

    A `Run Error` publishes only its class and which robot raised it. The titles
    describe how the desk's safety machinery failed ("the ALARM lane did not
    fire … nobody was paged") — true, useful to Junyan, and nobody else's
    business on a public page. The detail is one click away in Notion.

    Everything cleared in Notion vanishes here on the next build: the query asks
    for `Pending Review`, so Processed and Cancelled rows are simply not
    returned. There is no separate state to keep in sync."""
    today = now.date()
    horizon = today + timedelta(days=1)
    issues, run_errors = [], []

    rows = _notion_query(token, ACTIONS_DS, {
        "filter": {"property": "Status", "select": {"equals": "Pending Review"}},
        "page_size": 100,
    })
    for row in rows:
        p = row.get("properties", {})
        r = {
            "request": _prop_text(p.get("Request")) or "",
            "type": _prop_text(p.get("Type")) or "",
            "by": _prop_text(p.get("Raised by")) or "",
            "process_after": None,
        }
        after = _prop_text(p.get("Process on/after"))
        if after:
            d = _parse_notion_ts(after)
            r["process_after"] = d.date() if d else None
        urgent = r["request"].lstrip().startswith("🚨")
        if r["type"] not in FBS_ACTION_TYPES and not urgent:
            continue
        if not _is_near_term(r, today, horizon):
            continue
        when = _parse_notion_ts(_prop_text(p.get("Requested"))) \
            or _parse_notion_ts(row.get("created_time"))
        if r["type"] == "Run Error":
            run_errors.append((r["by"] or "unattributed", when))
        else:
            issues.append({"level": "crit" if urgent else "watch",
                           "label": "Access" if r["type"] == "Access / PIN" else "Urgent",
                           "text": redact(_lead(r["request"])),
                           "whenISO": when.isoformat() if when else None})

    # One line per robot, not per row. On 2026-08-15 there were twelve open Run
    # Errors across four robots; rendered individually they filled the tab with
    # "The Custodian — see Notion" three times over and pushed every door
    # problem off the page. The count is the signal; the detail is in Notion.
    for robot in sorted({r for r, _ in run_errors}):
        mine = [w for r, w in run_errors if r == robot]
        n = len(mine)
        issues.append({
            "level": "crit", "label": "Run error",
            "text": f"{robot}{f' ×{n}' if n > 1 else ''} — see Notion",
            "whenISO": min((w.isoformat() for w in mine if w), default=None),
        })

    for rep in reports or []:
        if rep.get("status") == "Ended Early":
            issues.append({"level": "crit", "label": "Run died",
                           "text": _lead(rep["run"]), "whenISO": rep.get("whenISO")})
        elif (rep.get("headline") or "").lstrip().startswith("⚠"):
            issues.append({"level": "watch", "label": "Report",
                           "text": redact(rep["headline"].lstrip("⚠ ").strip()),
                           "whenISO": rep.get("whenISO")})

    # Oldest first inside each severity — a door problem does not get less
    # urgent by sitting, and the stale ones are the ones that rot.
    order = {"crit": 0, "watch": 1}
    issues.sort(key=lambda i: (order.get(i["level"], 2), i["whenISO"] or ""))
    # The total counts open ROWS, not rendered lines, so "+N more" stays true
    # even though the Run Errors collapse to one line per robot.
    total = len(issues) - len({r for r, _ in run_errors}) + len(run_errors)
    return issues[:limit], total


def fetch_reports(token, now, days=3, limit=40):
    """Recent 📊 Workflow Reports rows — the Reports tab.

    Properties only — never bodies. That is not just a speed choice: **this
    board is public** (rule 5). Bodies carry renter names and, on some runs,
    specifics that have no business on a public page. Do not "improve" this by
    pulling body text in.

    The few-words answer lives in the `Headline` property (added 2026-08-15;
    contract in dc-canon library/autonomy.md §Output): one public-safe line,
    "what happened · what needs you", prefixed "⚠" when something needs a human
    now. A ⚠ headline lifts the row to `watch` so it reads as a problem at a
    glance even on a Completed run — a job can finish cleanly having FOUND
    something wrong; status and findings are different axes. Rows from before
    the property (or from a bot not yet filling it) have no headline and render
    as before, so the tab degrades to exactly its old shape.
    """
    since = (now - timedelta(days=days)).isoformat()  # already Toronto-aware
    rows = _notion_query(token, WORKFLOW_REPORTS_DS, {
        "filter": {"timestamp": "created_time", "created_time": {"on_or_after": since}},
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": 100,
    })
    out = []
    for row in rows:
        p = row.get("properties", {})
        title = _prop_text(p.get("Run")) or "(unnamed run)"
        status = _prop_text(p.get("Status")) or ""
        headline = _prop_text(p.get("Headline"))
        when = _parse_notion_ts(_prop_text(p.get("Completed At"))) \
            or _parse_notion_ts(row.get("created_time"))
        # crit/watch/ok mirror the Robots tab's classes so one stylesheet
        # covers both; an abandoned run must not read the same as a clean one.
        level = {"Ended Early": "crit", "Skipped Steps": "watch",
                 "In Progress": "watch"}.get(status, "ok")
        if headline and headline.lstrip().startswith("⚠") and level == "ok":
            level = "watch"
        out.append({
            "run": title,
            "status": status,
            "headline": headline,
            "level": level,
            "when": when.strftime("%a %-I:%M %p") if when else "",
            "whenISO": when.replace(microsecond=0).isoformat() if when else None,
        })
    # Sort on the time actually SHOWN, not on created_time. A rolling daily
    # report is created once in the morning and its Completed At advances all
    # day, so ordering by creation renders the column out of sequence
    # (10:45, 11:05, 11:58, 11:33 …) and the tab reads as unsorted.
    out.sort(key=lambda r: r["whenISO"] or "", reverse=True)
    return out[:limit]


def fetch_robots(token, now):
    rows = _notion_query(token, RUN_MONITOR_DS, {"page_size": 100})
    out = []
    for row in rows:
        p = row.get("properties", {})
        r = {
            "run": _prop_text(p.get("Run")) or "(unnamed)",
            "cadence": _prop_text(p.get("Cadence")),
            "expected": _prop_text(p.get("Expected")),
            "produces": _prop_text(p.get("Produces")),
            "monitoring": _prop_text(p.get("Monitoring")) or "Live",
            "stale_after": _prop_text(p.get("Stale after (min)")),
            "window_start": _prop_text(p.get("Window start")),
            "window_end": _prop_text(p.get("Window end")),
            "_self_hb": _parse_notion_ts(_prop_text(p.get("Self-heartbeat"))),
            "_last_seen": _parse_notion_ts(_prop_text(p.get("Last seen"))),
            # Rolling agents (Concierge, Receptionist, Responder…) create ONE report
            # per day and refresh its Completed At each pass — "Last seen" (report
            # created time) then reads hours old while the run is perfectly on time.
            # The DB's own Health formula uses "Last run", so read that too.
            "_last_completed": _parse_notion_ts(_prop_text(p.get("Last completed"))),
            "_last_run": _parse_notion_ts(_prop_text(p.get("Last run"))),
        }
        cls, label = robot_status(r, now)
        last = _last_checkin(r)
        out.append({
            "run": r["run"], "cadence": r["cadence"], "expected": r["expected"],
            "produces": r["produces"], "monitoring": r["monitoring"],
            "status": cls, "statusLabel": label,
            "lastISO": last.replace(microsecond=0).isoformat() if last else None,
        })
    rank = {"crit": 0, "watch": 1, "ok": 2, "plain": 3}
    out.sort(key=lambda x: (rank.get(x["status"], 9), x["run"].lower()))
    return out


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
    events, skedda_note = enrich_names_from_skedda(events, win_start, win_end)
    if skedda_note:
        print(f"NOTE: {skedda_note}")

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        die("NOTION_TOKEN missing.")
    events = join_notion(events, parse_notion(fetch_notion_rows(token, base_day.isoformat())))
    events = apply_missing_codes(events)

    # ---- Arrivals: panel ledger first, ADT email second ---------------------
    #
    # TWO SOURCES, DIFFERENT JOBS, AND NEITHER IS DECORATIVE.
    #   panel  (alarm-mcp /arm-history) — the TIMELINE. Written every minute off
    #          a cross-panel read that has never been the thing to break.
    #   email  (Gmail label "Artist Care - ADT") — the NAMES, which panel state
    #          cannot provide, and the sub-tick events state polling misses.
    #
    # Before 2026-08-18 there was only the email feed, and its second death that
    # month put a red "no arrival" on Kiah Francis' finished 07:30 booking while
    # the pipe had been silent for fourteen hours. A dead feed and a real
    # no-show produced the identical output — `arrived: null` — so the board
    # asserted a no-show on the strength of no evidence at all.
    #
    # Hence `arm_feed`: the build now knows WHICH sources answered, and says so
    # in the data. The page suppresses no-arrival flags when nothing answered.
    used_fallback = False
    arm_events, alarm_alerts, panel_prior = [], [], {}
    panel_events, mail_events = [], []
    arm_feed = {"panel": None, "mail": None, "updatedAt": None}

    if ARM_HISTORY_URL and ARM_HISTORY_TOKEN:
        try:
            panel_events, arm_feed["updatedAt"] = fetch_arm_history(win_start)
            arm_feed["panel"] = "ok"
        except Exception as e:          # noqa: BLE001 — soft: the email feed may still answer
            arm_feed["panel"] = "failed"
            print(f"NOTE: panel arm-history failed ({e}); leaning on the ADT email feed.")
    else:
        arm_feed["panel"] = "unconfigured"

    if os.environ.get("GMAIL_REFRESH_TOKEN"):
        try:
            mail_events, alarm_alerts, panel_prior = fetch_arm_events(win_start)
            arm_feed["mail"] = "ok"
        except SystemExit:
            raise                       # auth failure already died RED
        except Exception as e:          # noqa: BLE001
            arm_feed["mail"] = "failed"
            print(f"NOTE: Gmail arm fetch failed ({e}).")
    else:
        arm_feed["mail"] = "unconfigured"

    if panel_events or mail_events:
        arm_events = (enrich_arm_names(panel_events, mail_events)
                      if panel_events else mail_events)
        events = apply_arm_events(events, arm_events)
    else:
        # Nothing answered. The board's own Armed/Disarmed columns are a weaker
        # record (one row per studio, no actor) but they are a record.
        used_fallback = True
        # Distinguish "answered with nothing" from "did not answer" — they look
        # identical here and mean opposite things, and a note that says "no
        # source answered (panel: ok)" is the kind of self-contradiction that
        # costs an hour at 2am. `feed_is_down` already draws the same line.
        answered = [k for k in ("panel", "mail") if arm_feed[k] == "ok"]
        emit_fallback_note(
            (f"No arm events yet — {' and '.join(answered)} answered with an "
             "empty stream" if answered else "No arrival source answered")
            + f" (panel: {arm_feed['panel']}, mail: {arm_feed['mail']}); "
            "used board Armed/Disarmed fallback.")
        events = apply_board_fallback(events)

    arm_feed["down"] = feed_is_down(arm_events, arm_feed, datetime.now(TZ), win_start)
    if arm_feed["down"]:
        emit_fallback_note(
            "ARRIVAL FEED DOWN — no arm/disarm events from either source; "
            "no-arrival flags suppressed on the board.")

    clean = [{
        "studio": e["studio"], "who": e["who"], "kind": e["kind"],
        "tier": e["tier"], "gtg": e["gtg"], "hta": e["hta"],
        "arrived": e.get("arrived"), "departed": e.get("departed"),
        "wrong_studio": e.get("wrong_studio"),
        "dup_studios": e.get("dup_studios"),
        # Boolean only — see parse_notion(). The code never leaves the builder.
        "no_code": bool(e.get("no_code")),
        "start": round(e["start"], 4), "end": round(e["end"], 4),
    } for e in events]

    # Alarms tab: full arm/disarm stream, chronological across midnight (times
    # before 05:00 belong to the tail of the operating day).
    def day_key(hhmm):
        t = _time_to_decimal(hhmm)
        return t + 24 if t is not None and t < 5 else (t if t is not None else 99)
    # ADT emails one event more than once (a "disarmed by <name>" notice and the
    # panel notification, sometimes a minute apart), so the raw stream shows the
    # same arrival twice. Collapse anything with the same studio+kind inside a
    # 2-minute window, keeping the earliest and the most descriptive name.
    panel_state = merge_panel_state(load_panel_state(), arm_events, panel_prior)
    if arm_events or panel_prior:          # never blank the file on a Gmail fallback
        save_panel_state(panel_state)
    # A studio silent today falls back to its durable last-known event.
    today_studios = {e["studio"] for e in arm_events}
    panel_prior = {sid: ev for sid, ev in panel_state.items() if sid not in today_studios}

    arm_stream = []
    for a in sorted((a for a in arm_events if a.get("time")),
                    key=lambda a: (day_key(a["time"]), a.get("ts") or 0)):
        t = day_key(a["time"])
        dup = next((p for p in arm_stream
                    if p["studio"] == a["studio"] and p["kind"] == a["kind"]
                    and abs(day_key(p["time"]) - t) <= 2 / 60), None)
        if dup:
            if len(a.get("name") or "") > len(dup.get("name") or ""):
                dup["name"] = a["name"]
            continue
        arm_stream.append(dict(a))
    alarm_alerts.sort(key=lambda a: day_key(a["time"]))

    # Robots tab (soft source: page must never die because the roster is unreadable)
    robots, robots_note = None, None
    try:
        robots = fetch_robots(token, now)
    except Exception as e:  # noqa: BLE001
        robots_note = "Run Monitor unreadable — share the 🚥 Run Monitor DB with the integration."
        emit_fallback_note(f"Run Monitor fetch failed ({e}); Robots tab shows a notice.")

    # Reports tab — same soft posture: an unreadable Workflow Reports DB shows a
    # notice, it never takes the board down.
    reports, reports_note = None, None
    try:
        reports = fetch_reports(token, now)
    except Exception as e:  # noqa: BLE001
        reports_note = "Workflow Reports unreadable — share the 📊 Workflow Reports DB with the integration."
        emit_fallback_note(f"Workflow Reports fetch failed ({e}); Reports tab shows a notice.")

    # Issues tab — the worklist. Reports are one of its inputs, so it runs after
    # them and inherits the same soft posture.
    issues, issues_total, issues_note = None, 0, None
    try:
        issues, issues_total = fetch_issues(token, now, reports)
    except Exception as e:  # noqa: BLE001
        issues_note = "Actions to Perform unreadable — share the ✅ Actions to Perform DB with the integration."
        emit_fallback_note(f"Actions fetch failed ({e}); Issues tab shows a notice.")

    attention = []
    for a in alarm_alerts:
        lvl = "crit" if a["stage"] == "ALARM" else "warn"
        attention.append({"level": lvl, "text": f"Alarm {a['stage'].lower()} · {a['studio']} · {a['time']}"})
    if robots:
        overdue = sum(1 for r in robots if r["status"] == "crit")
        if overdue:
            attention.append({"level": "warn",
                              "text": f"{overdue} robot{'s' if overdue > 1 else ''} overdue — see Robots tab"})

    data = {
        "date": now.strftime("%A, %B %-d, %Y"),
        "generatedAt": now.strftime("%b %-d, %-I:%M %p ET"),
        "generatedAtISO": now.replace(microsecond=0).isoformat(),
        "studios": STUDIOS,
        "events": clean,
        "staff": sorted(staff, key=lambda s: s["start"]),
        "attention": attention,
        "armEvents": arm_stream,
        "panelPrior": panel_prior,
        "alarmAlerts": alarm_alerts,
        "armFallback": used_fallback,
        "armFeed": arm_feed,
        "robots": robots,
        "robotsNote": robots_note,
        "issues": issues,
        "issuesTotal": issues_total,
        "issuesNote": issues_note,
    }
    return data, used_fallback


def splice(data, template=None):
    tpl = open(template or TEMPLATE, encoding="utf-8").read()
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


def write_booking_state(data, fallback):
    """Publish per-booking presence for machine consumers (the event gate).

    Only the fields a presence question needs. `armFallback` is carried through
    deliberately: on a Gmail failure the builder falls back to the board's
    Armed/Disarmed columns, which lag the panel by up to a Concierge pass, and a
    consumer deciding whether someone is still in the room must be able to tell
    a live read from a degraded one rather than trusting both equally.
    """
    payload = {
        "generatedAtISO": data["generatedAtISO"],
        "dateISO": data["generatedAtISO"][:10],
        "armFallback": bool(fallback),
        "bookings": [{
            "studio": e["studio"],
            "who": e["who"],
            "start": e["start"],
            "end": e["end"],
            "arrived": e.get("arrived"),
            "departed": e.get("departed"),
        } for e in data["events"] if e.get("kind") == "booking"],
    }
    with open(BOOKING_STATE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def sync_booking_status(data, fallback, now):
    """Flip `Booking Status` to Complete for bookings the panel says are done.

    WHY THIS LIVES HERE. `Booking Status` is agent-written (Concierge JOB 1
    Step 4) while arrival/departure is machine-derived from the alarm panel on
    this ~15-minute rebuild. That gap is not cosmetic: The Responder's PBF only
    becomes due once the status reads Complete, and a PBF more than 1 day past
    its booking is never sent — so a slow flip does not delay the follow-up, it
    destroys it. Observed 2026-08-11: Kristel San Jose armed and left at 13:07,
    this file had `departed` by 13:12, and the board still read `In Studio` 75
    minutes later. The panel already knew; only the board did not.

    THE ONLY CASE WRITTEN IS THE UNAMBIGUOUS ONE — arrived AND departed AND the
    booking's end time has passed. Everything requiring judgment stays the
    Concierge's: no-shows, bookings with an arrival but no departure, anything
    already Cancelled / Missed Booking / Complete. Two writers on one field is
    how you get flapping, so the split is by certainty, not by convenience.

    NEVER RUNS ON FALLBACK DATA. When `armFallback` is set, arrival/departure
    came from the board's own Disarmed/Armed columns rather than the panel —
    writing that back would be laundering the board's guess into a fact.
    """
    if os.environ.get("BOOKING_STATUS_SYNC_DISABLED") == "1":
        print("Booking Status sync: disabled by kill switch.")
        return
    if fallback:
        print("Booking Status sync: skipped (arm data is board fallback).")
        return
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return
    dry = os.environ.get("BOOKING_STATUS_SYNC_DRYRUN") == "1"
    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    # `end` is decimal hours from the window's base day, so a 2:15 AM finish is
    # 26.25 — compare against a `now` measured the same way or post-midnight
    # bookings never qualify.
    base_day = now.date() if now.hour >= 5 else (now - timedelta(days=1)).date()
    now_dec = decimal_hours(now, base_day)
    flipped, failed = 0, 0
    for e in data["events"]:
        if e.get("kind") != "booking":
            continue
        if not (e.get("_notion_id") and e.get("arrived") and e.get("departed")):
            continue
        if e.get("_board_status") not in ("In Studio", "Upcoming"):
            continue
        end = e.get("end")
        if end is None or now_dec < end:
            continue               # still inside the window; they may return
        if dry:
            flipped += 1
            print(f"  [dry run] would set Complete: {e.get('who')} "
                  f"({e.get('studio')}, departed {e.get('departed')})")
            continue
        try:
            r = requests.patch(
                f"https://api.notion.com/v1/pages/{e['_notion_id']}",
                headers=headers,
                json={"properties": {"Booking Status": {"select": {"name": "Complete"}}}},
                timeout=30,
            )
            if r.status_code >= 300:
                failed += 1
                print(f"  ! Booking Status sync failed for {e.get('who')}: "
                      f"{r.status_code} {r.text[:200]}")
            else:
                flipped += 1
                print(f"  Booking Status → Complete: {e.get('who')} "
                      f"({e.get('studio')}, departed {e.get('departed')})")
        except Exception as exc:      # a write hiccup must not kill the board
            failed += 1
            print(f"  ! Booking Status sync error for {e.get('who')}: {exc}")
    if flipped or failed:
        print(f"Booking Status sync: {flipped} flipped, {failed} failed.")


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
    open(OUTPUT_MOBILE, "w", encoding="utf-8").write(splice(data, TEMPLATE_MOBILE))
    write_booking_state(data, fallback)
    sync_booking_status(data, fallback, now)
    # Written last, so it can never advertise an edition the pages don't carry yet.
    with open(VERSION, "w", encoding="utf-8") as fh:
        json.dump({"generatedAtISO": data["generatedAtISO"],
                   "generatedAt": data["generatedAt"]}, fh, separators=(",", ":"))
        fh.write("\n")
    n = len(data["events"])
    arrived = sum(1 for e in data["events"] if e["arrived"])
    print(f"Built index.html — {n} bookings, {arrived} with arrivals"
          + (" [board fallback]" if fallback else ""))


if __name__ == "__main__":
    main()
