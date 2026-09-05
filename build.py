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
# ✅ Actions to Perform — the fleet's human worklist. Only open Access / PIN
# row titles are read, and only a boolean reaches the page; see flag_access_gaps().
ACTIONS_DS = "20df225d-382f-4bb8-9c15-c31571c9f4e0"
# 📤 Message Queue — feeds the Messages tab (rows awaiting a human).
MESSAGES_DS = "df37abce-2222-4e68-8452-9457a4de32df"
# 📬 Correspondence Log — the J28 ledger's Notion projection. Read for ONE
# question only: has this renter texted us today? A `No GTG` chip means "nobody
# has confirmed with this renter"; an inbound text is that confirmation arriving,
# and the chip should clear on it without waiting for a human to flip GTG.
# Only artist page-ids and timestamps are read — never `Content`, never a
# Subject. See fetch_inbound_texts().
CORRESPONDENCE_DS = "defec0fd-e817-4191-8a50-b69ad3e72b59"
# Mediums that count as "they texted us". Email is deliberately excluded: an
# inbound email is not what Junyan asked to clear the chip on (2026-08-25).
TEXT_MEDIUMS = ("SMS", "WhatsApp", "RCS / Off-system text")

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
# alarm-mcp's /door-history — the websocket event ledger (canon
# studio-activity.md). NAMED keypad arm/disarm at second precision, plus the
# coverage verdict the arm-state ledger cannot give. Same service and bearer
# as /arm-history, so no new secret: derive the URL unless overridden.
DOOR_HISTORY_URL = (os.environ.get("DOOR_HISTORY_URL") or "").strip() or (
    ARM_HISTORY_URL.replace("/arm-history", "/door-history")
    if "/arm-history" in ARM_HISTORY_URL else "")
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


# `[Facilitator: Name]` on a booking title names the person actually running
# the session — an authorized user or a stand-in who attends WITHOUT the
# account holder (SOP 👫 Request for Additional Facilitator / Authorized User,
# Cases B and C write it). When it is present that person, not the booker, is
# who the panel should see at the keypad: Junyan, 2026-09-02 — "if a
# facilitator is indicated then we will select that one instead of the person
# who booked". Without it, an authorized user's disarm was silently credited
# to the account holder (Nina Li / Krista Flynn, Studio 901).
RE_FACILITATOR = re.compile(r"\[\s*facilitator\s*:\s*([^\]]+?)\s*\]", re.I)


def facilitator_of(title):
    m = RE_FACILITATOR.search(title or "")
    return m.group(1).strip() if m else None


def expected_name(e):
    """Whose name the keypad event should carry: the facilitator when one is
    named on the booking, else the booking title (name tokens and all — the
    pre-facilitator behaviour, unchanged)."""
    return e.get("facilitator") or e.get("who") or ""


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
    'Stefan (Studio 901 (Elements))'.

    TITLE-ONLY, and deliberately narrow. It is the fallback detector, kept
    because it is the only one that works on a RECURRING staff block (Skedda's
    hold feed does not expand series). Anything one-off is caught properly by
    mark_skedda_holds.

    DO NOT WIDEN THIS LIST, and do not relax it to a substring search. Tried
    and reverted 2026-09-03: Junyan asked for "if any of our staff names appear
    on there", and the live Artist Database says that class of rule is unsafe —
    "Rita Stefan [Skedda]" is a One-Off RENTER whose surname is Stefan, and
    "ela" sits inside Gabriela, Mihaela, Mariadela, Daniela, Pamela, Kaela and
    Elaine. Marking a renter as staff pulls them off the board AND out of every
    FBS message lane, which is worse than the bug it fixes. Junyan's ruling on
    seeing the collisions: "let's drop off the staff name rule then as that can
    definitely be an issue."

    The `clean`-or-two-words condition is what keeps even THIS list safe: it
    fires on "Stefan", "Stefan clean 693" and "Donny (Studio 527)", never on a
    renter whose booking title merely starts with one of those words.
    """
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
            # An UNTITLED Skedda hold syncs as the literal word "Unavailable"
            # and is dropped outright — there is nothing to show. A hold WITH a
            # title stays on the board (the room is genuinely occupied) and is
            # marked staff further down; see mark_skedda_holds.
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
                "facilitator": facilitator_of(summary),
                # Two staff detectors. is_cleaning reads the TITLE and is the
                # only cover for a RECURRING block; mark_skedda_holds (below)
                # reads Skedda's own UNAVAILABLE type and catches everything
                # one-off whatever it is called. Neither alone is enough.
                "kind": "staff" if is_cleaning(summary) else "booking",
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
# Open/Close the Studio) and any future placeholder title — never renders as
# staff. The unassigned placeholders surface separately as OPEN SHIFTS on the
# Shifts tab (fetch_open_shifts below), never as coverage.
STAFF_ROSTER = ("junyan", "kyjah", "ela", "stefan", "donny")


def _staff_identity(summary):
    low = summary.lower().strip()
    if "meeting" in low or "payroll" in low or "ela morning" in low:
        return None
    m = re.search(r"^\s*([A-Za-z][A-Za-z'’-]*)\s+.*?\b(FBS|Monitoring|Monitor|Viewing)\b", summary, re.I)
    if not m or m.group(1).lower() not in STAFF_ROSTER:
        return None
    role = m.group(2)
    role = {"monitor": "Monitoring"}.get(role.lower(), role[0].upper() + role[1:])
    return m.group(1), role


def parse_staff_row(summary, dtstart, dtend, base_day):
    identity = _staff_identity(summary)
    if not identity:
        return None
    name, role = identity
    return {"name": name, "role": role,
            "start": decimal_hours(dtstart, base_day),
            "end": decimal_hours(dtend, base_day)}


# Open (claimable) shifts — the unassigned placeholder blocks staff put on the
# Staff Scheduling calendar. Claiming itself happens on the ☑️ Open Shifts
# Notion board; the public page only ADVERTISES the gaps. The event description
# carries renter names and [Paid] markers, so only the studio number may be
# read out of it — nothing else from a description ever reaches the page.
OPEN_SHIFT_ROLES = (
    (re.compile(r"^\s*need\s+fbs\b", re.I), "FBS"),
    (re.compile(r"^\s*need\s+monitor(?:ing)?\b", re.I), "Monitoring"),
    (re.compile(r"^\s*(?:need\s+)?studio\s+viewing\b", re.I), "Viewing"),
    (re.compile(r"^\s*open\s*/\s*close\b|^\s*open\s+the\s+studio\b", re.I), "Open/Close"),
    (re.compile(r"^\s*close\s+the\s+studio\b", re.I), "Close"),
)
RE_SHIFT_STUDIO = re.compile(r"\bstudio\s+(\d{3}[AB]?)\b", re.I)
OPEN_SHIFT_LOOKAHEAD_DAYS = 3   # matches how far out The Planner posts shifts


def parse_open_shift(summary, description, dtstart, dtend, base_day):
    for rx, role in OPEN_SHIFT_ROLES:
        if rx.search(summary or ""):
            m = RE_SHIFT_STUDIO.search(description or "")
            day = dtstart.astimezone(TZ).date() if dtstart.tzinfo else dtstart.date()
            return {"role": role,
                    "studio": m.group(1).upper() if m else None,
                    "day_offset": (day - base_day).days,
                    "start": decimal_hours(dtstart, day),
                    "end": decimal_hours(dtend, day)}
    return None


def parse_staff_assignment(summary, description, dtstart, dtend, base_day):
    """Private lookahead shape used only to prove a placeholder was claimed.

    Staff descriptions may carry renter/payment text, so the same public-data
    rail as open shifts applies: retain only the studio number. The returned
    rows never enter DATA; they are consumed by reconcile_open_shifts().
    """
    identity = _staff_identity(summary or "")
    if not identity:
        return None
    name, role = identity
    local_start = dtstart.astimezone(TZ) if dtstart.tzinfo else dtstart
    day = local_start.date()
    m = RE_SHIFT_STUDIO.search(description or "")
    return {"name": name, "role": role,
            "studio": m.group(1).upper() if m else None,
            "day_offset": (day - base_day).days,
            "start": decimal_hours(dtstart, day),
            "end": decimal_hours(dtend, day)}


def _same_shift_window(open_shift, assignment):
    return (open_shift["day_offset"] == assignment["day_offset"]
            and open_shift["role"] == assignment["role"]
            and assignment["start"] <= open_shift["start"] + 1e-6
            and assignment["end"] >= open_shift["end"] - 1e-6)


def _open_shift_is_future(open_shift, now, base_day):
    local_now = now.astimezone(TZ) if now.tzinfo else now
    now_hour = ((local_now.date() - base_day).days * 24
                + local_now.hour + local_now.minute / 60)
    end_hour = open_shift["day_offset"] * 24 + open_shift["end"]
    return end_hour > now_hour


def reconcile_open_shifts(open_shifts, assignments, now, base_day):
    """Return only still-claimable placeholders.

    A claimed block must be covered by a rostered assignment with the same
    day, role and time. Studio is also required whenever both rows carry one.
    If either side lacks a studio, suppress only when the compatible open block
    is unique; simultaneous gaps must stay visible rather than being guessed.
    """
    current = [o for o in open_shifts if _open_shift_is_future(o, now, base_day)]
    out = []
    for open_shift in current:
        claimed = False
        for assignment in assignments:
            if not _same_shift_window(open_shift, assignment):
                continue
            if open_shift.get("studio") and assignment.get("studio"):
                if open_shift["studio"] == assignment["studio"]:
                    claimed = True
                    break
                continue
            compatible = [o for o in current if _same_shift_window(o, assignment)]
            if len(compatible) == 1:
                claimed = True
                break
        if not claimed:
            out.append(open_shift)
    return out


def fetch_open_shifts(ics_map, win_start, base_day, now=None):
    """Unassigned placeholders from the Staff calendar, today + the posting
    lookahead. Separate fetch because the board window is today-only, while
    claimable shifts are posted days ahead. Own fetch also means an outage
    here costs the Shifts list, never the board (caller soft-fails)."""
    url = ics_map.get("Staff")
    if not url:
        return []
    win_end = win_start + timedelta(days=OPEN_SHIFT_LOOKAHEAD_DAYS + 1)
    open_shifts, assignments = [], []
    for ev in fetch_ics(url, win_start, win_end):
        if ev.get("cancelled"):
            continue
        o = parse_open_shift(ev["summary"], ev.get("description"),
                             ev["dtstart"], ev["dtend"], base_day)
        if o and o["day_offset"] >= 0:
            d = base_day + timedelta(days=o["day_offset"])
            o["day"] = ("Today" if o["day_offset"] == 0 else
                        "Tomorrow" if o["day_offset"] == 1 else
                        d.strftime("%a %b %-d"))
            open_shifts.append(o)
            continue
        a = parse_staff_assignment(ev["summary"], ev.get("description"),
                                   ev["dtstart"], ev["dtend"], base_day)
        if a and a["day_offset"] >= 0:
            assignments.append(a)
    out = reconcile_open_shifts(open_shifts, assignments,
                                now or datetime.now(TZ), base_day)
    out.sort(key=lambda o: (o["day_offset"], o["start"]))
    return out


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


# 🚧 Studio Holds — Skedda's UNAVAILABLE blocks (type 2), published to Notion by
# the deterministic holds lane in dc-canon `services/event-gate/holds_sweep.py`.
#
# WHY NOTION AND NOT SKEDDA DIRECTLY. A Skedda read needs the venue session
# cookie, and this board builds in GitHub Actions, which holds no GCP
# credential — the workflow's `Read Skedda cookie` step has been SKIPPED on
# every run since it was added on 2026-08-15, so `skedda_names` has never once
# answered in production. The event-gate service runs inside the
# `danceannex-skedda` project and reads that cookie with its own identity, no
# key. So it asks, Notion carries, and this builder reads it with the
# NOTION_TOKEN it already has. No new credential on either side (Junyan,
# 2026-09-03: "I don't know how to get skedda credentials easily as they don't
# have an api key").
HOLDS_DS = "f6210728-6396-4047-9bf1-10971dbcbeb6"


def fetch_studio_holds(token, base_day):
    """Today's staff blocks from 🚧 Studio Holds, shaped like Skedda rows.

    Returns the same dicts mark_skedda_holds already takes, so the matching
    rule is identical whichever source answered.
    """
    rows = _notion_query(token, HOLDS_DS, {
        "filter": {"property": "Hold Date", "date": {"equals": base_day.isoformat()}},
        "page_size": 100,
    })
    out = []
    for row in rows:
        p = row.get("properties", {})
        start, end = norm_hm(_prop_text(p.get("Start Time"))), \
            norm_hm(_prop_text(p.get("End Time")))
        studio = (_prop_text(p.get("Studio")) or "").strip()
        if not (start and end and studio):
            continue
        out.append({"studio": studio, "title": _prop_text(p.get("Title")),
                    "user_name": None, "is_hold": True,
                    "_hm": (start, end)})
    return out


# A Skedda hold and its Google-mirror card are the SAME slot, written by one
# sync, so they agree to the minute. Allow a couple of minutes for clock skew
# and nothing more: a loose window is exactly the mistake join_notion made.
HOLD_SLOT_TOLERANCE = 2 / 60   # decimal hours


def mark_skedda_holds(events, rows, base_day):
    """Flip cards that are Skedda UNAVAILABLE blocks to kind "staff".

    WHY THIS IS NOT A TITLE CHECK. The builder reads the Google mirror, which
    carries a hold's title and nothing that says "this is a hold". The only
    title-shaped signal was `"unavailable" in summary` in build_calendar_events,
    which catches an untitled block and nothing else. On 2026-09-03 a 509B hold
    titled "Matterport 360° panorama capture — Studios 509A & 509B" (Skedda
    booking 119939698, type 2, no venueuser) rendered as a 13:00 booking, and
    join_notion then handed it Akira Huang's 14:15 Monitor Only row — 1.25 h
    away, inside the old 2 h window. The renter lost her tier, her HTA and both
    dispatch pills to a camera on a tripod. Skedda knows what its own blocks
    are; ask Skedda.

    Match is deliberately exact — same studio, same start AND same end within
    HOLD_SLOT_TOLERANCE, one candidate only. An ambiguous match leaves the card
    a booking: over-marking hides a real renter from the board, which is the
    worse failure. Returns the number of cards flipped.
    """
    holds = [r for r in rows if r.get("is_hold")]
    if not holds:
        return 0
    for r in holds:
        if r.get("_hm"):
            # A Notion row carries wall-clock HH:MM. The board's day is the
            # OPERATING day (05:00 → 05:00), so a 02:00 block sits at 26.0 on
            # the same card grid — lift it the way decimal_hours would.
            r["_start"], r["_end"] = (_time_to_decimal(v) for v in r["_hm"])
            if r["_start"] < 5:
                r["_start"] += 24
            if r["_end"] < 5:
                r["_end"] += 24
        else:                                 # a Skedda row carries datetimes
            r["_start"] = decimal_hours(r["start"], base_day)
            r["_end"] = decimal_hours(r["end"], base_day)
        # A block ending at or before it starts crossed midnight.
        if r["_end"] <= r["_start"]:
            r["_end"] += 24

    marked = 0
    for e in events:
        if e.get("kind") != "booking":
            continue
        hits = [r for r in holds
                if r["studio"] == e["studio"]
                and abs(r["_start"] - e["start"]) <= HOLD_SLOT_TOLERANCE
                and abs(r["_end"] - e["end"]) <= HOLD_SLOT_TOLERANCE]
        if len(hits) == 1:
            e["kind"] = "staff"
            marked += 1
    return marked


def enrich_names_from_skedda(events, win_start, win_end):
    """One Skedda read, two jobs: name the nameless, and mark the staff blocks.

    NAMES — fill in renter names the ICS feeds omit. Touches ONLY nameless
    platform titles, and only on an unambiguous match: one studio, one
    overlapping Skedda booking. Two candidates means the read cannot tell them
    apart, and a confidently wrong name at the door is worse than an honest
    "Giggster Booking".

    STAFF — see mark_skedda_holds.

    Any failure is soft. Names degrade to the ICS title as before; holds degrade
    to the title-only `is_cleaning` detector, i.e. exactly the pre-2026-09-03
    behaviour. The board is never worth losing over either.
    """
    targets = [e for e in events
               if e["kind"] == "booking" and _is_nameless_title(e.get("who"))]
    try:
        rows = skedda_names.fetch_named_bookings(win_start, win_end)
    except skedda_names.SkeddaUnavailable as e:
        return events, (f"Skedda lookup skipped ({e}); platform titles left as-is "
                        f"and staff blocks fall back to title detection.")
    except Exception as e:  # noqa: BLE001 — never let enrichment fail the build
        return events, f"Skedda lookup failed ({type(e).__name__}: {e})."

    base_day = win_start.date()
    for r in rows:
        r["_start"] = decimal_hours(r["start"], base_day)
        r["_end"] = decimal_hours(r["end"], base_day)

    # Staff first: a hold is not a renter, so it must not then be renamed as one.
    marked = mark_skedda_holds(events, rows, base_day)

    filled = 0
    for e in targets:
        if e["kind"] != "booking":
            continue   # just marked as staff
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

    notes = []
    if filled:
        notes.append(f"Skedda supplied {filled} renter name(s) the ICS feeds omitted.")
    if marked:
        notes.append(f"Skedda marked {marked} card(s) as staff (UNAVAILABLE blocks).")
    return events, (" ".join(notes) or None)


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


def _relation_id(prop):
    """First related page id from a relation property, dashes stripped."""
    if not prop or prop.get("type") != "relation":
        return None
    rel = prop.get("relation") or []
    if not rel:
        return None
    return (rel[0].get("id") or "").replace("-", "") or None


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
        # ⚠ DO NOT read `Alarm Code` here. It is a rollup of the renter's real
        # PIN and this board is a PUBLIC GitHub Pages site. The missing-code chip
        # that used to read it (as a boolean, never the string) was removed
        # 2026-08-25 — see the tombstone above join_notion(). Nothing on this
        # board needs the code, so the safest read is no read at all.
        out.append({
            "id": row.get("id"),
            "artist": _relation_id(p.get("🎨 Artist Database")),
            "status": (_prop_text(p.get("Booking Status")) or "").strip(),
            "studio": studio,
            "start": _prop_text(p.get("Start Time")),
            "end": _prop_text(p.get("End Time")),
            "tier": tier,
            "gtg": gtg if tier else True,
            "hta": _prop_text(p.get("HTA")),
            "ava": _prop_text(p.get("AVA")),
            "eob": _prop_text(p.get("EOB")),
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


# How far a Notion booking row may sit from its calendar card and still be the
# same booking. BOTH ends must agree.
#
# This was 2.0 hours, on the START only, and that is the whole of the
# 2026-09-03 Matterport bug: a 13:00–14:00 staff hold in 509B was 1.25 h from
# Akira Huang's 14:15–15:45 Monitor Only row, won it on the greedy earliest-card
# pass, and left the actual renter with no tier, no HTA and two false MISSING
# dispatch pills. Both sides of this join are generated from the same Skedda
# booking, so they agree to the MINUTE in practice — every matched card on the
# 2026-09-03 board lined up exactly on both ends. Half an hour is already
# generous; the old window was wide enough to swallow the neighbouring slot.
#
# Failure mode if this is too tight: a booking loses its tier chip and its
# dispatch pills. That is honest under-reporting. Too loose puts a renter's tier
# on someone else's card AND strips it from theirs — a lie in two places.
NOTION_SLOT_TOLERANCE = 0.5   # decimal hours, each end independently


def _notion_span(row):
    """(start, end) in decimal hours, end pushed past 24 for a cross-midnight row."""
    rs, re_ = _time_to_decimal(row.get("start")), _time_to_decimal(row.get("end"))
    if rs is None or re_ is None:
        return None
    return rs, (re_ + 24 if re_ <= rs else re_)


def _slot_gap(rs, re_, e_start, e_end):
    """Total start+end distance, or None when either end is out of tolerance.

    The card's clock is the board's OPERATING day (05:00 → 05:00 next), so a
    00:30 booking sits at 24.5 while Notion still says "00:30". Try the row in
    both day frames and keep the better fit — never let a clock frame decide a
    booking has no tier.
    """
    best = None
    for shift in (0, 24):
        start_gap, end_gap = abs(rs + shift - e_start), abs(re_ + shift - e_end)
        if start_gap > NOTION_SLOT_TOLERANCE or end_gap > NOTION_SLOT_TOLERANCE:
            continue
        total = start_gap + end_gap
        if best is None or total < best:
            best = total
    return best


def join_notion(events, notion_rows):
    used = [False] * len(notion_rows)
    for e in events:
        if e["kind"] != "booking":
            continue   # a staff block must never absorb a booking's tier row
        best, best_i, best_gap = None, -1, 1e9
        for i, r in enumerate(notion_rows):
            if used[i] or r["studio"] != e["studio"]:
                continue
            span = _notion_span(r)
            # A row with no usable time cannot be placed; it is only a candidate
            # when it is the studio's single unclaimed row, which the caller
            # cannot know here. Skip it rather than default it onto this card.
            if span is None:
                continue
            rs, re_ = span
            gap = _slot_gap(rs, re_, e["start"], e["end"])
            if gap is None:
                continue
            if gap < best_gap:
                best, best_i, best_gap = r, i, gap
        if best:
            used[best_i] = True
            e["tier"], e["gtg"], e["hta"] = best["tier"], best["gtg"], best["hta"]
            e["_ava_status"], e["_eob_status"] = best.get("ava"), best.get("eob")
            e["_board_disarmed"] = best["board_disarmed"]
            e["_board_armed"] = best["board_armed"]
            e["_notion_id"] = best["id"]
            e["_artist_id"] = best.get("artist")
            e["_board_status"] = best["status"]
    return events


# REMOVED 2026-08-25 — `apply_missing_codes()`, the missing-alarm-code chip.
#
# It never worked. Added 2026-08-07; from 2026-08-08 onward its own all-codeless
# suppression guard fired on 1,924 consecutive rebuilds, so the chip was never
# once rendered and every rebuild carried a warning that was not true. The codes
# were there the whole time (656 of 855 non-closed artists hold one); the builder
# could not see them through the `Alarm Code` rollup.
#
# It is not coming back, because it was the WEAKER of two checks. The Doorman
# verifies every upcoming renter against the LIVE Alarm.com panel on its ~06:00
# pass, 7 days ahead, and raises an `Access / PIN` row with days of lead — real
# door state, not a Notion record of it. This read a second-hand copy and would
# have disagreed with the panel sooner or later. Junyan's 2026-08-15 ruling
# ("no duplicates, all under the Doorman") already settled which one survives.
#
# If the board should show missing access again, RENDER THE DOORMAN'S ROWS —
# flag_access_gaps() reads ✅ Actions to Perform and pins them to today's
# cards. Do not re-derive the answer here.

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
#   credentials in conflict — excluded by name on Junyan's standing directive
#     (canon house-rules.md, 2026-08-01, no expiry); alarm-monitor's own filer
#     has skipped it since 2026-06.
#     NOT duplicate PINs. That was this comment's original claim and the
#     2026-08-19 investigation disproved it: 174 panel users hold 174 distinct
#     codes (fully paginated), no cloud-vs-panel drift, no reserved defaults,
#     and capacity is not close (IQ Panel 2/4 hold 242). All four instances are
#     ONE account-wide event at 2026-07-10 14:59:42 UTC. Per Alarm.com's KB it
#     is an Access Control credential duplicate — a badge or card, not a PIN —
#     and every credentials endpoint 404s here, so it is UI-only work.
#     Full write-up: dc-canon library/doorman-access-lifecycle.md, section
#     '"Credentials In Conflict" — what it is NOT'. Do not re-run those tests.
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


def fetch_door_events(win_start):
    """NAMED arm/disarm events from alarm-mcp's /door-history (the websocket
    event ledger — canon studio-activity.md).

    This is what replaced the dead ADT/TELUS email feed: keypad arm/disarm
    with the actor's name and a stable contact id, at second precision. It
    plays the role the emails played in enrich_arm_names() — the NAME source
    laid over the panel ledger's timeline — and more, since its events are
    also better-timed than the panel ledger's minute tick (509 events ran 1-6
    minutes late on 2026-08-20; the websocket saw the same events live).

    Returns (events, coverage_gaps):
      events: the arm_events shape apply_arm_events() expects, plus ts/source.
        * keypad arm/disarm  -> named, remote=False — attributable to a renter.
        * remote arm/disarm  -> remote=True. actor_source "remote" proves a
          button was pressed somewhere, not that anyone was in the building
          (canon: never attribute a remote event to a renter).
        * door/motion events are NOT returned — the board's stream renders
          arm state; presence-without-identity belongs to the departure
          verdict, which this builder does not compute (yet).
      covers_since: ISO instant the ledger's memory starts. A question about
        anything earlier is unanswerable, not clean — the ledger prunes at 72h.
      coverage_gaps: [{since, until}] ISO pairs where the listener was NOT
        recording. "No event in a gap" means "was not listening", never
        "nobody came" — the caller must not let a window containing a gap
        render as a no-show. Same ruling that retired the ADT feed.

    Raises on any failure, like fetch_arm_history — the caller records which
    feeds answered, and a partial answer that looks complete is the one thing
    this file must never produce.
    """
    r = requests.get(DOOR_HISTORY_URL,
                     headers={"Authorization": f"Bearer {ARM_HISTORY_TOKEN}"},
                     params={"since": win_start.astimezone(timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%S+00:00")},
                     timeout=30)
    r.raise_for_status()
    payload = r.json()
    out = []
    for ev in payload.get("events") or []:
        studio = norm_studio_label(ev.get("studio"))
        kind = {"disarm": "arrival", "arm": "departure"}.get(ev.get("kind"))
        if not studio or not kind:
            continue                     # door/motion — not arm state
        try:
            at = datetime.fromisoformat(str(ev["at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        local = at.astimezone(TZ)
        if local < win_start:
            continue
        remote = ev.get("actor_source") != "keypad"
        out.append({
            "studio": studio,
            "name": "Studio (remote)" if remote else (ev.get("actor") or ""),
            "time": f"{local.hour:02d}:{local.minute:02d}",
            "kind": kind,
            "remote": remote,
            "ts": int(at.timestamp() * 1000),
            "source": "doors",
        })
    gaps = []
    for g in payload.get("gaps") or []:
        since, until = _parse_iso_utc(g.get("since")), _parse_iso_utc(g.get("until"))
        if since and until and until.astimezone(TZ) >= win_start:
            gaps.append({"since": g["since"], "until": g["until"]})
    return out, gaps, payload.get("covers_since")


def reconcile_mail_against_doors(mail_events, door_events, door_gaps,
                                 covers_since, tolerance_s=300):
    """Cross-check the ADT/TELUS email feed against the websocket ledger.

    WHY THIS EXISTS. The email feed was ruled permanently dead on 2026-08-20
    and is delivering again — ~200 named messages in the seven days to
    2026-08-25, agreeing with the websocket to the second (509A disarmed by
    Ivanka Moskaliuk, 2026-08-24 23:05:22Z, both sources). That makes it a
    genuine second witness to the one fact the board cannot re-derive: WHO.

    It is a CHECK, never a layer. Two reasons nothing may depend on it:
    it carries no door open/close — so it cannot recover what a socket blink
    actually loses, which is the departure — and it throttles itself without
    warning ("High activity ... blocked or limited for up to 24 hours", four
    such notices on 2026-08-24/25 alone), precisely when activity is high.
    A backstop that disappears under load is not a backstop.

    WHAT IT ANSWERS. For every named arm/disarm the mail reports, did the
    websocket see it too? Three outcomes, and only one is a finding:

      matched  — both saw it. The expected case.
      in_gap   — mail saw it, the socket did not, and a RECORDED gap covers
                 that moment. Not a defect: this is the gap machinery being
                 honest, and it is the only routine proof that it works.
      missed   — mail saw it, the socket did not, and the socket claimed to
                 be listening. That is a hole in a window reported as clean,
                 which is the failure mode the whole coverage guard exists to
                 prevent. Loud.

    Deliberately one-directional. Door-only events are NOT reported: the
    socket legitimately sees more than the mail (remote arms, door, motion),
    and mail silence is as likely to be a throttle as a miss, so that
    direction is all noise. Mail before `covers_since` is skipped for the
    same reason — the ledger cannot speak for time it never held.

    Tolerance is 5 minutes, far wider than the observed 0 s, because a false
    "the socket missed one" costs an investigation while a missed detection
    costs one more pass. Errs quiet, on purpose."""
    floor = _parse_iso_utc(covers_since)
    gaps = [(_parse_iso_utc(g["since"]), _parse_iso_utc(g["until"]))
            for g in door_gaps or []]
    gaps = [(a, b) for a, b in gaps if a and b]
    matched, in_gap, missed = 0, 0, []
    for m in mail_events or []:
        if m.get("remote") or not m.get("name"):
            continue                     # only named, human-attributable events
        ts = m.get("ts")
        if not ts:
            continue
        when = datetime.fromtimestamp(ts / 1000, timezone.utc)
        if floor and when < floor:
            continue                     # older than the ledger's memory
        twin = any(d["studio"] == m["studio"] and d["kind"] == m["kind"]
                   and abs((d.get("ts") or 0) - ts) <= tolerance_s * 1000
                   for d in door_events or [])
        if twin:
            matched += 1
        elif any(a <= when <= b for a, b in gaps):
            in_gap += 1
        else:
            missed.append(f"{m['studio']} {m['kind']} {m.get('time')} "
                          f"({m.get('name')})")
    return {"matched": matched, "inGap": in_gap,
            "missed": missed[:12], "missedCount": len(missed)}


def _parse_iso_utc(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


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


def apply_arm_events(events, arm_events, now_dec=None, alerts=None):
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
                    and _name_match(expected_name(e), a["name"]):
                a["claimed"] = True
                (arrivals if a["kind"] == "arrival" else departures).append(
                    (a["t"], a["time"], a["name"]))
        if arrivals:
            _, e["arrived"], e["arrived_by"] = min(arrivals)
        if departures:
            _, e["departed"], e["departed_by"] = max(departures)

    # pass 2 — unclaimed events for still-unmatched bookings.
    #
    # Each event is spent ON ONE BOOKING. The first version looped over
    # bookings and never marked events claimed, so one nameless disarm was
    # credited to every in-window booking: on 2026-08-20 Anneka's 13:26
    # arrival in 509A rendered as HER arrival AND as Vanessa Zavatti's, whose
    # 2-minute viewing the panel tick had missed entirely — a phantom
    # "in 13:29" against a booking nobody attended in that room.
    #
    # The assignment is event-centric: each unclaimed event goes to the
    # candidate booking whose OWN INTERVAL it lies closest to (distance 0 when
    # inside [start, end]). Window-centre distance gets the same case wrong —
    # 13:29 sits nearer the centre of a 13:00-13:15 viewing than of a
    # 13:30-15:30 booking, but it is 1 minute before the latter's start and
    # 14 minutes after the former's end. Losers stay honestly blank.
    def _iv_dist(e, t):
        return max(e["start"] - t, t - e["end"], 0.0)

    # Arrivals ascending (earliest disarm is the arrival), departures
    # DESCENDING (last arm is the departure — a mid-booking arm/disarm pair
    # must not read as leaving), preserving pass 1's earliest/latest rule.
    ordered = sorted((a for a in timed if a["kind"] == "arrival"),
                     key=lambda a: a["t"]) + \
              sorted((a for a in timed if a["kind"] == "departure"),
                     key=lambda a: -a["t"])
    for a in ordered:
        if a["claimed"]:
            continue
        want = "arrived" if a["kind"] == "arrival" else "departed"
        cands = [e for e in events
                 if e["studio"] == a["studio"] and not e[want] and in_window(e, a["t"])]
        if not cands:
            continue
        best = min(cands, key=lambda e: (_iv_dist(e, a["t"]), e["start"]))
        best[want] = a["time"]
        best[want + "_by"] = a["name"]
        a["claimed"] = True

    # A departure that precedes the arrival is a mis-claimed neighbour's arm
    # (e.g. the main booking's 13:10 arm landing on its own 13:18 extension row).
    for e in events:
        arr, dep = _time_to_decimal(e.get("arrived")), _time_to_decimal(e.get("departed"))
        if arr is not None and dep is not None and dep < arr:
            e["departed"] = None
            e["departed_by"] = None

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
            if in_window(e, a["t"]) and _name_match(expected_name(e), a["name"]):
                e["wrong_studio"] = {"studio": a["studio"], "at": a["time"]}
                a["claimed"] = True
                break

    # pass 4 — the studio was already open.
    #
    # The panel can only witness an arrival as a DISARM. On a tight transition
    # the outgoing renter often walks out without arming; the panel stays
    # disarmed, the incoming renter has nothing to disarm, and their arrival is
    # physically unobservable — which rendered as a red "no arrival" on someone
    # standing in the room (2026-08-26: Fabio Hernandez, 509A 20:15, right
    # behind Daniel's 19:00-20:00). A no-show and an unobservable arrival need
    # different treatment: only the first is a billing question.
    #
    # When the LAST panel-state change at or before start(+grace) in the
    # booking's studio is a disarm, the door was open when the booking began —
    # assume an on-time arrival, marked `assumed` so the page renders it as an
    # inference, never as a measured time. Remote and already-claimed events
    # count here: panel STATE is what matters, not who caused it. Only bookings
    # already past start get the assumption — a future booking must not read
    # as "in".
    # A second unobservable shape, found the same night: the outgoing renter
    # arms WITH THE DOOR SENSOR BYPASSED (2026-08-26: Ishfaaq armed 509A 19:06,
    # "Door was bypassed at 7:07 PM"), so the next renter opens the door
    # without tripping the panel and never needs the keypad. Armed panel, no
    # disarm, someone inside. `assumed` therefore carries the REASON — "open"
    # (nobody armed) or "bypass" (armed around a sensor) — and the pages render
    # both as an amber warning: an arrival we inferred because the alarm was
    # not used correctly, never a clean measured time.
    grace = 10 / 60
    day_frame = lambda t: t + 24 if t < 5 else t   # 01:30 belongs to the day's tail
    if now_dec is None:                            # injectable for tests
        _n = datetime.now(TZ)
        now_dec = day_frame(_n.hour + _n.minute / 60)
    panel_timed = []
    for a in arm_events:
        t = _time_to_decimal(a["time"]) if a.get("time") else None
        if t is not None:
            panel_timed.append((a["studio"], day_frame(t), a["kind"], a.get("remote")))
    bypasses = []
    for b in (alerts or []):
        if b.get("stage") == "BYPASS":
            t = _time_to_decimal(b.get("time")) if b.get("time") else None
            if t is not None:
                bypasses.append((b["studio"], day_frame(t)))
    for e in events:
        if e["kind"] != "booking" or e.get("arrived") or e.get("wrong_studio"):
            continue
        if now_dec <= e["start"] + grace:
            continue
        before = [(t, k, r) for s, t, k, r in panel_timed
                  if s == e["studio"] and t <= e["start"] + grace]
        # The deciding event must be a KEYPAD disarm — a human physically in
        # the room. A remote disarm opens the panel but proves no presence
        # (canon: never attribute a remote event to a renter), so it never
        # licenses the assumption — while a remote ARM still blocks it, since
        # panel state is armed either way.
        last = max(before) if before else None
        reason = None
        if last and last[1] == "arrival" and not last[2]:
            reason = "open"
        elif last and last[1] == "departure":
            # Armed, but with the door bypassed at/after the arming (the notice
            # trails the arm email by a minute) and before this booking began:
            # the door opens silently, so the disarm this pass waits for can
            # never come.
            if any(s == e["studio"] and last[0] - grace <= t <= e["start"] + grace
                   for s, t in bypasses):
                reason = "bypass"
        if reason:
            e["arrived"] = f"{int(e['start']) % 24:02d}:{round(e['start'] % 1 * 60):02d}"
            e["assumed"] = reason

    # pass 5 — who actually keyed in.
    #
    # The card used to carry arrival/departure TIMES only; the keypad name
    # never left the Alarms tab. So an authorized user, a stand-in, or staff
    # closing out rendered as the booker's own clean green "in"/"out". Now the
    # actor rides along, and is flagged `foreign` when their name shares no
    # token with the expected person (the facilitator if named, else the
    # booking title). A nameless event (ledger backstop) flags nothing.
    for e in events:
        if e.get("kind") != "booking":
            continue
        want = expected_name(e)
        for k in ("arrived", "departed"):
            who = e.get(k + "_by")
            e[k + "_foreign"] = bool(who) and not _name_match(want, who)
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


# Manual operational truth wins over report-derived heartbeats.  These lanes
# were confirmed missing by Junyan on 2026-09-04; The Responder in particular
# still had a fresh attributed report, which is not proof that its intended
# runtime exists.  Remove a lane only after its runtime is restored and
# independently verified.  Paused/Not-reporting below remain explicit operator
# overrides, so they intentionally take precedence over this incident list.
CONFIRMED_MISSING_RUNS = {
    "The Loop",
    "The Responder",
    "The Custodian",
}


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
    if r.get("run") in CONFIRMED_MISSING_RUNS:
        return "crit", "🔴 MISSING"
    last = _last_checkin(r)
    stale = r["stale_after"] or 120
    cadence = (r["cadence"] or "").lower()
    ws, we = r["window_start"], r["window_end"]
    if ws is not None and we is not None and not (ws <= now.hour < we):
        # Off-hours suppresses the normal overnight age, not a pass that was
        # already missing when the active window closed.  The old early return
        # hid a dead lane every night: on 2026-09-04 it turned The Loop, The
        # Responder and The Custodian grey even though none had completed the
        # latest active window.
        #
        # Compare with the end of the most recently closed window.  A healthy
        # last pass may land up to `stale` minutes before that boundary; an
        # older heartbeat means the lane missed while it was supposed to be
        # running and remains red until it checks in again.
        if cadence not in ("weekly", "monthly"):
            window_end = now.replace(hour=we, minute=0, second=0, microsecond=0)
            if now.hour < ws:
                window_end -= timedelta(days=1)
            if last is None or last < window_end - timedelta(minutes=stale):
                return "crit", "🔴 MISSING"
        return "plain", "Off-hours"
    if last is None:
        return "crit", "Never checked in"
    age_min = (now - last).total_seconds() / 60
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
    first is the build refusing to publish money-shaped classes at all (only
    ⚠ report headlines pass through here since the Issues tab was removed)."""
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


# THE ISSUES TAB IS GONE — deleted 2026-08-25 on Junyan's call, one day after
# it was built out ("these issues are not really useful"). What it tried to be
# is served better elsewhere: forward door gaps live in the Doorman's report
# and ✅ Actions to Perform; run health is the Robots tab; and the one thing
# this app genuinely needs to say about access — THIS renter, TODAY, cannot
# get in — now renders as a red pill on the booking card itself, where eyes
# actually are during a shift. fetch_issues(), its deadline/housekeeping
# machinery and the tab in both templates went with it.

# The sweep writes its keys in TWO shapes, and both must be read or the row
# silently degrades to a name-only match against the whole board:
#
#   [sweep:509B:2026-08-24T14:00:access]                        studio + date
#   [sweep:access:<artist-page-id>:2026-08-31:access]           artist + date
#
# The second shape is deliberately NOT studio-scoped ("one text covers every
# room they hold that day"), so it is matched on the DATE, never the studio.
RE_SWEEP_KEY = re.compile(r"\[sweep:(\w+):(\d{4}-\d{2}-\d{2})")
RE_SWEEP_ARTIST_KEY = re.compile(
    r"\[sweep:access:([0-9a-fA-F-]{32,36}):(\d{4}-\d{2}-\d{2})")


def fetch_open_access_rows(token):
    """Open Access / PIN rows → [{text, artist}] for flag_access_gaps().

    `text` is the row TITLE only and `artist` is a Notion page id — no notes, no
    bodies, no codes. Neither reaches the payload; only the boolean does.
    """
    rows = _notion_query(token, ACTIONS_DS, {
        "filter": {"and": [
            {"property": "Status", "select": {"equals": "Pending Review"}},
            {"property": "Type", "select": {"equals": "Access / PIN"}},
        ]},
        "page_size": 100,
    })
    out = []
    for r in rows:
        props = r.get("properties", {})
        out.append({"text": _prop_text(props.get("Request")) or "",
                    "artist": _relation_id(props.get("Artist"))})
    return out


def fetch_inbound_texts(token, since_dt):
    """Artist page-ids who have texted US since `since_dt`.

    Returns a SET OF IDS and nothing else — no subjects, no bodies, no phone
    numbers. `Content` on this DB is verbatim customer message text and must
    never be read here; this board is public.

    Window is the board's own day (05:00 → 05:00, same base_day the rest of the
    page runs on). Yesterday's chatter must not suppress today's chip.
    """
    rows = _notion_query(token, CORRESPONDENCE_DS, {
        "filter": {"and": [
            {"property": "Direction", "select": {"equals": "→ Us"}},
            {"property": "Date & Time", "date": {"on_or_after": since_dt.isoformat()}},
            {"or": [{"property": "Medium", "select": {"equals": m}}
                    for m in TEXT_MEDIUMS]},
        ]},
        "page_size": 100,
    })
    out = set()
    for r in rows:
        aid = _relation_id(r.get("properties", {}).get("Artist"))
        if aid:
            out.add(aid)
    return out


def apply_heard(events, texted_ids):
    """Mark bookings whose renter has texted us today.

    Junyan, 2026-08-25: "the No GTG needs to disappear the moment we see a text
    message from them." GTG itself stays truthful — it is the Notion board's
    value and the desk still has to flip it — this only decides whether the
    board WARNS about it. Matching is by Artist page-id (the FBS row and the
    ledger row both relate to the same Artist page), never by name."""
    for e in events:
        aid = e.get("_artist_id")
        e["heard"] = bool(aid and aid in texted_ids)
    return events


# Statuses that mean "a human still has to act on this row", in display order.
MSG_PENDING_STATUSES = ("Error", "Pending Review", "Ready to Send")

DISPATCH_TEMPLATES = {
    "ava_staff_available": "AVA",
    "ava_staff_unavailable": "AVA",
    "eob_booking_bending": "EOB",
}


def _dispatch_kind(properties):
    """AVA/EOB across current Template rows and legacy Host rows.

    The Host's older queue rows leave Template blank but carry stable
    `AVA-sweep-…` / `EOB-sweep-…` Message Codes. Ignoring that generation
    falsely turns already scheduled or sent work into MISSING.
    """
    kind = DISPATCH_TEMPLATES.get(_prop_text(properties.get("Template")))
    if kind:
        return kind
    code = (_prop_text(properties.get("Message Code")) or "").strip().upper()
    prefix = code.split("-", 1)[0]
    return prefix if prefix in ("AVA", "EOB") else None


def fetch_pending_messages(token):
    """Message Queue rows awaiting processing, projected for a PUBLIC page.

    Only the row TITLE (through redact()), channel, status, studio, template,
    raiser and created time cross. Message bodies, Reasoning, Reply To, email
    subjects and every rollup (Phone / Email / Alarm Code) must NEVER reach
    this projection — an HTA body carries access instructions, and the
    rollups are exactly the fields redact() exists to keep off the page."""
    filt = {"or": [{"property": "Status", "status": {"equals": s}}
                   for s in MSG_PENDING_STATUSES]}
    rows = _notion_query(token, MESSAGES_DS, {"filter": filt, "page_size": 100})
    out = []
    for r in rows:
        p = r.get("properties", {})
        created = ""
        try:
            ct = r.get("created_time")
            if ct:
                created = datetime.fromisoformat(ct.replace("Z", "+00:00")) \
                    .astimezone(TZ).strftime("%b %-d · %H:%M")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "code": redact(_prop_text(p.get("Message Code")) or "Untitled message"),
            "channel": _prop_text(p.get("Channel")),
            "status": _prop_text(p.get("Status")),
            "studio": _prop_text(p.get("Studio")),
            "raised_by": _prop_text(p.get("Raised by")),
            "created": created,
        })
    order = {s: i for i, s in enumerate(MSG_PENDING_STATUSES)}
    out.sort(key=lambda m: (order.get(m["status"], 9), m["created"]))
    return out


def fetch_message_dispatch(token, win_start, win_end):
    """Reduced AVA/EOB queue state for booking-card pills.

    The public payload receives only message kind, state and a Toronto-local
    clock time. Bodies, recipients, row ids and Artist ids remain internal.
    Scheduled rows are selected by their dispatch window even when the Host
    prepared them the night before. A short creation-time lookback also finds
    rows that exist but have not received a Send After yet.
    """
    recent = win_start - timedelta(days=2)
    # Notion rejects an OR-of-ANDs nested below another compound filter. Read
    # the three bounded windows separately, merge by page id, then discard
    # non-AVA/EOB templates locally. This is still a reduced projection: row
    # bodies and recipient rollups are never read below.
    filters = [
        {"and": [
            {"property": "Send After", "date": {"on_or_after": win_start.isoformat()}},
            {"property": "Send After", "date": {"on_or_before": win_end.isoformat()}},
        ]},
        {"and": [
            {"property": "Sent At", "date": {"on_or_after": win_start.isoformat()}},
            {"property": "Sent At", "date": {"on_or_before": win_end.isoformat()}},
        ]},
        {"timestamp": "created_time",
         "created_time": {"on_or_after": recent.isoformat()}},
    ]
    merged = {}
    for filt in filters:
        for row in _notion_query(token, MESSAGES_DS, {"filter": filt, "page_size": 100}):
            merged[row.get("id") or f"anonymous-{len(merged)}"] = row
    out = []
    for row in merged.values():
        p = row.get("properties", {})
        kind = _dispatch_kind(p)
        artist = _relation_id(p.get("Artist"))
        studio = (_prop_text(p.get("Studio")) or "").strip()
        if not (kind and artist and studio):
            continue
        out.append({
            "kind": kind,
            "artist": artist,
            "studio": studio,
            "status": _prop_text(p.get("Status")) or "",
            "send_after": _prop_text(p.get("Send After")),
            "sent_at": _prop_text(p.get("Sent At")),
            "created": row.get("created_time") or "",
        })
    return out


def _dispatch_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


# A row in one of these has already reached its end state: it went out, it was
# ruled unnecessary, or it failed loudly somewhere that is not this board. None
# of them is work still owed, so none of them draws a pill.
DISPATCH_TERMINAL_STATUSES = ("will not send", "sent", "error")


def _is_terminal_dispatch(status):
    return (status or "").strip().lower() in DISPATCH_TERMINAL_STATUSES


def _dispatch_pill(kind, board_status, row):
    if row is None:
        if _is_terminal_dispatch(board_status):
            return None
        return {"kind": kind, "state": "missing", "time": None}

    if _is_terminal_dispatch(row.get("status")):
        return None
    send_time = _dispatch_time(row.get("send_after"))
    return {"kind": kind, "state": "scheduled" if send_time else "queued",
            "time": send_time}


def _event_clock(base_day, decimal_hour):
    """Operating-day decimal hour to a Toronto datetime."""
    hour = float(decimal_hour)
    day_offset, hour = divmod(hour, 24)
    if day_offset == 0 and hour < 5:
        day_offset = 1
    whole_hour = int(hour)
    minute = round((hour - whole_hour) * 60)
    return (datetime.combine(base_day, datetime.min.time(), TZ)
            + timedelta(days=int(day_offset), hours=whole_hour, minutes=minute))


def _expected_dispatch_at(event, kind, base_day):
    point = event.get("start") if kind == "AVA" else event.get("end")
    if point is None:
        return None
    dt = _event_clock(base_day, point)
    return dt - (timedelta(hours=2) if kind == "AVA" else timedelta(minutes=15))


def _row_datetime(row, field):
    value = row.get(field)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt).astimezone(TZ)
    except (TypeError, ValueError):
        return None


def apply_message_dispatch(events, rows, base_day):
    """Attach the approved compact AVA/EOB state to FBS/Monitor bookings.

    An answered queue read with no expected row is MISSING. A failed queue read
    never calls this function, so an outage cannot manufacture a wall of false
    missing alarms. Timed rows join to the booking's canonical AVA (start−2h)
    or EOB (end−15m) time; an untimed row is safe only when Artist + Studio has
    one booking today. Duplicate replacements resolve newest-first.

    MISSING is a claim about the queue, so it is only ever made when nothing
    finished. A terminal row that fails the time join suppresses the pill
    rather than proving absence — see the comment at the claim itself.
    """
    by_key = {}
    for row in rows:
        key = (row.get("artist"), row.get("studio"), row.get("kind"))
        if not all(key):
            continue
        by_key.setdefault(key, []).append(row)

    booking_count = {}
    for event in events:
        if event.get("kind") != "booking" or event.get("tier") not in ("FBS", "Monitor"):
            continue
        artist, studio = event.get("_artist_id"), event.get("studio")
        if artist and studio:
            booking_count[(artist, studio)] = booking_count.get((artist, studio), 0) + 1

    for event in events:
        event["dispatch"] = []
        if event.get("kind") != "booking" or event.get("tier") not in ("FBS", "Monitor"):
            continue
        artist, studio = event.get("_artist_id"), event.get("studio")
        # Without both sides of the exact relation join, absence is UNKNOWN,
        # never MISSING. Publishing nothing is the fail-safe state.
        if not artist or not studio:
            continue
        for kind, field in (("AVA", "_ava_status"), ("EOB", "_eob_status")):
            candidates = by_key.get((artist, studio, kind), [])
            expected = _expected_dispatch_at(event, kind, base_day)
            timed = [r for r in candidates
                     if _row_datetime(r, "send_after") is not None
                     and expected is not None
                     and abs((_row_datetime(r, "send_after") - expected).total_seconds()) <= 300]
            if timed:
                matches = timed
            elif booking_count.get((artist, studio)) == 1:
                matches = [r for r in candidates if _row_datetime(r, "send_after") is None]
            else:
                # An untimed row cannot be assigned safely between two bookings.
                # Its existence also means absence cannot be proved for either.
                untimed = any(_row_datetime(r, "send_after") is None for r in candidates)
                matches = [] if not untimed else None
            row = (max(matches, key=lambda r: r.get("created") or "")
                   if matches else None)
            if matches is None:
                continue
            # Nothing joined, but a FINISHED row for this artist + studio + kind
            # is sitting right there. Its clock simply does not line up, which is
            # what a booking that moved after the sweep scheduled its message
            # looks like: Stella Dada's 2026-09-04 booking grew 13:00 → 13:30
            # mid-session, so her EOB went out on time at 12:45 while the card's
            # end−15m had become 13:15 — a 30-minute gap against a 5-minute
            # window, and the board called a sent message MISSING. A failed time
            # match is not evidence of absence. Publish nothing instead.
            if row is None and any(
                    _is_terminal_dispatch(r.get("status")) for r in candidates):
                continue
            pill = _dispatch_pill(
                kind,
                event.get(field),
                row,
            )
            if pill:
                event["dispatch"].append(pill)
    return events


def _access_row_hits(row, event, day):
    """Does this open Access / PIN row concern THIS card, today?

    Three row shapes, narrowest first:

    1. studio-keyed  — must match the studio AND today's date AND the name.
    2. artist-keyed  — day-scoped by design (one text covers every room the
       renter holds that day), so studio is not required; date is.
    3. unkeyed       — about the person, not a slot. If the row carries an
       Artist relation, that id is the match and the name is not consulted:
       relations do not collide, names do. Only a row with NO artist at all
       falls back to a name substring.

    Rule 3's artist check was added 2026-09-03. Without it, the open row
    "🔑 Studio 901 authorized users — Krista Flynn … (Akira Huang done)" — Nina
    Li's row, about Studio 901 on Sep 9, saying in its own title that Akira was
    finished — painted VERIFY ACCESS on Akira Huang's 509B booking that
    afternoon, and would have kept doing it on every booking of hers until
    somebody closed the row.

    Rules 1 and 2 are day-scoped; rule 3 is not, and is not meant to be. An
    unkeyed row is about the PERSON — "this renter has an unresolved access
    item" — so it lights every booking of theirs while it stays open, including
    one whose code works today. That is the pill's stated meaning (README §5:
    "an access check for this renter and today's booking is still open"), not a
    bug. It is also stricter than it looks: matching on the relation rather than
    the name makes the pill appear for renters a name substring used to miss
    (Adalia Knight, whose card reads "Adalia Knight (X Movement").
    """
    text = (row.get("text") or "")
    who = (event.get("who") or "").split(" — ")[0].strip().lower()
    artist = row.get("artist")

    m = RE_SWEEP_ARTIST_KEY.search(text)
    if m:
        if m.group(2) != day:
            return False
        if artist and event.get("_artist_id"):
            return artist == event["_artist_id"]
        return bool(who) and who in text.lower()

    m = RE_SWEEP_KEY.search(text)
    if m:
        return (m.group(1) == event["studio"] and m.group(2) == day
                and bool(who) and who in text.lower())

    if artist:
        return artist == event.get("_artist_id")
    return bool(who) and who in text.lower()


def flag_access_gaps(events, rows, base_day):
    """Red pill on today's booking when an open Access / PIN row concerns it.

    The card is the ONLY access surface on this app (Junyan, 2026-08-25): a
    renter whose code will not work shows as a red chip on their booking, and
    nothing else here talks about access. Matching is deliberately narrow — see
    _access_row_hits for the three row shapes and what each requires. Only a
    boolean crosses into the payload: this page is public, and the row titles
    are internal text (see redact()).
    """
    day = base_day.isoformat()
    for e in events:
        if e.get("kind") != "booking":
            continue
        for row in rows:
            if _access_row_hits(row, e, day):
                e["access_gap"] = True
                break
    return events


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


# Board-only display names. The Run Monitor rows keep their technical titles
# (heartbeat writers reference them by name); the board shows what the job
# actually does. Requested by Junyan 2026-08-25 — "watchlist-refresh (GitHub
# Actions)" tells a staff member nothing.
ROBOT_DISPLAY = {
    "watchlist-refresh (GitHub Actions)": "Watchlist sync",
    "qa-review (GitHub Actions)": "QA release check",
}


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
            "run": ROBOT_DISPLAY.get(r["run"], r["run"]),
            "cadence": r["cadence"], "expected": r["expected"],
            "produces": r["produces"], "monitoring": r["monitoring"],
            "status": cls, "statusLabel": label,
            "lastISO": last.replace(microsecond=0).isoformat() if last else None,
        })
    rank = {"crit": 0, "watch": 1, "ok": 2, "plain": 3}
    out.sort(key=lambda x: (rank.get(x["status"], 9), x["run"].lower()))
    return out


def prepare_board_events(events):
    """The per-event dicts the board carries.

    Fields with a leading underscore are internal — they exist so the
    post-write Booking Status sync can find the Notion row this event
    matched, and public_data() strips them before anything is published."""
    return [{
        "studio": e["studio"], "who": e["who"], "kind": e["kind"],
        "tier": e["tier"], "gtg": e["gtg"], "hta": e["hta"],
        # Public-safe AVA/EOB projection: kind + small state vocabulary + local
        # clock only. No queue ids, Artist ids, recipients or message content.
        "dispatch": e.get("dispatch", []),
        # Boolean only: "this renter has texted us today". Suppresses the No GTG
        # chip — see apply_heard(). No content of any kind crosses.
        "heard": bool(e.get("heard")),
        "arrived": e.get("arrived"), "departed": e.get("departed"),
        # Who keyed in / out, per the panel — a name, same class of data as
        # `who` (renter names are published by design; see redact()). Paired
        # `_foreign` booleans say the actor is not the expected person.
        "facilitator": e.get("facilitator"),
        "arrived_by": e.get("arrived_by"), "departed_by": e.get("departed_by"),
        "arrived_foreign": bool(e.get("arrived_foreign")),
        "departed_foreign": bool(e.get("departed_foreign")),
        # Truthy when `arrived` is an inference, not a witnessed disarm —
        # carries the reason: "open" (nobody armed after the previous renter)
        # or "bypass" (armed with the door sensor bypassed). See pass 4.
        "assumed": e.get("assumed"),
        "wrong_studio": e.get("wrong_studio"),
        "dup_studios": e.get("dup_studios"),
        "start": round(e["start"], 4), "end": round(e["end"], 4),
        # Boolean only — the Access / PIN row titles are internal text and this
        # page is public. See flag_access_gaps().
        "access_gap": bool(e.get("access_gap")),
        # Internal, never published: sync_booking_status() runs off this list
        # after the page is written and needs the Notion row it matched. Before
        # 2026-08-21 these were dropped here, so every event failed the sync's
        # `_notion_id` guard and no row ever flipped to Complete — silently, as
        # the guard `continue`s before any log line. splice() strips them.
        "_notion_id": e.get("_notion_id"),
        "_board_status": e.get("_board_status"),
    } for e in events]


# ──────────────────────────────────────────────────────────────────────────────
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
    # Shifts tab: open placeholders, today + lookahead. Soft source — a failed
    # fetch costs the Open list this edition, never the board. fetch_ics exits
    # via die() on failure, so SystemExit must be absorbed here too.
    try:
        open_shifts = fetch_open_shifts(ics_map, win_start, base_day, now)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        open_shifts = []
        emit_fallback_note(f"Staff ICS lookahead failed ({e}); open shifts absent this edition.")
    events, skedda_note = enrich_names_from_skedda(events, win_start, win_end)
    if skedda_note:
        print(f"NOTE: {skedda_note}")

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        die("NOTION_TOKEN missing.")
    # Staff blocks. Notion is the PRIMARY source (see HOLDS_DS): this runner has
    # no Skedda credential, so a direct read answers only on a developer's
    # machine. Marking runs before join_notion so a hold can never take a
    # renter's tier row. Soft: a failed read costs the Staff pills and falls
    # back to the title-only is_cleaning() detector, never the board.
    try:
        marked = mark_skedda_holds(events, fetch_studio_holds(token, base_day),
                                   base_day)
        if marked:
            print(f"NOTE: 🚧 Studio Holds marked {marked} card(s) as staff.")
    except Exception as e:  # noqa: BLE001
        emit_fallback_note(f"Studio Holds fetch failed ({e}); staff blocks fall "
                           f"back to title detection this edition.")
    events = join_notion(events, parse_notion(fetch_notion_rows(token, base_day.isoformat())))
    # Access pills — soft source, same posture as Robots/Reports: an unreadable
    # Actions DB costs the pills, never the board.
    try:
        events = flag_access_gaps(events, fetch_open_access_rows(token), base_day)
    except Exception as e:  # noqa: BLE001
        emit_fallback_note(f"Actions fetch failed ({e}); access pills absent this edition.")
    # Heard-from-them — soft: if the ledger is unreadable, the No GTG chip simply
    # behaves as it did before this existed (shown), never the reverse. Failing
    # this read must not HIDE a warning.
    try:
        events = apply_heard(events, fetch_inbound_texts(token, win_start))
    except Exception as e:  # noqa: BLE001
        emit_fallback_note(f"Correspondence fetch failed ({e}); No GTG chips not text-cleared.")
    # Messages tab — same soft posture: an unreadable queue costs the tab's
    # list this edition, never the board.
    try:
        messages = fetch_pending_messages(token)
    except Exception as e:  # noqa: BLE001
        messages = []
        emit_fallback_note(f"Message Queue fetch failed ({e}); Messages tab empty this edition.")

    # AVA/EOB pills — a second reduced projection of the same queue. This read
    # is deliberately separate from the Messages tab because it includes Sent
    # and Will Not Send rows needed to distinguish intentional absence from a
    # missing row. If the source is unreadable, publish no pills; never turn a
    # connector outage into dozens of false MISSING warnings.
    try:
        events = apply_message_dispatch(
            events, fetch_message_dispatch(token, win_start, win_end), base_day)
    except Exception as e:  # noqa: BLE001
        for event in events:
            event["dispatch"] = []
        emit_fallback_note(f"Message Queue dispatch fetch failed ({e}); AVA/EOB pills absent this edition.")

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
    panel_events, mail_events, door_events, door_gaps = [], [], [], []
    door_covers_since = None
    arm_feed = {"panel": None, "mail": None, "doors": None, "updatedAt": None}

    # doors — the websocket event ledger (/door-history). The NAME source, and
    # better-timed than the panel tick. It can have recorded outage gaps, which
    # is why the panel ledger below stays on as the timeline backstop.
    if DOOR_HISTORY_URL and ARM_HISTORY_TOKEN:
        try:
            door_events, door_gaps, door_covers_since = fetch_door_events(win_start)
            arm_feed["doors"] = "ok"
        except Exception as e:          # noqa: BLE001 — soft: panel ledger still answers
            arm_feed["doors"] = "failed"
            print(f"NOTE: door-history failed ({e}); events will be nameless.")
    else:
        arm_feed["doors"] = "unconfigured"

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

    # When the door feed answered, IT is the timeline — named and true-timed.
    # The panel ledger's minute tick reports the same arm changes 1-6 minutes
    # late (measured 2026-08-20: 13:26:24 -> 13:29), which is wider than
    # enrich_arm_names' 2-minute match window — so laying one over the other
    # would keep both copies. Instead, a panel event survives only if no door
    # event of the same studio+kind sits within 8 minutes: those survivors are
    # exactly the events the websocket missed (its gaps are real — the panel
    # poll is immune to them), which is the backstop role canon gives it.
    # Mail events ride along for the rare account still emailing.
    if door_events:
        def _door_twin(p):
            return any(d["studio"] == p["studio"] and d["kind"] == p["kind"]
                       and abs((d.get("ts") or 0) - (p.get("ts") or 0)) <= 8 * 60 * 1000
                       for d in door_events)
        backstop = [p for p in panel_events if not _door_twin(p)]
        arm_events = sorted(door_events + backstop, key=lambda e: e.get("ts") or 0)
        if mail_events:
            arm_events = enrich_arm_names(arm_events, mail_events)
        events = apply_arm_events(events, arm_events, alerts=alarm_alerts)
    elif panel_events or mail_events:
        arm_events = (enrich_arm_names(panel_events, mail_events)
                      if panel_events else mail_events)
        events = apply_arm_events(events, arm_events, alerts=alarm_alerts)
    else:
        # Nothing answered. The board's own Armed/Disarmed columns are a weaker
        # record (one row per studio, no actor) but they are a record.
        used_fallback = True
        # Distinguish "answered with nothing" from "did not answer" — they look
        # identical here and mean opposite things, and a note that says "no
        # source answered (panel: ok)" is the kind of self-contradiction that
        # costs an hour at 2am. `feed_is_down` already draws the same line.
        answered = [k for k in ("doors", "panel", "mail") if arm_feed[k] == "ok"]
        emit_fallback_note(
            (f"No arm events yet — {' and '.join(answered)} answered with an "
             "empty stream" if answered else "No arrival source answered")
            + f" (doors: {arm_feed['doors']}, panel: {arm_feed['panel']}, mail: {arm_feed['mail']}); "
            "used board Armed/Disarmed fallback.")
        events = apply_board_fallback(events)

    # Carried in the data so a reader can see WHEN the door feed was deaf.
    # No flag logic keys off these: the panel ledger polls and is immune to
    # socket gaps, so arm state survives them — but a "who was it" question
    # about a gap window has to fall back to nameless evidence, and the page
    # saying so beats an agent re-deriving it.
    # Second witness. The mail feed is a check on the websocket, never a
    # source it leans on — see reconcile_mail_against_doors.
    if door_events and mail_events:
        check = reconcile_mail_against_doors(mail_events, door_events,
                                             door_gaps, door_covers_since)
        arm_feed["mailCheck"] = check
        if check["missedCount"]:
            emit_fallback_note(
                f"DOOR LEDGER HOLE — the ADT email reports "
                f"{check['missedCount']} named arm/disarm event(s) the "
                f"websocket did not record, in windows it claimed to cover: "
                + "; ".join(check["missed"]) + ". Treat those windows as "
                "uncovered for billing.")

    arm_feed["doorGaps"] = door_gaps
    arm_feed["down"] = feed_is_down(arm_events, arm_feed, datetime.now(TZ), win_start)
    if arm_feed["down"]:
        emit_fallback_note(
            "ARRIVAL FEED DOWN — no arm/disarm events from either source; "
            "no-arrival flags suppressed on the board.")

    board_events = prepare_board_events(events)

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
        "shiftBaseDay": base_day.isoformat(),
        "generatedAt": now.strftime("%b %-d, %-I:%M %p ET"),
        "generatedAtISO": now.replace(microsecond=0).isoformat(),
        "studios": STUDIOS,
        "events": board_events,
        "staff": sorted(staff, key=lambda s: s["start"]),
        "openShifts": open_shifts,
        "messages": messages,
        "attention": attention,
        "armEvents": arm_stream,
        "panelPrior": panel_prior,
        "alarmAlerts": alarm_alerts,
        "armFallback": used_fallback,
        "armFeed": arm_feed,
        "robots": robots,
        "robotsNote": robots_note,
    }
    return data, used_fallback


def public_data(data):
    """`data` minus the builder's internal per-event fields.

    Everything under a leading underscore is machinery for the write-back and
    has no business on a public page. Strip at the publishing boundary rather
    than at construction, so the sync that runs after the page is written can
    still see what row each event came from."""
    out = dict(data)
    out["events"] = [{k: v for k, v in e.items() if not k.startswith("_")}
                     for e in data["events"]]
    return out


def splice(data, template=None):
    tpl = open(template or TEMPLATE, encoding="utf-8").read()
    payload = json.dumps(public_data(data), indent=2, ensure_ascii=False)
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
                json={"properties": {"Booking Status": {"status": {"name": "Complete"}}}},
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
