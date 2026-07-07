import pytest
from fastapi.testclient import TestClient
from backend.app import app

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def admin_token(client):
    client.post('/api/auth/register', json={'name': 'Test Admin', 'email': 'admin@test.com', 'password': 'password123', 'role': 'RM'})
    resp = client.post('/api/auth/login', json={'email': 'admin@test.com', 'password': 'password123'})
    return resp.json()['token']

def test_ingest_statement_endpoint_requires_consent(client, admin_token):
    response = client.post(
        '/api/customers/TEST1/ingest-statement',
        files={'file': ('test.csv', b'Date,Description,Amount\n2026-01-01,Salary,50000', 'text/csv')},
        data={'consent_confirmed': 'false'},
        headers={'Authorization': f'Bearer {admin_token}'}
    )
    assert response.status_code == 422

