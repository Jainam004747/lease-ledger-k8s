"""HTTP API for lease-ledger.

Health endpoints are split the way Kubernetes uses them:
  /healthz  liveness: the process is up. Never touches the database, so a database outage
            doesn't make Kubernetes restart every pod in a loop.
  /readyz   readiness: the database answers and every migration in this image is applied.
            Until then the pod gets no traffic.
"""
import json
import logging
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from . import db

VERSION = os.environ.get("APP_VERSION", "dev")
COMMIT = os.environ.get("GIT_SHA", "unknown")
POD = socket.gethostname()

log = logging.getLogger("api")
logging.basicConfig(stream=sys.stdout, level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Request duration", ["route"])

pool = db.make_pool()


@asynccontextmanager
async def lifespan(app):
    pool.open(wait=False)
    log.info(json.dumps({"event": "startup", "version": VERSION, "commit": COMMIT, "pod": POD}))
    yield
    log.info(json.dumps({"event": "shutdown", "pod": POD}))
    pool.close()


app = FastAPI(title="lease-ledger API", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        took = time.perf_counter() - t0
        REQUESTS.labels(request.method, path, status).inc()
        LATENCY.labels(path).observe(took)
        if path not in ("/healthz", "/readyz", "/metrics"):
            log.info(json.dumps({"method": request.method, "path": request.url.path, "status": status,
                                 "ms": round(took * 1000, 1), "pod": POD}))


def money(v):
    return float(v) if isinstance(v, Decimal) else v


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        with pool.connection(timeout=2) as conn:
            if not db.schema_ready(conn):
                return JSONResponse({"status": "waiting for migrations"}, status_code=503)
    except Exception as e:                                  # noqa: BLE001 - any failure means not ready
        return JSONResponse({"status": "database unavailable", "error": type(e).__name__}, status_code=503)
    return {"status": "ready"}


@app.get("/version")
def version():
    return {"version": VERSION, "commit": COMMIT, "pod": POD}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/leases/{lease_id}/statement")
def statement(lease_id: str, last: int = 12):
    with pool.connection() as conn:
        head = conn.execute("""select l.lease_id, t.name, u.property_id, u.unit_no, l.monthly_rent
                               from leases l join tenants t using (tenant_id) join units u using (unit_id)
                               where lease_id = %s""", [lease_id]).fetchone()
        if not head:
            raise HTTPException(404, f"no lease {lease_id}")
        rows = conn.execute("""
            select entry_date, memo, debit, credit,
                   sum(debit - credit) over (order by entry_date, entry_id, line_no) as balance
            from journal_lines join journal_entries using (entry_id, entry_date)
            where lease_id = %s and account = '1100'
            order by entry_date, entry_id, line_no""", [lease_id]).fetchall()
    return {"lease_id": head[0], "tenant": head[1], "property_id": head[2], "unit": head[3],
            "monthly_rent": money(head[4]),
            "lines": [{"date": str(r[0]), "description": r[1], "charge": money(r[2]) or None,
                       "payment": money(r[3]) or None, "balance": money(r[4])} for r in rows[-last:]]}


class Payment(BaseModel):
    lease_id: str
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    received_on: date
    reference: str = Field(min_length=1, max_length=40)


@app.post("/payments", status_code=201)
def record_payment(p: Payment):
    with pool.connection() as conn:
        try:
            with conn.transaction():
                pid = conn.execute("select record_payment(%s, %s, %s, %s)",
                                   [p.lease_id, p.amount, p.received_on, p.reference]).fetchone()[0]
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"payment {p.reference} was already recorded for {p.lease_id}") from None
        except psycopg.errors.RaiseException as e:
            raise HTTPException(404, e.diag.message_primary) from None
        applied = conn.execute("""select c.kind, c.period, a.amount from allocations a join charges c using (charge_id)
                                  where a.payment_id = %s order by c.due_date""", [pid]).fetchall()
    used = sum(a[2] for a in applied)
    return {"payment_id": pid, "applied": [{"kind": k, "period": str(per)[:7], "amount": money(a)} for k, per, a in applied],
            "credit": money(p.amount - used)}


@app.get("/reports/aging")
def aging(as_of: date):
    with pool.connection() as conn:
        rows = conn.execute("select * from aged_receivables(%s)", [as_of]).fetchall()
    keys = ["property_id", "lease_id", "tenant", "current_0_30", "days_31_60", "days_61_90", "over_90", "credit", "total"]
    return {"as_of": str(as_of), "leases": [dict(zip(keys, map(money, r), strict=True)) for r in rows],
            "total": money(sum(r[8] for r in rows))}


@app.get("/reports/reconcile")
def reconcile(as_of: date):
    with pool.connection() as conn:
        sub, gl, off = conn.execute("""
            select (select coalesce(sum(total), 0) from aged_receivables(%(d)s)),
                   (select coalesce(sum(balance), 0) from gl_receivables(%(d)s)),
                   (select count(*) from aged_receivables(%(d)s) s full join gl_receivables(%(d)s) g using (lease_id)
                     where coalesce(s.total, 0) <> coalesce(g.balance, 0))""", {"d": as_of}).fetchone()
    ok = sub == gl and off == 0
    return JSONResponse({"as_of": str(as_of), "gl_receivables": money(gl), "subledger": money(sub),
                         "leases_off": off, "ok": ok}, status_code=200 if ok else 409)
