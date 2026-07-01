import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

try:
    from backend import db, engine
except ImportError:
    import db, engine  # type: ignore[no-redef]

logger = logging.getLogger("lens.governance")

# Cost and Benefit Assumptions for ROI Calculations
COST_PER_ASSESSMENT = 5.0      # Cost to assess a single customer's transactions (INR)
COST_PER_OUTREACH = 50.0       # Cost to contact a lead (notification/SMS/call) (INR)
EXPECTED_CONVERSION_RATE = 0.15 # Lead to Loan disbursal rate (15%)
AVERAGE_LOAN_AMOUNT = 200000.0 # Average loan ticket size (INR)
NET_YIELD_MARGIN = 0.03        # Net profit margin per disbursed loan (3%)

def generate_fairness_report(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Dynamically assesses conversion rates across different customer segments (employment types)
    and flags segments that underperform the best segment by 20 percentage points or more.
    """
    logger.info("Generating dynamic fairness report")
    conn = db.connect(db_path)
    try:
        customers = db.rows(conn, "SELECT * FROM customers")
        if not customers:
            return {
                "segments": [],
                "best_performing_segment": None,
                "best_conversion_rate_pct": 0.0,
                "underperforming_segments": [],
                "flagged_segments": [],
                "recommendation_summary": "No customer data available to perform fairness analysis."
            }

        # Calculate scores dynamically for all customers using the exposed score_customer function
        segment_data: Dict[str, Dict[str, int]] = {}
        for cust in customers:
            emp_type = cust.get("employment_type", "Unknown") or "Unknown"
            if emp_type not in segment_data:
                segment_data[emp_type] = {"total": 0, "leads": 0}
            
            segment_data[emp_type]["total"] += 1
            
            txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp", (cust["customer_id"],))
            score_res = engine.score_customer(cust, txns=txns, conn=conn)
            if score_res and score_res.get("is_lead"):
                segment_data[emp_type]["leads"] += 1

        segments_list = []
        for segment_name, counts in segment_data.items():
            total = counts["total"]
            leads = counts["leads"]
            rate = round((leads / total) * 100.0, 1) if total > 0 else 0.0
            segments_list.append({
                "segment_name": segment_name,
                "total_customers": total,
                "total_leads": leads,
                "conversion_rate_pct": rate,
                "is_underperforming": False,
                "gap_to_best_pp": 0.0
            })

        # Identify best segment
        best_segment_name = None
        best_rate = 0.0
        if segments_list:
            best_seg = max(segments_list, key=lambda x: x["conversion_rate_pct"])
            best_segment_name = best_seg["segment_name"]
            best_rate = best_seg["conversion_rate_pct"]

        underperforming_names = []
        flagged_segments = []

        for seg in segments_list:
            gap = round(best_rate - seg["conversion_rate_pct"], 1)
            seg["gap_to_best_pp"] = gap
            if gap >= 20.0:
                seg["is_underperforming"] = True
                underperforming_names.append(seg["segment_name"])
                
                # Dynamic recommendations avoiding hardcoded assumptions
                recommendation = (
                    f"Segment '{seg['segment_name']}' conversion rate ({seg['conversion_rate_pct']}%) "
                    f"trails best segment '{best_segment_name}' ({best_rate}%) by {gap}pp. "
                    f"Action Required: Consider adjusting lead thresholds or scoring trigger weights for this group."
                )
                flagged_segments.append({
                    "segment_name": seg["segment_name"],
                    "conversion_rate_pct": seg["conversion_rate_pct"],
                    "gap_to_best_pp": gap,
                    "recommendation": recommendation
                })

        # Order segment results by name for consistency
        segments_list.sort(key=lambda x: x["segment_name"])
        flagged_segments.sort(key=lambda x: x["segment_name"])
        underperforming_names.sort()

        if flagged_segments:
            recommendation_summary = (
                f"Fairness gaps detected in {len(flagged_segments)} segment(s): "
                f"{', '.join(underperforming_names)}. Review lead-scoring threshold calibrations."
            )
        else:
            recommendation_summary = "No significant fairness gaps detected (all segments within 20pp of the best-performing segment)."

        return {
            "segments": segments_list,
            "best_performing_segment": best_segment_name,
            "best_conversion_rate_pct": best_rate,
            "underperforming_segments": underperforming_names,
            "flagged_segments": flagged_segments,
            "recommendation_summary": recommendation_summary
        }
    finally:
        conn.close()

def generate_compliance_report(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Evaluates system attributes against Indian DPDP Act 2023 and RBI Digital Lending guidelines.
    Reads current user registration and lead metrics to contextualize.
    """
    logger.info("Generating compliance report")
    conn = db.connect(db_path)
    try:
        user_count = db.scalar(conn, "SELECT COUNT(*) FROM users") or 0
        lead_count = db.scalar(conn, "SELECT COUNT(*) FROM leads") or 0
        db_type = "PostgreSQL" if db.IS_POSTGRES else "SQLite"
    except Exception as e:
        logger.error(f"Error reading DB stats for compliance: {e}")
        user_count = 0
        lead_count = 0
        db_type = "Unknown"
    finally:
        conn.close()

    standards = {
        "DPDP_Act_2023": {
            "status": "Attention Required",
            "description": "Digital Personal Data Protection Act (India)",
            "considerations": "Requires clear notice, active consent mechanisms, right to erasure, and minimal processing.",
            "gaps": [
                "No database table recording timestamped consent metadata for customer transactions.",
                "No API endpoint or mechanism to support right to erasure / right to delete customer history."
            ],
            "recommendations": [
                "Implement a new database table and endpoint for active user consent records.",
                "Create a customer profile deletion script that cascades cleanly across transactions and leads tables."
            ]
        },
        "RBI_Guidelines": {
            "status": "Attention Required",
            "description": "RBI Digital Lending Directives & Fair Practices Code",
            "considerations": "Requires RM audit trails, data localization, transparency of scoring, and clear credit justification.",
            "gaps": [
                "Relationship Manager dashboard queries are not logged for audit tracking.",
                "Scoring parameter overrides lack dual-authorization ('Maker-Checker') workflows."
            ],
            "recommendations": [
                "Introduce an RM activity audit log table to record all queries on lead detail endpoints.",
                "Implement a multi-signature approval check in the FastAPI router before changing lead scoring configurations."
            ]
        },
        "Data_Minimization": {
            "status": "Compliant",
            "description": "Limiting collected data to specific processing needs",
            "considerations": "Only transaction metadata (type, amount, counterparty) and basic demographics are stored.",
            "gaps": [],
            "recommendations": [
                "Maintain policy of not storing raw text payloads, full bank statements, or unrelated behavioral files."
            ]
        },
        "Explainability": {
            "status": "Compliant",
            "description": "Ensuring automated scoring outcomes can be audited and explained",
            "considerations": "LENS relies on clear, deterministic rules mapping transactions to specific intent triggers.",
            "gaps": [],
            "recommendations": [
                "Avoid replacing current transparent rules with black-box deep learning models without an explainability wrapper."
            ]
        },
        "Risk_Assessment": {
            "status": "Compliant",
            "description": "Continuous validation of algorithmic accuracy and bias mitigation",
            "considerations": "Clarity engine computes income deviations, and dynamic fairness reports track segment disparities.",
            "gaps": [
                "No automated process to alert admins if dynamic fairness gaps exceed a warning threshold."
            ],
            "recommendations": [
                "Add an automated daily cron alert evaluating segment conversion rate disparities."
            ]
        }
    }

    gaps = []
    recommendations = []
    for std in standards.values():
        gaps.extend(std["gaps"])
        recommendations.extend(std["recommendations"])

    overall_status = "Attention Required" if any(s["status"] == "Attention Required" for s in standards.values()) else "Compliant"

    return {
        "compliance_status": overall_status,
        "standards": standards,
        "overall_summary": f"System evaluated against current schemas using {db_type}. Identified {len(gaps)} regulatory gaps.",
        "gaps": gaps,
        "recommendations": recommendations,
        "governance_notes": f"Scoring engine runs with {lead_count} leads and {user_count} registered users. Explainability is fully compliant, but consent logs and audit trails require implementation."
    }

def generate_sandbox_mapping() -> Dict[str, Any]:
    """
    Returns a machine-readable schema mapping between LENS internal database fields
    and standardized API Sandbox fields.
    """
    logger.info("Generating sandbox mapping")
    return {
        "schema_version": "1.0.0",
        "validation_notes": "All currency values must be formatted as decimal numbers in INR. Timestamps must conform to ISO-8601 UTC format.",
        "mappings": [
            {
                "internal_field": "customers.customer_id",
                "sandbox_field": "client_ref_id",
                "transformation": "Direct Mapping",
                "status": "mapped",
                "validation_rules": "Required, string (min 3, max 40), alphanumeric"
            },
            {
                "internal_field": "customers.name",
                "sandbox_field": "customer_full_name",
                "transformation": "Direct Mapping",
                "status": "mapped",
                "validation_rules": "Required, string (min 2, max 120), letters and spaces"
            },
            {
                "internal_field": "customers.employment_type",
                "sandbox_field": "employment_classification",
                "transformation": "Direct Mapping",
                "status": "mapped",
                "validation_rules": "Required, enum: [Salaried, Self-Employed, Gig Worker, Freelancer]"
            },
            {
                "internal_field": "customers.declared_income",
                "sandbox_field": "declared_monthly_salary",
                "transformation": "Cast to float",
                "status": "mapped",
                "validation_rules": "Optional, float >= 0.0"
            },
            {
                "internal_field": "transactions.type",
                "sandbox_field": "txn_category",
                "transformation": "Map to sandbox transaction categories (e.g. UPI_CREDIT -> INFLOW_UPI)",
                "status": "mapped",
                "validation_rules": "Required, enum: [UPI_CREDIT, SALARY_CREDIT, UPI_DEBIT, IMPS, NEFT, EMI_DEBIT, BILL_PAY, WALLET_TOPUP]"
            },
            {
                "internal_field": "transactions.amount",
                "sandbox_field": "transaction_value_inr",
                "transformation": "Direct Mapping",
                "status": "mapped",
                "validation_rules": "Required, float > 0.0"
            },
            {
                "internal_field": "leads.intent_score",
                "sandbox_field": "propensity_index",
                "transformation": "Normalize to 0.0 - 1.0 (intent_score / 100.0)",
                "status": "mapped",
                "validation_rules": "Float between 0.0 and 1.0"
            },
            {
                "internal_field": "leads.tier",
                "sandbox_field": "risk_segment",
                "transformation": "Map 'Tier 1' -> 'LOW_RISK', 'Tier 2' -> 'MEDIUM_RISK', 'Tier 3' -> 'HIGH_RISK'",
                "status": "mapped",
                "validation_rules": "Required, enum: [LOW_RISK, MEDIUM_RISK, HIGH_RISK]"
            }
        ],
        "missing_mappings": [
            {
                "internal_field": "customers.persona",
                "sandbox_field": "N/A",
                "reason": "Internal personas (e.g. home_loan_intent) are for generating synthetic test signal and are excluded from outbound sandbox API payloads."
            },
            {
                "internal_field": "leads.match_correct",
                "sandbox_field": "N/A",
                "reason": "This is a ground-truth evaluation flag computed offline and not exposed to operational third-party sandboxes."
            }
        ]
    }

def generate_roi_report(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Dynamically computes LENS business impact, costs, and ROI metrics using real counts
    from the database and segment statistics.
    """
    logger.info("Generating dynamic ROI report")
    conn = db.connect(db_path)
    try:
        customers = db.rows(conn, "SELECT * FROM customers")
        if not customers:
            return {
                "total_customers": 0,
                "total_leads": 0,
                "conversion_rate_pct": 0.0,
                "estimated_revenue": 0.0,
                "estimated_cost": 0.0,
                "net_profit": 0.0,
                "roi_multiplier": 0.0,
                "segment_performance": [],
                "cost_assumptions": {
                    "cost_per_assessment": COST_PER_ASSESSMENT,
                    "cost_per_outreach": COST_PER_OUTREACH,
                    "expected_conversion_rate": EXPECTED_CONVERSION_RATE,
                    "average_loan_amount": AVERAGE_LOAN_AMOUNT,
                    "net_yield_margin": NET_YIELD_MARGIN
                }
            }

        # Count leads dynamically
        leads_count = 0
        segment_customers: Dict[str, List[Dict[str, Any]]] = {}
        for cust in customers:
            emp = cust.get("employment_type", "Unknown") or "Unknown"
            if emp not in segment_customers:
                segment_customers[emp] = []
            segment_customers[emp].append(cust)

            txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp", (cust["customer_id"],))
            score_res = engine.score_customer(cust, txns=txns, conn=conn)
            if score_res and score_res.get("is_lead"):
                leads_count += 1

        total_customers = len(customers)
        global_conversion_rate = round((leads_count / total_customers) * 100.0, 1)

        # Cost & Revenue calculations
        assessment_cost = total_customers * COST_PER_ASSESSMENT
        outreach_cost = leads_count * COST_PER_OUTREACH
        total_cost = assessment_cost + outreach_cost

        # Revenue = leads * probability of closing * profit per loan
        profit_per_loan = AVERAGE_LOAN_AMOUNT * NET_YIELD_MARGIN  # 200,000 * 3% = 6,000 INR
        expected_loans = leads_count * EXPECTED_CONVERSION_RATE
        estimated_revenue = expected_loans * profit_per_loan
        net_profit = estimated_revenue - total_cost
        roi_multiplier = round(estimated_revenue / total_cost, 2) if total_cost > 0.0 else 0.0

        # Segment-specific performance
        segment_perf = []
        for segment_name, custs in segment_customers.items():
            seg_leads = 0
            for cust in custs:
                txns = db.rows(conn, "SELECT * FROM transactions WHERE customer_id=? ORDER BY timestamp", (cust["customer_id"],))
                score_res = engine.score_customer(cust, txns=txns, conn=conn)
                if score_res and score_res.get("is_lead"):
                    seg_leads += 1
            
            seg_total = len(custs)
            seg_rate = round((seg_leads / seg_total) * 100.0, 1) if seg_total > 0 else 0.0
            
            seg_assess_cost = seg_total * COST_PER_ASSESSMENT
            seg_outreach_cost = seg_leads * COST_PER_OUTREACH
            seg_cost = seg_assess_cost + seg_outreach_cost
            
            seg_expected_loans = seg_leads * EXPECTED_CONVERSION_RATE
            seg_rev = seg_expected_loans * profit_per_loan
            seg_roi_pct = round(((seg_rev - seg_cost) / seg_cost) * 100.0, 1) if seg_cost > 0.0 else 0.0

            segment_perf.append({
                "segment_name": segment_name,
                "total_customers": seg_total,
                "total_leads": seg_leads,
                "conversion_rate_pct": seg_rate,
                "estimated_outreach_cost": seg_cost,
                "expected_loans_disbursed": round(seg_expected_loans, 2),
                "expected_revenue": seg_rev,
                "roi_pct": seg_roi_pct
            })

        segment_perf.sort(key=lambda x: x["segment_name"])

        return {
            "total_customers": total_customers,
            "total_leads": leads_count,
            "conversion_rate_pct": global_conversion_rate,
            "estimated_revenue": round(estimated_revenue, 2),
            "estimated_cost": round(total_cost, 2),
            "net_profit": round(net_profit, 2),
            "roi_multiplier": roi_multiplier,
            "segment_performance": segment_perf,
            "cost_assumptions": {
                "cost_per_assessment": COST_PER_ASSESSMENT,
                "cost_per_outreach": COST_PER_OUTREACH,
                "expected_conversion_rate": EXPECTED_CONVERSION_RATE,
                "average_loan_amount": AVERAGE_LOAN_AMOUNT,
                "net_yield_margin": NET_YIELD_MARGIN
            }
        }
    finally:
        conn.close()
