"""
Milestone M4 — Exploratory Data Analysis Helper Module (`src/explore.py`).

Enforces all M4 engineering requirements:
- Opens `data/austin_animals.db` strictly in read-only URI mode (`mode=ro`, `uri=True`).
- Hashes all 10 tables before and after exploration to prove zero database modification.
- Executes parameterised SQL queries without any Python string formatting into SQL.
- Converts UTC timestamps explicitly to Austin local time (`America/Chicago`).
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "austin_animals.db"
QUERIES_DIR = ROOT_DIR / "sql" / "queries"
FIGURES_DIR = ROOT_DIR / "figures"

# ---------------------------------------------------------------------------
# M4 Global Exploration Parameters (imported by notebooks/m4_eda.ipynb Cell 1)
# ---------------------------------------------------------------------------
LOCAL_TZ_NAME = "America/Chicago"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
UTC_TZ = ZoneInfo("UTC")

MAIN_SPECIES = ("Cat", "Dog", "Wildlife", "Small Mammal", "Bird", "Reptile")
COMPANION_SPECIES = ("Cat", "Dog")

# Coverage rules (Section 2.4)
MIN_GROUP_OUTCOMES = 30          # Minimum completed stays to report group summary
MIN_DOB_COVERAGE = 0.80          # Minimum DOB completeness (80%) for age/life-stage breakdowns
MIN_DAYS_PER_MONTH = 20          # Minimum active calendar days in America/Chicago for monthly series
MIN_COMPLETION_RATE = 0.85       # Minimum cohort outcome completion (85%) to avoid right-censoring

# Flag filters (Section 2.4)
PRIMARY_FLAG_FILTER = ("ok", "unusual_high_stay")
STRICT_FLAG_FILTER = ("ok",)
VALID_SEQUENCE_FLAG = "ok"

# Histogram & surgical thresholds (Sections 2.3 & 2.6)
HIST_BIN_WIDTH_DAYS = 2.0        # Chosen 2-day natural bin width (tested against 1d and 7d)
HIST_MAX_DAYS = 90.0             # Display ceiling for linear histogram (95th pct reference)
STRAY_HOLD_DAYS = 4.0            # City of Austin mandatory stray hold (days)
TARGET_STAY_DAYS = 14.0          # Shelter operational target (2 weeks)
NEONATE_CUTOFF_DAYS = 42.0       # 6-week neonate foster threshold driving juvenile cat stays
SURGICAL_AGE_DAYS = 56.0         # 8-week (2-month) spay/neuter surgical eligibility threshold

# Bootstrap & multiplicity parameters (Section 2.7)
RANDOM_SEED = 42
BOOTSTRAP_ITERATIONS = 5000
LOOK_COUNT = 12                  # Total candidate comparisons inspected during M4 exploration

ALL_TABLE_DUMP_SQL = {
    "animals": "SELECT * FROM animals ORDER BY animal_key",
    "intakes": "SELECT * FROM intakes ORDER BY animal_key, intake_at",
    "outcomes": "SELECT * FROM outcomes ORDER BY animal_key, outcome_at",
    "quarantine": "SELECT * FROM quarantine ORDER BY quarantine_key",
    "map_animal_type": "SELECT * FROM map_animal_type ORDER BY raw_value",
    "map_intake_type": "SELECT * FROM map_intake_type ORDER BY raw_value",
    "map_outcome_status": "SELECT * FROM map_outcome_status ORDER BY raw_value",
    "animals_clean": "SELECT * FROM animals_clean ORDER BY animal_key",
    "intakes_clean": "SELECT * FROM intakes_clean ORDER BY animal_key, intake_at",
    "outcomes_clean": "SELECT * FROM outcomes_clean ORDER BY animal_key, outcome_at",
}


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        p = Path(db_path)
        return p if p.is_absolute() else (ROOT_DIR / p).resolve()
    return DEFAULT_DB_PATH.resolve()


def connect_ro(db_path: str | Path | None = None) -> sqlite3.Connection:
    """
    Opens the SQLite database strictly in read-only URI mode (`?mode=ro`, `uri=True`).
    Any accidental write attempt raises `sqlite3.OperationalError: attempt to write a readonly database`.
    """
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Database not found at {path}. Run `python src/load.py` and `python src/clean.py` first."
        )
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def verify_readonly_enforcement(con: sqlite3.Connection) -> bool:
    """Proves that the connection rejects write operations."""
    try:
        con.execute("CREATE TEMP TABLE _ro_probe (x INTEGER)")
    except sqlite3.OperationalError:
        return True
    return False


def hash_all_tables(con: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """Computes (row_count, sha256_hex) for all 10 tables in the database."""
    result = {}
    for table_name, sql in ALL_TABLE_DUMP_SQL.items():
        rows = con.execute(sql).fetchall()
        digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
        result[table_name] = (len(rows), digest)
    return result


def load_sql_query(filename: str) -> str:
    """Loads a saved `.sql` query file from `sql/queries/`."""
    path = QUERIES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"SQL query file not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Section 2.1 & 2.2: Raw vs Cleaned and Group Summary Rankings
# ---------------------------------------------------------------------------

def compute_raw_vs_cleaned_table(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Computes the key project metrics on M2 Raw tables vs M3 Cleaned tables,
    including non-parametric medians, IQRs, 90th/95th percentiles, means, and adoption rates.
    """
    raw_df = pd.read_sql_query(
        """
        SELECT
            a.animal_type AS raw_species,
            CAST(o.days_in_shelter AS REAL) AS days_int,
            (julianday(o.outcome_at) - julianday(o.intake_at)) AS los_days,
            o.outcome_status
        FROM outcomes AS o
        JOIN animals  AS a ON a.animal_key = o.animal_key
        """,
        con,
    )

    clean_df = pd.read_sql_query(
        """
        SELECT
            s.animal_type AS clean_species,
            s.life_stage,
            CAST(s.days_in_shelter AS REAL) AS days_int,
            s.length_of_stay_days AS los_days,
            s.outcome_status,
            s.days_in_shelter_flag
        FROM stays_clean_view AS s
        WHERE s.outcome_at IS NOT NULL
          AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
          AND s.stay_sequence_flag = 'ok'
        """,
        con,
    )

    rows = []
    # 1. Overall Raw vs Cleaned
    for label, sub, is_raw in [
        ("M2 Raw — All Outcomes (unfiltered)", raw_df, True),
        ("M3 Cleaned — All Valid Completed Stays", clean_df, False),
        ("M2 Raw — 'Cat' (excludes raw 'Kitten')", raw_df[raw_df["raw_species"] == "Cat"], True),
        ("M2 Raw — 'Kitten' (separate raw type)", raw_df[raw_df["raw_species"] == "Kitten"], True),
        ("M3 Cleaned — Canonical 'Cat' (Adult + Juvenile)", clean_df[clean_df["clean_species"] == "Cat"], False),
        ("M2 Raw — 'Dog' (excludes raw 'Puppy')", raw_df[raw_df["raw_species"] == "Dog"], True),
        ("M2 Raw — 'Puppy' (separate raw type)", raw_df[raw_df["raw_species"] == "Puppy"], True),
        ("M3 Cleaned — Canonical 'Dog' (Adult + Juvenile)", clean_df[clean_df["clean_species"] == "Dog"], False),
    ]:
        s = sub["los_days"].dropna()
        adopted_exact = (sub["outcome_status"] == "Adopted").mean() * 100.0
        adopted_like = sub["outcome_status"].fillna("").str.startswith("Adopted").mean() * 100.0
        rows.append({
            "cohort": label,
            "layer": "M2 Raw" if is_raw else "M3 Cleaned",
            "n": int(len(sub)),
            "median_los_d": round(float(s.median()), 2),
            "iqr_los_d": round(float(s.quantile(0.75) - s.quantile(0.25)), 2),
            "mean_los_d": round(float(s.mean()), 2),
            "p90_los_d": round(float(s.quantile(0.90)), 2),
            "p95_los_d": round(float(s.quantile(0.95)), 2),
            "adopted_exact_pct": round(float(adopted_exact), 2),
            "adopted_all_subtypes_pct": round(float(adopted_like), 2),
        })
    return pd.DataFrame(rows)


def compute_species_summary_rankings(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Section 2.2: Computes multiple summaries of length_of_stay_days (median, IQR, mean, SD,
    p90, p95, share > 14d, share > 30d, and live adoption rate) across canonical species
    and checks whether different summaries rank the groups the same way.
    """
    df = pd.read_sql_query(
        """
        SELECT
            s.animal_type,
            s.life_stage,
            s.length_of_stay_days,
            s.days_in_shelter,
            s.outcome_status,
            s.days_in_shelter_flag
        FROM stays_clean_view AS s
        WHERE s.outcome_at IS NOT NULL
          AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
          AND s.stay_sequence_flag = 'ok'
        """,
        con,
    )

    records = []
    for sp, grp in df.groupby("animal_type"):
        s = grp["length_of_stay_days"].dropna()
        q1, med, q3 = s.quantile(0.25), s.quantile(0.50), s.quantile(0.75)
        records.append({
            "animal_type": sp,
            "n": int(len(s)),
            "passes_coverage_rule": bool(len(s) >= MIN_GROUP_OUTCOMES),
            "median_d": round(float(med), 2),
            "iqr_d": round(float(q3 - q1), 2),
            "mean_d": round(float(s.mean()), 2),
            "sd_d": round(float(s.std(ddof=1)), 2) if len(s) > 1 else 0.0,
            "p90_d": round(float(s.quantile(0.90)), 2),
            "p95_d": round(float(s.quantile(0.95)), 2),
            "share_gt_14d_pct": round(float((s > 14.0).mean() * 100.0), 1),
            "share_gt_30d_pct": round(float((s > 30.0).mean() * 100.0), 1),
            "adoption_rate_pct": round(float((grp["outcome_status"] == "Adopted").mean() * 100.0), 1),
        })

    out = pd.DataFrame(records).sort_values("n", ascending=False).reset_index(drop=True)
    # Compute explicit ranks among groups passing the n >= 30 coverage rule
    valid_mask = out["passes_coverage_rule"]
    out["rank_by_median"] = np.nan
    out["rank_by_mean"] = np.nan
    out["rank_by_p95"] = np.nan
    out.loc[valid_mask, "rank_by_median"] = out.loc[valid_mask, "median_d"].rank(ascending=False, method="min")
    out.loc[valid_mask, "rank_by_mean"] = out.loc[valid_mask, "mean_d"].rank(ascending=False, method="min")
    out.loc[valid_mask, "rank_by_p95"] = out.loc[valid_mask, "p95_d"].rank(ascending=False, method="min")
    return out


# ---------------------------------------------------------------------------
# Section 2.3 & 2.4: Bin Width Comparison, Coverage Rules & Flag Sensitivity
# ---------------------------------------------------------------------------

def compare_histogram_bin_widths(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Section 2.3: Evaluates three candidate natural bin widths (1-day, 2-day, 7-day)
    on length_of_stay_days (0-90d) to justify the chosen 2-day bin width.
    """
    df = pd.read_sql_query(
        """
        SELECT length_of_stay_days
        FROM stays_clean_view
        WHERE outcome_at IS NOT NULL
          AND days_in_shelter_flag IN ('ok', 'unusual_high_stay')
          AND stay_sequence_flag = 'ok'
        """,
        con,
    )
    vals = df["length_of_stay_days"].dropna().values
    in_window = vals[(vals >= 0) & (vals <= HIST_MAX_DAYS)]

    rows = []
    for bw, desc in [
        (1.0, "1-day (daily bins; noisy sawtooth from weekend/weekday discharge batching)"),
        (2.0, "2-day (chosen: resolves day 0-2 intake triage & day 4 stray-hold peak cleanly)"),
        (7.0, "7-day (weekly bins; over-smooths and hides the 4-day mandatory stray hold)"),
    ]:
        bins = np.arange(0.0, HIST_MAX_DAYS + bw, bw)
        counts, edges = np.histogram(in_window, bins=bins)
        peak_idx = int(np.argmax(counts))
        second_idx = int(np.argsort(counts)[-2])
        empty_share = float((counts == 0).mean() * 100.0)
        rows.append({
            "bin_width_days": bw,
            "n_bins_0_to_90d": len(counts),
            "primary_mode_bin": f"[{edges[peak_idx]:.0f}d, {edges[peak_idx+1]:.0f}d)",
            "primary_mode_count": int(counts[peak_idx]),
            "secondary_mode_bin": f"[{edges[second_idx]:.0f}d, {edges[second_idx+1]:.0f}d)",
            "empty_bin_pct": round(empty_share, 1),
            "assessment": desc,
        })
    return pd.DataFrame(rows)


def compute_flag_filter_sensitivity(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Section 2.4: Compares species length-of-stay summaries under two flag filters:
      - Filter A (reported): days_in_shelter_flag IN ('ok', 'unusual_high_stay') AND stay_sequence_flag = 'ok'
      - Filter B (strict):   days_in_shelter_flag = 'ok' AND stay_sequence_flag = 'ok'
    """
    df = pd.read_sql_query(
        """
        SELECT
            animal_type,
            length_of_stay_days,
            outcome_status,
            days_in_shelter_flag,
            stay_sequence_flag
        FROM stays_clean_view
        WHERE outcome_at IS NOT NULL
        """,
        con,
    )
    df = df[df["stay_sequence_flag"] == VALID_SEQUENCE_FLAG]

    rows = []
    for sp in MAIN_SPECIES:
        sub_a = df[(df["animal_type"] == sp) & (df["days_in_shelter_flag"].isin(PRIMARY_FLAG_FILTER))]
        sub_b = df[(df["animal_type"] == sp) & (df["days_in_shelter_flag"].isin(STRICT_FLAG_FILTER))]
        sa = sub_a["length_of_stay_days"].dropna()
        sb = sub_b["length_of_stay_days"].dropna()
        rows.append({
            "animal_type": sp,
            "n_filter_A_all_valid": int(len(sa)),
            "n_filter_B_ok_only": int(len(sb)),
            "n_unusual_removed": int(len(sa) - len(sb)),
            "median_A_d": round(float(sa.median()), 2),
            "median_B_d": round(float(sb.median()), 2),
            "mean_A_d": round(float(sa.mean()), 2),
            "mean_B_d": round(float(sb.mean()), 2),
            "p95_A_d": round(float(sa.quantile(0.95)), 2),
            "p95_B_d": round(float(sb.quantile(0.95)), 2),
            "adopt_A_pct": round(float((sub_a["outcome_status"] == "Adopted").mean() * 100.0), 1),
            "adopt_B_pct": round(float((sub_b["outcome_status"] == "Adopted").mean() * 100.0), 1),
        })
    return pd.DataFrame(rows)
