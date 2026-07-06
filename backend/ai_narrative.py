"""
LENS AI Narrative Generation (Feature 1)
=========================================
Calls the Anthropic Claude API to generate a human-readable narrative,
a draft outreach message, and likely objections + responses for a scored lead.

This sits AFTER the TRUST stage — it does not alter any score.
"""
import os
import json

# Lazy import so the module loads even if anthropic is not installed
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


NARRATIVE_SYSTEM_PROMPT = """\
You are an assistant embedded in LENS, a bank lending-intelligence platform used by IDBI Bank \
Relationship Managers (RMs). You are given a fully-computed lead record (all scores already \
calculated by a deterministic rule engine — you do not compute or alter any score).

Your job is to translate the structured lead data into three things, and return STRICT JSON only \
(no markdown fences, no preamble):

{
  "narrative": "<3-4 sentence plain-English explanation of why this customer is a good lead, \
referencing the specific triggers and amounts>",
  "outreach_draft": "<a ready-to-send message in the tone appropriate for the outreach_channel field \
(App Notification = short and casual, RM Call = a phone script with opening line, Branch Visit Prompt \
= a formal invitation)>",
  "objections": [
    {"question": "<a likely customer question or pushback>", "response": "<a suggested RM response, \
grounded in the actual capacity/FOIR numbers provided>"}
  ]
}

Rules:
- Never invent numbers. Only reference figures present in the input JSON.
- Keep the narrative factual and audit-friendly — an RM should be able to defend every sentence to a compliance reviewer.
- objections should contain exactly 3 items.
- Do not use the customer's full data beyond what's given; do not speculate about protected characteristics.
"""


def generate_lead_narrative(lead_payload: dict) -> dict:
    """
    lead_payload: the same dict returned by GET /api/leads/{customer_id}
    (customer, lead, income_breakdown, capacity)

    Returns {narrative, outreach_draft, objections} or raises RuntimeError.
    """
    client = _get_client()

    # Trim payload for token efficiency — keep essential fields only
    slim_payload = {
        "customer": {k: lead_payload.get("customer", {}).get(k) for k in
                     ("customer_id", "name", "age", "employment_type", "city")},
        "lead": {k: lead_payload.get("lead", {}).get(k) for k in
                 ("intent_score", "trust_score", "tier", "triggers_fired",
                  "predicted_loan_type", "outreach_channel")} if lead_payload.get("lead") else None,
        "income_breakdown": lead_payload.get("income_breakdown"),
        "capacity": lead_payload.get("capacity"),
    }

    user_content = json.dumps(slim_payload, default=str)

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1200,
        system=NARRATIVE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    raw_text = "".join(
        block.text for block in response.content if block.type == "text"
    )

    # Strip accidental code fences defensively
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove opening fence (```json or ```) and closing fence
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = {
            "narrative": "Narrative generation succeeded but response parsing failed — raw output below.",
            "outreach_draft": raw_text,
            "objections": [],
        }
    return parsed
