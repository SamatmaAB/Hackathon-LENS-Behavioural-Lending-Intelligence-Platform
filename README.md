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

On a fresh 150-customer run this lands close to the deck's benchmark numbers
(~30% conversion, ~3–7h time-to-lead, ~90% loan-type match) because it's
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
- **Data**: transactions are synthetically generated with realistic personas
  (a home-loan-intent persona, an auto-loan persona, etc.) so the engine has
  real signal to detect — it isn't fed pre-labelled answers.
