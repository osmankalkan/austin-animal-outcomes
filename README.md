# Austin Animal Center — Ingestion Pipeline & Repository

## 1. Project Title
**Predicting Animal Shelter Length of Stay and Outcome Status (Austin Animal Center Pipeline)**

## 2. The Question (Project Intent)
This data lets us explain and predict how an animal's species, breed, sex, spay/neuter status, age (from date of birth) and intake source affect how long it stays in the shelter and how it leaves (adoption, transfer, return to owner, euthanasia). We will build a regression model for length of stay and a classification model for the outcome type. Monthly intake volume shows a clear seasonal pattern, so we will also examine seasonal effects. The results can help a shelter decide which animal profiles need earlier and stronger promotion.

## 3. Data Sources
- **Austin Animal Center Intakes**: Live intake events from the City of Austin Open Data Portal. Detailed provenance and field definitions: [docs/sources.md#1-austin-animal-center-intakes](docs/sources.md#1-austin-animal-center-intakes).
- **Austin Animal Center Outcomes**: Live outcome exits from the City of Austin Open Data Portal. Detailed provenance and field definitions: [docs/sources.md#2-austin-animal-center-outcomes](docs/sources.md#2-austin-animal-center-outcomes).
- *Attribution*: Public datasets published by the City of Austin Open Data Portal (data.austintexas.gov) under Public Domain / City of Austin Open Data Terms of Use.

## 4. How to Run It
Follow these steps from a fresh clone:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/osmankalkan/austin-animal-outcomes.git
   cd austin-animal-outcomes
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Optionally edit .env to provide your AUSTIN_APP_TOKEN
   ```

5. **Run the ingestion script:**
   ```bash
   python src/ingest.py
   ```
   The script retrieves both Intakes and Outcomes, lands unmodified raw JSON files under `data/raw/<source>/<TIMESTAMP>.json`, and prints a run health summary.

## 5. Repository Structure
```text
austin-animal-outcomes/
├── README.md               # Project documentation and execution instructions
├── .gitignore              # Ignores secrets (.env), virtualenvs, and raw data dumps
├── .env.example            # Template for environment configuration & API credentials
├── requirements.txt        # Exact Python dependencies
├── data/
│   └── raw/                # Landed raw JSON responses (organized by source, timestamped)
│       ├── intakes/        # Raw intake snapshots
│       └── outcomes/       # Raw outcome snapshots
├── src/
│   ├── client.py           # Robust SODA API client (retry backoff, timeouts, pagination)
│   ├── storage.py          # UTC timestamped raw file landing mechanism
│   └── ingest.py           # Single-command CLI runner and run summary reporter
└── docs/
    └── sources.md          # Comprehensive Data Source provenance cards
```

## 6. Status
M1 — ingestion complete

## 7. Team
- Ferdi Derebaşı (Student ID: 230717056) — GitHub: [@Ferdi-krbk](https://github.com/Ferdi-krbk)
- Irmak Köse (Student ID: 230717027) — GitHub: [@irmak23](https://github.com/irmak23)
- Osman Kalkan (Student ID: 230717048) — GitHub: [@osmankalkan](https://github.com/osmankalkan)
