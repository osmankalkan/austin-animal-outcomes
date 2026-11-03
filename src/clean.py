"""
M3 cleaning pipeline: builds the cleaned layer from the M2 tables.

    python src/clean.py

Rules enforced:
- Reads ONLY from the M2 SQLite tables (animals, intakes, outcomes) and
  committed mapping CSVs in data/mappings/*.csv. Never touches data/raw/ or the API.
- Never modifies any M2 table (verified by hashing every M2 table before and after).
- Keeps exact row count and primary keys for animals_clean, intakes_clean, outcomes_clean.
- Stores (<col>_raw, <col>, <col>_flag) for every value cleaning can change.
- Never imputes: untrusted or missing values become NULL with an explicit flag.
- Runs inside one transaction (atomic) and produces identical state on re-runs (idempotent).
- Every value reaches SQL through a placeholder.
"""

import csv
import hashlib
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.clean_config import (
    ADULT_LABEL_MIN_AGE_DAYS,
    AUSTIN_METRO_ZIP_PREFIXES,
    HEALTH_STANDARD_MAP,
    IQR_FENCE_MULTIPLIER,
    JUVENILE_CONFLICT_AGE_DAYS,
    JUVENILE_MAX_AGE_DAYS,
    MAX_VALID_DATE,
    MIN_GROUP_SIZE_FOR_IQR,
    PLACEHOLDER_NAMES,
    TEXAS_ZIP_PREFIXES,
)
from src.db import DUMP_SQL, TABLES as M2_TABLES, connect, get_db_path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLEAN_SCHEMA_PATH = ROOT_DIR / "sql" / "clean_schema.sql"
MAPPINGS_DIR = ROOT_DIR / "data" / "mappings"

PURE_DIGITS_RE = re.compile(r"^\d+$")
ID_NOTE_RE = re.compile(r"^\d{4,}\s*[-:]")


class CleanError(Exception):
    """Raised when cleaning fails or a constraint / M2 invariance check breaks."""


def hash_m2_tables(con: sqlite3.Connection) -> dict:
    """Computes SHA-256 hashes of every M2 table to prove raw tables are untouched."""
    hashes = {}
    for table in M2_TABLES:
        rows = con.execute(DUMP_SQL[table]).fetchall()
        digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
        hashes[table] = (len(rows), digest)
    return hashes


def apply_clean_schema(con: sqlite3.Connection) -> None:
    con.executescript(CLEAN_SCHEMA_PATH.read_text(encoding="utf-8"))


def read_mapping_csv(filename: str) -> list:
    path = MAPPINGS_DIR / filename
    if not path.exists():
        raise CleanError(f"Mapping file missing: {path}")
    with open(path, mode="r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_mappings(con: sqlite3.Connection) -> dict:
    """Loads committed CSV mappings into map_* tables inside the transaction."""
    animal_type_rows = read_mapping_csv("animal_type_map.csv")
    intake_type_rows = read_mapping_csv("intake_type_map.csv")
    outcome_status_rows = read_mapping_csv("outcome_status_map.csv")

    con.execute("DELETE FROM map_animal_type")
    con.executemany(
        """
        INSERT INTO map_animal_type
            (raw_value, canonical_value, life_stage_default, kind, decided_by, decided_at, note)
        VALUES (:raw_value, :canonical_value, :life_stage_default, :kind, :decided_by, :decided_at, :note)
        """,
        animal_type_rows,
    )

    con.execute("DELETE FROM map_intake_type")
    con.executemany(
        """
        INSERT INTO map_intake_type
            (raw_value, canonical_value, kind, decided_by, decided_at, note)
        VALUES (:raw_value, :canonical_value, :kind, :decided_by, :decided_at, :note)
        """,
        intake_type_rows,
    )

    con.execute("DELETE FROM map_outcome_status")
    con.executemany(
        """
        INSERT INTO map_outcome_status
            (raw_value, canonical_value, kind, decided_by, decided_at, note)
        VALUES (:raw_value, :canonical_value, :kind, :decided_by, :decided_at, :note)
        """,
        outcome_status_rows,
    )

    return {
        "animal_type": {r["raw_value"]: r for r in animal_type_rows},
        "intake_type": {r["raw_value"]: r for r in intake_type_rows},
        "outcome_status": {r["raw_value"]: r for r in outcome_status_rows},
    }


def clean_animal_name(raw_name):
    """
    Applies documented name rules in order:
    1. NULL / empty -> (None, 'missing_not_recorded')
    2. Placeholder ('Unknown', 'No Name', '*', digits, shelter note) -> (None, 'missing_placeholder')
    3. Leading '*' (shelter-assigned prefix) -> stripped, NFC normalised ('stripped_shelter_prefix')
    4. All-uppercase or all-lowercase multi-letter name -> Title-cased ('normalised_case')
    5. Otherwise -> NFC normalised ('ok')
    """
    if raw_name is None:
        return None, "missing_not_recorded"
    text = unicodedata.normalize("NFC", str(raw_name)).strip()
    if not text:
        return None, "missing_not_recorded"

    folded = text.casefold()
    if folded in PLACEHOLDER_NAMES or PURE_DIGITS_RE.match(text) or ID_NOTE_RE.match(text):
        return None, "missing_placeholder"

    had_star = False
    if text.startswith("*"):
        had_star = True
        text = text.lstrip("*").strip()
        if not text or text.casefold() in PLACEHOLDER_NAMES or PURE_DIGITS_RE.match(text):
            return None, "missing_placeholder"

    # Normalise ALL-CAPS or all-lowercase names (e.g. 'DOLLY' -> 'Dolly', 'spencer' -> 'Spencer')
    alpha_chars = [ch for ch in text if ch.isalpha()]
    changed_case = False
    if len(alpha_chars) >= 2 and (text.isupper() or text.islower()):
        titled = text.title()
        if titled != text:
            text = titled
            changed_case = True

    if had_star:
        return text, "stripped_shelter_prefix"
    if changed_case:
        return text, "normalised_case"
    return text, "ok"


def clean_zip(raw_zip):
    """
    Applies documented ZIP code rules in order:
    1. NULL -> (None, 'missing_no_zip_in_address')
    2. Does not start with a Texas prefix (street house number captured when ZIP was omitted)
       -> (None, 'invalid_house_number_as_zip')
    3. Valid Texas ZIP outside Austin metro (not 786xx/787xx) -> kept, 'unusual_outside_service_area'
    4. Austin metro ZIP (786xx/787xx) -> kept, 'ok'
    """
    if raw_zip is None:
        return None, "missing_no_zip_in_address"
    z = str(raw_zip).strip()
    if not z.startswith(TEXAS_ZIP_PREFIXES):
        return None, "invalid_house_number_as_zip"
    if not z.startswith(AUSTIN_METRO_ZIP_PREFIXES):
        return z, "unusual_outside_service_area"
    return z, "ok"


def compute_iqr_fences(stay_lengths_by_species: dict) -> dict:
    """Computes Tukey outer fence Q3 + IQR_FENCE_MULTIPLIER * IQR per canonical species."""
    fences = {}
    all_vals = sorted(v for vals in stay_lengths_by_species.values() for v in vals)
    if all_vals:
        n_all = len(all_vals)
        q1_all = all_vals[int(0.25 * (n_all - 1))]
        q3_all = all_vals[int(0.75 * (n_all - 1))]
        default_fence = q3_all + IQR_FENCE_MULTIPLIER * max(q3_all - q1_all, 1.0)
    else:
        default_fence = 120.0

    for species, vals in stay_lengths_by_species.items():
        s = sorted(vals)
        if len(s) < MIN_GROUP_SIZE_FOR_IQR:
            fences[species] = default_fence
            continue
        n = len(s)
        q1 = s[int(0.25 * (n - 1))]
        q3 = s[int(0.75 * (n - 1))]
        iqr = max(q3 - q1, 1.0)
        fences[species] = q3 + IQR_FENCE_MULTIPLIER * iqr
    return fences


def days_between_iso(ts_start: str, ts_end: str) -> float:
    dt_start = datetime.strptime(ts_start, "%Y-%m-%dT%H:%M:%SZ")
    dt_end = datetime.strptime(ts_end, "%Y-%m-%dT%H:%M:%SZ")
    return (dt_end - dt_start).total_seconds() / 86400.0


def build_cleaned_layer(con: sqlite3.Connection, mappings: dict) -> dict:
    type_map = mappings["animal_type"]
    intake_map = mappings["intake_type"]
    outcome_map = mappings["outcome_status"]

    raw_animals = con.execute(
        """
        SELECT animal_key, animal_id, animal_type, sex, primary_breed,
               primary_color, secondary_color, date_of_birth, retrieved_at
        FROM animals
        ORDER BY animal_key
        """
    ).fetchall()

    raw_intakes = con.execute(
        """
        SELECT animal_key, intake_at, source_record_id, intake_type, name_at_intake,
               previously_spayed_neutered, intake_health_condition, found_zip, retrieved_at
        FROM intakes
        ORDER BY animal_key, intake_at
        """
    ).fetchall()

    raw_outcomes = con.execute(
        """
        SELECT animal_key, outcome_at, intake_at, source_record_id, outcome_status,
               euthanasia_reason, name, spayed_neutered, days_in_shelter, retrieved_at
        FROM outcomes
        ORDER BY animal_key, outcome_at
        """
    ).fetchall()

    # First intake date per animal (for DOB validation and age-at-intake life-stage check)
    first_intake_by_animal = {}
    intakes_by_animal = defaultdict(list)
    intake_spay_lookup = {}
    for row in raw_intakes:
        ak, intake_at = row[0], row[1]
        if ak not in first_intake_by_animal or intake_at < first_intake_by_animal[ak]:
            first_intake_by_animal[ak] = intake_at
        intakes_by_animal[ak].append(intake_at)
        intake_spay_lookup[(ak, intake_at)] = row[5]

    # Outcomes grouped by stay (animal_key, intake_at) to detect duplicate exits & sequence overlaps
    outcomes_by_stay = defaultdict(list)
    for row in raw_outcomes:
        outcomes_by_stay[(row[0], row[2])].append(row[1])  # outcome_at is already ordered

    first_outcome_by_stay = {stay: outs[0] for stay, outs in outcomes_by_stay.items()}

    # ------------------------------------------------------------------
    # 1. Clean animals -> animals_clean
    # ------------------------------------------------------------------
    animals_clean_rows = []
    canonical_species_by_key = {}
    unmatched_mappings = {"animal_type": set(), "intake_type": set(), "outcome_status": set()}

    for (ak, aid, a_type_raw, sex_raw, breed, p_col_raw, s_col, dob_raw, ret_at) in raw_animals:
        # 1a. animal_type mapping
        m = type_map.get(a_type_raw)
        if m is None:
            unmatched_mappings["animal_type"].add(a_type_raw)
            a_type_clean = a_type_raw or "Other"
            a_type_flag = "ok"
            default_stage = "unknown"
        else:
            a_type_clean = m["canonical_value"]
            default_stage = m["life_stage_default"]
            if m["kind"] == "species_lifestage":
                a_type_flag = "mapped_life_stage"
            elif a_type_clean != a_type_raw:
                a_type_flag = "mapped_category"
            else:
                a_type_flag = "ok"

        canonical_species_by_key[ak] = a_type_clean

        # 1b. date_of_birth rules (Order: missing -> future -> after first intake -> ok)
        first_ia = first_intake_by_animal.get(ak)
        first_ia_date = first_ia[:10] if first_ia else None
        if dob_raw is None:
            dob_clean, dob_flag = None, "missing_not_recorded"
        elif dob_raw > MAX_VALID_DATE:
            dob_clean, dob_flag = None, "invalid_future_date"
        elif first_ia_date and dob_raw > first_ia_date:
            dob_clean, dob_flag = None, "invalid_after_intake"
        else:
            dob_clean, dob_flag = dob_raw, "ok"

        # 1c. life_stage resolution (derived before per-entity statistics)
        if dob_clean is not None and first_ia_date is not None:
            age_days = (date.fromisoformat(first_ia_date) - date.fromisoformat(dob_clean)).days
            computed_stage = "juvenile" if age_days <= JUVENILE_MAX_AGE_DAYS else "adult"
            if a_type_raw in ("Kitten", "Puppy") and age_days > JUVENILE_CONFLICT_AGE_DAYS:
                life_stage, life_stage_flag = computed_stage, "conflict_type_vs_age"
            elif a_type_raw in ("Cat", "Dog") and age_days < ADULT_LABEL_MIN_AGE_DAYS:
                life_stage, life_stage_flag = computed_stage, "conflict_type_vs_age"
            else:
                life_stage, life_stage_flag = computed_stage, "from_age_at_intake"
        else:
            if default_stage in ("juvenile", "adult"):
                life_stage, life_stage_flag = default_stage, "from_raw_type"
            else:
                life_stage, life_stage_flag = "unknown", "unknown_no_dob"

        # 1d. sex rules (Order: NULL -> 'Unknown' -> 'Male'/'Female')
        if sex_raw is None:
            sex_clean, sex_flag = None, "missing_unrecorded"
        elif sex_raw.strip().casefold() == "unknown":
            sex_clean, sex_flag = None, "missing_coded_unknown"
        else:
            sex_clean, sex_flag = sex_raw.strip(), "ok"

        # 1e. primary_color rules ('Unknown'/'unknown' -> NULL)
        if p_col_raw is not None and p_col_raw.strip().casefold() == "unknown":
            p_col_clean, p_col_flag = None, "missing_coded_unknown"
        else:
            p_col_clean, p_col_flag = p_col_raw, "ok"

        animals_clean_rows.append({
            "animal_key": ak,
            "animal_id": aid,
            "animal_type_raw": a_type_raw,
            "animal_type": a_type_clean,
            "animal_type_flag": a_type_flag,
            "life_stage": life_stage,
            "life_stage_flag": life_stage_flag,
            "sex_raw": sex_raw,
            "sex": sex_clean,
            "sex_flag": sex_flag,
            "primary_breed": breed,
            "primary_color_raw": p_col_raw,
            "primary_color": p_col_clean,
            "primary_color_flag": p_col_flag,
            "secondary_color": s_col,
            "date_of_birth_raw": dob_raw,
            "date_of_birth": dob_clean,
            "date_of_birth_flag": dob_flag,
            "retrieved_at": ret_at,
        })

    # ------------------------------------------------------------------
    # 2. Clean intakes -> intakes_clean
    # ------------------------------------------------------------------
    # Precompute sequence flags per (animal_key, intake_at)
    sequence_flag_by_stay = {}
    for ak, ia_list in intakes_by_animal.items():
        prev_ia = None
        for ia in ia_list:
            if prev_ia is None:
                sequence_flag_by_stay[(ak, ia)] = "ok"
            else:
                prev_first_out = first_outcome_by_stay.get((ak, prev_ia))
                if prev_first_out is None:
                    sequence_flag_by_stay[(ak, ia)] = "reintake_after_open_stay"
                elif prev_first_out > ia:
                    sequence_flag_by_stay[(ak, ia)] = "overlap_with_prior_stay"
                else:
                    sequence_flag_by_stay[(ak, ia)] = "ok"
            prev_ia = ia

    intakes_clean_rows = []
    for (ak, ia, src_id, itype_raw, name_raw, prev_spay, health_raw, zip_raw, ret_at) in raw_intakes:
        m = intake_map.get(itype_raw)
        if m is None:
            unmatched_mappings["intake_type"].add(itype_raw)
            itype_clean, itype_flag = (itype_raw or "Other"), "ok"
        else:
            itype_clean = m["canonical_value"]
            itype_flag = "mapped_category" if itype_clean != itype_raw else "ok"

        name_clean, name_flag = clean_animal_name(name_raw)

        if health_raw is None:
            health_clean, health_flag = None, "missing_not_logged"
        else:
            mapped_h = HEALTH_STANDARD_MAP.get(health_raw, health_raw)
            health_clean = mapped_h
            health_flag = "standardised_label" if mapped_h != health_raw else "ok"

        zip_clean, zip_flag = clean_zip(zip_raw)
        seq_flag = sequence_flag_by_stay[(ak, ia)]

        intakes_clean_rows.append({
            "animal_key": ak,
            "intake_at": ia,
            "source_record_id": src_id,
            "intake_type_raw": itype_raw,
            "intake_type": itype_clean,
            "intake_type_flag": itype_flag,
            "name_at_intake_raw": name_raw,
            "name_at_intake": name_clean,
            "name_at_intake_flag": name_flag,
            "previously_spayed_neutered": prev_spay,
            "intake_health_raw": health_raw,
            "intake_health": health_clean,
            "intake_health_flag": health_flag,
            "found_zip_raw": zip_raw,
            "found_zip": zip_clean,
            "found_zip_flag": zip_flag,
            "stay_sequence_flag": seq_flag,
            "retrieved_at": ret_at,
        })

    # ------------------------------------------------------------------
    # 3. Clean outcomes -> outcomes_clean
    #    First compute per-species IQR fences on valid primary stays
    # ------------------------------------------------------------------
    valid_stays_by_species = defaultdict(list)
    for (ak, oa, ia, src_id, ostatus_raw, euth_raw, name_raw, spay_raw, days_raw, ret_at) in raw_outcomes:
        is_first_exit = (oa == first_outcome_by_stay.get((ak, ia)))
        if is_first_exit and oa >= ia and days_raw is not None and days_raw >= 0:
            species = canonical_species_by_key.get(ak, "Other")
            valid_stays_by_species[species].append(float(days_raw))

    iqr_fences = compute_iqr_fences(valid_stays_by_species)

    outcomes_clean_rows = []
    for (ak, oa, ia, src_id, ostatus_raw, euth_raw, name_raw, spay_raw, days_raw, ret_at) in raw_outcomes:
        m = outcome_map.get(ostatus_raw)
        if m is None:
            unmatched_mappings["outcome_status"].add(ostatus_raw)
            ostatus_clean, ostatus_flag = (ostatus_raw or "Administrative / Other"), "ok"
        else:
            ostatus_clean = m["canonical_value"]
            ostatus_flag = "mapped_subtype" if ostatus_clean != ostatus_raw else "ok"

        # Euthanasia reason consistency with outcome_status
        if ostatus_clean == "Euthanized":
            if euth_raw is None:
                euth_clean, euth_flag = None, "missing_euthanized_no_reason"
            else:
                euth_clean, euth_flag = euth_raw, "ok"
        else:
            if euth_raw is not None:
                # Keep raw in euthanasia_reason_raw, set cleaned to NULL because outcome was not euthanasia
                euth_clean, euth_flag = None, "conflict_non_euthanasia_status"
            else:
                euth_clean, euth_flag = None, "not_applicable"

        name_clean, name_flag = clean_animal_name(name_raw)

        # Spayed/neutered rules (Order: NULL -> 'Unknown' -> conflict with intake -> lowercase 'yes' -> 'ok')
        prev_spay = intake_spay_lookup.get((ak, ia))
        if spay_raw is None:
            spay_clean, spay_flag = None, "missing_unrecorded"
        elif spay_raw.strip().casefold() == "unknown":
            spay_clean, spay_flag = None, "missing_coded_unknown"
        elif spay_raw.strip().casefold() == "no" and prev_spay == 1:
            spay_clean, spay_flag = None, "conflict_previously_spayed"
        elif spay_raw == "yes":
            spay_clean, spay_flag = "Yes", "normalised_case"
        elif spay_raw in ("Yes", "No"):
            spay_clean, spay_flag = spay_raw, "ok"
        else:
            spay_clean, spay_flag = None, "missing_coded_unknown"

        # Length of stay & days_in_shelter rules
        # Order: 1) outcome < intake (invalid) -> NULL
        #        2) secondary exit for the same intake stay -> NULL ('duplicate_exit_for_stay')
        #        3) above species IQR outer fence -> kept & flagged ('unusual_high_stay')
        #        4) otherwise -> kept & 'ok'
        is_first_exit = (oa == first_outcome_by_stay.get((ak, ia)))
        species = canonical_species_by_key.get(ak, "Other")
        fence = iqr_fences.get(species, 120.0)

        if oa < ia or (days_raw is not None and days_raw < 0):
            days_clean = None
            los_days = None
            days_flag = "invalid_outcome_before_intake"
        elif not is_first_exit:
            days_clean = None
            los_days = None
            days_flag = "duplicate_exit_for_stay"
        else:
            days_clean = days_raw
            los_days = round(days_between_iso(ia, oa), 4)
            if days_raw is not None and days_raw > fence:
                days_flag = "unusual_high_stay"
            else:
                days_flag = "ok"

        outcomes_clean_rows.append({
            "animal_key": ak,
            "outcome_at": oa,
            "intake_at": ia,
            "source_record_id": src_id,
            "outcome_status_raw": ostatus_raw,
            "outcome_status": ostatus_clean,
            "outcome_status_flag": ostatus_flag,
            "euthanasia_reason_raw": euth_raw,
            "euthanasia_reason": euth_clean,
            "euthanasia_reason_flag": euth_flag,
            "name_raw": name_raw,
            "name": name_clean,
            "name_flag": name_flag,
            "spayed_neutered_raw": spay_raw,
            "spayed_neutered": spay_clean,
            "spayed_neutered_flag": spay_flag,
            "days_in_shelter_raw": days_raw,
            "days_in_shelter": days_clean,
            "length_of_stay_days": los_days,
            "days_in_shelter_flag": days_flag,
            "retrieved_at": ret_at,
        })

    return {
        "animals_clean": animals_clean_rows,
        "intakes_clean": intakes_clean_rows,
        "outcomes_clean": outcomes_clean_rows,
        "iqr_fences": iqr_fences,
        "unmatched_mappings": unmatched_mappings,
    }


INSERT_ANIMALS_CLEAN = """
INSERT INTO animals_clean (
    animal_key, animal_id, animal_type_raw, animal_type, animal_type_flag,
    life_stage, life_stage_flag, sex_raw, sex, sex_flag, primary_breed,
    primary_color_raw, primary_color, primary_color_flag, secondary_color,
    date_of_birth_raw, date_of_birth, date_of_birth_flag, retrieved_at
) VALUES (
    :animal_key, :animal_id, :animal_type_raw, :animal_type, :animal_type_flag,
    :life_stage, :life_stage_flag, :sex_raw, :sex, :sex_flag, :primary_breed,
    :primary_color_raw, :primary_color, :primary_color_flag, :secondary_color,
    :date_of_birth_raw, :date_of_birth, :date_of_birth_flag, :retrieved_at
)
"""

INSERT_INTAKES_CLEAN = """
INSERT INTO intakes_clean (
    animal_key, intake_at, source_record_id, intake_type_raw, intake_type,
    intake_type_flag, name_at_intake_raw, name_at_intake, name_at_intake_flag,
    previously_spayed_neutered, intake_health_raw, intake_health, intake_health_flag,
    found_zip_raw, found_zip, found_zip_flag, stay_sequence_flag, retrieved_at
) VALUES (
    :animal_key, :intake_at, :source_record_id, :intake_type_raw, :intake_type,
    :intake_type_flag, :name_at_intake_raw, :name_at_intake, :name_at_intake_flag,
    :previously_spayed_neutered, :intake_health_raw, :intake_health, :intake_health_flag,
    :found_zip_raw, :found_zip, :found_zip_flag, :stay_sequence_flag, :retrieved_at
)
"""

INSERT_OUTCOMES_CLEAN = """
INSERT INTO outcomes_clean (
    animal_key, outcome_at, intake_at, source_record_id, outcome_status_raw,
    outcome_status, outcome_status_flag, euthanasia_reason_raw, euthanasia_reason,
    euthanasia_reason_flag, name_raw, name, name_flag, spayed_neutered_raw,
    spayed_neutered, spayed_neutered_flag, days_in_shelter_raw, days_in_shelter,
    length_of_stay_days, days_in_shelter_flag, retrieved_at
) VALUES (
    :animal_key, :outcome_at, :intake_at, :source_record_id, :outcome_status_raw,
    :outcome_status, :outcome_status_flag, :euthanasia_reason_raw, :euthanasia_reason,
    :euthanasia_reason_flag, :name_raw, :name, :name_flag, :spayed_neutered_raw,
    :spayed_neutered, :spayed_neutered_flag, :days_in_shelter_raw, :days_in_shelter,
    :length_of_stay_days, :days_in_shelter_flag, :retrieved_at
)
"""


def write_cleaned_tables(con: sqlite3.Connection, built: dict) -> None:
    """Rebuilds the cleaned tables atomically inside the active transaction."""
    con.execute("DELETE FROM outcomes_clean")
    con.execute("DELETE FROM intakes_clean")
    con.execute("DELETE FROM animals_clean")

    con.executemany(INSERT_ANIMALS_CLEAN, built["animals_clean"])
    con.executemany(INSERT_INTAKES_CLEAN, built["intakes_clean"])
    con.executemany(INSERT_OUTCOMES_CLEAN, built["outcomes_clean"])


def run_clean(db_path=None, quiet=False) -> dict:
    path = Path(db_path) if db_path else get_db_path()
    if not path.exists():
        raise CleanError(f"Database not found at {path}. Run `python src/load.py` first.")

    started = time.time()
    con = connect(path)
    try:
        apply_clean_schema(con)
        hashes_before = hash_m2_tables(con)

        with con:  # single atomic transaction for mappings + all cleaned tables
            mappings = load_mappings(con)
            built = build_cleaned_layer(con, mappings)
            write_cleaned_tables(con, built)
            violations = con.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise CleanError(f"foreign_key_check failed: {violations[:5]}")

        hashes_after = hash_m2_tables(con)
        if hashes_before != hashes_after:
            raise CleanError("M2 raw tables changed during cleaning!")
    finally:
        con.close()

    summary = {
        "db_path": path,
        "raw_counts": {t: hashes_after[t][0] for t in M2_TABLES},
        "m2_hashes_identical": hashes_before == hashes_after,
        "mappings": mappings,
        "built": built,
        "seconds": round(time.time() - started, 2),
    }
    if not quiet:
        print_summary(summary)
    return summary


def format_flag_counter(rows: list, col: str) -> str:
    counts = Counter(r[col] for r in rows)
    return ", ".join(f"{flag}={n:,}" for flag, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def print_summary(s: dict) -> None:
    built = s["built"]
    ac = built["animals_clean"]
    ic = built["intakes_clean"]
    oc = built["outcomes_clean"]
    raw_c = s["raw_counts"]

    print("=" * 72)
    print("Austin Animal Center - M3 Data Cleaning Summary")
    print("=" * 72)
    print(f"database:        {s['db_path']}")
    print(f"M2 raw tables:   UNTOUCHED (SHA-256 hashes verified before & after: {s['m2_hashes_identical']})")
    print("-" * 72)

    print(f"animals_clean:   {len(ac):,} rows (raw: {raw_c['animals']:,})")
    print(f"  animal_type_flag:    {format_flag_counter(ac, 'animal_type_flag')}")
    print(f"  life_stage_flag:     {format_flag_counter(ac, 'life_stage_flag')}")
    print(f"  sex_flag:            {format_flag_counter(ac, 'sex_flag')}")
    print(f"  primary_color_flag:  {format_flag_counter(ac, 'primary_color_flag')}")
    print(f"  date_of_birth_flag:  {format_flag_counter(ac, 'date_of_birth_flag')}")

    print(f"\nintakes_clean:   {len(ic):,} rows (raw: {raw_c['intakes']:,})")
    print(f"  intake_type_flag:    {format_flag_counter(ic, 'intake_type_flag')}")
    print(f"  name_at_intake_flag: {format_flag_counter(ic, 'name_at_intake_flag')}")
    print(f"  intake_health_flag:  {format_flag_counter(ic, 'intake_health_flag')}")
    print(f"  found_zip_flag:      {format_flag_counter(ic, 'found_zip_flag')}")
    print(f"  stay_sequence_flag:  {format_flag_counter(ic, 'stay_sequence_flag')}")

    print(f"\noutcomes_clean:  {len(oc):,} rows (raw: {raw_c['outcomes']:,})")
    print(f"  outcome_status_flag:    {format_flag_counter(oc, 'outcome_status_flag')}")
    print(f"  euthanasia_reason_flag: {format_flag_counter(oc, 'euthanasia_reason_flag')}")
    print(f"  name_flag:              {format_flag_counter(oc, 'name_flag')}")
    print(f"  spayed_neutered_flag:   {format_flag_counter(oc, 'spayed_neutered_flag')}")
    print(f"  days_in_shelter_flag:   {format_flag_counter(oc, 'days_in_shelter_flag')}")

    # Per-species days_in_shelter_flag breakdown (as in section 5.1 example)
    species_by_ak = {r["animal_key"]: r["animal_type"] for r in ac}
    by_sp = defaultdict(Counter)
    for r in oc:
        by_sp[species_by_ak[r["animal_key"]]][r["days_in_shelter_flag"]] += 1

    flag_cols = ("ok", "unusual_high_stay", "duplicate_exit_for_stay", "invalid_outcome_before_intake")
    print("\noutcomes_clean days_in_shelter_flag by canonical species:")
    header = f"  {'species':<14}" + "".join(f"{c:>30}" for c in flag_cols) + f"{'IQR_fence(d)':>14}"
    print(header)
    for sp, cnt in sorted(by_sp.items(), key=lambda x: -sum(x[1].values())):
        fence = built["iqr_fences"].get(sp, 120.0)
        line = f"  {sp:<14}" + "".join(f"{cnt[c]:>30,}" for c in flag_cols) + f"{fence:>14.1f}"
        print(line)

    unmatched = built["unmatched_mappings"]
    unmatched_total = sum(len(v) for v in unmatched.values())
    m_counts = {k: len(v) for k, v in s["mappings"].items()}
    print("-" * 72)
    print(
        f"mappings:        map_animal_type {m_counts['animal_type']} rows · "
        f"map_intake_type {m_counts['intake_type']} rows · "
        f"map_outcome_status {m_counts['outcome_status']} rows · "
        f"unmatched raw values: {unmatched_total}"
    )
    print(f"duration:        {s['seconds']}s")
    print("STATUS: COMPLETE")
    print("=" * 72)


def main() -> None:
    try:
        run_clean()
    except Exception as exc:
        print(f"CLEAN FAILED: {exc}")
        print("STATUS: FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
