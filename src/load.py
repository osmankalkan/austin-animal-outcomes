"""
M2 loader: builds the SQLite database from the landed raw files.

    python src/load.py

- Reads data/raw/<source>/<UTC timestamp>.json only; never calls the API.
- Deduplicates by natural key; the most recently retrieved version wins.
- Validates and normalises types and timestamps (Austin local -> UTC).
- Upserts with INSERT ... ON CONFLICT ... DO UPDATE (idempotent).
- Writes everything inside one transaction (atomic).
- Routes records it cannot load to the quarantine table, never drops them.
"""

import json
import os
import sys
import time
from collections import Counter, namedtuple
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.db import apply_schema, connect, get_db_path, table_counts
from src.transform import (
    CleanLog,
    clean_text,
    retrieved_at_from_filename,
    to_bool_int,
    to_date,
    to_int,
    to_utc_timestamp,
    zip_from_address,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

SOURCES = ("intakes", "outcomes")
RawEntry = namedtuple("RawEntry", "retrieved_at file_name index record")


class LoadError(Exception):
    """The run cannot continue; nothing from it is written."""


# ---------------------------------------------------------------------------
# 1. Read landed raw files
# ---------------------------------------------------------------------------

def get_raw_dir() -> Path:
    path = Path(os.getenv("RAW_DATA_DIR", "data/raw"))
    return path if path.is_absolute() else ROOT_DIR / path


def read_raw_files(raw_dir: Path):
    """Returns ({source: [RawEntry]}, file_info). A corrupt file aborts the run."""
    entries = {source: [] for source in SOURCES}
    file_info = {"files": Counter(), "skipped": [], "retrieved": []}
    for source in SOURCES:
        folder = Path(raw_dir) / source
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            retrieved_at = retrieved_at_from_filename(path.name)
            if retrieved_at is None:
                file_info["skipped"].append(str(path))
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise LoadError(f"Corrupt raw file {path}: {exc}") from exc
            if not isinstance(data, list):
                raise LoadError(f"Raw file {path} is not a JSON list of records.")
            for index, record in enumerate(data):
                if not isinstance(record, dict):
                    raise LoadError(f"Raw file {path}, record {index} is not a JSON object.")
                entries[source].append(RawEntry(retrieved_at, path.name, index, record))
            file_info["files"][source] += 1
            file_info["retrieved"].append(retrieved_at)
    return entries, file_info


# ---------------------------------------------------------------------------
# 2. Deduplicate by key (latest retrieved_at wins) and validate
# ---------------------------------------------------------------------------

def is_newer(candidate: RawEntry, current: RawEntry) -> bool:
    return (candidate.retrieved_at, candidate.file_name) > (current.retrieved_at, current.file_name)


def latest_by_key(entries, key_fn):
    """Keeps the most recently retrieved entry per natural key; returns (winners, unkeyed, duplicates)."""
    winners, unkeyed = {}, []
    for entry in entries:
        key = key_fn(entry.record)
        if key is None:
            unkeyed.append(entry)
            continue
        current = winners.get(key)
        if current is None or is_newer(entry, current):
            winners[key] = entry
    duplicates = len(entries) - len(unkeyed) - len(winners)
    return winners, unkeyed, duplicates


def record_id(entry: RawEntry) -> str:
    return clean_text(entry.record.get("id")) or f"{entry.file_name}#{entry.index}"


def quarantine_row(source: str, entry: RawEntry, reason: str) -> dict:
    return {
        "source": source,
        "source_record_id": record_id(entry),
        "reason": reason,
        "raw_record": json.dumps(entry.record, ensure_ascii=False, sort_keys=True),
        "retrieved_at": entry.retrieved_at,
    }


def intake_key(record):
    animal_id = clean_text(record.get("animal_id"))
    intake_at = to_utc_timestamp(record.get("source_date"))
    if not animal_id or not intake_at or not clean_text(record.get("id")):
        return None
    return animal_id, intake_at


def outcome_key(record):
    animal_id = clean_text(record.get("animal_id"))
    outcome_at = to_utc_timestamp(record.get("outcome_date"))
    if not animal_id or not outcome_at or not clean_text(record.get("id")):
        return None
    return animal_id, outcome_at


def prepare(entries, log: CleanLog) -> dict:
    """Turns raw entries into validated rows ready for upsert, plus quarantine rows."""
    quarantine = {}

    def send_to_quarantine(source, entry, reason):
        row = quarantine_row(source, entry, reason)
        key = (source, row["source_record_id"])
        current = quarantine.get(key)
        if current is None or row["retrieved_at"] >= current["retrieved_at"]:
            quarantine[key] = row

    # Intakes ---------------------------------------------------------------
    intake_winners, intake_unkeyed, intake_dups = latest_by_key(entries["intakes"], intake_key)
    for entry in intake_unkeyed:
        send_to_quarantine("intakes", entry, "missing_or_invalid_key")

    intakes, animal_sources = [], {}
    for (animal_id, intake_at), entry in intake_winners.items():
        rec = entry.record
        intakes.append({
            "animal_id": animal_id,
            "intake_at": intake_at,
            "source_record_id": clean_text(rec.get("id")),
            "intake_type": clean_text(rec.get("source_name")),
            "name_at_intake": clean_text(rec.get("name_at_intake")),
            "previously_spayed_neutered": to_bool_int(
                rec.get("ispreviouslyspayedneutered"), "previously_spayed_neutered", log),
            "intake_health_condition": clean_text(rec.get("intake_health_condition")),
            "found_zip": zip_from_address(rec.get("found_address")),
            "retrieved_at": entry.retrieved_at,
        })
        # Animal attributes come from its most recently retrieved, latest intake.
        current = animal_sources.get(animal_id)
        rank = (entry.retrieved_at, intake_at)
        if current is None or rank > current[0]:
            animal_sources[animal_id] = (rank, entry)

    animals = []
    for animal_id, (_, entry) in animal_sources.items():
        rec = entry.record
        animals.append({
            "animal_id": animal_id,
            "animal_type": clean_text(rec.get("type")),
            "sex": clean_text(rec.get("sex")),
            "primary_breed": clean_text(rec.get("primary_breed")),
            "primary_color": clean_text(rec.get("primary_color")),
            "secondary_color": clean_text(rec.get("secondary_color")),
            "date_of_birth": to_date(rec.get("date_of_birth"), "date_of_birth", log),
            "retrieved_at": entry.retrieved_at,
        })

    # Outcomes --------------------------------------------------------------
    outcome_winners, outcome_unkeyed, outcome_dups = latest_by_key(entries["outcomes"], outcome_key)
    for entry in outcome_unkeyed:
        send_to_quarantine("outcomes", entry, "missing_or_invalid_key")

    outcomes = []
    for (animal_id, outcome_at), entry in outcome_winners.items():
        rec = entry.record
        intake_at = to_utc_timestamp(rec.get("intake_date"))
        if intake_at is None:
            send_to_quarantine("outcomes", entry, "missing_or_invalid_intake_date")
            continue
        outcomes.append({
            "entry": entry,
            "animal_id": animal_id,
            "outcome_at": outcome_at,
            "intake_at": intake_at,
            "source_record_id": clean_text(rec.get("id")),
            "outcome_status": clean_text(rec.get("outcome_status")),
            "euthanasia_reason": clean_text(rec.get("euthanasia_reason")),
            "name": clean_text(rec.get("name")),
            "spayed_neutered": clean_text(rec.get("spayed_neutered")),
            "days_in_shelter": to_int(rec.get("days_in_shelter"), "days_in_shelter", log, minimum=0),
            "retrieved_at": entry.retrieved_at,
        })

    return {
        "animals": animals,
        "intakes": intakes,
        "outcomes": outcomes,
        "quarantine": quarantine,
        "duplicates": intake_dups + outcome_dups,
    }


# ---------------------------------------------------------------------------
# 3. Upserts (every value through a named placeholder)
# ---------------------------------------------------------------------------

UPSERT_ANIMAL = """
INSERT INTO animals (animal_id, animal_type, sex, primary_breed, primary_color,
                     secondary_color, date_of_birth, retrieved_at)
VALUES (:animal_id, :animal_type, :sex, :primary_breed, :primary_color,
        :secondary_color, :date_of_birth, :retrieved_at)
ON CONFLICT (animal_id) DO UPDATE SET
    animal_type     = excluded.animal_type,
    sex             = excluded.sex,
    primary_breed   = excluded.primary_breed,
    primary_color   = excluded.primary_color,
    secondary_color = excluded.secondary_color,
    date_of_birth   = excluded.date_of_birth,
    retrieved_at    = excluded.retrieved_at
WHERE excluded.retrieved_at >= animals.retrieved_at
"""

UPSERT_INTAKE = """
INSERT INTO intakes (animal_key, intake_at, source_record_id, intake_type, name_at_intake,
                     previously_spayed_neutered, intake_health_condition, found_zip, retrieved_at)
VALUES ((SELECT animal_key FROM animals WHERE animal_id = :animal_id),
        :intake_at, :source_record_id, :intake_type, :name_at_intake,
        :previously_spayed_neutered, :intake_health_condition, :found_zip, :retrieved_at)
ON CONFLICT (animal_key, intake_at) DO UPDATE SET
    source_record_id           = excluded.source_record_id,
    intake_type                = excluded.intake_type,
    name_at_intake             = excluded.name_at_intake,
    previously_spayed_neutered = excluded.previously_spayed_neutered,
    intake_health_condition    = excluded.intake_health_condition,
    found_zip                  = excluded.found_zip,
    retrieved_at               = excluded.retrieved_at
WHERE excluded.retrieved_at >= intakes.retrieved_at
"""

UPSERT_OUTCOME = """
INSERT INTO outcomes (animal_key, outcome_at, intake_at, source_record_id, outcome_status,
                      euthanasia_reason, name, spayed_neutered, days_in_shelter, retrieved_at)
VALUES (:animal_key, :outcome_at, :intake_at, :source_record_id, :outcome_status,
        :euthanasia_reason, :name, :spayed_neutered, :days_in_shelter, :retrieved_at)
ON CONFLICT (animal_key, outcome_at) DO UPDATE SET
    intake_at         = excluded.intake_at,
    source_record_id  = excluded.source_record_id,
    outcome_status    = excluded.outcome_status,
    euthanasia_reason = excluded.euthanasia_reason,
    name              = excluded.name,
    spayed_neutered   = excluded.spayed_neutered,
    days_in_shelter   = excluded.days_in_shelter,
    retrieved_at      = excluded.retrieved_at
WHERE excluded.retrieved_at >= outcomes.retrieved_at
"""

UPSERT_QUARANTINE = """
INSERT INTO quarantine (source, source_record_id, reason, raw_record, retrieved_at)
VALUES (:source, :source_record_id, :reason, :raw_record, :retrieved_at)
ON CONFLICT (source, source_record_id) DO UPDATE SET
    reason       = excluded.reason,
    raw_record   = excluded.raw_record,
    retrieved_at = excluded.retrieved_at
WHERE excluded.retrieved_at >= quarantine.retrieved_at
"""

DELETE_RESOLVED = "DELETE FROM quarantine WHERE source = :source AND source_record_id = :source_record_id"

SELECT_ANIMAL_KEYS = "SELECT animal_id, animal_key FROM animals"
SELECT_INTAKE_KEYS = "SELECT animal_key, intake_at FROM intakes"


def upsert_animals(con, rows):
    con.executemany(UPSERT_ANIMAL, rows)


def upsert_intakes(con, rows):
    con.executemany(UPSERT_INTAKE, rows)


def upsert_outcomes(con, rows):
    con.executemany(UPSERT_OUTCOME, rows)


def upsert_quarantine(con, rows):
    con.executemany(UPSERT_QUARANTINE, rows)


def write_all(con, prepared: dict) -> dict:
    """All writes of one run. Called inside a single transaction."""
    upsert_animals(con, prepared["animals"])
    upsert_intakes(con, prepared["intakes"])

    # Resolve outcome references against what the database now holds.
    animal_keys = dict(con.execute(SELECT_ANIMAL_KEYS).fetchall())
    intake_keys = set(con.execute(SELECT_INTAKE_KEYS).fetchall())

    quarantine = dict(prepared["quarantine"])
    outcome_rows = []
    for row in prepared["outcomes"]:
        entry = row["entry"]
        animal_key = animal_keys.get(row["animal_id"])
        reason = None
        if animal_key is None:
            reason = "unknown_animal"
        elif (animal_key, row["intake_at"]) not in intake_keys:
            reason = "intake_not_found"
        if reason:
            q = quarantine_row("outcomes", entry, reason)
            quarantine[("outcomes", q["source_record_id"])] = q
            continue
        values = {k: v for k, v in row.items() if k not in ("entry", "animal_id")}
        values["animal_key"] = animal_key
        outcome_rows.append(values)

    upsert_outcomes(con, outcome_rows)

    # Records that loaded this run are no longer quarantined.
    loaded = [{"source": "intakes", "source_record_id": r["source_record_id"]} for r in prepared["intakes"]]
    loaded += [{"source": "outcomes", "source_record_id": r["source_record_id"]} for r in outcome_rows]
    loaded_ids = {(r["source"], r["source_record_id"]) for r in loaded}
    quarantine_rows = [q for key, q in quarantine.items() if key not in loaded_ids]

    con.executemany(DELETE_RESOLVED, loaded)
    upsert_quarantine(con, quarantine_rows)

    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise LoadError(f"foreign_key_check failed: {violations[:5]}")

    return {"quarantine_rows": quarantine_rows}


# ---------------------------------------------------------------------------
# 4. Run + summary
# ---------------------------------------------------------------------------

def run_load(db_path=None, raw_dir=None, quiet=False) -> dict:
    """Builds/updates the database from raw files. Raises LoadError if nothing could be written."""
    db_path = Path(db_path) if db_path else get_db_path()
    raw_dir = Path(raw_dir) if raw_dir else get_raw_dir()
    started = time.time()

    con = connect(db_path)
    try:
        apply_schema(con)
        before = table_counts(con)

        entries, file_info = read_raw_files(raw_dir)
        log = CleanLog()
        prepared = prepare(entries, log)

        with con:  # one transaction: everything lands together, or nothing does
            written = write_all(con, prepared)

        after = table_counts(con)
        fk_ok = not con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()

    summary = {
        "db_path": db_path,
        "file_info": file_info,
        "before": before,
        "after": after,
        "duplicates": prepared["duplicates"],
        "cleaned": log,
        "quarantine_rows": written["quarantine_rows"],
        "fk_ok": fk_ok,
        "seconds": round(time.time() - started, 2),
    }
    if not quiet:
        print_summary(summary)
    return summary


def print_summary(s: dict) -> None:
    info, before, after = s["file_info"], s["before"], s["after"]
    n_files = sum(info["files"].values())
    days = sorted(r[:10] for r in info["retrieved"])
    span = f"{days[0]} .. {days[-1]}" if days else "none"
    per_source = ", ".join(f"{src}: {info['files'][src]}" for src in SOURCES)
    reasons = Counter(q["reason"] for q in s["quarantine_rows"])
    reason_text = ", ".join(f"{r}={n:,}" for r, n in sorted(reasons.items())) or "none"

    def line(table, unit):
        delta = after[table] - before[table]
        return f"{after[table]:,} in database ({delta:+,} {unit} this run)"

    print("=" * 64)
    print("Austin Animal Center - M2 database load")
    print("=" * 64)
    print(f"database:              {s['db_path']}")
    print(f"files read:            {n_files} raw files ({per_source}; {span})")
    if info["skipped"]:
        print(f"files skipped:         {len(info['skipped'])} (no UTC timestamp in name)")
    print(f"duplicates resolved:   {s['duplicates']:,} re-fetched copies (latest retrieved_at wins)")
    print(f"animals:               {line('animals', 'new')}")
    print(f"intakes:               {line('intakes', 'new')}")
    print(f"outcomes:              {line('outcomes', 'new')}")
    cleaned = s["cleaned"]
    detail = f"  ({cleaned.describe()})" if cleaned.total else ""
    print(f"values cleaned:        {cleaned.total:,}{detail}")
    print(f"quarantined:           {after['quarantine']:,} records ({reason_text})")
    print(f"foreign_key_check:     {'OK' if s['fk_ok'] else 'FAILED'}")
    print(f"duration:              {s['seconds']}s")
    print("STATUS: COMPLETE" if s["fk_ok"] else "STATUS: FAILED")
    print("=" * 64)


def main() -> None:
    try:
        summary = run_load()
    except LoadError as exc:
        print(f"LOAD FAILED: {exc}")
        print("STATUS: FAILED (transaction rolled back, nothing from this run was written)")
        sys.exit(1)
    if not summary["fk_ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
