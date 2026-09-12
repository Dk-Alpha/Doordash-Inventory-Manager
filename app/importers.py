"""CSV/XLSX ingestion: encoding/delimiter sniffing, fuzzy header detection,
and a stable "fingerprint" of a header row used to key remembered column
mapping presets.
"""
import csv
import hashlib
import json
import os
import re
import warnings

import pandas as pd

# field -> alias substrings checked against a lowercased, de-punctuated header.
# Order matters: more specific aliases should be listed before generic ones
# within a field, but fields themselves are tried in this order too, so a
# header like "upc_id" doesn't get claimed by a looser field first.
MASTER_FIELD_ALIASES = {
    "upc_id": ["upc_id", "upc code", "upc", "barcode", "gtin"],
    "sku_id": ["sku_id", "sku"],
    "item_name": ["item_name", "product name", "item name", "name", "title", "description"],
    "category_l1": ["category_l1", "category 1", "l1 category", "category"],
    "category_l2": ["category_l2", "category 2", "l2 category", "subcategory"],
    "default_price": ["default_price", "price", "list price", "sale price", "cost"],
    "status": ["status", "is_active", "active"],
    "currency": ["currency", "curr"],
    "business_id": ["business_id", "business id"],
    "store_id": ["store_id", "store id"],
}

WORKLIST_FIELD_ALIASES = {
    "upc_id": MASTER_FIELD_ALIASES["upc_id"],
    "item_name": MASTER_FIELD_ALIASES["item_name"],
    "new_price": ["new_price", "new price", "updated price", "price"],
    "new_status": ["new_status", "new status", "updated status", "status"],
    "category_l1": MASTER_FIELD_ALIASES["category_l1"],
    "category_l2": MASTER_FIELD_ALIASES["category_l2"],
}

REQUIRED_MASTER_FIELDS = ["upc_id"]
REQUIRED_WORKLIST_FIELDS = ["upc_id"]

MAX_HEADER_SCAN_ROWS = 5


def _clean(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(header).lower()).strip()


def _build_alias_clean_set() -> set:
    cleaned = set()
    for aliases_dict in (MASTER_FIELD_ALIASES, WORKLIST_FIELD_ALIASES):
        for alias_list in aliases_dict.values():
            for alias in alias_list:
                cleaned.add(_clean(alias))
    return cleaned


_ALL_ALIASES_CLEAN = _build_alias_clean_set()


def _count_exact_alias_matches(cells: list) -> int:
    """Strict (exact, post-cleaning) match count -- used only to pick which
    row is the real header among several candidates, where the loose
    substring matching in detect_columns would be too eager to false-positive
    on ordinary data values."""
    return sum(1 for c in cells if _clean(c) and _clean(c) in _ALL_ALIASES_CLEAN)


def detect_columns(headers: list[str], kind: str = "master") -> dict[str, str]:
    """Return {field: original_header} best-guess mapping. A field is
    omitted if no header matches it — the caller / UI must let the user
    fill that in manually."""
    aliases = MASTER_FIELD_ALIASES if kind == "master" else WORKLIST_FIELD_ALIASES
    cleaned = {h: _clean(h) for h in headers}
    mapping: dict[str, str] = {}
    claimed: set[str] = set()

    for field, alias_list in aliases.items():
        best_header = None
        for alias in alias_list:
            for header, clean_header in cleaned.items():
                if header in claimed:
                    continue
                if clean_header == alias or alias in clean_header.split():
                    best_header = header
                    break
                if best_header is None and alias.replace(" ", "") in clean_header.replace(" ", ""):
                    best_header = header
            if best_header:
                break
        if best_header:
            mapping[field] = best_header
            claimed.add(best_header)

    return mapping


def header_fingerprint(headers: list[str]) -> str:
    """Stable key for a header row, used to look up a remembered mapping
    preset regardless of row order in the source file."""
    normalized = sorted(_clean(h) for h in headers)
    return hashlib.sha256(json.dumps(normalized).encode("utf-8")).hexdigest()


def sniff_csv_dialect(path: str, sample_size: int = 8192) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(sample_size)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def read_table(path: str) -> pd.DataFrame:
    """Read CSV or XLSX as all-string columns (so UPCs/prices aren't
    silently coerced), with a UTF-8 -> latin-1 fallback for CSV encoding
    issues (spec section 5)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str)

    last_err = None
    for encoding in ("utf-8", "latin-1"):
        try:
            return _read_csv_best_effort(path, encoding)
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise last_err


def _pick_delimiter(path: str, encoding: str) -> str:
    """Comma is by far the most common export delimiter, so try it first
    rather than trusting csv.Sniffer blindly: real inventory data often
    contains literal '|', ';', or other punctuation inside UPC/SKU cells
    (some exports wrap values as "|012345|" to force text formatting),
    which can fool the sniffer into picking that punctuation as the
    delimiter instead of the real one -- producing a baffling "expected 1
    field, found 5" error on a data row while the header parses fine."""
    try:
        probe = pd.read_csv(path, dtype=str, sep=",", encoding=encoding, header=None, nrows=20)
        if probe.shape[1] > 1:
            return ","
    except pd.errors.ParserError:
        pass
    return sniff_csv_dialect(path)


def _best_header_row_index(rows: list[list[str]]) -> int:
    """Given raw preview rows (no header interpretation), guess which one
    looks most like real field names, by counting exact alias matches.
    Falls back to row 0 (the normal case) when nothing scores higher."""
    if not rows:
        return 0
    best_idx = 0
    best_score = _count_exact_alias_matches([str(v) for v in rows[0]])
    for i in range(1, len(rows)):
        score = _count_exact_alias_matches([str(v) for v in rows[i]])
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _detect_header_row(path: str, delimiter: str, encoding: str) -> int:
    """Some exports (Google-Sheets-style, with a frozen instructions row like
    "Read only" / "Can update" above the real headers) don't put column names
    on row 1. Scan the first few rows and use whichever one looks most like
    real field names, falling back to row 0 (the normal case)."""
    try:
        preview = pd.read_csv(
            path, sep=delimiter, encoding=encoding, header=None,
            nrows=MAX_HEADER_SCAN_ROWS, dtype=str, engine="python", on_bad_lines="skip",
        )
    except Exception:
        return 0
    if len(preview) == 0:
        return 0
    return _best_header_row_index(preview.fillna("").values.tolist())


def preview_rows(path: str, max_rows: int = 25) -> list[list[str]]:
    """Read the first max_rows rows of a CSV/XLSX with no header
    interpretation at all, for a "which row are the column names on?" UI
    preview. Column names are not assumed to live in row 1 or 2 -- the
    caller shows these raw rows and lets the user point at (or confirm an
    auto-detected guess of) the real header row."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, header=None, dtype=str, nrows=max_rows)
        return df.fillna("").astype(str).values.tolist()

    last_err = None
    for encoding in ("utf-8", "latin-1"):
        try:
            delimiter = _pick_delimiter(path, encoding)
            df = pd.read_csv(
                path, sep=delimiter, encoding=encoding, header=None, dtype=str,
                nrows=max_rows, engine="python", on_bad_lines="skip",
            )
            return df.fillna("").astype(str).values.tolist()
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise last_err


def guess_header_row(rows: list[list[str]]) -> int:
    """Best-guess index (0-based) of which of `rows` (as returned by
    preview_rows) looks most like the real column-name row."""
    return _best_header_row_index(rows[:MAX_HEADER_SCAN_ROWS])


def read_table_with_header_row(path: str, header_row: int) -> pd.DataFrame:
    """Read CSV/XLSX as all-string columns using a caller-chosen (0-based)
    header row index, e.g. one confirmed by the user in the import UI
    rather than auto-detected."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str, header=header_row)
        if header_row > 0:
            df.attrs["header_row_skipped"] = header_row
        return df

    last_err = None
    for encoding in ("utf-8", "latin-1"):
        try:
            return _read_csv_at_header_row(path, encoding, header_row)
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise last_err


def _read_csv_at_header_row(path: str, encoding: str, header_row: int) -> pd.DataFrame:
    delimiter = _pick_delimiter(path, encoding)
    try:
        df = pd.read_csv(path, dtype=str, sep=delimiter, encoding=encoding, header=header_row)
    except pd.errors.ParserError:
        # A genuinely malformed row (e.g. an unescaped comma inside an
        # unquoted text field) -- don't crash the whole import over it;
        # skip just that row and surface which ones were dropped.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            df = pd.read_csv(
                path, dtype=str, sep=delimiter, encoding=encoding, header=header_row,
                engine="python", on_bad_lines="warn",
            )
        skipped = [str(w.message) for w in caught if "Skipping line" in str(w.message)]
        if skipped:
            df.attrs["skipped_lines"] = skipped

    if header_row > 0:
        df.attrs["header_row_skipped"] = header_row
    return df


def _read_csv_best_effort(path: str, encoding: str) -> pd.DataFrame:
    delimiter = _pick_delimiter(path, encoding)
    header_row = _detect_header_row(path, delimiter, encoding)
    return _read_csv_at_header_row(path, encoding, header_row)
