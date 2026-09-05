# Vercel edition

The same board, built by the same `build.py` in GitHub Actions, served from
Vercel behind a staff login — with **Claim** on every open shift. The GitHub
Pages edition keeps running untouched; this is a second, independent copy, so
one going down does not take the other.

```
GitHub Actions (unchanged, every ~15 min)
  build.py → index.html, mobile.html, version.json, open-shifts.json
     ├─ git commit  → GitHub Pages   (public, read-only, as before)
     └─ publish.py  → Postgres `pages` table
                          ↑
Vercel (this)             │
  api/index.py → server/app.py
     serves the pages from the table behind a login
     writes a claim to the Staff calendar, records it in `claims`
```

Why this split: `build.py` fetches five calendars, Notion and Gmail and can
run for a minute or more. That is a poor fit for a serverless function, so
the builder stays where it is. The Vercel side does only short requests —
sign-in, identity, claim — and reads the built files from the database
because a function has no disk that survives a request.

## What you need

| Thing | Why |
|---|---|
| A Vercel project connected to the `fbs-monitor` repo (Pro plan for business use) | Every push deploys |
| A Postgres database (Vercel → Storage → **Neon**, or any Postgres) | Accounts, claims, and the built pages |
| A Google service account JSON with the **Staff calendar shared to it** at *Make changes to events* | A claim writes a named shift event |
| The Staff calendar's id (Google Calendar → settings → *Integrate calendar*) | Which calendar the placeholders live on |

## Set up (once)

1. **Vercel → Add New → Project → import `junyanboon/fbs-monitor`.**
   Framework preset: *Other*. Leave build and output settings empty.
   `vercel.json` routes every request to `api/index.py`; `.vercelignore`
   keeps the built pages and the builder out of the upload.

2. **Storage → Create → Neon Postgres**, attach it to the project. This sets
   `DATABASE_URL` on the project. Copy the *pooled* connection string — the
   host contains `-pooler` — for the two places below.

3. **Project → Settings → Environment Variables**, add:

   | Name | Value |
   |---|---|
   | `FBS_SECRET_KEY` | 48+ random characters, see below |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the service-account key file, pasted whole |
   | `STAFF_CALENDAR_ID` | e.g. `abc123@group.calendar.google.com` |

   ```bash
   python3 -c 'import secrets;print(secrets.token_urlsafe(48))'
   ```

4. **GitHub → repo → Settings → Secrets → Actions**, add `DATABASE_URL` with
   the same pooled string. From the next build on, `publish.py` uploads
   every edition. Until then `/healthz` reports `"built": null`.

5. **Create the staff accounts** from your laptop, against the same database:

   ```bash
   cd fbs-monitor && pip install -r server/requirements.txt
   ```
   ```bash
   DATABASE_URL='postgres://…-pooler…' python -m server.users add "KyJah" kyjah@example.com
   ```

   Repeat for Junyan, Ela, Stefan, Donny (the roster in `build.STAFF_ROSTER`).
   Each command prints a temporary password. The person signs in with it and
   is sent to `/account` to set their own. There is no self-signup.

6. **Domain**: Project → Settings → Domains → add `fbs.danceannex.ca` (or
   similar) and create the CNAME Vercel shows. HTTPS is automatic. The
   session cookie is `Secure`, so the app is HTTPS-only by design.

7. Open `https://<your-domain>/healthz`. You want:

   ```json
   {"ok": true, "built": "2026-09-05T…", "calendar": true, "db": "postgres"}
   ```

## How a claim flows

1. Staff taps **Claim** on an open shift. The page POSTs the shift's id.
2. The server checks the id is still in the latest `open-shifts.json`, that
   the shift has not ended, and that nobody else holds it.
3. It creates one event on the Staff calendar, titled the way the Planner
   titles a named shift (`KyJah FBS`, studio in the description), then
   records the claim.
4. Both pages overlay `/api/claims` on the board at once, so the row reads
   *Claimed by KyJah* without waiting for a rebuild.
5. The next Actions build sees the calendar event, and
   `build.reconcile_open_shifts` drops the placeholder for good.

**Release** deletes that event and the claim. Only the person who claimed
can release.

**Offline**: the pages keep the last-known identity and a claim outbox in
`localStorage`. A claim made without signal is shown as *waiting for
signal* and sent the next time the page loads with a connection. The server
is the only judge — an outbox replay that lost the race gets the server's
answer, never a second event.

## Day to day

- **Deploy a code change**: push to `main`. Vercel builds it in about a
  minute. The board data is not part of the deploy; it keeps arriving from
  Actions.
- **Reset a password**:
  `DATABASE_URL=… python -m server.users set-password kyjah@example.com`
- **Remove someone**: `… python -m server.users remove kyjah@example.com`
- **Sign everyone out**: rotate `FBS_SECRET_KEY` in Vercel and redeploy.
- **Rotate the calendar key**: replace `GOOGLE_SERVICE_ACCOUNT_JSON`,
  redeploy. Claims made with the old key keep their event ids and still
  release.
- **Actions down**: both editions keep serving the last build. The pages
  show their own data age and mark themselves stale past 40 minutes.

## Where things fail, and what it looks like

| Symptom | Cause | Fix |
|---|---|---|
| `/healthz` → `"built": null` | `publish.py` never ran: `DATABASE_URL` missing in GitHub secrets, or no build since | Add the secret; run the workflow by hand |
| Claim → *Calendar write is not configured* | `GOOGLE_SERVICE_ACCOUNT_JSON` or `STAFF_CALENDAR_ID` unset on Vercel | Set them, redeploy |
| Claim → *Calendar write failed: 403* | The Staff calendar is not shared to the service account at *Make changes to events* | Share it |
| Claim → *Calendar write failed: 404* | Wrong `STAFF_CALENDAR_ID` | Copy it from the calendar's *Integrate calendar* panel |
| Row still shows **Claim** on the Pages edition | Expected: GitHub Pages has no server, so it keeps the Notion link | — |
| Sign-in page says *Too many attempts* | 10 failed tries from one address in 10 minutes | Wait, or clear `login_attempts` |

## Local run (no Vercel, no Postgres)

```bash
FBS_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') CLAIM_WITHOUT_CALENDAR=1 .venv/bin/uvicorn server.app:app --reload
```

Without `DATABASE_URL` the app uses SQLite under `server/data/` and reads the
files `build.py` wrote beside it. Tests: `python -m pytest test_server.py`.
