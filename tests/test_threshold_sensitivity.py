from backend import threshold_sensitivity


class FakeDB:
    @staticmethod
    def rows(conn, query):
        return conn


def test_empty_dataset_returns_empty_curve():
    result = threshold_sensitivity.compute_threshold_curve(conn=[], db=FakeDB)
    assert result["curve"] == []
    assert result["total_customers"] == 0


def test_curve_is_monotonic_non_increasing_in_volume():
    # Higher threshold should never produce MORE leads than a lower one
    rows = [
        {"customer_id": f"C{i}", "trust_score": score, "true_loan_type": "Personal Loan" if score > 60 else "None"}
        for i, score in enumerate([20, 35, 45, 55, 65, 75, 85, 95])
    ]
    result = threshold_sensitivity.compute_threshold_curve(conn=rows, db=FakeDB)
    volumes = [c["lead_volume"] for c in result["curve"]]
    assert all(volumes[i] >= volumes[i + 1] for i in range(len(volumes) - 1))


def test_conversion_rate_increases_at_higher_thresholds_when_quality_correlates_with_score():
    # Genuine prospects clustered at high trust scores, noise at low scores
    rows = (
        [{"customer_id": f"G{i}", "trust_score": 80, "true_loan_type": "Home Loan"} for i in range(10)] +
        [{"customer_id": f"N{i}", "trust_score": 35, "true_loan_type": "None"} for i in range(10)]
    )
    result = threshold_sensitivity.compute_threshold_curve(conn=rows, db=FakeDB, thresholds=[30, 70])
    low_thresh = next(c for c in result["curve"] if c["trust_score_threshold"] == 30)
    high_thresh = next(c for c in result["curve"] if c["trust_score_threshold"] == 70)
    assert high_thresh["conversion_rate_pct"] > low_thresh["conversion_rate_pct"]


def test_recommended_threshold_finds_first_above_30pct():
    rows = (
        [{"customer_id": f"G{i}", "trust_score": 80, "true_loan_type": "Home Loan"} for i in range(5)] +
        [{"customer_id": f"N{i}", "trust_score": 80, "true_loan_type": "None"} for i in range(20)]
    )
    result = threshold_sensitivity.compute_threshold_curve(conn=rows, db=FakeDB, thresholds=[30, 50, 70])
    # At trust_score=80 for everyone, conversion is fixed (5/25=20%) regardless of threshold —
    # so recommended should be None here since it never clears 30%
    assert result["recommended_threshold_for_30pct_target"] is None
