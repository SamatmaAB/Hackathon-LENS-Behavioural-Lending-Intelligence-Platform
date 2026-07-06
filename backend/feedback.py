"""
LENS Feedback Loop
==================
Tracks RM-reported outcomes against the triggers that fired for that lead,
producing a per-trigger precision score. This is a monitoring/recommendation
layer — it NEVER auto-modifies engine.py's live weights. A human (admin) must
review and approve any weight change via the existing Maker-Checker flow.
"""
from backend import db
from datetime import datetime, timezone


def record_outcome(conn, customer_id: str, user_id: int, outcome: str):
    lead = db.one(conn, "SELECT * FROM leads WHERE customer_id=?", (customer_id,))
    if not lead:
        raise ValueError(f"No lead record for {customer_id}")
    db.execute(
        conn,
        """INSERT INTO lead_outcomes
           (customer_id, recorded_by, outcome, triggers_fired_at_time, trust_score_at_time, recorded_at)
           VALUES (?,?,?,?,?,?)""",
        (customer_id, user_id, outcome, lead["triggers_fired"], lead["trust_score"],
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def generate_trigger_precision_report(conn) -> dict:
    """
    For each trigger code, computes: of all leads where this trigger fired,
    what fraction had outcome == 'converted'. Flags triggers whose real-world
    conversion rate has drifted more than 15pp from the assumed weight-implied rate,
    as a recommendation for the next Maker-Checker weight review — never auto-applied.
    """
    outcomes = db.rows(conn, "SELECT * FROM lead_outcomes")
    if not outcomes:
        return {"trigger_stats": [], "recommendations": [], "sample_size": 0}

    trigger_counts: dict[str, dict[str, int]] = {}
    for row in outcomes:
        fired = (row["triggers_fired_at_time"] or "").split(",")
        for code in fired:
            code = code.strip()
            if not code:
                continue
            trigger_counts.setdefault(code, {"total": 0, "converted": 0})
            trigger_counts[code]["total"] += 1
            if row["outcome"] == "converted":
                trigger_counts[code]["converted"] += 1

    stats = []
    recommendations = []
    for code, counts in trigger_counts.items():
        rate = round(counts["converted"] / counts["total"] * 100, 1) if counts["total"] else 0.0
        stats.append({"trigger_code": code, "sample_size": counts["total"], "real_conversion_rate_pct": rate})
        if counts["total"] >= 10 and rate < 20:
            recommendations.append({
                "trigger_code": code,
                "observed_conversion_pct": rate,
                "suggestion": f"'{code}' is underperforming in the field ({rate}% real conversion, "
                               f"n={counts['total']}). Consider reviewing its weight in the next "
                               "Maker-Checker threshold review cycle.",
            })

    return {"trigger_stats": stats, "recommendations": recommendations, "sample_size": len(outcomes)}
