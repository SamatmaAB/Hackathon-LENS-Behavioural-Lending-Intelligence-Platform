"""
LENS Backend API
================
FastAPI service exposing:
  POST /api/generate          - (re)generate synthetic customers + transactions, run the engine
  GET  /api/stats              - KPI summary for the dashboard header
  GET  /api/leads              - ranked lead list (filterable by tier / search)
  GET  /api/leads/{customer_id}- full lead detail: triggers, income breakdown, transactions
  GET  /api/customers/{id}/transactions

Run with:  uvicorn app:app --reload --port 8000
"""

import os
import sqlite3
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import data_gen
import engine

DB_PATH = os.path.join(os.path.dirname(__file__), "lens.db")

app = FastAPI(title="LENS — Behavioural Intelligence Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_exists():
    return os.path.exists(DB_PATH)


@app.on_event("startup")
def ensure_data():
    if not db_exists():
        data_gen.build_database(DB_PATH, n_customers=150, seed=42)
        engine.run_engine(DB_PATH)


@app.post("/api/generate")
def generate(n_customers: int = Query(150, ge=20, le=1000), seed: int = Query(None)):
    seed = seed if seed is not None else datetime.now().microsecond
    n_cust, n_txn = data_gen.build_database(DB_PATH, n_customers=n_customers, seed=seed)
    summary = engine.run_engine(DB_PATH)
    return {"customers_generated": n_cust, "transactions_generated": n_txn, **summary}


@app.get("/api/stats")
def stats():
    if not db_exists():
        raise HTTPException(404, "No dataset yet — call POST /api/generate")
    conn = get_conn()
    cur = conn.cursor()
    total_customers = cur.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    total_leads = cur.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    tiers = dict(cur.execute("SELECT tier, COUNT(*) FROM leads GROUP BY tier").fetchall())
    avg_hours = cur.execute("SELECT AVG(hours_to_lead) FROM leads").fetchone()[0]
    match_acc = cur.execute("SELECT AVG(match_correct) FROM leads").fetchone()[0]
    avg_income_dev = cur.execute("SELECT AVG(income_accuracy_pct) FROM leads "
                                  "WHERE income_accuracy_pct IS NOT NULL").fetchone()[0]
    avg_intent = cur.execute("SELECT AVG(intent_score) FROM leads").fetchone()[0]
    conn.close()
    return {
        "total_customers": total_customers,
        "total_leads": total_leads,
        "lead_conversion_rate_pct": round(100 * total_leads / total_customers, 1) if total_customers else 0,
        "tier_distribution": {"Tier 1": tiers.get("Tier 1", 0), "Tier 2": tiers.get("Tier 2", 0),
                               "Tier 3": tiers.get("Tier 3", 0)},
        "avg_hours_to_lead": round(avg_hours, 2) if avg_hours else 0,
        "loan_type_accuracy_pct": round(100 * match_acc, 1) if match_acc else 0,
        "avg_income_deviation_pct": round(avg_income_dev, 1) if avg_income_dev else 0,
        "avg_intent_score": round(avg_intent, 1) if avg_intent else 0,
        "industry_benchmark": {
            "lead_conversion_rate_pct": 10, "time_to_lead_hours": 48,
            "false_positive_rate_pct": 45,
        },
    }


@app.get("/api/leads")
def list_leads(tier: str = None, search: str = None, sort: str = "trust_score",
               limit: int = 100):
    conn = get_conn()
    cur = conn.cursor()
    q = """SELECT l.*, c.name, c.age, c.city, c.state, c.employment_type, c.declared_income
           FROM leads l JOIN customers c ON c.customer_id = l.customer_id WHERE 1=1"""
    params = []
    if tier:
        q += " AND l.tier = ?"
        params.append(tier)
    if search:
        q += " AND (c.name LIKE ? OR c.customer_id LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sort_col = sort if sort in ("trust_score", "intent_score", "hours_to_lead") else "trust_score"
    q += f" ORDER BY {sort_col} DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in cur.execute(q, params)]
    conn.close()
    for r in rows:
        r["triggers_fired"] = r["triggers_fired"].split(",") if r["triggers_fired"] else []
        r["trigger_labels"] = [engine.TRIGGER_LABELS.get(t, t) for t in r["triggers_fired"]]
    return rows


@app.get("/api/leads/{customer_id}")
def lead_detail(customer_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cust_row = cur.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if not cust_row:
        conn.close()
        raise HTTPException(404, "Customer not found")
    cust = dict(cust_row)

    lead_row = cur.execute("SELECT * FROM leads WHERE customer_id=?", (customer_id,)).fetchone()
    txns = [dict(r) for r in cur.execute(
        "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp DESC", (customer_id,))]
    conn.close()

    result = {"customer": cust, "transactions": txns, "is_lead": bool(lead_row)}

    if lead_row:
        lead = dict(lead_row)
        triggers = lead["triggers_fired"].split(",") if lead["triggers_fired"] else []
        lead["triggers_fired"] = [
            {"code": t, "label": engine.TRIGGER_LABELS.get(t, t), "weight": engine.TRIGGER_WEIGHTS.get(t, 0)}
            for t in triggers
        ]
        result["lead"] = lead

        # recompute the live income breakdown so the UI can show the method/clusters
        result["income_breakdown"] = engine.reconstruct_income(cust, txns)

    return result


@app.get("/api/customers/{customer_id}/transactions")
def customer_transactions(customer_id: str):
    conn = get_conn()
    cur = conn.cursor()
    rows = [dict(r) for r in cur.execute(
        "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp DESC", (customer_id,))]
    conn.close()
    if not rows:
        raise HTTPException(404, "No transactions found")
    return rows


@app.get("/api/health")
def health():
    return {"status": "ok", "data_ready": db_exists()}
