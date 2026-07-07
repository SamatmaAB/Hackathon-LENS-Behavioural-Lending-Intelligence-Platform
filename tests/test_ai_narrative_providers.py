import pytest
from unittest.mock import patch, MagicMock
from backend import ai_narrative

SAMPLE_PAYLOAD = {
    "customer": {"customer_id": "CUST1", "name": "Test Customer", "age": 34,
                 "employment_type": "Salaried", "city": "Mumbai"},
    "lead": {"intent_score": 72, "trust_score": 65, "tier": "Tier 1",
             "triggers_fired": [{"code": "property_related_payment", "label": "Property payment"}],
             "predicted_loan_type": "Home Loan", "outreach_channel": "RM Call"},
    "income_breakdown": {"synthetic_monthly_income": 85000},
    "capacity": {"recommended_eligible_amount": 2500000},
}


def _fake_response(content: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def test_nvidia_success_returns_parsed_json(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-nvidia-key")
    payload_json = '{"narrative": "Strong lead.", "outreach_draft": "Hi there", "objections": []}'

    with patch("httpx.post", return_value=_fake_response(payload_json)) as mock_post:
        result = ai_narrative.generate_lead_narrative(SAMPLE_PAYLOAD)

    assert result["narrative"] == "Strong lead."
    assert result["source"] == "nvidia"
    # confirm it actually called the NVIDIA endpoint, not groq
    called_url = mock_post.call_args[0][0]
    assert "integrate.api.nvidia.com" in called_url


def test_nvidia_missing_key_falls_through_to_groq(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    payload_json = '{"narrative": "Groq narrative.", "outreach_draft": "Hi", "objections": []}'

    with patch("httpx.post", return_value=_fake_response(payload_json)) as mock_post:
        result = ai_narrative.generate_lead_narrative(SAMPLE_PAYLOAD)

    assert result["narrative"] == "Groq narrative."
    assert result["source"] == "groq"
    called_url = mock_post.call_args[0][0]
    assert "groq.com" in called_url


def test_both_providers_unavailable_uses_local_fallback(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = ai_narrative.generate_lead_narrative(SAMPLE_PAYLOAD)

    assert result["source"] == "local_fallback"
    assert "Test Customer" in result["narrative"]
    assert "Home Loan" in result["narrative"]
    assert len(result["objections"]) == 3


def test_nvidia_http_error_falls_through_to_groq(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-nvidia-key")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")

    error_resp = MagicMock()
    error_resp.status_code = 500
    error_resp.text = "internal error"
    groq_payload = '{"narrative": "Groq saved it.", "outreach_draft": "Hi", "objections": []}'

    with patch("httpx.post", side_effect=[error_resp, _fake_response(groq_payload)]):
        result = ai_narrative.generate_lead_narrative(SAMPLE_PAYLOAD)

    assert result["source"] == "groq"


def test_local_fallback_handles_missing_optional_fields_gracefully():
    minimal_payload = {"customer": {"customer_id": "CUST2"}, "lead": None,
                        "income_breakdown": None, "capacity": None}
    result = ai_narrative._local_fallback_narrative(minimal_payload, reason="test")
    assert "CUST2" in result["narrative"]
    assert "not available" in result["narrative"]  # _format_money(None) path


def test_parse_model_json_strips_markdown_fences():
    raw = '```json\n{"narrative": "x", "outreach_draft": "y", "objections": []}\n```'
    result = ai_narrative._parse_model_json(raw, source="nvidia")
    assert result["narrative"] == "x"
    assert result["source"] == "nvidia"


def test_parse_model_json_handles_invalid_json_gracefully():
    result = ai_narrative._parse_model_json("not valid json at all", source="nvidia")
    assert "raw output below" in result["narrative"]
    assert result["outreach_draft"] == "not valid json at all"
