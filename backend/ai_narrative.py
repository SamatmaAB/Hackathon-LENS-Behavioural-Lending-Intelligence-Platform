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

NVIDIA_TIMEOUT_SECONDS = float(os.environ.get("NVIDIA_TIMEOUT_SECONDS", "3"))
GROQ_TIMEOUT_SECONDS = float(os.environ.get("GROQ_TIMEOUT_SECONDS", "30"))

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


def _get_nvidia_api_key():
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        raise RuntimeError("NVIDIA_API_KEY environment variable not set")
    return key


def _get_groq_api_key():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY environment variable not set")
    return key


def _format_money(value):
    if value is None:
        return "not available"
    try:
        return f"INR {float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _value(obj, *keys):
    for key in keys:
        if isinstance(obj, dict) and obj.get(key) is not None:
            return obj.get(key)
        if hasattr(obj, key):
            value = getattr(obj, key)
            if value is not None:
                return value
    return None


def _local_fallback_narrative(lead_payload: dict, reason: str = "") -> dict:
    """Create a deterministic briefing when the external NIM API is unavailable."""
    customer = lead_payload.get("customer", {})
    lead = lead_payload.get("lead") or {}
    income = lead_payload.get("income_breakdown") or {}
    capacity = lead_payload.get("capacity") or lead.get("capacity") or {}

    name = _value(customer, "name") or _value(customer, "customer_id") or "This customer"
    tier = _value(lead, "tier") or "Unclassified"
    predicted_loan = _value(lead, "predicted_loan_type") or "loan"
    intent = _value(lead, "intent_score") or "not available"
    trust = _value(lead, "trust_score") or "not available"
    channel = _value(lead, "outreach_channel") or "RM follow-up"
    synthetic_income = _value(income, "synthetic_monthly_income") or _value(lead, "synthetic_income")
    eligible_amount = (
        _value(capacity, "recommended_eligible_amount")
        or _value(capacity, "eligible_amount")
        or _value(capacity, "max_eligible_amount")
        or _value(capacity, "loan_capacity")
    )

    trigger_items = _value(lead, "triggers_fired") or []
    trigger_labels = []
    for item in trigger_items:
        if isinstance(item, dict):
            trigger_labels.append(item.get("label") or item.get("code"))
        else:
            trigger_labels.append(str(item))
    trigger_text = ", ".join([t for t in trigger_labels if t][:4]) or "the current behavioural signals"

    narrative = (
        f"{name} is classified as {tier} with an intent score of {intent} and trust score of {trust}. "
        f"The strongest observed signals are {trigger_text}, supporting a {predicted_loan} conversation. "
        f"Reconstructed monthly income is {_format_money(synthetic_income)}, with estimated capacity of "
        f"{_format_money(eligible_amount)} where available. This briefing was generated locally because "
        f"the external NVIDIA narrative service was unavailable."
    )

    outreach_draft = (
        f"Hello {name}, this is your relationship manager from IDBI Bank. "
        f"We noticed recent banking patterns that may make this a good time to discuss {predicted_loan} options. "
        f"Could we schedule a short conversation through {channel} to review eligibility and repayment comfort?"
    )

    objections = [
        {
            "question": "Why am I being contacted?",
            "response": "The recommendation is based on recent account behaviour already visible to the bank, not on any new credit decision.",
        },
        {
            "question": "Does this mean I am approved?",
            "response": "No. This is an eligibility conversation; final approval still depends on KYC, policy checks, and repayment assessment.",
        },
        {
            "question": "How was affordability estimated?",
            "response": f"LENS used reconstructed monthly income of {_format_money(synthetic_income)} and available capacity signals to guide the discussion.",
        },
    ]

    return {
        "narrative": narrative,
        "outreach_draft": outreach_draft,
        "objections": objections,
        "source": "local_fallback",
        "fallback_reason": reason,
    }


def _parse_model_json(raw_text: str, source: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

    try:
        parsed = json.loads(cleaned)
        parsed["source"] = source
        return parsed
    except json.JSONDecodeError:
        return {
            "narrative": "Narrative generation succeeded but response parsing failed — raw output below.",
            "outreach_draft": raw_text,
            "objections": [],
            "source": source,
        }


def _post_chat_completion(provider: str, api_key: str, url: str, model: str, messages: list, timeout_seconds: float) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    response = httpx.post(url, headers=headers, json=body, timeout=timeout_seconds)
    if response.status_code != 200:
        raise RuntimeError(f"{provider} HTTP {response.status_code}: {response.text}")
    raw_text = response.json()["choices"][0]["message"]["content"]
    return _parse_model_json(raw_text, provider)


def generate_lead_narrative(lead_payload: dict) -> dict:
    """
    lead_payload: the same dict returned by GET /api/leads/{customer_id}
    (customer, lead, income_breakdown, capacity)

    Returns {narrative, outreach_draft, objections} or raises RuntimeError.
    """
    try:
        api_key = _get_api_key()
    except RuntimeError as e:
        return _local_fallback_narrative(lead_payload, str(e))

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
                timeout=NVIDIA_TIMEOUT_SECONDS
            )
            if response.status_code == 200:
                res_data = response.json()
                raw_text = res_data["choices"][0]["message"]["content"]
                break
            if response.status_code == 401:
                return _local_fallback_narrative(
                    lead_payload,
                    "NVIDIA API authentication failed. Check NVIDIA_API_KEY.",
                )
            else:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        except (httpx.HTTPError, RuntimeError) as e:
            last_err = e
            if isinstance(e, httpx.TimeoutException):
                return _local_fallback_narrative(
                    lead_payload,
                    f"NVIDIA API timed out after {NVIDIA_TIMEOUT_SECONDS:g} seconds.",
                )
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    else:
        return _local_fallback_narrative(
            lead_payload,
            f"NVIDIA API request failed after 3 attempts: {last_err}",
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


def generate_lead_narrative(lead_payload: dict) -> dict:
    """
    Generate a lead briefing. Provider order:
    1. NVIDIA NIM
    2. Groq OpenAI-compatible chat completions
    3. Deterministic local fallback
    """
    slim_payload = {
        "customer": {k: lead_payload.get("customer", {}).get(k) for k in
                     ("customer_id", "name", "age", "employment_type", "city")},
        "lead": {k: lead_payload.get("lead", {}).get(k) for k in
                 ("intent_score", "trust_score", "tier", "triggers_fired",
                  "predicted_loan_type", "outreach_channel")} if lead_payload.get("lead") else None,
        "income_breakdown": lead_payload.get("income_breakdown"),
        "capacity": lead_payload.get("capacity"),
    }
    messages = [
        {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(slim_payload, default=str)},
    ]

    provider_errors = []
    try:
        return _post_chat_completion(
            provider="nvidia",
            api_key=_get_nvidia_api_key(),
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            model="meta/llama-3.3-70b-instruct",
            messages=messages,
            timeout_seconds=NVIDIA_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, RuntimeError) as e:
        provider_errors.append(str(e))

    try:
        return _post_chat_completion(
            provider="groq",
            api_key=_get_groq_api_key(),
            url="https://api.groq.com/openai/v1/chat/completions",
            model="llama-3.3-70b-versatile",
            messages=messages,
            timeout_seconds=GROQ_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, RuntimeError) as e:
        provider_errors.append(str(e))

    return _local_fallback_narrative(
        lead_payload,
        " | ".join(provider_errors[-3:]) or "External narrative providers unavailable.",
    )
