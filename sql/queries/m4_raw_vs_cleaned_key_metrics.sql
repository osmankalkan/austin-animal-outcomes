-- =====================================================================
-- M4 Section 2.1 & 2.2: Raw (M2) vs Cleaned (M3) Key Project Numbers
-- Question: How do length of stay (days) and live adoption rate (%)
--           compare between the raw M2 tables and the M3 cleaned layer?
-- Result grain: One row per (layer, animal_group).
-- Flag filter on M3 layer:
--   days_in_shelter_flag IN ('ok', 'unusual_high_stay')
--   AND stay_sequence_flag = 'ok'
-- =====================================================================

WITH raw_stays AS (
    SELECT
        '1_M2_Raw' AS layer,
        CASE
            WHEN a.animal_type IN ('Cat', 'Dog', 'Kitten', 'Puppy', 'Mammal') THEN a.animal_type
            ELSE 'Other_Raw'
        END AS group_label,
        CAST(o.days_in_shelter AS REAL) AS stay_days_int,
        (julianday(o.outcome_at) - julianday(o.intake_at)) AS stay_days_exact,
        CASE WHEN o.outcome_status = 'Adopted' THEN 1.0 ELSE 0.0 END AS is_adopted_exact,
        CASE WHEN o.outcome_status LIKE 'Adopted%' THEN 1.0 ELSE 0.0 END AS is_adopted_any
    FROM outcomes AS o
    JOIN animals  AS a ON a.animal_key = o.animal_key
),
clean_stays AS (
    SELECT
        '2_M3_Cleaned' AS layer,
        s.animal_type AS group_label,
        CAST(s.days_in_shelter AS REAL) AS stay_days_int,
        s.length_of_stay_days AS stay_days_exact,
        CASE WHEN s.outcome_status = 'Adopted' THEN 1.0 ELSE 0.0 END AS is_adopted_exact,
        CASE WHEN s.outcome_status = 'Adopted' THEN 1.0 ELSE 0.0 END AS is_adopted_any
    FROM stays_clean_view AS s
    WHERE s.outcome_at IS NOT NULL
      AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
      AND s.stay_sequence_flag = 'ok'
),
combined AS (
    SELECT * FROM raw_stays
    UNION ALL
    SELECT layer, 'ALL_STAYS' AS group_label, stay_days_int, stay_days_exact, is_adopted_exact, is_adopted_any FROM raw_stays
    UNION ALL
    SELECT * FROM clean_stays
    UNION ALL
    SELECT layer, 'ALL_STAYS' AS group_label, stay_days_int, stay_days_exact, is_adopted_exact, is_adopted_any FROM clean_stays
)
SELECT
    layer,
    group_label,
    COUNT(*) AS n_completed_stays,
    ROUND(AVG(stay_days_int), 2) AS mean_days_integer,
    ROUND(AVG(stay_days_exact), 2) AS mean_los_days,
    ROUND(100.0 * AVG(is_adopted_exact), 2) AS adopted_exact_pct,
    ROUND(100.0 * AVG(is_adopted_any), 2) AS adopted_canonical_pct
FROM combined
GROUP BY layer, group_label
ORDER BY layer, n_completed_stays DESC;
