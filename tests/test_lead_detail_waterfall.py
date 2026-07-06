import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from backend.app import app


client = TestClient(app)


def _login_admin():
    r = client.post("/api/auth/login", json={"email": "admin@idbibank.com", "password": "idbi@12345"})
    return r.json()["token"]


def test_lead_detail_exposes_trust_subscores():
    token = _login_admin()
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/generate?seed=42&n_customers=40", headers=headers)
    leads = client.get("/api/leads?limit=5", headers=headers).json()
    assert leads, "expected at least one lead in a 40-customer seeded dataset"
    detail = client.get(f"/api/leads/{leads[0]['customer_id']}", headers=headers).json()
    lead = detail["lead"]
    assert "income_confidence" in lead and lead["income_confidence"] is not None
    assert "repay_score" in lead and lead["repay_score"] is not None
    # Sanity-check the waterfall math actually reconstructs the stored trust_score
    recomputed = round(
        lead["intent_score"] * 0.4 + lead["income_confidence"] * 0.3 + lead["repay_score"] * 0.3,
        1,
    )
    assert abs(recomputed - lead["trust_score"]) <= 15.1  # allows for SENTRY anomaly -15 dock
