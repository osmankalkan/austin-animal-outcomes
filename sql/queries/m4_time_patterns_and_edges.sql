-- =====================================================================
-- M4 Section 2.5: Monthly Cohort Volume, Active Days, and Edge Coverage
-- Question: Which calendar months have complete daily intake coverage and
--           sufficient outcome completion to avoid right-censoring bias?
-- Result grain: One row per UTC intake month (YYYY-MM); local-clock
--               America/Chicago equivalents are verified in src/explore.py.
-- Flag filter: stay_sequence_flag = 'ok'; completed stays require
--              days_in_shelter_flag IN ('ok', 'unusual_high_stay').
-- =====================================================================

SELECT
    substr(s.intake_at, 1, 7) AS intake_month_utc,
    COUNT(DISTINCT substr(s.intake_at, 1, 10)) AS active_intake_days,
    MIN(substr(s.intake_at, 1, 10)) AS first_date_in_month,
    MAX(substr(s.intake_at, 1, 10)) AS last_date_in_month,
    COUNT(*) AS n_intakes,
    SUM(CASE WHEN s.outcome_at IS NOT NULL
              AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
             THEN 1 ELSE 0 END) AS n_completed_stays,
    SUM(CASE WHEN s.outcome_at IS NULL THEN 1 ELSE 0 END) AS n_open_censored,
    ROUND(100.0 * SUM(CASE WHEN s.outcome_at IS NOT NULL
                            AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
                           THEN 1.0 ELSE 0.0 END) / COUNT(*), 1) AS cohort_completion_pct,
    ROUND(AVG(CASE WHEN s.outcome_at IS NOT NULL
                    AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
                   THEN s.length_of_stay_days END), 2) AS mean_los_days
FROM stays_clean_view AS s
WHERE s.stay_sequence_flag = 'ok'
GROUP BY substr(s.intake_at, 1, 7)
ORDER BY intake_month_utc;
