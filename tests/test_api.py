import shutil
import threading

import psycopg
import pytest
from conftest import fresh_db

from app import db, hammer
from app.seed import seed


def test_liveness_does_not_need_the_database(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_not_ready_until_migrations_are_applied(client, api_url):
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "waiting for migrations"
    with psycopg.connect(api_url, autocommit=True) as conn:
        db.migrate(conn, log=lambda s: None)
        seed(conn, progress=lambda s: None)
    assert client.get("/readyz").json() == {"status": "ready"}


def test_pods_starting_together_migrate_once():
    url = fresh_db("ledger_migrate_race")
    logs, errors = [], []

    def run():
        try:
            with psycopg.connect(url, autocommit=True) as conn:
                db.migrate(conn, log=logs.append)
        except Exception as e:                   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sum("  applied" in line for line in logs) == len(db.expected_versions())
    with psycopg.connect(url) as conn:
        assert conn.execute("select count(*) from schema_migrations").fetchone()[0] == len(db.expected_versions())


def test_edited_migration_is_refused(api_url, tmp_path):
    folder = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS, folder)
    (folder / "003_billing.sql").write_text((folder / "003_billing.sql").read_text() + "\n-- hotfix\n")
    with psycopg.connect(api_url, autocommit=True) as conn:
        with pytest.raises(db.MigrationError, match="003_billing.sql was changed"):
            db.migrate(conn, folder, log=lambda s: None)


def test_payment_is_applied_oldest_first(client):
    r = client.post("/payments", json={"lease_id": "L-0043", "amount": "4000", "received_on": "2026-10-29", "reference": "EFT-T1"})
    assert r.status_code == 201
    body = r.json()
    assert [a["period"] for a in body["applied"]][:2] == ["2026-08", "2026-08"]
    assert body["credit"] == 330.0


def test_payment_errors(client):
    p = {"lease_id": "L-0043", "amount": "10", "received_on": "2026-10-29", "reference": "EFT-T1"}
    assert client.post("/payments", json=p).status_code == 409
    assert client.post("/payments", json={**p, "lease_id": "L-9999", "reference": "X"}).status_code == 404
    assert client.post("/payments", json={**p, "amount": "-5", "reference": "Y"}).status_code == 422


def test_statement_and_reports(client):
    s = client.get("/leases/L-0004/statement", params={"last": 3}).json()
    assert s["tenant"] == "Hana Ali" and len(s["lines"]) == 3
    assert client.get("/leases/L-9999/statement").status_code == 404
    aging = client.get("/reports/aging", params={"as_of": "2026-10-29"}).json()
    rec = client.get("/reports/reconcile", params={"as_of": "2026-10-29"})
    assert rec.status_code == 200 and rec.json()["ok"]
    assert rec.json()["subledger"] == aging["total"]


def test_metrics_count_requests(client):
    client.get("/version")
    text = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/version",status="200"}' in text


def test_hammer_reports_failures():
    assert hammer.main(["--url", "http://127.0.0.1:9/version", "--seconds", "0.5", "--rate", "10"]) == 1
