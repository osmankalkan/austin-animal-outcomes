"""
Milestone M1 & M5 Data Ingestion Pipeline (`src/ingest.py`).

Self-healing incremental ingestion with evidence-based overlap and source memory checks:
- Resolves shared pipeline clock (`--run-time` -> `PIPELINE_RUN_TIME` -> current UTC).
- Determines the last successful retrieval timestamp (`last_success`) from `data/pipeline_log.db`
  (`SELECT MAX(run_time) FROM pipeline_run WHERE step = 'src/ingest.py' AND exit_code = 0`),
  falling back to the latest landed raw filename timestamp in `data/raw/<source>/`.
- Queries the Socrata SODA API incrementally for records in
  `[last_success - OVERLAP_HOURS, run_time]` (or performs a full fetch when `--full` is passed
  or when `data/raw/<source>/` has no prior raw files).
- Checks the gap `(run_time - last_success)` against `SOURCE_MEMORY_DAYS` and exits non-zero
  if the gap exceeds the source's retention memory or if any auth/HTTP/empty-payload error occurs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Ensure the repository root is in sys.path when invoked via `python src/ingest.py`
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.client import DEFAULT_MAX_RECORDS_PER_RUN, SodaClient
from src.db import get_run_time
from src.storage import save_raw_json
from src.transform import retrieved_at_from_filename, to_utc_timestamp

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ---------------------------------------------------------------------------
# M5 Incremental Window, Catch-Up & Source Memory Constants
# ---------------------------------------------------------------------------
# Measured arrival delay across landed data/raw/ snapshots:
#   - Intakes frontier delay (retrieved_at - max(source_date_utc)): 0.83h to 0.97h
#     (max same-day late-posted intake litter delay between Oct-08 pulls: 4.58h).
#   - Outcomes frontier delay (retrieved_at - max(outcome_date_utc)): 1.63h to 13.24h
#     (overnight surgery/discharge batching gap before morning runs).
# Choosing OVERLAP_HOURS = 48 (2 full days) provides >3.6x safety margin over the 13.24h
# maximum frontier delay and >10x safety margin over same-day intake entry lag.
OVERLAP_HOURS = int(os.getenv("INGEST_OVERLAP_HOURS", 48))

# Austin Animal Center live SODA endpoints (pyqf-r2dc, gsvs-ypi7) retain active shelter
# records back to the May 4, 2025 software migration (~18+ months / 540 days).
# Any outage gap exceeding SOURCE_MEMORY_DAYS triggers a loud warning and non-zero exit.
SOURCE_MEMORY_DAYS = int(os.getenv("SOURCE_MEMORY_DAYS", 540))

# Maximum records fetched per endpoint per run (>2.5x full 18-month historical dataset).
MAX_RECORDS_PER_RUN = int(os.getenv("MAX_RECORDS_PER_RUN", DEFAULT_MAX_RECORDS_PER_RUN))

AUSTIN_LOCAL_TZ = ZoneInfo("America/Chicago")
LOG_DB_PATH = ROOT_DIR / "data" / "pipeline_log.db"

SELECT_LAST_INGEST_SUCCESS_SQL = """
SELECT MAX(run_time)
FROM pipeline_run
WHERE step = ?
  AND exit_code = 0
  AND run_time <= ?
"""


def parse_utc_iso(ts_iso: str) -> datetime:
    """Parses canonical ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`) into a timezone-aware UTC datetime."""
    return datetime.strptime(ts_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def to_socrata_local_floating(dt_utc: datetime) -> str:
    """
    Converts a UTC datetime to Austin local time (`America/Chicago`) formatted as
    a Socrata floating timestamp (`YYYY-MM-DDTHH:MM:SS.000`).
    """
    dt_local = dt_utc.astimezone(AUSTIN_LOCAL_TZ)
    return dt_local.strftime("%Y-%m-%dT%H:%M:%S.000")


def latest_raw_file_timestamp(raw_dir: Path, source_name: str, max_run_time: str | None = None) -> str | None:
    """Returns the latest canonical UTC `retrieved_at` timestamp from landed `data/raw/<source>/*.json`."""
    folder = raw_dir / source_name
    if not folder.is_dir():
        return None
    timestamps = []
    for path in sorted(folder.glob("*.json")):
        ts = retrieved_at_from_filename(path.name)
        if ts is not None:
            if max_run_time is None or ts <= max_run_time:
                timestamps.append(ts)
    if not timestamps:
        # If all landed files have timestamps after max_run_time, still return latest existing
        all_ts = [retrieved_at_from_filename(p.name) for p in sorted(folder.glob("*.json"))]
        all_ts = [t for t in all_ts if t is not None]
        return max(all_ts) if all_ts else None
    return max(timestamps)


def resolve_last_success(
    raw_dir: Path,
    source_name: str,
    run_time: str,
    log_db_path: Path = LOG_DB_PATH,
) -> tuple[str | None, str]:
    """
    Determines the last successful retrieval timestamp for `source_name`:
      1. If `data/raw/<source_name>/` has no `.json` files, returns `(None, "no_prior_raw_files")`
         so a full initial fetch is performed.
      2. Queries `data/pipeline_log.db` for `MAX(run_time)` where `step = 'src/ingest.py'` and `exit_code = 0`.
      3. Falls back to the latest landed raw filename timestamp in `data/raw/<source_name>/`.
    """
    latest_raw_ts = latest_raw_file_timestamp(raw_dir, source_name, max_run_time=run_time)
    if latest_raw_ts is None:
        return None, "no_prior_raw_files"

    log_ts: str | None = None
    if log_db_path.exists():
        con = sqlite3.connect(log_db_path)
        try:
            row = con.execute(
                SELECT_LAST_INGEST_SUCCESS_SQL,
                ("src/ingest.py", run_time),
            ).fetchone()
            if row and row[0]:
                log_ts = str(row[0])
        except sqlite3.OperationalError:
            log_ts = None
        finally:
            con.close()

    if log_ts is not None:
        # Use whichever is anchored to our actual landed raw data or logged run_time
        return log_ts, "pipeline_log.db"
    return latest_raw_ts, f"data/raw/{source_name}"


def measure_arrival_delays(raw_dir: Path) -> list[dict]:
    """
    Measures empirical publication/arrival delay (`retrieved_at - max(event_utc)` and
    10th-most-recent event lag) across all landed `data/raw/<source>/*.json` files.
    """
    source_fields = [("intakes", "source_date"), ("outcomes", "outcome_date")]
    rows = []
    for source_name, field in source_fields:
        folder = raw_dir / source_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            ret_iso = retrieved_at_from_filename(path.name)
            if ret_iso is None:
                continue
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(records, list) or not records:
                continue
            ret_dt = parse_utc_iso(ret_iso)
            event_dts = []
            for rec in records:
                if isinstance(rec, dict):
                    ts_utc = to_utc_timestamp(rec.get(field))
                    if ts_utc:
                        event_dts.append(parse_utc_iso(ts_utc))
            if not event_dts:
                continue
            event_dts.sort(reverse=True)
            max_event_dt = event_dts[0]
            p10_event_dt = event_dts[min(9, len(event_dts) - 1)]
            frontier_lag_h = round((ret_dt - max_event_dt).total_seconds() / 3600.0, 2)
            p10_lag_h = round((ret_dt - p10_event_dt).total_seconds() / 3600.0, 2)
            rows.append({
                "source": source_name,
                "file_name": path.name,
                "retrieved_at_utc": ret_iso,
                "n_records": len(records),
                "latest_event_utc": max_event_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "frontier_delay_hours": frontier_lag_h,
                "top10_recent_span_hours": p10_lag_h,
            })
    return rows


def print_arrival_delay_table(rows: list[dict]) -> None:
    """Prints the measured arrival-delay summary table for `docs/pipeline.md`."""
    print("\n" + "-" * 86)
    print(f"MEASURED DATA ARRIVAL DELAY ACROSS LANDED SNAPSHOTS (OVERLAP_HOURS = {OVERLAP_HOURS}h)")
    print("-" * 86)
    if not rows:
        print("  (No existing landed raw files found; initial full fetch will establish baseline.)")
        print("-" * 86)
        return
    print(
        f"  {'Source':<10} {'File':<22} {'Records':>8} {'Retrieved At (UTC)':<21} "
        f"{'Latest Event (UTC)':<21} {'Delay(h)':>8}"
    )
    max_delay = 0.0
    for r in rows:
        max_delay = max(max_delay, r["frontier_delay_hours"])
        print(
            f"  {r['source']:<10} {r['file_name']:<22} {r['n_records']:>8,} "
            f"{r['retrieved_at_utc']:<21} {r['latest_event_utc']:<21} {r['frontier_delay_hours']:>7.2f}h"
        )
    print(
        f"  -> Max measured frontier delay: {max_delay:.2f}h | Chosen OVERLAP_HOURS: {OVERLAP_HOURS}h "
        f"| SOURCE_MEMORY_DAYS: {SOURCE_MEMORY_DAYS}d"
    )
    print("-" * 86)


def run_ingestion(
    run_time: str | None = None,
    force_full: bool = False,
    raw_data_dir: str | Path | None = None,
    log_db_path: Path = LOG_DB_PATH,
) -> None:
    resolved_run_time = get_run_time(run_time)
    run_time_dt = parse_utc_iso(resolved_run_time)

    intakes_url = os.getenv("INTAKES_API_URL", "https://data.austintexas.gov/resource/pyqf-r2dc.json")
    outcomes_url = os.getenv("OUTCOMES_API_URL", "https://data.austintexas.gov/resource/gsvs-ypi7.json")
    raw_dir_str = str(raw_data_dir or os.getenv("RAW_DATA_DIR", "data/raw"))
    raw_dir = Path(raw_dir_str) if Path(raw_dir_str).is_absolute() else (ROOT_DIR / raw_dir_str)

    client = SodaClient(max_records_per_run=MAX_RECORDS_PER_RUN)

    sources = [
        {"name": "intakes", "url": intakes_url, "timestamp_field": "source_date"},
        {"name": "outcomes", "url": outcomes_url, "timestamp_field": "outcome_date"},
    ]
    print("=" * 72)
    print("🚀 Austin Animal Center - Milestone M5 Self-Healing Data Ingestion")
    print("=" * 72)
    print(f"run_time:            {resolved_run_time}")
    print(f"overlap_window:      {OVERLAP_HOURS} hours")
    print(f"source_memory_limit: {SOURCE_MEMORY_DAYS} days")
    print(f"max_records_per_run: {MAX_RECORDS_PER_RUN:,}")

    delay_rows = measure_arrival_delays(raw_dir)
    print_arrival_delay_table(delay_rows)

    summary = []
    has_failure = False
    start_time = time.time()

    for src in sources:
        src_name = src["name"]
        ts_field = src["timestamp_field"]
        last_success_iso, anchor_source = resolve_last_success(
            raw_dir, src_name, resolved_run_time, log_db_path=log_db_path
        )

        use_full = force_full or (last_success_iso is None)
        mode_label = "FULL (initial/forced)" if use_full else "INCREMENTAL (self-healing)"

        print(f"\n[INGEST] Source: {src_name} | Mode: {mode_label}")
        try:
            if not use_full and last_success_iso is not None:
                last_success_dt = parse_utc_iso(last_success_iso)
                gap_days = (run_time_dt - last_success_dt).total_seconds() / 86400.0
                if gap_days > SOURCE_MEMORY_DAYS:
                    raise RuntimeError(
                        f"[WARN] Outage gap of {gap_days:.1f} days since last_success={last_success_iso} "
                        f"exceeds SOURCE_MEMORY_DAYS ({SOURCE_MEMORY_DAYS}d). Data before the source retention "
                        f"window cannot be recovered automatically; aborting with non-zero exit."
                    )
                # Self-healing window: [min(last_success, run_time) - OVERLAP_HOURS, run_time]
                anchor_dt = min(last_success_dt, run_time_dt)
                window_start_utc = anchor_dt - timedelta(hours=OVERLAP_HOURS)
                start_local_str = to_socrata_local_floating(window_start_utc)
                end_local_str = to_socrata_local_floating(run_time_dt)
                window_desc = (
                    f"UTC [{window_start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} .. {resolved_run_time}] "
                    f"(anchor={last_success_iso} from {anchor_source}, overlap={OVERLAP_HOURS}h, gap={gap_days:.2f}d)"
                )
                print(f"  Window: {window_desc}")
                records = client.fetch_incremental(
                    src["url"],
                    timestamp_field=ts_field,
                    start_local_iso=start_local_str,
                    end_local_iso=end_local_str,
                    max_records=MAX_RECORDS_PER_RUN,
                )
            else:
                window_desc = f"FULL fetch up to {resolved_run_time} (reason={anchor_source})"
                print(f"  Window: {window_desc}")
                records = client.fetch_all(src["url"], max_records=MAX_RECORDS_PER_RUN)

            if not records:
                raise ValueError(
                    f"Empty response (0 records) received from {src['url']} for window ({window_desc}); "
                    f"refusing to silently save an empty dataset."
                )

            saved_path = save_raw_json(records, src_name, base_dir=str(raw_dir))
            summary.append({
                "source": src_name,
                "mode": mode_label,
                "window": window_desc,
                "status": "SUCCESS",
                "records": len(records),
                "path": saved_path,
                "error": None,
            })
            print(f"✅ Successfully landed {len(records)} records -> {saved_path}")
        except Exception as e:
            has_failure = True
            summary.append({
                "source": src_name,
                "mode": mode_label,
                "window": last_success_iso or "FULL",
                "status": "FAILED",
                "records": 0,
                "path": None,
                "error": str(e),
            })
            print(f"❌ Failed to fetch {src_name}: {e}")

    elapsed_time = round(time.time() - start_time, 2)
    print("\n" + "=" * 72)
    print("📊 INGESTION RUN SUMMARY")
    print("=" * 72)
    for item in summary:
        print(f"- Source: {item['source']}")
        print(f"  Mode: {item['mode']}")
        print(f"  Window: {item['window']}")
        print(f"  Status: {item['status']}")
        print(f"  Records Landed: {item['records']}")
        print(f"  Saved Path: {item['path']}")
        if item["error"]:
            print(f"  Error: {item['error']}")
    print("-" * 72)
    print(f"Total Duration: {elapsed_time}s")
    print(f"Overall Run Health: {'HEALTHY ✅' if not has_failure else 'DEGRADED / FAILED ❌'}")
    print("=" * 72)
    if has_failure:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Self-healing incremental SODA ingestion for Austin Animal Center."
    )
    parser.add_argument(
        "--run-time",
        default=None,
        help="Optional ISO 8601 UTC pipeline run timestamp (defaults to PIPELINE_RUN_TIME or current UTC).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full dataset fetch instead of an incremental window query.",
    )
    parser.add_argument(
        "--measure-delay",
        action="store_true",
        help="Print the measured arrival delay table from landed data/raw/ files and exit.",
    )
    args = parser.parse_args()

    try:
        resolved_rt = get_run_time(args.run_time)
    except ValueError as exc:
        print(f"INGEST FAILED: {exc}")
        sys.exit(1)

    if args.measure_delay:
        raw_dir_str = os.getenv("RAW_DATA_DIR", "data/raw")
        raw_dir = Path(raw_dir_str) if Path(raw_dir_str).is_absolute() else (ROOT_DIR / raw_dir_str)
        print_arrival_delay_table(measure_arrival_delays(raw_dir))
        return

    run_ingestion(run_time=resolved_rt, force_full=args.full)


if __name__ == "__main__":
    main()
