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
import secrets
import tempfile
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from backend import data_gen, db, engine
except ImportError:
    import data_gen, db, engine  # type: ignore[no-redef]


BASE_DIR = os.path.dirname(__file__)
DEFAULT_DB_PATH = os.path.join(tempfile.gettempdir(), "lens.db") if os.getenv("VERCEL") else os.path.join(BASE_DIR, "lens.db")
DB_PATH = os.getenv("LENS_DB_PATH", DEFAULT_DB_PATH)
SESSION_HOURS = 12
ROLES = {"admin", "relationship_manager", "analyst"}
WRITE_ROLES = {"admin", "relationship_manager"}

app = FastAPI(title="LENS — Behavioural Intelligence Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    return db.connect(DB_PATH)


def db_exists():
    return os.path.exists(DB_PATH)


AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('admin', 'relationship_manager', 'analyst')),
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
"""

AUTH_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('admin', 'relationship_manager', 'analyst')),
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
"""


def init_database():
    conn = get_conn()
    if not db.IS_POSTGRES:
        conn.execute("PRAGMA foreign_keys = ON")
    data_gen.create_schema(conn)
    db.executescript(conn, AUTH_SCHEMA_POSTGRES if db.IS_POSTGRES else AUTH_SCHEMA)
    conn.commit()
    conn.close()


@app.on_event("startup")
def ensure_schema_and_data():
    db_already_exists = db_exists()
    init_database()
    if not db_already_exists:
        conn = get_conn()
        customer_count = db.scalar(conn, "SELECT COUNT(*) FROM customers")
        conn.close()
        if customer_count == 0:
            data_gen.build_current_database(n_customers=150, seed=42)
            engine.run_engine(DB_PATH)


def hash_password(password: str, salt: Optional[str] = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return salt, digest.hex()


def verify_password(password: str, salt: str, stored_hash: str):
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def public_user(row):
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


def create_session(conn, user_id: int):
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=SESSION_HOURS)
    db.execute(
        conn,
        "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (token, user_id, expires_at.isoformat(), now.isoformat()),
    )
    return token, expires_at


def require_user(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    conn = get_conn()
    row = db.one(
        conn,
        """SELECT u.* FROM sessions s
           JOIN users u ON u.user_id = s.user_id
           WHERE s.token = ? AND s.expires_at > ?""",
        (token, datetime.utcnow().isoformat()),
    )
    conn.close()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    return row


def require_write_user(user=Depends(require_user)):
    if user["role"] not in WRITE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This role cannot modify data")
    return user


def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    email: str = Field(..., min_length=5, max_length=120)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = "relationship_manager"


class LoginRequest(BaseModel):
    email: str
    password: str


class CustomerRequest(BaseModel):
    customer_id: str = Field(..., min_length=3, max_length=40)
    name: str = Field(..., min_length=2, max_length=120)
    age: int = Field(..., ge=18, le=100)
    city: str = Field(..., min_length=2, max_length=80)
    state: str = Field(..., min_length=2, max_length=40)
    employment_type: str = Field(..., pattern="^(Salaried|Self-Employed|Gig Worker|Freelancer)$")
    declared_income: Optional[float] = Field(None, ge=0)
    true_monthly_income: Optional[float] = Field(None, ge=0)
    true_loan_type: str = Field("None", pattern="^(None|Personal Loan|Auto Loan|Home Loan|Mortgage)$")
    persona: str = "manual"


class TransactionRequest(BaseModel):
    customer_id: str
    timestamp: Optional[str] = None
    type: str = Field(..., pattern="^(UPI_CREDIT|SALARY_CREDIT|UPI_DEBIT|IMPS|NEFT|EMI_DEBIT|BILL_PAY|WALLET_TOPUP)$")
    amount: float = Field(..., gt=0)
    counterparty: str = Field(..., min_length=2, max_length=120)


@app.post("/api/auth/register")
def register(payload: RegisterRequest):
    role = payload.role if payload.role in ROLES else "relationship_manager"
    email = payload.email.strip().lower()
    conn = get_conn()
    user_count = db.scalar(conn, "SELECT COUNT(*) FROM users")
    if user_count == 0:
        role = "admin"
    salt, password_hash = hash_password(payload.password)
    try:
        insert_sql = """INSERT INTO users (name, email, role, password_salt, password_hash, created_at)
               VALUES (?,?,?,?,?,?)"""
        if db.IS_POSTGRES:
            insert_sql += " RETURNING user_id AS id"
        cursor = db.execute(
            conn,
            insert_sql,
            (payload.name.strip(), email, role, salt, password_hash, datetime.utcnow().isoformat()),
        )
        user_id = db.last_insert_id(cursor)
        token, expires_at = create_session(conn, user_id)
        conn.commit()
        user = db.one(conn, "SELECT * FROM users WHERE user_id=?", (user_id,))
    except Exception as exc:
        conn.close()
        if db.is_integrity_error(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered")
        raise
    conn.close()
    return {"token": token, "expires_at": expires_at.isoformat(), "user": public_user(user)}


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    conn = get_conn()
    user = db.one(conn, "SELECT * FROM users WHERE email=?", (payload.email.strip().lower(),))
    if not user or not verify_password(payload.password, user["password_salt"], user["password_hash"]):
        conn.close()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token, expires_at = create_session(conn, user["user_id"])
    db.execute(conn, "UPDATE users SET last_login_at=? WHERE user_id=?", (datetime.utcnow().isoformat(), user["user_id"]))
    conn.commit()
    conn.close()
    return {"token": token, "expires_at": expires_at.isoformat(), "user": public_user(user)}


@app.get("/api/auth/me")
def me(user=Depends(require_user)):
    return public_user(user)


@app.post("/api/auth/logout")
def logout(authorization: str = Header(None), user=Depends(require_user)):
    token = authorization.split(" ", 1)[1].strip()
    conn = get_conn()
    db.execute(conn, "DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/roles")
def roles():
    return {"roles": sorted(ROLES), "write_roles": sorted(WRITE_ROLES)}




@app.post("/api/generate")
def generate(n_customers: int = Query(150, ge=20, le=1000), seed: int = Query(None), user=Depends(require_write_user)):
    seed = seed if seed is not None else datetime.now().microsecond
    n_cust, n_txn = data_gen.build_current_database(n_customers=n_customers, seed=seed)
    init_database()
    summary = engine.run_engine(DB_PATH)
    return {"customers_generated": n_cust, "transactions_generated": n_txn, **summary}


@app.get("/api/stats")
def stats(user=Depends(require_user)):
    if not db_exists():
        raise HTTPException(404, "No dataset yet — call POST /api/generate")
    conn = get_conn()
    total_customers = db.scalar(conn, "SELECT COUNT(*) FROM customers")
    total_transactions = db.scalar(conn, "SELECT COUNT(*) FROM transactions")
    total_leads = db.scalar(conn, "SELECT COUNT(*) FROM leads")
    tiers = {r["tier"]: r["count"] for r in db.rows(conn, "SELECT tier, COUNT(*) AS count FROM leads GROUP BY tier")}
    avg_hours = db.scalar(conn, "SELECT AVG(hours_to_lead) FROM leads")
    match_acc = db.scalar(conn, "SELECT AVG(match_correct) FROM leads")
    avg_income_dev = db.scalar(conn, "SELECT AVG(income_accuracy_pct) FROM leads WHERE income_accuracy_pct IS NOT NULL")
    avg_intent = db.scalar(conn, "SELECT AVG(intent_score) FROM leads")
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



@app.get("/api/customers")
def list_customers(search: str = None, limit: int = 100, user=Depends(require_user)):
    conn = get_conn()
    cur = conn.cursor()
    q = "SELECT * FROM customers WHERE 1=1"
    params = []
    if search:
        q += " AND (name LIKE ? OR customer_id LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    q += " ORDER BY customer_id DESC LIMIT ?"
    params.append(limit)
    rows = db.rows(conn, q, params)
    conn.close()
    return rows


@app.post("/api/customers")
def create_customer(payload: CustomerRequest, user=Depends(require_write_user)):
    monthly_income = payload.true_monthly_income
    if monthly_income is None:
        monthly_income = payload.declared_income or 1
    conn = get_conn()
    try:
        db.execute(
            conn,
            """INSERT INTO customers (customer_id, name, age, city, state, employment_type,
               declared_income, true_monthly_income, true_loan_type, persona)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.customer_id.strip(),
                payload.name.strip(),
                payload.age,
                payload.city.strip(),
                payload.state.strip(),
                payload.employment_type,
                payload.declared_income,
                monthly_income,
                payload.true_loan_type,
                payload.persona.strip() or "manual",
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        if db.is_integrity_error(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, "Customer ID already exists")
        raise
    conn.close()
    try:
        engine.run_engine(DB_PATH)
    except Exception as e:
        print(f"Error running engine after customer creation: {e}")
    return {"ok": True, "customer_id": payload.customer_id.strip()}


@app.post("/api/transactions")
def create_transaction(payload: TransactionRequest, user=Depends(require_write_user)):
    timestamp = payload.timestamp or datetime.now().isoformat()
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "timestamp must be ISO-8601")
    conn = get_conn()
    exists = db.one(conn, "SELECT 1 FROM customers WHERE customer_id=?", (payload.customer_id,))
    if not exists:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    insert_sql = """INSERT INTO transactions (customer_id, timestamp, type, amount, counterparty)
           VALUES (?,?,?,?,?)"""
    if db.IS_POSTGRES:
        insert_sql += " RETURNING txn_id AS id"
    cursor = db.execute(
        conn,
        insert_sql,
        (payload.customer_id, timestamp, payload.type, payload.amount, payload.counterparty.strip()),
    )
    txn_id = db.last_insert_id(cursor)
    conn.commit()
    conn.close()
    try:
        engine.run_engine(DB_PATH)
    except Exception as e:
        print(f"Error running engine after transaction creation: {e}")
    return {"ok": True, "txn_id": txn_id}



@app.get("/api/leads")
def list_leads(tier: str = None, search: str = None, sort: str = "trust_score",
               limit: int = 100, user=Depends(require_user)):
    conn = get_conn()
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
    rows = db.rows(conn, q, params)
    conn.close()
    for r in rows:
        r["triggers_fired"] = r["triggers_fired"].split(",") if r["triggers_fired"] else []
        r["trigger_labels"] = [engine.TRIGGER_LABELS.get(t, t) for t in r["triggers_fired"]]
    return rows


@app.get("/api/leads/{customer_id}")
def lead_detail(customer_id: str, user=Depends(require_user)):
    conn = get_conn()
    cust_row = db.one(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if not cust_row:
        conn.close()
        raise HTTPException(404, "Customer not found")
    cust = cust_row

    lead_row = db.one(conn, "SELECT * FROM leads WHERE customer_id=?", (customer_id,))
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp DESC", (customer_id,))
    conn.close()

    result = {"customer": cust, "transactions": txns, "is_lead": bool(lead_row)}

    if lead_row:
        lead = lead_row
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
def customer_transactions(customer_id: str, user=Depends(require_user)):
    conn = get_conn()
    rows = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp DESC", (customer_id,))
    conn.close()
    if not rows:
        raise HTTPException(404, "No transactions found")
    return rows


@app.get("/api/health")
def health():
    conn = get_conn()
    users = db.scalar(conn, "SELECT COUNT(*) FROM users")
    customers = db.scalar(conn, "SELECT COUNT(*) FROM customers")
    conn.close()
    return {"status": "ok", "data_ready": customers > 0, "users_registered": users}
