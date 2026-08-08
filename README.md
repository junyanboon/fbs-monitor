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
ADT arm/disarm mail (Gmail API, label `Artist Care - ADT`), and the 🚥 Run Monitor
DB (Notion) for the Robots tab.

---

## Rules that are easy to break

These are the ones that have actually cost us a wrong board. Each is covered by
`test_adt_parsing.py`, which runs in CI **before** every build.

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

### 5. Same-minute events order by email receipt

Subjects carry `HH:MM` only, and Gmail lists newest-first. When a disarm and an
arm share a minute, the panel card and the event stream disagreed. Every ordering
sorts on `(panel minute, email receipt ts)`.

### 6. Panel state is durable, not derived-per-build

`panel-state.json` is committed with each build and remembers every studio's last
known arm/disarm **indefinitely**. Precedence: today's events → the 5-day Gmail
lookback → the stored file. It is never blanked on a Gmail fallback. A studio
quiet for a week must not regress the board to "Unknown".

### 7. Alarms clear; troubles do not

An **alarm** is outstanding only if nobody touched that panel at/after it — a
disarm means someone was there. A **trouble** (tamper / malfunction / low battery
/ power loss) stands until ADT says otherwise; disarming does not clear it.

> ADT mail does not carry every trouble Alarm.com knows about. The live panel
> snapshot (partitions, bypassed sensors, trouble conditions) lives behind the
> alarm connector, which the cloud build cannot reach. Getting that into the
> build would need the Alarm.com credentials in Actions — not done.

### 8. The staff rail is people only

Only rostered names render: **Junyan, KyJah, Ela, Stefan, Donny**
(`STAFF_ROSTER`). Unassigned placeholders — `Need FBS`, `Need Monitoring`,
`Studio Viewing Support`, `Open the Studio`, `Close the Studio` — never appear;
they are coverage bookkeeping, not a person on site. Open/Close blocks were also
retired at the source (🗓️ Skill: Staff Time Blocker, 2026-07-31).

### 9. Colours follow Skedda

| Colour | Meaning |
|---|---|
| 🔵 blue | recurring renter (contract / Fixed Option) |
| 🟢 green | Artist Plan (AAP / 1AP) |
| 🟡 yellow | one-off booking, not on contract |
| 🔴 red | missed booking (no arrival logged) |

Skedda's tag colours don't travel through the ICS feed, so `plan_of()`
reconstructs them from the booking title + recurrence. AAP and Fixed Option are
explicit in titles; a recurring renter with neither marker is inferred from RRULE.

### 10. Clients poll `version.json`, not the page

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

### 11. Everything is Toronto time

Junyan reads these from Brazil. Both pages derive "now" from
`Intl.DateTimeFormat` parts in `America/Toronto` — never the device clock, never
`toLocaleString()` parsing (engine-dependent). The mobile clock is labelled
TORONTO. `America/Toronto` (not a fixed EST offset) so DST follows automatically.

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

The build is gated to 07:00–02:59 Toronto (quiet 03:00–06:59) and skips green
until `NOTION_TOKEN` is set. `workflow_dispatch` bypasses the time gate.
