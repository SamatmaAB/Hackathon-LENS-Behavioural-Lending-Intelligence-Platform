import pytest
from backend import db, app as app_module


def test_outcomes_table_created_on_sqlite(tmp_path):
    db_path = str(tmp_path / "test_schema.db")
    conn = db.connect(db_path)
    app_module._migrate_database(conn)
    rows = db.rows(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='lead_outcomes'")
    assert len(rows) > 0
    conn.close()


def test_schema_init_is_idempotent(tmp_path):
    # Running init twice on the same DB must not raise (covers the try/except
    # around CREATE TABLE IF NOT EXISTS paths)
    db_path = str(tmp_path / "test_schema2.db")
    conn = db.connect(db_path)
    app_module._migrate_database(conn)
    app_module._migrate_database(conn)  # second call — should be a no-op, not an error
    conn.close()


def test_outcomes_table_uses_serial_on_postgres_branch(monkeypatch, tmp_path):
    # Simulate the IS_POSTGRES branch without a real Postgres instance —
    # confirm the SERIAL-rewrite code path at least constructs valid SQL
    # and doesn't silently swallow a real error when it shouldn't.
    db_path = str(tmp_path / "test_schema3.db")  # still sqlite under the hood for this test
    conn = db.connect(db_path)
    monkeypatch.setattr(db, "IS_POSTGRES", True)
    
    original_execute = db.execute
    fallback_called = False
    
    def mock_execute(conn, query, *args, **kwargs):
        nonlocal fallback_called
        if "lead_outcomes" in query and "AUTOINCREMENT" in query:
            raise Exception("Simulated Postgres syntax error on AUTOINCREMENT")
        if "lead_outcomes" in query and "SERIAL" in query:
            fallback_called = True
        # Let the second call (the SERIAL fallback) through.
        return original_execute(conn, query, *args, **kwargs)
        
    monkeypatch.setattr(db, "execute", mock_execute)
    
    # It won't raise an exception because SQLite accepts SERIAL PRIMARY KEY as a valid type
    app_module._migrate_database(conn)
        
    # verify the fallback was executed
    assert fallback_called
    conn.close()
