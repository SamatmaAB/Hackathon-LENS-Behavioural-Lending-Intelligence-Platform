"""
LENS Semantic Counterparty Classifier (Feature 2)
===================================================
Uses a local sentence-transformer model (all-MiniLM-L6-v2, ~80 MB) to classify
counterparty names into loan-intent categories via embedding similarity.

Falls back to keyword matching if:
  - The model is not loaded / import fails
  - No category clears the similarity threshold

This gives the engine genuine generalisation beyond the synthetic vocabulary
while keeping keyword matching as the safety-net fallback for auditability.
"""
import numpy as np

_model = None
_anchor_embeddings = {}

CATEGORY_ANCHORS = {
    "property_related_payment": [
        "real estate developer payment", "property purchase installment",
        "sub-registrar office stamp duty", "home construction payment",
        "builder payment for apartment",
    ],
    "auto_dealer_payment": [
        "car dealership payment", "automobile showroom purchase",
        "two wheeler dealer payment", "vehicle down payment",
    ],
    "education_fee_payment": [
        "school tuition fee payment", "university fee payment",
        "college admission fee", "online course payment",
    ],
    "medical_large_expense": [
        "hospital bill payment", "health insurance premium",
        "medical treatment expense", "diagnostic center payment",
    ],
    "wedding_season_spike": [
        "banquet hall booking payment", "wedding caterer payment",
        "jewellery purchase", "wedding decorator payment",
    ],
}

SIMILARITY_THRESHOLD = 0.45


def _load_model():
    """Lazy-load the sentence transformer. Returns None on failure."""
    global _model, _anchor_embeddings
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        _anchor_embeddings = {
            category: _model.encode(phrases, normalize_embeddings=True)
            for category, phrases in CATEGORY_ANCHORS.items()
        }
        return _model
    except Exception as e:
        print(f"[SENTRY] Could not load sentence-transformer model: {e}. Keyword fallback will be used.")
        _model = None
        return None


def classify_counterparty(counterparty_name: str) -> dict:
    """
    Returns {"category": str|None, "confidence": float, "method": "semantic"|"unavailable"}
    Falls back to None if nothing clears the threshold or model is unavailable.
    """
    if not counterparty_name or not counterparty_name.strip():
        return {"category": None, "confidence": 0.0, "method": "unavailable"}

    model = _load_model()
    if model is None:
        return {"category": None, "confidence": 0.0, "method": "unavailable"}

    try:
        query_embedding = model.encode([counterparty_name], normalize_embeddings=True)[0]

        best_category, best_score = None, 0.0
        for category, anchor_matrix in _anchor_embeddings.items():
            sims = anchor_matrix @ query_embedding  # cosine (already normalised)
            max_sim = float(np.max(sims))
            if max_sim > best_score:
                best_score = max_sim
                best_category = category

        if best_score >= SIMILARITY_THRESHOLD:
            return {"category": best_category, "confidence": round(best_score, 3), "method": "semantic"}

        return {"category": None, "confidence": round(best_score, 3), "method": "semantic"}

    except Exception as e:
        print(f"[SemanticClassifier] Inference error for '{counterparty_name}': {e}")
        return {"category": None, "confidence": 0.0, "method": "unavailable"}
