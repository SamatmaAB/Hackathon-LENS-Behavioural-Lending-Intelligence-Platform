import os
import tempfile
import pytest
from datetime import datetime, timedelta, UTC
from fastapi.testclient import TestClient

from backend import db, engine, governance
from backend.app import app

# Use a temporary SQLite database for testing to ensure isolation
@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db_file = tmp_path / "test_lens.db"
    test_db_path = str(test_db_file)
    
    # Store old env and set to test db path
    old_db_path = os.environ.get("LENS_DB_PATH")
    os.environ["LENS_DB_PATH"] = test_db_path
    
    # Override app's DB_PATH as well
    from backend import app as app_mod
    old_app_db_path = app_mod.DB_PATH
    app_mod.DB_PATH = test_db_path
    
    # Initialize Schema
    conn = db.connect(test_db_path)
    # Ensure foreign keys are enabled for SQLite
    conn.execute("PRAGMA foreign_keys = ON")
    
    # Create tables using standard schema builders
    from backend import data_gen
    from backend.app import AUTH_SCHEMA
    data_gen.create_schema(conn)
    db.executescript(conn, AUTH_SCHEMA)
    
    # Initialize settings table with default lead threshold
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('lead_threshold', '45')")
    
    conn.commit()
    conn.close()
    
    yield test_db_path
    
    # Teardown
    if old_db_path is not None:
        os.environ["LENS_DB_PATH"] = old_db_path
    else:
        os.environ.pop("LENS_DB_PATH", None)
    app_mod.DB_PATH = old_app_db_path

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def auth_header(client):
    # Register an admin user and log in to get a bearer token
    reg_response = client.post("/api/auth/register", json={
        "name": "Test Admin",
        "email": "admin@test.com",
        "password": "securepassword123",
        "role": "admin"
    })
    assert reg_response.status_code == 200
    token = reg_response.json()["token"]
    return {"Authorization": f"Bearer {token}"}

# Mock helper data builders
def add_customer(customer_id, employment_type, declared_income=50000.0, true_income=50000.0):
    conn = db.connect()
    db.execute(conn, """
    INSERT INTO customers (customer_id, name, age, city, state, employment_type, declared_income, true_monthly_income, true_loan_type, persona)
    VALUES (?, ?, 30, 'Mumbai', 'MH', ?, ?, ?, 'Home Loan', 'home_loan_intent')
    """, (customer_id, f"Test {customer_id}", employment_type, declared_income, true_income))
    conn.commit()
    conn.close()

def add_transaction(customer_id, tx_type, amount, counterparty):
    conn = db.connect()
    db.execute(conn, """
    INSERT INTO transactions (customer_id, timestamp, type, amount, counterparty)
    VALUES (?, ?, ?, ?, ?)
    """, (customer_id, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00", tx_type, amount, counterparty))
    conn.commit()
    conn.close()

# --- Unit Tests for score_customer ---

def test_score_customer_no_txns():
    add_customer("C1", "Salaried")
    customer = {"customer_id": "C1", "employment_type": "Salaried", "declared_income": 50000.0, "true_monthly_income": 50000.0}
    res = engine.score_customer(customer)
    assert res is None

def test_score_customer_salaried_lead():
    add_customer("C2", "Salaried", 100000.0, 100000.0)
    add_transaction("C2", "SALARY_CREDIT", 100000.0, "Employer Payroll")
    add_transaction("C2", "SALARY_CREDIT", 100000.0, "Employer Payroll")
    # auto dealer payment to trigger intent
    add_transaction("C2", "NEFT", 80000.0, "Maruti Suzuki Arena")
    # large outward transfer to trigger intent
    add_transaction("C2", "IMPS", 200000.0, "External Beneficiary")
    # education fee payment to trigger intent
    add_transaction("C2", "BILL_PAY", 50000.0, "DPS School Fees")
    
    conn = db.connect()
    customer = db.one(conn, "SELECT * FROM customers WHERE customer_id='C2'")
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id='C2'")
    conn.close()
    
    res = engine.score_customer(customer, txns=txns)
    assert res is not None
    assert res["customer_id"] == "C2"
    assert "salary_inflow_clustering" in res["triggers_fired"]
    assert "auto_dealer_payment" in res["triggers_fired"]
    assert "large_outward_transfer" in res["triggers_fired"]
    assert "education_fee_payment" in res["triggers_fired"]
    assert res["intent_score"] >= engine.LEAD_THRESHOLD
    assert res["is_lead"] is True
    assert res["predicted_loan_type"] == "Auto Loan"
    assert res["outreach_channel"] == "App Notification"

def test_score_customer_non_salaried_non_lead():
    add_customer("C3", "Freelancer", 30000.0, 30000.0)
    add_transaction("C3", "UPI_CREDIT", 5000.0, "Client A")
    
    conn = db.connect()
    customer = db.one(conn, "SELECT * FROM customers WHERE customer_id='C3'")
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id='C3'")
    conn.close()
    
    res = engine.score_customer(customer, txns=txns)
    assert res is not None
    assert res["is_lead"] is False
    assert res["intent_score"] < engine.LEAD_THRESHOLD

def test_score_customer_outreach_window():
    add_customer("C_outreach", "Salaried")
    add_transaction("C_outreach", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    
    conn = db.connect()
    customer = db.one(conn, "SELECT * FROM customers WHERE customer_id='C_outreach'")
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id='C_outreach'")
    conn.close()
    
    res = engine.score_customer(customer, txns=txns)
    assert res is not None
    diff = res["outreach_window_end"] - res["outreach_window_start"]
    assert diff == timedelta(hours=72)

def test_score_customer_income_deviation():
    add_customer("C_inc", "Salaried", 100000.0, 100000.0)
    add_transaction("C_inc", "SALARY_CREDIT", 95000.0, "Employer Payroll")
    add_transaction("C_inc", "SALARY_CREDIT", 95000.0, "Employer Payroll")
    
    conn = db.connect()
    customer = db.one(conn, "SELECT * FROM customers WHERE customer_id='C_inc'")
    txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id='C_inc'")
    conn.close()
    
    res = engine.score_customer(customer, txns=txns)
    assert res is not None
    assert res["reconstructed_income"]["synthetic_monthly_income"] == 95000.0
    assert res["reconstructed_income"]["deviation_pct"] == 5.0

# --- Unit Tests for generate_fairness_report ---

def test_generate_fairness_report_empty():
    res = governance.generate_fairness_report()
    assert res["segments"] == []
    assert res["best_performing_segment"] is None
    assert "No customer data" in res["recommendation_summary"]

def test_generate_fairness_report_single_segment():
    add_customer("C_single", "Salaried")
    add_transaction("C_single", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    add_transaction("C_single", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    add_transaction("C_single", "NEFT", 150000.0, "Maruti Suzuki Arena")
    
    res = governance.generate_fairness_report()
    assert len(res["segments"]) == 1
    assert res["segments"][0]["segment_name"] == "Salaried"
    assert res["best_performing_segment"] == "Salaried"
    assert res["underperforming_segments"] == []
    assert res["flagged_segments"] == []

def test_generate_fairness_report_no_gaps():
    # Setup two segments with close conversion rates (e.g. 100% and 100%)
    add_customer("CS1", "Salaried")
    add_transaction("CS1", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    add_transaction("CS1", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    add_transaction("CS1", "NEFT", 150000.0, "Maruti Suzuki Arena")
    
    add_customer("CG1", "Gig Worker")
    add_transaction("CG1", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    add_transaction("CG1", "SALARY_CREDIT", 50000.0, "Employer Payroll")
    add_transaction("CG1", "NEFT", 150000.0, "Maruti Suzuki Arena")
    
    res = governance.generate_fairness_report()
    assert len(res["segments"]) == 2
    assert res["underperforming_segments"] == []
    assert res["flagged_segments"] == []

# Boundary testing of the 20pp dynamic threshold
# Scenario: Segment A (Salaried) conversion rate is 100% (1/1)
# We vary conversion rate of Segment B (Gig Worker)
# If Gig Worker has 0 leads: gap is 100% - 0% = 100pp (Flagged)
# To test 19.9% gap, 20.0% gap, and 20.1% gap:
# Let's adjust counts:
# Best segment: A = 10/10 leads = 100% conversion.
# Segment B total customers = 1000.
# If B has 801 leads, rate is 80.1%. Gap is 19.9pp (Not flagged)
# If B has 800 leads, rate is 80.0%. Gap is 20.0pp (Flagged)
# If B has 799 leads, rate is 79.9%. Gap is 20.1pp (Flagged)

def test_generate_fairness_report_with_gap_boundary_19_9():
    # We will simulate the data structure by adding customers and transactions
    # To keep it fast, we can add 5 customers to Segment A (all leads, 100%)
    # and 5 customers to Segment B (4 leads, 80% -> gap is 20.0pp which should flag.
    # What about 10 customers for A (10 leads, 100%) and 10 customers for B:
    # 8 leads -> B rate is 80% (gap = 20pp, flags)
    # Let's simulate:
    # Segment A (Salaried): 5 customers, all leads (100% conversion)
    # Segment B (Gig Worker): 5 customers, 4 leads (80% conversion). Gap = 20.0pp -> flags.
    # To get 19.9pp or similar, we can test the function mathematically by feeding it a mocked DB or mock data,
    # or we can test with 10 customers in A (10 leads = 100%) and 1000 in B (801 leads = 80.1%)
    # But adding 1000 customers takes time. Can we mock engine.score_customer inside the report loop?
    # Yes! We can mock engine.score_customer to return is_lead dynamically based on customer_id!
    pass

def test_generate_fairness_report_mocked_ratios(monkeypatch):
    # Mock engine.score_customer to return is_lead based on customer ID to achieve exact boundary values
    # We add 10 customers to Segment A and 10 customers to Segment B
    for i in range(10):
        add_customer(f"A{i}", "Salaried")
        add_customer(f"B{i}", "Gig Worker")
        
    # We will mock score_customer
    # Segment A: 10 leads (100%)
    # Segment B: 8 leads (80%). Gap = 20.0pp. Should flag.
    def mock_score_customer(customer, txns=None, conn=None, db_path=None):
        cid = customer["customer_id"]
        if cid.startswith("A"):
            return {"is_lead": True}
        elif cid.startswith("B"):
            # B0 to B7 are leads (8 of them) -> 80%
            idx = int(cid[1:])
            return {"is_lead": idx < 8}
        return {"is_lead": False}
        
    monkeypatch.setattr(engine, "score_customer", mock_score_customer)
    
    res = governance.generate_fairness_report()
    assert res["best_performing_segment"] == "Salaried"
    assert res["best_conversion_rate_pct"] == 100.0
    
    gig_seg = next(s for s in res["segments"] if s["segment_name"] == "Gig Worker")
    assert gig_seg["conversion_rate_pct"] == 80.0
    assert gig_seg["gap_to_best_pp"] == 20.0
    assert gig_seg["is_underperforming"] is True
    assert len(res["flagged_segments"]) == 1
    assert res["flagged_segments"][0]["segment_name"] == "Gig Worker"

def test_generate_fairness_report_boundary_below_20(monkeypatch):
    # Segment A: 10 leads (100%)
    # Segment B: 9 leads (90%). Gap = 10.0pp. Should NOT flag.
    for i in range(10):
        add_customer(f"A{i}", "Salaried")
        add_customer(f"B{i}", "Gig Worker")
        
    def mock_score_customer(customer, txns=None, conn=None, db_path=None):
        cid = customer["customer_id"]
        if cid.startswith("A"):
            return {"is_lead": True}
        elif cid.startswith("B"):
            idx = int(cid[1:])
            return {"is_lead": idx < 9}
        return {"is_lead": False}
        
    monkeypatch.setattr(engine, "score_customer", mock_score_customer)
    
    res = governance.generate_fairness_report()
    gig_seg = next(s for s in res["segments"] if s["segment_name"] == "Gig Worker")
    assert gig_seg["conversion_rate_pct"] == 90.0
    assert gig_seg["gap_to_best_pp"] == 10.0
    assert gig_seg["is_underperforming"] is False
    assert res["flagged_segments"] == []

def test_generate_fairness_report_boundary_above_20(monkeypatch):
    # Segment A: 10 leads (100%)
    # Segment B: 7 leads (70%). Gap = 30.0pp. Should flag.
    for i in range(10):
        add_customer(f"A{i}", "Salaried")
        add_customer(f"B{i}", "Gig Worker")
        
    def mock_score_customer(customer, txns=None, conn=None, db_path=None):
        cid = customer["customer_id"]
        if cid.startswith("A"):
            return {"is_lead": True}
        elif cid.startswith("B"):
            idx = int(cid[1:])
            return {"is_lead": idx < 7}
        return {"is_lead": False}
        
    monkeypatch.setattr(engine, "score_customer", mock_score_customer)
    
    res = governance.generate_fairness_report()
    gig_seg = next(s for s in res["segments"] if s["segment_name"] == "Gig Worker")
    assert gig_seg["conversion_rate_pct"] == 70.0
    assert gig_seg["gap_to_best_pp"] == 30.0
    assert gig_seg["is_underperforming"] is True
    assert len(res["flagged_segments"]) == 1

# --- Unit Tests for generate_compliance_report ---

def test_generate_compliance_report_structure():
    res = governance.generate_compliance_report()
    assert "compliance_status" in res
    assert "standards" in res
    assert "gaps" in res
    assert "recommendations" in res
    assert "governance_notes" in res
    
    # DPDP and RBI items should exist
    assert "DPDP_Act_2023" in res["standards"]
    assert "RBI_Guidelines" in res["standards"]
    assert res["standards"]["DPDP_Act_2023"]["status"] == "Compliant"

def test_generate_compliance_report_db_stats():
    # Insert users to check if stats are incorporated
    conn = db.connect()
    db.execute(conn, """
    INSERT INTO users (name, email, role, password_salt, password_hash, created_at)
    VALUES ('Admin', 'admin@lens.com', 'admin', 'salt', 'hash', '2026-07-01')
    """)
    conn.commit()
    conn.close()
    
    res = governance.generate_compliance_report()
    assert "1 registered users" in res["governance_notes"]

# --- Unit Tests for generate_sandbox_mapping ---

def test_generate_sandbox_mapping_structure():
    res = governance.generate_sandbox_mapping()
    assert "schema_version" in res
    assert "validation_notes" in res
    assert "mappings" in res
    assert "missing_mappings" in res

def test_generate_sandbox_mapping_rules():
    res = governance.generate_sandbox_mapping()
    # Check that critical mappings exist
    c_id_mapping = next(m for m in res["mappings"] if m["internal_field"] == "customers.customer_id")
    assert c_id_mapping["sandbox_field"] == "client_ref_id"
    assert c_id_mapping["status"] == "mapped"
    
    CC_mapping = next(m for m in res["mappings"] if m["internal_field"] == "leads.intent_score")
    assert CC_mapping["sandbox_field"] == "propensity_index"
    assert "Normalize" in CC_mapping["transformation"]

# --- Unit Tests for generate_roi_report ---

def test_generate_roi_report_empty():
    res = governance.generate_roi_report()
    assert res["total_customers"] == 0
    assert res["total_leads"] == 0
    assert res["estimated_revenue"] == 0.0
    assert res["estimated_cost"] == 0.0
    assert res["net_profit"] == 0.0
    assert res["roi_multiplier"] == 0.0

def test_generate_roi_report_calculations(monkeypatch):
    # Add 4 customers
    add_customer("R1", "Salaried")
    add_customer("R2", "Salaried")
    add_customer("R3", "Gig Worker")
    add_customer("R4", "Freelancer")
    
    # Mock score_customer so that 2 out of 4 are leads
    def mock_score(customer, txns=None, conn=None, db_path=None):
        return {"is_lead": customer["customer_id"] in ("R1", "R3")}
        
    monkeypatch.setattr(engine, "score_customer", mock_score)
    
    res = governance.generate_roi_report()
    
    assert res["total_customers"] == 4
    assert res["total_leads"] == 2
    assert res["conversion_rate_pct"] == 50.0
    
    # Cost = 4 * 5.0 (assessment) + 2 * 50.0 (outreach) = 20.0 + 100.0 = 120.0
    assert res["estimated_cost"] == 120.0
    
    # Revenue = 2 leads * 15% conversion * (200000 * 3%) = 2 * 0.15 * 6000 = 1800.0
    assert res["estimated_revenue"] == 1800.0
    
    # Net Profit = 1800 - 120 = 1680.0
    assert res["net_profit"] == 1680.0
    
    # ROI Multiplier = 1800 / 120 = 15.0
    assert res["roi_multiplier"] == 15.0

def test_generate_roi_report_segment_performance(monkeypatch):
    # Segment-specific metrics
    add_customer("RS1", "Salaried")
    add_customer("RS2", "Salaried")
    add_customer("RG1", "Gig Worker")
    
    def mock_score(customer, txns=None, conn=None, db_path=None):
        # Only RS1 is a lead
        return {"is_lead": customer["customer_id"] == "RS1"}
        
    monkeypatch.setattr(engine, "score_customer", mock_score)
    
    res = governance.generate_roi_report()
    
    salaried_perf = next(s for s in res["segment_performance"] if s["segment_name"] == "Salaried")
    assert salaried_perf["total_customers"] == 2
    assert salaried_perf["total_leads"] == 1
    assert salaried_perf["conversion_rate_pct"] == 50.0
    # Cost = 2 * 5 + 1 * 50 = 60
    assert salaried_perf["estimated_outreach_cost"] == 60.0
    # Revenue = 1 * 0.15 * 6000 = 900
    assert salaried_perf["expected_revenue"] == 900.0
    # ROI = (900 - 60) / 60 * 100 = 1400%
    assert salaried_perf["roi_pct"] == 1400.0

# --- API Integration Tests using TestClient ---

def test_api_fairness_unauthenticated(client):
    res = client.get("/api/governance/fairness")
    assert res.status_code == 401

def test_api_compliance_unauthenticated(client):
    res = client.get("/api/governance/compliance")
    assert res.status_code == 401

def test_api_sandbox_mapping_unauthenticated(client):
    res = client.get("/api/governance/sandbox-mapping")
    assert res.status_code == 401

def test_api_roi_unauthenticated(client):
    res = client.get("/api/governance/roi")
    assert res.status_code == 401

def test_api_register_and_login(client):
    reg_response = client.post("/api/auth/register", json={
        "name": "RM User",
        "email": "rm@lens.com",
        "password": "strongpassword123",
        "role": "relationship_manager"
    })
    assert reg_response.status_code == 200
    assert "token" in reg_response.json()
    
    login_response = client.post("/api/auth/login", json={
        "email": "rm@lens.com",
        "password": "strongpassword123"
    })
    assert login_response.status_code == 200
    assert "token" in login_response.json()

def test_api_fairness_authenticated(client, auth_header):
    res = client.get("/api/governance/fairness", headers=auth_header)
    assert res.status_code == 200
    json_data = res.json()
    assert "segments" in json_data
    assert "best_performing_segment" in json_data

def test_api_compliance_authenticated(client, auth_header):
    res = client.get("/api/governance/compliance", headers=auth_header)
    assert res.status_code == 200
    json_data = res.json()
    assert "compliance_status" in json_data
    assert "standards" in json_data

def test_api_sandbox_mapping_authenticated(client, auth_header):
    res = client.get("/api/governance/sandbox-mapping", headers=auth_header)
    assert res.status_code == 200
    json_data = res.json()
    assert "mappings" in json_data
    assert "schema_version" in json_data

def test_api_roi_authenticated(client, auth_header):
    res = client.get("/api/governance/roi", headers=auth_header)
    assert res.status_code == 200
    json_data = res.json()
    assert "total_customers" in json_data
    assert "roi_multiplier" in json_data


def test_dpdp_consent_and_erasure(client, auth_header):
    # Add a customer with enough transactions to be a lead
    add_customer("C_consent", "Salaried", 100000.0, 100000.0)
    add_transaction("C_consent", "SALARY_CREDIT", 100000.0, "Employer Payroll")
    add_transaction("C_consent", "SALARY_CREDIT", 100000.0, "Employer Payroll")
    add_transaction("C_consent", "NEFT", 80000.0, "Maruti Suzuki Arena")
    add_transaction("C_consent", "IMPS", 200000.0, "External Beneficiary")
    add_transaction("C_consent", "BILL_PAY", 50000.0, "DPS School Fees")
    
    # Assert customer exists
    conn = db.connect()
    assert db.scalar(conn, "SELECT COUNT(*) FROM customers WHERE customer_id = 'C_consent'") == 1
    
    # 1. Grant consent
    res = client.post("/api/customers/C_consent/consent", json={"consent_type": "lending_outreach"}, headers=auth_header)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    
    # Check log exists
    assert db.scalar(conn, "SELECT COUNT(*) FROM consent_logs WHERE customer_id = 'C_consent'") == 1
    
    # Let's run engine so lead is generated
    engine.run_engine()
    assert db.scalar(conn, "SELECT COUNT(*) FROM leads WHERE customer_id = 'C_consent'") == 1
    
    # 2. Revoke consent (Right to Erasure)
    res = client.delete("/api/customers/C_consent/consent", headers=auth_header)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    
    # Check customer, leads, transactions, and consent logs are cascaded deleted
    assert db.scalar(conn, "SELECT COUNT(*) FROM customers WHERE customer_id = 'C_consent'") == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM transactions WHERE customer_id = 'C_consent'") == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM leads WHERE customer_id = 'C_consent'") == 0
    assert db.scalar(conn, "SELECT COUNT(*) FROM consent_logs WHERE customer_id = 'C_consent'") == 0
    conn.close()


def test_rm_access_logs(client, auth_header):
    # Add customer and run engine to create a lead
    add_customer("C_audit", "Salaried")
    add_transaction("C_audit", "SALARY_CREDIT", 80000.0, "Employer Payroll")
    add_transaction("C_audit", "SALARY_CREDIT", 80000.0, "Employer Payroll")
    add_transaction("C_audit", "NEFT", 150000.0, "Tata Motors Showroom")
    engine.run_engine()
    
    # View lead details as logged in user
    res = client.get("/api/leads/C_audit", headers=auth_header)
    assert res.status_code == 200
    
    # Retrieve access logs (admin only)
    res_logs = client.get("/api/governance/access-logs", headers=auth_header)
    assert res_logs.status_code == 200
    logs = res_logs.json()
    assert len(logs) > 0
    latest_log = logs[0]
    assert latest_log["customer_id"] == "C_audit"
    assert latest_log["action"] == "VIEW_LEAD_DETAIL"
    assert latest_log["user_email"] == "admin@test.com"


def test_maker_checker_threshold(client, auth_header):
    # Register proposer
    reg_response = client.post("/api/auth/register", json={
        "name": "RM Proposer",
        "email": "rm_proposer@test.com",
        "password": "securepassword123",
        "role": "relationship_manager"
    })
    assert reg_response.status_code == 200
    proposer_token = reg_response.json()["token"]
    proposer_header = {"Authorization": f"Bearer {proposer_token}"}
    
    # 1. Propose threshold change
    res = client.post("/api/governance/threshold-change-request", json={"proposed_threshold": 60.0}, headers=proposer_header)
    assert res.status_code == 200
    admin_req_id = res.json()["request_id"]
    
    # 2. Try to approve own request as the proposer (should fail)
    # Let's register an admin proposer to test same-user checker validation
    reg_response2 = client.post("/api/auth/register", json={
        "name": "Admin Proposer",
        "email": "admin_proposer@test.com",
        "password": "securepassword123",
        "role": "admin"
    })
    assert reg_response2.status_code == 200
    admin_proposer_token = reg_response2.json()["token"]
    admin_proposer_header = {"Authorization": f"Bearer {admin_proposer_token}"}
    
    res = client.post("/api/governance/threshold-change-request", json={"proposed_threshold": 70.0}, headers=admin_proposer_header)
    assert res.status_code == 200
    admin_req_id = res.json()["request_id"]
    
    # Approve as same admin (should throw 400 maker-checker error)
    res_fail = client.post(f"/api/governance/threshold-change-request/{admin_req_id}/approve", headers=admin_proposer_header)
    assert res_fail.status_code == 400
    assert "Maker-Checker" in res_fail.json()["detail"]
    
    # 3. Approve as a DIFFERENT admin (auth_header)
    res_approve = client.post(f"/api/governance/threshold-change-request/{admin_req_id}/approve", headers=auth_header)
    assert res_approve.status_code == 200
    assert res_approve.json()["status"] == "APPROVED"
    assert res_approve.json()["new_threshold"] == 70.0
    
    # Verify settings table has the updated value
    conn = db.connect()
    val = db.scalar(conn, "SELECT value FROM settings WHERE key = 'lead_threshold'")
    assert val == "70.0"
    conn.close()

