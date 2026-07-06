from pydantic import BaseModel
from typing import Dict, Optional, Any

class CapacityResult(BaseModel):
    customer_id: str
    reconstructed_income: float
    declared_income: Optional[float]
    existing_emi_monthly: float
    disposable_income: float
    foir_ratio_applied: float
    safe_emi_ceiling: float
    dti_ratio: float
    eligible_amount_by_type: Dict[str, float]
    recommended_loan_type: str
    recommended_eligible_amount: float
    recommended_tenure_months: int
    assumptions: Dict[str, Any]
    over_leveraged: bool = False
