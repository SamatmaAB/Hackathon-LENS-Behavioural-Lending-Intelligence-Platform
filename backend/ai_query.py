"""
LENS AI Governance Query (Feature 6)
======================================
Provides a natural-language query interface over existing governance data.
Uses Claude with tool-calling to translate English questions into calls against
existing /api/governance/* handlers, then answers conversationally.

No new scoring logic — purely a UX layer over data already exposed.
"""
import os
import json

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            from anthropic import Anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")
            _client = Anthropic(api_key=api_key)
        except ImportError:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic==0.39.0")
    return _client


GOVERNANCE_TOOLS = [
    {
        "name": "get_leads",
        "description": "Fetch the ranked lead list, optionally filtered by tier or search term.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tier":   {"type": "string", "enum": ["Tier 1", "Tier 2", "Tier 3"]},
                "search": {"type": "string"},
            },
        },
    },
    {
        "name": "get_fairness_report",
        "description": "Fetch the fairness/bias audit across employment-type segments.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_roi_report",
        "description": "Fetch the ROI/business-value estimate for the current lead pipeline.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_anomaly_report",
        "description": "Fetch leads flagged as anomalous by the SENTRY fraud detector.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

SYSTEM_PROMPT = (
    "You are a banking-governance analyst assistant embedded in LENS, the IDBI Bank lending intelligence platform. "
    "Use the provided tools to fetch real, live data before answering. "
    "Never fabricate numbers — every figure you cite must come from a tool response. "
    "Be concise (2-4 sentences typically). Cite key metrics explicitly."
)


def run_governance_query(question: str, tool_executor) -> str:
    """
    question: plain-English governance question from an admin/analyst.
    tool_executor: callable(tool_name: str, tool_input: dict) -> dict

    Returns a conversational answer string.
    """
    client = _get_client()
    messages = [{"role": "user", "content": question}]

    # Agentic loop — at most 5 tool rounds to prevent runaway
    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            tools=GOVERNANCE_TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = tool_executor(block.name, block.input)
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "".join(b.text for b in response.content if b.type == "text")
