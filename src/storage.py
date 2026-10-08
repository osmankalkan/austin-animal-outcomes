import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Dict


def get_utc_timestamp() -> str:
    """Returns current UTC timestamp formatted as YYYYMMDDTHHMMSSZ."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_raw_json(data: List[Dict[str, Any]], source_name: str, base_dir: str = "data/raw") -> str:
    """
    Saves raw API response data as JSON without modification.
    Creates a new file with UTC timestamp, guaranteeing no overwrite.
    """
    target_dir = Path(base_dir) / source_name
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = get_utc_timestamp()
    filename = f"{timestamp}.json"
    file_path = target_dir / filename

    # If called multiple times within the same second, ensure no overwrite
    counter = 1
    while file_path.exists():
        filename = f"{timestamp}_{counter}.json"
        file_path = target_dir / filename
        counter += 1

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return str(file_path)
