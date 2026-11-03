# Cleaning Report — Austin Animal Center Shelter Intakes & Outcomes

## 1. Summary

Before conducting the Milestone M4 exploratory analysis on what drives length of stay and outcome pathways at the Austin Animal Center, our team systematically profiled all 17,696 unique animal entities, 18,428 intake events, 17,299 outcome events, and 1,088 quarantined records loaded in Milestone M2. Profiling across all six required data-quality families revealed that while primary keys and entity-event linkages are structurally sound, raw operational values contain substantial disguised missingness (`'Unknown'` sentinels in sex, coat color, and reproductive status; placeholder tokens and numeric IDs in animal names), non-random missingness conditioned on species and intake channel, and four distinct cross-field or cross-row inconsistencies. Most notably, raw `animal_type` conflates taxonomic species (`Cat`, `Dog`) with juvenile life stages (`Kitten`, `Puppy`) and contradicts calculated age at intake in 3,071 records; 109 intake addresses without postal codes had their 5-digit street house numbers extracted as `found_zip` by M2's regex; 69 animals have a recorded birth date after their first intake; 46 intake stays have multiple outcome rows; and 99 non-euthanasia outcomes (primarily `DOA` and `Unassisted Death`) carry a non-null `euthanasia_reason`. To resolve these issues without altering historical evidence, we built an idempotent, atomic cleaning layer (`animals_clean`, `intakes_clean`, `outcomes_clean`, and `stays_clean_view`) governed by committed CSV mapping tables and `STRICT` SQLite constraints that preserve every M2 table and row count untouched, store `<col>_raw`, `<col>`, and `<col>_flag` triplets, and never impute missing or untrusted measurements. No changes to the M2 loader (`src/load.py` or `sql/schema.sql`) were required after the `m2` tag because M2's primary-key deduplication, timestamp parsing, and quarantine routing operated without data loss; the street-number-as-ZIP extraction artifact was intentionally left untouched in M2 and resolved transparently in the M3 cleaned layer.

---

## 2. Profile highlights

All profiling queries, distributions, and diagnostic visualisations are committed with full execution outputs in [`notebooks/m3_profile.ipynb`](../notebooks/m3_profile.ipynb). Three empirical highlights shaped our cleaning architecture:

1. **Disguised Missingness and Conditioning by Species and Intake Channel (Family 1):** Counting only SQL `NULL`s severely understates missingness in shelter operations. In `animals.sex`, only 21 rows are SQL `NULL`, whereas 2,782 rows (15.72%) are stored as the literal string `'Unknown'`. Similarly, `outcomes.spayed_neutered` has 15 SQL `NULL`s alongside 3,112 `'Unknown'` strings (18.00%), and `intakes.name_at_intake` has 7,553 SQL `NULL`s plus 266 placeholder strings (`'Unknown'`, `'No Name'`, `'*'`, `'Kitten'`, or raw numeric IDs such as `'32103'`). Cross-tabulation in [`notebooks/m3_profile.ipynb`](../notebooks/m3_profile.ipynb) shows that missingness is strongly conditioned on species and intake channel (**MAR**): `date_of_birth` is missing in 75.6% of canonical `Wildlife` (`640 / 847`) and 66.7% of `Bird` (`54 / 81`) records, compared to just 2.4% of `Cat` (`247 / 10,337`) and 2.5% of `Dog` (`163 / 6,569`) records.
2. **Right-Skewed Length-of-Stay Distributions Across Species (Family 3):** Because shelter stays are bounded below by zero and exhibit a heavy right tail caused by medical recovery, foster placement, and legal holds, Gaussian outlier rules (`mean ± 3 SD`) fail completely. Using robust non-parametric quantiles computed after canonical species mapping, median stay is `8.0` days (`IQR = 21.0`, `MAD = 6.0`, `Q3 + 3×IQR = 88.0` days) for `Cat` and `7.0` days (`IQR = 26.0`, `MAD = 6.0`, `Q3 + 3×IQR = 112.0` days) for `Dog`, while `Wildlife` has a median stay of `0.0` days (`Q1 = 0.0`, `Q3 = 35.0`, `IQR = 35.0`, `Q3 + 3×IQR = 140.0` days) because most wildlife arrivals are transferred or euthanized on the day of intake.

| Canonical Species | Completed Outcomes (`n`) | Min (d) | `Q1` (d) | Median (d) | `MAD` (d) | `Q3` (d) | `IQR` (d) | `p95` (d) | `p99` (d) | Max (d) | Outer Fence `Q3 + 3×IQR` (d) | Flagged `unusual_high_stay` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `Cat` | 10,036 | 0 | 4.0 | 8.0 | 6.0 | 25.0 | 21.0 | 80.0 | 145.0 | 358 | 88.0 | 278 |
| `Dog` | 6,303 | 0 | 2.0 | 7.0 | 6.0 | 28.0 | 26.0 | 106.0 | 204.0 | 484 | 112.0 | 365 |
| `Wildlife` | 694 | 0 | 0.0 | 0.0 | 0.0 | 35.0 | 35.0 | 150.3 | 172.1 | 190 | 140.0 | 42 |
| `Small Mammal` | 140 | 0 | 4.0 | 9.0 | 6.0 | 35.0 | 31.0 | 88.1 | 132.2 | 142 | 149.0 | 0 |
| `Bird` | 80 | 0 | 0.0 | 4.0 | 4.0 | 21.2 | 21.2 | 95.3 | 149.1 | 162 | 85.0 | 4 |
| `Reptile` | 35 | 0 | 1.0 | 4.0 | 3.0 | 22.0 | 21.0 | 91.6 | 115.6 | 121 | 85.0 | 3 |
| `Other` | 6 | 4 | 4.0 | 4.0 | 0.0 | 7.8 | 3.8 | 21.6 | 25.1 | 26 | 100.0 (fallback) | 0 |
| `Livestock` | 4 | 0 | 0.0 | 9.0 | 9.0 | 22.5 | 22.5 | 34.6 | 36.5 | 37 | 100.0 (fallback) | 0 |

3. **Multi-Outcome Stays and Cross-Field Clinical Contradictions (Family 4):** Inspecting records per `(animal_key, intake_at)` stay revealed 46 intake stays linked to 2 or 3 outcome rows (`50` secondary exit rows total), where an initial outcome (such as `DOA` disposal or an initial adoption timestamp) was followed by an administrative update linked to the same intake timestamp. Furthermore, 99 non-euthanasia outcomes (`66 DOA`, `27 Unassisted Death`, `4 Reclaimed`, `2 Adopted`) contained non-null values in `euthanasia_reason` (`Suffering`, `Rabies Risk`, `Injured`).

---

## 3. Issues register

Every data-quality issue discovered during profiling is catalogued below with its family, scope, empirical evidence, root cause classification (`Confirmed`, `Inferred`, or `Unknown`), cleaning action, flag value, and the team member who investigated and documented it.

| ID | Family | Table.column | What | Where / when / how many | Evidence | Cause | Decision | Flag | Investigated by |
|---|---|---|---|---|---|---|---|---|---|
| `I-01` | 1 — Missing | `animals.date_of_birth` | Unrecorded birth dates (`NULL`) | 1,561 / 17,696 animals (8.82%); concentrated in `Wildlife` (75.6%) & `ACO - Impound` (19.8%) | Cross-tab in `m3_profile.ipynb`: `Cat` 2.4%, `Dog` 2.5% vs `Wildlife` 75.6%, `Bird` 66.7%, `Reptile` 63.6% | **Confirmed (MAR):** field officers and wildlife staff cannot estimate exact birth dates for wild or unexamined animals | Keep `NULL` (never impute); set `life_stage = 'unknown'` when raw type gives no juvenile signal | `missing_not_recorded` | Osman Kalkan |
| `I-02` | 1 — Missing | `animals.sex`, `animals.primary_color`, `outcomes.spayed_neutered` | Disguised missing values stored as string `'Unknown'` / `'unknown'` alongside SQL `NULL`s | `sex`: 21 `NULL` + 2,782 `'Unknown'`; `primary_color`: 21 `'Unknown'`/`'unknown'`; `spayed_neutered`: 15 `NULL` + 3,112 `'Unknown'` | `repr()` frequency tables in `m3_profile.ipynb`; 97.4% of raw `Mammal` and 30.9% of `Kitten` have `'Unknown'` sex | **Confirmed (MAR):** intake staff select `'Unknown'` in the web form when neonates, fractious wildlife, or DOA animals cannot be sexed | Convert `'Unknown'`/`'unknown'` to `NULL` in cleaned column; preserve original string in `<col>_raw`; distinguish SQL `NULL` from coded `'Unknown'` via separate flags | `missing_unrecorded`, `missing_coded_unknown` | Osman Kalkan |
| `I-03` | 1 — Missing | `intakes.intake_health_condition` | High rate of unlogged intake health (`NULL`) | 15,367 / 18,428 intakes (83.39%) across entire 2025–2026 period | Logged in 50.0% of `Ambulance` intakes (`160/320`) vs 8.6% of `Stray` (`792/9,212`); when logged, 61.9% are abnormal (`Injured`, `Sick`, `Neonate`, `Dead`, `Medical`) | **Inferred (MNAR / MAR):** clinical condition field is optional at front desk and is preferentially filled when an animal presents with visible illness or trauma | Keep `NULL` (never assume `NULL` means `'Normal'`); flag as `missing_not_logged` and restrict M4 health analyses to explicitly logged sub-cohorts | `missing_not_logged` | Irmak Köse |
| `I-04` | 1 — Missing | `intakes.name_at_intake`, `outcomes.name` | Missing names (`NULL`) and non-name placeholder tokens (`'Unknown'`, `'No Name'`, `'*'`, `'Kitten'`, numeric IDs like `'32103'`) | `intakes`: 7,553 `NULL` + 266 placeholders; `outcomes`: 7,235 `NULL` + 249 placeholders | `Stray` intakes are 48.0% `NULL` vs `Owner Surrender` 3.3%; regex check found 118 numeric/ID notes (e.g. `'26902- pico...'`) and 148 generic tokens | **Confirmed (MAR):** strays and wildlife arrive without known names; staff occasionally type cage/animal IDs or species words into the name box | Keep `NULL` as `missing_not_recorded`; convert placeholder/ID tokens to `NULL` with flag `missing_placeholder` while keeping raw string in `<col>_raw` | `missing_not_recorded`, `missing_placeholder` | Irmak Köse |
| `I-05` | 2 — Invalid | `animals.date_of_birth` | Birth date occurs *after* the animal's first shelter intake date | 69 / 17,696 animals (0.39%), spread across Oct 2025 – Sep 2026 (`age_days_at_intake` from `-56` to `-1` days) | Query joining `animals.date_of_birth` against `MIN(substr(intake_at, 1, 10))` in `m3_profile.ipynb` (e.g. `animal_id = '19707'` intake `2026-01-13`, DOB `2026-03-10`) | **Inferred:** clerical data-entry error or retroactive DOB update entered with wrong month/year during medical exam | Set cleaned `date_of_birth = NULL` (do not guess intended birth date); preserve in `date_of_birth_raw` | `invalid_after_intake` | Osman Kalkan |
| `I-06` | 2 — Invalid | `intakes.found_zip` | 5-digit street house numbers captured as `found_zip` when raw address lacked a postal code | 109 / 18,428 intakes (0.59%) across 29 non-Texas 5-digit codes (`10810`, `12209`, `12320`, `11410`, etc.) | Joining `intakes` back to raw JSONL shows raw `found_location` strings such as `'12209 Loma Vista Circle, TX, United States'` where the postal code was omitted and M2's `\b(\d{5})\b` regex matched the house number; plus 2,447 rows with `'TX, United States'` (`NULL` ZIP) | **Confirmed:** upstream address string omitted the 5-digit postal code, causing positional regex extraction to capture the 5-digit street number | Set non-Texas 5-digit numbers (outside prefixes `73, 75, 76, 77, 78, 79`) to `NULL` in `found_zip`; flag as `invalid_house_number_as_zip` | `invalid_house_number_as_zip`, `missing_no_zip_in_address` | Ferdi Derebaşı |
| `I-07` | 2 — Invalid | `outcomes.days_in_shelter` | Outcome timestamp occurs before linked intake timestamp (`days_in_shelter < 0`) | 1 / 17,299 outcomes (`animal_id = '22385'`, `DOA` cat on `2026-03-05`: `intake_at = 23:38:00Z`, `outcome_at = 22:16:00Z`, `-82` min) | Direct timestamp comparison `outcome_at < intake_at` in `m3_profile.ipynb` | **Inferred:** staff logged the field officer's DOA time of death (`22:16Z`) as the outcome before completing the intake creation screen (`23:38Z`) | Set cleaned `days_in_shelter = NULL` and `length_of_stay_days = NULL`; retain `-1` in `days_in_shelter_raw` | `invalid_outcome_before_intake` | Irmak Köse |
| `I-08` | 3 — Unusual | `outcomes.days_in_shelter`, `intakes.found_zip` | Extreme long shelter stays (`> Q3 + 3×IQR` per species, up to 484 days) and valid Texas ZIPs outside Austin metro (`786xx`/`787xx`) | `692` outcomes (`278 Cat`, `365 Dog`, `42 Wildlife`, `4 Bird`, `3 Reptile`); `15` intakes in valid non-Austin TX ZIPs (`73301`, `76642`, `78957`, etc.) | Species-specific Tukey outer fences (`88d` Cat, `112d` Dog, `140d` Wildlife) and Texas postal prefix verification in `m3_profile.ipynb` | **Confirmed:** genuine long-term shelter residents (behavioral/medical rehabilitation, court holds) and regional Texas transfers/strays outside Travis County | **Keep all values intact** in cleaned columns; annotate with `unusual_high_stay` and `unusual_outside_service_area` so M4 can run sensitivity checks | `unusual_high_stay`, `unusual_outside_service_area` | Ferdi Derebaşı |
| `I-09` | 4 — Cross-field | `animals.animal_type`, `animals.date_of_birth` | Raw `animal_type` mixes species (`Cat`, `Dog`) with juvenile stage (`Kitten`, `Puppy`) and contradicts age at first intake | `2,126` rows raw-labelled `Kitten`/`Puppy`; `3,071` rows have age-vs-label conflict (`3,070` raw `Cat`/`Dog` aged `< 56` days at intake; `1` raw `Kitten` `animal_id = '36020'` aged `732` days) | Age-at-intake (`first_intake_date - date_of_birth`) distribution by raw `animal_type` in `m3_profile.ipynb` | **Confirmed:** shelter software stores `'Kitten'` and `'Puppy'` as values inside the `animal_type` dropdown, and staff frequently leave `'Cat'` or `'Dog'` selected for neonatal litters | Separate canonical species (`animal_type`) from `life_stage` (`juvenile` if `age <= 180d`, `adult` if `> 180d`); flag discrepancies as `conflict_type_vs_age` | `mapped_life_stage`, `conflict_type_vs_age` | Osman Kalkan |
| `I-10` | 4 — Cross-row | `outcomes` (`animal_key, intake_at`), `intakes` sequence | Multiple outcome events linked to the same intake stay, plus overlapping or unclosed multi-stay sequences | `46` intake stays have 2–3 outcomes (`50` secondary exit rows); `1` intake (`animal_key = 13834`) overlaps prior stay; `12` intakes follow an open prior stay | Grouped counts by `(animal_key, intake_at)` and `LAG(intake_at)` window checks in `m3_profile.ipynb` | **Confirmed:** secondary outcomes are disposal/administrative updates logged against the same intake; open-stay re-intakes occur when a prior foster/transfer exit was not closed before return | Keep all rows to preserve M2 row parity; null out `days_in_shelter` on the `50` secondary exits (`duplicate_exit_for_stay`) and filter them out in `stays_clean_view`; flag sequence anomalies on `intakes_clean` | `duplicate_exit_for_stay`, `overlap_with_prior_stay`, `reintake_after_open_stay` | Irmak Köse |
| `I-11` | 4 — Cross-field | `outcomes.euthanasia_reason`, `outcomes.spayed_neutered` | Non-euthanasia outcomes carrying a `euthanasia_reason`; plus missing reason on euthanized rows and spay status regression | `99` non-euthanized rows (`66 DOA`, `27 Unassisted Death`, `4 Reclaimed`, `2 Adopted`) have `euthanasia_reason`; `20` `Euthanized` rows lack a reason; `1` outcome has `spayed_neutered = 'No'` after `previously_spayed_neutered = 1` | Cross-tab of `outcome_status` vs `euthanasia_reason` and `intakes.previously_spayed_neutered` vs `outcomes.spayed_neutered` in `m3_profile.ipynb` | **Inferred:** staff used the euthanasia reason field to note cause of death (`Suffering`, `Injured`, `Rabies Risk`) on DOA/unassisted deaths, or left a pre-selected reason when changing outcome status | Set cleaned `euthanasia_reason = NULL` on the `99` non-euthanasia rows (`conflict_non_euthanasia_status`); flag the `20` euthanized rows with `NULL` reason (`missing_euthanized_no_reason`); null out the `1` contradictory spay record (`conflict_previously_spayed`) | `conflict_non_euthanasia_status`, `missing_euthanized_no_reason`, `conflict_previously_spayed` | Ferdi Derebaşı |
| `I-12` | 5 — Text / Categories | `animal_type`, `intake_type`, `outcome_status`, `spayed_neutered`, `intake_health_condition`, `name` | Multi-level subtype slashes, fine-grained wildlife taxa, casing inconsistencies, and shelter `'*'` name prefixes | `animal_type`: 20 raw -> 8 canonical (`1,063 mapped_category`, `2,126 mapped_life_stage`); `intake_type`: 21 -> 8 (`10,847 mapped_category`); `outcome_status`: 27 -> 7 (`2,892 mapped_subtype`); `spayed_neutered`: `2,027` `'yes'` -> `'Yes'`; `intake_health`: `12` uppercase variants; `name`: `129` `'*'` prefixes, `16` case fixes | `repr()` inspection of all categorical and text columns in `m3_profile.ipynb` | **Confirmed:** M2 preserved exact source strings (`'Adopted / Foster'`, `'yes'`, `'NEONATAL'`, `'*Bella'`), which require canonical grouping and case normalisation before M4 aggregation | Map categories via committed CSV tables (`data/mappings/*.csv`); standardise `'yes'` -> `'Yes'` and health labels; strip leading `'*'` shelter prefixes while preserving `<col>_raw` | `mapped_category`, `mapped_subtype`, `normalised_case`, `standardised_label`, `stripped_shelter_prefix` | Ferdi Derebaşı |

### Checked and not found

| Family | Check | Result |
|---|---|---|
| 2 — Invalid | Future `date_of_birth > '2026-10-08'` or `intake_at` / `outcome_at` after snapshot retrieval timestamp | **0 rows found** (`MAX(date_of_birth) = '2026-09-29'`, `MAX(intake_at) = '2026-10-04T23:54:00Z'`, `MAX(outcome_at) = '2026-10-05T03:00:00Z'`). |
| 2 — Invalid | Implausibly old companion animal birth dates (`age_at_intake > 30 years` / `10,950 days`) | **0 rows found** (`MIN(date_of_birth) = '2004-06-16'`, corresponding to a 21.8-year-old tortoise/bird/senior animal within biological limits). |
| 5 — Text | Leading/trailing whitespace or invisible Unicode control characters in categorical columns | **0 rows found** (`repr()` inspection confirmed M2's `clean_text()` stripped all surrounding whitespace). |
| 5 — Units | Mixed duration units (hours vs days) in `outcomes.days_in_shelter` | **0 rows found** (`days_in_shelter` is consistently integer calendar days `julianday(date(outcome_at)) - julianday(date(intake_at))` across all 17,298 valid rows; we additionally compute exact fractional `length_of_stay_days` from UTC timestamps). |
| 6 — Identity | Duplicate primary keys in `animals`, `intakes`, or `outcomes`, or confirmed split-identity animals across distinct `animal_id` values | **0 duplicate PKs** (`STRICT` PKs enforced in M2); out of 24 multi-ID groups sharing `(name, DOB, breed, color, sex)`, 21 share identical intake timestamps and sequential IDs (littermates surrendered together), and only 1 named pair (`'Oliver'`, IDs `'25447'` and `'30349'`) is a plausible re-registration across separate months, which cannot be merged without microchip data. |

---

## 4. Missing values

We analysed every column with non-trivial missingness by cross-tabulating missing shares against canonical species, intake channel, and calendar month in [`notebooks/m3_profile.ipynb`](../notebooks/m3_profile.ipynb):

1. **`animals.date_of_birth` (`1,561` missing + `69` invalidated = `1,630` NULLs in `animals_clean`, 9.21%) — Mechanism: MAR.** Missingness is strongly predictable from observed species and intake type: canonical `Wildlife` (`75.6%` missing), `Bird` (`66.7%`), `Reptile` (`63.6%`), and `Small Mammal` (`32.4%`) lack birth dates because wild and exotic arrivals rarely have known histories, whereas `Cat` (`2.4%`) and `Dog` (`2.5%`) routinely receive dental or developmental age estimates during intake triage. By intake channel, `ACO - Impound` has `19.8%` missing DOB compared to `3.4%` in `Owner Surrender` and `0.4%` in `Returns`. **Consequence for M4:** Any age-stratified analysis (such as comparing juvenile vs adult adoption rates or length of stay) is valid for `Cat` and `Dog` (where `> 97.5%` have valid DOB or raw juvenile labels), but **unsafe** across `Wildlife`, `Bird`, and `Reptile` without explicitly including the `life_stage = 'unknown'` category.
2. **`animals.sex` (`2,803` NULLs in `animals_clean`, 15.84%) and `outcomes.spayed_neutered` (`3,128` NULLs in `outcomes_clean`, 18.08%) — Mechanism: MAR.** Both columns are dominated by coded `'Unknown'` strings (`2,782` and `3,112` rows respectively) rather than SQL `NULL`s (`21` and `15` rows). Missingness is heavily concentrated in `Wildlife` (`81.5%` unknown sex), `Bird` (`75.3%`), and neonatal kittens/puppies (`30.9%` in raw `Kitten`, `26.6%` in raw `Puppy` vs `4.3%` in raw adult `Dog`) where external sex determination is difficult or deferred until spay/neuter surgery. **Consequence for M4:** M4 models comparing outcomes by sex or sterilization status must restrict to companion animals above surgical age or treat `NULL` (`missing_coded_unknown`) as an explicit analytical stratum rather than dropping rows via complete-case deletion, which would disproportionately drop neonates and wildlife.
3. **`intakes.intake_health_condition` (`15,367` NULLs in `intakes_clean`, 83.39%) — Mechanism: MNAR and MAR.** Across all 13 months from October 2025 through October 2026, between `80.5%` and `86.4%` of monthly intakes have `NULL` in `intake_health_condition`. However, ambulance intakes have a `50.0%` logging rate (`160 / 320`) compared to `8.6%` for strays (`792 / 9,212`), and among the `3,061` intakes where health *is* logged, `61.9%` (`1,895` rows) record an abnormal condition (`Injured = 885`, `Sick = 491`, `Neonate = 359`, `Dead = 153`, `Medical = 7`) while only `38.1%` (`1,166` rows) record `'Normal'`. Staff preferentially fill this field when an animal is visibly sick, injured, or neonatal (**MNAR**: the unobserved health state drives whether the field is recorded). **Consequence for M4:** M4 **must not** treat `intake_health IS NULL` as `'Normal'`, nor can M4 estimate overall shelter morbidity rates from the `16.61%` logged subset without acknowledging severe selection bias.
4. **`intakes.name_at_intake` (`7,819` NULLs in `intakes_clean`, 42.43%) and `intakes.found_zip` (`2,556` NULLs in `intakes_clean`, 13.87%) — Mechanism: MAR.** Name missingness is driven by intake channel (`48.0%` missing in `Stray` and `91.2%` in `Wildlife` vs `3.3%` in `Owner Surrender`). Postal code missingness occurs when field officers record only the state/country placeholder `'TX, United States'` (`2,447` rows) or omit the 5-digit ZIP code from a street address (`109` rows where the street number was captured). **Consequence for M4:** Geographic analyses by `found_zip` represent `86.13%` of intakes and should report the `13.87%` unmapped share rather than assuming uniform spatial missingness.

---

## 5. Flags and rules

When multiple rules could apply to a column, `src/clean.py` evaluates them in a strict, deterministic precedence order (`Order 1 -> 2 -> 3 ...`) so every row receives a single unambiguous `<col>_flag`. The complete flag dictionary across `animals_clean`, `intakes_clean`, and `outcomes_clean` is documented below.

| Order | Table.column | Flag | Meaning | Rule (with constants) | Cleaned value | Count |
|---:|---|---|---|---|---|---:|
| 1 | `animals_clean.animal_type` | `mapped_life_stage` | Raw type encoded juvenile stage (`Kitten` / `Puppy`) | `map_animal_type.kind == 'life_stage_to_species'` | Canonical species (`'Cat'` or `'Dog'`) | 2,126 |
| 2 | `animals_clean.animal_type` | `mapped_category` | Fine-grained taxon mapped to canonical species group | `map_animal_type.kind == 'taxonomic_grouping'` | Canonical species (`'Wildlife'`, `'Small Mammal'`, `'Bird'`, `'Reptile'`) | 1,063 |
| 3 | `animals_clean.animal_type` | `ok` | Raw type already equals canonical species name | `map_animal_type.kind == 'exact'` | Unchanged canonical species | 14,507 |
| 1 | `animals_clean.life_stage` | `unknown_no_dob` | No valid DOB and raw type has `life_stage_default = 'unknown'` | `cleaned_dob IS NULL` and `life_stage_default == 'unknown'` | `'unknown'` | 776 |
| 2 | `animals_clean.life_stage` | `from_raw_type` | No valid DOB, but raw type is `Kitten`, `Puppy`, `Cat`, or `Dog` | `cleaned_dob IS NULL` and `life_stage_default IN ('juvenile', 'adult')` | `life_stage_default` | 854 |
| 3 | `animals_clean.life_stage` | `conflict_type_vs_age` | Age at first intake contradicts raw `Kitten`/`Puppy` or `Cat`/`Dog` label | `(raw IN ('Kitten','Puppy') AND age_days > JUVENILE_CONFLICT_AGE_DAYS [365])` OR `(raw IN ('Cat','Dog') AND age_days < ADULT_LABEL_MIN_AGE_DAYS [56])` | `'juvenile'` if `age_days <= JUVENILE_MAX_AGE_DAYS (180)` else `'adult'` | 3,071 |
| 4 | `animals_clean.life_stage` | `from_age_at_intake` | Derived from valid age at first intake with no label conflict | `cleaned_dob IS NOT NULL` and no conflict under Order 3 | `'juvenile'` if `age_days <= JUVENILE_MAX_AGE_DAYS (180)` else `'adult'` | 12,995 |
| 1 | `animals_clean.sex` | `missing_unrecorded` | Sex is SQL `NULL` in M2 | `sex_raw IS NULL` | `NULL` | 21 |
| 2 | `animals_clean.sex` | `missing_coded_unknown` | Sex coded as `'Unknown'` sentinel | `sex_raw.casefold() == 'unknown'` | `NULL` | 2,782 |
| 3 | `animals_clean.sex` | `ok` | Valid biological sex (`'Male'` or `'Female'`) | `sex_raw IN ('Male', 'Female')` | `sex_raw` | 14,893 |
| 1 | `animals_clean.primary_color` | `missing_coded_unknown` | Coat color coded as `'Unknown'` or `'unknown'` | `primary_color_raw.casefold() == 'unknown'` | `NULL` | 21 |
| 2 | `animals_clean.primary_color` | `ok` | Recorded coat color string | Otherwise | `primary_color_raw` | 17,675 |
| 1 | `animals_clean.date_of_birth` | `missing_not_recorded` | Birth date not recorded in M2 | `date_of_birth_raw IS NULL` | `NULL` | 1,561 |
| 2 | `animals_clean.date_of_birth` | `invalid_future_date` | Birth date after dataset snapshot date | `date_of_birth_raw > MAX_VALID_DATE ('2026-10-08')` | `NULL` | 0 |
| 3 | `animals_clean.date_of_birth` | `invalid_after_intake` | Birth date occurs after animal's first shelter intake date | `date_of_birth_raw > first_intake_date` | `NULL` | 69 |
| 4 | `animals_clean.date_of_birth` | `ok` | Plausible birth date on or before first intake | Otherwise | `date_of_birth_raw` | 16,066 |
| 1 | `intakes_clean.intake_type` | `mapped_category` | Subtype slash or operational variant mapped to canonical intake channel | `map_intake_type.kind != 'exact'` | Canonical intake category (`8` categories) | 10,847 |
| 2 | `intakes_clean.intake_type` | `ok` | Raw intake type already matches canonical category | `map_intake_type.kind == 'exact'` | `intake_type_raw` | 7,581 |
| 1 | `intakes_clean.name_at_intake` / `outcomes_clean.name` | `missing_not_recorded` | Name is SQL `NULL` in M2 | `name_raw IS NULL` | `NULL` | 7,553 / 7,235 |
| 2 | `intakes_clean.name_at_intake` / `outcomes_clean.name` | `missing_placeholder` | Generic token (`'Unknown'`, `'No Name'`, `'*'`, `'Kitten'`) or numeric shelter ID | `core.casefold() IN PLACEHOLDER_NAMES` or matches `^\d+$` or `^\d{4,}\s*[-–]` | `NULL` | 266 / 249 |
| 3 | `intakes_clean.name_at_intake` / `outcomes_clean.name` | `stripped_shelter_prefix` | Leading shelter administrative asterisk `'*'` stripped | `name_raw.startswith('*')` and remaining name is valid | Stripped & case-normalised name | 129 / 129 |
| 4 | `intakes_clean.name_at_intake` / `outcomes_clean.name` | `normalised_case` | Multi-letter ALL-CAPS or all-lowercase name title-cased | `len(alpha) > 3` and (`isupper()` or `islower()`) | `.title()` normalised name | 16 / 16 |
| 5 | `intakes_clean.name_at_intake` / `outcomes_clean.name` | `ok` | Valid animal name needing no transformation | Otherwise | `name_raw` | 10,464 / 9,670 |
| 1 | `intakes_clean.intake_health` | `missing_not_logged` | Intake health condition not logged (`NULL`) | `intake_health_raw IS NULL` | `NULL` | 15,367 |
| 2 | `intakes_clean.intake_health` | `standardised_label` | Uppercase / abbreviated variant (`'NEONATAL'`, `'MED ATTN'`, `'MEDICAL'`) | `intake_health_raw != HEALTH_STANDARD_MAP[intake_health_raw]` | `'Neonate'` or `'Medical'` | 12 |
| 3 | `intakes_clean.intake_health` | `ok` | Standard health condition label | Otherwise | `intake_health_raw` | 3,049 |
| 1 | `intakes_clean.found_zip` | `missing_no_zip_in_address` | No 5-digit token present in raw address (`'TX, United States'`) | `found_zip_raw IS NULL` | `NULL` | 2,447 |
| 2 | `intakes_clean.found_zip` | `invalid_house_number_as_zip` | 5-digit street house number outside Texas postal prefixes | `not found_zip_raw.startswith(TEXAS_ZIP_PREFIXES)` | `NULL` | 109 |
| 3 | `intakes_clean.found_zip` | `unusual_outside_service_area` | Valid Texas ZIP outside Austin metro (`786xx`/`787xx`) | `not found_zip_raw.startswith(AUSTIN_METRO_ZIP_PREFIXES)` | Kept (`found_zip_raw`) | 15 |
| 4 | `intakes_clean.found_zip` | `ok` | Valid Austin metro 5-digit ZIP code (`786xx` or `787xx`) | `found_zip_raw.startswith(('786', '787'))` | `found_zip_raw` | 15,857 |
| 1 | `intakes_clean.stay_sequence` | `overlap_with_prior_stay` | Intake occurs *before* the animal's prior stay outcome timestamp | `intake_at < prior_stay_max_outcome_at` | Flagged on intake row | 1 |
| 2 | `intakes_clean.stay_sequence` | `reintake_after_open_stay` | Intake occurs while the animal's prior intake has no recorded outcome | `prior_stay_outcome_count == 0` | Flagged on intake row | 12 |
| 3 | `intakes_clean.stay_sequence` | `ok` | First intake or cleanly follows a completed prior stay | Otherwise | Flagged `'ok'` | 18,415 |
| 1 | `outcomes_clean.outcome_status` | `mapped_subtype` | Subtype slash (`'Adopted / Foster'`, `'Transfer / Rescue Group'`) mapped to canonical outcome | `map_outcome_status.kind != 'exact'` | Canonical outcome (`7` categories) | 2,892 |
| 2 | `outcomes_clean.outcome_status` | `ok` | Raw status already matches canonical outcome category | `map_outcome_status.kind == 'exact'` | `outcome_status_raw` | 14,407 |
| 1 | `outcomes_clean.euthanasia_reason` | `not_applicable` | Non-euthanasia outcome with `NULL` euthanasia reason | `canonical_outcome != 'Euthanized'` and `euthanasia_reason_raw IS NULL` | `NULL` | 16,117 |
| 2 | `outcomes_clean.euthanasia_reason` | `conflict_non_euthanasia_status` | Non-euthanasia outcome (`DOA`, `Unassisted Death`, `Reclaimed`, `Adopted`) carrying a non-null euthanasia reason | `canonical_outcome != 'Euthanized'` and `euthanasia_reason_raw IS NOT NULL` | `NULL` (raw reason kept in `euthanasia_reason_raw`) | 99 |
| 3 | `outcomes_clean.euthanasia_reason` | `missing_euthanized_no_reason` | `Euthanized` outcome missing a recorded reason | `canonical_outcome == 'Euthanized'` and `euthanasia_reason_raw IS NULL` | `NULL` | 20 |
| 4 | `outcomes_clean.euthanasia_reason` | `ok` | `Euthanized` outcome with valid documented reason | `canonical_outcome == 'Euthanized'` and `euthanasia_reason_raw IS NOT NULL` | `euthanasia_reason_raw` | 1,063 |
| 1 | `outcomes_clean.spayed_neutered` | `missing_unrecorded` | Reproductive status is SQL `NULL` in M2 | `spayed_neutered_raw IS NULL` | `NULL` | 15 |
| 2 | `outcomes_clean.spayed_neutered` | `missing_coded_unknown` | Coded as `'Unknown'` sentinel | `spayed_neutered_raw.casefold() == 'unknown'` | `NULL` | 3,112 |
| 3 | `outcomes_clean.spayed_neutered` | `conflict_previously_spayed` | Outcome records `'No'` despite `intakes.previously_spayed_neutered == 1` | `norm_val == 'No'` and `previously_spayed_neutered == 1` | `NULL` | 1 |
| 4 | `outcomes_clean.spayed_neutered` | `normalised_case` | Lowercase `'yes'` normalised to `'Yes'` | `spayed_neutered_raw == 'yes'` | `'Yes'` | 2,027 |
| 5 | `outcomes_clean.spayed_neutered` | `ok` | Valid `'Yes'` or `'No'` consistent with intake history | Otherwise | `'Yes'` or `'No'` | 12,144 |
| 1 | `outcomes_clean.days_in_shelter` | `invalid_outcome_before_intake` | Outcome timestamp precedes linked intake timestamp (`days_in_shelter < 0`) | `outcome_at < intake_at` or `days_in_shelter_raw < 0` | `NULL` (`days_in_shelter` & `length_of_stay_days`) | 1 |
| 2 | `outcomes_clean.days_in_shelter` | `duplicate_exit_for_stay` | Secondary outcome record for the same `(animal_key, intake_at)` stay | `row_number() OVER (PARTITION BY animal_key, intake_at ORDER BY outcome_at) > 1` | `NULL` (prevents double-counting stay days; raw kept) | 50 |
| 3 | `outcomes_clean.days_in_shelter` | `unusual_high_stay` | Primary outcome stay exceeds species-specific Tukey outer fence `Q3 + 3×IQR` | `days_in_shelter_raw > species_fence[canonical_species]` | **Kept** (`days_in_shelter_raw` & exact `length_of_stay_days`) | 692 |
| 4 | `outcomes_clean.days_in_shelter` | `ok` | Primary outcome stay within normal species duration fence | Otherwise | **Kept** (`days_in_shelter_raw` & exact `length_of_stay_days`) | 16,556 |

### Constants

Every threshold in `src/clean_config.py` is justified below with one sentence explaining why it takes its value and what changes if it is moved:

- `JUVENILE_MAX_AGE_DAYS = 180`: Standard veterinary and shelter threshold (6 months) separating juvenile (`Kitten`/`Puppy`) from adult companion animals, covering 99.9% of rows raw-labelled `Kitten` (median 35d) or `Puppy` (median 46d); lowering it to 120 days reclassifies 1,418 4–6 month old animals from `juvenile` to `adult`.
- `JUVENILE_CONFLICT_AGE_DAYS = 365`: Hard 1-year upper bound beyond which a raw `'Kitten'` or `'Puppy'` label is biologically incompatible with a juvenile life stage (flagging 1 two-year-old cat `animal_id = '36020'`); lowering it to 180 days would flag only 2 additional rows (`198d` and `240d`).
- `ADULT_LABEL_MIN_AGE_DAYS = 56`: Eight-week weaning threshold below which an animal raw-labelled `'Cat'` or `'Dog'` is indisputably a unweaned neonate/juvenile (`3,070` rows flagged `conflict_type_vs_age`); raising it to 180 days increases `conflict_type_vs_age` from `3,071` to `6,593` rows.
- `MAX_VALID_DATE = "2026-10-08"`: Snapshot retrieval date of the raw dataset, serving as the hard ceiling for calendar date checks (`0` rows exceed this ceiling).
- `IQR_FENCE_MULTIPLIER = 3.0`: Tukey's outer fence multiplier (`Q3 + 3.0 × IQR`) chosen because length of stay is heavily right-skewed (`Cat` fence `88.0d`, `Dog` fence `112.0d`), flagging `692` extreme stays (`4.0%`); reducing it to `1.5` (inner fence: `56.5d` Cat, `67.0d` Dog) would flag `1,684` routine 2–3 month stays (`9.7%`).
- `MIN_GROUP_SIZE_FOR_IQR = 30`: Minimum completed outcomes required for a canonical species to estimate its own quartiles (`Livestock n=4` and `Other n=6` fall back to the global `100.0d` fence); changing it between `10` and `35` leaves all group assignments unchanged because `Reptile` has `n=35`.
- `TEXAS_ZIP_PREFIXES = ("73", "75", "76", "77", "78", "79")`: Official USPS 2-digit prefix range for Texas (including `733xx` Austin IRS and `75xxx–79xxx` statewide), separating `15,872` genuine Texas ZIPs from `109` 5-digit street house numbers (`10810`, `12209`, etc.).
- `AUSTIN_METRO_ZIP_PREFIXES = ("786", "787")`: USPS 3-digit prefixes for Travis/Williamson/Hays counties (Austin metro service area), separating `15,857` local intakes from `15` valid out-of-area Texas ZIPs (`unusual_outside_service_area`).
- `PLACEHOLDER_NAMES = frozenset({"unknown", "no name", ...})`: Explicit case-folded lexicon of 18 non-name shelter tokens that, together with numeric ID regexes, nulls out `266` intake and `249` outcome non-name placeholders; removing species tokens (`kitten`, `puppy`, `cat`, `dog`, `bat`, `baby`) would leave `64` generic species labels in the cleaned name column.
- `HEALTH_STANDARD_MAP = {...}`: Maps `12` uppercase variants (`'NEONATAL'` -> `'Neonate'`, `'MED ATTN'`/`'MEDICAL'` -> `'Medical'`) into canonical clinical labels so case variants do not split M4 groupbys.
- `MAX_CORE_DOB_MISSING_SHARE = 0.10`: Maximum tolerable `date_of_birth` missing share (`10%`) in core companion species (`Cat = 2.6%`, `Dog = 2.8%`) before `validate.py` raises a core-species warning.
- `MAX_OVERALL_DOB_MISSING_SHARE = 0.20`: Per-species missingness warning threshold (`20%`) that surfaces the expected MAR missingness in non-companion species (`Wildlife = 75.6%`, `Bird = 66.7%`, `Reptile = 63.6%`, `Small Mammal = 32.4%`) in `WARN-1`.
- `FRESHNESS_WINDOW_DAYS = 7`: Rolling window before `MAX_VALID_DATE` (`>= 2026-10-01`) used by `WARN-2` to verify active intake ingestion per species (`6/8` species active; rare `Livestock` and `Other` flagged as `WARN`).
- `MAX_EXPECTED_DUPLICATE_EXITS = 0`: Zero-tolerance threshold for unflagged secondary outcomes on a single stay (`WARN-3` surfaces the `50` secondary outcome rows excluded from `stays_clean_view`).

---

## 6. Mapping and identity decisions

Rather than hardcoding dictionaries inside Python scripts, all category mappings are stored as committed CSV data in [`data/mappings/animal_type_map.csv`](../data/mappings/animal_type_map.csv), [`data/mappings/intake_type_map.csv`](../data/mappings/intake_type_map.csv), and [`data/mappings/outcome_status_map.csv`](../data/mappings/outcome_status_map.csv), and loaded into `STRICT` SQLite tables (`map_animal_type`, `map_intake_type`, `map_outcome_status`) with `decided_by`, `decided_at`, and `note` metadata.

| Mapping Table (CSV) | Distinct Raw Values | Step 1: Exact Canonical Match | Step 2: Normalised / Prefix Key Match | Step 3: Domain Taxonomic Grouping (Human Decision) | Unmatched Raw Values |
|---|---:|---:|---:|---:|---:|
| [`animal_type_map.csv`](../data/mappings/animal_type_map.csv) | 20 | 4 (`Cat`, `Dog`, `Bird`, `Reptile` -> `14,507` rows) | 2 (`Bird (wildlife)` -> `Bird`, `Reptile (Wildlife)` -> `Reptile` -> `41` rows) | 14 (`Kitten` -> `Cat`, `Puppy` -> `Dog` [`2,126` rows]; `Mammal`, `Marsupial` -> `Wildlife`; `Fowl` -> `Bird`; `Rabbit`, `Guinea Pig`, `Rodent`, `Ferret` -> `Small Mammal`; `Goat`, `Sheep` -> `Livestock`; `Crab/fish`, `Insect / Arachnid`, `Zoo` -> `Other` -> `1,022` rows) | **0** |
| [`intake_type_map.csv`](../data/mappings/intake_type_map.csv) | 21 | 3 (`Stray`, `Owner Surrender`, `Wildlife` -> `7,581` rows) | 10 (`ACO - Owner Surrender`, `Ambulance - Owner Surrender`, `Humane Officer Surrendered` -> `Owner Surrender`; `Returns` -> `Return`; `Ambulance - Wildlife` -> `Wildlife`; `Community Animal - *` -> `Community Program`; `ACO - Impound` -> `ACO Impound` -> `9,977` rows) | 8 (`Abandoned` -> `Stray`; `Humane Officer` -> `ACO Impound`; `Agency` -> `Transfer In`; `Ambulance` -> `Medical / Emergency`; `Shelter Offspring`, `Foster Offspring`, `Cruelty Case Offspring` -> `Born in Care` -> `870` rows) | **0** |
| [`outcome_status_map.csv`](../data/mappings/outcome_status_map.csv) | 27 | 4 (`Adopted`, `Transfer Out`, `Euthanized`, `Return to Habitat` -> `14,407` rows) | 9 (`Adopted Altered`, `Adopted Offsite(Altered)`, `Adopted Unaltered`, `Adopted Offsite`, `Adopted Offsite(Unaltered)`, `Adopted to Finder` -> `Adopted`; `Euthanasia - RTO` -> `Euthanized`; `Returned To Owner` -> `Return to Owner` -> `792` rows) | 14 (`Reclaimed`, `Redemption (Offsite)`, `Bite Quarantine (Home)` -> `Return to Owner`; `DOA`, `Unassisted Death`, `Unassisted Death - In Transit`, `Unassisted Death - In Foster`, `Interred`, `Cremated`, etc. -> `Died / DOA`; `Unresolved File`, `Escaped`, `Stolen` -> `Administrative / Other` -> `2,100` rows) | **0** |

**Identity Decisions (Family 6):** We evaluated whether distinct `animal_id` records sharing identical `(normalised_name, date_of_birth, primary_breed, primary_color, sex)` represent split identities of the same physical animal. Across all 17,696 animals, 24 attribute groups contained multiple `animal_id`s:
- **21 groups (`52` animals)** had identical `intake_at` timestamps down to the minute and sequential shelter IDs (e.g., `animal_id` `'31772'` and `'31773'` both named `'Siouxsie'`, Domestic Shorthair born `2026-04-28`, admitted `2026-06-28T16:12:00Z`; or litters named `'Kitten'`, `'Unknown'`, `'Star'`). These are confirmed **littermates** surrendered together under a shared litter/mother name, not duplicate keys.
- **2 groups (`4` animals)** carried generic names (`'Unknown'`, `'Puppy'`) across different weeks and represent distinct unnamed animals of common breeds (`Domestic Shorthair`, `Pit Bull`).
- **1 group (`2` animals)** involved two Orange Tabby male Domestic Shorthairs named `'Oliver'` born `2026-03-24`: `animal_id = '25447'` (admitted as a stray `2026-04-06`, adopted `2026-05-11`) and `animal_id = '30349'` (admitted via owner surrender `2026-07-22`, adopted `2026-07-25`). While `'25447'` and `'30349'` could plausibly be the same cat re-registered upon surrender without its prior shelter ID, public Austin Animal Center exports do not include microchip numbers, and `'Oliver'` is one of the most common cat names for Orange Tabby Domestic Shorthairs during spring kitten season. Following M3 rules against speculative merging, we kept all 17,696 `animal_id` entities unmerged.

---

## 7. Counts and validation

Running `python src/clean.py` twice in succession confirmed idempotency, atomic execution, and zero modification to M2 tables (verified via SHA-256 table hashes before and after):

```text
========================================================================
Austin Animal Center - M3 Data Cleaning Summary
========================================================================
database:        data/austin_animals.db
M2 raw tables:   UNTOUCHED (SHA-256 hashes verified before & after: True)
------------------------------------------------------------------------
animals_clean:   17,696 rows (raw: 17,696)
  animal_type_flag:    ok=14,507, mapped_life_stage=2,126, mapped_category=1,063
  life_stage_flag:     from_age_at_intake=12,995, conflict_type_vs_age=3,071, from_raw_type=854, unknown_no_dob=776
  sex_flag:            ok=14,893, missing_coded_unknown=2,782, missing_unrecorded=21
  primary_color_flag:  ok=17,675, missing_coded_unknown=21
  date_of_birth_flag:  ok=16,066, missing_not_recorded=1,561, invalid_after_intake=69

intakes_clean:   18,428 rows (raw: 18,428)
  intake_type_flag:    mapped_category=10,847, ok=7,581
  name_at_intake_flag: ok=10,464, missing_not_recorded=7,553, missing_placeholder=266, stripped_shelter_prefix=129, normalised_case=16
  intake_health_flag:  missing_not_logged=15,367, ok=3,049, standardised_label=12
  found_zip_flag:      ok=15,857, missing_no_zip_in_address=2,447, invalid_house_number_as_zip=109, unusual_outside_service_area=15
  stay_sequence_flag:  ok=18,415, reintake_after_open_stay=12, overlap_with_prior_stay=1

outcomes_clean:  17,299 rows (raw: 17,299)
  outcome_status_flag:    ok=14,407, mapped_subtype=2,892
  euthanasia_reason_flag: not_applicable=16,117, ok=1,063, conflict_non_euthanasia_status=99, missing_euthanized_no_reason=20
  name_flag:              ok=9,670, missing_not_recorded=7,235, missing_placeholder=249, stripped_shelter_prefix=129, normalised_case=16
  spayed_neutered_flag:   ok=12,144, missing_coded_unknown=3,112, normalised_case=2,027, missing_unrecorded=15, conflict_previously_spayed=1
  days_in_shelter_flag:   ok=16,556, unusual_high_stay=692, duplicate_exit_for_stay=50, invalid_outcome_before_intake=1
------------------------------------------------------------------------
mappings:        map_animal_type 20 rows · map_intake_type 21 rows · map_outcome_status 27 rows · unmatched raw values: 0
STATUS: COMPLETE
========================================================================
```

Running `python src/validate.py` executes 7 automated checks (3 `STOP`, 4 `WARN`) and exits with code `0`:

```text
========================================================================
AUSTIN ANIMAL CENTER -- M3 DATA QUALITY VALIDATION REPORT
========================================================================
[PASS] STOP-1 (STOP): Row count parity between M2 and M3 cleaned tables
       -> Exact match: animals=17696, intakes=18428, outcomes=17299
[PASS] STOP-2 (STOP): Zero invalid or sentinel values surviving in cleaned columns
       -> 0 future/post-intake DOBs, 0 negative stays, 0 'Unknown' sentinels, 0 non-TX ZIPs, 0 unmapped categories
[PASS] STOP-3 (STOP): Foreign key referential integrity and STRICT table enforcement
       -> 0 FK violations across 10 STRICT tables
[WARN] WARN-1 (WARN): Missing date_of_birth share by canonical species
       -> Non-companion groups >20% (MAR): Wildlife=75.6% (640/847), Small Mammal=32.4% (46/142), Bird=66.7% (54/81), Reptile=63.6% (28/44), Other=83.3% (5/6), Livestock=60.0% (3/5)
[WARN] WARN-2 (WARN): Species freshness in final 7-day window (>= 2026-10-01)
       -> 6/8 species active; rare groups with 0 intakes in last 7d: Other (latest=2026-05-13, total=6), Livestock (latest=2026-07-21, total=5)
[WARN] WARN-3 (WARN): Stay sequence anomalies and secondary exit records
       -> duplicate_exit_for_stay=50 (excluded from stays_clean_view=18428/18428), overlap_with_prior_stay=1, reintake_after_open_stay=12
[WARN] WARN-4 (WARN): Cross-field clinical conflicts (euthanasia_reason & spayed_neutered)
       -> conflict_non_euthanasia_status=99 (nulled), missing_euthanized_no_reason=20, conflict_previously_spayed=1 (nulled)
------------------------------------------------------------------------
SUMMARY: 7 checks run | PASS=3 | WARN=4 | STOP=0 | EXIT_CODE=0
========================================================================
```

**Classification of each `WARN` (World vs Measurement vs Pipeline):**
- **`WARN-1` (Missing `date_of_birth` in non-companion species) — Measurement:** Wild animals, birds, and reptiles have a true biological birth date in the physical world, but shelter intake staff cannot observe or measure it without prior owner records (`MAR`), whereas `Cat` (`2.6%`) and `Dog` (`2.8%`) are well below the `10%` threshold.
- **`WARN-2` (Zero intakes in final 7 days for `Livestock` and `Other`) — World:** Only `5` livestock and `6` `'Other'` animals arrived across the entire year; having zero arrivals in the final week (`2026-10-01` to `2026-10-08`) reflects genuine rarity in the real world rather than an ingestion outage (`Cat`, `Dog`, `Wildlife`, `Small Mammal`, `Bird`, and `Reptile` all recorded intakes during the final week).
- **`WARN-3` (`50` duplicate exit records and `13` sequence anomalies) — Measurement:** Shelter staff occasionally log a second disposal/adoption timestamp against an existing intake or admit a returning animal before closing its prior foster/transfer stay in the source database; our pipeline isolates the `50` secondary outcomes via `duplicate_exit_for_stay` so `stays_clean_view` has exact 1-to-1 parity (`18,428` rows) with `intakes_clean`.
- **`WARN-4` (`99` non-euthanasia outcomes with `euthanasia_reason`, `20` euthanized without reason, `1` spay conflict) — Measurement:** Frontline staff used the euthanasia reason field to record cause of death (`Suffering`, `Injured`, `Rabies Risk`) on `DOA` and `Unassisted Death` records in the source software; our pipeline nulls out the conflicting cleaned values while retaining `<col>_raw`.

**Proving every `STOP` check fails when broken (`python src/validate.py --test-stops`):**
`src/validate.py` includes a self-test routine (`--test-stops`) that copies `data/austin_animals.db` into a `tempfile.TemporaryDirectory()` and breaks each `STOP` invariant on an isolated copy:
1. **`STOP-1` test:** Deletes 1 row from `outcomes_clean` on `break_stop1.db` -> triggers `STOP-1` (`outcomes (17299) != outcomes_clean (17298)`) and returns `exit_code = 1`.
2. **`STOP-2` test:** Updates `animals_clean.date_of_birth = '2099-01-01'` for `animal_key = 1` on `break_stop2.db` -> triggers `STOP-2` (`future_dob=1, dob_after_intake=1`) and returns `exit_code = 1`.
3. **`STOP-3` test:** Creates a non-`STRICT` table `rogue_non_strict` and breaks `animals_clean.animal_key` referential integrity on `break_stop3.db` -> triggers `STOP-3` (`foreign_key_check returned 1 violation(s); non-STRICT tables found: ['rogue_non_strict']`) and returns `exit_code = 1`.

---

## 8. What we can't say

Even after building the cleaned layer, four structural limitations remain that Milestone M4 must respect:

1. **Overall shelter morbidity cannot be estimated from `intake_health`:** Because `intake_health_condition` is unlogged (`missing_not_logged`) for `83.39%` of intakes and is preferentially recorded when an animal is visibly injured, sick, or neonatal (**MNAR**), M4 cannot claim that `83.39%` of arrivals are healthy or compute population-wide disease prevalence. M4 must treat `intake_health IS NOT NULL` as a clinically triaged sub-cohort.
2. **Age-dependent comparisons are restricted to `Cat` and `Dog`:** Because `75.6%` of `Wildlife`, `66.7%` of `Bird`, and `63.6%` of `Reptile` records have `date_of_birth IS NULL` (`life_stage = 'unknown'`), M4 cannot compare juvenile vs adult outcomes for non-companion taxa.
3. **Sub-zip spatial granularity is unavailable:** While `86.05%` (`15,857 / 18,428`) of intakes resolve to a valid 5-digit Austin metro ZIP code, `2,447` intakes recorded only `'TX, United States'` and `109` recorded street addresses without a postal code (`invalid_house_number_as_zip`). We chose not to geocode raw street strings via external APIs in M3 to keep `clean.py` deterministic and offline.
4. **Right-censoring of recent intakes and quarantined orphan outcomes:** Exactly `1,179` intake stays have no outcome as of the snapshot cutoff (`2026-10-08`) because those animals were still in shelter care (`1,167` open current stays plus `12` open prior stays), and `1,088` early October 2025 outcome rows remain in `quarantine` because their intakes occurred prior to the M1 snapshot window. M4 length-of-stay analyses must account for right-censoring in late September / October 2026 cohorts and run sensitivity checks both with and without the `692` `unusual_high_stay` records.
