import os
import sqlite3

DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("POSTGRES_URL_NON_POOLING")
    or os.getenv("POSTGRES_PRISMA_URL")
)
IS_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")))


def _load_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("DATABASE_URL is set, but psycopg is not installed") from exc
    return psycopg, dict_row


def connect(sqlite_path=None):
    if IS_POSTGRES:
        psycopg, dict_row = _load_psycopg()
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(sqlite_path or os.getenv("LENS_DB_PATH") or "lens.db")
    conn.row_factory = sqlite3.Row
    return conn


def is_integrity_error(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    if IS_POSTGRES:
        psycopg, _ = _load_psycopg()
        return isinstance(exc, psycopg.IntegrityError)
    return False


def sql(query):
    return query.replace("?", "%s") if IS_POSTGRES else query


def execute(conn, query, params=()):
    return conn.execute(sql(query), params)


def executemany(conn, query, rows):
    cur = conn.cursor()
    try:
        cur.executemany(sql(query), rows)
    finally:
        cur.close()


def scalar(conn, query, params=()):
    row = execute(conn, query, params).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def rows(conn, query, params=()):
    return [dict(row) for row in execute(conn, query, params).fetchall()]


def one(conn, query, params=()):
    row = execute(conn, query, params).fetchone()
    return dict(row) if row else None


def executescript(conn, script):
    if not IS_POSTGRES:
        conn.executescript(script)
        return
    statements = [stmt.strip() for stmt in script.split(";") if stmt.strip()]
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def last_insert_id(cursor):
    if IS_POSTGRES:
        row = cursor.fetchone()
        return row["id"] if isinstance(row, dict) else row[0]
    return cursor.lastrowid
