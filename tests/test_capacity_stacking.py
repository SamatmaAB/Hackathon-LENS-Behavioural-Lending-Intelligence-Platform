import pytest
from backend import capacity


def _make_capacity_result(disposable_income=50000.0, existing_emi=5000.0):
    return capacity.compute_capacity(
        customer_id="TEST1",
        transactions=[],
        reconstructed_income=disposable_income + existing_emi,
        declared_income=None,
        predicted_loan_type="Personal Loan",
        repay_score=50.0,
    )


def test_stacking_requires_at_least_two_types():
    cap = _make_capacity_result()
    with pytest.raises(ValueError, match="at least 2"):
        capacity.check_loan_stacking(cap, ["Home Loan"])


def test_stacking_rejects_unknown_loan_type():
    cap = _make_capacity_result()
    with pytest.raises(ValueError, match="Unknown loan type"):
        capacity.check_loan_stacking(cap, ["Home Loan", "Yacht Loan"])


def test_low_income_customer_flagged_as_breaching_ceiling():
    cap = _make_capacity_result(disposable_income=8000.0, existing_emi=15000.0)
    result = capacity.check_loan_stacking(cap, ["Home Loan", "Auto Loan", "Personal Loan"])
    assert result["stacking_breaches_60pct_ceiling"] is True
    assert "sequencing" in result["recommendation"]


def test_high_headroom_customer_always_breaches_with_two_maxed_loans():
    cap = _make_capacity_result(disposable_income=200000.0, existing_emi=2000.0)
    result = capacity.check_loan_stacking(cap, ["Home Loan", "Auto Loan"])
    assert result["stacking_breaches_60pct_ceiling"] is True


def test_per_type_breakdown_includes_all_requested_types():
    cap = _make_capacity_result()
    result = capacity.check_loan_stacking(cap, ["Home Loan", "Auto Loan", "Mortgage Loan"])
    types_returned = {b["loan_type"] for b in result["per_type_breakdown"]}
    assert types_returned == {"Home Loan", "Auto Loan", "Mortgage Loan"}
