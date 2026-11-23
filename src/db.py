"""
Shared SQLite and pipeline-clock helpers for the Austin Animal Center pipeline.

Every script connects through connect(), which turns on foreign key
enforcement immediately after opening the connection and refuses to
silently create an empty database unless create=True (used only by src/load.py).

Table names are fixed in this module, so no SQL string is ever built
from values at runtime: every value reaches SQL through a placeholder.
"""

import os
import sqlite3
from datetime import datetime, timezone
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


def get_run_time(cli_value: str | None = None) -> str:
    """
    Resolves the shared pipeline clock in order:
      1. Explicit `--run-time` CLI value (`cli_value`)
      2. `PIPELINE_RUN_TIME` environment variable (set by `src/run_pipeline.py`)
      3. Current UTC wall-clock time (`YYYY-MM-DDTHH:MM:SSZ`)
    Normalises any valid ISO 8601 timestamp to canonical UTC `YYYY-MM-DDTHH:MM:SSZ`.
    """
    raw = cli_value or os.getenv("PIPELINE_RUN_TIME")
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"Invalid run-time timestamp '{raw}'. Expected ISO 8601 UTC (e.g. 2026-11-23T18:00:00Z)."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path=None, create: bool = False) -> sqlite3.Connection:
    """
    Opens the database and enforces foreign keys before anything else happens.

    When create=False (the default for every script except src/load.py),
    raises FileNotFoundError if the database file does not already exist
    instead of letting sqlite3.connect() silently create an empty file.
    """
    if db_path is not None:
        p = Path(db_path)
        path = p if p.is_absolute() else ROOT_DIR / p
    else:
        path = get_db_path()

    if not create and not path.exists():
        raise FileNotFoundError(
            f"Database not found at {path}. Build it first with `python src/load.py`."
        )

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
