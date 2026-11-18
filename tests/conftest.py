import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

BASE = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")


def url_for(name):
    p = conninfo_to_dict(BASE)
    p["dbname"] = name
    return make_conninfo(**p)


def fresh_db(name):
    with psycopg.connect(url_for("postgres"), autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")
        admin.execute(f"create database {name}")
    return url_for(name)


@pytest.fixture(scope="session")
def api_url():
    """A database for the API tests. Starts empty; the first test checks /readyz says so."""
    url = fresh_db("ledger_api_test")
    os.environ["DATABASE_URL"] = url
    return url


@pytest.fixture(scope="session")
def client(api_url):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c
