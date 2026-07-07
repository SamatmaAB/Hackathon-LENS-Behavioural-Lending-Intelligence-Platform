import json
import pytest
from unittest.mock import patch, MagicMock
from backend import ai_query


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY or ANTHROPIC_API_KEY"):
        ai_query._get_api_key()


def test_direct_answer_without_tool_calls(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "There are 42 Tier 1 leads.", "tool_calls": None}}]
    }

    with patch("httpx.post", return_value=mock_response):
        result = ai_query.run_governance_query("How many Tier 1 leads?", tool_executor=lambda n, a: {})
    assert "42" in result


def test_tool_call_loop_executes_and_returns_final_answer(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key")

    tool_call_response = MagicMock()
    tool_call_response.status_code = 200
    tool_call_response.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_1", "function": {"name": "get_leads", "arguments": "{}"}}],
        }}]
    }
    final_response = MagicMock()
    final_response.status_code = 200
    final_response.json.return_value = {
        "choices": [{"message": {"content": "Based on the data, conversion is strong.", "tool_calls": None}}]
    }

    executor_calls = []
    def fake_executor(name, args):
        executor_calls.append(name)
        return {"leads": []}

    with patch("httpx.post", side_effect=[tool_call_response, final_response]):
        result = ai_query.run_governance_query("Summarize leads", tool_executor=fake_executor)

    assert executor_calls == ["get_leads"]
    assert "conversion" in result


def test_retries_on_http_error_then_succeeds(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key")

    error_response = MagicMock()
    error_response.status_code = 500
    error_response.text = "server error"
    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.json.return_value = {"choices": [{"message": {"content": "ok", "tool_calls": None}}]}

    with patch("httpx.post", side_effect=[error_response, ok_response]), patch("time.sleep"):
        result = ai_query.run_governance_query("test", tool_executor=lambda n, a: {})
    assert result == "ok"


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key")
    error_response = MagicMock()
    error_response.status_code = 500
    error_response.text = "server error"

    with patch("httpx.post", return_value=error_response), patch("time.sleep"):
        with pytest.raises(RuntimeError, match="API query failed"):
            ai_query.run_governance_query("test", tool_executor=lambda n, a: {})
