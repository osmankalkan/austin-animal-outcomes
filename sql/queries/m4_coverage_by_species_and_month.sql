-- =====================================================================
-- M4 Section 2.4: Coverage Table by Canonical Species x Life Stage
-- Question: How many total intakes, completed stays, open stays, and
--           non-null covariates exist in each species and life-stage group?
-- Result grain: One row per (animal_type, life_stage).
-- Flag filter: stay_sequence_flag = 'ok' for valid intakes;
--              days_in_shelter_flag IN ('ok', 'unusual_high_stay') for valid completed stays.
-- =====================================================================

SELECT
    s.animal_type,
    s.life_stage,
    COUNT(*) AS n_intakes,
    SUM(CASE WHEN s.outcome_at IS NOT NULL
              AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
             THEN 1 ELSE 0 END) AS n_completed_stays,
    SUM(CASE WHEN s.outcome_at IS NULL THEN 1 ELSE 0 END) AS n_open_stays,
    ROUND(100.0 * SUM(CASE WHEN s.outcome_at IS NOT NULL
                            AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
                           THEN 1.0 ELSE 0.0 END) / COUNT(*), 1) AS completion_rate_pct,
    ROUND(100.0 * SUM(CASE WHEN s.date_of_birth IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 1) AS dob_coverage_pct,
    ROUND(100.0 * SUM(CASE WHEN s.sex IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 1) AS sex_coverage_pct,
    ROUND(100.0 * SUM(CASE WHEN s.found_zip IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 1) AS zip_coverage_pct,
    ROUND(100.0 * SUM(CASE WHEN s.intake_health IS NOT NULL THEN 1.0 ELSE 0.0 END) / COUNT(*), 1) AS health_logged_pct,
    SUM(CASE WHEN s.days_in_shelter_flag = 'unusual_high_stay' THEN 1 ELSE 0 END) AS n_unusual_high_stay
FROM stays_clean_view AS s
WHERE s.stay_sequence_flag = 'ok'
GROUP BY s.animal_type, s.life_stage
ORDER BY n_intakes DESC;
