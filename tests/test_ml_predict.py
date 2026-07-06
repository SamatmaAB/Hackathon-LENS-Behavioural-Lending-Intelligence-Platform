import pytest
from backend import ml_predict


def test_predict_without_model_files_raises(monkeypatch, tmp_path):
    # Force _load_attempted reset and point at an empty models dir
    ml_predict._load_attempted = False
    ml_predict._model = None
    monkeypatch.setattr(ml_predict, "_MODELS_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="ML model not available"):
        ml_predict.predict_loan_type_ml({"age": 30, "employment_type": "Salaried"}, set())


def test_predict_with_trained_model_returns_expected_shape():
    ml_predict._load_attempted = False
    ml_predict._model = None
    result = ml_predict.predict_loan_type_ml(
        {"age": 34, "employment_type": "Salaried"},
        {"property_related_payment", "emi_burden_increase"},
    )
    assert result["predicted_loan_type"] in {
        "Home Loan", "Mortgage", "Auto Loan", "Personal Loan"
    }
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["model"] == "xgboost_v1"
    assert isinstance(result["top_contributing_features"], list)
    assert len(result["top_contributing_features"]) <= 5
    for feat in result["top_contributing_features"]:
        assert "feature" in feat and "shap_value" in feat


def test_predict_handles_unknown_employment_type_gracefully():
    ml_predict._load_attempted = False
    ml_predict._model = None
    # employment_type not seen in training — should not raise
    result = ml_predict.predict_loan_type_ml(
        {"age": 40, "employment_type": "Unknown Category"}, {"auto_dealer_payment"}
    )
    assert "predicted_loan_type" in result
