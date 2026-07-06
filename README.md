# LENS — Behavioural Intelligence Engine
### Working prototype for Team Heapify · IDBI Innovate 2026 (Prospect Assist AI)

This is a fully working, runnable implementation of the LENS system described
in the submission deck: **PULSE → CLARITY → MOMENT → MATCH → TRUST SCORE**,
running end-to-end against a generated synthetic transaction warehouse (standing
in for IDBI's real UPI/NEFT/IMPS/EMI data pipeline).

Nothing here is mocked at the scoring layer — every lead, trigger, income
figure, and outreach recommendation you see is computed live by the rule
engine in `backend/engine.py` against rows in SQLite. Re-run the generator
with a different seed and the leads change accordingly.

```
lens/
├── backend/            FastAPI service + scoring engine
│   ├── app.py           REST API
│   ├── engine.py         PULSE / CLARITY / MATCH / MOMENT / TRUST SCORE
│   ├── data_gen.py       Synthetic customer + transaction generator
│   └── requirements.txt
└── frontend/
    └── index.html        RM Lead Console (React, single file, no build step)
```

## What's implemented

| Component | What it does | Where |
|---|---|---|
| **PULSE** | 14 behavioural triggers evaluated against each customer's 90-day transaction history (salary clustering, large outward transfers, property/auto/education/medical payments, wedding spend spikes, EMI changes, wallet top-up frequency, overdraft near-misses, etc.) → weighted **Intent Score** | `engine.py::_detect_triggers`, `compute_intent_score` |
| **CLARITY** | For non-salaried customers, clusters UPI credit transactions by counterparty/source regularity and projects a **Synthetic Monthly Income**, benchmarked against the generator's ground-truth income | `engine.py::reconstruct_income` |
| **MATCH** | Predicts **Home / Auto / Personal / Mortgage** loan type from the dominant trigger pattern | `engine.py::predict_loan_type` |
| **MOMENT** | Computes the 72-hour outreach window and recommends a channel (App Notification / RM Call / Branch Visit) based on age, employment type and trigger profile | `engine.py::determine_outreach` |
| **TRUST SCORE** | Combines Intent (40%) + Income confidence (30%) + Repayment-behaviour indicators (30%) into Tier 1 / 2 / 3 | `engine.py::compute_trust_score` |

On a fresh 150-customer run with 20% dataset noise, this lands close to:
(~42% conversion, ~3–4h time-to-lead, ~66.7% loan-type match) because it's
calibrated against the same kind of behavioural patterns — but the numbers
are recomputed every time you regenerate the dataset, not hardcoded.

## Run it

### 1. Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

The first request auto-generates a 150-customer synthetic dataset into
`backend/lens.db`. Visit `http://localhost:8000/docs` for interactive API docs.

Key endpoints:
- `POST /api/generate?n_customers=150` — regenerate the dataset and rescan
- `GET /api/stats` — KPI summary for the dashboard header
- `GET /api/leads` — ranked lead queue (filter with `?tier=Tier 1&search=...&sort=trust_score`)
- `GET /api/leads/{customer_id}` — full lead detail: triggers, income breakdown, transactions

### 2. Frontend

No build step — it's a single HTML file using React/Babel from a CDN.

```bash
cd frontend
python3 -m http.server 5500
```

Open `http://localhost:5500` in a browser (keep the backend running on port
8000 in another terminal). If your backend runs elsewhere, open
`http://localhost:5500/?api=http://your-host:8000` instead.

## What you'll see

The **RM Lead Console**: a ranked queue of leads (Tier 1/2/3, intent-score
"pulse" waveform, trust score) on the left; click any lead to open the full
explainable breakdown on the right — which of the 14 triggers fired and why,
the reconstructed income vs. declared income, the recommended outreach
channel and window, and the underlying transaction stream that produced it.

## Honest scope notes (for the judges)

- **Stream processing**: the deck specs Kafka + Flink for production-scale
  real-time ingestion. This prototype processes the full transaction history
  in a single batch pass per customer, which is the right substitute for a
  hackathon timebox — the trigger logic itself is what Kafka/Flink would run
  per-event in production, unchanged.
- **ML models**: the deck specs XGBoost/Random Forest/LSTM for income,
  loan-type, and timing prediction. This prototype uses transparent,
  auditable rule-and-statistics logic (clustering by source regularity,
  weighted trigger scoring) instead — easier to explain to underwriting and
  RBI compliance reviewers, and a clean drop-in point to swap in trained
  models later without touching the API contract.
- Data: transactions are synthetically generated with realistic personas
  (a home-loan-intent persona, an auto-loan persona, etc.) so the engine has
  real signal to detect — it isn't fed pre-labelled answers.

## Conversion Rate & Underwriting Methodology

**Conversion Rate Definition:** In the LENS simulation, the conversion rate is defined as the percentage of flagged leads whose ground-truth persona matches genuine loan intent (i.e. has a true loan type other than "None"), evaluated against the synthetic labels generated with a configurable dataset noise (default `noise_level=20%`).

**Underwriting & Capacity Assumptions:** The CAPACITY module utilizes standard Fixed Obligation to Income Ratio (FOIR) bands (Personal Loan: 40-50%, Auto Loan: 45-55%, Home Loan: 50-60%, Mortgage Loan: 50-55%) and retail interest rate/tenure assumptions (Personal: 13.0% APR / 60m, Auto: 9.5% APR / 84m, Home: 8.5% APR / 240m, Mortgage: 9.0% APR / 180m) to calculate prudent repayment limits and eligible principal amounts. In a production deployment, these rates and bands would be pulled dynamically from live bank rate cards.

**Granular Accuracy Breakdown:** To support underwriting precision auditing, the Governance tab now displays a per-loan-type accuracy breakdown (Precision, Recall, and F1-score for Personal/Auto/Home/Mortgage products) alongside the aggregate model metrics.

## Governance, Compliance & Business ROI

We run a suite of automated governance metrics dynamically against our SQLite data store to audit fairness, regulatory compliance, sandbox schemas, and return-on-investment (ROI).

### 1. Fairness Audit and Findings
Evaluating the engine output on our default 150-customer dataset (generated with random seed `42`) reveals a systematic **Fairness Gap**:
* **Observation**: Gig Workers and Freelancers represent **37%** of our customers, but they currently generate **0 leads**.
* **Root Cause**: The maximum intent score for these segments is **43**, which is below the static `LEAD_THRESHOLD` of **45**. Their irregular income structures and trigger weights result in a lower raw propensity score, meaning they are completely locked out of credit offers.
* **Dynamic Mitigation**: We have implemented dynamic fairness analysis. If a segment's conversion rate trails the best-converting segment (Salaried, at **65.4%**) by **20 percentage points or more**, it is flagged for intervention. This allows the system to auto-detect and flag biases on any future dataset.

### 2. DPDP Act & RBI Compliance Considerations
The governance module evaluates LENS against the Indian digital lending regulatory landscape:
* **DPDP Act (2023)**: Requires explicit customer consent tracking and data minimization. LENS complies with data minimization (storing only transaction metadata, avoiding PII/raw chat payloads) but requires adding dedicated consent logs and right-to-erasure workflows in production.
* **RBI Digital Lending Guidelines**: Mandates auditable lead cards, transparency of scoring, and data localization. LENS excels in explainability since all triggers are mapped to transaction records in SQLite. However, RM consoles require access log audit trails, and threshold parameter changes require Maker-Checker verification.
* **Risk Assessment**: CLARITY dynamically calculates model risk by comparing Synthetic Monthly Income against ground-truth income (tracking average income deviation, which is currently **0%** variance for salary clustering and highly accurate for irregular inflows).

### 3. Sandbox Field Mapping
Third-party systems integrate with LENS via standard sandbox mappings. The internal schemas are transformed as follows:
* `customers.customer_id` → `client_ref_id` (Direct mapping, alphanumeric)
* `customers.name` → `customer_full_name` (Direct mapping)
* `customers.employment_type` → `employment_classification` (Enum mapping)
* `transactions.type` → `txn_category` (Enum mapped to standard categories)
* `leads.intent_score` → `propensity_index` (Normalized to a float between `0.0` and `1.0` via `intent_score / 100.0`)
* `leads.tier` → `risk_segment` (Mapped 'Tier 1' → 'LOW_RISK', 'Tier 2' → 'MEDIUM_RISK', etc.)
* *Note*: Internal evaluation metrics like `persona` and `match_correct` are excluded from the sandbox mappings.

### 4. Business ROI Methodology
ROI is calculated dynamically based on real data counts in `lens.db`:
* **Cost Assumptions**:
  * **Data/Compute Cost**: ₹5 per customer assessment.
  * **Outreach Cost**: ₹50 per lead contacted (RM Call, notifications, or prompts).
* **Expected Return**:
  * **Expected Disbursal Rate**: 15% of generated leads.
  * **Average Disbursal Profit**: 3% yield on a ₹2,00,000 loan (equals ₹6,000 net profit per loan).
* **Formula**:
  * $\text{Assessment Cost} = \text{Total Customers} \times ₹5$
  * $\text{Outreach Cost} = \text{Total Leads} \times ₹50$
  * $\text{Expected Revenue} = \text{Total Leads} \times 15\% \times ₹6000$
  * $\text{ROI Multiplier} = \frac{\text{Expected Revenue}}{\text{Assessment Cost} + \text{Outreach Cost}}$
* **Current ROI Stats**: Based on the 150-customer dataset (45 leads), the assessment and outreach cost is ₹3,000. Expected revenue is ₹40,500, yielding a net profit of **₹37,500** and a **13.5x ROI Multiplier**.

### 5. Governance API Endpoints
All endpoints are secured via Bearer tokens (`Depends(require_user)`):
* `GET /api/governance/fairness`: Dynamic conversion rates, segment statistics, and flagged underperforming segments.
* `GET /api/governance/compliance`: Standard audits against DPDP 2023 and RBI Lending Guidelines, listing gaps and recommendations.
* `GET /api/governance/sandbox-mapping`: Machine-readable mapping configurations between internal and sandbox variables.
* `GET /api/governance/roi`: Financial ROI calculations and cost-benefit breakdowns by customer segment.

