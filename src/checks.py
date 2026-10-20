"""
M2 checks: prove the schema and the loader do what we claim.

    python src/checks.py

Runs against a temporary copy of the database (never the real one) and
reports PASS/FAIL for the six required checks. Exit code 1 if any fail.
"""

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import src.load as load
from src.db import connect, get_db_path, table_contents, table_counts

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []


def report(name: str, passed: bool, detail: str) -> None:
    RESULTS.append(passed)
    print(f"{name:<24} {'PASS' if passed else 'FAIL'}   {detail}")


def expect_rejected(db: Path, sql: str, params: tuple, must_contain: str):
    """Runs one statement in its own transaction; passes only if SQLite refuses it."""
    con = connect(db)
    before = table_counts(con)
    try:
        with con:
            con.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        unchanged = table_counts(con) == before
        con.close()
        ok = must_contain in message and unchanged
        return ok, f"rejected: {message}"
    con.close()
    return False, "statement was ACCEPTED"


def check_duplicate_natural_key(db: Path) -> None:
    con = connect(db)
    animal_id = con.execute("SELECT animal_id FROM animals LIMIT 1").fetchone()[0]
    con.close()
    ok, detail = expect_rejected(
        db,
        "INSERT INTO animals (animal_id, retrieved_at) VALUES (?, ?)",
        (animal_id, "2026-10-20T00:00:00Z"),
        "UNIQUE constraint failed",
    )
    report("duplicate natural key", ok, detail)


def check_orphan_event(db: Path) -> None:
    con = connect(db)
    missing_key = con.execute("SELECT COALESCE(MAX(animal_key), 0) + 1000 FROM animals").fetchone()[0]
    con.close()
    ok, detail = expect_rejected(
        db,
        "INSERT INTO intakes (animal_key, intake_at, source_record_id, retrieved_at) VALUES (?, ?, ?, ?)",
        (missing_key, "2026-10-20T12:00:00Z", "check-orphan", "2026-10-20T00:00:00Z"),
        "FOREIGN KEY constraint failed",
    )
    report("orphan event", ok, detail)


def check_null_key(db: Path) -> None:
    con = connect(db)
    animal_key = con.execute("SELECT animal_key FROM animals LIMIT 1").fetchone()[0]
    con.close()
    ok, detail = expect_rejected(
        db,
        "INSERT INTO intakes (animal_key, intake_at, source_record_id, retrieved_at) VALUES (?, ?, ?, ?)",
        (animal_key, None, "check-null", "2026-10-20T00:00:00Z"),
        "NOT NULL constraint failed",
    )
    report("NULL key", ok, detail)


def check_wrong_type(db: Path) -> None:
    con = connect(db)
    animal_key, intake_at = con.execute("SELECT animal_key, intake_at FROM intakes LIMIT 1").fetchone()
    con.close()
    ok, detail = expect_rejected(
        db,
        "INSERT INTO outcomes (animal_key, outcome_at, intake_at, source_record_id, days_in_shelter, retrieved_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (animal_key, "2099-01-01T00:00:00Z", intake_at, "check-type", "abc", "2026-10-20T00:00:00Z"),
        "cannot store TEXT value in INTEGER column",
    )
    report("wrong type", ok, detail)


def check_idempotence(db: Path) -> None:
    load.run_load(db_path=db, quiet=True)
    con = connect(db)
    counts_1, contents_1 = table_counts(con), table_contents(con)
    con.close()
    load.run_load(db_path=db, quiet=True)
    con = connect(db)
    counts_2, contents_2 = table_counts(con), table_contents(con)
    con.close()
    ok = counts_1 == counts_2 and contents_1 == contents_2
    report("idempotence", ok, f"two loads -> {counts_2} (contents identical: {contents_1 == contents_2})")


def synthetic_batch(raw_dir: Path) -> None:
    """A brand-new animal with an intake and an outcome, in a newer landed snapshot."""
    (raw_dir / "intakes").mkdir(parents=True)
    (raw_dir / "outcomes").mkdir(parents=True)
    intake = {"id": "check-1", "animal_id": "CHECK-ANIMAL", "source_date": "2099-01-01T10:00:00.000",
              "type": "Dog", "source_name": "Stray", "sex": "Male"}
    outcome = {"id": "check-2", "animal_id": "CHECK-ANIMAL", "outcome_date": "2099-01-05T10:00:00.000",
               "intake_date": "2099-01-01T10:00:00.000", "outcome_status": "Adopted", "days_in_shelter": "4"}
    (raw_dir / "intakes" / "20990101T000000Z.json").write_text(json.dumps([intake]), encoding="utf-8")
    (raw_dir / "outcomes" / "20990101T000000Z.json").write_text(json.dumps([outcome]), encoding="utf-8")


def check_atomicity(db: Path, tmp: Path) -> None:
    con = connect(db)
    before_counts, before_contents = table_counts(con), table_contents(con)
    con.close()

    # (a) The load fails partway: animals and intakes are already written
    #     inside the transaction when the outcome step breaks.
    raw_a = tmp / "raw_failing_midway"
    synthetic_batch(raw_a)
    reached = {"outcomes_step": False}
    original = load.upsert_outcomes

    def broken_upsert_outcomes(con, rows):
        reached["outcomes_step"] = True
        raise RuntimeError("injected failure while writing outcomes")

    load.upsert_outcomes = broken_upsert_outcomes
    failed_a = False
    try:
        load.run_load(db_path=db, raw_dir=raw_a, quiet=True)
    except RuntimeError:
        failed_a = True
    finally:
        load.upsert_outcomes = original

    # (b) One broken record: a landed file that is no longer valid JSON.
    raw_b = tmp / "raw_broken_record"
    synthetic_batch(raw_b)
    bad = raw_b / "outcomes" / "20990102T000000Z.json"
    bad.write_text('[{"id": "check-3", "animal_id": "CHECK-ANIMAL", "outcome_date": ', encoding="utf-8")
    failed_b = False
    try:
        load.run_load(db_path=db, raw_dir=raw_b, quiet=True)
    except load.LoadError:
        failed_b = True

    con = connect(db)
    after_counts, after_contents = table_counts(con), table_contents(con)
    con.close()
    unchanged = before_counts == after_counts and before_contents == after_contents
    ok = failed_a and reached["outcomes_step"] and failed_b and unchanged
    report("atomicity", ok, f"failed midway + broken file -> counts {before_counts} -> {after_counts}")


def main() -> None:
    real_db = get_db_path()
    if not real_db.exists():
        print(f"Database not found at {real_db}. Build it first: python src/load.py")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        db = tmp / "check.db"
        shutil.copyfile(real_db, db)
        print("=" * 64)
        print(f"M2 checks on a temporary copy of {real_db.name}")
        print("=" * 64)
        check_duplicate_natural_key(db)
        check_orphan_event(db)
        check_null_key(db)
        check_wrong_type(db)
        check_idempotence(db)
        check_atomicity(db, tmp)
        print("-" * 64)
        passed = sum(RESULTS)
        print(f"{passed}/{len(RESULTS)} checks passed")
        print("=" * 64)

    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
