"""One small database door with two backends.

  Postgres   DATABASE_URL set — production on Vercel (Neon via the Vercel
             marketplace, or any Postgres). A serverless function has no disk
             that survives a request, so this is where accounts, claims AND
             the built pages live.
  SQLite     no DATABASE_URL — a developer laptop and the tests. One file under
             FBS_DATA_DIR.

The SQL in auth.py / app.py is written once, in SQLite's `?` placeholder
style, and the Postgres side rewrites `?` → `%s`. Keep the queries to the
overlap both engines share: no `INSERT OR …`, no `RETURNING`-free lastrowid
tricks, no engine-specific types outside SCHEMA below.

Connections are per request on purpose. On Vercel every invocation may be a
fresh process, and a pooled Neon URL (`-pooler` in the host) makes that cheap.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  pw_hash     BLOB NOT NULL,
  salt        BLOB NOT NULL,
  must_change INTEGER NOT NULL DEFAULT 1,
  created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
  shift_id       TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL REFERENCES users(id),
  name           TEXT NOT NULL,
  role           TEXT NOT NULL,
  studio         TEXT,
  date           TEXT NOT NULL,
  start_iso      TEXT NOT NULL,
  end_iso        TEXT NOT NULL,
  gcal_event_id  TEXT,
  created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
  addr  TEXT NOT NULL,
  ts    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
  name        TEXT PRIMARY KEY,
  body        TEXT NOT NULL,
  updated_at  REAL NOT NULL
);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS users (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  pw_hash     BYTEA NOT NULL,
  salt        BYTEA NOT NULL,
  must_change INTEGER NOT NULL DEFAULT 1,
  created_at  DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
  shift_id       TEXT PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES users(id),
  name           TEXT NOT NULL,
  role           TEXT NOT NULL,
  studio         TEXT,
  date           TEXT NOT NULL,
  start_iso      TEXT NOT NULL,
  end_iso        TEXT NOT NULL,
  gcal_event_id  TEXT,
  created_at     DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
  addr  TEXT NOT NULL,
  ts    DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
  name        TEXT PRIMARY KEY,
  body        TEXT NOT NULL,
  updated_at  DOUBLE PRECISION NOT NULL
);
"""


class Connection:
    """Uniform face over sqlite3 / psycopg: execute(sql, params) → rows as dicts."""

    def __init__(self, raw, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params=()) -> "Cursor":
        cur = self.raw.execute(self._sql(sql), tuple(params))
        return Cursor(cur, self.postgres)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:  # noqa: BLE001
            pass


class Cursor:
    def __init__(self, raw, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def _cols(self):
        return [d[0] for d in (self.raw.description or [])]

    def fetchone(self):
        row = self.raw.fetchone()
        if row is None:
            return None
        return dict(zip(self._cols(), row))

    def fetchall(self):
        cols = self._cols()
        return [dict(zip(cols, r)) for r in self.raw.fetchall()]

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount


def is_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def connect() -> Connection:
    url = os.environ.get("DATABASE_URL")
    if url:
        import psycopg
        raw = psycopg.connect(url, autocommit=False, connect_timeout=10)
        con = Connection(raw, postgres=True)
        for stmt in SCHEMA_POSTGRES.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
        con.commit()
        return con
    data_dir = Path(os.environ.get("FBS_DATA_DIR") or HERE / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(data_dir / "fbs.db", check_same_thread=False)
    raw.executescript(SCHEMA_SQLITE)
    return Connection(raw, postgres=False)
