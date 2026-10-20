"""
Shared SQLite helpers for the Austin Animal Center pipeline.

Every script connects through connect(), which turns on foreign key
enforcement immediately after opening the connection.

Table names are fixed in this module, so no SQL string is ever built
from values at runtime: every value reaches SQL through a placeholder.
"""

import os
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT_DIR / "sql" / "schema.sql"
DEFAULT_DB_PATH = "data/austin_animals.db"

TABLES = ("animals", "intakes", "outcomes", "quarantine")

COUNT_SQL = {
    "animals": "SELECT COUNT(*) FROM animals",
    "intakes": "SELECT COUNT(*) FROM intakes",
    "outcomes": "SELECT COUNT(*) FROM outcomes",
    "quarantine": "SELECT COUNT(*) FROM quarantine",
}

DUMP_SQL = {
    "animals": "SELECT * FROM animals ORDER BY animal_key",
    "intakes": "SELECT * FROM intakes ORDER BY animal_key, intake_at",
    "outcomes": "SELECT * FROM outcomes ORDER BY animal_key, outcome_at",
    "quarantine": "SELECT * FROM quarantine ORDER BY source, source_record_id",
}


def get_db_path() -> Path:
    """Database location: DB_PATH from the environment, relative to the repo root."""
    path = Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))
    return path if path.is_absolute() else ROOT_DIR / path


def connect(db_path=None) -> sqlite3.Connection:
    """Opens the database and enforces foreign keys before anything else happens."""
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        con.close()
        raise RuntimeError("SQLite refused to enable foreign key enforcement.")
    return con


def apply_schema(con: sqlite3.Connection) -> None:
    """Creates every table if it does not exist yet (sql/schema.sql is re-runnable)."""
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def table_counts(con: sqlite3.Connection) -> dict:
    """Row count of every table."""
    return {t: con.execute(COUNT_SQL[t]).fetchone()[0] for t in TABLES}


def table_contents(con: sqlite3.Connection) -> dict:
    """Full, ordered contents of every table (used to prove idempotence)."""
    return {t: con.execute(DUMP_SQL[t]).fetchall() for t in TABLES}
