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


def test_threshold_change_requires_different_approver(client, admin_token):
    resp = client.post("/api/governance/threshold-change-request", json={"proposed_threshold": 50},
                        headers={"Authorization": f"Bearer {admin_token}"})
    request_id = resp.json()["request_id"]
    approve_resp = client.post(f"/api/governance/threshold-change-request/{request_id}/approve",
                                headers={"Authorization": f"Bearer {admin_token}"})
    assert approve_resp.status_code == 400


def test_generate_conflict_when_already_running(client, admin_token, monkeypatch):
    from backend import app as app_module
    monkeypatch.setattr(app_module, "is_generating", True)
    resp = client.post("/api/generate", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 409
