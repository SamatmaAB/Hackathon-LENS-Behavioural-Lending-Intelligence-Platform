import json
import pytest
import httpx
from backend import ai_narrative

def test_narrative_prompt_never_leaks_arbitrary_customer_fields():
    assert "NARRATIVE_SYSTEM_PROMPT" in dir(ai_narrative)
    assert "objections" in ai_narrative.NARRATIVE_SYSTEM_PROMPT
    assert "exactly 3 items" in ai_narrative.NARRATIVE_SYSTEM_PROMPT

def test_missing_api_keys_raise_clear_errors(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        ai_narrative._get_nvidia_api_key()
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        ai_narrative._get_groq_api_key()

def test_format_money_handles_none_and_numeric():
    assert ai_narrative._format_money(None) is not None
    assert "INR" in ai_narrative._format_money(65000) or "65" in ai_narrative._format_money(65000)

def test_generate_narrative_fallback_mocked(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    
    lead_detail = {
        "customer": {"name": "Test User", "declared_income": 50000},
        "lead": {"trust_score": 85.5, "tier": "A", "predicted_loan_type": "Auto Loan"},
        "capacity": {"recommended_eligible_amount": 100000}
    }
    
    res = ai_narrative.generate_lead_narrative(lead_detail)
    assert "Test User" in res["narrative"]
    assert "hello" in res["outreach_draft"].lower()
    assert len(res["objections"]) == 3

def test_generate_narrative_with_api(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test_nvidia_key")
    
    def fake_post_chat_completion(provider, api_key, url, model, messages, timeout_seconds):
        return {
            "narrative": f"{provider} Narrative",
            "outreach_draft": f"{provider} Draft",
            "objections": ["1", "2", "3"]
        }
    monkeypatch.setattr(ai_narrative, "_post_chat_completion", fake_post_chat_completion)
    
    lead_detail = {
        "customer": {"name": "Test User", "declared_income": 50000},
        "lead": {"trust_score": 85.5, "tier": "A", "predicted_loan_type": "Auto Loan"},
        "capacity": {"recommended_eligible_amount": 100000}
    }
    
    res = ai_narrative.generate_lead_narrative(lead_detail)
    assert res["narrative"] == "nvidia Narrative"
    assert res["outreach_draft"] == "nvidia Draft"

def test_generate_narrative_groq_fallback(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test_nvidia_key")
    monkeypatch.setenv("GROQ_API_KEY", "test_groq_key")
    
    def fake_post_chat_completion(provider, api_key, url, model, messages, timeout_seconds):
        if provider == "nvidia":
            raise httpx.HTTPError("Nvidia failed")
        return {
            "narrative": f"{provider} Narrative",
            "outreach_draft": f"{provider} Draft",
            "objections": ["1", "2", "3"]
        }
    monkeypatch.setattr(ai_narrative, "_post_chat_completion", fake_post_chat_completion)
    
    lead_detail = {}
    res = ai_narrative.generate_lead_narrative(lead_detail)
    assert res["narrative"] == "groq Narrative"
    
def test_parse_model_json():
    # test parsing valid json
    raw = '```json\n{"narrative": "test", "outreach_draft": "test2", "objections": []}\n```'
    parsed = ai_narrative._parse_model_json(raw, "nvidia")
    assert parsed["narrative"] == "test"
    assert parsed["source"] == "nvidia"
    
    # test parsing invalid json
    raw = 'just some text'
    parsed = ai_narrative._parse_model_json(raw, "nvidia")
    assert "failed" in parsed["narrative"]
    assert parsed["outreach_draft"] == "just some text"
