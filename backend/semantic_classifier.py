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
        "Lodha Developers", "Godrej Properties", "DLF Homes", "Prestige Properties", "Brigade Group",
        "real estate builder payment", "property purchase installment", "apartment construction booking", "sub-registrar registry stamp duty"
    ],
    "auto_dealer_payment": [
        "Maruti Suzuki Showroom", "Tata Motors Dealer", "Hyundai Motors Dealer", "Mahindra Auto Showroom", "TVS Motor Showroom",
        "car dealer purchase", "automobile showroom downpayment", "two wheeler dealership installment"
    ],
    "education_fee_payment": [
        "DPS School Fees", "VIT Vellore Fees", "Manipal University admission", "school tuition fee payment", "college semester fees payment",
        "academic course fees"
    ],
    "medical_large_expense": [
        "Apollo Hospital bill", "Fortis Healthcare billing", "hospital treatment cost", "clinical surgery payment",
        "health insurance premium"
    ],
    "wedding_season_spike": [
        "Banquet Hall booking payment", "Wedding Decorator services", "bridal jewellery purchase", "marriage catering services", "wedding ceremony spending"
    ]
}

SIMILARITY_THRESHOLD = 0.42


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
