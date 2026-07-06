from backend import sentry


def _make_txns(n, amount=1000.0, counterparty="Test Payee"):
    return [
        {"amount": amount + i, "counterparty": counterparty, "timestamp": f"2026-01-{(i % 28) + 1:02d}T10:00:00"}
        for i in range(n)
    ]


def test_extract_features_on_empty_txns_returns_zero_vector():
    feats = sentry._extract_txn_features([])
    assert feats.shape == (7,)
    assert feats.sum() == 0


def test_small_population_returns_neutral_scores():
    # Fewer than 10 customers — IsolationForest is skipped by design
    data = {f"CUST{i}": _make_txns(5) for i in range(5)}
    result = sentry.compute_fraud_risk_scores(data)
    assert all(v["fraud_risk_score"] == 0.0 and v["is_anomalous"] is False for v in result.values())


def test_large_population_flags_at_least_one_outlier():
    normal = {f"CUST{i}": _make_txns(20, amount=1000.0) for i in range(20)}
    # inject one wildly anomalous customer: huge amounts, many unique counterparties, night-time heavy
    anomalous_txns = [
        {"amount": 500000.0 + i * 1000, "counterparty": f"Payee{i}", "timestamp": f"2026-01-{(i % 28)+1:02d}T03:00:00"}
        for i in range(20)
    ]
    normal["CUST_ANOMALY"] = anomalous_txns
    result = sentry.compute_fraud_risk_scores(normal)
    assert result["CUST_ANOMALY"]["fraud_risk_score"] > 50
    assert any(v["is_anomalous"] for v in result.values())


def test_missing_sklearn_falls_back_to_neutral(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError("simulated missing sklearn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    data = {f"CUST{i}": _make_txns(15) for i in range(15)}
    result = sentry.compute_fraud_risk_scores(data)
    assert all(v["fraud_risk_score"] == 0.0 for v in result.values())
