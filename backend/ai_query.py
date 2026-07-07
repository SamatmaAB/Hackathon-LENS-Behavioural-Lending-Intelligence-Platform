"""
LENS AI Governance Query (Feature 6)
======================================
Provides a natural-language query interface over existing governance data.
Uses Llama 3.3 hosted on NVIDIA NIM with OpenAI-compatible tool-calling to translate
English questions into calls against existing /api/governance/* handlers,
then answers conversationally.

No new scoring logic — purely a UX layer over data already exposed.
"""
import os
import json
import httpx
import time

NVIDIA_GOVERNANCE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_leads",
            "description": "Fetch the ranked lead list, optionally filtered by tier or search term.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tier":   {"type": "string", "enum": ["Tier 1", "Tier 2", "Tier 3"]},
                    "search": {"type": "string"},
                },
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_fairness_report",
            "description": "Fetch the fairness/bias audit across employment-type segments.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_roi_report",
            "description": "Fetch the ROI/business-value estimate for the current lead pipeline.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_anomaly_report",
            "description": "Fetch leads flagged as anomalous by the SENTRY fraud detector.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

SYSTEM_PROMPT = (
    "You are a banking-governance analyst assistant embedded in LENS, the IDBI Bank lending intelligence platform. "
    "Use the provided tools to fetch real, live data before answering. "
    "Never fabricate numbers — every figure you cite must come from a tool response. "
    "Be concise (2-4 sentences typically). Cite key metrics explicitly."
)


def _get_api_key():
    key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("NVIDIA_API_KEY or ANTHROPIC_API_KEY environment variable not set")
    return key


def _post_with_retry(headers, json_body, timeout=120.0, retries=3, skip_nvidia=False):
    last_err = None
    # Try NVIDIA
    if not skip_nvidia:
        for attempt in range(retries):
            try:
                response = httpx.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers=headers,
                    json=json_body,
                    timeout=httpx.Timeout(timeout, connect=3.0)
                )
                if response.status_code == 200:
                    return response, "nvidia"
                elif response.status_code in (401, 403):
                    # Don't retry auth errors
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
                else:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
            except (httpx.HTTPError, RuntimeError) as e:
                last_err = e
                # Only retry if it's not an auth error
                if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    break
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                
    # Fallback to GROQ if NVIDIA fails or is skipped
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        groq_headers = {
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json"
        }
        groq_body = json_body.copy()
        groq_body["model"] = "llama-3.1-8b-instant"
        
        try:
            response = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=groq_headers,
                json=groq_body,
                timeout=httpx.Timeout(timeout, connect=3.0)
            )
            if response.status_code == 200:
                return response, "groq"
            else:
                last_err = RuntimeError(f"Groq HTTP {response.status_code}: {response.text}")
        except Exception as e:
            last_err = e

    raise RuntimeError(f"API query failed after {retries} attempts: {last_err}")


def run_governance_query(question: str, tool_executor) -> str:
    """
    question: plain-English governance question from an admin/analyst.
    tool_executor: callable(tool_name: str, tool_input: dict) -> dict

    Returns a conversational answer string.
    """
    api_key = _get_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question}
    ]

    active_provider = None
    # Agentic loop — at most 5 tool rounds to prevent runaway
    for _ in range(5):
        body = {
            "model": "meta/llama-3.3-70b-instruct",
            "messages": messages,
            "tools": NVIDIA_GOVERNANCE_TOOLS,
            "temperature": 0.2,
            "max_tokens": 1024
        }
        
        skip_nvidia = (active_provider == "groq")
        response, provider = _post_with_retry(headers, body, timeout=15.0, skip_nvidia=skip_nvidia)
        active_provider = provider
        
        res_data = response.json()
        message = res_data["choices"][0]["message"]

        # Check if model wants to call tools
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # No tool calls, return conversational content
            return message.get("content", "")

        # Append assistant response (which contains tool calls) to message history
        messages.append(message)

        # Process each tool call
        for tc in tool_calls:
            tc_id = tc["id"]
            func = tc["function"]
            name = func["name"]
            try:
                args = json.loads(func["arguments"])
            except Exception:
                args = {}

            # Execute tool
            try:
                result = tool_executor(name, args)
            except Exception as e:
                result = {"error": str(e)}

            # Append tool result to messages
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": json.dumps(result, default=str)
            })

    # If loop ends, get final response text
    try:
        body = {
            "model": "meta/llama-3.3-70b-instruct",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1024
        }
        response = _post_with_retry(headers, body, timeout=60.0)
        return response.json()["choices"][0]["message"].get("content", "")
    except Exception:
        pass

    return "Failed to complete governance query loop."
