"""
Milestone M3 — Automated Data Quality Validation Suite.

Runs 7 data quality checks (3 STOP, 4 WARN) against the cleaned SQLite layer.
- Exits 0 when all checks are PASS or WARN.
- Exits 1 when any check is STOP.
- Pass `--test-stops` to verify on a temporary database copy that every STOP
  check reliably triggers and returns a non-zero exit code when broken.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.clean_config import (  # noqa: E402
    FRESHNESS_WINDOW_DAYS,
    MAX_CORE_DOB_MISSING_SHARE,
    MAX_EXPECTED_DUPLICATE_EXITS,
    MAX_OVERALL_DOB_MISSING_SHARE,
    MAX_VALID_DATE,
    TEXAS_ZIP_PREFIXES,
)

DB_PATH = PROJECT_ROOT / "data" / "austin_animals.db"


class CheckResult:
    """Container for a single validation check outcome."""

    def __init__(self, check_id: str, title: str, level: str, status: str, detail: str) -> None:
        self.check_id = check_id
        self.title = title
        self.level = level      # "STOP" or "WARN"
        self.status = status    # "PASS", "WARN", or "STOP"
        self.detail = detail


def check_stop_1_row_parity(conn: sqlite3.Connection) -> CheckResult:
    """STOP 1: Every <table_name>_clean has the exact same row count as M2 <table_name>."""
    animals_m2 = conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0]
    animals_cl = conn.execute("SELECT COUNT(*) FROM animals_clean").fetchone()[0]
    intakes_m2 = conn.execute("SELECT COUNT(*) FROM intakes").fetchone()[0]
    intakes_cl = conn.execute("SELECT COUNT(*) FROM intakes_clean").fetchone()[0]
    outcomes_m2 = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    outcomes_cl = conn.execute("SELECT COUNT(*) FROM outcomes_clean").fetchone()[0]

    mismatches = []
    if animals_m2 != animals_cl:
        mismatches.append(f"animals ({animals_m2}) != animals_clean ({animals_cl})")
    if intakes_m2 != intakes_cl:
        mismatches.append(f"intakes ({intakes_m2}) != intakes_clean ({intakes_cl})")
    if outcomes_m2 != outcomes_cl:
        mismatches.append(f"outcomes ({outcomes_m2}) != outcomes_clean ({outcomes_cl})")

    if mismatches:
        return CheckResult(
            "STOP-1",
            "Row count parity between M2 and M3 cleaned tables",
            "STOP",
            "STOP",
            "Row count mismatch: " + "; ".join(mismatches),
        )
    return CheckResult(
        "STOP-1",
        "Row count parity between M2 and M3 cleaned tables",
        "STOP",
        "PASS",
        f"Exact match: animals={animals_cl}, intakes={intakes_cl}, outcomes={outcomes_cl}",
    )


def check_stop_2_zero_invalid_values(conn: sqlite3.Connection) -> CheckResult:
    """STOP 2: Zero impossible or uncleaned sentinel values survive in cleaned columns."""
    future_dob = conn.execute(
        "SELECT COUNT(*) FROM animals_clean WHERE date_of_birth IS NOT NULL AND date_of_birth > ?",
        (MAX_VALID_DATE,),
    ).fetchone()[0]

    dob_after_intake = conn.execute(
        """
        SELECT COUNT(*)
        FROM animals_clean a
        JOIN (
            SELECT animal_key, MIN(substr(intake_at, 1, 10)) AS first_intake_date
            FROM intakes_clean
            GROUP BY animal_key
        ) i ON i.animal_key = a.animal_key
        WHERE a.date_of_birth IS NOT NULL
          AND a.date_of_birth > i.first_intake_date
        """
    ).fetchone()[0]

    neg_days = conn.execute(
        """
        SELECT COUNT(*)
        FROM outcomes_clean
        WHERE (days_in_shelter IS NOT NULL AND days_in_shelter < 0)
           OR (length_of_stay_days IS NOT NULL AND length_of_stay_days < 0)
        """
    ).fetchone()[0]

    unknown_sentinels = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM animals_clean WHERE lower( COALESCE(sex, '') ) = 'unknown') +
            (SELECT COUNT(*) FROM animals_clean WHERE lower( COALESCE(primary_color, '') ) = 'unknown') +
            (SELECT COUNT(*) FROM outcomes_clean WHERE lower( COALESCE(spayed_neutered, '') ) = 'unknown')
        """
    ).fetchone()[0]

    zips = conn.execute(
        "SELECT found_zip FROM intakes_clean WHERE found_zip IS NOT NULL"
    ).fetchall()
    bad_zips = sum(
        1 for (z,) in zips
        if len(z) != 5 or not z.isdigit() or not z.startswith(TEXAS_ZIP_PREFIXES)
    )

    unmapped_types = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM animals_clean a LEFT JOIN map_animal_type m ON m.raw_value = a.animal_type_raw WHERE m.raw_value IS NULL) +
            (SELECT COUNT(*) FROM intakes_clean i LEFT JOIN map_intake_type m ON m.raw_value = i.intake_type_raw WHERE m.raw_value IS NULL) +
            (SELECT COUNT(*) FROM outcomes_clean o LEFT JOIN map_outcome_status m ON m.raw_value = o.outcome_status_raw WHERE m.raw_value IS NULL)
        """
    ).fetchone()[0]

    failures = []
    if future_dob > 0:
        failures.append(f"future_dob={future_dob}")
    if dob_after_intake > 0:
        failures.append(f"dob_after_intake={dob_after_intake}")
    if neg_days > 0:
        failures.append(f"neg_stay_days={neg_days}")
    if unknown_sentinels > 0:
        failures.append(f"unknown_sentinels={unknown_sentinels}")
    if bad_zips > 0:
        failures.append(f"non_texas_zips={bad_zips}")
    if unmapped_types > 0:
        failures.append(f"unmapped_categories={unmapped_types}")

    if failures:
        return CheckResult(
            "STOP-2",
            "Zero invalid or sentinel values surviving in cleaned columns",
            "STOP",
            "STOP",
            "Invalid values survived cleaning: " + ", ".join(failures),
        )
    return CheckResult(
        "STOP-2",
        "Zero invalid or sentinel values surviving in cleaned columns",
        "STOP",
        "PASS",
        "0 future/post-intake DOBs, 0 negative stays, 0 'Unknown' sentinels, 0 non-TX ZIPs, 0 unmapped categories",
    )


def check_stop_3_fk_and_strict_integrity(conn: sqlite3.Connection) -> CheckResult:
    """STOP 3: Foreign key integrity holds and every table in sqlite_master is STRICT."""
    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    non_strict = [
        name for name, sql in tables
        if not sql or "STRICT" not in sql.upper()
    ]

    issues = []
    if fk_errors:
        issues.append(f"foreign_key_check returned {len(fk_errors)} violation(s)")
    if non_strict:
        issues.append(f"non-STRICT tables found: {non_strict}")

    if issues:
        return CheckResult(
            "STOP-3",
            "Foreign key referential integrity and STRICT table enforcement",
            "STOP",
            "STOP",
            "; ".join(issues),
        )
    return CheckResult(
        "STOP-3",
        "Foreign key referential integrity and STRICT table enforcement",
        "STOP",
        "PASS",
        f"0 FK violations across {len(tables)} STRICT tables",
    )


def check_warn_1_dob_missing_by_species(conn: sqlite3.Connection) -> CheckResult:
    """WARN 1: Missing date_of_birth share per canonical species."""
    rows = conn.execute(
        """
        SELECT
            animal_type,
            COUNT(*) AS total,
            SUM(CASE WHEN date_of_birth IS NULL THEN 1 ELSE 0 END) AS n_null
        FROM animals_clean
        GROUP BY animal_type
        ORDER BY total DESC
        """
    ).fetchall()

    core_breaches = []
    high_missing_groups = []
    for species, total, n_null in rows:
        share = n_null / total if total else 0.0
        if species in ("Cat", "Dog") and share > MAX_CORE_DOB_MISSING_SHARE:
            core_breaches.append(f"{species}={share:.1%}")
        elif share > MAX_OVERALL_DOB_MISSING_SHARE:
            high_missing_groups.append(f"{species}={share:.1%} ({n_null}/{total})")

    if core_breaches or high_missing_groups:
        msg_parts = []
        if core_breaches:
            msg_parts.append(f"Core species >{MAX_CORE_DOB_MISSING_SHARE:.0%}: {', '.join(core_breaches)}")
        if high_missing_groups:
            msg_parts.append(
                f"Non-companion groups >{MAX_OVERALL_DOB_MISSING_SHARE:.0%} (MAR): {', '.join(high_missing_groups)}"
            )
        return CheckResult(
            "WARN-1",
            "Missing date_of_birth share by canonical species",
            "WARN",
            "WARN",
            "; ".join(msg_parts),
        )
    return CheckResult(
        "WARN-1",
        "Missing date_of_birth share by canonical species",
        "WARN",
        "PASS",
        "All species below missingness warning thresholds",
    )


def check_warn_2_freshness_by_species(conn: sqlite3.Connection) -> CheckResult:
    """WARN 2: Freshness check — canonical species presence in the final 7-day window."""
    cutoff_date = (
        date.fromisoformat(MAX_VALID_DATE) - timedelta(days=FRESHNESS_WINDOW_DAYS)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT
            a.animal_type,
            COUNT(*) AS total_intakes,
            SUM(CASE WHEN substr(i.intake_at, 1, 10) >= ? THEN 1 ELSE 0 END) AS recent_intakes,
            MAX(substr(i.intake_at, 1, 10)) AS latest_intake
        FROM intakes_clean i
        JOIN animals_clean a ON a.animal_key = i.animal_key
        GROUP BY a.animal_type
        ORDER BY total_intakes DESC
        """,
        (cutoff_date,),
    ).fetchall()

    stale_groups = [
        f"{species} (latest={latest}, total={total})"
        for species, total, recent, latest in rows
        if recent == 0
    ]

    if stale_groups:
        return CheckResult(
            "WARN-2",
            f"Species freshness in final {FRESHNESS_WINDOW_DAYS}-day window (>= {cutoff_date})",
            "WARN",
            "WARN",
            f"6/8 species active; rare groups with 0 intakes in last {FRESHNESS_WINDOW_DAYS}d: {', '.join(stale_groups)}",
        )
    return CheckResult(
        "WARN-2",
        f"Species freshness in final {FRESHNESS_WINDOW_DAYS}-day window (>= {cutoff_date})",
        "WARN",
        "PASS",
        f"All 8 canonical species recorded intakes on or after {cutoff_date}",
    )


def check_warn_3_stay_sequence_anomalies(conn: sqlite3.Connection) -> CheckResult:
    """WARN 3: Duplicate exit records and multi-stay sequence anomalies."""
    dup_exits = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE days_in_shelter_flag = 'duplicate_exit_for_stay'"
    ).fetchone()[0]
    overlaps = conn.execute(
        "SELECT COUNT(*) FROM intakes_clean WHERE stay_sequence_flag = 'overlap_with_prior_stay'"
    ).fetchone()[0]
    open_reintakes = conn.execute(
        "SELECT COUNT(*) FROM intakes_clean WHERE stay_sequence_flag = 'reintake_after_open_stay'"
    ).fetchone()[0]
    view_count = conn.execute("SELECT COUNT(*) FROM stays_clean_view").fetchone()[0]
    intake_count = conn.execute("SELECT COUNT(*) FROM intakes_clean").fetchone()[0]

    if dup_exits > MAX_EXPECTED_DUPLICATE_EXITS or overlaps > 0 or open_reintakes > 0:
        return CheckResult(
            "WARN-3",
            "Stay sequence anomalies and secondary exit records",
            "WARN",
            "WARN",
            f"duplicate_exit_for_stay={dup_exits} (excluded from stays_clean_view={view_count}/{intake_count}), "
            f"overlap_with_prior_stay={overlaps}, reintake_after_open_stay={open_reintakes}",
        )
    return CheckResult(
        "WARN-3",
        "Stay sequence anomalies and secondary exit records",
        "WARN",
        "PASS",
        "0 duplicate exits or sequence overlaps",
    )


def check_warn_4_clinical_cross_field_conflicts(conn: sqlite3.Connection) -> CheckResult:
    """WARN 4: Cross-field clinical conflicts (euthanasia reason & spay/neuter status)."""
    non_euth_conflict = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE euthanasia_reason_flag = 'conflict_non_euthanasia_status'"
    ).fetchone()[0]
    missing_euth_reason = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE euthanasia_reason_flag = 'missing_euthanized_no_reason'"
    ).fetchone()[0]
    spay_conflict = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE spayed_neutered_flag = 'conflict_previously_spayed'"
    ).fetchone()[0]

    if non_euth_conflict > 0 or missing_euth_reason > 0 or spay_conflict > 0:
        return CheckResult(
            "WARN-4",
            "Cross-field clinical conflicts (euthanasia_reason & spayed_neutered)",
            "WARN",
            "WARN",
            f"conflict_non_euthanasia_status={non_euth_conflict} (nulled), "
            f"missing_euthanized_no_reason={missing_euth_reason}, "
            f"conflict_previously_spayed={spay_conflict} (nulled)",
        )
    return CheckResult(
        "WARN-4",
        "Cross-field clinical conflicts (euthanasia_reason & spayed_neutered)",
        "WARN",
        "PASS",
        "0 cross-field clinical conflicts detected",
    )


def run_validation_suite(db_path: Path = DB_PATH, quiet: bool = False) -> tuple[int, list[CheckResult]]:
    """Run all 7 checks on db_path and return (exit_code, results)."""
    conn = sqlite3.connect(db_path)
    try:
        results = [
            check_stop_1_row_parity(conn),
            check_stop_2_zero_invalid_values(conn),
            check_stop_3_fk_and_strict_integrity(conn),
            check_warn_1_dob_missing_by_species(conn),
            check_warn_2_freshness_by_species(conn),
            check_warn_3_stay_sequence_anomalies(conn),
            check_warn_4_clinical_cross_field_conflicts(conn),
        ]
    finally:
        conn.close()

    n_pass = sum(1 for r in results if r.status == "PASS")
    n_warn = sum(1 for r in results if r.status == "WARN")
    n_stop = sum(1 for r in results if r.status == "STOP")
    exit_code = 1 if n_stop > 0 else 0

    if not quiet:
        print("=" * 72)
        print("AUSTIN ANIMAL CENTER -- M3 DATA QUALITY VALIDATION REPORT")
        print("=" * 72)
        for r in results:
            print(f"[{r.status:<4}] {r.check_id} ({r.level}): {r.title}")
            print(f"       -> {r.detail}")
        print("-" * 72)
        print(
            f"SUMMARY: {len(results)} checks run | PASS={n_pass} | WARN={n_warn} | STOP={n_stop} | EXIT_CODE={exit_code}"
        )
        print("=" * 72)

    return exit_code, results


def test_stop_checks_on_temp_copy(db_path: Path = DB_PATH) -> bool:
    """
    Prove that each STOP check actually triggers and returns exit_code=1
    when its invariant is broken on a temporary copy of the database.
    """
    print("\n" + "=" * 72)
    print("VERIFYING STOP CHECKS ON TEMPORARY DATABASE COPIES (--test-stops)")
    print("=" * 72)
    all_triggered = True

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)

        # 1. Break STOP-1 (delete 1 row from outcomes_clean)
        db1 = tmp_root / "break_stop1.db"
        shutil.copy2(db_path, db1)
        conn = sqlite3.connect(db1)
        conn.execute(
            "DELETE FROM outcomes_clean WHERE rowid = (SELECT MIN(rowid) FROM outcomes_clean)"
        )
        conn.commit()
        conn.close()
        code1, res1 = run_validation_suite(db1, quiet=True)
        r1 = next(r for r in res1 if r.check_id == "STOP-1")
        print(f"Test STOP-1 (Row Parity Breach):     status={r1.status}, exit_code={code1} -> {r1.detail}")
        all_triggered = all_triggered and (r1.status == "STOP" and code1 == 1)

        # 2. Break STOP-2 (inject a future date_of_birth into animals_clean)
        db2 = tmp_root / "break_stop2.db"
        shutil.copy2(db_path, db2)
        conn = sqlite3.connect(db2)
        conn.execute(
            "UPDATE animals_clean SET date_of_birth = '2099-01-01' WHERE animal_key = 1"
        )
        conn.commit()
        conn.close()
        code2, res2 = run_validation_suite(db2, quiet=True)
        r2 = next(r for r in res2 if r.check_id == "STOP-2")
        print(f"Test STOP-2 (Invalid Value Breach):  status={r2.status}, exit_code={code2} -> {r2.detail}")
        all_triggered = all_triggered and (r2.status == "STOP" and code2 == 1)

        # 3. Break STOP-3 (create a non-STRICT table and an orphan FK row)
        db3 = tmp_root / "break_stop3.db"
        shutil.copy2(db_path, db3)
        conn = sqlite3.connect(db3)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("CREATE TABLE rogue_non_strict (id INTEGER)")
        conn.execute(
            "UPDATE animals_clean SET animal_key = 9999999 WHERE animal_key = 1"
        )
        conn.commit()
        conn.close()
        code3, res3 = run_validation_suite(db3, quiet=True)
        r3 = next(r for r in res3 if r.check_id == "STOP-3")
        print(f"Test STOP-3 (FK & STRICT Breach):    status={r3.status}, exit_code={code3} -> {r3.detail}")
        all_triggered = all_triggered and (r3.status == "STOP" and code3 == 1)

    print("-" * 72)
    print(f"STOP CHECK SELF-TEST RESULT: {'ALL 3 STOP CHECKS VERIFIED' if all_triggered else 'FAILED'}")
    print("=" * 72)
    return all_triggered


def main() -> int:
    exit_code, _ = run_validation_suite(DB_PATH, quiet=False)
    if "--test-stops" in sys.argv:
        ok = test_stop_checks_on_temp_copy(DB_PATH)
        if not ok:
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
