"""
LENS ML Inference (Feature 3)
==============================
Loads trained XGBoost model from .joblib files and runs inference with
SHAP-based explainability at request time.

If model files are not found (not yet trained), returns None and the
caller should fall back to the deterministic rule engine.
"""
import os

_MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

FEATURE_TRIGGER_CODES = [
    "salary_inflow_clustering", "large_outward_transfer", "recurring_self_transfer",
    "emi_burden_increase", "property_related_payment", "auto_dealer_payment",
    "education_fee_payment", "medical_large_expense", "wedding_season_spike",
    "multiple_income_sources", "bill_payment_consistency", "wallet_topup_frequency",
    "overdraft_near_miss", "credit_card_full_payment",
]

# Lazy-loaded model artifacts
_model = None
_encoder = None
_feature_columns = None
_explainer = None
_load_attempted = False


def _load_artifacts():
    """Attempt to load model artifacts. Sets globals; returns True on success."""
    global _model, _encoder, _feature_columns, _explainer, _load_attempted
    if _load_attempted:
        return _model is not None
    _load_attempted = True

    model_path    = os.path.join(_MODELS_DIR, "loan_type_model.joblib")
    encoder_path  = os.path.join(_MODELS_DIR, "loan_type_encoder.joblib")
    features_path = os.path.join(_MODELS_DIR, "loan_type_features.joblib")

    if not all(os.path.exists(p) for p in (model_path, encoder_path, features_path)):
        print("[ml_predict] Model files not found — run `python -m backend.train_models` first.")
        return False

    try:
        import joblib
        import shap
        _model           = joblib.load(model_path)
        _encoder         = joblib.load(encoder_path)
        _feature_columns = joblib.load(features_path)
        _explainer       = shap.TreeExplainer(_model)
        print(f"[ml_predict] XGBoost loan-type model loaded ({len(_feature_columns)} features, {len(_encoder.classes_)} classes)")
        return True
    except Exception as e:
        print(f"[ml_predict] Failed to load model: {e}")
        _model = None
        return False


def predict_loan_type_ml(customer: dict, fired_keys: set) -> dict:
    """
    Run ML inference for loan type.

    Returns dict with keys:
      predicted_loan_type, confidence, model, top_contributing_features
    Raises RuntimeError if models are not available (caller should fall back).
    """
    if not _load_artifacts():
        raise RuntimeError("ML model not available")

    import pandas as pd
    import numpy as np

    # Build feature row
    row = {code: int(code in fired_keys) for code in FEATURE_TRIGGER_CODES}
    row["age"] = customer.get("age", 30)

    df = pd.DataFrame([row])

    # Add one-hot employment_type columns to match training frame
    emp_type = customer.get("employment_type", "Salaried")
    for col in _feature_columns:
        if col.startswith("employment_type_"):
            df[col] = 1 if col == f"employment_type_{emp_type}" else 0

    # Ensure all expected columns present, fill missing with 0
    for col in _feature_columns:
        if col not in df.columns:
            df[col] = 0
    df = df[_feature_columns]

    probs = _model.predict_proba(df)[0]
    pred_idx = int(probs.argmax())
    predicted_type = _encoder.inverse_transform([pred_idx])[0]
    confidence = float(probs[pred_idx])

    # SHAP explanation for predicted class
    try:
        shap_values = _explainer.shap_values(df)
        # For multi-class XGBoost: shap_values is a list of arrays [class0, class1, ...]
        if isinstance(shap_values, list):
            class_shap = shap_values[pred_idx][0]
        else:
            # Newer shap versions return 3D array: (samples, features, classes)
            class_shap = shap_values[0, :, pred_idx]

        top_features = sorted(
            zip(_feature_columns, class_shap), key=lambda x: abs(x[1]), reverse=True
        )[:5]
        shap_top = [{"feature": f, "shap_value": round(float(v), 4)} for f, v in top_features]
    except Exception:
        shap_top = []

    return {
        "predicted_loan_type": predicted_type,
        "confidence": round(confidence, 3),
        "model": "xgboost_v1",
        "top_contributing_features": shap_top,
    }
