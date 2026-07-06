import os
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend import db


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db_file = tmp_path / "test_lens.db"
    test_db_path = str(test_db_file)
    
    # Store old env and set to test db path
    old_db_path = os.environ.get("LENS_DB_PATH")
    os.environ["LENS_DB_PATH"] = test_db_path
    
    # Re-initialize db for testing
    import backend.app as app_module
    app_module.DB_PATH = test_db_path
    app_module.init_database()
    
    yield test_db_path
    
    # Restore env
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


def test_segmentation_endpoint_returns_valid_data(client, admin_token):
    # First generate data so the endpoint has something to return
    client.post("/api/generate?n_customers=20", headers={"Authorization": f"Bearer {admin_token}"})
    
    resp = client.get("/api/leads/segmentation", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    if data:
        item = data[0]
        assert "customer_id" in item
        assert "income" in item
        assert "trust_score" in item
        assert "tier" in item
        assert "eligible_amount" in item
        assert "employment_type" in item


def test_governance_evaluation_endpoint(client, admin_token):
    client.post("/api/generate?n_customers=20", headers={"Authorization": f"Bearer {admin_token}"})
    resp = client.get("/api/governance/evaluation", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert "class_metrics" in data or "macro_averages" in data


def test_generate_conflict_when_already_running(client, admin_token, monkeypatch):
    import backend.app as app_module
    monkeypatch.setattr(app_module, "is_generating", True)
    resp = client.post("/api/generate?n_customers=25", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 409


def test_lead_narrative_endpoint(client, admin_token, monkeypatch):
    client.post('/api/generate?n_customers=20', headers={'Authorization': f'Bearer {admin_token}'})
    def fake_generate(*args, **kwargs):
        return {'narrative': 'mocked', 'outreach_draft': 'draft', 'objections': ['obj']}
    from backend import app as app_module
    monkeypatch.setattr(app_module, '_gen_narrative', fake_generate)
    resp = client.get('/api/leads', headers={'Authorization': f'Bearer {admin_token}'})
    leads = resp.json()
    if not leads:
        return # nothing to test if no leads
    cust_id = leads[0]['customer_id']
    resp = client.post(f'/api/leads/{cust_id}/narrative', headers={'Authorization': f'Bearer {admin_token}'})
    assert resp.status_code == 200
    assert resp.json()['narrative'] == 'mocked'

