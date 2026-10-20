"""
Validation and normalisation helpers for the M2 loader.

Only what loading requires is transformed here: types, timestamps and keys.
Cleaning decisions (outliers, category merging, ...) belong to M3.
"""

import re
from collections import Counter
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# The Austin portal publishes local wall-clock time without an offset.
SOURCE_TZ = ZoneInfo("America/Chicago")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

RAW_FILENAME_TS = re.compile(r"^(\d{8}T\d{6}Z)")
ZIP_CODE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

TRUE_VALUES = {"true", "yes", "y", "1"}
FALSE_VALUES = {"false", "no", "n", "0"}


class CleanLog:
    """Counts every value the loader had to change, per field and reason."""

    def __init__(self):
        self.counts = Counter()

    def add(self, field: str, reason: str) -> None:
        self.counts[(field, reason)] += 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def describe(self) -> str:
        return ", ".join(f"{field}: {n} {reason}" for (field, reason), n in sorted(self.counts.items()))


def retrieved_at_from_filename(file_name: str):
    """'20261008T213334Z.json' -> '2026-10-08T21:33:34Z' (None if the name has no timestamp)."""
    match = RAW_FILENAME_TS.match(file_name)
    if not match:
        return None
    parsed = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    return parsed.strftime(UTC_FORMAT)


def to_utc_timestamp(value):
    """
    Source timestamp -> ISO 8601 UTC text, or None if missing/unparseable.
    Values without an offset are Austin local time and are converted explicitly.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SOURCE_TZ)
    return parsed.astimezone(timezone.utc).strftime(UTC_FORMAT)


def clean_text(value):
    """Strips text; empty text becomes None (a missing value, not a cleaned one)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_date(value, field: str, log: CleanLog):
    """Calendar date (date of birth) -> 'YYYY-MM-DD'. Unparseable -> None, counted."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            pass
    log.add(field, "unparseable date -> NULL")
    return None


def to_int(value, field: str, log: CleanLog, minimum=None):
    """Whole number (sent as text by the API). Non-numeric or below minimum -> None, counted."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        log.add(field, "non-numeric -> NULL")
        return None
    if minimum is not None and number < minimum:
        log.add(field, f"below {minimum} -> NULL")
        return None
    return number


def to_bool_int(value, field: str, log: CleanLog):
    """true/false (JSON or text, any casing) -> 1/0. Anything else -> None, counted."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return 1
    if text in FALSE_VALUES:
        return 0
    if text:
        log.add(field, "not a boolean -> NULL")
    return None


def zip_from_address(value):
    """Keeps only the 5-digit ZIP code of a free-text address (no street-level data stored)."""
    if not isinstance(value, str):
        return None
    matches = ZIP_CODE.findall(value)
    return matches[-1] if matches else None
