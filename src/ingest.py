import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

# Ensure the repository root is in sys.path when invoked via `python src/ingest.py`
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.client import SodaClient
from src.storage import save_raw_json

load_dotenv()


def run_ingestion():
    intakes_url = os.getenv("INTAKES_API_URL", "https://data.austintexas.gov/resource/pyqf-r2dc.json")
    outcomes_url = os.getenv("OUTCOMES_API_URL", "https://data.austintexas.gov/resource/gsvs-ypi7.json")
    raw_data_dir = os.getenv("RAW_DATA_DIR", "data/raw")
    client = SodaClient()

    sources = [
        {"name": "intakes", "url": intakes_url},
        {"name": "outcomes", "url": outcomes_url}
    ]
    print("=" * 60)
    print("🚀 Austin Animal Center - Milestone M1 Data Ingestion Pipeline")
    print("=" * 60)
    summary = []
    has_failure = False
    start_time = time.time()
    for src in sources:
        print(f"\n[INGEST] Fetching raw records for: {src['name']}...")
        try:
            records = client.fetch_all(src["url"])
            if not records:
                raise ValueError(f"Empty response received from {src['url']}; refusing to silently save empty dataset.")
            saved_path = save_raw_json(records, src["name"], base_dir=raw_data_dir)
            summary.append({
                "source": src["name"],
                "status": "SUCCESS",
                "records": len(records),
                "path": saved_path,
                "error": None
            })
            print(f"✅ Successfully landed {len(records)} records -> {saved_path}")
        except Exception as e:
            has_failure = True
            summary.append({
                "source": src["name"],
                "status": "FAILED",
                "records": 0,
                "path": None,
                "error": str(e)
            })
            print(f"❌ Failed to fetch {src['name']}: {e}")
    elapsed_time = round(time.time() - start_time, 2)
    # Section 3.3 Run Summary
    print("\n" + "=" * 60)
    print("📊 INGESTION RUN SUMMARY")
    print("=" * 60)
    for item in summary:
        print(f"- Source: {item['source']}")
        print(f"  Status: {item['status']}")
        print(f"  Records Landed: {item['records']}")
        print(f"  Saved Path: {item['path']}")
        if item["error"]:
            print(f"  Error: {item['error']}")
    print("-" * 60)
    print(f"Total Duration: {elapsed_time}s")
    print(f"Overall Run Health: {'HEALTHY ✅' if not has_failure else 'DEGRADED / FAILED ❌'}")
    print("=" * 60)
    if has_failure:
        sys.exit(1)


if __name__ == "__main__":
    run_ingestion()
