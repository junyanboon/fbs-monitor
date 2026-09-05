"""Write a claimed shift to the Staff Google Calendar.

The builder's reconcile step (build.reconcile_open_shifts) treats a placeholder
as claimed when a rostered, named event covers the same day, role, time and
studio. So a claim is exactly that: one event on the Staff calendar, titled the
way the Planner titles a named shift, with the studio in the description where
build.RE_SHIFT_STUDIO looks for it. Nothing else here is load-bearing.

Auth is a Google service account (GOOGLE_SERVICE_ACCOUNT_JSON, or _FILE on a laptop) that the Staff
calendar has been shared with at "Make changes to events". Plain REST; the
google-api-python-client discovery stack is not worth its weight for two calls.
"""
from __future__ import annotations

import json
import os

import requests

CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars"
SCOPE = "https://www.googleapis.com/auth/calendar.events"

# Titles must satisfy build._staff_identity: "<RosterName> … (FBS|Monitoring|Viewing)".
ROLE_TITLE = {
    "FBS": "{name} FBS",
    "Monitoring": "{name} Monitoring",
    "Viewing": "{name} Studio Viewing Support",
    # Open/Close has no identity shape in the builder; the event still lands
    # on the calendar for humans, and the local claims ledger hides the row.
    "Open/Close": "{name} Open/Close the Studio",
    "Close": "{name} Close the Studio",
}


class NotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool((os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
                 or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"))
                and os.environ.get("STAFF_CALENDAR_ID"))


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    # Vercel has no files to point at, so the key JSON itself is the env var.
    # A laptop may still use a path.
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    else:
        path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not path or not os.path.exists(path):
            raise NotConfigured("GOOGLE_SERVICE_ACCOUNT_JSON / _FILE is unset or missing")
        creds = service_account.Credentials.from_service_account_file(path, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def _calendar_id() -> str:
    cid = os.environ.get("STAFF_CALENDAR_ID")
    if not cid:
        raise NotConfigured("STAFF_CALENDAR_ID is unset")
    return cid


def event_body(name: str, email: str, shift: dict) -> dict:
    title = ROLE_TITLE.get(shift["role"], "{name} " + shift["role"]).format(name=name)
    studio_line = f"Studio {shift['studio']}" if shift.get("studio") else "Studio: (not given)"
    return {
        "summary": title,
        "description": f"{studio_line}\nClaimed on FBS Monitor by {name} <{email}>",
        "start": {"dateTime": shift["startISO"], "timeZone": "America/Toronto"},
        "end": {"dateTime": shift["endISO"], "timeZone": "America/Toronto"},
        "extendedProperties": {"private": {"fbsMonitorShiftId": shift["id"]}},
    }


def create_event(name: str, email: str, shift: dict) -> str:
    """Create the named shift event. Returns the Google event id."""
    r = requests.post(f"{CALENDAR_API}/{_calendar_id()}/events",
                      headers={"Authorization": f"Bearer {_token()}"},
                      json=event_body(name, email, shift), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Calendar insert failed: {r.status_code} {r.text[:300]}")
    return r.json()["id"]


def delete_event(event_id: str) -> None:
    r = requests.delete(f"{CALENDAR_API}/{_calendar_id()}/events/{event_id}",
                        headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
    if r.status_code not in (200, 204, 404, 410):
        raise RuntimeError(f"Calendar delete failed: {r.status_code} {r.text[:300]}")
