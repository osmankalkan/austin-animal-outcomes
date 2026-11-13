-- =====================================================================
-- M4 Section 2.6 & 2.7: Companion Animal Subgroup Composition Table
-- Question: How do length of stay and live adoption rate vary across
--           species (Cat vs Dog), life stage (juvenile vs adult), and
--           canonical intake channel?
-- Result grain: One row per (animal_type, life_stage, intake_type).
-- Flag filter: outcome_at IS NOT NULL
--              AND days_in_shelter_flag IN ('ok', 'unusual_high_stay')
--              AND stay_sequence_flag = 'ok'
-- =====================================================================

SELECT
    s.animal_type,
    s.life_stage,
    s.intake_type,
    COUNT(*) AS n_completed_stays,
    ROUND(AVG(s.length_of_stay_days), 2) AS mean_los_days,
    ROUND(AVG(CASE WHEN s.days_in_shelter_flag = 'ok' THEN s.length_of_stay_days END), 2) AS mean_los_ok_only,
    SUM(CASE WHEN s.outcome_status = 'Adopted' THEN 1 ELSE 0 END) AS n_adopted,
    ROUND(100.0 * AVG(CASE WHEN s.outcome_status = 'Adopted' THEN 1.0 ELSE 0.0 END), 2) AS adoption_rate_pct,
    SUM(CASE WHEN s.outcome_status = 'Transfer Out' THEN 1 ELSE 0 END) AS n_transfer_out,
    SUM(CASE WHEN s.outcome_status = 'Return to Owner' THEN 1 ELSE 0 END) AS n_return_to_owner,
    SUM(CASE WHEN s.outcome_status = 'Euthanized' THEN 1 ELSE 0 END) AS n_euthanized
FROM stays_clean_view AS s
WHERE s.outcome_at IS NOT NULL
  AND s.days_in_shelter_flag IN ('ok', 'unusual_high_stay')
  AND s.stay_sequence_flag = 'ok'
  AND s.animal_type IN ('Cat', 'Dog')
GROUP BY s.animal_type, s.life_stage, s.intake_type
ORDER BY s.animal_type, s.life_stage, n_completed_stays DESC;
