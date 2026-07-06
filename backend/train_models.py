"""
LENS ML Model Training Script (Feature 3)
==========================================
Trains a gradient-boosted XGBoost classifier to predict loan type from
behavioural trigger patterns (14 features) + customer demographics.

Run once after data generation:
    python -m backend.train_models

Produces three .joblib files in backend/models/:
  - loan_type_model.joblib
  - loan_type_encoder.joblib
  - loan_type_features.joblib
"""
import os
import sys

# Allow running as both `python -m backend.train_models` and `python backend/train_models.py`
try:
    from backend import db
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import db  # type: ignore

import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
import joblib

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

FEATURE_TRIGGER_CODES = [
    "salary_inflow_clustering", "large_outward_transfer", "recurring_self_transfer",
    "emi_burden_increase", "property_related_payment", "auto_dealer_payment",
    "education_fee_payment", "medical_large_expense", "wedding_season_spike",
    "multiple_income_sources", "bill_payment_consistency", "wallet_topup_frequency",
    "overdraft_near_miss", "credit_card_full_payment",
]


def _get_db_path():
    import tempfile
    default = os.path.join(os.path.dirname(__file__), "lens.db")
    return os.getenv("LENS_DB_PATH", default)


def build_training_frame(db_path=None):
    """Build a pandas DataFrame from the DB for training."""
    db_path = db_path or _get_db_path()
    conn = db.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.customer_id, c.age, c.employment_type, c.true_monthly_income,
               c.true_loan_type, l.triggers_fired, l.intent_score
        FROM customers c
        LEFT JOIN leads l ON c.customer_id = l.customer_id
    """)
    rows = cur.fetchall()
    conn.close()

    records = []
    for r in rows:
        if hasattr(r, "keys"):
            row_dict = dict(r)
        else:
            cols = [d[0] for d in cur.description] if cur.description else []
            row_dict = dict(zip(cols, r))

        fired_str = row_dict.get("triggers_fired") or ""
        fired = set(fired_str.split(",")) if fired_str else set()

        record = {code: int(code in fired) for code in FEATURE_TRIGGER_CODES}
        record["age"] = row_dict.get("age", 30)
        record["employment_type"] = row_dict.get("employment_type", "Salaried")
        record["true_loan_type"] = row_dict.get("true_loan_type") or "None"
        records.append(record)

    return pd.DataFrame(records)


def train_loan_type_model(db_path=None):
    """Train the XGBoost loan-type model and save artifacts."""
    os.makedirs(MODELS_DIR, exist_ok=True)

    df = build_training_frame(db_path)
    df = df[df["true_loan_type"] != "None"].copy()

    if len(df) < 20:
        print(f"ERROR: Only {len(df)} training samples — generate more data first.")
        return None

    X = pd.get_dummies(df.drop(columns=["true_loan_type"]), columns=["employment_type"])
    y_encoder = LabelEncoder()
    y = y_encoder.fit_transform(df["true_loan_type"])

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y if len(set(y)) > 1 else None
    )

    model = xgb.XGBClassifier(
        n_estimators=150,
        max_depth=4,
        learning_rate=0.08,
        objective="multi:softprob",
        eval_metric="mlogloss",
        random_state=42,
        verbosity=0,
    )
    model.fit(X_train, y_train)

    accuracy = model.score(X_test, y_test)
    print(f"[train_models] Loan type model test accuracy: {accuracy:.3f} ({len(X_train)} train, {len(X_test)} test samples)")

    joblib.dump(model,            os.path.join(MODELS_DIR, "loan_type_model.joblib"))
    joblib.dump(y_encoder,        os.path.join(MODELS_DIR, "loan_type_encoder.joblib"))
    joblib.dump(list(X.columns),  os.path.join(MODELS_DIR, "loan_type_features.joblib"))
    joblib.dump(list(model.feature_importances_), os.path.join(MODELS_DIR, "loan_type_importances.joblib"))

    print(f"[train_models] Models saved to {MODELS_DIR}/")
    return accuracy


if __name__ == "__main__":
    acc = train_loan_type_model()
    if acc is not None:
        print(f"Training complete — accuracy: {acc:.1%}")
