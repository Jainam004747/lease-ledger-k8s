"""python -m app.migrate: wait for the database, then apply migrations. Runs as an init container."""
import os
import sys
import time

import psycopg

from .db import MigrationError, migrate, url


def main():
    deadline = time.monotonic() + int(os.environ.get("DB_WAIT_SECONDS", "60"))
    while True:
        try:
            conn = psycopg.connect(url(), autocommit=True, connect_timeout=3)
            break
        except psycopg.OperationalError as e:
            if time.monotonic() > deadline:
                print(f"database not reachable: {str(e).strip().splitlines()[0]}", flush=True)
                return 1
            print("waiting for the database...", flush=True)
            time.sleep(2)
    with conn:
        print("migrations:", flush=True)
        try:
            migrate(conn, log=lambda s: print(s, flush=True))
        except MigrationError as e:
            print(f"ERROR: {e}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
