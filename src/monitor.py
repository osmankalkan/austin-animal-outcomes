"""
Milestone M5 — Read-Only External Pipeline Monitor (`src/monitor.py`).

Checks the health of the scheduled Austin Animal Center pipeline from outside
the runner by reading `data/pipeline_log.db` and `data/austin_animals.db`
strictly read-only (`file:...?mode=ro`, `uri=True`, `PRAGMA query_only = ON`).

CLI:
    python src/monitor.py [--now 2026-11-24T12:00:00Z]

Exit codes:
    0 — All 4 checks PASS.
    1 — One or more checks raise ALARM (or a database is missing/unreadable).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db import get_run_time  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_LOG_DB = ROOT_DIR / "data" / "pipeline_log.db"
DEFAULT_DATA_DB = ROOT_DIR / "data" / "austin_animals.db"

# ---------------------------------------------------------------------------
# Named Monitoring Thresholds (Schedule Interval = 6 Hours)
# ---------------------------------------------------------------------------
# 2x the 6-hour scheduled interval: allows one missed/delayed slot before alarming.
MAX_HOURS_SINCE_SUCCESS = 12.0

# Catches 2+ consecutive missed 6-hour runs anywhere in the lookback window.
MAX_GAP_HOURS = 12.0

# Rolling lookback window (days) for checking inter-run gaps in pipeline_log.db.
GAP_LOOKBACK_DAYS = 7

# Rolling lookback window (complete UTC days before --now) for daily species completeness.
COMPLETENESS_DAYS = 7

# Core companion species expected to record intakes every single day.
EXPECTED_CORE_SPECIES = ("Cat", "Dog")

# Austin Animal Center portal updates daily; >48h behind --now means the feed stopped.
MAX_FRESHNESS_HOURS = 48.0

# Full scheduled run executes all 4 steps: ingest -> load -> clean -> validate.
EXPECTED_FULL_RUN_STEPS = 4

# ---------------------------------------------------------------------------
# Parameterised Read-Only SQL Queries (zero string formatting)
# ---------------------------------------------------------------------------
SQL_LAST_SUCCESS = """
SELECT MAX(run_time) AS last_success
FROM (
    SELECT run_id, run_time
    FROM pipeline_run
    WHERE run_time <= :now
    GROUP BY run_id, run_time
    HAVING COUNT(*) = :expected_steps
       AND SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) = :expected_steps
);
"""

SQL_RUN_GAPS = """
WITH distinct_runs AS (
    SELECT DISTINCT run_id, run_time
    FROM pipeline_run
    WHERE run_time >= :since
      AND run_time <= :now
),
ordered_runs AS (
    SELECT
        run_id,
        run_time,
        LAG(run_time) OVER (ORDER BY run_time) AS prev_run_time
    FROM distinct_runs
)
SELECT
    prev_run_time,
    run_time,
    ROUND((julianday(run_time) - julianday(prev_run_time)) * 24.0, 2) AS gap_hours
FROM ordered_runs
WHERE prev_run_time IS NOT NULL
ORDER BY gap_hours DESC;
"""

SQL_COUNT_RECENT_RUNS = """
SELECT COUNT(DISTINCT run_id)
FROM pipeline_run
WHERE run_time >= :since
  AND run_time <= :now;
"""

SQL_COMPLETENESS = """
WITH RECURSIVE cal(day) AS (
    SELECT :start_day
    UNION ALL
    SELECT date(day, '+1 day')
    FROM cal
    WHERE day < :end_day
),
species(animal_type) AS (
    VALUES (:sp1), (:sp2)
),
daily_counts AS (
    SELECT
        substr(i.intake_at, 1, 10) AS day,
        a.animal_type,
        COUNT(*) AS n_intakes
    FROM intakes_clean i
    JOIN animals_clean a ON a.animal_key = i.animal_key
    WHERE substr(i.intake_at, 1, 10) BETWEEN :start_day AND :end_day
      AND a.animal_type IN (:sp1, :sp2)
    GROUP BY substr(i.intake_at, 1, 10), a.animal_type
)
SELECT
    c.day,
    s.animal_type,
    COALESCE(d.n_intakes, 0) AS n_intakes
FROM cal c
CROSS JOIN species s
LEFT JOIN daily_counts d
       ON d.day = c.day
      AND d.animal_type = s.animal_type
WHERE COALESCE(d.n_intakes, 0) = 0
ORDER BY c.day, s.animal_type;
"""

SQL_FRESHNESS_BY_SPECIES = """
SELECT
    a.animal_type,
    MAX(i.intake_at) AS latest_intake_at
FROM intakes_clean i
JOIN animals_clean a ON a.animal_key = i.animal_key
WHERE a.animal_type IN (:sp1, :sp2)
  AND i.intake_at <= :now
GROUP BY a.animal_type;
"""


def _parse_utc_iso(ts_iso: str) -> datetime:
    cleaned = ts_iso.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Opens a SQLite database strictly read-only (`mode=ro`, `uri=True`, `PRAGMA query_only = ON`)."""
    resolved = db_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Database file not found: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def check_1_last_success(log_con: sqlite3.Connection, now_iso: str) -> tuple[str, str]:
    """Check 1: Most recent full 4-step successful run is within MAX_HOURS_SINCE_SUCCESS of --now."""
    row = log_con.execute(
        SQL_LAST_SUCCESS,
        {"now": now_iso, "expected_steps": EXPECTED_FULL_RUN_STEPS},
    ).fetchone()
    last_ts = row[0] if row else None
    if not last_ts:
        return "ALARM", f"no successful {EXPECTED_FULL_RUN_STEPS}-step run found at or before {now_iso}"

    age_h = (_parse_utc_iso(now_iso) - _parse_utc_iso(last_ts)).total_seconds() / 3600.0
    if age_h > MAX_HOURS_SINCE_SUCCESS:
        return (
            "ALARM",
            f"last full success {last_ts} is {age_h:.2f}h before --now (limit={MAX_HOURS_SINCE_SUCCESS:.1f}h)",
        )
    return (
        "PASS",
        f"last full success {last_ts} ({age_h:.2f}h ago <= {MAX_HOURS_SINCE_SUCCESS:.1f}h)",
    )


def check_2_gaps(log_con: sqlite3.Connection, now_iso: str) -> tuple[str, str]:
    """Check 2: No inter-run gap > MAX_GAP_HOURS over the last GAP_LOOKBACK_DAYS."""
    since_iso = (_parse_utc_iso(now_iso) - timedelta(days=GAP_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    n_runs = log_con.execute(
        SQL_COUNT_RECENT_RUNS,
        {"since": since_iso, "now": now_iso},
    ).fetchone()[0]
    if n_runs == 0:
        return "ALARM", f"0 runs logged in the last {GAP_LOOKBACK_DAYS} days (since {since_iso})"

    rows = log_con.execute(
        SQL_RUN_GAPS,
        {"since": since_iso, "now": now_iso},
    ).fetchall()
    bad_gaps = [(prev_ts, cur_ts, gap_h) for prev_ts, cur_ts, gap_h in rows if gap_h > MAX_GAP_HOURS]
    if bad_gaps:
        worst_prev, worst_cur, worst_h = bad_gaps[0]
        return (
            "ALARM",
            f"{len(bad_gaps)} gap(s) > {MAX_GAP_HOURS:.1f}h in last {GAP_LOOKBACK_DAYS}d "
            f"(worst={worst_h:.2f}h between {worst_prev} and {worst_cur})",
        )
    max_gap = rows[0][2] if rows else 0.0
    return (
        "PASS",
        f"{n_runs} run(s) in last {GAP_LOOKBACK_DAYS}d; max gap={max_gap:.2f}h (limit={MAX_GAP_HOURS:.1f}h)",
    )


def check_3_completeness(data_con: sqlite3.Connection, now_iso: str) -> tuple[str, str]:
    """Check 3: Every complete calendar day in the last COMPLETENESS_DAYS has Cat & Dog intakes."""
    now_date = _parse_utc_iso(now_iso).date()
    end_day = (now_date - timedelta(days=1)).isoformat()
    start_day = (now_date - timedelta(days=COMPLETENESS_DAYS)).isoformat()

    missing = data_con.execute(
        SQL_COMPLETENESS,
        {
            "start_day": start_day,
            "end_day": end_day,
            "sp1": EXPECTED_CORE_SPECIES[0],
            "sp2": EXPECTED_CORE_SPECIES[1],
        },
    ).fetchall()

    if missing:
        sample = ", ".join(f"{day}/{sp}" for day, sp, _ in missing[:6])
        suffix = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
        return (
            "ALARM",
            f"{len(missing)} missing day-species slot(s) in [{start_day}..{end_day}]: {sample}{suffix}",
        )
    return (
        "PASS",
        f"all {COMPLETENESS_DAYS} days [{start_day}..{end_day}] complete for {', '.join(EXPECTED_CORE_SPECIES)}",
    )


def check_4_freshness(data_con: sqlite3.Connection, now_iso: str) -> tuple[str, str]:
    """Check 4: Every core species' MAX(intake_at) is within MAX_FRESHNESS_HOURS of --now."""
    now_dt = _parse_utc_iso(now_iso)
    rows = data_con.execute(
        SQL_FRESHNESS_BY_SPECIES,
        {
            "sp1": EXPECTED_CORE_SPECIES[0],
            "sp2": EXPECTED_CORE_SPECIES[1],
            "now": now_iso,
        },
    ).fetchall()
    latest_by_sp = {sp: ts for sp, ts in rows}

    stale = []
    ok_parts = []
    for sp in EXPECTED_CORE_SPECIES:
        ts = latest_by_sp.get(sp)
        if not ts:
            stale.append(f"{sp}=MISSING")
        else:
            lag_h = (now_dt - _parse_utc_iso(ts)).total_seconds() / 3600.0
            ok_parts.append(f"{sp}={lag_h:.2f}h ({ts})")
            if lag_h > MAX_FRESHNESS_HOURS:
                stale.append(f"{sp}={lag_h:.2f}h > {MAX_FRESHNESS_HOURS:.0f}h (latest={ts})")

    if stale:
        return "ALARM", "; ".join(stale)
    return "PASS", f"all core species within {MAX_FRESHNESS_HOURS:.0f}h of {now_iso}: " + ", ".join(ok_parts)


def run_monitor(
    now_iso: str,
    log_db_path: Path = DEFAULT_LOG_DB,
    data_db_path: Path = DEFAULT_DATA_DB,
    log_file: Path | None = None,
) -> int:
    """Runs all 4 read-only monitor checks and returns 0 (all PASS) or 1 (any ALARM)."""
    output_lines: list[str] = []

    def emit_line(msg: str) -> None:
        print(msg)
        output_lines.append(msg)

    def flush_log() -> None:
        if log_file is None:
            return
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"=== monitor run_now={now_iso} ===\n")
            for ln in output_lines:
                fh.write(ln + "\n")

    try:
        log_con = connect_readonly(log_db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        emit_line(f"[ALARM] monitor_init: cannot open log DB read-only ({exc})")
        flush_log()
        return 1

    try:
        data_con = connect_readonly(data_db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        log_con.close()
        emit_line(f"[ALARM] monitor_init: cannot open data DB read-only ({exc})")
        flush_log()
        return 1

    try:
        checks = [
            ("check_1_last_success", *check_1_last_success(log_con, now_iso)),
            ("check_2_gaps", *check_2_gaps(log_con, now_iso)),
            ("check_3_completeness", *check_3_completeness(data_con, now_iso)),
            ("check_4_freshness", *check_4_freshness(data_con, now_iso)),
        ]
    finally:
        log_con.close()
        data_con.close()

    alarms = 0
    for name, status, detail in checks:
        emit_line(f"[{status:<5}] {name}: {detail}")
        if status == "ALARM":
            alarms += 1

    flush_log()
    return 1 if alarms > 0 else 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only external monitor for the Austin Animal Center pipeline."
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Reference UTC timestamp in ISO 8601 format (e.g. 2026-11-24T12:00:00Z). Defaults to current UTC.",
    )
    parser.add_argument(
        "--log-db",
        default=str(DEFAULT_LOG_DB),
        help="Path to pipeline_log.db (opened strictly read-only).",
    )
    parser.add_argument(
        "--data-db",
        default=str(DEFAULT_DATA_DB),
        help="Path to austin_animals.db (opened strictly read-only).",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to append monitor output (e.g. logs/monitor.log for scheduled runs).",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        now_iso = get_run_time(args.now)
    except ValueError as exc:
        print(f"[ALARM] invalid --now: {exc}")
        return 1
    log_file = Path(args.log_file) if args.log_file else None
    return run_monitor(
        now_iso=now_iso,
        log_db_path=Path(args.log_db),
        data_db_path=Path(args.data_db),
        log_file=log_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
