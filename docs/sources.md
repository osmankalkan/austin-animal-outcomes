# Data Source Cards

This document records the provenance, licensing, access mechanisms, and known data quirks for all approved data sources utilized in the project, following the Milestone M1 data engineering standards.

---

## 1. Austin Animal Center Intakes

```yaml
source_name: Austin Animal Center Intakes
provider: City of Austin Open Data Portal (data.austintexas.gov)
url: https://data.austintexas.gov/resource/pyqf-r2dc.json
access_method: REST API (Socrata SODA2), public access, optional app token
licence: City of Austin Open Data Terms of Use / Public Domain
terms_notes: Data is published for public access and analysis. No redistribution restrictions apply. Attribution to City of Austin Open Data Portal is recommended.
update_cadence: hourly
coverage: 4 May 2025 to present (live system dataset; historical frozen 2013–2025 dataset is excluded)
record_meaning: one row = one intake of one animal into the shelter
join_key: animal_id + source_date (joins to Outcomes: animal_id + intake_date)
first_retrieved: 2026-10-08T19:00:00Z
known_issues: SODA API returns 1,000 rows per request by default ($limit/$offset pagination required). found_address is free text containing street-level addresses (will be aggregated or excluded). Inconsistent casing in boolean/categorical fields (e.g. 'Yes' vs 'yes' in spayed/neutered), and wildlife entries (e.g. raccoons) need future filtering.
```

### Detailed Field Metadata (Intakes)
- `id`: Unique record identifier for the intake event (text)
- `animal_id`: Identifier for the animal across shelter events (text)
- `source_date`: Intake event datetime (datetime)
- `timestamp`: Record creation timestamp in system (datetime)
- `type`: Animal type (e.g., Dog, Cat, Other, Wildlife)
- `source_name`: Intake source / intake type (e.g., Stray, Owner Surrender, Public Assist)
- `sex`: Sex and intactness indicator (text)
- `primary_breed`: Primary breed name (text)
- `primary_color`: Primary coat color (text)
- `secondary_color`: Secondary coat color (text)
- `ispreviouslyspayedneutered`: Spay/neuter status prior to intake (boolean)
- `found_address`: Free text location where animal was retrieved (text)

---

## 2. Austin Animal Center Outcomes

```yaml
source_name: Austin Animal Center Outcomes
provider: City of Austin Open Data Portal (data.austintexas.gov)
url: https://data.austintexas.gov/resource/gsvs-ypi7.json
access_method: REST API (Socrata SODA2), public access, optional app token
licence: City of Austin Open Data Terms of Use / Public Domain
terms_notes: Data is published for public access and analysis. No redistribution restrictions apply. Attribution to City of Austin Open Data Portal is recommended.
update_cadence: hourly
coverage: 5 May 2025 to present (live system dataset; historical frozen 2013–2025 dataset is excluded)
record_meaning: one row = one exit of one animal from the shelter (e.g. adoption, transfer, return to owner, euthanasia)
join_key: animal_id + intake_date (joins to Intakes: animal_id + source_date)
first_retrieved: 2026-10-08T19:00:00Z
known_issues: Animals admitted before the May 2025 system cutover (stays >700 days) lack matching Intakes rows. Animals currently in the shelter do not yet have an outcome record. date_of_birth is occasionally null. Inconsistent type labels (e.g. 'Kitten' sometimes listed separately from 'Cat').
```

### Detailed Field Metadata (Outcomes)
- `id`: Unique record identifier for the outcome event (text)
- `animal_id`: Identifier for the animal (text)
- `name`: Given name of animal if available (text)
- `outcome_date`: Exit event datetime (datetime)
- `intake_date`: Admitted datetime for this stay (datetime)
- `date_of_birth`: Estimated or known birth date (datetime)
- `timestamp`: System record creation timestamp (datetime)
- `days_in_shelter`: Total duration of stay in days (integer)
- `outcome_status`: Outcome category (e.g., Adopted, Transfer Out, Reclaimed, Euthanized, Return to Habitat)
- `euthanasia_reason`: Justification if euthanized (text)
- `type`: Animal species/type (text)
- `sex`: Sex status at exit (text)
- `spayed_neutered`: Current spay/neuter status (text)
- `primary_breed`: Primary breed (text)
- `primary_color`: Primary coat color (text)
- `secondary_color`: Secondary coat color (text)

---

## 3. Optional Secondary Source: Open-Meteo Weather Data

```yaml
source_name: Open-Meteo Historical Weather API
provider: Open-Meteo (open-meteo.com)
url: https://archive-api.open-meteo.com/v1/archive
access_method: Public REST API, no authentication required for non-commercial use
licence: Open Database License (ODbL) / CC BY 4.0 for weather models
terms_notes: Free for non-commercial open data research with attribution to Open-Meteo.
update_cadence: daily
coverage: 2025-05-01 to present, Austin TX coordinates (30.2672° N, 97.7431° W)
record_meaning: one row = daily aggregated weather metrics (temperature, precipitation) in Austin
join_key: date (joins to Intakes/Outcomes on date part of source_date/outcome_date)
first_retrieved: 2026-10-08T19:00:00Z
known_issues: Secondary optional join candidate proposed in M0 to examine seasonal and weather correlations with shelter intake surges.
```
