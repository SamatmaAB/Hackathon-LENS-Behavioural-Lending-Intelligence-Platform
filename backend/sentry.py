"""
LENS SENTRY — Anomaly Detection Module (Feature 4)
====================================================
Runs unsupervised anomaly detection (IsolationForest) over each customer's
transaction pattern to flag statistically unusual behaviour.

Feeds fraud_risk_score and is_anomalous into the leads table;
does not alter any deterministic PULSE/TRUST score unless anomalous (trust -15).
"""
import numpy as np
from datetime import datetime


def _extract_txn_features(txns: list) -> np.ndarray:
    """
    One feature vector per customer from their transaction list.
    Features (7):
      txn_count, mean_amount, std_amount, max_amount,
      unique_counterparties, night_time_ratio, weekend_ratio
    """
    if not txns:
        return np.zeros(7)

    amounts = np.array([t["amount"] for t in txns], dtype=float)
    counterparties = set(t["counterparty"] for t in txns)

    hours = []
    weekend_count = 0
    for t in txns:
        try:
            ts = t.get("timestamp", "")
            if ts:
                ts_clean = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
                dt = datetime.fromisoformat(ts_clean)
                hours.append(dt.hour)
                if dt.weekday() >= 5:
                    weekend_count += 1
        except Exception:
            continue

    n = max(len(txns), 1)
    nh = max(len(hours), 1)
    night_ratio = sum(1 for h in hours if h < 6 or h > 22) / nh
    weekend_ratio = weekend_count / n

    return np.array([
        len(txns),
        float(amounts.mean()),
        float(amounts.std()) if len(amounts) > 1 else 0.0,
        float(amounts.max()),
        len(counterparties),
        night_ratio,
        weekend_ratio,
    ])


def compute_fraud_risk_scores(all_customer_txns: dict) -> dict:
    """
    all_customer_txns: {customer_id: [txn_dict, ...], ...}
    Returns: {customer_id: {"fraud_risk_score": float 0-100, "is_anomalous": bool}}
    """
    customer_ids = list(all_customer_txns.keys())
    if not customer_ids:
        return {}

    feature_matrix = np.array(
        [_extract_txn_features(all_customer_txns[cid]) for cid in customer_ids],
        dtype=float,
    )

    if len(customer_ids) < 10:
        # Not enough data for a meaningful IsolationForest — return neutral scores
        return {cid: {"fraud_risk_score": 0.0, "is_anomalous": False} for cid in customer_ids}

    try:
        from sklearn.ensemble import IsolationForest
        model = IsolationForest(n_estimators=150, contamination=0.05, random_state=42)
        model.fit(feature_matrix)

        raw_scores = model.decision_function(feature_matrix)   # higher = more normal
        predictions = model.predict(feature_matrix)            # -1 = anomaly, 1 = normal

        min_s, max_s = raw_scores.min(), raw_scores.max()
        span = max(max_s - min_s, 1e-6)

        results = {}
        for i, cid in enumerate(customer_ids):
            normalized = (raw_scores[i] - min_s) / span  # 0=most anomalous, 1=most normal
            risk_score = round((1 - normalized) * 100, 1)
            results[cid] = {
                "fraud_risk_score": risk_score,
                "is_anomalous": bool(predictions[i] == -1),
            }
        return results

    except ImportError:
        print("[SENTRY] scikit-learn not installed — returning neutral anomaly scores.")
        return {cid: {"fraud_risk_score": 0.0, "is_anomalous": False} for cid in customer_ids}
    except Exception as e:
        print(f"[SENTRY] IsolationForest error: {e} — returning neutral anomaly scores.")
        return {cid: {"fraud_risk_score": 0.0, "is_anomalous": False} for cid in customer_ids}
