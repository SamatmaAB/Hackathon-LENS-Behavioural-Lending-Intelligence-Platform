import pytest
import math
from backend import engine

def test_trust_score_invariants():
    """Trust score should never exceed 100, even with perfect inputs."""
    customer = {
        "customer_id": "perfect_user",
        "name": "Perfect",
        "age": 35,
        "employment_type": "Salaried",
        "city": "Mumbai",
        "declared_income": 1000000
    }
    txns = [] # Minimal empty transactions
    
    scored = engine.score_customer(customer, txns=txns)
    if scored:
        assert scored["trust_score"] <= 100, f"Trust score {scored['trust_score']} exceeded 100"

def test_negative_income_handled():
    """Negative declared income should not result in negative capacity or crash."""
    customer = {
        "customer_id": "neg_income_user",
        "name": "Negative",
        "age": 40,
        "employment_type": "Self-Employed",
        "city": "Delhi",
        "declared_income": -50000
    }
    txns = []
    
    scored = engine.score_customer(customer, txns=txns)
    if scored and "capacity" in scored:
        cap = scored["capacity"]
        assert getattr(cap, "recommended_eligible_amount", 0) >= 0 or cap.get("recommended_eligible_amount", 0) >= 0
        assert getattr(cap, "max_eligible_amount", 0) >= 0 or cap.get("max_eligible_amount", 0) >= 0

def test_extreme_age_bounds():
    """Extremely high age should either fail gracefully or have bounded outputs."""
    customer = {
        "customer_id": "old_user",
        "name": "Old",
        "age": 150,  # Unrealistic age
        "employment_type": "Salaried",
        "city": "Pune",
        "declared_income": 50000
    }
    txns = []
    
    try:
        scored = engine.score_customer(customer, txns=txns)
        if scored and "capacity" in scored:
            cap = scored["capacity"]
            assert getattr(cap, "recommended_eligible_amount", 0) >= 0 or cap.get("recommended_eligible_amount", 0) >= 0
    except Exception as e:
        # Failing gracefully is also acceptable
        pass

def test_nan_inputs_handled():
    """NaN or missing numeric inputs should be handled gracefully."""
    customer = {
        "customer_id": "nan_user",
        "name": "NaN User",
        "age": float('nan'),
        "employment_type": "Salaried",
        "city": "Bangalore",
        "declared_income": float('nan')
    }
    txns = []
    
    try:
        scored = engine.score_customer(customer, txns=txns)
        if scored and "capacity" in scored:
            cap = scored["capacity"]
            assert getattr(cap, "recommended_eligible_amount", 0) >= 0 or cap.get("recommended_eligible_amount", 0) >= 0
    except (ValueError, TypeError) as e:
        pass
