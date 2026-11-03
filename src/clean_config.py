"""
Thresholds and constants for the M3 cleaning and validation pipeline.

Every threshold and constant lives here, with a name and a comment
explaining why it has this value (see docs/cleaning_report.md Section 5).
"""

# 1. Life-stage boundary (days of age at first intake)
# Standard veterinary kitten/puppy threshold is 6 months (~180 days);
# 99.9% of rows raw-labelled Kitten/Puppy have age <= 180 days (median 35d / 46d).
JUVENILE_MAX_AGE_DAYS = 180

# Hard upper bound for Kitten/Puppy raw label contradiction check:
# any animal raw-labelled Kitten/Puppy with age > 365 days at intake is a label conflict.
JUVENILE_CONFLICT_AGE_DAYS = 365

# Youngest age (days) at which a Cat/Dog raw label conflicts with a neonate/juvenile age (< 8 weeks).
ADULT_LABEL_MIN_AGE_DAYS = 56

# 2. Maximum plausible calendar date for date_of_birth (snapshot retrieval date).
MAX_VALID_DATE = "2026-10-08"

# 3. Robust outlier fence multiplier on length of stay (Q3 + IQR_FENCE_MULTIPLIER * IQR, per species).
# Tukey's outer fence (3.0 * IQR) handles right-skewed shelter stays without flagging routine 1-3 month stays;
# flags long-stay animals (~3.9% of outcomes) as unusual_high_stay while keeping their values intact.
IQR_FENCE_MULTIPLIER = 3.0

# Minimum completed outcomes in a canonical species group to compute its own IQR fence;
# smaller groups fall back to the overall median/IQR fence.
MIN_GROUP_SIZE_FOR_IQR = 30

# 4. Geographic ZIP prefixes for Texas & Austin metro service area.
# Valid Texas ZIP codes start with 73, 75, 76, 77, 78, or 79 (733xx is Austin IRS, 786xx/787xx is Austin metro).
# Any 5-digit token not starting with a Texas prefix came from a street house number when ZIP was omitted.
TEXAS_ZIP_PREFIXES = ("73", "75", "76", "77", "78", "79")
AUSTIN_METRO_ZIP_PREFIXES = ("786", "787")

# 5. Placeholder / non-name tokens in name_at_intake and outcomes.name (case-folded).
PLACEHOLDER_NAMES = frozenset({
    "unknown", "no name", "noname", "none", "n/a", "na", "unk", "tbd", "?", "*",
    "kitten", "kittens", "puppy", "puppies", "cat", "dog", "bat", "baby",
})

# 6. Health condition label standardisation (merging uppercase variants into canonical labels).
HEALTH_STANDARD_MAP = {
    "Normal": "Normal",
    "Injured": "Injured",
    "Sick": "Sick",
    "Dead": "Dead",
    "Neonate": "Neonate",
    "NEONATAL": "Neonate",
    "MED ATTN": "Medical",
    "MEDICAL": "Medical",
}

# 7. Validation thresholds (used by src/validate.py, justified in docs/cleaning_report.md Section 5 & 7).
# Warn if date_of_birth missing share exceeds 10% in core companion species (Cat, Dog).
MAX_CORE_DOB_MISSING_SHARE = 0.10

# Warn if any canonical species group has > 20% missing date_of_birth overall.
MAX_OVERALL_DOB_MISSING_SHARE = 0.20

# Freshness window (days before latest snapshot date 2026-10-08) within which each core species must appear.
FRESHNESS_WINDOW_DAYS = 7

# Warn if duplicate-exit or sequence-overlap rows exceed 0 rows.
MAX_EXPECTED_DUPLICATE_EXITS = 0
