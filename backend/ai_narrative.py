"""
LENS AI Narrative Generation (Feature 1)
=========================================
Calls the NVIDIA NIM API to generate a human-readable narrative,
a draft outreach message, and likely objections + responses for a scored lead.

This sits AFTER the TRUST stage — it does not alter any score.
"""
import os
import json
import httpx
import time

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


def _get_api_key():
    key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("NVIDIA_API_KEY or ANTHROPIC_API_KEY environment variable not set")
    return key


def generate_lead_narrative(lead_payload: dict) -> dict:
    """
    lead_payload: the same dict returned by GET /api/leads/{customer_id}
    (customer, lead, income_breakdown, capacity)

    Returns {narrative, outreach_draft, objections} or raises RuntimeError.
    """
    api_key = _get_api_key()

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

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    body = {
        "model": "meta/llama-3.3-70b-instruct",
        "messages": [
            {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2,
        "max_tokens": 1024
    }

    # Robust retry-on-timeout loop
    last_err = None
    for attempt in range(3):
        try:
            response = httpx.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=120.0
            )
            if response.status_code == 200:
                res_data = response.json()
                raw_text = res_data["choices"][0]["message"]["content"]
                break
            else:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        except (httpx.HTTPError, RuntimeError) as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"NVIDIA API request failed after 3 attempts: {last_err}")

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
