-- =====================================================================
-- Austin Animal Center — M3 Cleaned Layer & Mapping Tables (SQLite)
-- Rules enforced:
--   1. M2 tables (animals, intakes, outcomes, quarantine) are NEVER modified.
--   2. Every table is STRICT and safe to run more than once (IF NOT EXISTS).
--   3. Each cleaned table has the exact same grain, primary key, and row count
--      as its M2 source table.
--   4. Every value cleaning can change is stored as (<col>_raw, <col>, <col>_flag)
--      with a CHECK constraint on every flag column.
--   5. Cleaned values are NEVER imputed (untrusted/missing -> NULL).
-- =====================================================================

-- ---------------------------------------------------------------------
-- MAPPING TABLES (loaded from data/mappings/*.csv)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS map_animal_type (
    raw_value           TEXT PRIMARY KEY,
    canonical_value     TEXT NOT NULL,
    life_stage_default  TEXT NOT NULL CHECK (life_stage_default IN ('juvenile', 'adult', 'unknown')),
    kind                TEXT NOT NULL,
    decided_by          TEXT NOT NULL,
    decided_at          TEXT NOT NULL,
    note                TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS map_intake_type (
    raw_value           TEXT PRIMARY KEY,
    canonical_value     TEXT NOT NULL,
    kind                TEXT NOT NULL,
    decided_by          TEXT NOT NULL,
    decided_at          TEXT NOT NULL,
    note                TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS map_outcome_status (
    raw_value           TEXT PRIMARY KEY,
    canonical_value     TEXT NOT NULL,
    kind                TEXT NOT NULL,
    decided_by          TEXT NOT NULL,
    decided_at          TEXT NOT NULL,
    note                TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------------
-- CLEANED ENTITY TABLE: animals_clean (same grain & PK as animals)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS animals_clean (
    animal_key           INTEGER PRIMARY KEY REFERENCES animals (animal_key),
    animal_id            TEXT    NOT NULL UNIQUE,
    animal_type_raw      TEXT,
    animal_type          TEXT    NOT NULL,
    animal_type_flag     TEXT    NOT NULL CHECK (animal_type_flag IN (
                             'ok', 'mapped_life_stage', 'mapped_category')),
    life_stage           TEXT    NOT NULL CHECK (life_stage IN (
                             'juvenile', 'adult', 'unknown')),
    life_stage_flag      TEXT    NOT NULL CHECK (life_stage_flag IN (
                             'from_age_at_intake', 'from_raw_type', 'conflict_type_vs_age', 'unknown_no_dob')),
    sex_raw              TEXT,
    sex                  TEXT    CHECK (sex IS NULL OR sex IN ('Male', 'Female')),
    sex_flag             TEXT    NOT NULL CHECK (sex_flag IN (
                             'ok', 'missing_unrecorded', 'missing_coded_unknown')),
    primary_breed        TEXT,
    primary_color_raw    TEXT,
    primary_color        TEXT,
    primary_color_flag   TEXT    NOT NULL CHECK (primary_color_flag IN (
                             'ok', 'missing_coded_unknown')),
    secondary_color      TEXT,
    date_of_birth_raw    TEXT,
    date_of_birth        TEXT    CHECK (
                             date_of_birth IS NULL
                             OR date_of_birth GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                         ),
    date_of_birth_flag   TEXT    NOT NULL CHECK (date_of_birth_flag IN (
                             'ok', 'missing_not_recorded', 'invalid_after_intake', 'invalid_future_date')),
    retrieved_at         TEXT    NOT NULL
) STRICT;

-- ---------------------------------------------------------------------
-- CLEANED EVENT TABLE: intakes_clean (same grain & PK as intakes)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intakes_clean (
    animal_key                  INTEGER NOT NULL REFERENCES animals (animal_key),
    intake_at                   TEXT    NOT NULL,
    source_record_id            TEXT    NOT NULL,
    intake_type_raw             TEXT,
    intake_type                 TEXT    NOT NULL,
    intake_type_flag            TEXT    NOT NULL CHECK (intake_type_flag IN (
                                    'ok', 'mapped_category')),
    name_at_intake_raw          TEXT,
    name_at_intake              TEXT,
    name_at_intake_flag         TEXT    NOT NULL CHECK (name_at_intake_flag IN (
                                    'ok', 'missing_not_recorded', 'missing_placeholder',
                                    'stripped_shelter_prefix', 'normalised_case')),
    previously_spayed_neutered  INTEGER CHECK (previously_spayed_neutered IN (0, 1)),
    intake_health_raw           TEXT,
    intake_health               TEXT,
    intake_health_flag          TEXT    NOT NULL CHECK (intake_health_flag IN (
                                    'ok', 'missing_not_logged', 'standardised_label')),
    found_zip_raw               TEXT,
    found_zip                   TEXT,
    found_zip_flag              TEXT    NOT NULL CHECK (found_zip_flag IN (
                                    'ok', 'missing_no_zip_in_address', 'invalid_house_number_as_zip',
                                    'unusual_outside_service_area')),
    stay_sequence_flag          TEXT    NOT NULL CHECK (stay_sequence_flag IN (
                                    'ok', 'overlap_with_prior_stay', 'reintake_after_open_stay')),
    retrieved_at                TEXT    NOT NULL,
    PRIMARY KEY (animal_key, intake_at),
    FOREIGN KEY (animal_key, intake_at) REFERENCES intakes (animal_key, intake_at)
) STRICT;

-- ---------------------------------------------------------------------
-- CLEANED EVENT TABLE: outcomes_clean (same grain & PK as outcomes)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes_clean (
    animal_key               INTEGER NOT NULL REFERENCES animals (animal_key),
    outcome_at               TEXT    NOT NULL,
    intake_at                TEXT    NOT NULL,
    source_record_id         TEXT    NOT NULL,
    outcome_status_raw       TEXT,
    outcome_status           TEXT    NOT NULL,
    outcome_status_flag      TEXT    NOT NULL CHECK (outcome_status_flag IN (
                                 'ok', 'mapped_subtype')),
    euthanasia_reason_raw    TEXT,
    euthanasia_reason        TEXT,
    euthanasia_reason_flag   TEXT    NOT NULL CHECK (euthanasia_reason_flag IN (
                                 'ok', 'not_applicable', 'missing_euthanized_no_reason',
                                 'conflict_non_euthanasia_status')),
    name_raw                 TEXT,
    name                     TEXT,
    name_flag                TEXT    NOT NULL CHECK (name_flag IN (
                                 'ok', 'missing_not_recorded', 'missing_placeholder',
                                 'stripped_shelter_prefix', 'normalised_case')),
    spayed_neutered_raw      TEXT,
    spayed_neutered          TEXT    CHECK (spayed_neutered IS NULL OR spayed_neutered IN ('Yes', 'No')),
    spayed_neutered_flag     TEXT    NOT NULL CHECK (spayed_neutered_flag IN (
                                 'ok', 'normalised_case', 'missing_unrecorded',
                                 'missing_coded_unknown', 'conflict_previously_spayed')),
    days_in_shelter_raw      INTEGER,
    days_in_shelter          INTEGER CHECK (days_in_shelter IS NULL OR days_in_shelter >= 0),
    length_of_stay_days      REAL    CHECK (length_of_stay_days IS NULL OR length_of_stay_days >= 0),
    days_in_shelter_flag     TEXT    NOT NULL CHECK (days_in_shelter_flag IN (
                                 'ok', 'invalid_outcome_before_intake',
                                 'duplicate_exit_for_stay', 'unusual_high_stay')),
    retrieved_at             TEXT    NOT NULL,
    PRIMARY KEY (animal_key, outcome_at),
    FOREIGN KEY (animal_key, intake_at) REFERENCES intakes (animal_key, intake_at),
    FOREIGN KEY (animal_key, outcome_at) REFERENCES outcomes (animal_key, outcome_at)
) STRICT;

-- ---------------------------------------------------------------------
-- CONVENIENCE VIEW FOR M4 ANALYSES (joins resolved entity + stay + outcome)
-- ---------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS stays_clean_view AS
SELECT
    i.animal_key,
    a.animal_id,
    a.animal_type_raw,
    a.animal_type,
    a.life_stage,
    a.sex,
    a.primary_breed,
    a.primary_color,
    a.date_of_birth,
    a.date_of_birth_flag,
    i.intake_at,
    i.intake_type_raw,
    i.intake_type,
    i.intake_health,
    i.found_zip,
    i.found_zip_flag,
    i.stay_sequence_flag,
    o.outcome_at,
    o.outcome_status_raw,
    o.outcome_status,
    o.spayed_neutered,
    o.days_in_shelter,
    o.length_of_stay_days,
    o.days_in_shelter_flag
FROM intakes_clean AS i
JOIN animals_clean AS a ON a.animal_key = i.animal_key
LEFT JOIN outcomes_clean AS o
    ON o.animal_key = i.animal_key
   AND o.intake_at  = i.intake_at
   AND o.days_in_shelter_flag <> 'duplicate_exit_for_stay';
