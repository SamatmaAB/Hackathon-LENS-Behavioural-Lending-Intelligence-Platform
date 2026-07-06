import os
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend import db

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db_file = tmp_path / "test_lens.db"
    test_db_path = str(test_db_file)
    old_db_path = os.environ.get("LENS_DB_PATH")
    os.environ["LENS_DB_PATH"] = test_db_path
    
    import backend.app as app_module
    app_module.DB_PATH = test_db_path
    app_module.init_database()
    
    yield test_db_path
    
    if old_db_path is not None:
        os.environ["LENS_DB_PATH"] = old_db_path
    else:
        del os.environ["LENS_DB_PATH"]

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def admin_token(client):
    client.post("/api/auth/register", json={
        "name": "Test Admin",
        "email": "admin@test.com",
        "password": "password123",
        "role": "RM"
    })
    resp = client.post("/api/auth/login", json={
        "email": "admin@test.com",
        "password": "password123"
    })
    return resp.json()["token"]

@pytest.fixture
def sample_customer_id(client, admin_token):
    from backend import db
    conn = db.connect(os.environ["LENS_DB_PATH"])
    customer_id = "TEST_WHATIF_1"
    db.execute(conn, "INSERT INTO customers (customer_id, name, employment_type) VALUES (?, ?, ?)", (customer_id, "Test User", "Salaried"))
    db.execute(conn, "INSERT INTO leads (customer_id, trust_score, intent_score, triggers_fired) VALUES (?, ?, ?, ?)", (customer_id, 80, 50, "wallet_topup_frequency"))
    conn.commit()
    conn.close()
    return customer_id

def test_what_if_property_payment_pushes_score_up(client, admin_token, sample_customer_id, monkeypatch):
    import backend.engine as engine_module
    monkeypatch.setattr(engine_module, '_semantic_classify', None)
    resp = client.post("/api/simulate/what-if", json={
        "customer_id": sample_customer_id,
        "hypothetical_transaction": {
            "type": "IMPS", "amount": 200000, "counterparty": "DLF Homes",
            "timestamp": "2026-07-07T10:00:00",
        },
    }, headers={"Authorization": f"Bearer {admin_token}"})
    body = resp.json()
    assert body["delta_intent"] > 0
    assert "property_related_payment" in body["newly_fired_triggers"]
