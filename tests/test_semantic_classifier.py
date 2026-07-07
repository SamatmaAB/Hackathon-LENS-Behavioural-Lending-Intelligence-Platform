import pytest
from backend import semantic_classifier


def test_classify_counterparty_empty_string():
    assert semantic_classifier.classify_counterparty("") == {
        "category": None, "confidence": 0.0, "method": "unavailable"
    }


def test_classify_falls_back_to_unavailable_when_model_missing(monkeypatch):
    monkeypatch.setattr(semantic_classifier, "_load_model", lambda: None)
    result = semantic_classifier.classify_counterparty("Lodha Developers Booking Payment")
    assert result["category"] is None
    assert result["method"] == "unavailable"


def test_classify_counterparty_successful_match(monkeypatch):
    # Mock the cached function to simulate a successful ML classification without needing sentence-transformers
    monkeypatch.setattr(semantic_classifier, "_classify_counterparty_cached", 
                        lambda name: ("Property", 0.95, "semantic"))
    result = semantic_classifier.classify_counterparty("DLF Phase 4 Installment")
    assert result["category"] == "Property"
    assert result["confidence"] == 0.95
    assert result["method"] == "semantic"
