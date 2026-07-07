import pytest
from backend import threshold_sensitivity

class FakeDB:
    @staticmethod
    def rows(conn, query):
        return conn

def test_empty_dataset_returns_empty_curve():
    result = threshold_sensitivity.compute_threshold_curve(conn=[], db=FakeDB)
    assert result['curve'] == []
    assert result['total_leads_evaluated'] == 0
    assert result['recommended_threshold_for_30pct_target'] is None

def test_empty_and_populated_results_have_identical_key_sets():
    empty_result = threshold_sensitivity.compute_threshold_curve(conn=[], db=FakeDB)
    populated_rows = [
        {'customer_id': 'C1', 'trust_score': 80, 'true_loan_type': 'Home Loan'},
    ]
    populated_result = threshold_sensitivity.compute_threshold_curve(conn=populated_rows, db=FakeDB)
    assert set(empty_result.keys()) == set(populated_result.keys()), (
        'Empty and populated responses must have the same shape — '
        'a caller shouldn\'t need a branch to handle both cases safely.'
    )