import numpy as np
import pytest
from backend import semantic_classifier


class FakeSentenceTransformer:
    """Deterministic fake: returns a fixed embedding based on keyword presence,
    so we can control similarity scores precisely in tests without downloading
    the real 80MB model."""
    def encode(self, texts, normalize_embeddings=True):
        out = []
        for t in texts:
            t_lower = t.lower()
            if "lodha" in t_lower or "property" in t_lower or "developer" in t_lower:
                out.append(np.array([1.0, 0.0, 0.0]))
            elif "maruti" in t_lower or "showroom" in t_lower or "dealer" in t_lower:
                out.append(np.array([0.0, 1.0, 0.0]))
            else:
                out.append(np.array([0.0, 0.0, 1.0]))
        return np.array(out)


@pytest.fixture(autouse=True)
def reset_classifier_state():
    """Every test gets a clean slate — _load_model uses module-level globals
    and lru_cache, both of which leak across tests otherwise."""
    semantic_classifier._model = None
    semantic_classifier._model_load_failed = False
    semantic_classifier._anchor_embeddings = {}
    semantic_classifier._classify_counterparty_cached.cache_clear()
    yield
    semantic_classifier._model = None
    semantic_classifier._model_load_failed = False
    semantic_classifier._classify_counterparty_cached.cache_clear()


def test_classifies_property_payment_via_embedding_similarity(monkeypatch):
    fake_model = FakeSentenceTransformer()
    monkeypatch.setattr(semantic_classifier, "_load_model", lambda: fake_model)
    # Pre-populate anchor embeddings the way _load_model normally would
    semantic_classifier._anchor_embeddings = {
        "property_related_payment": np.array([[1.0, 0.0, 0.0]] * 9),
        "auto_dealer_payment": np.array([[0.0, 1.0, 0.0]] * 8),
        "education_fee_payment": np.array([[0.0, 0.0, 1.0]] * 6),
        "medical_large_expense": np.array([[0.0, 0.0, 1.0]] * 5),
        "wedding_season_spike": np.array([[0.0, 0.0, 1.0]] * 5),
    }
    result = semantic_classifier.classify_counterparty("Prestige Lakeside Developers Booking")
    assert result["category"] == "property_related_payment"
    assert result["method"] == "semantic"
    assert result["confidence"] >= semantic_classifier.SIMILARITY_THRESHOLD


def test_below_threshold_returns_none_category(monkeypatch):
    fake_model = FakeSentenceTransformer()
    monkeypatch.setattr(semantic_classifier, "_load_model", lambda: fake_model)
    semantic_classifier._anchor_embeddings = {
        "property_related_payment": np.array([[1.0, 0.0, 0.0]]),
    }
    # "random unrelated text" encodes to [0,0,1] in the fake — orthogonal, similarity ~0
    result = semantic_classifier.classify_counterparty("Random Unrelated Text Payment")
    assert result["category"] is None
    assert result["method"] == "semantic"
    assert result["confidence"] < semantic_classifier.SIMILARITY_THRESHOLD


def test_inference_exception_returns_unavailable(monkeypatch):
    class BrokenModel:
        def encode(self, *a, **kw):
            raise RuntimeError("simulated inference crash")

    monkeypatch.setattr(semantic_classifier, "_load_model", lambda: BrokenModel())
    result = semantic_classifier.classify_counterparty("Anything")
    assert result["category"] is None
    assert result["method"] == "unavailable"


def test_empty_and_none_counterparty_short_circuits_before_model_load(monkeypatch):
    # Should never even call _load_model for empty input
    load_calls = []
    monkeypatch.setattr(semantic_classifier, "_load_model", lambda: load_calls.append(1) or None)
    assert semantic_classifier.classify_counterparty("")["method"] == "unavailable"
    assert semantic_classifier.classify_counterparty(None)["method"] == "unavailable"
    assert load_calls == []


def test_result_is_cached_across_repeated_calls(monkeypatch):
    call_count = {"n": 0}
    fake_model = FakeSentenceTransformer()

    def counting_load():
        call_count["n"] += 1
        return fake_model

    monkeypatch.setattr(semantic_classifier, "_load_model", counting_load)
    semantic_classifier._anchor_embeddings = {
        "property_related_payment": np.array([[1.0, 0.0, 0.0]]),
    }
    semantic_classifier.classify_counterparty("Lodha Developers Payment")
    semantic_classifier.classify_counterparty("Lodha Developers Payment")  # same input, should hit cache
    assert call_count["n"] == 1  # lru_cache means _load_model only invoked once
