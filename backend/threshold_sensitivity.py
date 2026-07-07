"""
LENS Threshold Sensitivity Analysis
=====================================
Sweeps candidate trust-score thresholds and reports, at each one:
  - lead_volume: how many customers would clear this bar
  - conversion_rate_pct: of those, what fraction are genuine prospects
    (true_loan_type != 'None' in the synthetic ground truth)

This turns a single reported conversion number into a tunable curve so
the bank can pick its own risk/volume tradeoff rather than trusting one
hardcoded cutoff.
"""
from typing import List, Dict, Any


def compute_threshold_curve(conn, db, thresholds: List[int] = None) -> Dict[str, Any]:
    if thresholds is None:
        thresholds = list(range(30, 91, 5))  # 30, 35, ..., 90

    rows = db.rows(
        conn,
        """
        SELECT l.customer_id, l.trust_score, c.true_loan_type
        FROM leads l
        JOIN customers c ON c.customer_id = l.customer_id
        """,
    )
    if not rows:
        return {
            "curve": [],
            "total_leads_evaluated": 0,
            "recommended_threshold_for_30pct_target": None,
        }

    curve = []
    for threshold in thresholds:
        cleared = [r for r in rows if (r["trust_score"] or 0) >= threshold]
        volume = len(cleared)
        genuine = sum(1 for r in cleared if r["true_loan_type"] and r["true_loan_type"] != "None")
        conversion_pct = round(100 * genuine / volume, 1) if volume else 0.0
        curve.append({
            "trust_score_threshold": threshold,
            "lead_volume": volume,
            "conversion_rate_pct": conversion_pct,
        })

    # Find the threshold that first clears 30% — useful for the pitch:
    # "at threshold X, we exceed the 30% target with Y leads/month"
    first_above_30 = next((c for c in curve if c["conversion_rate_pct"] >= 30.0), None)

    return {
        "curve": curve,
        "total_leads_evaluated": len(rows),
        "recommended_threshold_for_30pct_target": first_above_30,
    }
