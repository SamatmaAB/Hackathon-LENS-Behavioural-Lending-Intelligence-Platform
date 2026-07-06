import pytest
from backend import feedback, db, app
import os
import sqlite3

@pytest.fixture
def db_conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    app.DB_PATH = db_path
    db.DATABASE_URL = None
    db.IS_POSTGRES = False
    app.init_database()
    
    conn = db.connect(db_path)
    # Insert a dummy user
    db.execute(conn, "INSERT INTO users (name, email, role, password_salt, password_hash, created_at) VALUES ('Admin', 'admin@example.com', 'admin', 'salt', 'hash', '2026-01-01')")
    
    yield conn
    conn.close()

@pytest.fixture
def seeded_lead_customer(db_conn):
    # Insert customer
    customer_id = "TEST_CUST_1"
    db.execute(db_conn, "INSERT INTO customers (customer_id, name, employment_type) VALUES (?, ?, ?)", (customer_id, "Test User", "Salaried"))
    # Insert lead
    db.execute(db_conn, "INSERT INTO leads (customer_id, trust_score, intent_score, triggers_fired, lead_card_generated_at) VALUES (?, ?, ?, ?, ?)", (customer_id, 80, 50, "wallet_topup_frequency", "2026-01-01T00:00:00"))
    db_conn.commit()
    return customer_id

def test_record_outcome_requires_existing_lead(db_conn):
    with pytest.raises(ValueError, match="No lead record"):
        feedback.record_outcome(db_conn, "NONEXISTENT_CUST", user_id=1, outcome="converted")


def test_precision_report_flags_underperforming_trigger(db_conn, seeded_lead_customer):
    for i in range(12):
        feedback.record_outcome(db_conn, seeded_lead_customer, user_id=1, outcome="contacted_no_response")
    report = feedback.generate_trigger_precision_report(db_conn)
    flagged = [r["trigger_code"] for r in report["recommendations"]]
    assert "wallet_topup_frequency" in flagged
