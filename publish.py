#!/usr/bin/env python3
"""Upload the files build.py just wrote into the Vercel edition's database.

Runs at the end of every GitHub Actions build (after build.py, before the
commit). The Vercel app (server/app.py) has no disk that survives a request,
so it reads index.html, mobile.html, version.json and open-shifts.json from
the `pages` table instead. Same bytes as the Pages edition; one build, two
homes.

No DATABASE_URL → prints one line and exits 0. The Pages edition must never
fail because the Vercel one is not set up.

Order matters the same way it does on disk (README rule 11): the pages go up
first and version.json last, so a client that polls version.json never
reloads onto an edition the database does not hold yet.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["index.html", "mobile.html", "open-shifts.json", "booking-state.json",
         "panel-state.json", "version.json"]        # version.json LAST


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("publish: DATABASE_URL not set — Vercel edition skipped.")
        return 0
    sys.path.insert(0, str(HERE))
    from server import auth
    con = auth.connect()
    try:
        for name in FILES:
            path = HERE / name
            if not path.is_file():
                print(f"publish: {name} missing, skipped")
                continue
            auth.put_page(con, name, path.read_text(encoding="utf-8"))
            print(f"publish: {name} ({path.stat().st_size} bytes)")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
