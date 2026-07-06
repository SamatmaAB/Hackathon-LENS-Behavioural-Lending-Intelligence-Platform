import os
import joblib

def test_train_models_produces_valid_artifacts(tmp_path):
    from backend import app as app_module
    from backend import train_models
    from fastapi.testclient import TestClient
    
    test_db = str(tmp_path / "test_lens.db")
    app_module.DB_PATH = test_db
    app_module.init_database()
    
    client = TestClient(app_module.app)
    client.post("/api/auth/register", json={
        "name": "Admin", "email": "admin@idbibank.com", "password": "idbi@12345", "role": "RM"
    })
    r = client.post("/api/auth/login", json={"email": "admin@idbibank.com", "password": "idbi@12345"})
    token = r.json()["token"]
    
    client.post("/api/generate?n_customers=100", headers={"Authorization": f"Bearer {token}"})
    
    train_models.MODELS_DIR = str(tmp_path / "models")
    train_models.train_loan_type_model(test_db)
    
    models_dir = train_models.MODELS_DIR
    model = joblib.load(os.path.join(models_dir, "loan_type_model.joblib"))
    encoder = joblib.load(os.path.join(models_dir, "loan_type_encoder.joblib"))
    features = joblib.load(os.path.join(models_dir, "loan_type_features.joblib"))

    assert hasattr(model, "predict_proba")
    assert len(encoder.classes_) >= 3   # Personal/Auto/Home/Mortgage
    assert len(features) > 0
