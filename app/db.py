"""Database access and migrations.

Every pod runs migrations in an init container before the API starts. With several pods
starting at once they would race, so the runner takes a Postgres advisory lock first:
one pod applies the files, the others wait, then find nothing left to do.
"""
import hashlib
import os
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 7_340_001          # any fixed number; every migrator uses the same one


class MigrationError(Exception):
    pass


def url():
    return os.environ.get("DATABASE_URL", "postgresql://ledger:ledger@localhost:5432/ledger")


def expected_versions(folder=MIGRATIONS):
    return sorted(p.name.split("_", 1)[0] for p in Path(folder).glob("*.sql"))


def migrate(conn, folder=MIGRATIONS, log=print):
    conn.execute("select pg_advisory_lock(%s)", [LOCK_ID])
    try:
        conn.execute("""create table if not exists schema_migrations (
                            version    text primary key,
                            filename   text not null,
                            sha256     text not null,
                            applied_at timestamptz not null default now(),
                            applied_by text not null default current_user)""")
        applied = {v: h for v, h in conn.execute("select version, sha256 from schema_migrations")}
        for path in sorted(Path(folder).glob("*.sql")):
            version = path.name.split("_", 1)[0]
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if version in applied:
                if applied[version] != digest:
                    raise MigrationError(f"{path.name} was changed after it was applied")
                log(f"  {path.name:<22} already applied")
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("insert into schema_migrations (version, filename, sha256) values (%s, %s, %s)",
                             [version, path.name, digest])
            log(f"  {path.name:<22} applied")
    finally:
        conn.execute("select pg_advisory_unlock(%s)", [LOCK_ID])


def make_pool():
    return ConnectionPool(url(), min_size=1, max_size=int(os.environ.get("DB_POOL_SIZE", "5")),
                          kwargs={"autocommit": True}, open=False, timeout=5)


def schema_ready(conn):
    """True when every migration shipped in this image has been applied."""
    try:
        have = {v for (v,) in conn.execute("select version from schema_migrations")}
    except psycopg.errors.UndefinedTable:
        return False
    return set(expected_versions()) <= have
