import os
import pytest
from fastapi.testclient import TestClient
from backend.app import app


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


def test_lead_report_endpoint(client, admin_token):
    client.post("/api/generate", json={"n_customers": 20}, headers={"Authorization": f"Bearer {admin_token}"})
    resp = client.get("/api/leads", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    data = resp.json()
    if data:
        cust_id = data[0]["customer_id"]
        report_resp = client.get(f"/api/leads/{cust_id}/report", headers={"Authorization": f"Bearer {admin_token}"})
        assert report_resp.status_code == 200
        assert "LENS Executive Summary" in report_resp.text
