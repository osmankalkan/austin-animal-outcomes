"""
Milestone M3 & M5 — Automated Data Quality Validation Suite (`src/validate.py`).

Runs 8 data quality checks (3 STOP, 2 operational WARN, 3 regression-guarded INFO/WARN)
against the cleaned SQLite layer using the shared pipeline clock (`--run-time` ->
`PIPELINE_RUN_TIME` -> current UTC):
- Exits 0 when all checks are PASS, INFO, or WARN.
- Exits 1 when any check is STOP (or if the database is missing/unreadable).
- Healthy scheduled runs produce STOP=0 and WARN=0 (historical structural M3
  observations are logged as INFO unless they regress past alert thresholds).
- Pass `--test-stops` to verify on temporary database copies that every STOP
  check reliably triggers and returns exit_code=1 when broken.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.clean_config import (  # noqa: E402
    MAX_CORE_DOB_MISSING_SHARE,
    MAX_OVERALL_DOB_MISSING_SHARE,
    MAX_VALID_DATE,
    TEXAS_ZIP_PREFIXES,
)
from src.db import connect, get_db_path, get_run_time  # noqa: E402

# ---------------------------------------------------------------------------
# M5 Operational Validation Thresholds (justified in docs/pipeline.md)
# ---------------------------------------------------------------------------
# Austin Animal Center portal updates daily; a feed or core species older than
# 48 hours relative to `run_time` indicates an upstream stall or broken ingestion.
MAX_FEED_FRESHNESS_HOURS = 48.0

# Core companion species expected to record arrivals every day (unlike rare
# Livestock/Other groups, which can legitimately have zero arrivals for weeks).
CORE_SPECIES = ("Cat", "Dog")

# Volume check window and multiplier: compare trailing 24h event volume ending
# at `run_time` against the trailing 30-day median daily volume; warn on 0 or >10x.
VOLUME_LOOKBACK_DAYS = 30
VOLUME_WINDOW_HOURS = 24.0
VOLUME_MAX_MULTIPLIER = 10.0

# Regression thresholds for historical M3 structural anomalies (below these
# thresholds, known source quirks are reported as INFO rather than permanent WARNs).
MAX_DUPLICATE_EXIT_SHARE = 0.01     # 1.0% of outcomes (baseline ~0.34%)
MAX_SEQUENCE_ANOMALY_SHARE = 0.005  # 0.5% of intakes (baseline ~0.07%)
MAX_CLINICAL_CONFLICT_SHARE = 0.02  # 2.0% of outcomes (baseline ~0.86%)


def _parse_utc_iso(ts_iso: str) -> datetime:
    cleaned = ts_iso.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CheckResult:
    """Container for a single validation check outcome."""

    def __init__(self, check_id: str, title: str, level: str, status: str, detail: str) -> None:
        self.check_id = check_id
        self.title = title
        self.level = level      # "STOP", "WARN", or "INFO"
        self.status = status    # "PASS", "INFO", "WARN", or "STOP"
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


def check_stop_2_zero_invalid_values(conn: sqlite3.Connection, run_time: str | None = None) -> CheckResult:
    """STOP 2: Zero impossible or uncleaned sentinel values survive in cleaned columns."""
    run_date = get_run_time(run_time)[:10]
    max_ret_date = conn.execute(
        "SELECT COALESCE(MAX(substr(retrieved_at, 1, 10)), ?) FROM animals_clean",
        (MAX_VALID_DATE,),
    ).fetchone()[0]
    effective_max_date = max(MAX_VALID_DATE, run_date, max_ret_date)
    future_dob = conn.execute(
        "SELECT COUNT(*) FROM animals_clean WHERE date_of_birth IS NOT NULL AND date_of_birth > ?",
        (effective_max_date,),
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


def check_warn_freshness_from_run_time(conn: sqlite3.Connection, run_time: str) -> CheckResult:
    """
    WARN 1 (M5): Run-time freshness check for whole feed (intakes + outcomes)
    and per core companion species (Cat, Dog) measured against `run_time`.
    """
    rt_dt = _parse_utc_iso(run_time)
    max_intake = conn.execute(
        "SELECT MAX(intake_at) FROM intakes_clean WHERE intake_at <= ?",
        (run_time,),
    ).fetchone()[0]
    max_outcome = conn.execute(
        "SELECT MAX(outcome_at) FROM outcomes_clean WHERE outcome_at <= ?",
        (run_time,),
    ).fetchone()[0]

    species_rows = conn.execute(
        """
        SELECT a.animal_type, MAX(i.intake_at) AS latest_intake
        FROM intakes_clean i
        JOIN animals_clean a ON a.animal_key = i.animal_key
        WHERE a.animal_type IN (?, ?)
          AND i.intake_at <= ?
        GROUP BY a.animal_type
        """,
        (CORE_SPECIES[0], CORE_SPECIES[1], run_time),
    ).fetchall()
    species_latest = {sp: ts for sp, ts in species_rows}

    stale_parts = []
    lag_summary = []

    for label, ts in (("intakes", max_intake), ("outcomes", max_outcome)):
        if not ts:
            stale_parts.append(f"{label}=MISSING")
        else:
            lag_h = (rt_dt - _parse_utc_iso(ts)).total_seconds() / 3600.0
            lag_summary.append(f"{label}={lag_h:.1f}h")
            if lag_h > MAX_FEED_FRESHNESS_HOURS:
                stale_parts.append(f"{label} lag {lag_h:.1f}h > {MAX_FEED_FRESHNESS_HOURS:.0f}h (latest={ts})")

    for sp in CORE_SPECIES:
        ts = species_latest.get(sp)
        if not ts:
            stale_parts.append(f"{sp}=MISSING")
        else:
            lag_h = (rt_dt - _parse_utc_iso(ts)).total_seconds() / 3600.0
            lag_summary.append(f"{sp}={lag_h:.1f}h")
            if lag_h > MAX_FEED_FRESHNESS_HOURS:
                stale_parts.append(f"{sp} lag {lag_h:.1f}h > {MAX_FEED_FRESHNESS_HOURS:.0f}h (latest={ts})")

    if stale_parts:
        return CheckResult(
            "WARN-1",
            f"Run-time freshness (whole feed & core species within {MAX_FEED_FRESHNESS_HOURS:.0f}h of run_time)",
            "WARN",
            "WARN",
            "Stale relative to run_time=" + run_time + ": " + "; ".join(stale_parts),
        )
    return CheckResult(
        "WARN-1",
        f"Run-time freshness (whole feed & core species within {MAX_FEED_FRESHNESS_HOURS:.0f}h of run_time)",
        "WARN",
        "PASS",
        f"Fresh relative to {run_time}: " + ", ".join(lag_summary),
    )


def check_warn_volume_anomaly(conn: sqlite3.Connection, run_time: str) -> CheckResult:
    """
    WARN 2 (M5): Volume check comparing recent 24h intake & outcome volume
    (ending at `run_time`) against the trailing 30-day median daily volume.
    Warns if recent volume is 0 or > 10x typical daily volume.
    Skips cleanly with PASS/INFO if the database is a static rebuild whose
    latest retrieved_at is older than `run_time` by more than 48h.
    """
    rt_dt = _parse_utc_iso(run_time)
    window_start = (rt_dt - timedelta(hours=VOLUME_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lookback_start_date = (rt_dt - timedelta(days=VOLUME_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    run_date = rt_dt.strftime("%Y-%m-%d")

    max_ret = conn.execute(
        "SELECT MAX(retrieved_at) FROM intakes_clean"
    ).fetchone()[0]
    if max_ret:
        ret_lag_h = (rt_dt - _parse_utc_iso(max_ret)).total_seconds() / 3600.0
        if ret_lag_h > MAX_FEED_FRESHNESS_HOURS:
            return CheckResult(
                "WARN-2",
                f"Recent 24h intake/outcome volume vs {VOLUME_LOOKBACK_DAYS}d median daily baseline",
                "WARN",
                "INFO",
                f"Static rebuild detected (latest retrieved_at={max_ret} is {ret_lag_h:.1f}h before run_time); volume check skipped",
            )

    daily_intake_rows = conn.execute(
        """
        SELECT substr(intake_at, 1, 10) AS d, COUNT(*) AS n
        FROM intakes_clean
        WHERE substr(intake_at, 1, 10) >= ?
          AND substr(intake_at, 1, 10) < ?
        GROUP BY d
        """,
        (lookback_start_date, run_date),
    ).fetchall()
    daily_outcome_rows = conn.execute(
        """
        SELECT substr(outcome_at, 1, 10) AS d, COUNT(*) AS n
        FROM outcomes_clean
        WHERE substr(outcome_at, 1, 10) >= ?
          AND substr(outcome_at, 1, 10) < ?
        GROUP BY d
        """,
        (lookback_start_date, run_date),
    ).fetchall()

    recent_intakes = conn.execute(
        "SELECT COUNT(*) FROM intakes_clean WHERE intake_at > ? AND intake_at <= ?",
        (window_start, run_time),
    ).fetchone()[0]
    recent_outcomes = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE outcome_at > ? AND outcome_at <= ?",
        (window_start, run_time),
    ).fetchone()[0]

    med_intakes = statistics.median([n for _, n in daily_intake_rows]) if daily_intake_rows else 0.0
    med_outcomes = statistics.median([n for _, n in daily_outcome_rows]) if daily_outcome_rows else 0.0

    anomalies = []
    if recent_intakes == 0:
        anomalies.append(f"intakes_24h=0 (median={med_intakes:.1f}/d)")
    elif med_intakes > 0 and recent_intakes > VOLUME_MAX_MULTIPLIER * med_intakes:
        anomalies.append(f"intakes_24h={recent_intakes} > {VOLUME_MAX_MULTIPLIER:.0f}x median ({med_intakes:.1f}/d)")

    if recent_outcomes == 0:
        anomalies.append(f"outcomes_24h=0 (median={med_outcomes:.1f}/d)")
    elif med_outcomes > 0 and recent_outcomes > VOLUME_MAX_MULTIPLIER * med_outcomes:
        anomalies.append(f"outcomes_24h={recent_outcomes} > {VOLUME_MAX_MULTIPLIER:.0f}x median ({med_outcomes:.1f}/d)")

    if anomalies:
        return CheckResult(
            "WARN-2",
            f"Recent 24h intake/outcome volume vs {VOLUME_LOOKBACK_DAYS}d median daily baseline",
            "WARN",
            "WARN",
            "Volume anomaly detected: " + "; ".join(anomalies),
        )
    return CheckResult(
        "WARN-2",
        f"Recent 24h intake/outcome volume vs {VOLUME_LOOKBACK_DAYS}d median daily baseline",
        "WARN",
        "PASS",
        f"intakes_24h={recent_intakes} (30d median={med_intakes:.1f}/d), "
        f"outcomes_24h={recent_outcomes} (30d median={med_outcomes:.1f}/d)",
    )


def check_info_1_dob_missing_by_species(conn: sqlite3.Connection) -> CheckResult:
    """
    INFO 1 (regression-guarded): Missing date_of_birth share per canonical species.
    Raises WARN only if core companion species (Cat, Dog) exceed MAX_CORE_DOB_MISSING_SHARE (10%);
    expected non-companion MAR missingness is reported as INFO (not a permanent WARN).
    """
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
    non_companion_mar = []
    for species, total, n_null in rows:
        share = n_null / total if total else 0.0
        if species in CORE_SPECIES and share > MAX_CORE_DOB_MISSING_SHARE:
            core_breaches.append(f"{species}={share:.1%}")
        elif species not in CORE_SPECIES and share > MAX_OVERALL_DOB_MISSING_SHARE:
            non_companion_mar.append(f"{species}={share:.1%} ({n_null}/{total})")

    if core_breaches:
        return CheckResult(
            "INFO-1",
            "Missing date_of_birth share by canonical species",
            "WARN",
            "WARN",
            f"Core species >{MAX_CORE_DOB_MISSING_SHARE:.0%}: {', '.join(core_breaches)}",
        )
    if non_companion_mar:
        return CheckResult(
            "INFO-1",
            "Missing date_of_birth share by canonical species",
            "INFO",
            "INFO",
            f"Core Cat/Dog <=10% OK; expected non-companion MAR >{MAX_OVERALL_DOB_MISSING_SHARE:.0%}: {', '.join(non_companion_mar)}",
        )
    return CheckResult(
        "INFO-1",
        "Missing date_of_birth share by canonical species",
        "INFO",
        "PASS",
        "All species below missingness thresholds",
    )


def check_info_2_stay_sequence_anomalies(conn: sqlite3.Connection) -> CheckResult:
    """
    INFO 2 (regression-guarded): Duplicate exit records and multi-stay sequence anomalies.
    Raises WARN only if rates regress above MAX_DUPLICATE_EXIT_SHARE or MAX_SEQUENCE_ANOMALY_SHARE.
    """
    dup_exits = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE days_in_shelter_flag = 'duplicate_exit_for_stay'"
    ).fetchone()[0]
    overlaps = conn.execute(
        "SELECT COUNT(*) FROM intakes_clean WHERE stay_sequence_flag = 'overlap_with_prior_stay'"
    ).fetchone()[0]
    open_reintakes = conn.execute(
        "SELECT COUNT(*) FROM intakes_clean WHERE stay_sequence_flag = 'reintake_after_open_stay'"
    ).fetchone()[0]
    outcome_count = conn.execute("SELECT COUNT(*) FROM outcomes_clean").fetchone()[0]
    intake_count = conn.execute("SELECT COUNT(*) FROM intakes_clean").fetchone()[0]

    dup_share = dup_exits / outcome_count if outcome_count else 0.0
    seq_share = (overlaps + open_reintakes) / intake_count if intake_count else 0.0

    detail = (
        f"duplicate_exit_for_stay={dup_exits} ({dup_share:.2%}), "
        f"overlap_with_prior_stay={overlaps}, reintake_after_open_stay={open_reintakes} ({seq_share:.2%})"
    )
    if dup_share > MAX_DUPLICATE_EXIT_SHARE or seq_share > MAX_SEQUENCE_ANOMALY_SHARE:
        return CheckResult(
            "INFO-2",
            "Stay sequence anomalies and secondary exit records",
            "WARN",
            "WARN",
            "Regression above threshold: " + detail,
        )
    return CheckResult(
        "INFO-2",
        "Stay sequence anomalies and secondary exit records",
        "INFO",
        "INFO",
        "Within historical baseline (excluded from stays_clean_view): " + detail,
    )


def check_info_3_clinical_cross_field_conflicts(conn: sqlite3.Connection) -> CheckResult:
    """
    INFO 3 (regression-guarded): Cross-field clinical conflicts (euthanasia reason & spay/neuter).
    Raises WARN only if combined share exceeds MAX_CLINICAL_CONFLICT_SHARE (2.0%).
    """
    non_euth_conflict = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE euthanasia_reason_flag = 'conflict_non_euthanasia_status'"
    ).fetchone()[0]
    missing_euth_reason = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE euthanasia_reason_flag = 'missing_euthanized_no_reason'"
    ).fetchone()[0]
    spay_conflict = conn.execute(
        "SELECT COUNT(*) FROM outcomes_clean WHERE spayed_neutered_flag = 'conflict_previously_spayed'"
    ).fetchone()[0]
    outcome_count = conn.execute("SELECT COUNT(*) FROM outcomes_clean").fetchone()[0]

    total_conflicts = non_euth_conflict + missing_euth_reason + spay_conflict
    conflict_share = total_conflicts / outcome_count if outcome_count else 0.0

    detail = (
        f"conflict_non_euthanasia_status={non_euth_conflict} (nulled), "
        f"missing_euthanized_no_reason={missing_euth_reason}, "
        f"conflict_previously_spayed={spay_conflict} (nulled) [total={conflict_share:.2%}]"
    )
    if conflict_share > MAX_CLINICAL_CONFLICT_SHARE:
        return CheckResult(
            "INFO-3",
            "Cross-field clinical conflicts (euthanasia_reason & spayed_neutered)",
            "WARN",
            "WARN",
            "Regression above threshold: " + detail,
        )
    return CheckResult(
        "INFO-3",
        "Cross-field clinical conflicts (euthanasia_reason & spayed_neutered)",
        "INFO",
        "INFO",
        "Within historical baseline: " + detail,
    )


def run_validation_suite(
    db_path: Path | None = None,
    quiet: bool = False,
    run_time: str | None = None,
) -> tuple[int, list[CheckResult]]:
    """Run all 8 M5 validation checks on db_path and return (exit_code, results)."""
    resolved_path = Path(db_path) if db_path is not None else get_db_path()
    resolved_run_time = get_run_time(run_time)
    conn = connect(resolved_path, create=False)
    try:
        results = [
            check_stop_1_row_parity(conn),
            check_stop_2_zero_invalid_values(conn, run_time=resolved_run_time),
            check_stop_3_fk_and_strict_integrity(conn),
            check_warn_freshness_from_run_time(conn, run_time=resolved_run_time),
            check_warn_volume_anomaly(conn, run_time=resolved_run_time),
            check_info_1_dob_missing_by_species(conn),
            check_info_2_stay_sequence_anomalies(conn),
            check_info_3_clinical_cross_field_conflicts(conn),
        ]
    finally:
        conn.close()

    n_pass = sum(1 for r in results if r.status == "PASS")
    n_info = sum(1 for r in results if r.status == "INFO")
    n_warn = sum(1 for r in results if r.status == "WARN")
    n_stop = sum(1 for r in results if r.status == "STOP")
    exit_code = 1 if n_stop > 0 else 0

    if not quiet:
        print("=" * 72)
        print("AUSTIN ANIMAL CENTER -- M5 DATA QUALITY VALIDATION REPORT")
        print("=" * 72)
        print(f"database: {resolved_path} | run_time: {resolved_run_time}")
        for r in results:
            print(f"[{r.status:<4}] {r.check_id} ({r.level}): {r.title}")
            print(f"       -> {r.detail}")
        print("-" * 72)
        print(
            f"SUMMARY: {len(results)} checks run | PASS={n_pass} | INFO={n_info} | "
            f"WARN={n_warn} | STOP={n_stop} | EXIT_CODE={exit_code}"
        )
        print("=" * 72)

    return exit_code, results


def test_stop_checks_on_temp_copy(db_path: Path | None = None) -> bool:
    """
    Prove that each STOP check actually triggers and returns exit_code=1
    when its invariant is broken on a temporary copy of the database.
    """
    resolved_db = Path(db_path) if db_path is not None else get_db_path()
    print("\n" + "=" * 72)
    print("VERIFYING STOP CHECKS ON TEMPORARY DATABASE COPIES (--test-stops)")
    print("=" * 72)
    all_triggered = True

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)

        # 1. Break STOP-1 (delete 1 row from outcomes_clean)
        db1 = tmp_root / "break_stop1.db"
        shutil.copy2(resolved_db, db1)
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
        shutil.copy2(resolved_db, db2)
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
        shutil.copy2(resolved_db, db3)
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
    parser = argparse.ArgumentParser(description="Run M3/M5 automated data quality validation checks.")
    parser.add_argument(
        "--run-time",
        default=None,
        help="Optional ISO 8601 UTC pipeline run timestamp (defaults to PIPELINE_RUN_TIME or current UTC).",
    )
    parser.add_argument(
        "--test-stops",
        action="store_true",
        help="Verify on temporary database copies that all 3 STOP checks reliably trigger.",
    )
    args = parser.parse_args()
    try:
        resolved_rt = get_run_time(args.run_time)
        db_path = get_db_path()
        exit_code, _ = run_validation_suite(db_path, quiet=False, run_time=resolved_rt)
        if args.test_stops:
            ok = test_stop_checks_on_temp_copy(db_path)
            if not ok:
                return 1
        return exit_code
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"VALIDATION FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
