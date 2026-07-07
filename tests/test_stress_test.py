from backend import capacity


def test_shock_reduces_eligible_amount():
    result = capacity.stress_test_income_shock(
        customer_id="T1", transactions=[], reconstructed_income=100000.0,
        declared_income=None, predicted_loan_type="Personal Loan", repay_score=50.0,
    )
    assert result["shocked_recommended_eligible_amount"] < result["baseline_recommended_eligible_amount"]
    assert result["eligible_amount_drop_pct"] > 0


def test_zero_shock_produces_identical_results():
    result = capacity.stress_test_income_shock(
        customer_id="T1", transactions=[], reconstructed_income=100000.0,
        declared_income=None, predicted_loan_type="Personal Loan", repay_score=50.0,
        shock_pct=0.0,
    )
    assert result["eligible_amount_drop_pct"] == 0.0
    assert result["newly_over_leveraged_under_shock"] is False


def test_severe_shock_flags_newly_over_leveraged():
    # High existing EMI relative to income — a shock should tip it over
    heavy_emi_txns = [
        {"type": "EMI_DEBIT", "counterparty": "HDFC Bank Loan", "amount": 25000.0,
         "timestamp": f"2026-0{m}-05T10:00:00"} for m in range(1, 5)
    ]
    result = capacity.stress_test_income_shock(
        customer_id="T2", transactions=heavy_emi_txns, reconstructed_income=45000.0,
        declared_income=None, predicted_loan_type="Personal Loan", repay_score=50.0,
        shock_pct=0.30,
    )
    assert result["shocked_over_leveraged"] is True
