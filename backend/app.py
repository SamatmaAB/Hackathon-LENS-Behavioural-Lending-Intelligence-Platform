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
import threading
from datetime import datetime, timedelta, UTC
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

def format_utc_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"

try:
    from backend import data_gen, db, engine, governance
except ImportError:
    import data_gen, db, engine, governance  # type: ignore[no-redef]


BASE_DIR = os.path.dirname(__file__)
DEFAULT_DB_PATH = os.path.join(tempfile.gettempdir(), "lens.db") if os.getenv("VERCEL") else os.path.join(BASE_DIR, "lens.db")
DB_PATH = os.getenv("LENS_DB_PATH", DEFAULT_DB_PATH)
SESSION_HOURS = 12
ROLES = {"admin", "relationship_manager", "analyst"}
WRITE_ROLES = {"admin", "relationship_manager"}
is_generating = False
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


def init_database():
    conn = get_conn()
    if not db.IS_POSTGRES:
        conn.execute("PRAGMA foreign_keys = ON")
    data_gen.create_schema(conn)
    db.executescript(conn, AUTH_SCHEMA_POSTGRES if db.IS_POSTGRES else AUTH_SCHEMA)
    try:
        exists = db.scalar(conn, "SELECT COUNT(*) FROM settings WHERE key = 'lead_threshold'")
        if exists == 0:
            db.execute(conn, "INSERT INTO settings (key, value) VALUES ('lead_threshold', '45')")
    except Exception as e:
        print(f"Failed to initialize default lead threshold: {e}")
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_already_exists = db_exists()
    init_database()
    if not db_already_exists:
        conn = get_conn()
        customer_count = db.scalar(conn, "SELECT COUNT(*) FROM customers")
        conn.close()
        if customer_count == 0:
            data_gen.build_current_database(n_customers=150, seed=42, db_path=DB_PATH)
    yield

app.router.lifespan_context = lifespan


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
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=SESSION_HOURS)
    db.execute(
        conn,
        "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (token, user_id, format_utc_datetime(expires_at), format_utc_datetime(now)),
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




@app.post("/api/generate")
def generate(n_customers: int = Query(150, ge=20, le=1000), seed: int = Query(None), noise_level: float = Query(0.20, ge=0.0, le=1.0), user=Depends(require_write_user)):
    global is_generating
    with generating_lock:
        if is_generating:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Database generation is already in progress. Please wait."
            )
        is_generating = True
    try:
        seed = seed if seed is not None else datetime.now().microsecond
        n_cust, n_txn = data_gen.build_current_database(n_customers=n_customers, seed=seed, db_path=DB_PATH, noise_level=noise_level)
        init_database()
        summary = engine.run_engine(DB_PATH)
        return {"customers_generated": n_cust, "transactions_generated": n_txn, **summary}
    finally:
        with generating_lock:
            is_generating = False


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
        lead["triggers_fired"] = [
            {
                "code": t,
                "label": engine.TRIGGER_LABELS.get(t, t),
                "weight": engine.TRIGGER_WEIGHTS.get(t, 0),
                "contribution": contribs.get(t, 0.0)
            }
            for t in triggers
        ]
        result["lead"] = lead

        # recompute the live income breakdown so the UI can show the method/clusters
        result["income_breakdown"] = engine.reconstruct_income(cust, txns)

        # recompute dynamic capacity details
        scored = engine.score_customer(cust, txns=txns)
        if scored:
            # Wires capacity: CapacityResult to lead detail response
            result["capacity"] = scored.get("capacity")
            lead["capacity"] = scored.get("capacity")
            lead["tier_action_label"] = scored.get("tier_action_label")

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
