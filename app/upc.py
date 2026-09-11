"""UPC normalization shared by every import path.

Excel round-trips (and some POS exports) mangle UPCs: leading zeros get
stripped when a column is stored as a number, and the same physical code
can show up as "12345678901", "012345678901", or "12345678901.0". Matching
must happen on a single canonical key regardless of the original formatting.
"""
import re


def normalize_upc(raw) -> str:
    """Canonical match key: digits only, leading zeros stripped, no trailing
    ".0" from float-coerced Excel cells. Returns '' for blank/unusable input."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s == "" or s.lower() == "nan":
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    digits = re.sub(r"\D", "", s)
    digits = digits.lstrip("0")
    return digits


def pad_upc(raw, width: int = 12) -> str:
    """Display form: zero-padded to `width` digits (12 for UPC-A, 13 for EAN-13)."""
    normalized = normalize_upc(raw)
    if not normalized:
        return ""
    return normalized.zfill(width)
