import pytest
from backend import ingest


def test_ingest_refuses_without_explicit_consent_flag():
    csv_bytes = b"Date,Description,Amount\n2026-01-01,Salary,50000"
    with pytest.raises(ValueError, match="explicit customer consent"):
        ingest.parse_csv_statement(csv_bytes, "C1")


def test_ingest_proceeds_when_consent_confirmed():
    csv_bytes = b"Date,Description,Amount\n2026-01-01,Salary,50000"
    result = ingest.parse_csv_statement(csv_bytes, "C1", consent_confirmed=True)
    assert len(result) == 1
    assert result[0]["amount"] == 50000
