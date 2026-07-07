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
import logging
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, UTC
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Header, Depends, status, UploadFile, File, Body, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

def format_utc_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"

try:
    from backend import data_gen, db, engine, governance, geo, ingest, feedback
except ImportError:
    import data_gen, db, engine, governance, geo, ingest, feedback  # type: ignore[no-redef]


from backend import data_gen, db, engine

# Lazy AI imports — won't crash if packages not installed
try:
    from backend.ai_narrative import generate_lead_narrative as _gen_narrative
except ImportError:
    _gen_narrative = None

try:
    from backend.ai_query import run_governance_query as _run_gov_query
except ImportError:
    _run_gov_query = None

try:
    from backend.capacity import compute_capacity
except ImportError:
    from capacity import compute_capacity  # type: ignore[no-redef]

BASE_DIR = os.path.dirname(__file__)
DEFAULT_DB_PATH = os.path.join(tempfile.gettempdir(), "lens.db") if os.getenv("VERCEL") else os.path.join(BASE_DIR, "lens.db")
DB_PATH = os.getenv("LENS_DB_PATH", DEFAULT_DB_PATH)
SESSION_HOURS = 12
ROLES = {"admin", "relationship_manager", "analyst"}
WRITE_ROLES = {"admin", "relationship_manager"}
is_generating = False
generation_started_at = None
generating_lock = threading.Lock()

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


def _migrate_database(conn):
    """Add columns/tables introduced in AI-enhancement milestone to existing DBs."""
    new_columns = [
        ("leads", "fraud_risk_score", "REAL DEFAULT 0"),
        ("leads", "is_anomalous", "INTEGER DEFAULT 0"),
        ("leads", "predicted_loan_type_source", "TEXT DEFAULT 'rule_based'"),
    ]
    for table, col, col_def in new_columns:
        try:
            db.execute(conn, f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass  # Column already exists – safe to ignore

    # Create lead_narratives cache table if not already there
    lead_narratives_sql = """CREATE TABLE IF NOT EXISTS lead_narratives (
        customer_id TEXT PRIMARY KEY,
        narrative TEXT,
        outreach_draft TEXT,
        objections TEXT,
        generated_at TEXT
    )"""
    try:
        db.execute(conn, lead_narratives_sql)
    except Exception:
        pass

    CREATE_OUTCOMES_TABLE = """
    CREATE TABLE IF NOT EXISTS lead_outcomes (
        outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id TEXT NOT NULL,
        recorded_by INTEGER NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('converted', 'contacted_no_response', 'declined', 'not_reachable')),
        triggers_fired_at_time TEXT NOT NULL,
        trust_score_at_time REAL NOT NULL,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
        FOREIGN KEY (recorded_by) REFERENCES users(user_id)
    );
    """
    try:
        db.execute(conn, CREATE_OUTCOMES_TABLE)
    except Exception as e:
        # If running on Postgres, AUTOINCREMENT needs to be SERIAL instead. 
        # But this is a generic try/catch block just in case. Let's fix AUTOINCREMENT vs SERIAL properly if needed.
        if db.IS_POSTGRES:
             db.execute(conn, CREATE_OUTCOMES_TABLE.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
        else:
             pass

    CREATE_RM_CAPACITY_TABLE = """
    CREATE TABLE IF NOT EXISTS rm_capacity (
        user_id INTEGER PRIMARY KEY,
        max_daily_leads INTEGER NOT NULL DEFAULT 15,
        active_assigned_count INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );
    """
    CREATE_LEAD_ASSIGNMENTS_TABLE = """
    CREATE TABLE IF NOT EXISTS lead_assignments (
        customer_id TEXT PRIMARY KEY,
        assigned_rm_id INTEGER NOT NULL,
        assigned_at TEXT NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
        FOREIGN KEY (assigned_rm_id) REFERENCES users(user_id)
    );
    """
    try:
        db.execute(conn, CREATE_RM_CAPACITY_TABLE)
        db.execute(conn, CREATE_LEAD_ASSIGNMENTS_TABLE)
    except Exception:
        pass


def init_database():
    conn = get_conn()
    if not db.IS_POSTGRES:
        conn.execute("PRAGMA foreign_keys = ON")
    data_gen.create_schema(conn)
    _migrate_database(conn)
    db.executescript(conn, AUTH_SCHEMA_POSTGRES if db.IS_POSTGRES else AUTH_SCHEMA)
    try:
        exists = db.scalar(conn, "SELECT COUNT(*) FROM settings WHERE key = 'lead_threshold'")
        if exists == 0:
            db.execute(conn, "INSERT INTO settings (key, value) VALUES ('lead_threshold', '45')")
    except Exception as e:
        print(f"Failed to initialize default lead threshold: {e}")
    user_count = db.scalar(conn, "SELECT COUNT(*) FROM users")
    if user_count == 0:
        data_gen.clear_customer_data(conn)
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("VERCEL") and not os.path.exists(DB_PATH):
        src_db = os.path.join(BASE_DIR, "lens.db")
        if os.path.exists(src_db):
            try:
                import shutil
                shutil.copy2(src_db, DB_PATH)
                print(f"Successfully copied bundled database to {DB_PATH}")
            except Exception as e:
                print(f"Failed to copy bundled database: {e}")
                
    db_already_exists = db_exists()
    init_database()
    seed_default_users()
    if not db_already_exists:
        conn = get_conn()
        customer_count = db.scalar(conn, "SELECT COUNT(*) FROM customers")
        conn.close()
        if customer_count == 0:
            data_gen.build_current_database(n_customers=150, seed=42, db_path=DB_PATH)
            engine.run_engine(DB_PATH)
    yield

app.router.lifespan_context = lifespan
def hash_password(password: str, salt: Optional[str] = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return salt, digest.hex()


def verify_password(password: str, salt: str, stored_hash: str):
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def seed_default_users():
    conn = get_conn()
    emails = ["admin@idbibank.com", "rm@idbibank.com", "analyst@idbibank.com"]
    roles = {
        "admin@idbibank.com": ("LENS Admin", "admin"),
        "rm@idbibank.com": ("Relationship Manager", "relationship_manager"),
        "analyst@idbibank.com": ("LENS Analyst", "analyst")
    }
    default_pass = "idbi@12345"
    for email in emails:
        exists = db.scalar(conn, "SELECT COUNT(*) FROM users WHERE email = ?", (email,))
        if exists == 0:
            name, role = roles[email]
            salt, password_hash = hash_password(default_pass)
            db.execute(
                conn,
                """INSERT INTO users (name, email, role, password_salt, password_hash, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (name, email, role, salt, password_hash, datetime.utcnow().isoformat())
            )
    conn.commit()
    conn.close()


def public_user(row):
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


SESSION_SECRET = os.environ.get("SESSION_SECRET") or "lens-secret-session-key-239847293847"

def create_session(conn, user_id: int):
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=SESSION_HOURS)
    expires_ts = int(expires_at.timestamp())
    payload = f"{user_id}.{expires_ts}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = f"{payload}.{signature}"
    try:
        db.execute(
            conn,
            "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
            (token, user_id, format_utc_datetime(expires_at), format_utc_datetime(now)),
        )
    except Exception:
        pass
    return token, expires_at


def require_user(authorization: str = Header(None), token_q: str = Query(None, alias="token")):
    if not authorization and not token_q:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if token_q:
        token = token_q
    else:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
    
    # Try signed stateless validation first
    try:
        parts = token.split(".")
        if len(parts) == 3:
            user_id_str, expires_ts_str, signature = parts
            payload = f"{user_id_str}.{expires_ts_str}"
            expected_sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected_sig):
                expires_ts = int(expires_ts_str)
                if expires_ts > datetime.now(UTC).timestamp():
                    conn = get_conn()
                    row = db.one(conn, "SELECT * FROM users WHERE user_id = ?", (int(user_id_str),))
                    conn.close()
                    if row:
                        return row
    except Exception as e:
        print(f"Stateless session verification failed: {e}")

    # Fallback to database check (backwards compatibility / local fallback)
    conn = get_conn()
    row = db.one(
        conn,
        """SELECT u.* FROM sessions s
           JOIN users u ON u.user_id = s.user_id
           WHERE s.token = ? AND s.expires_at > ?""",
        (token, format_utc_datetime(datetime.now(UTC))),
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


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    email: str = Field(..., min_length=5, max_length=120)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = "relationship_manager"


class CreateUserRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    email: str = Field(..., min_length=5, max_length=120)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(..., pattern="^(admin|relationship_manager|analyst)$")


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


# Governance API Response Schemas
class SegmentStat(BaseModel):
    segment_name: str
    total_customers: int
    total_leads: int
    conversion_rate_pct: float
    is_underperforming: bool
    gap_to_best_pp: float


class FlaggedSegment(BaseModel):
    segment_name: str
    conversion_rate_pct: float
    gap_to_best_pp: float
    recommendation: str


class FairnessResponse(BaseModel):
    segments: List[SegmentStat]
    best_performing_segment: Optional[str]
    best_conversion_rate_pct: float
    underperforming_segments: List[str]
    flagged_segments: List[FlaggedSegment]
    recommendation_summary: str


class ComplianceStandard(BaseModel):
    status: str
    description: str
    considerations: str
    gaps: List[str]
    recommendations: List[str]


class ComplianceResponse(BaseModel):
    compliance_status: str
    standards: Dict[str, ComplianceStandard]
    overall_summary: str
    gaps: List[str]
    recommendations: List[str]
    governance_notes: str


class FieldMapping(BaseModel):
    internal_field: str
    sandbox_field: str
    transformation: str
    status: str
    validation_rules: str


class MissingMapping(BaseModel):
    internal_field: str
    sandbox_field: str
    reason: str


class SandboxMappingResponse(BaseModel):
    mappings: List[FieldMapping]
    missing_mappings: List[MissingMapping]
    validation_notes: str
    schema_version: str


class SegmentROIPerformance(BaseModel):
    segment_name: str
    total_customers: int
    total_leads: int
    conversion_rate_pct: float
    estimated_outreach_cost: float
    expected_loans_disbursed: float
    expected_revenue: float
    roi_pct: float


class ROIResponse(BaseModel):
    total_customers: int
    total_leads: int
    conversion_rate_pct: float
    estimated_revenue: float
    estimated_cost: float
    net_profit: float
    roi_multiplier: float
    segment_performance: List[SegmentROIPerformance]
    cost_assumptions: Dict[str, float]


class ClassMetrics(BaseModel):
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1_score: float

class MacroAverages(BaseModel):
    precision: float
    recall: float
    f1_score: float

class EvaluationResponse(BaseModel):
    confusion_matrix: Dict[str, Dict[str, int]]
    class_metrics: Dict[str, ClassMetrics]
    macro_averages: MacroAverages


# ---------------------------------------------------------------------------
# Auth Endpoints
# ---------------------------------------------------------------------------

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
            (payload.name.strip(), email, role, salt, password_hash, format_utc_datetime(datetime.now(UTC))),
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
    return {"token": token, "expires_at": format_utc_datetime(expires_at), "user": public_user(user)}


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    conn = get_conn()
    user = db.one(conn, "SELECT * FROM users WHERE email=?", (payload.email.strip().lower(),))
    if not user or not verify_password(payload.password, user["password_salt"], user["password_hash"]):
        conn.close()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token, expires_at = create_session(conn, user["user_id"])
    db.execute(conn, "UPDATE users SET last_login_at=? WHERE user_id=?", (format_utc_datetime(datetime.now(UTC)), user["user_id"]))
    conn.commit()
    conn.close()
    return {"token": token, "expires_at": format_utc_datetime(expires_at), "user": public_user(user)}


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


# ---------------------------------------------------------------------------
# Admin Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/users")
def list_users(admin: dict = Depends(require_admin)):
    """Return a list of all registered users (admin only)."""
    conn = get_conn()
    rows = db.rows(conn, "SELECT * FROM users")
    conn.close()
    return [public_user(row) for row in rows]


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    """Delete a user by ID (admin only)."""
    conn = get_conn()
    user = db.one(conn, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if not user:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    db.execute(conn, "DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "user_id": user_id}


@app.post("/api/users")
def admin_create_user(payload: CreateUserRequest, admin: dict = Depends(require_admin)):
    email = payload.email.strip().lower()
    conn = get_conn()
    salt, password_hash = hash_password(payload.password)
    try:
        insert_sql = """INSERT INTO users (name, email, role, password_salt, password_hash, created_at)
               VALUES (?,?,?,?,?,?)"""
        if db.IS_POSTGRES:
            insert_sql += " RETURNING user_id AS id"
        cursor = db.execute(
            conn,
            insert_sql,
            (payload.name.strip(), email, payload.role, salt, password_hash, datetime.utcnow().isoformat()),
        )
        user_id = db.last_insert_id(cursor)
        conn.commit()
        user = db.one(conn, "SELECT * FROM users WHERE user_id=?", (user_id,))
    except Exception as exc:
        conn.close()
        if db.is_integrity_error(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered")
        raise
    conn.close()
    return public_user(user)


# ---------------------------------------------------------------------------
# Data Generation
# ---------------------------------------------------------------------------

@app.post("/api/generate")
def generate(n_customers: int = Query(150, ge=20, le=1000), seed: int = Query(None), noise_level: float = Query(0.20, ge=0.0, le=1.0), user=Depends(require_write_user)):
    global is_generating, generation_started_at
    with generating_lock:
        if is_generating:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Database generation is already in progress. Please wait.",
                    "started_at": generation_started_at,
                }
            )
        is_generating = True
        generation_started_at = datetime.utcnow().isoformat()
    try:
        seed = seed if seed is not None else datetime.now().microsecond
        n_cust, n_txn = data_gen.build_current_database(n_customers=n_customers, seed=seed, db_path=DB_PATH, noise_level=noise_level)
        init_database()
        summary = engine.run_engine(DB_PATH)
        return {"customers_generated": n_cust, "transactions_generated": n_txn, **summary}
    finally:
        with generating_lock:
            is_generating = False
            generation_started_at = None


# ---------------------------------------------------------------------------
# Stats & Leads
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Customer Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/customers")
def list_customers(search: str = None, limit: int = 100, user=Depends(require_user)):
    conn = get_conn()
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
    if payload.timestamp:
        try:
            dt = datetime.fromisoformat(payload.timestamp)
            timestamp = format_utc_datetime(dt)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "timestamp must be ISO-8601")
    else:
        timestamp = format_utc_datetime(datetime.now(UTC))
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


# ---------------------------------------------------------------------------
# Lead Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/leads")
def list_leads(tier: str = None, search: str = None, sort: str = "trust_score",
               limit: int = 100, user=Depends(require_user)):
    conn = get_conn()
    q = """SELECT l.*, c.name, c.age, c.city, c.state, c.employment_type, c.declared_income, c.true_loan_type
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
        r["tier_action_label"] = engine.TIER_ACTION_LABELS.get(r["tier"], "Insufficient signal — do not action")
    return rows


@app.get("/api/leads/segmentation")
def leads_segmentation(user=Depends(require_user)):
    """
    Portfolio-level view for the segmentation bubble chart:
    income (x) vs trust_score (y) vs recommended eligible loan amount (bubble size)
    vs tier (color), for every current lead.
    """
    conn = get_conn()
    try:
        lead_rows = db.rows(
            conn,
            """SELECT l.customer_id, l.synthetic_income, l.trust_score, l.tier,
                      l.predicted_loan_type, c.declared_income, c.employment_type
               FROM leads l JOIN customers c ON c.customer_id = l.customer_id"""
        )
        result = []
        for lr in lead_rows:
            txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp",
                           (lr["customer_id"],))
            capacity_res = compute_capacity(
                customer_id=lr["customer_id"],
                transactions=txns,
                reconstructed_income=lr["synthetic_income"],
                declared_income=lr["declared_income"],
                predicted_loan_type=lr["predicted_loan_type"],
                repay_score=lr["trust_score"],
            )
            result.append({
                "customer_id": lr["customer_id"],
                "income": lr["synthetic_income"],
                "trust_score": lr["trust_score"],
                "tier": lr["tier"],
                "eligible_amount": capacity_res.recommended_eligible_amount,
                "employment_type": lr["employment_type"],
            })
        return result
    finally:
        conn.close()


@app.get("/api/leads/{customer_id}/loan-comparison")
def loan_comparison(customer_id: str, types: str = Query(..., description="Comma-separated loan types, e.g. 'Home Loan,Auto Loan'"),
                     user=Depends(require_user)):
    """
    Compares 2+ loan products side-by-side for one customer and flags
    whether pursuing them simultaneously breaches prudent FOIR limits.
    Directly answers the problem statement's "Personal Loan, Home loan,
    Mortgage Loan, Auto Loan" enumeration with a cross-product view.
    """
    from backend import capacity as capacity_module

    conn = get_conn()
    cust = db.row(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if not cust:
        conn.close()
        raise HTTPException(404, "Customer not found")

    lead = db.row(conn, "SELECT * FROM leads WHERE customer_id=?", (customer_id,))
    if not lead:
        conn.close()
        raise HTTPException(404, "No lead record — customer has not been scored yet")

    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=?", (customer_id,))
    cap_result = capacity_module.compute_capacity(
        customer_id=customer_id,
        transactions=txns,
        reconstructed_income=lead["synthetic_income"],
        declared_income=cust.get("declared_income"),
        predicted_loan_type=lead["predicted_loan_type"],
        repay_score=lead.get("trust_score", 50.0),
    )
    conn.close()

    requested = [t.strip() for t in types.split(",") if t.strip()]
    try:
        result = capacity_module.check_loan_stacking(cap_result, requested)
    except ValueError as e:
        raise HTTPException(422, str(e))

    return result


@app.get("/api/leads/{customer_id}")
def lead_detail(customer_id: str, user=Depends(require_user)):
    conn = get_conn()
    cust_row = db.one(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if not cust_row:
        conn.close()
        raise HTTPException(404, "Customer not found")
    cust = cust_row

    # Record access log
    try:
        db.execute(
            conn,
            "INSERT INTO access_logs (user_id, customer_id, action, accessed_at) VALUES (?, ?, 'VIEW_LEAD_DETAIL', ?)",
            (user["user_id"], customer_id, format_utc_datetime(datetime.now(UTC)))
        )
        conn.commit()
    except Exception as e:
        print(f"Failed to record access log: {e}")

    lead_row = db.one(conn, "SELECT * FROM leads WHERE customer_id=?", (customer_id,))
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp DESC", (customer_id,))
    conn.close()

    result = {"customer": cust, "transactions": txns, "is_lead": bool(lead_row)}

    if lead_row:
        lead = lead_row
        triggers = lead["triggers_fired"].split(",") if lead["triggers_fired"] else []
        contribs = engine.get_trigger_contributions(triggers)

        # recompute dynamic capacity details and get trigger transaction evidence
        scored = engine.score_customer(cust, txns=txns)
        fired_details = scored.get("fired_details", {}) if scored else {}

        lead["triggers_fired"] = []
        for t in triggers:
            txn = fired_details.get(t)
            conf, method = 1.0, "keyword_fallback"
            if txn:
                cls_res = engine._classify_txn_category(txn["counterparty"])
                conf = cls_res.get("confidence", 1.0)
                method = cls_res.get("method", "keyword_fallback")
            lead["triggers_fired"].append({
                "code": t,
                "label": engine.TRIGGER_LABELS.get(t, t),
                "weight": engine.TRIGGER_WEIGHTS.get(t, 0),
                "contribution": contribs.get(t, 0.0),
                "classification_confidence": conf,
                "classification_method": method
            })
        result["lead"] = lead

        # recompute the live income breakdown so the UI can show the method/clusters
        result["income_breakdown"] = engine.reconstruct_income(cust, txns)
        result["cashflow_breakdown"] = engine.build_cashflow_breakdown(txns)

        if scored:
            # Wires capacity: CapacityResult to lead detail response
            result["capacity"] = scored.get("capacity")
            lead["capacity"] = scored.get("capacity")
            lead["tier_action_label"] = scored.get("tier_action_label")
            # Expose the TRUST sub-scores so the frontend can render a waterfall
            # breakdown of exactly how the Trust Score was assembled.
            lead["income_confidence"] = scored.get("income_confidence")
            lead["repay_score"] = scored.get("repay_score")

            nba_conn = get_conn()
            result["next_best_action"] = engine.suggest_next_best_action(cust, txns, scored, nba_conn)
            nba_conn.close()

    return result


@app.get("/api/customers/{customer_id}/transactions")
def customer_transactions(customer_id: str, user=Depends(require_user)):
    conn = get_conn()
    rows = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp DESC", (customer_id,))
    conn.close()
    if not rows:
        raise HTTPException(404, "No transactions found")
    return rows


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    import os
    from backend import ml_predict, db
    
    ml_ready = ml_predict._load_artifacts()
    try:
        import sklearn  # noqa
        sentry_ready = True
    except ImportError:
        sentry_ready = False
        
    conn = get_conn()
    users = db.scalar(conn, "SELECT COUNT(*) FROM users")
    customers = db.scalar(conn, "SELECT COUNT(*) FROM customers")
    conn.close()
    
    with generating_lock:
        generation_status = {
            "is_generating": is_generating,
            "generation_started_at": generation_started_at,
        }
        
    return {
        "status": "ok", 
        "data_ready": customers > 0, 
        "users_registered": users, 
        **generation_status,
        "subsystems": {
            "rule_engine": True,
            "ml_loan_type_model": ml_ready,
            "anomaly_detection_sentry": sentry_ready,
            "narrative_llm": bool(os.environ.get("NVIDIA_API_KEY") or os.environ.get("GROQ_API_KEY")),
        }
    }


# ---------------------------------------------------------------------------
# Governance Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/governance/fairness", response_model=FairnessResponse)
def get_fairness_report(user=Depends(require_user)):
    """
    Returns a dynamic fairness audit evaluating conversion rates and gaps
    across customer segments (employment types).
    """
    try:
        return governance.generate_fairness_report(DB_PATH)
    except Exception as e:
        logger = logging.getLogger("lens.app")
        logger.error(f"Error in /api/governance/fairness: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/governance/compliance", response_model=ComplianceResponse)
def get_compliance_report(user=Depends(require_user)):
    """
    Returns an audit report assessing data minimization, explainability, audit trails,
    and compliance with RBI lending guidelines and India DPDP Act 2023.
    """
    try:
        return governance.generate_compliance_report(DB_PATH)
    except Exception as e:
        logger = logging.getLogger("lens.app")
        logger.error(f"Error in /api/governance/compliance: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/governance/sandbox-mapping", response_model=SandboxMappingResponse)
def get_sandbox_mapping(user=Depends(require_user)):
    """
    Returns schema field mapping details between LENS internal structures and standard API sandboxes.
    """
    try:
        return governance.generate_sandbox_mapping()
    except Exception as e:
        logger = logging.getLogger("lens.app")
        logger.error(f"Error in /api/governance/sandbox-mapping: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/governance/roi", response_model=ROIResponse)
def get_roi_report(user=Depends(require_user)):
    """
    Returns dynamic cost-benefit estimates and ROI multipliers derived from current lead counts.
    """
    try:
        return governance.generate_roi_report(DB_PATH)
    except Exception as e:
        logger = logging.getLogger("lens.app")
        logger.error(f"Error in /api/governance/roi: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/governance/evaluation", response_model=EvaluationResponse)
def get_evaluation_report(user=Depends(require_user)):
    """
    Returns the multi-class confusion matrix, precision, recall, and f1 metrics.
    """
    try:
        return governance.generate_evaluation_report(DB_PATH)
    except Exception as e:
        logger = logging.getLogger("lens.app")
        logger.error(f"Error in /api/governance/evaluation: {e}", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/governance/geo-distribution")
def get_geo_distribution(user=Depends(require_user)):
    """
    Returns city-level lead concentration for the illustrative governance map.
    """
    conn = get_conn()
    try:
        return geo.build_geo_distribution(conn, db)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature 1: AI Narrative endpoint
# ---------------------------------------------------------------------------

@app.post("/api/leads/{customer_id}/narrative")
def get_lead_narrative(customer_id: str, user=Depends(require_user)):
    """
    Generate an AI briefing for a lead: narrative, outreach draft, and likely objections.
    Checks the lead_narratives cache first; only calls Claude if missing or stale.
    """
    if _gen_narrative is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="AI narrative not available")

    conn = get_conn()
    cust_row = db.one(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if not cust_row:
        conn.close()
        raise HTTPException(404, "Customer not found")

    lead_row = db.one(conn, "SELECT * FROM leads WHERE customer_id=?", (customer_id,))
    if not lead_row:
        conn.close()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Customer is not a qualified lead")

    # Check cache: use cached narrative unless lead was updated after narrative was generated
    cached = db.one(conn, "SELECT * FROM lead_narratives WHERE customer_id=?", (customer_id,))
    lead_updated_at = lead_row.get("lead_card_generated_at", "")
    if cached:
        cached_at = cached.get("generated_at", "")
        if cached_at and lead_updated_at and cached_at >= lead_updated_at:
            conn.close()
            import json as _json
            return {
                "narrative":      cached["narrative"],
                "outreach_draft": cached["outreach_draft"],
                "objections":     _json.loads(cached["objections"] or "[]"),
                "cached":         True,
            }

    conn.close()

    # Assemble the lead payload exactly as the GET /api/leads/{id} endpoint does
    detail = lead_detail(customer_id, user)
    try:
        result = _gen_narrative(detail)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Narrative generation failed: {str(e)}")

    # Cache the result
    import json as _json
    now_str = format_utc_datetime(datetime.now(UTC))
    conn = get_conn()
    try:
        db.execute(
            conn,
            """INSERT OR REPLACE INTO lead_narratives
               (customer_id, narrative, outreach_draft, objections, generated_at)
               VALUES (?,?,?,?,?)""",
            (customer_id,
             result.get("narrative", ""),
             result.get("outreach_draft", ""),
             _json.dumps(result.get("objections", [])),
             now_str),
        )
        db.execute(
            conn,
            "INSERT INTO access_logs (user_id, customer_id, action, accessed_at) VALUES (?,?,?,?)",
            (user["user_id"], customer_id, "VIEW_AI_NARRATIVE", now_str),
        )
        conn.commit()
    except Exception as e:
        print(f"[narrative] Cache write failed: {e}")
    finally:
        conn.close()

    result["cached"] = False
    return result


# ---------------------------------------------------------------------------
# Feature 4: SENTRY anomaly governance endpoint
# ---------------------------------------------------------------------------

@app.get("/api/governance/anomalies")
def get_anomaly_report(user=Depends(require_user)):
    """
    Returns leads flagged as anomalous by the SENTRY IsolationForest detector.
    """
    conn = get_conn()
    try:
        rows = db.rows(
            conn,
            """SELECT l.customer_id, c.name, c.employment_type,
                      l.fraud_risk_score, l.is_anomalous, l.tier, l.trust_score
               FROM leads l
               JOIN customers c ON l.customer_id = c.customer_id
               ORDER BY l.fraud_risk_score DESC""",
        )
        all_leads_count = db.scalar(conn, "SELECT COUNT(*) FROM leads")
        flagged = [dict(r) for r in rows if r["is_anomalous"]]
    finally:
        conn.close()
    return {
        "total_leads": all_leads_count,
        "flagged_count": len(flagged),
        "flagged_leads": flagged,
        "all_leads": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Feature 3: Admin retrain endpoint
# ---------------------------------------------------------------------------

@app.post("/api/admin/retrain")
def retrain_model(user=Depends(require_admin)):
    """
    Trigger offline retraining of the XGBoost loan-type model (admin only).
    """
    try:
        from backend.train_models import train_loan_type_model
        accuracy = train_loan_type_model(DB_PATH)
        # Reset lazy-load flag so ml_predict picks up new artifacts
        try:
            import backend.ml_predict as _ml
            _ml._load_attempted = False
            _ml._model = None
        except Exception:
            pass
        return {"ok": True, "accuracy": accuracy, "message": "Model retrained successfully"}
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Training failed: {str(e)}")


# ---------------------------------------------------------------------------
# Feature 6: Natural-language governance query
# ---------------------------------------------------------------------------

class GovernanceAskPayload(BaseModel):
    question: str


@app.post("/api/governance/ask")
def ask_governance(payload: GovernanceAskPayload, user=Depends(require_user)):
    """
    Answer a plain-English governance question using Claude with tool-calling
    over existing /api/leads and /api/governance/* handlers.
    Admin and analyst roles only.
    """
    if user["role"] not in ("admin", "analyst"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin or analyst role required")

    if _run_gov_query is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="AI query not available")

    question = payload.question.strip()
    if not question:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "question required")

    def tool_executor(name: str, tool_input: dict):
        """Bridge Claude tool calls to existing internal functions."""
        try:
            if name == "get_leads":
                tier   = tool_input.get("tier")
                search = tool_input.get("search")
                result = list_leads(tier=tier, search=search, user=user)
                return result[:50] if isinstance(result, list) else result

            if name == "get_fairness_report":
                return governance.generate_fairness_report(DB_PATH)

            if name == "get_roi_report":
                return governance.generate_roi_report(DB_PATH)

            if name == "get_anomaly_report":
                return get_anomaly_report(user)

        except Exception as e:
            return {"error": str(e)}
        return {"error": f"unknown tool: {name}"}

    try:
        answer = _run_gov_query(question, tool_executor)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Query failed: {str(e)}")

    return {"answer": answer}


class ConsentPayload(BaseModel):
    consent_type: str = "lending_outreach"


class ThresholdChangePayload(BaseModel):
    proposed_threshold: float = Field(..., ge=0.0, le=100.0)


@app.post("/api/customers/{customer_id}/consent")
def grant_consent(customer_id: str, payload: ConsentPayload, user=Depends(require_user)):
    conn = get_conn()
    cust = db.one(conn, "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,))
    if not cust:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    
    now_str = format_utc_datetime(datetime.now(UTC))
    try:
        db.execute(
            conn,
            "INSERT INTO consent_logs (customer_id, user_id, consent_type, granted_at, revoked_at) VALUES (?, ?, ?, ?, NULL)",
            (customer_id, user["user_id"], payload.consent_type, now_str)
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    conn.close()
    return {"ok": True, "status": "Consent recorded"}


@app.delete("/api/customers/{customer_id}/consent")
def revoke_consent_and_erase(customer_id: str, user=Depends(require_write_user)):
    conn = get_conn()
    cust = db.one(conn, "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,))
    if not cust:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    
    now_str = format_utc_datetime(datetime.now(UTC))
    try:
        db.execute(
            conn,
            "UPDATE consent_logs SET revoked_at = ? WHERE customer_id = ? AND revoked_at IS NULL",
            (now_str, customer_id)
        )
        db.execute(conn, "DELETE FROM customers WHERE customer_id = ?", (customer_id,))
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    conn.close()
    return {"ok": True, "status": "Erasure complete"}


@app.get("/api/governance/access-logs")
def list_access_logs(user=Depends(require_admin)):
    conn = get_conn()
    q = """
        SELECT al.*, u.name AS user_name, u.email AS user_email, c.name AS customer_name
        FROM access_logs al
        LEFT JOIN users u ON al.user_id = u.user_id
        LEFT JOIN customers c ON al.customer_id = c.customer_id
        ORDER BY al.accessed_at DESC
    """
    rows = db.rows(conn, q)
    conn.close()
    return rows


@app.get("/api/governance/audit-trail")
def get_audit_trail(user=Depends(require_admin)):
    conn = get_conn()
    try:
        access_events = db.rows(
            conn,
            """
            SELECT 'ACCESS' AS event_type,
                   al.accessed_at AS occurred_at,
                   u.name AS actor,
                   COALESCE(c.name, al.customer_id) AS subject,
                   al.action AS detail
            FROM access_logs al
            LEFT JOIN users u ON al.user_id = u.user_id
            LEFT JOIN customers c ON al.customer_id = c.customer_id
            ORDER BY al.accessed_at DESC
            LIMIT 80
            """,
        )
        threshold_events = db.rows(
            conn,
            """
            SELECT 'THRESHOLD' AS event_type,
                   tr.updated_at AS occurred_at,
                   u.name AS actor,
                   CAST(tr.proposed_threshold AS TEXT) AS subject,
                   tr.status AS detail
            FROM threshold_requests tr
            LEFT JOIN users u ON tr.proposer_id = u.user_id
            ORDER BY tr.updated_at DESC
            LIMIT 40
            """,
        )
    finally:
        conn.close()

    events = [dict(r) for r in access_events] + [dict(r) for r in threshold_events]
    events.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)
    return events[:100]


@app.post("/api/governance/threshold-change-request")
def propose_threshold_change(payload: ThresholdChangePayload, user=Depends(require_write_user)):
    conn = get_conn()
    insert_sql = """
        INSERT INTO threshold_requests (proposer_id, proposed_threshold, status, approved_by, created_at, updated_at)
        VALUES (?, ?, 'PENDING', NULL, ?, ?)
    """
    if db.IS_POSTGRES:
        insert_sql += " RETURNING request_id AS id"
    now_str = format_utc_datetime(datetime.now(UTC))
    try:
        cursor = db.execute(conn, insert_sql, (user["user_id"], payload.proposed_threshold, now_str, now_str))
        request_id = db.last_insert_id(cursor)
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    conn.close()
    return {"ok": True, "request_id": request_id, "status": "PENDING"}


@app.get("/api/governance/threshold-change-requests")
def list_threshold_change_requests(user=Depends(require_user)):
    conn = get_conn()
    q = """
        SELECT tr.*, u.name AS proposer_name, u.email AS proposer_email,
               a.name AS approver_name, a.email AS approver_email
        FROM threshold_requests tr
        LEFT JOIN users u ON tr.proposer_id = u.user_id
        LEFT JOIN users a ON tr.approved_by = a.user_id
        ORDER BY tr.created_at DESC
    """
    rows = db.rows(conn, q)
    conn.close()
    return rows


@app.post("/api/governance/threshold-change-request/{request_id}/approve")
def approve_threshold_change(request_id: int, user=Depends(require_admin)):
    conn = get_conn()
    req = db.one(conn, "SELECT * FROM threshold_requests WHERE request_id = ?", (request_id,))
    if not req:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Threshold request not found")
        
    if req["status"] != "PENDING":
        conn.close()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Request is already {req['status']}")
        
    if req["proposer_id"] == user["user_id"]:
        conn.close()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Maker-Checker segregation: Proposer cannot approve their own request")
        
    now_str = format_utc_datetime(datetime.now(UTC))
    proposed_threshold = req["proposed_threshold"]
    
    try:
        db.execute(
            conn,
            "UPDATE threshold_requests SET status = 'APPROVED', approved_by = ?, updated_at = ? WHERE request_id = ?",
            (user["user_id"], now_str, request_id)
        )
        exists = db.scalar(conn, "SELECT COUNT(*) FROM settings WHERE key = 'lead_threshold'")
        if exists > 0:
            db.execute(conn, "UPDATE settings SET value = ? WHERE key = 'lead_threshold'", (str(proposed_threshold),))
        else:
            db.execute(conn, "INSERT INTO settings (key, value) VALUES ('lead_threshold', ?)", (str(proposed_threshold),))
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    conn.close()
    
    try:
        engine.run_engine(DB_PATH)
    except Exception as e:
        print(f"Error running engine after threshold approval: {e}")
        
    return {"ok": True, "status": "APPROVED", "new_threshold": proposed_threshold}


@app.post("/api/governance/threshold-change-request/{request_id}/reject")
def reject_threshold_change(request_id: int, user=Depends(require_admin)):
    conn = get_conn()
    req = db.one(conn, "SELECT * FROM threshold_requests WHERE request_id = ?", (request_id,))
    if not req:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Threshold request not found")
        
    if req["status"] != "PENDING":
        conn.close()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Request is already {req['status']}")
        
    now_str = format_utc_datetime(datetime.now(UTC))
    try:
        db.execute(
            conn,
            "UPDATE threshold_requests SET status = 'REJECTED', approved_by = ?, updated_at = ? WHERE request_id = ?",
            (user["user_id"], now_str, request_id)
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    conn.close()
    return {"ok": True, "status": "REJECTED"}


@app.post("/api/customers/{customer_id}/ingest-statement")
async def ingest_statement(
    customer_id: str,
    file: UploadFile = File(...),
    consent_confirmed: bool = Form(...),
    user=Depends(require_write_user),
):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV statements supported in this version")
    if not consent_confirmed:
        raise HTTPException(422, "Explicit customer consent is required before ingesting real transaction data")

    content = await file.read()
    try:
        transactions = ingest.parse_csv_statement(content, customer_id, consent_confirmed=consent_confirmed)
    except ValueError as e:
        raise HTTPException(422, str(e))

    conn = get_conn()
    # Record the consent event for audit — reuses the existing access_logs
    # pattern if present, otherwise a minimal insert:
    try:
        db.execute(
            conn,
            "INSERT INTO access_logs (user_id, customer_id, action, timestamp) VALUES (?,?,?,?)",
            (user["user_id"], customer_id, "real_statement_ingested_with_consent",
             datetime.now(timezone.utc).isoformat()),
        )
    except Exception:
        pass  # access_logs may not exist in all environments — non-fatal

    db.execute(conn, "DELETE FROM transactions WHERE customer_id=?", (customer_id,))
    for txn in transactions:
        db.execute(
            conn,
            "INSERT INTO transactions (customer_id, timestamp, type, amount, counterparty) VALUES (?,?,?,?,?)",
            (txn["customer_id"], txn["timestamp"], txn["type"], txn["amount"], txn["counterparty"]),
        )
    conn.commit()

    cust_rows = db.rows(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    cust = cust_rows[0] if cust_rows else None
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=?", (customer_id,))
    lead = engine.score_customer(cust, txns=txns, conn=conn)
    conn.close()

    return {
        "transactions_ingested": len(transactions),
        "source": "real_statement_upload",
        "consent_recorded": True,
        "lead_result": lead,
    }

@app.post("/api/leads/{customer_id}/outcome")
def record_lead_outcome(customer_id: str, outcome: str = Body(..., embed=True), user=Depends(require_write_user)):
    conn = get_conn()
    try:
        feedback.record_outcome(conn, customer_id, user["user_id"], outcome)
    except ValueError as e:
        conn.close()
        raise HTTPException(404, str(e))
    conn.close()
    return {"status": "recorded"}


@app.get("/api/governance/trigger-precision-report")
def trigger_precision_report(user=Depends(require_user)):
    conn = get_conn()
    report = feedback.generate_trigger_precision_report(conn)
    conn.close()
    return report


@app.post("/api/simulate/what-if")
def simulate_what_if(payload: dict = Body(...), user=Depends(require_user)):
    """
    payload = {
      "customer_id": "CUST10001",
      "hypothetical_transaction": {
        "type": "EMI_DEBIT", "amount": 25000, "counterparty": "Lodha Developers",
        "timestamp": "2026-07-07T10:00:00"
      }
    }
    Returns before/after Intent Score, Trust Score, and tier — without persisting anything.
    """
    conn = get_conn()
    customer_id = payload["customer_id"]
    cust = db.one(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if not cust:
        conn.close()
        raise HTTPException(404, "Customer not found")

    existing_txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=?", (customer_id,))
    before = engine.score_customer(cust, txns=existing_txns, conn=conn)

    hypothetical_txns = existing_txns + [payload["hypothetical_transaction"]]
    after = engine.score_customer(cust, txns=hypothetical_txns, conn=conn)
    conn.close()

    return {
        "customer_id": customer_id,
        "before": {"intent_score": before["intent_score"] if before else 0,
                   "trust_score": before["trust_score"] if before else 0,
                   "tier": before["tier"] if before else "Not a lead"},
        "after": {"intent_score": after["intent_score"], "trust_score": after["trust_score"], "tier": after["tier"]},
        "delta_intent": round((after["intent_score"]) - (before["intent_score"] if before else 0), 1),
        "newly_fired_triggers": sorted(set(after["triggers_fired"]) -
                                       set((before["triggers_fired"] if before else []))),
    }


from fastapi.responses import HTMLResponse


from backend import threshold_sensitivity

@app.get("/api/governance/threshold-sensitivity")
def get_threshold_sensitivity(user=Depends(require_user)):
    conn = get_conn()
    result = threshold_sensitivity.compute_threshold_curve(conn, db)
    conn.close()
    return result


@app.get("/api/leads/{customer_id}/stress-test")
def get_stress_test(customer_id: str, shock_pct: float = Query(0.15, ge=0.0, le=0.9),
                     user=Depends(require_user)):
    from backend import capacity as capacity_module

    conn = get_conn()
    cust = db.row(conn, "SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    lead = db.row(conn, "SELECT * FROM leads WHERE customer_id=?", (customer_id,))
    if not cust or not lead:
        conn.close()
        raise HTTPException(404, "Customer or lead record not found")

    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=?", (customer_id,))
    result = capacity_module.stress_test_income_shock(
        customer_id=customer_id, transactions=txns,
        reconstructed_income=lead["synthetic_income"], declared_income=cust.get("declared_income"),
        predicted_loan_type=lead["predicted_loan_type"], repay_score=lead.get("trust_score", 50.0),
        shock_pct=shock_pct,
    )
    conn.close()
    return result


from backend import assignment as assignment_module

@app.post("/api/leads/auto-assign")
def auto_assign_leads(limit: int = Query(50, ge=1, le=500), user=Depends(require_write_user)):
    conn = get_conn()
    unassigned = db.rows(
        conn,
        """
        SELECT l.customer_id, l.trust_score, l.tier FROM leads l
        LEFT JOIN lead_assignments la ON la.customer_id = l.customer_id
        WHERE la.customer_id IS NULL AND l.tier IN ('Tier 1', 'Tier 2')
        ORDER BY (l.tier = 'Tier 1') DESC, l.trust_score DESC
        LIMIT ?
        """,
        (limit,),
    )
    result = assignment_module.assign_leads_to_rms(conn, db, unassigned)
    conn.close()
    return result
@app.get("/api/leads/{customer_id}/report", response_class=HTMLResponse)
def get_lead_report(customer_id: str, user=Depends(require_user)):
    """Returns a styled HTML string suitable for window.print() as a PDF."""
    try:
        data = lead_detail(customer_id, user)
    except HTTPException as e:
        raise e
    
    lead = data["lead"]
    customer = data["customer"]
    
    cap = lead.get('capacity')
    if hasattr(cap, 'recommended_loan_amount'):
        rec_loan = cap.recommended_loan_amount
    elif isinstance(cap, dict):
        rec_loan = cap.get('recommended_loan_amount', 0)
    else:
        rec_loan = 0
    
    html = f"""
    <html>
    <head>
        <title>LENS Executive Summary - {customer['name']}</title>
        <style>
            body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #333; line-height: 1.6; padding: 40px; }}
            h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
            .header {{ display: flex; justify-content: space-between; margin-bottom: 30px; }}
            .metric {{ background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
            .metric-title {{ font-size: 12px; text-transform: uppercase; color: #7f8c8d; font-weight: bold; }}
            .metric-value {{ font-size: 24px; color: #2c3e50; font-weight: bold; }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
            th {{ background-color: #f8f9fa; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>LENS Executive Summary</h1>
                <p><strong>Customer:</strong> {customer['name']} ({customer['customer_id']})</p>
                <p><strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
            </div>
            <div>
                <h2>{lead['tier']}</h2>
                <p>{lead.get('tier_action_label', '')}</p>
            </div>
        </div>
        
        <div class="grid">
            <div class="metric">
                <div class="metric-title">Predicted Loan Type</div>
                <div class="metric-value">{lead['predicted_loan_type']}</div>
            </div>
            <div class="metric">
                <div class="metric-title">Trust Score</div>
                <div class="metric-value">{lead['trust_score']}/100</div>
            </div>
            <div class="metric">
                <div class="metric-title">Intent Score</div>
                <div class="metric-value">{lead['intent_score']}/100</div>
            </div>
            <div class="metric">
                <div class="metric-title">Est. Capacity</div>
                <div class="metric-value">₹{int(rec_loan):,}</div>
            </div>
        </div>
        
        <h3>Behavioural Triggers Fired</h3>
        <table>
            <tr><th>Trigger</th><th>Weight</th><th>Confidence</th></tr>
            {"".join(f"<tr><td>{t['label']}</td><td>{t['weight']}</td><td>{t['classification_confidence']*100:.0f}%</td></tr>" for t in lead['triggers_fired'])}
        </table>
        
        <script>
            window.onload = function() {{ window.print(); }}
        </script>
    </body>
    </html>
    """
    return html
