-- Question: For each animal type, how long does a completed shelter stay last,
--           and what share of stays end in adoption?
--           (M0 project question: how species affects length of stay and outcome.)
-- Result grain: one row per animal_type.
-- Each outcomes row is one completed stay: it closes exactly one intake
-- (composite FK on animal_key + intake_at), and the join to animals is
-- many-to-one, so no row is duplicated and every stay is counted once.
-- Length of stay is computed from the UTC timestamps; outcome_status values
-- starting with 'Adopted' (Adopted, Adopted Altered, Adopted Offsite...) count
-- as adoptions. Types with fewer than 30 completed stays are left out.
SELECT
    a.animal_type,
    COUNT(*)                                                           AS completed_stays,
    ROUND(AVG(julianday(o.outcome_at) - julianday(o.intake_at)), 1)    AS avg_stay_days,
    ROUND(100.0 * AVG(CASE WHEN o.outcome_status LIKE 'Adopted%' THEN 1 ELSE 0 END), 1)
                                                                       AS adoption_rate_pct
FROM outcomes AS o
JOIN animals  AS a ON a.animal_key = o.animal_key
GROUP BY a.animal_type
HAVING COUNT(*) >= 30
ORDER BY completed_stays DESC;
