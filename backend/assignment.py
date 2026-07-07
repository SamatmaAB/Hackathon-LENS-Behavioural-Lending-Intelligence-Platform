"""
LENS RM Workload-Aware Assignment
===================================
Assigns scored leads to relationship managers using round-robin weighted by
remaining daily capacity, so the highest-scoring leads don't all pile onto
one RM's queue while others sit idle. This is additive to lead scoring —
it never changes a lead's trust/intent score, only who works it and in
what order.
"""
from datetime import datetime, timezone
from typing import Dict, List, Any


def get_available_rms(conn, db) -> List[Dict[str, Any]]:
    rows = db.rows(
        conn,
        """
        SELECT u.user_id, u.name, COALESCE(rc.max_daily_leads, 15) AS max_daily_leads,
               COALESCE(rc.active_assigned_count, 0) AS active_assigned_count
        FROM users u
        LEFT JOIN rm_capacity rc ON rc.user_id = u.user_id
        WHERE u.role = 'relationship_manager'
        """,
    )
    return [r for r in rows if r["active_assigned_count"] < r["max_daily_leads"]]


def assign_leads_to_rms(conn, db, unassigned_leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    unassigned_leads: list of {customer_id, trust_score, tier} sorted by
    priority (Tier 1 first, then trust_score descending) — caller's
    responsibility to pre-sort by scoring priority; this function only
    decides WHO gets each lead, respecting remaining capacity.
    """
    rms = get_available_rms(conn, db)
    if not rms:
        return {"assigned": [], "unassigned_reason": "No RM capacity available"}

    rms_by_headroom = sorted(rms, key=lambda r: r["max_daily_leads"] - r["active_assigned_count"], reverse=True)
    assignments = []
    rm_idx = 0

    for lead in unassigned_leads:
        # Find next RM with remaining headroom, round-robin style
        attempts = 0
        while attempts < len(rms_by_headroom):
            candidate = rms_by_headroom[rm_idx % len(rms_by_headroom)]
            if candidate["active_assigned_count"] < candidate["max_daily_leads"]:
                db.execute(
                    conn,
                    "INSERT OR REPLACE INTO lead_assignments (customer_id, assigned_rm_id, assigned_at) VALUES (?,?,?)",
                    (lead["customer_id"], candidate["user_id"], datetime.now(timezone.utc).isoformat()),
                )
                candidate["active_assigned_count"] += 1
                assignments.append({
                    "customer_id": lead["customer_id"],
                    "assigned_rm_id": candidate["user_id"],
                    "assigned_rm_name": candidate["name"],
                    "tier": lead.get("tier"),
                })
                rm_idx += 1
                break
            rm_idx += 1
            attempts += 1
        else:
            # All RMs at capacity — stop assigning, leave the rest in the pool
            break

    conn.commit()
    return {
        "assigned": assignments,
        "assigned_count": len(assignments),
        "still_unassigned_count": len(unassigned_leads) - len(assignments),
    }
