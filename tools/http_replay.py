#!/usr/bin/env python3
"""Record/replay harness that proves two build.py versions produce the same bytes.

    python tools/http_replay.py record FIXTURES BUILD_DIR [--env-out FILE]
    python tools/http_replay.py replay FIXTURES BUILD_DIR [--env-out FILE]

`record` runs BUILD_DIR/build.py against the live services and writes every
HTTP exchange (requests.*) to FIXTURES, along with the clock it ran at.
`replay` runs another BUILD_DIR/build.py with the same clock and serves every
request from FIXTURES — no network — so the only thing that can differ between
the two runs' index.html / mobile.html / *.json is the code. The workflow
verify-build-equivalence.yml records main's build.py and replays the branch's,
then diffs.

Requests are keyed on method, URL (minus the ICS `_cb` cache-buster), params
and body, so the order they are issued in — which is exactly what the
parallel build changes — does not matter. Identical repeated requests replay
in recorded order; once exhausted the last answer repeats.

FIXTURES holds live Notion, Gmail and calendar payloads. It belongs on the
runner's disk for the length of the job and nowhere else. Never upload it.

`--env-out` points GITHUB_ENV at a file so emit_fallback_note()'s lines — the
commit message's source — can be diffed too.

Only `requests` traffic is recorded. skedda_names.py speaks urllib, and in
Actions it never gets that far (no cookie), so replay simply blocks urllib.
"""
import base64
import hashlib
import importlib.util
import json
import os
import sys
import re
import time
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import requests.api
from requests.structures import CaseInsensitiveDict


def _canon_url(url):
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_cb"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(q)), ""))


def _key(method, url, kwargs):
    params = kwargs.get("params")
    if isinstance(params, dict):
        params = sorted(params.items())
    body = kwargs.get("json")
    data = kwargs.get("data")
    if isinstance(data, dict):
        data = sorted(data.items())
    raw = json.dumps([method.upper(), _canon_url(url), params, body, data],
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class Recorder:
    def __init__(self, path):
        self.path, self.calls, self.misses = path, {}, []
        self.mode = None
        self.now = None
        self._real = requests.api.request

    # -- record --------------------------------------------------------------
    def record(self, method, url, **kwargs):
        r = self._real(method, url, **kwargs)
        k = _key(method, url, kwargs)
        self.calls.setdefault(k, []).append({
            "status": r.status_code,
            "reason": r.reason,
            "url": r.url,
            "encoding": r.encoding,
            "headers": dict(r.headers),
            "content": base64.b64encode(r.content).decode("ascii"),
        })
        return r

    # -- replay --------------------------------------------------------------
    def replay(self, method, url, **kwargs):
        k = _key(method, url, kwargs)
        answers = self.calls.get(k)
        if not answers:
            self.misses.append(f"{method.upper()} {_canon_url(url)[:80]}")
            raise requests.ConnectionError(f"replay: no fixture for {method.upper()} {_canon_url(url)[:80]}")
        rec = answers.pop(0) if len(answers) > 1 else answers[0]
        r = requests.Response()
        r.status_code = rec["status"]
        r.reason = rec["reason"]
        r.url = rec["url"]
        r.encoding = rec["encoding"]
        r.headers = CaseInsensitiveDict(rec["headers"])
        r._content = base64.b64decode(rec["content"])
        return r

    def save(self, now):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"now": now.isoformat(), "calls": self.calls}, fh)

    def load(self):
        with open(self.path, encoding="utf-8") as fh:
            d = json.load(fh)
        self.calls = d["calls"]
        return datetime.fromisoformat(d["now"])


def frozen_datetime(fixed):
    """A datetime whose now() is always `fixed` (in the requested zone).

    Installed as build.datetime. build.py's `isinstance(x, datetime)` checks
    must keep passing for the plain datetimes icalendar hands it, hence the
    metaclass: an instance of the real class is an instance of this one."""
    class _Meta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, datetime)

    class Frozen(datetime, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):
            d = fixed if tz is None else fixed.astimezone(tz)
            return cls(d.year, d.month, d.day, d.hour, d.minute, d.second,
                       d.microsecond, d.tzinfo, fold=d.fold)

        # build.py hands window bounds to parse worker processes. A local class
        # cannot be pickled by reference, so cross the boundary as the plain
        # datetime this stands in for — the value is all the parse reads.
        def __reduce_ex__(self, protocol):
            plain = datetime(self.year, self.month, self.day, self.hour, self.minute,
                             self.second, self.microsecond, self.tzinfo, fold=self.fold)
            return plain.__reduce_ex__(protocol)
    Frozen.__name__ = Frozen.__qualname__ = "datetime"
    return Frozen


def load_build(build_dir):
    build_dir = os.path.abspath(build_dir)
    sys.path.insert(0, build_dir)
    spec = importlib.util.spec_from_file_location("build", os.path.join(build_dir, "build.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv):
    if len(argv) < 3 or argv[0] not in ("record", "replay"):
        print(__doc__)
        return 2
    mode, fixtures, build_dir = argv[0], argv[1], argv[2]
    env_out = None
    if "--env-out" in argv:
        env_out = argv[argv.index("--env-out") + 1]
        open(env_out, "w").close()
        os.environ["GITHUB_ENV"] = env_out

    rec = Recorder(fixtures)
    if mode == "record":
        now = datetime.now().astimezone()
        requests.api.request = rec.record
    else:
        now = rec.load()
        requests.api.request = rec.replay
        # No network in replay: anything not in the fixtures is a fault.
        import urllib.request
        def _no_net(*a, **k):
            raise OSError("replay: urllib blocked")
        urllib.request.urlopen = _no_net

    build = load_build(build_dir)
    build.datetime = frozen_datetime(now)
    t0 = time.monotonic()
    code = 0
    try:
        build.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        print(f"HARNESS: {mode} finished in {time.monotonic() - t0:.2f}s, exit {code}, "
              f"{sum(len(v) for v in rec.calls.values())} exchanges on file", flush=True)
        if mode == "record":
            rec.save(now)
        if rec.misses:
            print(f"HARNESS: {len(rec.misses)} request(s) had no fixture:", flush=True)
            for m in rec.misses:
                print(f"  MISS {m}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
