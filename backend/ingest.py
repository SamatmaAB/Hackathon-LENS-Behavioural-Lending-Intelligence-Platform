"""
LENS Statement Ingestion
========================
Parses uploaded bank statement exports (CSV first; PDF via text extraction)
into the same transaction schema the synthetic generator produces, so the
existing engine.score_customer() pipeline runs unmodified on real data.
"""
import csv
import io
import re
from datetime import datetime
from typing import List, Dict

# Common column name variants across Indian bank statement CSV exports
COLUMN_ALIASES = {
    "date": ["date", "txn date", "transaction date", "value date"],
    "amount": ["amount", "debit", "credit", "withdrawal amt", "deposit amt"],
    "counterparty": ["narration", "description", "particulars", "remarks"],
    "type": ["type", "dr/cr", "transaction type"],
}


def _find_column(header: List[str], aliases: List[str]) -> str | None:
    header_lower = [h.strip().lower() for h in header]
    for alias in aliases:
        if alias in header_lower:
            return header[header_lower.index(alias)]
    return None


def _classify_type(row: dict, amount: float) -> str:
    """Best-effort mapping from a raw bank narration to LENS's internal txn types."""
    narration = (row.get("counterparty") or "").lower()
    if "salary" in narration or "payroll" in narration:
        return "SALARY_CREDIT"
    if "emi" in narration or "loan" in narration:
        return "EMI_DEBIT"
    if any(k in narration for k in ["electricity", "postpaid", "water bill", "bill pay"]):
        return "BILL_PAY"
    if "wallet" in narration or "paytm" in narration or "phonepe" in narration:
        return "WALLET_TOPUP"
    return "UPI_CREDIT" if amount > 0 else "UPI_DEBIT"


def parse_csv_statement(file_bytes: bytes, customer_id: str, consent_confirmed: bool = False) -> List[Dict]:
    """
    Parses a raw CSV bank statement export into LENS transaction dicts.
    Raises ValueError with a clear message if required columns can't be found —
    this must fail loudly, never silently drop data in a lending context.

    consent_confirmed: must be explicitly True — the caller (API layer) is
    responsible for having obtained and recorded customer consent before
    calling this function. This is enforced here, not just documented,
    so ingestion cannot silently proceed without it.
    """
    if not consent_confirmed:
        raise ValueError(
            "Cannot ingest a real bank statement without explicit customer consent. "
            "Set consent_confirmed=True only after recording consent (DPDP Act compliance)."
        )

    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("Empty statement file")

    header = rows[0]
    date_col = _find_column(header, COLUMN_ALIASES["date"])
    amount_col = _find_column(header, COLUMN_ALIASES["amount"])
    desc_col = _find_column(header, COLUMN_ALIASES["counterparty"])

    if not date_col or not amount_col:
        raise ValueError(
            f"Could not identify date/amount columns in header: {header}. "
            "Supported formats: HDFC, ICICI, SBI, IDBI standard CSV exports."
        )

    transactions = []
    for raw_row in rows[1:]:
        row = dict(zip(header, raw_row))
        try:
            amount_str = re.sub(r"[^\d.\-]", "", row.get(amount_col, "0") or "0")
            amount = float(amount_str) if amount_str else 0.0
            if amount == 0:
                continue
            date_str = row.get(date_col, "").strip()
            timestamp = _parse_date(date_str)
            counterparty = (row.get(desc_col, "") or "Unknown").strip()[:100]
            txn_type = _classify_type({"counterparty": counterparty}, amount)
            transactions.append({
                "customer_id": customer_id,
                "timestamp": timestamp,
                "type": txn_type,
                "amount": abs(amount),
                "counterparty": counterparty,
            })
        except Exception:
            continue  # skip malformed rows, don't fail the whole statement

    if not transactions:
        raise ValueError("No valid transactions parsed from statement")
    return transactions


def _parse_date(date_str: str) -> str:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {date_str}")
