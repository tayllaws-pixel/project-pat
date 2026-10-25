"""Minimal SQL migration runner (ADR-0004).

Each file in db/migrations is split into sections by `-- @db <name>` markers; a section runs only against the
database named (or `all`, or the common preamble before any marker). Applied versions are recorded with a checksum;
a changed file that was already applied is an error, never a silent re-run.

Usage: python3 db/migrate.py --dsn postgresql://... --db pat [--dry-run]
The splitting/planning logic is pure and unit-tested; the DB connection uses psycopg when present.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = re.compile(r"^--\s*@db\s+(\w+)\s*$", re.M)


def split_sections(sql: str) -> list[tuple[str, str]]:
    """Return [(db_name, sql_chunk)]; the chunk before the first marker is 'common'."""
    parts = MARK.split(sql)
    out = [("common", parts[0])]
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1]))
    return [(n, s) for n, s in out if s.strip()]


def sql_for_db(sql: str, db: str) -> str:
    return "\n".join(chunk for name, chunk in split_sections(sql) if name in ("common", "all", db))


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def migration_files() -> list[Path]:
    return sorted(p for p in (HERE / "migrations").glob("*.sql") if re.match(r"\d{4}_", p.name))


def plan(db: str, applied: dict[str, str]) -> list[tuple[str, str, str]]:
    """[(version, sql, checksum)] still to apply. Raises if an applied file changed."""
    todo = []
    for f in migration_files():
        version = f.name.split("_")[0]
        body = sql_for_db(f.read_text(), db)
        cs = checksum(body)
        if version in applied:
            if applied[version] != cs:
                raise RuntimeError(f"migration {version} already applied to {db} with a different checksum; write a new migration instead")
            continue
        todo.append((version, body, cs))
    return todo


def run(dsn: str, db: str, dry_run: bool = False) -> list[str]:
    import psycopg  # only needed for the real run
    done = []
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("SELECT version, checksum FROM schema_migrations")
        applied = dict(cur.fetchall())
        for version, body, cs in plan(db, applied):
            if dry_run:
                print(f"would apply {version} to {db} ({len(body)} bytes)")
                continue
            cur.execute(body)
            cur.execute("INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)", (version, cs))
            done.append(version)
        conn.commit()
    return done


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--db", required=True, choices=["pat", "wade", "sentinel"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print("applied:", run(a.dsn, a.db, a.dry_run))
    sys.exit(0)
