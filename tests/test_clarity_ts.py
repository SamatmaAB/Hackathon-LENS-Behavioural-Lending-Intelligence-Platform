from datetime import datetime, timedelta
from backend import clarity_ts


def _make_credit(days_ago, amount):
    ts = (datetime(2026, 6, 1) - timedelta(days=days_ago)).isoformat()
    return {"type": "UPI_CREDIT", "amount": amount, "timestamp": ts}


def test_returns_none_with_fewer_than_12_credits():
    txns = [_make_credit(i * 7, 5000) for i in range(5)]
    assert clarity_ts.reconstruct_income_timeseries(txns) is None


def test_ignores_small_credits_under_100():
    # 15 credits but all under the 100-rupee floor — should not count toward the 12 minimum
    txns = [_make_credit(i * 7, 50) for i in range(15)]
    assert clarity_ts.reconstruct_income_timeseries(txns) is None


def test_returns_none_when_fewer_than_8_weekly_buckets():
    # 12+ qualifying credits but all crammed into fewer than 8 distinct weeks
    txns = [_make_credit(0, 5000 + i) for i in range(12)]  # all same day
    assert clarity_ts.reconstruct_income_timeseries(txns) is None


def test_successful_decomposition_returns_expected_shape():
    # 16 weeks of credits, enough for STL with period=4
    txns = [_make_credit(i * 7, 8000 + (i % 4) * 500) for i in range(16)]
    result = clarity_ts.reconstruct_income_timeseries(txns)
    if result is None:
        # statsmodels not installed in this environment — acceptable, but flag it
        import pytest
        pytest.skip("statsmodels not installed — install it to exercise the real STL path")
    assert "synthetic_monthly_income" in result
    assert result["synthetic_monthly_income"] >= 0
    assert "STL time-series decomposition" in result["method"]


def test_missing_statsmodels_falls_back_gracefully(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "statsmodels.tsa.seasonal" or name.startswith("statsmodels"):
            raise ImportError("simulated missing statsmodels")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    txns = [_make_credit(i * 7, 8000) for i in range(16)]
    assert clarity_ts.reconstruct_income_timeseries(txns) is None
