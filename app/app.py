"""Duka API - application tier for the observability capstone.

A small order API for a Kenyan shop. It writes one JSON log line per request (so CloudWatch
Logs Insights can filter on fields), is traced end to end by AWS X-Ray (Flask + PyMySQL are
auto-instrumented, so DB calls appear as subsegments), and has fault-injection endpoints used
by the mock failure test.
"""
import json
import logging
import math
import os
import random
import sys
import time
import uuid

import pymysql
from flask import Flask, g, jsonify, request
from aws_xray_sdk.core import patch_all, xray_recorder
from aws_xray_sdk.ext.flask.middleware import XRayMiddleware

SERVICE = "duka-api"
VERSION = os.environ.get("APP_VERSION", "dev")

# ---------------------------------------------------------------- structured logging
class JsonFormatter(logging.Formatter):
    def format(self, record):
        doc = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": SERVICE,
            "message": record.getMessage(),
        }
        doc.update(getattr(record, "fields", {}))
        return json.dumps(doc)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.setLevel(logging.INFO)
log.addHandler(handler)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def event(level, message, **fields):
    log.log(level, message, extra={"fields": fields})


# ---------------------------------------------------------------- tracing
xray_recorder.configure(
    service=SERVICE,
    daemon_address=os.environ.get("AWS_XRAY_DAEMON_ADDRESS", "127.0.0.1:2000"),
    context_missing="LOG_ERROR",
    sampling=False,  # trace every request: low volume lab, full visibility
)
patch_all()  # instruments pymysql, boto3, requests

app = Flask(__name__)
XRayMiddleware(app, xray_recorder)


def db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "duka"),
        connect_timeout=5,
        read_timeout=30,
        autocommit=True,
    )


def init_schema():
    for attempt in range(30):
        try:
            conn = db()
            with conn.cursor() as cur:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS orders (
                         id VARCHAR(36) PRIMARY KEY,
                         customer VARCHAR(64) NOT NULL,
                         county VARCHAR(32) NOT NULL,
                         amount_kes INT NOT NULL,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         INDEX idx_created (created_at)
                       )"""
                )
                cur.execute("SELECT COUNT(*) FROM orders")
                if cur.fetchone()[0] == 0:
                    counties = ["Nairobi", "Mombasa", "Kisumu", "Nakuru", "Eldoret", "Nyeri"]
                    rows = [
                        (str(uuid.uuid4()), f"customer-{i}", random.choice(counties), random.randint(200, 25000))
                        for i in range(2000)
                    ]
                    cur.executemany(
                        "INSERT INTO orders (id, customer, county, amount_kes) VALUES (%s,%s,%s,%s)", rows
                    )
            conn.close()
            event(logging.INFO, "schema ready")
            return
        except Exception as exc:  # RDS may still be starting
            event(logging.WARNING, "db not ready", attempt=attempt, error=str(exc))
            time.sleep(5)


# ---------------------------------------------------------------- request logging
@app.before_request
def start_timer():
    g.start = time.time()
    g.request_id = request.headers.get("X-Amzn-Trace-Id", str(uuid.uuid4()))


@app.after_request
def log_request(resp):
    latency_ms = round((time.time() - g.start) * 1000, 1)
    level = logging.ERROR if resp.status_code >= 500 else logging.INFO
    event(
        level,
        "request",
        method=request.method,
        path=request.path,
        status=resp.status_code,
        latency_ms=latency_ms,
        trace_id=g.request_id,
        client_ip=request.headers.get("X-Forwarded-For", request.remote_addr),
    )
    return resp


# ---------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    return jsonify(status="ok", service=SERVICE, version=VERSION)


@app.get("/api/orders")
def list_orders():
    conn = db()
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, customer, county, amount_kes FROM orders ORDER BY created_at DESC LIMIT 20")
        rows = cur.fetchall()
    conn.close()
    return jsonify(count=len(rows), orders=rows)


@app.get("/api/report")
def county_report():
    """Revenue per county. Slow mode adds a server-side delay so it lands in the slow query log."""
    slow = float(request.args.get("slow", "0"))
    conn = db()
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        if slow > 0:
            # SLEEP() inside the query makes MySQL itself see a slow statement
            cur.execute(
                "SELECT county, COUNT(*) AS orders, SUM(amount_kes) AS revenue_kes, SLEEP(%s) AS _ "
                "FROM orders GROUP BY county",
                (slow / 6,),
            )
        else:
            cur.execute("SELECT county, COUNT(*) AS orders, SUM(amount_kes) AS revenue_kes FROM orders GROUP BY county")
        rows = cur.fetchall()
    conn.close()
    for r in rows:
        r.pop("_", None)
        r["revenue_kes"] = int(r["revenue_kes"])
    return jsonify(counties=rows)


@app.post("/api/orders")
def create_order():
    body = request.get_json(silent=True) or {}
    order_id = str(uuid.uuid4())
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orders (id, customer, county, amount_kes) VALUES (%s,%s,%s,%s)",
            (order_id, body.get("customer", "walk-in"), body.get("county", "Nairobi"), int(body.get("amount_kes", 1000))),
        )
    conn.close()
    event(logging.INFO, "order created", order_id=order_id)
    return jsonify(id=order_id), 201


# ---------------------------------------------------------------- fault injection (mock failure test)
@app.get("/api/fault/error")
def fault_error():
    """Simulates a payment-gateway failure: logs an ERROR and returns 500."""
    event(logging.ERROR, "payment gateway timeout", upstream="mpesa-stk-push", error_code="UPSTREAM_TIMEOUT")
    return jsonify(error="payment gateway timeout"), 500


@app.get("/api/fault/cpu")
def fault_cpu():
    """Burns CPU for N seconds to drive the ECS CPU alarm."""
    seconds = min(float(request.args.get("seconds", "20")), 60)
    end = time.time() + seconds
    x = 0.0
    while time.time() < end:
        for i in range(10000):
            x += math.sqrt(i) * math.sin(i)
    return jsonify(burned_seconds=seconds)


if __name__ == "__main__":
    init_schema()
    app.run(host="0.0.0.0", port=8080)
