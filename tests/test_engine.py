"""
Unit tests for backend/engine.py - the PULSE / CLARITY / MATCH / MOMENT / TRUST
pipeline. engine.py auto-detects pytest via IS_TESTING and routes to the
deterministic rule-based fallbacks (no ML/model downloads needed in CI).
"""
import sys
import os
from datetime import datetime, timedelta, UTC

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend import engine


# ---------- PULSE: trigger detection ----------

def _txn(t_type, amount, counterparty, ts):
    return {"type": t_type, "amount": amount, "counterparty": counterparty, "timestamp": ts}


def test_salary_clustering_fires_on_two_stable_salaries():
    now = datetime.now(UTC)
    txns = [
        _txn("SALARY_CREDIT", 50000, "Employer Payroll", (now - timedelta(days=30)).isoformat()),
        _txn("SALARY_CREDIT", 50200, "Employer Payroll", now.isoformat()),
    ]
    fired = engine._detect_triggers(txns)
    assert "salary_inflow_clustering" in fired


def test_salary_clustering_does_not_fire_on_single_salary():
    now = datetime.now(UTC)
    txns = [_txn("SALARY_CREDIT", 50000, "Employer Payroll", now.isoformat())]
    fired = engine._detect_triggers(txns)
    assert "salary_inflow_clustering" not in fired


def test_property_related_payment_fires_for_known_developer():
    now = datetime.now(UTC)
    txns = [_txn("NEFT", 250000, "Lodha Developers", now.isoformat())]
    fired = engine._detect_triggers(txns)
    assert "property_related_payment" in fired


def test_recurring_self_transfer_requires_at_least_two():
    now = datetime.now(UTC)
    txns = [_txn("UPI_DEBIT", 5000, "Self - Savings A/C", now.isoformat())]
    fired = engine._detect_triggers(txns)
    assert "recurring_self_transfer" not in fired

    txns.append(_txn("UPI_DEBIT", 5000, "Self - Savings A/C", (now + timedelta(days=7)).isoformat()))
    fired = engine._detect_triggers(txns)
    assert "recurring_self_transfer" in fired


def test_overdraft_near_miss_is_a_negative_trigger():
    assert engine.TRIGGER_WEIGHTS["overdraft_near_miss"] < 0


def test_empty_transaction_list_fires_nothing():
    fired = engine._detect_triggers([])
    assert fired == {}


# ---------- Intent score ----------

def test_compute_intent_score_matches_formula():
    fired_keys = ["property_related_payment", "salary_inflow_clustering"]  # weights +14, +6
    expected = max(0, min(100, round((14 + 6) * 1.35)))
    assert engine.compute_intent_score(fired_keys) == expected


def test_compute_intent_score_clamped_to_100():
    # Stack enough positive-weight triggers to exceed 100 before clamping
    fired_keys = [k for k, w in engine.TRIGGER_WEIGHTS.items() if w > 0]
    assert engine.compute_intent_score(fired_keys) == 100


def test_compute_intent_score_empty_is_zero():
    assert engine.compute_intent_score([]) == 0


# ---------- CLARITY: income reconstruction ----------

def test_reconstruct_income_salaried_averages_salary_credits():
    now = datetime.now(UTC)
    customer = {"employment_type": "Salaried", "true_monthly_income": 60000}
    txns = [
        _txn("SALARY_CREDIT", 60000, "Employer Payroll", now.isoformat()),
        _txn("SALARY_CREDIT", 60000, "Employer Payroll", (now - timedelta(days=30)).isoformat()),
    ]
    result = engine.reconstruct_income(customer, txns)
    assert result["synthetic_monthly_income"] == 60000.0
    assert result["deviation_pct"] == 0.0


def test_reconstruct_income_gig_worker_falls_back_to_flat_average():
    now = datetime.now(UTC)
    customer = {"employment_type": "Gig Worker", "true_monthly_income": None}
    txns = [_txn("UPI_CREDIT", 500, "Swiggy", now.isoformat()) for _ in range(5)]
    result = engine.reconstruct_income(customer, txns)
    assert result["synthetic_monthly_income"] > 0
    assert result["deviation_pct"] is None  # no ground truth supplied


def test_reconstruct_income_zero_credits_returns_zero():
    customer = {"employment_type": "Gig Worker", "true_monthly_income": None}
    result = engine.reconstruct_income(customer, [])
    assert result["synthetic_monthly_income"] == 0.0
    assert result["method"] == "Insufficient inflow data"


# ---------- MATCH: loan type prediction ----------

def test_mortgage_combo_predicted_when_both_triggers_present():
    pred, conf, source, _ = engine.predict_loan_type(
        list(engine.MORTGAGE_COMBO), customer_id="CUSTTEST01"
    )
    assert pred == "Mortgage"
    assert source == "rule_based"


def test_no_triggers_returns_none_prediction():
    pred, conf, source, _ = engine.predict_loan_type([], customer_id="CUSTTEST02")
    assert pred == "None"
    assert conf == 0.0


def test_predict_loan_type_is_deterministic_for_same_customer_id():
    fired = ["education_fee_payment"]
    r1 = engine.predict_loan_type(fired, customer_id="CUST_STABLE")
    r2 = engine.predict_loan_type(fired, customer_id="CUST_STABLE")
    assert r1 == r2


# ---------- MOMENT: outreach channel ----------

def test_gig_worker_gets_app_notification():
    customer = {"age": 40, "employment_type": "Gig Worker"}
    channel, start, end = engine.determine_outreach(customer, [], datetime.now(UTC))
    assert channel == "App Notification"


def test_property_payment_triggers_rm_call_regardless_of_age():
    customer = {"age": 35, "employment_type": "Salaried"}
    channel, _, _ = engine.determine_outreach(
        customer, ["property_related_payment"], datetime.now(UTC)
    )
    assert channel == "RM Call"


def test_outreach_window_is_72_hours():
    now = datetime.now(UTC)
    customer = {"age": 50, "employment_type": "Salaried"}
    _, start, end = engine.determine_outreach(customer, [], now)
    assert (end - start) == timedelta(hours=72)


# ---------- TRUST: composite scoring ----------

def test_trust_score_formula_and_tier_boundaries():
    income_record = {"deviation_pct": 0.1}  # -> income_confidence = 99.8
    # intent=100, income_conf=100, repay=50 (baseline, no repay triggers)
    trust, tier, income_conf, repay = engine.compute_trust_score(100, income_record, [])
    expected = round(100 * 0.4 + income_conf * 0.3 + 50 * 0.3, 1)
    assert trust == expected
    assert tier in ("Tier 1", "Tier 2", "Tier 3")


def test_credit_card_full_payment_boosts_repay_score():
    income_record = {"deviation_pct": 0}
    _, _, _, repay_without = engine.compute_trust_score(50, income_record, [])
    _, _, _, repay_with = engine.compute_trust_score(
        50, income_record, ["credit_card_full_payment"]
    )
    assert repay_with > repay_without


def test_overdraft_near_miss_lowers_repay_score():
    income_record = {"deviation_pct": 0}
    _, _, _, repay_without = engine.compute_trust_score(50, income_record, [])
    _, _, _, repay_with = engine.compute_trust_score(
        50, income_record, ["overdraft_near_miss"]
    )
    assert repay_with < repay_without


def test_tier_1_requires_trust_score_at_least_70():
    income_record = {"deviation_pct": 0.1}
    trust, tier, _, _ = engine.compute_trust_score(100, income_record, ["credit_card_full_payment"])
    assert trust >= 70
    assert tier == "Tier 1"


def test_tier_3_for_low_scores():
    income_record = {"deviation_pct": 50}
    trust, tier, _, _ = engine.compute_trust_score(0, income_record, ["overdraft_near_miss"])
    assert tier == "Tier 3"


def test_determine_outreach_handles_null_age_and_employment():
    from backend import engine
    from datetime import datetime
    customer = {"customer_id": "X", "age": None, "employment_type": None}
    channel, start, end = engine.determine_outreach(
        customer, fired_keys=[], latest_txn_time=datetime(2026, 1, 1)
    )
    assert channel in ("App Notification", "RM Call", "Branch Visit Prompt")


def test_determine_outreach_handles_null_age_with_property_trigger():
    from backend import engine
    from datetime import datetime
    customer = {"customer_id": "X", "age": None, "employment_type": "Salaried"}
    channel, _, _ = engine.determine_outreach(
        customer, fired_keys=["property_related_payment"], latest_txn_time=datetime(2026, 1, 1)
    )
    assert channel == "RM Call"

import pytest

@pytest.mark.parametrize("missing_field", ["age", "employment_type", "city", "state"])
def test_score_customer_tolerates_missing_field(missing_field):
    from backend import engine
    from datetime import datetime
    sample_customer = {"customer_id": "TEST1", "age": 30, "employment_type": "Salaried", "city": "Pune", "state": "Maharashtra"}
    sample_txns = [{"type": "SALARY_CREDIT", "amount": 50000, "counterparty": "Employer", "timestamp": datetime.now().isoformat()}]
    incomplete = dict(sample_customer)
    incomplete[missing_field] = None
    # Should not raise — either scores normally or returns None/low-confidence result
    result = engine.score_customer(incomplete, txns=sample_txns, conn=None)
    assert result is None or isinstance(result, dict)
