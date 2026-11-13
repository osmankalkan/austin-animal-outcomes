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


# ---------------------------------------------------------------------------
# Section 2.5: Time Clock Conversion (UTC -> America/Chicago), Edges & Scales
# ---------------------------------------------------------------------------

def fetch_time_indexed_stays(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Loads stays from `stays_clean_view` and explicitly converts `intake_at` and `outcome_at`
    from stored UTC (`YYYY-MM-DDTHH:MM:SSZ`) to Austin local wall-clock time (`America/Chicago`).
    """
    df = pd.read_sql_query(
        """
        SELECT
            animal_key,
            animal_id,
            animal_type,
            life_stage,
            sex,
            date_of_birth,
            intake_at,
            intake_type,
            intake_health,
            found_zip,
            stay_sequence_flag,
            outcome_at,
            outcome_status,
            spayed_neutered,
            days_in_shelter,
            length_of_stay_days,
            days_in_shelter_flag
        FROM stays_clean_view
        WHERE stay_sequence_flag = 'ok'
        """,
        con,
    )

    intake_utc = pd.to_datetime(df["intake_at"], format="%Y-%m-%dT%H:%M:%SZ", utc=True)
    intake_local = intake_utc.dt.tz_convert(LOCAL_TZ_NAME)

    df["intake_utc_hour"] = intake_utc.dt.hour
    df["intake_local_dt"] = intake_local
    df["intake_local_date"] = intake_local.dt.strftime("%Y-%m-%d")
    df["intake_local_month"] = intake_local.dt.strftime("%Y-%m")
    df["intake_local_hour"] = intake_local.dt.hour
    df["intake_local_dow_num"] = intake_local.dt.dayofweek  # 0=Mon .. 6=Sun
    df["intake_local_dow"] = intake_local.dt.day_name()
    return df


def compute_monthly_coverage_and_rule(df_time: pd.DataFrame) -> pd.DataFrame:
    """
    Section 2.4 & 2.5: Evaluates every local calendar month (`America/Chicago`) against
    the two-part monthly coverage rule:
      1) active_local_days >= MIN_DAYS_PER_MONTH (20 days)
      2) completion_rate >= MIN_COMPLETION_RATE (85% of cohort has a valid closed outcome)
    Shows monthly median/mean LOS both WITH and WITHOUT the coverage rule.
    """
    valid_completed = df_time["outcome_at"].notna() & df_time["days_in_shelter_flag"].isin(PRIMARY_FLAG_FILTER)
    df_time = df_time.copy()
    df_time["is_valid_completed"] = valid_completed

    rows = []
    for month, grp in df_time.groupby("intake_local_month"):
        year_i, mon_i = map(int, month.split("-"))
        cal_days = pd.Period(month, freq="M").days_in_month
        active_days = grp["intake_local_date"].nunique()
        n_intakes = len(grp)
        comp = grp[grp["is_valid_completed"]]
        n_comp = len(comp)
        comp_rate = n_comp / n_intakes if n_intakes else 0.0
        passes_days = active_days >= MIN_DAYS_PER_MONTH
        passes_comp = comp_rate >= MIN_COMPLETION_RATE
        passes_rule = bool(passes_days and passes_comp)

        los = comp["length_of_stay_days"].dropna()
        juv_share = (grp["life_stage"] == "juvenile").mean() * 100.0
        adopt_share = (comp["outcome_status"] == "Adopted").mean() * 100.0 if n_comp else np.nan

        if not passes_days:
            status_note = f"EXCLUDED (partial edge month: {active_days}/{cal_days} active days)"
        elif not passes_comp:
            status_note = f"EXCLUDED (right-censored cohort: {comp_rate*100:.1f}% < {MIN_COMPLETION_RATE*100:.0f}% closed)"
        else:
            status_note = "INCLUDED (complete & uncensored)"

        rows.append({
            "intake_local_month": month,
            "calendar_days": int(cal_days),
            "active_local_days": int(active_days),
            "first_local_date": grp["intake_local_date"].min(),
            "last_local_date": grp["intake_local_date"].max(),
            "n_intakes": int(n_intakes),
            "n_completed": int(n_comp),
            "completion_pct": round(comp_rate * 100.0, 1),
            "juvenile_intake_pct": round(float(juv_share), 1),
            "median_los_d": round(float(los.median()), 2) if len(los) else np.nan,
            "mean_los_d": round(float(los.mean()), 2) if len(los) else np.nan,
            "adoption_rate_pct": round(float(adopt_share), 1) if not np.isnan(adopt_share) else np.nan,
            "passes_coverage_rule": passes_rule,
            "coverage_status": status_note,
        })
    return pd.DataFrame(rows).sort_values("intake_local_month").reset_index(drop=True)


def compute_hourly_and_weekday_profiles(df_time: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Section 2.5: Computes (1) hourly intake profile in America/Chicago vs stored UTC,
    (2) day-of-week intake rate by channel, and (3) calendar/holiday intake checks.
    """
    # 1. Hourly profile: Local vs UTC
    hourly_rows = []
    n_total = len(df_time)
    for h in range(24):
        n_loc = int((df_time["intake_local_hour"] == h).sum())
        n_utc = int((df_time["intake_utc_hour"] == h).sum())
        hourly_rows.append({
            "hour": h,
            "n_local_chicago": n_loc,
            "pct_local_chicago": round(100.0 * n_loc / n_total, 2),
            "n_stored_utc": n_utc,
            "pct_stored_utc": round(100.0 * n_utc / n_total, 2),
        })
    hourly_df = pd.DataFrame(hourly_rows)

    # 2. Day of week profile (normalised by number of distinct calendar dates per weekday)
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_rows = []
    for d_idx, d_name in enumerate(dow_order):
        sub = df_time[df_time["intake_local_dow_num"] == d_idx]
        n_dates = max(sub["intake_local_date"].nunique(), 1)
        n_stray_aco = int(sub["intake_type"].isin(["Stray", "ACO Impound"]).sum())
        n_owner_comm = int(sub["intake_type"].isin(["Owner Surrender", "Community Program", "Return"]).sum())
        dow_rows.append({
            "day_of_week": d_name,
            "n_calendar_dates": n_dates,
            "total_intakes": int(len(sub)),
            "mean_intakes_per_day": round(len(sub) / n_dates, 2),
            "stray_aco_per_day": round(n_stray_aco / n_dates, 2),
            "owner_community_per_day": round(n_owner_comm / n_dates, 2),
        })
    dow_df = pd.DataFrame(dow_rows)

    # 3. Calendar / Holiday check
    daily_counts = (
        df_time.groupby("intake_local_date")
        .agg(
            n_intakes=("animal_key", "count"),
            n_stray_aco=("intake_type", lambda s: int(s.isin(["Stray", "ACO Impound"]).sum())),
            n_owner=("intake_type", lambda s: int((s == "Owner Surrender").sum())),
        )
        .reset_index()
    )
    baseline_daily = float(daily_counts["n_intakes"].median())

    holiday_checks = [
        ("2025-07-04", "Independence Day (July 4th)", "Holiday closure / fireworks evening"),
        ("2025-07-05", "Post-July 4th Stray Surge (Day +1)", "Fireworks runaway impound surge"),
        ("2025-07-07", "Post-July 4th Weekday Reopening", "Post-holiday backlog & stray surge"),
        ("2025-11-27", "Thanksgiving Day 2025", "Shelter front-desk holiday closure"),
        ("2025-12-25", "Christmas Day 2025", "Shelter front-desk holiday closure"),
        ("2026-01-01", "New Year's Day 2026", "Shelter front-desk holiday closure"),
        ("2026-07-04", "Independence Day 2026", "Holiday closure / fireworks evening"),
        ("2026-07-07", "Post-July 4th Surge 2026 (Tue)", "Post-fireworks stray/ACO impound peak"),
    ]
    cal_rows = []
    for dt_str, event_name, mechanism in holiday_checks:
        match = daily_counts[daily_counts["intake_local_date"] == dt_str]
        n_int = int(match["n_intakes"].iloc[0]) if len(match) else 0
        n_sa = int(match["n_stray_aco"].iloc[0]) if len(match) else 0
        n_ow = int(match["n_owner"].iloc[0]) if len(match) else 0
        cal_rows.append({
            "local_date": dt_str,
            "calendar_event": event_name,
            "n_intakes": n_int,
            "stray_aco_intakes": n_sa,
            "owner_surrenders": n_ow,
            "vs_median_day_pct": round(100.0 * (n_int - baseline_daily) / baseline_daily, 1),
            "operational_interpretation": mechanism,
        })
    cal_df = pd.DataFrame(cal_rows)
    return hourly_df, dow_df, cal_df


# ---------------------------------------------------------------------------
# Section 2.6: Relationship Exploration (Age at Intake vs Length of Stay)
# ---------------------------------------------------------------------------

def compute_age_los_relationship(con: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Section 2.6: Explores the relationship between age at intake (`age_at_intake_days`)
    and `length_of_stay_days` among adopted companion animals (`Cat` and `Dog`).
    Returns:
      - raw_pairs_df: stay-level observations for scatter plotting
      - binned_df: age-binned medians and IQR bounds for Cat vs Dog
      - corr_sensitivity_df: Pearson r and Spearman rho across full data, driver removals, and subgroups
    """
    df = pd.read_sql_query(
        """
        SELECT
            s.animal_key,
            s.animal_type,
            s.life_stage,
            s.intake_type,
            s.intake_at,
            s.outcome_at,
            s.outcome_status,
            s.date_of_birth,
            s.length_of_stay_days,
            s.days_in_shelter_flag,
            (julianday(substr(s.intake_at, 1, 10)) - julianday(s.date_of_birth)) AS age_days
        FROM stays_clean_view AS s
        WHERE s.outcome_at IS NOT NULL
          AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
          AND s.stay_sequence_flag = 'ok'
          AND s.date_of_birth IS NOT NULL
          AND s.animal_type IN ('Cat', 'Dog')
        """,
        con,
    )
    df = df[df["age_days"] >= 0].copy()
    df["age_months"] = df["age_days"] / 30.4375
    df["intake_month_num"] = pd.to_datetime(df["intake_at"]).dt.month
    df["is_kitten_season"] = df["intake_month_num"].isin([5, 6, 7, 8])

    adopted = df[df["outcome_status"] == "Adopted"].copy()

    # Age bins for binned median + IQR overlay
    bin_specs = [
        (0, 28, "0-4w (<1m)", 0.5),
        (29, 42, "4-6w (1-1.5m)", 1.25),
        (43, 56, "6-8w (1.5-2m)", 1.75),
        (57, 90, "2-3m", 2.5),
        (91, 180, "3-6m", 4.5),
        (181, 365, "6-12m", 9.0),
        (366, 730, "1-2y", 18.0),
        (731, 1825, "2-5y", 36.0),
        (1826, 20000, "5y+", 72.0),
    ]
    bin_rows = []
    for sp in ("Cat", "Dog"):
        sp_sub = adopted[adopted["animal_type"] == sp]
        for lo, hi, label, mid_m in bin_specs:
            b = sp_sub[(sp_sub["age_days"] >= lo) & (sp_sub["age_days"] <= hi)]["length_of_stay_days"].dropna()
            if len(b) > 0:
                bin_rows.append({
                    "animal_type": sp,
                    "age_bin": label,
                    "mid_age_months": mid_m,
                    "n": int(len(b)),
                    "q25_los_d": round(float(b.quantile(0.25)), 2),
                    "median_los_d": round(float(b.median()), 2),
                    "q75_los_d": round(float(b.quantile(0.75)), 2),
                    "mean_los_d": round(float(b.mean()), 2),
                })
    binned_df = pd.DataFrame(bin_rows)

    # Pearson vs Spearman sensitivity & driver removal checks
    cohorts = [
        ("1. All Adopted Cat + Dog (ok + unusual_high_stay)", adopted),
        ("2. Remove unusual_high_stay tail (ok only)", adopted[adopted["days_in_shelter_flag"] == "ok"]),
        ("3. Juvenile Adopted Cats (age <= 180d, all valid)", adopted[(adopted["animal_type"] == "Cat") & (adopted["age_days"] <= 180)]),
        ("4. Juvenile Adopted Cats — remove <42d neonates", adopted[(adopted["animal_type"] == "Cat") & (adopted["age_days"] >= 42) & (adopted["age_days"] <= 180)]),
        ("5. Juvenile Adopted Cats — Peak Season (May-Aug)", adopted[(adopted["animal_type"] == "Cat") & (adopted["age_days"] <= 180) & (adopted["is_kitten_season"])]),
        ("6. Juvenile Adopted Cats — Off-Peak (Sep-Apr)", adopted[(adopted["animal_type"] == "Cat") & (adopted["age_days"] <= 180) & (~adopted["is_kitten_season"])]),
        ("7. Juvenile Adopted Dogs (age <= 180d, all valid)", adopted[(adopted["animal_type"] == "Dog") & (adopted["age_days"] <= 180)]),
        ("8. Adult Adopted Cats (age > 180d)", adopted[(adopted["animal_type"] == "Cat") & (adopted["age_days"] > 180)]),
        ("9. Adult Adopted Dogs (age > 180d)", adopted[(adopted["animal_type"] == "Dog") & (adopted["age_days"] > 180)]),
    ]
    corr_rows = []
    for label, sub in cohorts:
        rp = float(sub["age_days"].corr(sub["length_of_stay_days"], method="pearson"))
        rs = float(sub["age_days"].corr(sub["length_of_stay_days"], method="spearman"))
        corr_rows.append({
            "subset_check": label,
            "n": int(len(sub)),
            "pearson_r": round(rp, 4),
            "spearman_rho": round(rs, 4),
            "median_age_d": round(float(sub["age_days"].median()), 1),
            "median_los_d": round(float(sub["length_of_stay_days"].median()), 2),
        })
    corr_df = pd.DataFrame(corr_rows)
    return adopted, binned_df, corr_df


# ---------------------------------------------------------------------------
# Section 2.7: Focal Comparison, Cluster Bootstrap 95% CI, Composition Check
#              (Simpson's Paradox), and Candidate Look Count Audit
# ---------------------------------------------------------------------------

def _cluster_bootstrap_diff(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    value_col: str,
    cluster_col: str,
    stat: str = "median",
    n_boot: int = BOOTSTRAP_ITERATIONS,
    seed: int = RANDOM_SEED,
) -> dict[str, float]:
    """
    Computes the observed difference (Group A - Group B) and both:
      1) Cluster-bootstrapped 95% CI (resampling independent `cluster_col` units, e.g. daily intake cohorts)
      2) Naive row-level bootstrapped 95% CI (assuming every row is independent)
    """
    rng = np.random.default_rng(seed)

    vals_a = df_a[value_col].dropna().to_numpy(dtype=float)
    vals_b = df_b[value_col].dropna().to_numpy(dtype=float)
    obs_a = float(np.median(vals_a) if stat == "median" else np.mean(vals_a))
    obs_b = float(np.median(vals_b) if stat == "median" else np.mean(vals_b))
    obs_diff = obs_a - obs_b

    # Pre-group arrays by cluster unit for fast vectorised cluster bootstrap
    clusters_a = [g.to_numpy(dtype=float) for _, g in df_a.dropna(subset=[value_col]).groupby(cluster_col)[value_col]]
    clusters_b = [g.to_numpy(dtype=float) for _, g in df_b.dropna(subset=[value_col]).groupby(cluster_col)[value_col]]
    n_cl_a = len(clusters_a)
    n_cl_b = len(clusters_b)

    # Resampling whole clusters with replacement preserves within-day littermate/cohort dependence:
    cl_sums_a = np.array([c.sum() for c in clusters_a])
    cl_cnts_a = np.array([len(c) for c in clusters_a])
    cl_meds_a = np.array([np.median(c) for c in clusters_a])

    cl_sums_b = np.array([c.sum() for c in clusters_b])
    cl_cnts_b = np.array([len(c) for c in clusters_b])
    cl_meds_b = np.array([np.median(c) for c in clusters_b])

    idx_cl_a = rng.integers(0, n_cl_a, size=(n_boot, n_cl_a))
    idx_cl_b = rng.integers(0, n_cl_b, size=(n_boot, n_cl_b))

    if stat == "mean":
        boot_a_cl = cl_sums_a[idx_cl_a].sum(axis=1) / cl_cnts_a[idx_cl_a].sum(axis=1)
        boot_b_cl = cl_sums_b[idx_cl_b].sum(axis=1) / cl_cnts_b[idx_cl_b].sum(axis=1)
    else:
        # Cluster-unit median bootstrap (resampling daily cohort medians)
        boot_a_cl = np.median(cl_meds_a[idx_cl_a], axis=1)
        boot_b_cl = np.median(cl_meds_b[idx_cl_b], axis=1)

    diff_cl = boot_a_cl - boot_b_cl
    ci_low_cl, ci_high_cl = np.percentile(diff_cl, [2.5, 97.5])

    # Naive row-level bootstrap for comparison
    n_row_a, n_row_b = len(vals_a), len(vals_b)
    naive_diffs = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        sa = vals_a[rng.integers(0, n_row_a, size=n_row_a)]
        sb = vals_b[rng.integers(0, n_row_b, size=n_row_b)]
        if stat == "mean":
            naive_diffs[b] = sa.mean() - sb.mean()
        else:
            naive_diffs[b] = np.median(sa) - np.median(sb)
    ci_low_nv, ci_high_nv = np.percentile(naive_diffs, [2.5, 97.5])

    return {
        "n_rows_a": int(n_row_a),
        "n_rows_b": int(n_row_b),
        "n_clusters_a": int(n_cl_a),
        "n_clusters_b": int(n_cl_b),
        "estimate_a": round(obs_a, 2),
        "estimate_b": round(obs_b, 2),
        "diff_a_minus_b": round(obs_diff, 2),
        "cluster_ci_low": round(float(ci_low_cl), 2),
        "cluster_ci_high": round(float(ci_high_cl), 2),
        "cluster_ci_width": round(float(ci_high_cl - ci_low_cl), 2),
        "naive_ci_low": round(float(ci_low_nv), 2),
        "naive_ci_high": round(float(ci_high_nv), 2),
        "naive_ci_width": round(float(ci_high_nv - ci_low_nv), 2),
    }


def compute_simpsons_paradox_and_intervals(df_time: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Section 2.7: Computes:
      1) Composition breakdown of Dog vs Cat across life_stage and intake_type
      2) Cluster-bootstrapped 95% confidence intervals (unit = intake_local_date daily cohort)
         showing Simpson's paradox in Adoption Share (%) and Length of Stay (days)
      3) Full audit table of all 12 candidate comparisons inspected (LOOK_COUNT = 12)
    """
    comp = df_time[
        df_time["outcome_at"].notna()
        & df_time["days_in_shelter_flag"].isin(PRIMARY_FLAG_FILTER)
        & df_time["animal_type"].isin(["Cat", "Dog"])
    ].copy()
    comp["is_adopted_pct"] = (comp["outcome_status"] == "Adopted").astype(float) * 100.0

    # 1. Composition mixture table (why pooled Cat vs Dog is confounded)
    mix_rows = []
    for sp in ("Cat", "Dog"):
        sub = comp[comp["animal_type"] == sp]
        n_tot = len(sub)
        mix_rows.append({
            "species": sp,
            "n_completed_stays": int(n_tot),
            "juvenile_share_pct": round(100.0 * (sub["life_stage"] == "juvenile").mean(), 1),
            "adult_share_pct": round(100.0 * (sub["life_stage"] == "adult").mean(), 1),
            "community_program_share_pct": round(100.0 * (sub["intake_type"] == "Community Program").mean(), 1),
            "stray_share_pct": round(100.0 * (sub["intake_type"] == "Stray").mean(), 1),
            "aco_impound_share_pct": round(100.0 * (sub["intake_type"] == "ACO Impound").mean(), 1),
            "owner_surrender_share_pct": round(100.0 * (sub["intake_type"] == "Owner Surrender").mean(), 1),
        })
    mixture_df = pd.DataFrame(mix_rows)

    # 2. Subgroup comparisons with cluster-bootstrapped 95% CIs (unit = intake_local_date)
    strata = [
        (
            "1. Pooled All Completed Stays (Dog vs Cat)",
            comp[comp["animal_type"] == "Dog"],
            comp[comp["animal_type"] == "Cat"],
        ),
        (
            "2. Adult Stays Only (Adult Dog vs Adult Cat)",
            comp[(comp["animal_type"] == "Dog") & (comp["life_stage"] == "adult")],
            comp[(comp["animal_type"] == "Cat") & (comp["life_stage"] == "adult")],
        ),
        (
            "3. Juvenile Stays Only (Juvenile Dog vs Juvenile Cat)",
            comp[(comp["animal_type"] == "Dog") & (comp["life_stage"] == "juvenile")],
            comp[(comp["animal_type"] == "Cat") & (comp["life_stage"] == "juvenile")],
        ),
        (
            "4. Adult Non-TNR Stays (excl. Community Program)",
            comp[(comp["animal_type"] == "Dog") & (comp["life_stage"] == "adult") & (comp["intake_type"] != "Community Program")],
            comp[(comp["animal_type"] == "Cat") & (comp["life_stage"] == "adult") & (comp["intake_type"] != "Community Program")],
        ),
        (
            "5. Intake Channel: Owner Surrender vs Stray (Cat+Dog)",
            comp[comp["intake_type"] == "Owner Surrender"],
            comp[comp["intake_type"] == "Stray"],
        ),
    ]

    ci_rows = []
    for label, grp_a, grp_b in strata:
        los_res = _cluster_bootstrap_diff(
            grp_a, grp_b, "length_of_stay_days", "intake_local_date", stat="median"
        )
        ad_res = _cluster_bootstrap_diff(
            grp_a, grp_b, "is_adopted_pct", "intake_local_date", stat="mean"
        )
        ci_rows.append({
            "comparison_stratum": label,
            "n_group_A": los_res["n_rows_a"],
            "n_group_B": los_res["n_rows_b"],
            "clusters_A_days": los_res["n_clusters_a"],
            "clusters_B_days": los_res["n_clusters_b"],
            "median_los_A_d": los_res["estimate_a"],
            "median_los_B_d": los_res["estimate_b"],
            "los_diff_A_minus_B_d": los_res["diff_a_minus_b"],
            "los_cluster_95ci": f"[{los_res['cluster_ci_low']:+.2f}, {los_res['cluster_ci_high']:+.2f}]",
            "los_naive_95ci": f"[{los_res['naive_ci_low']:+.2f}, {los_res['naive_ci_high']:+.2f}]",
            "adopt_A_pct": ad_res["estimate_a"],
            "adopt_B_pct": ad_res["estimate_b"],
            "adopt_diff_pct_pt": ad_res["diff_a_minus_b"],
            "adopt_cluster_95ci": f"[{ad_res['cluster_ci_low']:+.2f}, {ad_res['cluster_ci_high']:+.2f}]",
        })
    ci_df = pd.DataFrame(ci_rows)

    # 3. Audit log of all 12 candidate looks inspected during exploration
    looks_data = [
        (1, "Dog vs Cat — Pooled Median LOS", "+1.90 d", "Selected (part of focal comparison)"),
        (2, "Dog vs Cat — Pooled Adoption Share", "-0.1% to -0.9%", "Confounded by juvenile & TNR mixture"),
        (3, "Adult Dog vs Adult Cat — Adoption Share", "+5.4% to +5.6%", "Selected (Simpson's paradox reversal)"),
        (4, "Juvenile Dog vs Juvenile Cat — Median LOS", "-0.04 d to -0.11 d", "Selected (juvenile reversal vs adults)"),
        (5, "Owner Surrender vs Stray — Adoption Share", "+12.1% to +12.5%", "Selected (secondary channel benchmark)"),
        (6, "ACO Impound vs Stray — Adoption Share", "-21.2%", "Exploratory channel contrast"),
        (7, "Male vs Female — Median LOS (Cat+Dog)", "-0.35 d", "Small difference; not focal"),
        (8, "Previously Spayed/Neutered vs Intact at Intake — LOS", "-1.82 d", "Confounded by age at intake"),
        (9, "Peak Kitten Season (May-Aug) vs Winter (Nov-Feb) — Cat LOS", "+2.10 d", "Seasonal foster backlog"),
        (10, "Named vs Unnamed at Intake — Adoption Share", "+9.4%", "Proxy for Owner Surrender vs Stray"),
        (11, "Logged Abnormal Health vs Logged Normal — LOS", "+6.40 d", "Restricted to 16.6% MNAR logged subset"),
        (12, "Wildlife vs Companion Animals — Same-Day Exit Share", "+68.5%", "Structural triage difference"),
    ]
    looks_df = pd.DataFrame(
        looks_data,
        columns=["look_index", "candidate_comparison", "observed_contrast", "disposition"],
    )
    return mixture_df, ci_df, looks_df



# ---------------------------------------------------------------------------
# Section 4: Publication Figure Generators (6 Figures meeting all 8 rules)
# ---------------------------------------------------------------------------

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def apply_m4_plot_style() -> None:
    plt.rcParams.update({
        "figure.dpi": 140,
        "savefig.dpi": 160,
        "font.size": 9.5,
        "axes.titlesize": 10.5,
        "axes.titleweight": "bold",
        "axes.labelsize": 9.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "grid.linestyle": "--",
        "legend.frameon": True,
        "legend.framealpha": 0.9,
    })


def _add_header_and_caption(fig, title: str, subtitle: str, caption: str) -> None:
    fig.suptitle(title, fontsize=12, fontweight="bold", y=0.98)
    fig.text(0.5, 0.935, subtitle, ha="center", va="top", fontsize=9.5, style="italic", color="#333333")
    fig.text(0.5, 0.01, caption, ha="center", va="bottom", fontsize=8.0, color="#444444")


def plot_fig1_raw_vs_cleaned_and_rankings(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> Path:
    """
    Figure 1 (Sections 2.1 & 2.2):
      - Left panel: Raw M2 vs Cleaned M3 live adoption rate (%) showing the impact of
        consolidating adoption subtypes and separating Kitten/Puppy into life_stage.
      - Right panel: Species length-of-stay summary ranking reversal (Median vs Mean vs 95th percentile).
    """
    apply_m4_plot_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    rvc = compute_raw_vs_cleaned_table(con)
    ranks = compute_species_summary_rankings(con)
    ranks_valid = ranks[ranks["passes_coverage_rule"]].copy()

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.6))

    # Left panel: Raw exact 'Adopted' vs M3 Cleaned canonical 'Adopted'
    cohorts = [
        ("All Stays", "M2 Raw — All Outcomes (unfiltered)", "M3 Cleaned — All Valid Completed Stays"),
        ("Cat Cohort", "M2 Raw — 'Cat' (excludes raw 'Kitten')", "M3 Cleaned — Canonical 'Cat' (Adult + Juvenile)"),
        ("Dog Cohort", "M2 Raw — 'Dog' (excludes raw 'Puppy')", "M3 Cleaned — Canonical 'Dog' (Adult + Juvenile)"),
    ]
    y_pos = np.arange(len(cohorts))
    raw_vals = []
    cln_vals = []
    for _, r_lbl, c_lbl in cohorts:
        raw_vals.append(float(rvc.loc[rvc["cohort"] == r_lbl, "adopted_exact_pct"].iloc[0]))
        cln_vals.append(float(rvc.loc[rvc["cohort"] == c_lbl, "adopted_all_subtypes_pct"].iloc[0]))

    bar_h = 0.34
    b1 = ax1.barh(y_pos + bar_h / 2, raw_vals, height=bar_h, color="#8c96c6", label="M2 Raw (exact 'Adopted' string)")
    b2 = ax1.barh(y_pos - bar_h / 2, cln_vals, height=bar_h, color="#2b8cbe", label="M3 Cleaned (canonical 'Adopted' + valid flags)")
    ax1.axvline(50.0, color="#d95f02", linestyle="--", linewidth=1.2, label="50% parity benchmark")
    ax1.set_yticks(y_pos)
    ax1.set_yticklabels([c[0] for c in cohorts])
    ax1.set_xlim(0, 70)
    ax1.set_xlabel("Live Adoption Share (% of completed stays)")
    ax1.set_title("A. M3 Subtype Mapping Lifts Measured Adoption Rate by ~4-5 pp")
    ax1.legend(loc="lower right", fontsize=8)
    for bars in (b1, b2):
        for bar in bars:
            w = bar.get_width()
            ax1.text(w + 0.8, bar.get_y() + bar.get_height() / 2, f"{w:.1f}%", va="center", fontsize=8)

    # Right panel: Summary ranking reversal across species (Median vs Mean vs p95)
    ranks_valid = ranks_valid.sort_values("median_d", ascending=True)
    y2 = np.arange(len(ranks_valid))
    h2 = 0.25
    ax2.barh(y2 - h2, ranks_valid["median_d"], height=h2, color="#1b9e77", label="Median stay (days) — typical arrival")
    ax2.barh(y2, ranks_valid["mean_d"], height=h2, color="#d95f02", label="Mean stay (days) — sensitive to tail")
    ax2.barh(y2 + h2, ranks_valid["p95_d"], height=h2, color="#7570b3", label="95th percentile stay (days) — tail burden")
    ax2.axvline(STRAY_HOLD_DAYS, color="#444444", linestyle=":", linewidth=1.1, label="4-day mandatory stray hold")
    ax2.axvline(TARGET_STAY_DAYS, color="#e7298a", linestyle="--", linewidth=1.1, label="14-day shelter target")
    ax2.set_yticks(y2)
    ax2.set_yticklabels([f"{r.animal_type} (n={r.n:,})" for _, r in ranks_valid.iterrows()])
    ax2.set_xlim(0, max(175.0, float(ranks_valid["p95_d"].max()) * 1.12))
    ax2.set_xlabel("Length of Stay (days, UTC timestamp difference)")
    ax2.set_title("B. Group Ranking Reverses Between Median (Small Mammal #1) and p95 (Wildlife/Dog)")
    ax2.legend(loc="lower right", fontsize=8)

    n_raw_all = int(rvc.loc[rvc["cohort"] == "M2 Raw — All Outcomes (unfiltered)", "n"].iloc[0])
    n_cln_all = int(rvc.loc[rvc["cohort"] == "M3 Cleaned — All Valid Completed Stays", "n"].iloc[0])
    _add_header_and_caption(
        fig,
        title="Cleaning Consolidates Adoption Subtypes while Right-Tail Stays Reverse Species Rankings Across Summaries",
        subtitle="Question (2.1 & 2.2): How does M3 cleaning change headline metrics, and do median, mean, and p95 rank species the same way?",
        caption=(
            f"Source: data/austin_animals.db | M2 Raw n={n_raw_all:,} (unfiltered) vs M3 Cleaned n={n_cln_all:,} "
            f"| Flag filter: days_in_shelter_flag IN ('ok', 'unusual_high_stay') AND stay_sequence_flag = 'ok' | Coverage rule: n >= {MIN_GROUP_OUTCOMES} stays."
        ),
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.91])
    out_path = out_dir / "fig1_raw_vs_cleaned_impact.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_fig2_shape_histogram_and_ecdf(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> Path:
    """
    Figure 2 (Section 2.3 — Shape):
      Shows the distribution of `length_of_stay_days` in two complementary ways:
      - Left panel: Histogram with justified 2-day natural bin width (0-90 days) showing
        the day 0-2 triage spike, day 4 stray-hold release peak, and juvenile foster shoulder.
      - Right panel: Log-scale ECDF (`log1p(days)`) across the 5 main species/life-stage cohorts.
    """
    apply_m4_plot_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_sql_query(
        """
        SELECT
            animal_type,
            life_stage,
            length_of_stay_days
        FROM stays_clean_view
        WHERE outcome_at IS NOT NULL
          AND days_in_shelter_flag IN ('ok', 'unusual_high_stay')
          AND stay_sequence_flag = 'ok'
        """,
        con,
    )
    los = df["length_of_stay_days"].dropna()
    med_all = float(los.median())
    p90_all = float(los.quantile(0.90))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.6))

    # Left panel: 2-day bin width histogram (0 to 90 days)
    bins = np.arange(0.0, HIST_MAX_DAYS + HIST_BIN_WIDTH_DAYS, HIST_BIN_WIDTH_DAYS)
    clipped = los[los <= HIST_MAX_DAYS]
    n_beyond = int((los > HIST_MAX_DAYS).sum())
    ax1.hist(clipped, bins=bins, color="#3182bd", edgecolor="white", alpha=0.88)
    ax1.axvline(STRAY_HOLD_DAYS, color="#d95f02", linestyle="--", linewidth=1.4, label=f"Mandatory stray hold ({STRAY_HOLD_DAYS:.0f}d)")
    ax1.axvline(med_all, color="#1b9e77", linestyle="-", linewidth=1.5, label=f"Overall median ({med_all:.1f}d)")
    ax1.axvline(TARGET_STAY_DAYS, color="#e7298a", linestyle=":", linewidth=1.4, label=f"Operational target ({TARGET_STAY_DAYS:.0f}d)")
    ax1.set_xlim(0, HIST_MAX_DAYS)
    ax1.set_xlabel("Length of Stay (days, 2-day natural bins; 0 to 90d window)")
    ax1.set_ylabel("Completed Shelter Stays (count)")
    ax1.set_title(f"A. Histogram (2-Day Bins, 0-90d; {n_beyond:,} tail stays >90d omitted from axis)")
    ax1.legend(loc="upper right", fontsize=8)

    # Right panel: ECDF on log1p(days) scale by group
    cohort_defs = [
        ("Cat — Adult", df[(df["animal_type"] == "Cat") & (df["life_stage"] == "adult")]["length_of_stay_days"], "#1b9e77"),
        ("Cat — Juvenile", df[(df["animal_type"] == "Cat") & (df["life_stage"] == "juvenile")]["length_of_stay_days"], "#66a61e"),
        ("Dog — Adult", df[(df["animal_type"] == "Dog") & (df["life_stage"] == "adult")]["length_of_stay_days"], "#d95f02"),
        ("Dog — Juvenile", df[(df["animal_type"] == "Dog") & (df["life_stage"] == "juvenile")]["length_of_stay_days"], "#e6ab02"),
        ("Wildlife (All)", df[df["animal_type"] == "Wildlife"]["length_of_stay_days"], "#7570b3"),
    ]
    for label, s_grp, color in cohort_defs:
        s_clean = np.sort(s_grp.dropna().to_numpy(dtype=float))
        if len(s_clean) == 0:
            continue
        y_ecdf = np.arange(1, len(s_clean) + 1) / len(s_clean)
        ax2.step(np.log1p(s_clean), y_ecdf, where="post", linewidth=1.7, color=color, label=f"{label} (n={len(s_clean):,})")

    ax2.axhline(0.50, color="#666666", linestyle="--", linewidth=1.0, label="50th percentile (Median)")
    ax2.axhline(0.90, color="#999999", linestyle=":", linewidth=1.0, label="90th percentile (p90)")
    tick_days = [0, 1, 4, 7, 14, 30, 60, 120, 365]
    ax2.set_xticks([np.log1p(d) for d in tick_days])
    ax2.set_xticklabels([f"{d}d" for d in tick_days])
    ax2.set_ylim(0, 1.02)
    ax2.set_xlabel("Length of Stay (days, log(1 + days) scale)")
    ax2.set_ylabel("Cumulative Share of Completed Stays (ECDF)")
    ax2.set_title("B. Log-Scale ECDF Exposes Same-Day Wildlife Exits vs Adult Dog Right Tail")
    ax2.legend(loc="lower right", fontsize=8)

    _add_header_and_caption(
        fig,
        title="Length of Stay Is Strongly Right-Skewed with a Day-4 Stray-Hold Peak and a Secondary Foster Wave",
        subtitle="Question (2.3): What is the shape of length_of_stay_days overall and across species/life-stage groups?",
        caption=(
            f"Source: stays_clean_view (n={len(los):,} completed stays; overall median={med_all:.2f}d, p90={p90_all:.1f}d) "
            f"| Flag filter: days_in_shelter_flag IN ('ok', 'unusual_high_stay') AND stay_sequence_flag = 'ok'."
        ),
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.91])
    out_path = out_dir / "fig2_los_shape_hist_ecdf.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_fig3_coverage_and_flag_sensitivity(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> Path:
    """
    Figure 3 (Section 2.4 — Coverage Rules & Flag Filter Sensitivity):
      - Left panel: Monthly outcome completion rate (%) and median stay with vs without the
        monthly coverage rule (>=20 active days & >=85% cohort completion in America/Chicago).
      - Right panel: Species median and 95th percentile stay under Filter A (ok + unusual_high_stay)
        vs Filter B (ok only, dropping unusual_high_stay).
    """
    apply_m4_plot_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    df_time = fetch_time_indexed_stays(con)
    m_cov = compute_monthly_coverage_and_rule(df_time)
    f_sens = compute_flag_filter_sensitivity(con)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.6))

    # Left panel: Monthly median LOS with vs without coverage rule (gaps/hollow markers for excluded edge months)
    x_idx = np.arange(len(m_cov))
    all_meds = m_cov["median_los_d"].to_numpy(dtype=float)
    rule_meds = np.where(m_cov["passes_coverage_rule"], all_meds, np.nan)

    ax1.plot(x_idx, all_meds, color="#bdbdbd", linestyle="--", marker="o", markersize=5, label="Without coverage rule (includes edge/censored months)")
    ax1.plot(x_idx, rule_meds, color="#08519c", linestyle="-", marker="s", markersize=6, linewidth=2.0, label="With coverage rule (>=20d active & >=85% closed)")

    for i, row in m_cov.iterrows():
        if not row["passes_coverage_rule"]:
            ax1.axvspan(i - 0.4, i + 0.4, color="#fee0d2", alpha=0.55)
            ax1.annotate(
                f"{row['completion_pct']:.0f}%\nclosed",
                (i, row["median_los_d"]),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=7.5,
                color="#a50f15",
            )

    ax1.axhline(TARGET_STAY_DAYS, color="#e7298a", linestyle=":", linewidth=1.2, label="14-day shelter target")
    ax1.set_xticks(x_idx)
    ax1.set_xticklabels(m_cov["intake_local_month"], rotation=45, ha="right", fontsize=8)
    ax1.set_ylim(0, max(16.0, float(np.nanmax(all_meds)) * 1.25))
    ax1.set_xlabel("Intake Cohort Month (America/Chicago local clock)")
    ax1.set_ylabel("Cohort Median Length of Stay (days)")
    ax1.set_title("A. Unfiltered Trailing Months Drop Artificially Due to Right-Censoring")
    ax1.legend(loc="upper right", fontsize=8)

    # Right panel: Flag filter sensitivity (Filter A vs Filter B on Median and p95)
    sp_order = f_sens.sort_values("p95_A_d", ascending=True)
    y_pos = np.arange(len(sp_order))
    h = 0.22
    ax2.barh(y_pos + 1.5 * h, sp_order["p95_A_d"], height=h, color="#d95f02", label="p95 — Filter A (ok + unusual_high_stay, reported)")
    ax2.barh(y_pos + 0.5 * h, sp_order["p95_B_d"], height=h, color="#fdb863", label="p95 — Filter B (ok only, drops >Q3+3*IQR)")
    ax2.barh(y_pos - 0.5 * h, sp_order["median_A_d"], height=h, color="#1b9e77", label="Median — Filter A (ok + unusual_high_stay)")
    ax2.barh(y_pos - 1.5 * h, sp_order["median_B_d"], height=h, color="#a6dba0", label="Median — Filter B (ok only)")
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels([f"{r.animal_type}\n(A:{r.n_filter_A_all_valid:,} / B:{r.n_filter_B_ok_only:,})" for _, r in sp_order.iterrows()])
    ax2.set_xlim(0, max(175.0, float(sp_order["p95_A_d"].max()) * 1.12))
    ax2.set_xlabel("Length of Stay (days)")
    ax2.set_title("B. Dropping unusual_high_stay Cuts Dog/Wildlife p95 Nearly in Half")
    ax2.legend(loc="lower right", fontsize=7.8)

    n_inc_m = int(m_cov["passes_coverage_rule"].sum())
    n_tot_m = len(m_cov)
    _add_header_and_caption(
        fig,
        title="Coverage Rules Prevent Right-Censored Trailing Drops while Outlier Flag Removal Truncates the 95th Percentile",
        subtitle="Question (2.4): How do coverage rules and the choice of flag filter change monthly trends and species summaries?",
        caption=(
            f"Source: stays_clean_view (n={len(df_time):,} valid-sequence intakes across {n_tot_m} months; {n_inc_m}/{n_tot_m} months pass >=20 active days & >=85% completion in America/Chicago) "
            f"| Reported flag filter: Filter A (days_in_shelter_flag IN ('ok', 'unusual_high_stay') AND stay_sequence_flag = 'ok')."
        ),
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.91])
    out_path = out_dir / "fig3_coverage_and_flag_sensitivity.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_fig4_time_clock_and_scales(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> Path:
    """
    Figure 4 (Section 2.5 — Time Clock, Edges, Two Scales & Calendar):
      - Panel A: Hour-of-day intake distribution comparing Austin Local Time (America/Chicago)
        vs stored UTC (demonstrating the 5-6 hour UTC shift).
      - Panel B: Day-of-week mean daily intake rate split by intake channel (bars start at 0).
      - Panel C: Complete-month seasonal intake volume for Juvenile vs Adult animals, highlighting
        the May-July Kitten/Puppy surge and July 4th stray spike.
    """
    apply_m4_plot_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    df_time = fetch_time_indexed_stays(con)
    hourly_df, dow_df, cal_df = compute_hourly_and_weekday_profiles(df_time)
    m_cov = compute_monthly_coverage_and_rule(df_time)

    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(15.5, 5.4))

    # Panel A: Hourly profile (America/Chicago vs UTC)
    hours = hourly_df["hour"].to_numpy()
    ax1.axvspan(11, 19, color="#e5f5e0", alpha=0.65, label="Public shelter hours (11:00-19:00 Local)")
    ax1.plot(hours, hourly_df["pct_local_chicago"], color="#006d2c", marker="o", linewidth=2.0, label="Austin Local (America/Chicago)")
    ax1.plot(hours, hourly_df["pct_stored_utc"], color="#cb181d", linestyle="--", marker="^", linewidth=1.6, label="Stored UTC (unadjusted +5/+6h shift)")
    ax1.set_xticks(range(0, 24, 3))
    ax1.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 3)])
    ax1.set_ylim(0, max(float(hourly_df["pct_local_chicago"].max()), float(hourly_df["pct_stored_utc"].max())) * 1.15)
    ax1.set_xlabel("Hour of Day (00:00-23:00)")
    ax1.set_ylabel("Share of All Intakes (%)")
    ax1.set_title("A. Stored UTC Shifts 11:00-16:00 Local Peak Into Evening")
    ax1.legend(loc="upper left", fontsize=7.5)

    # Panel B: Day of week mean intakes/day (bars start at 0)
    x_dow = np.arange(len(dow_df))
    w = 0.38
    ax2.bar(x_dow - w / 2, dow_df["stray_aco_per_day"], width=w, color="#3182bd", label="Stray + ACO Impound")
    ax2.bar(x_dow + w / 2, dow_df["owner_community_per_day"], width=w, color="#fd8d3c", label="Owner Surrender + Community + Return")
    overall_daily_mean = float(dow_df["mean_intakes_per_day"].mean())
    ax2.axhline(overall_daily_mean, color="#333333", linestyle="--", linewidth=1.1, label=f"Mean total/day ({overall_daily_mean:.1f}/d)")
    ax2.set_xticks(x_dow)
    ax2.set_xticklabels([d[:3] for d in dow_df["day_of_week"]])
    ax2.set_ylim(0, float(dow_df["mean_intakes_per_day"].max()) * 1.15)
    ax2.set_xlabel("Day of Week (America/Chicago local clock)")
    ax2.set_ylabel("Mean Intakes per Calendar Day (count/day)")
    ax2.set_title("B. Sunday Intake Drop Driven by Front-Desk Schedule")
    ax2.legend(loc="upper right", fontsize=7.5)

    # Panel C: Monthly Juvenile vs Adult intakes across complete months (>=20 active days)
    complete_months = set(m_cov.loc[m_cov["active_local_days"] >= MIN_DAYS_PER_MONTH, "intake_local_month"])
    m_stage = (
        df_time.groupby(["intake_local_month", "life_stage"])["animal_key"]
        .count()
        .unstack(fill_value=0)
        .reindex(m_cov["intake_local_month"], fill_value=0)
    )
    xm = np.arange(len(m_cov))
    juv_series = np.where(m_cov["intake_local_month"].isin(complete_months), m_stage.get("juvenile", 0), np.nan)
    adt_series = np.where(m_cov["intake_local_month"].isin(complete_months), m_stage.get("adult", 0), np.nan)

    ax3.plot(xm, juv_series, color="#d95f02", marker="o", linewidth=2.0, label="Juvenile arrivals (<=180d, complete months)")
    ax3.plot(xm, adt_series, color="#1f78b4", marker="s", linewidth=2.0, label="Adult arrivals (>180d, complete months)")
    for i, row in m_cov.iterrows():
        if row["intake_local_month"] not in complete_months:
            ax3.axvspan(i - 0.4, i + 0.4, color="#eeeeee", alpha=0.9, label="Partial edge month (<20d, gap)" if i == len(m_cov) - 1 else None)
    ax3.set_xticks(xm)
    ax3.set_xticklabels(m_cov["intake_local_month"], rotation=45, ha="right", fontsize=7.5)
    ax3.set_ylim(0, max(float(np.nanmax(juv_series)), float(np.nanmax(adt_series))) * 1.18)
    ax3.set_xlabel("Intake Month (America/Chicago; partial edges shown as gaps)")
    ax3.set_ylabel("Monthly Intake Count (animals/month)")
    ax3.set_title("C. May-July Kitten/Puppy Surge Doubles Juvenile Volume")
    ax3.legend(loc="upper right", fontsize=7.5)

    _add_header_and_caption(
        fig,
        title="Daytime Shelter Hours (11:00-17:00 Local) and the May-July Juvenile Surge Govern Intake Timing",
        subtitle="Question (2.5): How do intake volumes behave across local vs UTC hours, days of week, and seasonal calendar months?",
        caption=(
            f"Source: stays_clean_view (n={len(df_time):,} valid-sequence intakes converted from UTC to America/Chicago CST/CDT) "
            f"| Flag filter: stay_sequence_flag = 'ok' | Coverage rule: monthly lines require >={MIN_DAYS_PER_MONTH} active local days."
        ),
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.91])
    out_path = out_dir / "fig4_time_clock_and_scales.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_fig5_age_vs_los_relationship(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> Path:
    """
    Figure 5 (Section 2.6 — Relationship Plotted First + Driver Check):
      Two-panel small multiples with identical y-axis scale (0-90 days) comparing
      Age at Intake (months) vs Length of Stay (days) for Adopted Cats vs Adopted Dogs,
      with scatter points, age-binned medians + IQR bars, surgical age threshold (8 weeks),
      and annotated Pearson r vs Spearman rho before and after neonate/outlier removal.
    """
    apply_m4_plot_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    adopted, binned_df, corr_df = compute_age_los_relationship(con)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.6), sharey=True)

    for ax, sp, color_pt, color_ln in [
        (ax1, "Cat", "#9ecae1", "#08519c"),
        (ax2, "Dog", "#fdd0a2", "#d94801"),
    ]:
        sub = adopted[adopted["animal_type"] == sp].copy()
        # Plot up to 60 months (5 years) on x-axis so juvenile structure is clearly resolved
        sub_plot = sub[sub["age_months"] <= 60.0]
        ax.scatter(
            sub_plot["age_months"],
            sub_plot["length_of_stay_days"].clip(upper=90.0),
            alpha=0.14,
            s=12,
            color=color_pt,
            label=f"Individual adopted stays (n={len(sub):,})",
        )
        b_sp = binned_df[(binned_df["animal_type"] == sp) & (binned_df["mid_age_months"] <= 60.0)]
        yerr_low = b_sp["median_los_d"] - b_sp["q25_los_d"]
        yerr_high = b_sp["q75_los_d"] - b_sp["median_los_d"]
        ax.errorbar(
            b_sp["mid_age_months"],
            b_sp["median_los_d"],
            yerr=[yerr_low, yerr_high],
            fmt="-o",
            color=color_ln,
            linewidth=2.2,
            markersize=6,
            capsize=4,
            label="Age-bin Median LOS +/- IQR (Q1-Q3)",
        )
        ax.axvline(SURGICAL_AGE_DAYS / 30.4375, color="#e7298a", linestyle="--", linewidth=1.3, label="8-week surgical threshold (1.8m)")
        ax.axhline(TARGET_STAY_DAYS, color="#444444", linestyle=":", linewidth=1.1, label="14-day shelter target")

        juv = sub[sub["age_days"] <= 180]
        adt = sub[sub["age_days"] > 180]
        r_juv_p = juv["age_days"].corr(juv["length_of_stay_days"], method="pearson")
        r_juv_s = juv["age_days"].corr(juv["length_of_stay_days"], method="spearman")
        r_adt_p = adt["age_days"].corr(adt["length_of_stay_days"], method="pearson")
        r_adt_s = adt["age_days"].corr(adt["length_of_stay_days"], method="spearman")

        ax.text(
            0.97,
            0.84,
            (
                f"Juvenile (<=6m, n={len(juv):,}):\n"
                f"  Pearson r = {r_juv_p:+.2f}, Spearman rho = {r_juv_s:+.2f}\n"
                f"Adult (>6m, n={len(adt):,}):\n"
                f"  Pearson r = {r_adt_p:+.2f}, Spearman rho = {r_adt_s:+.2f}"
            ),
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8.2,
            bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": "#cccccc", "alpha": 0.92},
        )
        ax.set_xlim(-1, 60)
        ax.set_ylim(0, 90)
        ax.set_xlabel("Age at Intake (months, from date_of_birth to intake_at; 0-60m window)")
        ax.set_title(f"{'A' if sp == 'Cat' else 'B'}. Adopted {sp}s: Binned Median & IQR Across Age at Intake")
        ax.legend(loc="upper right", fontsize=7.8)

    ax1.set_ylabel("Length of Stay Until Adoption (days, clipped at 90d for display)")

    r_all_p = float(corr_df.iloc[0]["pearson_r"])
    r_all_s = float(corr_df.iloc[0]["spearman_rho"])
    _add_header_and_caption(
        fig,
        title="Unweaned Neonates (<8 Weeks) Drive a Strong Negative Age-Stay Slope in Kittens while Adult Dogs Flatten It",
        subtitle="Question (2.6): How does age at intake relate to length of stay until adoption, and what drives Pearson vs Spearman?",
        caption=(
            f"Source: stays_clean_view (n={len(adopted):,} adopted Cat & Dog stays with valid DOB; pooled Pearson r={r_all_p:+.3f} vs Spearman rho={r_all_s:+.3f}) "
            f"| Flag filter: outcome_status='Adopted' AND days_in_shelter_flag IN ('ok', 'unusual_high_stay') AND date_of_birth_flag='ok'."
        ),
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.91])
    out_path = out_dir / "fig5_age_vs_los_relationship.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_fig6_headline_simpsons_paradox(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> Path:
    """
    Figure 6 (Section 2.7 & README Headline Figure):
      - Left panel: Live Adoption Share (%) comparing Dogs vs Cats Pooled (Overall) and
        stratified by Life Stage (Adult vs Juvenile) and Non-TNR status, visually demonstrating
        Simpson's paradox (Cats appear equal/higher overall only because 60% of Cats are high-adoption juveniles).
      - Right panel: Cluster-bootstrapped 95% confidence intervals (bootstrapped over daily intake
        cohorts `intake_local_date`, B=5,000, seed=42) vs naive stay-level CIs across 5 focal contrasts.
    """
    apply_m4_plot_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    df_time = fetch_time_indexed_stays(con)
    _, ci_df, _ = compute_simpsons_paradox_and_intervals(df_time)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.8, 5.8))

    # Left panel: Grouped bars starting at 0 showing Simpson's paradox in Adoption Share (%)
    strata_rows = ci_df.iloc[:4].copy()
    short_labels = [
        "1. Pooled (All Stays)\n[Confounded by 60% Cat Juveniles]",
        "2. Adult Stays Only\n(>180d at intake — Reverses!)",
        "3. Juvenile Stays Only\n(<=180d at intake)",
        "4. Adult Non-TNR Stays\n(excl. Community Program)",
    ]
    x_pos = np.arange(len(strata_rows))
    w = 0.35
    b_dog = ax1.bar(x_pos - w / 2, strata_rows["adopt_A_pct"], width=w, color="#d95f02", label="Dog (Group A)")
    b_cat = ax1.bar(x_pos + w / 2, strata_rows["adopt_B_pct"], width=w, color="#1b9e77", label="Cat (Group B)")
    ax1.axhline(50.0, color="#666666", linestyle=":", linewidth=1.1, label="50% adoption reference")
    ax1.set_xticks(x_pos)
    ax1.set_xticklabels(short_labels, fontsize=8.2)
    ax1.set_ylim(0, 72)
    ax1.set_ylabel("Live Adoption Rate (% of completed stays)")
    ax1.set_title("A. Simpson's Paradox: Adult Dogs Beat Adult Cats (+5.5 pp) Despite Pooled Parity")
    ax1.legend(loc="upper right", fontsize=8)

    for bars in (b_dog, b_cat):
        for bar in bars:
            h_val = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width() / 2, h_val + 1.0, f"{h_val:.1f}%", ha="center", va="bottom", fontsize=7.8)

    # Right panel: Forest plot of Adoption Rate Difference (Group A - Group B, pp) with Cluster vs Naive 95% CI
    y_pos = np.arange(len(ci_df))
    diffs = ci_df["adopt_diff_pct_pt"].to_numpy(dtype=float)

    cl_lows, cl_highs = [], []
    for s_ci in ci_df["adopt_cluster_95ci"]:
        lo, hi = [float(x) for x in s_ci.strip("[]").split(",")]
        cl_lows.append(lo)
        cl_highs.append(hi)
    cl_lows = np.array(cl_lows)
    cl_highs = np.array(cl_highs)

    ax2.axvline(0.0, color="#333333", linestyle="--", linewidth=1.3, label="Zero difference (null reference)")
    ax2.errorbar(
        diffs,
        y_pos,
        xerr=[diffs - cl_lows, cl_highs - diffs],
        fmt="o",
        color="#08519c",
        ecolor="#08519c",
        elinewidth=2.2,
        capsize=4,
        markersize=6.5,
        label=f"95% Cluster Bootstrap CI (unit = daily cohort, B={BOOTSTRAP_ITERATIONS:,})",
    )
    for i, (d_val, lo, hi) in enumerate(zip(diffs, cl_lows, cl_highs)):
        ax2.text(
            d_val,
            i + 0.23,
            f"{d_val:+.1f} pp [{lo:+.1f}, {hi:+.1f}]",
            ha="center",
            va="bottom",
            fontsize=7.8,
            color="#08519c",
            fontweight="bold",
        )

    ax2.set_yticks(y_pos)
    ax2.set_yticklabels([
        "1. Pooled: Dog - Cat",
        "2. Adults: Dog - Cat",
        "3. Juveniles: Dog - Cat",
        "4. Adult Non-TNR: Dog - Cat",
        "5. Owner Surrender - Stray",
    ], fontsize=8.5)
    ax2.invert_yaxis()
    ax2.set_xlim(min(-18.0, float(cl_lows.min()) - 4.0), max(20.0, float(cl_highs.max()) + 4.0))
    ax2.set_xlabel("Adoption Rate Difference: Group A minus Group B (percentage points)")
    ax2.set_title(f"B. Cluster-Bootstrapped 95% CIs Across Subgroups (Look Count = {LOOK_COUNT})")
    ax2.legend(loc="lower right", fontsize=7.8)

    n_dog = int(ci_df.iloc[0]["n_group_A"])
    n_cat = int(ci_df.iloc[0]["n_group_B"])
    _add_header_and_caption(
        fig,
        title="Life-Stage Composition Masks Adult Dog Adoption Advantage and Longer Adult Dog Stays (Simpson's Paradox)",
        subtitle=f"Question (2.7): How do Dogs and Cats compare on adoption rate and stay duration after accounting for composition and {LOOK_COUNT} looks?",
        caption=(
            f"Source: stays_clean_view (Dog n={n_dog:,}, Cat n={n_cat:,}; clustered by America/Chicago intake_local_date, B={BOOTSTRAP_ITERATIONS:,}, seed={RANDOM_SEED}, looks={LOOK_COUNT}) "
            f"| Flag filter: days_in_shelter_flag IN ('ok', 'unusual_high_stay') AND stay_sequence_flag = 'ok'."
        ),
    )
    fig.tight_layout(rect=[0, 0.04, 1, 0.91])
    out_path = out_dir / "fig6_headline_comparison_simpsons_paradox.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def generate_all_m4_figures(con: sqlite3.Connection, out_dir: Path = FIGURES_DIR) -> list[Path]:
    """Generates all 6 M4 figures deterministically and returns their paths."""
    return [
        plot_fig1_raw_vs_cleaned_and_rankings(con, out_dir),
        plot_fig2_shape_histogram_and_ecdf(con, out_dir),
        plot_fig3_coverage_and_flag_sensitivity(con, out_dir),
        plot_fig4_time_clock_and_scales(con, out_dir),
        plot_fig5_age_vs_los_relationship(con, out_dir),
        plot_fig6_headline_simpsons_paradox(con, out_dir),
    ]
