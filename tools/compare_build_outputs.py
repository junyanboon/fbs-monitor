#!/usr/bin/env python3
"""Compare two build output directories.

    python tools/compare_build_outputs.py REF_DIR NEW_DIR [--strict]

Byte-for-byte first. Where the pages or open-shifts.json differ, the DATA
payload is compared structurally with ONE allowance: the order of rows in
`openShifts` and `staff` that share a slot. main's build has never been
byte-stable there — recurring_ical_events yields a series' edited instances
from a set of objects hashed by identity, so two edited placeholders on the
same slot come out either way round from one run to the next (seen twice on
2026-09-20, runs 35519735214 and 35520336231). Everything else — the template
bytes around the payload, every other key, every other row — must match
exactly. `--strict` drops the allowance (used for serial-vs-parallel, where the
same code must give the same bytes).

Exit 0 when equivalent, 1 otherwise. Prints one line per file.
"""
import json
import os
import re
import sys

FILES = ("index.html", "mobile.html", "panel-state.json", "booking-state.json",
         "open-shifts.json", "version.json", "notes.txt", "github.env")
ORDER_FREE = ("openShifts", "staff")
PAYLOAD = re.compile(r"/\*__DATA__\*/(.*?)/\*__END_DATA__\*/", re.S)


def canon(obj):
    """Sort the order-free lists by their full content."""
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for key in ORDER_FREE:
        if isinstance(out.get(key), list):
            out[key] = sorted(out[key], key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
    return out


def split_page(text):
    m = PAYLOAD.search(text)
    if not m:
        return None, text
    return json.loads(m.group(1)), text[:m.start(1)] + text[m.end(1):]


def equivalent(name, a, b):
    """(verdict, detail) for one file's contents."""
    if a == b:
        return True, "identical"
    if name.endswith(".html"):
        da, ra = split_page(a)
        db, rb = split_page(b)
        if da is None or db is None or ra != rb:
            return False, "DIFFERS outside the DATA payload"
    elif name == "open-shifts.json":
        da, db = json.loads(a), json.loads(b)
    else:
        return False, "DIFFERS"
    if canon(da) == canon(db):
        moved = sum(1 for k in ORDER_FREE
                    if isinstance(da.get(k), list) and da.get(k) != db.get(k))
        return True, f"identical up to same-slot order in {moved} of {ORDER_FREE}"
    keys = sorted(k for k in set(da) | set(db) if canon(da).get(k) != canon(db).get(k))
    return False, f"DIFFERS in payload keys {keys}"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    ref, new, strict = argv[0], argv[1], "--strict" in argv
    ok = True
    for name in FILES:
        pa, pb = os.path.join(ref, name), os.path.join(new, name)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            print(f"  MISSING    {name}")
            ok = False
            continue
        a = open(pa, encoding="utf-8").read()
        b = open(pb, encoding="utf-8").read()
        if strict:
            same, detail = (a == b), ("identical" if a == b else "DIFFERS")
        else:
            same, detail = equivalent(name, a, b)
        print(f"  {'ok   ' if same else 'FAIL '}  {name:20s} {len(a):>7} bytes  {detail}")
        ok = ok and same
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
