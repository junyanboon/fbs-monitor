"""Read-only Skedda lookup — supplies the renter name the ICS feeds omit.

WHY THIS EXISTS
The studio ICS feeds are the board's only source of bookings, and for
marketplace-synced bookings their SUMMARY carries no person at all: Giggster
sends "Booking on Giggster.com https://…", so the board showed a renter called
"Giggster Booking" while Skedda's own record said "Welton R. Giggster"
(booking 118386232, 693, 2026-08-15 19:30-22:30). Front desk cannot greet a
marketplace.

WHY NOT IMPORT skedda-cli
`skedda-cli` (github.com/junyanboon/skedda-cli, `skedda/client.py`) is the canon
client and this is a deliberate partial copy of its read path — the token
scrape and two GETs, ~80 lines of its 1169. The rest of that file creates,
edits, cancels and re-holders bookings. The board builder runs unattended every
15 minutes on a schedule nobody watches; it should be structurally unable to
write to Skedda, not merely disinclined. If the auth shape changes, canon is
skedda-cli and this file follows it.

WHAT IT ALSO DOES
Marks Skedda UNAVAILABLE blocks (`is_hold`) so the board can render a staff
block as staff instead of guessing from its title. Added 2026-09-03 after a
"Matterport 360deg panorama capture" hold in 509B was rendered as a booking and
absorbed the FBS Monitor row belonging to the renter an hour later.

WHAT IT DELIBERATELY DOES NOT DO
Recurring series are NOT expanded. Skedda returns a series as one record stamped
with its FIRST occurrence, so today's occurrences of a weekly booking simply do
not appear in this read. That is fine for the job: marketplace bookings are
one-off casual bookings, never recurring. It is a KNOWN GAP for `is_hold` —
a RECURRING staff block does not appear here and so is not marked as staff; the
cleaner-name detector in build.py (`is_cleaning`) is the only cover for those.
It is NOT fine for deciding which
studio a booking belongs to — an unexpanded series would read as "no Skedda
counterpart", which is indistinguishable from a moved booking's ghost. So this
module names renters and nothing else; see the 2026-08-15 Plumbing Map entry for
the studio-authority idea it is deliberately not doing yet.

AUTH
Cookie from SKEDDA_COOKIE, else GCP Secret Manager via gcloud (the `skedda-cookie`
secret, refilled daily 06:00 Toronto by the `skedda-refresh` Cloud Run job).
Neither available → returns nothing and the board builds exactly as before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_URL = os.environ.get("SKEDDA_BASE_URL", "https://danceannex.skedda.com").rstrip("/")
SECRET_NAME = os.environ.get("SKEDDA_COOKIE_SECRET", "skedda-cookie")
SECRET_PROJECT = os.environ.get("SKEDDA_COOKIE_PROJECT", "danceannex-skedda")
TOKEN_HEADER = "X-Skedda-RequestVerificationToken"
_TOKEN_RE = re.compile(r'name="?__RequestVerificationToken"?[^>]*value="([^"]+)"')
TIMEOUT = 20
# Skedda's booking `type` enum (canon: skedda-cli `skedda/client.py` BOOKING_TYPES).
#   0 internal · 1 user/casual · 2 UNAVAILABLE
# Type 2 is the venue's own block — cleaning, maintenance, a photo shoot, a hold.
# It is the ONLY reliable way to tell a staff block from a booking: the Google
# mirror the board reads carries just the title, and a hold titled "Matterport
# 360deg panorama capture" is indistinguishable from a renter by title alone.
# Do NOT infer this from a missing venueuser — a type-1 "casual" booking (every
# Tagvenue/Peerspace/Giggster mirror) also has no venue user and IS a real
# booking.
UNAVAILABLE_TYPE = 2


class SkeddaUnavailable(RuntimeError):
    """Any reason the lookup could not run. Always soft — never fails a build."""


def _cookie():
    env = os.environ.get("SKEDDA_COOKIE")
    if env:
        return env.strip()
    gcloud = shutil.which("gcloud")
    if not gcloud:
        return None
    try:
        out = subprocess.run(
            [gcloud, "secrets", "versions", "access", "latest",
             f"--secret={SECRET_NAME}", "--project", SECRET_PROJECT],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — gcloud missing/hung is just "no cookie"
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def _get(path, cookie, params=None, token=None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json" if token else "text/html",
        "Cookie": cookie,
        "User-Agent": "fbs-monitor/1.0 (read-only)",
    }
    if token:
        headers[TOKEN_HEADER] = token
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SkeddaUnavailable(f"HTTP {e.code} on {path}") from e
    except Exception as e:  # noqa: BLE001 — network, DNS, timeout
        raise SkeddaUnavailable(f"{type(e).__name__} on {path}") from e


def _get_json(path, cookie, token, params=None):
    raw = _get(path, cookie, params=params, token=token)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SkeddaUnavailable(f"non-JSON response from {path}") from e


def _studio_id(space_name):
    """Skedda space name → board studio id. 'Studio 901 (Elements)' → '901'."""
    s = re.sub(r"\s*\(.*\)\s*", "", space_name or "").strip()
    s = re.sub(r"^studio\s+", "", s, flags=re.I).strip()
    return s or None


def fetch_named_bookings(win_start, win_end):
    """[{studio, start, end, title, user_name, is_hold}] overlapping the window.

    `is_hold` marks a Skedda UNAVAILABLE block (type 2) — a staff block, not a
    booking. See UNAVAILABLE_TYPE above.

    Times are naive local strings from Skedda, parsed to naive datetimes — the
    same wall clock the ICS events are compared on. Raises SkeddaUnavailable for
    every failure; callers treat that as "no enrichment", never as an error.
    """
    cookie = _cookie()
    if not cookie:
        raise SkeddaUnavailable("no SKEDDA_COOKIE and no gcloud access to Secret Manager")

    html = _get("/booking", cookie)
    m = _TOKEN_RE.search(html)
    if not m:
        raise SkeddaUnavailable("anti-forgery token not in page HTML (cookie likely expired)")
    token = m.group(1)

    spaces = {a.get("id"): a.get("name")
              for a in (_get_json("/webs", cookie, token).get("assets") or [])}
    data = _get_json("/bookingslists", cookie, token, params={
        "start": win_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": win_end.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    users = {u.get("id"): u for u in (data.get("venueusers") or [])}
    # Skedda's times are naive local wall clock. Callers hand in aware datetimes
    # (the board runs in America/Toronto); compare like with like.
    lo, hi = win_start.replace(tzinfo=None), win_end.replace(tzinfo=None)

    out = []
    for b in data.get("bookings") or []:
        # A recurring series is stamped with its first occurrence; leaving it in
        # would offer a match for the wrong day. Drop it — see module docstring.
        if b.get("recurrenceRule"):
            continue
        try:
            start = datetime.strptime(b.get("start") or "", "%Y-%m-%dT%H:%M:%S")
            end = datetime.strptime(b.get("end") or "", "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if end <= lo or start >= hi:
            continue
        u = users.get(b.get("venueuser")) or {}
        name = " ".join(p for p in (u.get("firstName"), u.get("lastName"))
                        if p and p.strip()).strip() or None
        for sid in (b.get("spaces") or []):
            studio = _studio_id(spaces.get(sid))
            if studio:
                out.append({
                    "studio": studio, "start": start, "end": end,
                    "title": (b.get("title") or "").strip() or None,
                    "user_name": name,
                    "is_hold": b.get("type") == UNAVAILABLE_TYPE,
                    "id": b.get("id"),
                })
    return out


def main():
    """`python skedda_names.py [YYYY-MM-DD]` — smoke test against the live venue."""
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    start = datetime.strptime(day + "T05:00:00", "%Y-%m-%dT%H:%M:%S")
    end = start.replace(hour=23, minute=59)
    try:
        rows = fetch_named_bookings(start, end)
    except SkeddaUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 1
    for r in rows:
        print(f"{r['studio']:<5} {r['start']:%H:%M}-{r['end']:%H:%M}  "
              f"title={r['title']!r} user={r['user_name']!r}"
              f"{'  [HOLD]' if r['is_hold'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
