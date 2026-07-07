from backend import ingest
import pytest


SAMPLE_CSV = b"""Date,Description,Amount
01/01/2026,SALARY CREDIT ACME CORP,65000
03/01/2026,UPI TO SWIGGY,-450
15/01/2026,EMI DEBIT HDFC HOME LOAN,-22000
20/01/2026,NEFT TO LODHA DEVELOPERS,-150000
"""


def test_parse_csv_statement_basic():
    txns = ingest.parse_csv_statement(SAMPLE_CSV, "CUST_REAL_001", consent_confirmed=True)
    assert len(txns) == 4
    types = {t["type"] for t in txns}
    assert "SALARY_CREDIT" in types
    assert "EMI_DEBIT" in types


def test_parse_csv_rejects_missing_columns():
    bad_csv = b"col1,col2\nfoo,bar\n"
    with pytest.raises(ValueError, match="Could not identify"):
        ingest.parse_csv_statement(bad_csv, "CUST_X", consent_confirmed=True)


def test_parse_csv_skips_malformed_rows_without_failing():
    csv_with_junk = SAMPLE_CSV + b"garbage,row,here\n"
    txns = ingest.parse_csv_statement(csv_with_junk, "CUST_REAL_002", consent_confirmed=True)
    assert len(txns) == 4  # junk row silently skipped, valid ones kept
