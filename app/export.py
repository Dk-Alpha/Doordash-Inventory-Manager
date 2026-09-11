"""CSV exports in DoorDash's exact template column order, with a timestamped
filename so an export never silently overwrites a previous one."""
import csv
import sqlite3
from datetime import datetime
from pathlib import Path

DOORDASH_COLUMNS = [
    "business_id", "store_id", "upc_id", "sku_id", "item_name",
    "category_l1", "category_l2", "default_price", "status", "currency",
]


class ExportValidationError(Exception):
    def __init__(self, message: str, invalid_rows: list):
        super().__init__(message)
        self.invalid_rows = invalid_rows


def timestamped_path(base_path: str) -> str:
    p = Path(base_path)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return str(p.with_name(f"{p.stem}_{stamp}{p.suffix or '.csv'}"))


def _row_to_export_dict(row: sqlite3.Row, store: sqlite3.Row) -> dict:
    return {
        "business_id": store["business_id"],
        "store_id": store["store_id"],
        "upc_id": row["upc_padded"] or row["upc_normalized"],
        "sku_id": row["sku_id"] or "",
        "item_name": row["item_name"] or "",
        "category_l1": row["category_l1"] or "",
        "category_l2": row["category_l2"] or "",
        "default_price": row["default_price"] if row["default_price"] is not None else "",
        "status": row["status"],
        "currency": row["currency"] or store["currency"],
    }


def _write_csv(path: str, rows: list[dict]) -> str:
    out_path = timestamped_path(path)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DOORDASH_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return out_path


def export_updated_inventory(conn: sqlite3.Connection, store_pk: int, path: str) -> str:
    store = conn.execute("SELECT * FROM stores WHERE id = ?", (store_pk,)).fetchone()
    items = conn.execute(
        "SELECT * FROM inventory_items WHERE store_id = ? ORDER BY item_name COLLATE NOCASE", (store_pk,)
    ).fetchall()
    rows = [_row_to_export_dict(item, store) for item in items]
    return _write_csv(path, rows)


def export_new_skus(conn: sqlite3.Connection, store_pk: int, path: str) -> str:
    from app.repo import REQUIRED_NEW_SKU_FIELDS

    store = conn.execute("SELECT * FROM stores WHERE id = ?", (store_pk,)).fetchone()
    items = conn.execute(
        "SELECT * FROM inventory_items WHERE store_id = ? AND match_status = 'new' ORDER BY item_name COLLATE NOCASE",
        (store_pk,),
    ).fetchall()

    invalid = [item for item in items if any(item[f] in (None, "") for f in REQUIRED_NEW_SKU_FIELDS)]
    if invalid:
        names = ", ".join((row["item_name"] or row["upc_padded"] or "?") for row in invalid[:10])
        raise ExportValidationError(
            f"{len(invalid)} new item(s) are missing required fields and cannot be exported yet: {names}",
            invalid,
        )

    rows = [_row_to_export_dict(item, store) for item in items]
    return _write_csv(path, rows)


def export_summary(conn: sqlite3.Connection, store_pk: int) -> dict:
    """Pre-export summary counts for the confirmation modal."""
    activated = conn.execute(
        "SELECT COUNT(*) AS n FROM changelog c JOIN inventory_items i ON i.id = c.item_id WHERE i.store_id = ? AND c.field = 'status' AND c.new_value = 'active'",
        (store_pk,),
    ).fetchone()["n"]
    deactivated = conn.execute(
        "SELECT COUNT(*) AS n FROM changelog c JOIN inventory_items i ON i.id = c.item_id WHERE i.store_id = ? AND c.field = 'status' AND c.new_value = 'inactive'",
        (store_pk,),
    ).fetchone()["n"]
    price_changes = conn.execute(
        "SELECT COUNT(DISTINCT c.item_id) AS n FROM changelog c JOIN inventory_items i ON i.id = c.item_id WHERE i.store_id = ? AND c.field = 'default_price'",
        (store_pk,),
    ).fetchone()["n"]
    new_skus = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory_items WHERE store_id = ? AND match_status = 'new'", (store_pk,)
    ).fetchone()["n"]
    return {
        "activated": activated, "deactivated": deactivated,
        "price_changes": price_changes, "new_skus": new_skus,
    }
