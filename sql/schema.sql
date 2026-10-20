-- =====================================================================
-- Austin Animal Center — M2 schema (SQLite)
-- Safe to run more than once: every statement is IF NOT EXISTS.
-- Every table is STRICT. Timestamps are ISO 8601 UTC TEXT
-- (YYYY-MM-DDTHH:MM:SSZ); calendar dates are YYYY-MM-DD TEXT.
-- Every table keeps retrieved_at = UTC time of the landed raw file
-- the row came from (taken from the raw file name).
-- =====================================================================

-- ---------------------------------------------------------------------
-- ENTITY: one row = one animal known to the shelter (type 1: latest
-- retrieved version of its attributes wins).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS animals (
    animal_key       INTEGER PRIMARY KEY,                 -- surrogate key
    animal_id        TEXT    NOT NULL UNIQUE,             -- source identifier
    animal_type      TEXT,                                -- Cat, Dog, Kitten, ...
    sex              TEXT,
    primary_breed    TEXT,
    primary_color    TEXT,
    secondary_color  TEXT,
    date_of_birth    TEXT CHECK (
        date_of_birth IS NULL
        OR date_of_birth GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    retrieved_at     TEXT    NOT NULL
) STRICT;

-- ---------------------------------------------------------------------
-- EVENT: one row = one intake (arrival) of one animal at one moment.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intakes (
    animal_key                  INTEGER NOT NULL REFERENCES animals (animal_key),
    intake_at                   TEXT    NOT NULL,         -- UTC
    source_record_id            TEXT    NOT NULL,         -- provider row id
    intake_type                 TEXT,                     -- source_name: Stray, Owner Surrender, ...
    name_at_intake              TEXT,
    previously_spayed_neutered  INTEGER CHECK (previously_spayed_neutered IN (0, 1)),
    intake_health_condition     TEXT,
    found_zip                   TEXT,                     -- ZIP only; street address is not stored
    retrieved_at                TEXT    NOT NULL,
    PRIMARY KEY (animal_key, intake_at)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_intakes_source_record_id
    ON intakes (source_record_id);

-- ---------------------------------------------------------------------
-- EVENT: one row = one outcome (exit) of one animal at one moment,
-- tied to the intake (stay) it closes.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    animal_key         INTEGER NOT NULL,
    outcome_at         TEXT    NOT NULL,                  -- UTC
    intake_at          TEXT    NOT NULL,                  -- UTC, the stay this outcome closes
    source_record_id   TEXT    NOT NULL,
    outcome_status     TEXT,                              -- Adopted, Transfer Out, ...
    euthanasia_reason  TEXT,
    name               TEXT,
    spayed_neutered    TEXT,
    days_in_shelter    INTEGER CHECK (days_in_shelter IS NULL OR days_in_shelter >= 0),
    retrieved_at       TEXT    NOT NULL,
    PRIMARY KEY (animal_key, outcome_at),
    FOREIGN KEY (animal_key) REFERENCES animals (animal_key),
    FOREIGN KEY (animal_key, intake_at) REFERENCES intakes (animal_key, intake_at)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_outcomes_intake
    ON outcomes (animal_key, intake_at);

CREATE INDEX IF NOT EXISTS idx_outcomes_source_record_id
    ON outcomes (source_record_id);

-- ---------------------------------------------------------------------
-- QUARANTINE: one row = one raw record we could not load yet
-- (unknown animal, intake not found, missing/invalid key), kept
-- verbatim with the reason. Resolved records are removed on later runs.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_key     INTEGER PRIMARY KEY,
    source             TEXT NOT NULL CHECK (source IN ('intakes', 'outcomes')),
    source_record_id   TEXT NOT NULL,
    reason             TEXT NOT NULL,
    raw_record         TEXT NOT NULL,                     -- original JSON object
    retrieved_at       TEXT NOT NULL,
    UNIQUE (source, source_record_id)
) STRICT;
