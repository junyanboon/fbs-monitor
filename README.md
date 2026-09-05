# FBS Monitor

Two static editions, rebuilt from the same DATA every ~15 min by GitHub Actions:

| Page | URL | For |
|---|---|---|
| `index.html` | https://junyanboon.github.io/fbs-monitor/ | Desktop board (studio timeline) |
| `mobile.html` | https://junyanboon.github.io/fbs-monitor/mobile.html | FBS Mobile PWA (agenda) |

`build.py` fetches the sources, builds one DATA blob, and splices it into
`template.html` → `index.html` and `template-mobile.html` → `mobile.html`.
**Edit the templates, never the built HTML** — the next build overwrites it.

Sources: studio + staff calendars (secret ICS), the FBS AI Support board (Notion),
ADT arm/disarm mail (Gmail API, label `Artist Care - ADT`), the 🚥 Run Monitor
DB (Notion) for the Robots tab, and 📊 Workflow Reports (Notion) for the Reports
tab. The booking-card AVA/EOB cluster reads a reduced projection of 📤 Message
Queue: Template, Artist relation, Studio, Status, Send After, Sent At, and created
time only.

**Each studio calendar is written by two syncs, and the board takes only one.**
The Skedda→Google mirror (`Spaces: Studio NNN` in the description) is the system
of record: room, price, paid status, and it tracks moves. The marketplace feeds
(Peerspace, Giggster) write their own event on whichever studio their *listing*
names and never learn about a move. Taking both is what put one renter in two
studios and named another "Booking on Giggster.com https".

**Skedda only** (Junyan, 2026-08-15): `lane_of()` classifies each event and
`drop_marketplace_mirrors()` ignores any marketplace event whose Skedda
counterpart overlaps it — matched by renter name (catches a moved booking, whose
mirror sits in the wrong room) or by room (catches a nameless mirror like
"Giggster Booking", since a studio holds one booking at a time). Every ignored
row prints a `NOTE:` line.

**The one thing that is never dropped:** a marketplace booking with *no* Skedda
counterpart. That is a hole in the system of record, so it stays on the board and
the run prints `⚠ … NO Skedda booking`. An empty studio on the board is how a
sync gap becomes someone standing at a locked door.

One optional source: **Skedda, read-only, for renter names only**
(`skedda_names.py`). Marketplace-synced ICS summaries name the platform rather
than the person — Giggster sends `Booking on Giggster.com https://…`, so the
board read "Giggster Booking" while Skedda held "Welton R. Giggster". The
lookup fills in nameless platform titles and touches nothing else. It needs the
`GCP_SA_KEY` repo secret (a service account with `secretAccessor` on
`skedda-cookie` in project `danceannex-skedda`, refilled daily by the
`skedda-refresh` Cloud Run job). **Without that secret the workflow skips the
lookup and the board builds exactly as before** — this is a nicety, never a
dependency. Canon for the Skedda API is
[`skedda-cli`](https://github.com/junyanboon/skedda-cli); `skedda_names.py` is a
deliberate read-only partial copy, so the builder cannot write to Skedda.

`sw.js` is a service worker, registered by both editions, so the board stays
readable when the connection drops — Junyan reads it from Brazil and on the
move. It is **network-first for the pages and never caches `version.json`**;
both properties are load-bearing and the reasoning is in the file's own header.

Three JSON files ship beside the pages. They are a published interface, not
build scratch — something else reads each one, so a field rename is a breaking
change. The workflow's `git add` list is explicit: a new build output that isn't
added there is built every run and published never.

| File | Read by | Carries |
|---|---|---|
| `version.json` | both pages (30 s poll) | the build timestamp, ~60 bytes |
| `panel-state.json` | the builder itself, next run | last arm/disarm per **studio**, durable |
| `booking-state.json` | the event gate | arrival/departure per **booking**, today |

`booking-state.json` exists so the event gate can answer "is this renter still
in the studio?" before it sends a canned how-to reply. It publishes the
attribution this builder already does — remote events excluded, wrong-studio
handled, subject shapes parsed — so no consumer re-derives any of it from the
ADT mail. Per-booking, not per-studio: `panel-state.json` cannot answer the
question, because a back-to-back renter's arrival overwrites the previous
renter's departure. Same fields the pages already render publicly; no PINs (see
rule 5).

**Vercel edition** (2026-09-05): the same build, served from Vercel behind a
staff login, with **Claim** on every open shift. Setup and operation:
[`deploy/README.md`](deploy/README.md). The workflow's `publish.py` step
uploads each build into the app's Postgres `pages` table (skipped when the
`DATABASE_URL` secret is unset — the Pages edition never depends on it).
`server/` is the claim server; `open-shifts.json` is its input and is never
committed. Both templates carry `FBS_AUTH`, which asks `api/me` once: a 404
(GitHub Pages) means "no server" and the page renders exactly as before.

---

## Rules that are easy to break

These are the ones that have actually cost us a wrong board. Each is covered by
`test_adt_parsing.py` / `test_arm_history.py`, both of which run in CI **before**
every build.

### 0. Arrivals have TWO sources, and an empty stream is not "nobody came"

Arrival/departure times come from `alarm-mcp`'s `/arm-history` — the arm-state
ledger its watchdog tick writes every minute (`armed -> disarmed` = arrival).
The ADT email feed is the **second** source, and its job is now the **actor
names**, which panel state cannot provide, plus any sub-tick event state
polling misses. `enrich_arm_names()` lends a mail event's name to the panel
event of the same studio and kind within two minutes.

Why it changed, 2026-08-18: the email feed was the *only* source, it died
overnight, and the board rendered Kiah Francis' finished 07:30 booking as a red
"no arrival". A dead feed and a real no-show produce the identical
`arrived: null`, so the board accused a renter on no evidence at all.

So `feed_is_down()` exists, and **it is not optional decoration**. Zero events
across all five studios, five hours into the operating day, with no healthy
panel feed = a broken pipe. The page then suppresses every no-arrival flag and
says so. Three conditions, each load-bearing:

* one event **anywhere** clears it — studios do not all sit idle while the feed works
* a **healthy** panel feed returning nothing is believed — it genuinely holds no
  events before the day's first disarm, and suppressing then would hide real no-shows
* the five-hour delay stops it firing every morning before the studios open

**Arrival times run about 4–7 minutes late, and that is not fixable here.**
Audited 2026-08-20 against Alarm.com's own exported log for 693: three real
transitions, all three captured, all three late (+6m37s, +3m49s, +4m06s). The
watchdog tick was verified at exact 60-second intervals over the same window,
so this is Alarm.com's partition *state* trailing its own *event log*, not our
polling. Good enough for "did they show up" and "are they still in the room";
**not** good enough for "were they late" or a billing dispute over minutes.

Unset `ALARM_HISTORY_URL` and the builder behaves exactly as it did before —
Gmail-only — with the guard still armed. Covered by `test_arm_history.py`.

### 1. The site prefix is not the partition

ADT prefixes every subject with the **site**, then names the **partition**:

```
Studio 509: Studio 509B was Armed Away at 6:04 PM (info@danceannex.ca)
      ^^^ site — NOT a studio        ^^^^ the partition
```

`509` alone is not a studio, so taking the *first* `Studio <n>` match drops the
message. `studio_from_subject()` scans every token and keeps the **last valid**
one. This bug was silently discarding every 509A/509B alarm, bypass and tamper
mail until the tests caught it on 2026-08-02.

### 2. Subject wording changes without warning

TELUS/ADT has changed shapes at least twice, each time silently dropping events:

| Shape | Example |
|---|---|
| by-name | `Studio 509A was Disarmed by Keerthana Vijay at 2:08 PM` |
| panel, by-name | `Studio 901 Panel was Armed Away by Himanshi Mehta at 5:12 PM` |
| nameless, name in parens | `Studio 509B was Armed Away at 6:04 PM (Panel User)` |

If a studio's state looks wrong, **suspect an unmatched subject first**. Run the
workflow with `debug_arm: true` to log how every subject parsed, then add the
shape to the regexes *and* to `test_adt_parsing.py`.

### 3. Remote events set state but never attribute

An arm/disarm by `info@danceannex.ca` is a real panel change, so it counts for
panel state — but it is **not** a renter arriving. Those events carry
`remote: true` and are excluded from booking attribution.

### 4. A renter in the wrong studio is not a no-show

If a booking has no arrival in its own studio, but the renter disarmed a
**different** studio inside the same window, `apply_arm_events()` pass 3 sets
`wrong_studio: {studio, at}` on the booking and both editions render
`⚠ in <studio> at <HH:MM>` in place of `⚠ no arrival`.

The two need opposite responses. A no-show is a billing question answered later;
a wrong studio is a person to go move **now**, usually before the room's real
booking walks in.

> 2026-08-07: Ayden Mauro booked 527 18:00–19:15 (30 attendees, paid) and ran the
> session in 693, which nobody had booked. The board said only "no arrival", which
> reads as a no-show, so nothing prompted anyone to go look. He armed 693 at 18:52 —
> one minute before Amandeep Kaur's class disarmed the same room.

Pass 3 only considers events **no booking has claimed**. If someone with a shared
name token legitimately booked that other studio, pass 1 claims the event first and
no flag is raised. `arrived` is never set from a foreign studio — the person did not
arrive where they were supposed to be, and the board must not imply they did.

### 5. Access warnings are boolean here, never codes

**This board is a public GitHub Pages site.** It never reads or publishes the
`Alarm Code` rollup. `fetch_open_access_rows()` reads only the titles of open
`Access / PIN` actions plus each row's `Artist` relation id, and
`flag_access_gaps()` collapses the matching action to `access_gap: true/false`
before DATA is built. Message bodies, action notes, Alarm.com identifiers, and
PIN values never cross into the public payload.

The red `VERIFY ACCESS` chip therefore means exactly one thing: an access check
for this renter and today's booking is still open. It does **not** assert that
the code field is empty; the unresolved item may instead be an access window,
a dormant panel user, or an unavailable Alarm.com directory read. It also never
says WHO verifies — every one of these rows carries `For: Ops Lead`, and the
answer is always the desk.

Which rows reach which card (`_access_row_hits`, three shapes, narrowest first):

| Row shape | Example key | Must match |
|---|---|---|
| studio-keyed | `[sweep:509B:2026-08-24T14:00:access]` | studio + today + name |
| artist-keyed | `[sweep:access:<artist-id>:2026-08-31:access]` | today + artist (day-scoped by design — one text covers every room they hold) |
| unkeyed | *(none)* | the `Artist` relation, or the name when the row has no artist |

> 2026-09-03: the unkeyed rule matched on the name substring alone, so the open
> row "🔑 Studio 901 authorized users — Krista Flynn … **(Akira Huang done)**" —
> Nina Li's row, about Studio 901 on Sep 9 — painted `VERIFY ACCESS` on Akira
> Huang's 509B booking that afternoon. Relations do not collide; names do.

### 6. Same-minute events order by email receipt

Subjects carry `HH:MM` only, and Gmail lists newest-first. When a disarm and an
arm share a minute, the panel card and the event stream disagreed. Every ordering
sorts on `(panel minute, email receipt ts)`.

### 7. Panel state is durable, not derived-per-build

`panel-state.json` is committed with each build and remembers every studio's last
known arm/disarm **indefinitely**. Precedence: today's events → the 5-day Gmail
lookback → the stored file. It is never blanked on a Gmail fallback. A studio
quiet for a week must not regress the board to "Unknown".

### 8. Alarms clear; troubles do not

An **alarm** is outstanding only if nobody touched that panel at/after it — a
disarm means someone was there. A **trouble** (tamper / malfunction / low battery
/ power loss) stands until ADT says otherwise; disarming does not clear it.

> ADT mail does not carry every trouble Alarm.com knows about. The live panel
> snapshot (partitions, bypassed sensors, trouble conditions) lives behind the
> alarm connector, which the cloud build cannot reach. Getting that into the
> build would need the Alarm.com credentials in Actions — not done.

### 9. The staff rail is people only

Only rostered names render: **Junyan, KyJah, Ela, Stefan, Donny**
(`STAFF_ROSTER`). Unassigned placeholders — `Need FBS`, `Need Monitoring`,
`Studio Viewing Support`, `Open the Studio`, `Close the Studio` — never appear;
they are coverage bookkeeping, not a person on site. Open/Close blocks were also
retired at the source (🗓️ Skill: Staff Time Blocker, 2026-07-31).

### 10. Colours follow Skedda

| Colour | Meaning |
|---|---|
| 🔵 blue | recurring renter (contract / Fixed Option) |
| 🟢 green | Artist Plan (AAP / 1AP) |
| 🟡 yellow | one-off booking, not on contract |
| 🔴 red | missed booking (no arrival logged) |

Skedda's tag colours don't travel through the ICS feed, so `plan_of()`
reconstructs them from the booking title + recurrence. AAP and Fixed Option are
explicit in titles; a recurring renter with neither marker is inferred from RRULE.

### 11. Clients poll `version.json`, not the page

`build.py` writes `version.json` (~80 bytes) carrying the same `generatedAtISO`
as the pages, and both editions poll **that** every 30 s to decide whether a
newer edition exists. They used to refetch the whole page (59 KB / 39 KB) every
5 minutes for the same one-field comparison.

Two things follow, and both are easy to break:

- **`version.json` must be committed by the workflow.** It is in the same
  `git add` line as the pages. Drop it and every open client polls a 404
  forever — silently, because the probe swallows errors by design.
- **Write it last, after both pages.** It advertises an edition; if it lands
  first, a client can reload onto a page that hasn't been written yet.

### 12. Everything is Toronto time

Junyan reads these from Brazil. Both pages derive "now" from
`Intl.DateTimeFormat` parts in `America/Toronto` — never the device clock, never
`toLocaleString()` parsing (engine-dependent). The mobile clock is labelled
TORONTO. `America/Toronto` (not a fixed EST offset) so DST follows automatically.

### 13. The Reports tab reads titles, never bodies

`fetch_reports()` pulls the `Run` title out of 📊 Workflow Reports and nothing
else. That is a safety rule wearing a performance rule's clothes.

Report **titles** are written to be published — each fleet job puts its headline
there (`🔑 Code Mirror — Sat Aug 15 · checked 696 · matched 162 · corrected 0 ·
needs a human 0`). Report **bodies** are not: they carry renter names, booking
specifics, and whatever a run happened to find. **This board is a public GitHub
Pages site** (see rule 5). Pulling body text in to "add detail" would publish all
of it. Open the row in Notion for the detail — that is what the tab's own
subtitle tells the reader to do.

It also means the whole tab is one query with no per-row fetch, which is why it
adds nothing measurable to a 15-minute rebuild.

### 14. Robots and Reports answer different questions

The Robots tab asks **did it run?** — heartbeats against the 🚥 Run Monitor
roster. The Reports tab asks **what did it say?**

A job can check in perfectly on time having found something badly wrong. Before
the Reports tab existed, that answer lived only in Notion, and the board that is
actually open when a call comes in showed a green robot. Do not merge the two
tabs on the grounds that they both list runs.

### 15. AVA/EOB pills are queue state, not message content

FBS and Monitor booking cards carry a right-side AVA/EOB dispatch cluster:

- a clock time means the queue row has `Send After`;
- `QUEUED` means the row exists but has no send time;
- `MISSING` means the queue read succeeded but the FBS row expects that message
  and no matching queue row exists;
- terminal rows (`Sent`, `Will Not Send`, or `Error`) render no queue pill.

The join is Artist relation + Studio + message kind, then the booking's
canonical dispatch time (AVA = start−2h; EOB = end−15m). An untimed row is
shown only when that artist has one booking in that studio today; otherwise
the board declines to guess. Message kind comes from the current `Template`
field or the legacy Host row's stable `AVA-sweep-…` / `EOB-sweep-…` code. The page publishes
only `{kind, state, time}`. It never publishes queue ids, Artist ids, recipient
details, message bodies, access instructions, reasoning, or dispatch receipts.
If Message Queue is unreadable, every booking gets an empty dispatch list — an
outage must remove the pills, never manufacture a wall of false `MISSING` alerts.

### 16. An open shift must still be claimable

The Staff calendar can temporarily hold both `Need FBS` / `Need Monitoring`
placeholders and the named shift that claimed them. The open-shift lookahead
therefore reads both shapes from the same three-day feed and removes a
placeholder only when a rostered assignment covers the same day, role, time,
and studio. A missing studio is accepted only when that role/time has one
possible open block; simultaneous studios fail open rather than guessing.

Expired blocks are removed in the builder and filtered again in both pages so
a row ages out at its end time between 15-minute rebuilds. Cross-midnight times
use the build's `shiftBaseDay`; `23:45–00:15` remains claimable until 00:15 and
not a minute longer.

### 17. A staff block is identified by Skedda, not by its title

The board reads the Google mirror, which carries a hold's **title and nothing
else** — no field says "this is a venue block". Two detectors cover it, and both
are needed:

| Detector | Reads | Covers | Misses |
|---|---|---|---|
| `is_cleaning()` | the ICS title — a cleaner's name, leading | recurring cleaning blocks | anything titled differently (e.g. "Matterport …", "Caney") |
| `mark_skedda_holds()` | 🚧 Studio Holds in Notion — Skedda's own `type == 2` (UNAVAILABLE) | every one-off block, whatever it is called | recurring blocks (Skedda does not expand series) |

A matched card becomes `kind: "staff"` and renders a **red `Staff` chip**. Do
not infer a hold from a missing `venueuser` — a type-1 "casual" booking (every
Tagvenue / Peerspace / Giggster mirror) also has none and *is* a real booking.

**Why Notion and not Skedda directly.** A Skedda read needs the venue session
cookie, and this board builds in GitHub Actions, which holds no GCP credential:
the workflow's `Read Skedda cookie` step has been **skipped on every run since
it was added on 2026-08-15**, so `skedda_names` has never once answered in
production. `dc-canon services/event-gate/holds_sweep.py` runs inside the
`danceannex-skedda` project and reads that cookie with the service's own
identity — no key, no token — and publishes each hold to 🚧 Studio Holds
(`f6210728-6396-4047-9bf1-10971dbcbeb6`). This builder reads it with the
`NOTION_TOKEN` it already has. No new credential on either side.

`enrich_names_from_skedda`'s direct Skedda path still exists and still marks
holds when a cookie IS present — that is the developer-laptop case. In CI it
soft-fails and Notion answers.

**The title detector stays narrow, and that is deliberate.** On 2026-09-03
Junyan asked for "if any of our staff names appear on there (Caney, Junyan,
Donny, Stefan)". Built, then reverted the same day on the evidence: the live
Artist Database holds **"Rita Stefan [Skedda]"**, a One-Off *renter* whose
surname is Stefan, and `ela` sits inside Gabriela, Mihaela, Mariadela, Daniela,
Pamela, Kaela and Elaine — seven more. Marking a renter as staff pulls them off
the board **and out of every FBS message lane**, which is worse than the bug it
fixes. Junyan's ruling on seeing the collisions: *"let's drop off the staff name
rule then as that can definitely be an issue."*

`CLEANERS` stays at `stefan`, `donny`, `ela`, first token only, and only when
the title also says `clean` or is two words or fewer. That last condition is
what keeps even this list safe.

Two other title-shaped ideas were tested and rejected the same day:

- **The `Price:` line** in the calendar description looks like a hold marker and
  is not — Lordasa Charles's Studio Viewing and Ifreestyle's free block both
  lack it and are real bookings. It means "costs money", not "is a booking".
- **Stamping the type onto the Google event.** Those calendars are a
  GAS-ICS-Sync that rewrites events (Kyle FitzGerald's two 509B events both
  showed `updated` 14:12Z on the day they were inspected), so an appended
  marker is wiped silently — and the script's own input is the same ICS, which
  does not carry the type either.

`join_notion()` matches a card to its FBS row on **both ends within 30 minutes**
(`NOTION_SLOT_TOLERANCE`). Both sides come from the same Skedda booking and
agree to the minute in practice.

> 2026-09-03: a 509B hold titled "Matterport 360° panorama capture — Studios
> 509A & 509B" (Skedda 119939698, type 2), 13:00–14:00, rendered as a booking.
> `join_notion` then matched it — on the START only, inside a 2-hour window — to
> Akira Huang's 14:15–15:45 `Monitor Only` row. The camera wore her `MONITOR`
> chip and her `HTA: Sent`, and picked up two false `AVA MISSING` / `EOB MISSING`
> pills because the queue rows were timed off *her* 14:15, not its 13:00. She
> read as an untiered booking with no messages at all.

### 18. The session verdict certifies only what was observed

Each **finished** booking carries one glyph at the end of its name row — right
of the tier pill, or beside the name when there is no pill. It answers the one
question the desk asks while scanning a lane: did this one go normally?

| Glyph | Means | Triggered by |
|---|---|---|
| ✓ green | Nothing to do | arrival **and** departure both seen, both within 16 min of the window |
| ⚠ amber | Worth a look | out >16 min late, in >16 min early, an inferred arrival (`assumed`), a keypad name that is not the expected person, or **no departure seen at all** |
| ! red | Act on it | no arrival at all, the renter was in another studio, or out **>60 min** late |

`verdictOf()` lives in both templates and is covered case-for-case, on both
editions at once, by `test_session_verdict.py`. The thresholds are the timeline's
own `FLAG_MIN` (16 min) and a new `OVER_BIG` (1 h) — change them in one place.

**It stays silent twice, and each silence is load-bearing:**

* **The booking is not over.** A live booking has no departure yet, so a ✓ would
  be a guess that goes stale the moment the renter overstays.
* **`FEED_DOWN` is set.** `arrived` is then null for *every* booking because
  nothing was watching (rule 0). A verdict there is manufactured out of an
  outage — the same reason `stateOf()` suppresses the Missing flag.

`dup_studios` suppresses it too, for the reason `stateOf()` already does: a
second calendar row for the same renter is not evidence about anything.

**A departure nobody observed is amber, and must stay amber.** Back-to-back
bookings in one studio share a panel: nobody arms between them, so the earlier
renter honestly has `departed: null` (see `attribute_arm_events` pass 2). That
is the routine case, not an alarm — but it is also indistinguishable here from
a studio left genuinely open, and Junyan's ruling on 2026-09-05 was to flag it:
*"12 departure never seen back to back is warning."* The desk tells the two
apart in a glance; this builder cannot. Promoting it to red would put an
act-on-it badge on a large share of every ordinary day, which is how a signal
stops being read at all.

---

## Working on it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_adt_parsing.py     # must pass before pushing
```

A full `build.py` run needs the secrets; to preview a template change locally,
splice the live DATA into it:

```bash
curl -s https://junyanboon.github.io/fbs-monitor/mobile.html -o /tmp/live.html
python - <<'PY'
import re
D=re.escape("/*__DATA__*/"); E=re.escape("/*__END_DATA__*/")
data=re.search(D+"(.*?)"+E, open("/tmp/live.html").read(), re.S).group(1)
tpl=open("template-mobile.html").read()
out=re.sub(D+".*?"+E, lambda _: "/*__DATA__*/"+data+"/*__END_DATA__*/", tpl, count=1, flags=re.S)
open("/tmp/preview.html","w").write(out)
PY
```

The build has no time gate (since 2026-09-05). Daytime cadence is the day-sheet
tick every 15 min, 08:00–02:45 Toronto; 03:00–06:59 the workflow's own cron
fires once an hour (best-effort — GitHub throttles it) so the board rolls to the
new day at 05:00 instead of freezing on yesterday until the first tick. The
build skips green until `NOTION_TOKEN` is set.
