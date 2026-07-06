import statistics
from datetime import datetime
from typing import Dict, List, Optional, Any
try:
    from backend.models import CapacityResult
except ImportError:
    from models import CapacityResult  # type: ignore[no-redef]

# Industry and RBI guidelines reference:
# Standard FOIR (Fixed Obligation to Income Ratio) bands range between 40% and 60%
# depending on loan type and income level. Prudent underwriting shifts this based
# on risk/repayment score indicators (e.g. defaults, CC payments, balance trends).
FOIR_BANDS = {
    "Personal Loan": (0.40, 0.50),
    "Auto Loan":     (0.45, 0.55),
    "Home Loan":     (0.50, 0.60),
    "Mortgage Loan": (0.50, 0.55),
}

# Assumed retail lending rates in India (illustrative / editable config)
ASSUMED_RATES = {
    "Personal Loan": 13.0,
    "Auto Loan": 9.5,
    "Home Loan": 8.5,
    "Mortgage Loan": 9.0,
}

ASSUMED_TENURE_MONTHS = {
    "Personal Loan": 60,
    "Auto Loan": 84,
    "Home Loan": 240,
    "Mortgage Loan": 180,
}

def eligible_principal(emi: float, annual_rate_pct: float, tenure_months: int) -> float:
    """Computes eligible principal given EMI, annual rate pct, and tenure in months."""
    r = (annual_rate_pct / 12) / 100
    if r == 0:
        return emi * tenure_months
    return emi * ((1 + r) ** tenure_months - 1) / (r * (1 + r) ** tenure_months)

def normalize_loan_type(loan_type: str) -> str:
    """Normalizes MATCH-predicted loan type to match the capacity estimation keys."""
    if loan_type == "Mortgage":
        return "Mortgage Loan"
    if loan_type in FOIR_BANDS:
        return loan_type
    return "Personal Loan"  # Default fallback

def compute_capacity(
    customer_id: str,
    transactions: List[Dict[str, Any]],
    reconstructed_income: float,
    declared_income: Optional[float],
    predicted_loan_type: str,
    repay_score: float = 50.0
) -> CapacityResult:
    """
    Computes capacity details for a customer.
    
    1. Scan transactions to detect recurring monthly debits (spaced between 20 and 45 days)
       representing existing EMIs or utility/rent outflows.
    2. Compute disposable income as reconstructed_income - existing_emi_monthly - recurring_non_debt_outflows.
    3. Apply FOIR band shifted by TRUST's repayment-signal score.
    4. Compute eligible loan amount for all 4 types using reducing-balance formula.
    5. Flag over-leveraged if existing_emi_monthly > 60% of reconstructed_income.
    """
    # 1. Recurring Obligations Detection
    debits_by_counterparty = {}
    for t in transactions:
        # Match standard debit transaction types from engine
        if t.get("type") in ("UPI_DEBIT", "IMPS", "NEFT", "EMI_DEBIT", "BILL_PAY"):
            cp = t.get("counterparty", "")
            if cp:
                debits_by_counterparty.setdefault(cp, []).append(t)

    existing_emi_monthly = 0.0
    recurring_non_debt_outflows = 0.0

    # Lender keywords to recognize EMI collection entities
    lender_keywords = ["bank", "nbfc", "finance", "capital", "loan"]
    # Utility/rent keywords
    utility_keywords = ["rent", "utility", "electricity", "postpaid", "water", "bill", "school", "tuition", "insurance", "landlord"]

    for cp, txs in debits_by_counterparty.items():
        if len(txs) < 3:
            continue
        
        # Sort chronologically
        txs_sorted = sorted(txs, key=lambda x: x["timestamp"])
        
        # Check if all consecutive intervals are between 20 and 45 days
        is_monthly_cadence = True
        for i in range(1, len(txs_sorted)):
            try:
                t1 = datetime.fromisoformat(txs_sorted[i-1]["timestamp"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(txs_sorted[i]["timestamp"].replace("Z", "+00:00"))
                days = (t2 - t1).days
                if not (20 <= days <= 45):
                    is_monthly_cadence = False
                    break
            except Exception:
                is_monthly_cadence = False
                break
        
        if not is_monthly_cadence:
            continue

        # Regularity check (coefficient of variation of amount)
        amounts = [float(tx["amount"]) for tx in txs_sorted]
        mean_amt = statistics.mean(amounts)
        pstdev_amt = statistics.pstdev(amounts)
        cv = (pstdev_amt / mean_amt) if mean_amt > 0 else 0.0

        cp_lower = cp.lower()
        if "self" in cp_lower:
            # Self-transfers are not external liabilities/outflows
            continue

        is_emi_debit_type = any(tx.get("type") == "EMI_DEBIT" for tx in txs_sorted)
        is_lender_name = any(kw in cp_lower for kw in lender_keywords)
        is_amount_highly_regular = cv < 0.05

        # Classify as EMI obligation
        if is_emi_debit_type or is_lender_name or is_amount_highly_regular:
            existing_emi_monthly += mean_amt
        # Classify as recurring rent/utility
        elif any(kw in cp_lower for kw in utility_keywords) or any(tx.get("type") == "BILL_PAY" for tx in txs_sorted):
            recurring_non_debt_outflows += mean_amt

    # Clamp reconstructed income to 0
    base_reconstructed = max(0.0, reconstructed_income)

    # 2. Disposable Income
    disposable_income = base_reconstructed - existing_emi_monthly - recurring_non_debt_outflows
    
    # Over-leveraged check
    over_leveraged = False
    if base_reconstructed > 0 and existing_emi_monthly > (base_reconstructed * 0.6):
        over_leveraged = True
    elif base_reconstructed == 0 and existing_emi_monthly > 0:
        over_leveraged = True

    # Clamp disposable income at 0
    disposable_income = max(0.0, disposable_income)

    # 3. FOIR Band selection & Shift based on repay_score
    norm_rec_loan = normalize_loan_type(predicted_loan_type)
    
    eligible_amount_by_type = {}
    foir_applied_by_type = {}
    
    # We calculate FOIR & eligible amount for all 4 types to display in the comparison table
    for ltype in FOIR_BANDS.keys():
        lower_bound, upper_bound = FOIR_BANDS[ltype]
        # Shift ratio between lower and upper bound based on repay_score (0 to 100)
        # repay_score = 50 gives exactly the midpoint: lower_bound + (upper_bound - lower_bound)*0.5
        foir_ratio = lower_bound + (upper_bound - lower_bound) * (repay_score / 100.0)
        foir_ratio = max(lower_bound, min(upper_bound, foir_ratio))
        foir_applied_by_type[ltype] = foir_ratio

        # safe_emi_ceiling = disposable_income * foir_ratio
        emi_ceiling = disposable_income * foir_ratio
        
        rate = ASSUMED_RATES[ltype]
        tenure = ASSUMED_TENURE_MONTHS[ltype]
        
        principal = eligible_principal(emi_ceiling, rate, tenure)
        eligible_amount_by_type[ltype] = round(max(0.0, principal), 2)

    # Values for recommended type
    rec_foir_ratio = foir_applied_by_type[norm_rec_loan]
    rec_safe_emi_ceiling = round(disposable_income * rec_foir_ratio, 2)
    rec_eligible_amount = eligible_amount_by_type[norm_rec_loan]
    rec_tenure = ASSUMED_TENURE_MONTHS[norm_rec_loan]

    # 5. DTI Ratio
    # dti_ratio = (existing_emi_monthly + safe_emi_ceiling) / reconstructed_income
    if base_reconstructed > 0:
        dti_ratio = (existing_emi_monthly + rec_safe_emi_ceiling) / base_reconstructed
    else:
        dti_ratio = 1.0 if (existing_emi_monthly + rec_safe_emi_ceiling) > 0 else 0.0
    dti_ratio = max(0.0, min(1.0, dti_ratio))

    # Assumptions dict for transparency
    assumptions = {
        "assumed_rates": ASSUMED_RATES,
        "assumed_tenure_months": ASSUMED_TENURE_MONTHS,
        "foir_bands": FOIR_BANDS
    }

    return CapacityResult(
        customer_id=customer_id,
        reconstructed_income=round(reconstructed_income, 2),
        declared_income=round(declared_income, 2) if declared_income is not None else None,
        existing_emi_monthly=round(existing_emi_monthly, 2),
        disposable_income=round(disposable_income, 2),
        foir_ratio_applied=round(rec_foir_ratio, 4),
        safe_emi_ceiling=rec_safe_emi_ceiling,
        dti_ratio=round(dti_ratio, 4),
        eligible_amount_by_type=eligible_amount_by_type,
        recommended_loan_type=predicted_loan_type, # Return MATCH's original label
        recommended_eligible_amount=rec_eligible_amount,
        recommended_tenure_months=rec_tenure,
        assumptions=assumptions,
        over_leveraged=over_leveraged
    )
