import pytest
from backend.capacity import eligible_principal, compute_capacity, normalize_loan_type
from backend.models import CapacityResult
from backend.engine import score_customer

def test_eligible_principal_golden_values():
    # Golden Case 1: EMI = 10000, Rate = 9.0%, Tenure = 180 months -> P = 985934.1
    p1 = eligible_principal(10000, 9.0, 180)
    assert abs(p1 - 985934.1) < 0.1
    
    # Golden Case 2: EMI = 5000, Rate = 12.0%, Tenure = 60 months -> P = 224775.2
    p2 = eligible_principal(5000, 12.0, 60)
    assert abs(p2 - 224775.2) < 0.1

def test_recurring_emi_detection():
    # 90-day window transactions
    transactions = [
        {"timestamp": "2026-04-01T10:00:00+00:00", "type": "EMI_DEBIT", "amount": 10000.0, "counterparty": "HDFC Bank"},
        {"timestamp": "2026-05-01T10:00:00+00:00", "type": "EMI_DEBIT", "amount": 10000.0, "counterparty": "HDFC Bank"},
        {"timestamp": "2026-06-01T10:00:00+00:00", "type": "EMI_DEBIT", "amount": 10000.0, "counterparty": "HDFC Bank"},
        # One-off debit of similar amount
        {"timestamp": "2026-05-15T12:00:00+00:00", "type": "UPI_DEBIT", "amount": 10000.0, "counterparty": "Mall Purchase"},
    ]
    
    result = compute_capacity(
        customer_id="test_cust_1",
        transactions=transactions,
        reconstructed_income=50000.0,
        declared_income=50000.0,
        predicted_loan_type="Personal Loan",
        repay_score=50.0
    )
    
    # HDFC Bank EMI should be detected (monthly cadence and lender keyword)
    # Mall Purchase should be ignored
    assert result.existing_emi_monthly == 10000.0
    assert result.disposable_income == 40000.0  # 50000 - 10000

def test_foir_band_shifting():
    # Personal Loan FOIR band is (0.40, 0.50)
    # High repay score (100) -> FOIR ratio applied should shift to upper bound (0.50)
    res_high = compute_capacity(
        customer_id="test_cust_high",
        transactions=[],
        reconstructed_income=50000.0,
        declared_income=50000.0,
        predicted_loan_type="Personal Loan",
        repay_score=100.0
    )
    assert abs(res_high.foir_ratio_applied - 0.50) < 1e-4
    
    # Low repay score (0) -> FOIR ratio applied should shift to lower bound (0.40)
    res_low = compute_capacity(
        customer_id="test_cust_low",
        transactions=[],
        reconstructed_income=50000.0,
        declared_income=50000.0,
        predicted_loan_type="Personal Loan",
        repay_score=0.0
    )
    assert abs(res_low.foir_ratio_applied - 0.40) < 1e-4

    # Midpoint repay score (50) -> FOIR ratio applied should be midpoint (0.45)
    res_mid = compute_capacity(
        customer_id="test_cust_mid",
        transactions=[],
        reconstructed_income=50000.0,
        declared_income=50000.0,
        predicted_loan_type="Personal Loan",
        repay_score=50.0
    )
    assert abs(res_mid.foir_ratio_applied - 0.45) < 1e-4

def test_dti_ratio_and_over_leveraged():
    # Case 1: normal leverage
    res_normal = compute_capacity(
        customer_id="test_cust_normal",
        transactions=[],
        reconstructed_income=100000.0,
        declared_income=100000.0,
        predicted_loan_type="Personal Loan",
        repay_score=50.0
    )
    assert not res_normal.over_leveraged
    assert res_normal.dti_ratio >= 0.0
    assert res_normal.dti_ratio <= 1.0

    # Case 2: existing EMI exceeds 60% of reconstructed income -> over_leveraged: True
    # Let's create transactions that represent 40,000 EMI on 50,000 reconstructed income (80% leverage)
    transactions = [
        {"timestamp": "2026-04-01T10:00:00Z", "type": "EMI_DEBIT", "amount": 40000.0, "counterparty": "Tata Finance"},
        {"timestamp": "2026-05-01T10:00:00Z", "type": "EMI_DEBIT", "amount": 40000.0, "counterparty": "Tata Finance"},
        {"timestamp": "2026-06-01T10:00:00Z", "type": "EMI_DEBIT", "amount": 40000.0, "counterparty": "Tata Finance"},
    ]
    res_leveraged = compute_capacity(
        customer_id="test_cust_leveraged",
        transactions=transactions,
        reconstructed_income=50000.0,
        declared_income=50000.0,
        predicted_loan_type="Personal Loan",
        repay_score=50.0
    )
    assert res_leveraged.over_leveraged
    assert res_leveraged.disposable_income == 10000.0  # 50000 - 40000
    assert res_leveraged.dti_ratio <= 1.0

def test_full_pipeline_integration():
    customer = {
        "customer_id": "cust_int_1",
        "name": "Integration Test Customer",
        "age": 35,
        "city": "Mumbai",
        "state": "MH",
        "employment_type": "Salaried",
        "declared_income": 80000.0,
        "true_monthly_income": 80000.0,
        "true_loan_type": "Home Loan",
        "persona": "home_loan_intent"
    }
    
    # 3 months of salary inflow + 3 months of housing self RD transfers
    txns = [
        {"timestamp": "2026-04-05T09:00:00Z", "type": "SALARY_CREDIT", "amount": 80000.0, "counterparty": "Employer Payroll"},
        {"timestamp": "2026-05-05T09:00:00Z", "type": "SALARY_CREDIT", "amount": 80000.0, "counterparty": "Employer Payroll"},
        {"timestamp": "2026-06-05T09:00:00Z", "type": "SALARY_CREDIT", "amount": 80000.0, "counterparty": "Employer Payroll"},
        {"timestamp": "2026-04-10T18:00:00Z", "type": "IMPS", "amount": 10000.0, "counterparty": "Self - RD Account"},
        {"timestamp": "2026-05-10T18:00:00Z", "type": "IMPS", "amount": 10000.0, "counterparty": "Self - RD Account"},
        {"timestamp": "2026-06-10T18:00:00Z", "type": "IMPS", "amount": 10000.0, "counterparty": "Self - RD Account"}
    ]
    
    scored = score_customer(customer, txns=txns)
    assert scored is not None
    assert "capacity" in scored
    assert "tier_action_label" in scored
    
    capacity = scored["capacity"]
    assert isinstance(capacity, CapacityResult)
    assert capacity.reconstructed_income == 80000.0
    assert capacity.existing_emi_monthly == 0.0  # RD is self-transfer, not EMI debt obligation
    assert capacity.disposable_income == 80000.0
    assert capacity.recommended_eligible_amount > 0
    assert capacity.recommended_tenure_months in (60, 84, 180, 240)  # depending on MATCH prediction
    assert len(capacity.eligible_amount_by_type) == 4
