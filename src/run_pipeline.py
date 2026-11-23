"""
Milestone M5 — Automated Pipeline Runner (`src/run_pipeline.py`).

Runs the Austin Animal Center pipeline steps as separate processes in order:
    src/ingest.py -> src/load.py -> src/clean.py -> src/validate.py

Guarantees:
- Separate subprocess per step (`subprocess.run([sys.executable, ...])`).
- Stops at the first non-zero exit code and exits with that step's exit code.
- Enforces per-step timeouts; a timed-out step is terminated and logged as exit code 124.
- Records every executed step in `data/pipeline_log.db` (`pipeline_run` STRICT table).
- Accepts `--run-time` (ISO 8601 UTC, defaulting to now) and passes it to all steps via
  `PIPELINE_RUN_TIME` so every step uses one shared clock.
- Supports `--skip-ingest` to rebuild the entire database from landed `data/raw/` files.
- Enforces single-instance execution via an atomic lock file (`data/pipeline.lock`),
  exiting with code 75 if another run is already in progress.
- Appends full run output to `logs/pipeline.log` and prints one closing status line.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DB_PATH = ROOT_DIR / "data" / "pipeline_log.db"
LOCK_FILE_PATH = ROOT_DIR / "data" / "pipeline.lock"
DEFAULT_LOG_FILE = ROOT_DIR / "logs" / "pipeline.log"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ordered pipeline steps: (relative_script_path, default_timeout_seconds)
PIPELINE_STEPS: list[tuple[str, int]] = [
    ("src/ingest.py", 600),
    ("src/load.py", 300),
    ("src/clean.py", 300),
    ("src/validate.py", 180),
]

EXIT_ALREADY_RUNNING = 75
EXIT_TIMEOUT = 124

CREATE_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id      TEXT    NOT NULL,
    run_time    TEXT    NOT NULL,
    step        TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT    NOT NULL,
    exit_code   INTEGER NOT NULL,
    detail      TEXT,
    PRIMARY KEY (run_id, step)
) STRICT;
"""

INSERT_LOG_ROW_SQL = """
INSERT INTO pipeline_run (
    run_id, run_time, step, started_at, finished_at, exit_code, detail
) VALUES (?, ?, ?, ?, ?, ?, ?);
"""


def utc_now_iso() -> str:
    """Returns current wall-clock UTC timestamp in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_run_time(raw: str | None) -> str:
    """Parses and normalises `--run-time` into canonical ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`)."""
    if raw is None:
        return utc_now_iso()
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid --run-time value '{raw}'. Use ISO 8601 UTC, e.g. 2026-11-23T21:30:00Z."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def init_log_db(log_db_path: Path = LOG_DB_PATH) -> sqlite3.Connection:
    """Opens `data/pipeline_log.db` and ensures the STRICT `pipeline_run` table exists."""
    log_db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(log_db_path)
    con.execute(CREATE_LOG_TABLE_SQL)
    con.commit()
    return con


def record_step_run(
    con: sqlite3.Connection,
    run_id: str,
    run_time: str,
    step: str,
    started_at: str,
    finished_at: str,
    exit_code: int,
    detail: str,
) -> None:
    """Inserts a single step execution record into `pipeline_run` using placeholders."""
    with con:
        con.execute(
            INSERT_LOG_ROW_SQL,
            (run_id, run_time, step, started_at, finished_at, int(exit_code), detail),
        )


def summarize_step_output(step: str, stdout: str, stderr: str, exit_code: int) -> str:
    """Extracts a compact one-line summary from a step's output for the `detail` column."""
    if exit_code == EXIT_TIMEOUT:
        return "timed out"
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    err_lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]

    if step == "src/ingest.py":
        counts = re.findall(r"Records Landed:\s*(\d+)", stdout or "")
        if counts:
            return f"records_landed={'+'.join(counts)}"
    elif step == "src/load.py":
        parts = []
        for tbl in ("animals", "intakes", "outcomes"):
            m = re.search(rf"^{tbl}:\s+([\d,]+)\s+in database\s+\(([+-][\d,]+)", stdout or "", re.MULTILINE)
            if m:
                parts.append(f"{tbl}={m.group(1)}({m.group(2)})")
        if parts:
            return "; ".join(parts)
    elif step == "src/clean.py":
        m = re.search(r"STATUS:\s*(.+)", stdout or "")
        if m:
            return f"STATUS: {m.group(1).strip()}"
    elif step == "src/validate.py":
        m = re.search(r"SUMMARY:\s*(.+)", stdout or "")
        if m:
            return m.group(1).strip()

    if exit_code != 0:
        tail = err_lines[-1] if err_lines else (lines[-1] if lines else "non-zero exit")
        return tail[:240]
    return (lines[-1] if lines else "ok")[:240]


def acquire_lock(lock_path: Path, run_id: str, run_time: str) -> int | None:
    """
    Atomically creates the lock file with O_CREAT | O_EXCL.
    Returns the open file descriptor on success, or None if another run holds the lock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    payload = f"run_id={run_id} pid={os.getpid()} run_time={run_time} started_at={utc_now_iso()}\n"
    os.write(fd, payload.encode("utf-8"))
    return fd


def release_lock(fd: int | None, lock_path: Path) -> None:
    """Closes the lock file descriptor and removes the lock file."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass


def append_log_file(log_file: Path, text: str) -> None:
    """Appends text to `logs/pipeline.log` (creating parent directory if needed)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)


def emit(message: str, log_file: Path | None) -> None:
    """Prints a line to stdout and mirrors it to the log file."""
    print(message, flush=True)
    if log_file is not None:
        append_log_file(log_file, message + "\n")


def run_pipeline(
    run_time: str,
    skip_ingest: bool = False,
    timeout_override: int | None = None,
    log_db_path: Path = LOG_DB_PATH,
    lock_path: Path = LOCK_FILE_PATH,
    log_file: Path | None = DEFAULT_LOG_FILE,
) -> int:
    run_id = uuid.uuid4().hex[:8]
    lock_fd = acquire_lock(lock_path, run_id, run_time)
    if lock_fd is None:
        emit(
            f"[LOCK] Another pipeline run is already in progress ({lock_path}). Exiting with code {EXIT_ALREADY_RUNNING}.",
            log_file,
        )
        return EXIT_ALREADY_RUNNING

    con: sqlite3.Connection | None = None
    try:
        con = init_log_db(log_db_path)
        steps = [
            (step, timeout_override if timeout_override is not None else default_timeout)
            for step, default_timeout in PIPELINE_STEPS
            if not (skip_ingest and step == "src/ingest.py")
        ]

        emit("=" * 72, log_file)
        emit(
            f"[PIPELINE] run_id={run_id} run_time={run_time} skip_ingest={skip_ingest} steps={len(steps)}",
            log_file,
        )

        child_env = os.environ.copy()
        child_env["PIPELINE_RUN_TIME"] = run_time
        child_env["PIPELINE_RUN_ID"] = run_id
        child_env["PYTHONIOENCODING"] = "utf-8"

        env_timeout = os.getenv("PIPELINE_STEP_TIMEOUT")
        if timeout_override is None and env_timeout:
            try:
                parsed_env_timeout = int(env_timeout)
                if parsed_env_timeout > 0:
                    steps = [(step, parsed_env_timeout) for step, _ in steps]
            except ValueError:
                pass

        for step, step_timeout in steps:
            script_path = ROOT_DIR / step
            started_at = utc_now_iso()
            emit(f"[STEP START] {step} (timeout={step_timeout}s) at {started_at}", log_file)

            try:
                proc = subprocess.run(
                    [sys.executable, str(script_path), "--run-time", run_time],
                    cwd=str(ROOT_DIR),
                    env=child_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=step_timeout,
                )
                exit_code = proc.returncode
                stdout_text = proc.stdout or ""
                stderr_text = proc.stderr or ""
            except subprocess.TimeoutExpired as exc:
                exit_code = EXIT_TIMEOUT
                stdout_text = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr_text = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                stderr_text = (stderr_text + f"\nStep {step} timed out after {step_timeout}s.").strip()

            finished_at = utc_now_iso()
            if stdout_text.strip():
                emit(stdout_text.rstrip(), log_file)
            if stderr_text.strip():
                emit(stderr_text.rstrip(), log_file)

            detail = summarize_step_output(step, stdout_text, stderr_text, exit_code)
            record_step_run(con, run_id, run_time, step, started_at, finished_at, exit_code, detail)
            emit(f"[STEP END]   {step} exit_code={exit_code} finished_at={finished_at} detail={detail}", log_file)

            if exit_code != 0:
                emit(
                    f"PIPELINE FAILED: run_id={run_id} run_time={run_time} failed_step={step} exit_code={exit_code}",
                    log_file,
                )
                return exit_code

        emit(
            f"PIPELINE SUCCEEDED: run_id={run_id} run_time={run_time} steps_completed={len(steps)} exit_code=0",
            log_file,
        )
        return 0
    finally:
        if con is not None:
            con.close()
        release_lock(lock_fd, lock_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Austin Animal Center pipeline (ingest -> load -> clean -> validate)."
    )
    parser.add_argument(
        "--run-time",
        type=parse_run_time,
        default=None,
        help="Pipeline clock in ISO 8601 UTC (e.g. 2026-11-23T21:30:00Z). Defaults to current UTC time.",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip src/ingest.py and rebuild the database from landed data/raw/ files.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Optional per-step timeout override in seconds (positive integer).",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Do not append output to logs/pipeline.log.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be a positive integer.")
    run_time = args.run_time if args.run_time is not None else utc_now_iso()
    log_file = None if args.no_log_file else DEFAULT_LOG_FILE
    return run_pipeline(
        run_time=run_time,
        skip_ingest=args.skip_ingest,
        timeout_override=args.timeout,
        log_file=log_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
