"""Data-access layer: everything that reads/writes inventory_items and its
supporting tables. UI code should never write raw SQL directly — go through
here so the changelog/undo/change_flag bookkeeping stays consistent.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.matching import match_worklist
from app.upc import normalize_upc, pad_upc

VALID_STATUSES = {"active", "inactive"}

STATUS_ALIASES = {
    "active": "active", "1": "active", "true": "active", "yes": "active", "enabled": "active",
    "inactive": "inactive", "0": "inactive", "false": "inactive", "no": "inactive", "disabled": "inactive",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_batch_action_id() -> str:
    return uuid.uuid4().hex


def normalize_status(value) -> str:
    if value is None:
        return "active"
    s = str(value).strip().lower()
    return STATUS_ALIASES.get(s, "active")


def parse_price(value) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if s == "" or s.lower() == "nan":
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- stores ---

def list_stores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM stores ORDER BY display_name").fetchall()


def create_store(conn: sqlite3.Connection, business_id: str, store_id: str, display_name: str, currency: str = "USD") -> int:
    cur = conn.execute(
        "INSERT INTO stores(business_id, store_id, display_name, currency) VALUES (?, ?, ?, ?)",
        (business_id, store_id, display_name, currency),
    )
    conn.commit()
    return cur.lastrowid


def delete_store(conn: sqlite3.Connection, store_pk: int) -> None:
    conn.execute("DELETE FROM inventory_items WHERE store_id = ?", (store_pk,))
    conn.execute("DELETE FROM import_batches WHERE store_id = ?", (store_pk,))
    conn.execute("DELETE FROM stores WHERE id = ?", (store_pk,))
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# --------------------------------------------------------------- import ---

def has_unsaved_changes(conn: sqlite3.Connection, store_pk: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory_items WHERE store_id = ? AND change_flag = 1", (store_pk,)
    ).fetchone()
    return row["n"]


def import_master(conn: sqlite3.Connection, store_pk: int, rows: list[dict], filename: str, mapping: dict) -> int:
    """Upsert master inventory rows for a store, keyed by normalized UPC.
    Returns the new import_batch id."""
    import json

    batch_id = conn.execute(
        "INSERT INTO import_batches(store_id, kind, filename, imported_at, column_mapping_json) VALUES (?, 'master', ?, ?, ?)",
        (store_pk, filename, now_iso(), json.dumps(mapping)),
    ).lastrowid

    for row in rows:
        upc_norm = normalize_upc(row.get("upc_id"))
        if not upc_norm:
            continue
        existing = conn.execute(
            "SELECT id FROM inventory_items WHERE store_id = ? AND upc_normalized = ?",
            (store_pk, upc_norm),
        ).fetchone()
        price = parse_price(row.get("default_price"))
        status = normalize_status(row.get("status"))
        values = (
            row.get("sku_id"), row.get("item_name"), row.get("category_l1"), row.get("category_l2"),
            price, status, row.get("currency"), pad_upc(upc_norm), now_iso(), batch_id,
        )
        if existing:
            conn.execute(
                """UPDATE inventory_items SET
                    sku_id = ?, item_name = ?, category_l1 = ?, category_l2 = ?,
                    default_price = ?, status = ?, currency = ?, upc_padded = ?,
                    last_modified_ts = ?, import_batch_id = ?, source = 'master'
                   WHERE id = ?""",
                values + (existing["id"],),
            )
        else:
            conn.execute(
                """INSERT INTO inventory_items(
                    store_id, upc_normalized, upc_padded, sku_id, item_name, category_l1, category_l2,
                    default_price, status, currency, match_status, source, last_modified_ts,
                    change_flag, original_price, original_status, import_batch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'existing', 'master', ?, 0, ?, ?, ?)""",
                (store_pk, upc_norm, pad_upc(upc_norm), row.get("sku_id"), row.get("item_name"),
                 row.get("category_l1"), row.get("category_l2"), price, status, row.get("currency"),
                 now_iso(), price, status, batch_id),
            )
    conn.commit()
    return batch_id


def _fetch_inventory_as_dicts(conn: sqlite3.Connection, store_pk: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM inventory_items WHERE store_id = ?", (store_pk,)).fetchall()
    return [dict(r) for r in rows]


def import_worklist(conn: sqlite3.Connection, store_pk: int, rows: list[dict], filename: str, mapping: dict):
    """Match worklist rows against current inventory for this store, apply
    proposed changes to existing items, and insert unmatched rows as
    match_status='new'. Returns (batch_id, MatchResult)."""
    import json

    store = conn.execute("SELECT * FROM stores WHERE id = ?", (store_pk,)).fetchone()

    batch_id = conn.execute(
        "INSERT INTO import_batches(store_id, kind, filename, imported_at, column_mapping_json) VALUES (?, 'worklist', ?, ?, ?)",
        (store_pk, filename, now_iso(), json.dumps(mapping)),
    ).lastrowid
    batch_action_id = new_batch_action_id()

    inventory_rows = _fetch_inventory_as_dicts(conn, store_pk)
    result = match_worklist(
        inventory_rows, rows,
        inventory_upc_field="upc_normalized", worklist_upc_field="upc_id",
    )

    for matched in result.existing:
        item_id = matched.inventory_row["id"]
        current = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        changes = matched.proposed_changes
        if not changes:
            continue

        first_edit = current["change_flag"] == 0
        set_clauses = []
        params = []

        if "item_name" in changes:
            set_clauses.append("item_name = ?")
            params.append(changes["item_name"])
        if "category_l1" in changes:
            set_clauses.append("category_l1 = ?")
            params.append(changes["category_l1"])
        if "category_l2" in changes:
            set_clauses.append("category_l2 = ?")
            params.append(changes["category_l2"])
        if "new_price" in changes:
            new_price = parse_price(changes["new_price"])
            if new_price is not None:
                conn.execute(
                    "INSERT INTO changelog(item_id, field, old_value, new_value, changed_at, batch_action_id) VALUES (?, 'default_price', ?, ?, ?, ?)",
                    (item_id, current["default_price"], new_price, now_iso(), batch_action_id),
                )
                set_clauses.append("default_price = ?")
                params.append(new_price)
        if "new_status" in changes:
            new_status = normalize_status(changes["new_status"])
            conn.execute(
                "INSERT INTO changelog(item_id, field, old_value, new_value, changed_at, batch_action_id) VALUES (?, 'status', ?, ?, ?, ?)",
                (item_id, current["status"], new_status, now_iso(), batch_action_id),
            )
            set_clauses.append("status = ?")
            params.append(new_status)

        if not set_clauses:
            continue

        if first_edit:
            set_clauses += ["original_price = ?", "original_status = ?"]
            params += [current["default_price"], current["status"]]

        set_clauses += ["change_flag = 1", "last_modified_ts = ?"]
        params.append(now_iso())
        params.append(item_id)
        conn.execute(f"UPDATE inventory_items SET {', '.join(set_clauses)} WHERE id = ?", params)

    for new_row in result.new:
        wl = new_row.worklist_row
        price = parse_price(wl.get("new_price"))
        status = normalize_status(wl.get("new_status")) if wl.get("new_status") else "active"
        conn.execute(
            """INSERT INTO inventory_items(
                store_id, upc_normalized, upc_padded, sku_id, item_name, category_l1, category_l2,
                default_price, status, currency, match_status, source, last_modified_ts,
                change_flag, original_price, original_status, import_batch_id
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'new', 'worklist', ?, 0, NULL, NULL, ?)""",
            (store_pk, new_row.upc_normalized, pad_upc(new_row.upc_normalized), wl.get("item_name"),
             wl.get("category_l1"), wl.get("category_l2"), price, status,
             store["currency"] if store else "USD", now_iso(), batch_id),
        )

    conn.commit()
    return batch_id, result


# --------------------------------------------------------------- filters ---

def build_filter_query(store_pk: int, filters: dict) -> tuple[str, list]:
    """Compose the two-stage text filter plus the advanced filter panel into
    one WHERE clause, AND'd together, so bulk actions on a filtered view are
    guaranteed to only touch what's on screen.

    filters keys (all optional): status ('active'|'inactive'|'all'),
    text1, text2 (substrings; text2 narrows text1's result set),
    match_mode ('AND'|'OR', applies to multi-word tokens within one text filter),
    price_min, price_max, category_l1, changed_only (bool), match_status.
    """
    clauses = ["store_id = ?"]
    params: list = [store_pk]

    status = filters.get("status", "all")
    if status in ("active", "inactive"):
        clauses.append("status = ?")
        params.append(status)

    match_mode = filters.get("match_mode", "AND")
    for text_key in ("text1", "text2"):
        text = (filters.get(text_key) or "").strip()
        if not text:
            continue
        tokens = text.split()
        token_clauses = []
        for token in tokens:
            like = f"%{token}%"
            token_clauses.append("(item_name LIKE ? OR upc_padded LIKE ? OR sku_id LIKE ?)")
            params += [like, like, like]
        joiner = " AND " if match_mode == "AND" else " OR "
        clauses.append("(" + joiner.join(token_clauses) + ")")

    if filters.get("price_min") is not None:
        clauses.append("default_price >= ?")
        params.append(filters["price_min"])
    if filters.get("price_max") is not None:
        clauses.append("default_price <= ?")
        params.append(filters["price_max"])
    if filters.get("category_l1"):
        clauses.append("category_l1 = ?")
        params.append(filters["category_l1"])
    if filters.get("changed_only"):
        clauses.append("change_flag = 1")
    if filters.get("match_status") and filters["match_status"] != "all":
        clauses.append("match_status = ?")
        params.append(filters["match_status"])

    return " AND ".join(clauses), params


def count_items(conn: sqlite3.Connection, store_pk: int, filters: dict) -> int:
    where, params = build_filter_query(store_pk, filters)
    row = conn.execute(f"SELECT COUNT(*) AS n FROM inventory_items WHERE {where}", params).fetchone()
    return row["n"]


def list_items(conn: sqlite3.Connection, store_pk: int, filters: dict, limit: int = 200, offset: int = 0) -> list[sqlite3.Row]:
    where, params = build_filter_query(store_pk, filters)
    order = "item_name COLLATE NOCASE"
    rows = conn.execute(
        f"SELECT * FROM inventory_items WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return rows


# ---------------------------------------------------------- bulk actions ---

def apply_bulk_status(conn: sqlite3.Connection, item_ids: list[int], to_status: str) -> str:
    batch_action_id = new_batch_action_id()
    for item_id in item_ids:
        current = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if current is None or current["status"] == to_status:
            continue
        conn.execute(
            "INSERT INTO changelog(item_id, field, old_value, new_value, changed_at, batch_action_id) VALUES (?, 'status', ?, ?, ?, ?)",
            (item_id, current["status"], to_status, now_iso(), batch_action_id),
        )
        first_edit = current["change_flag"] == 0
        if first_edit:
            conn.execute(
                "UPDATE inventory_items SET status = ?, change_flag = 1, last_modified_ts = ?, original_status = ? WHERE id = ?",
                (to_status, now_iso(), current["status"], item_id),
            )
        else:
            conn.execute(
                "UPDATE inventory_items SET status = ?, change_flag = 1, last_modified_ts = ? WHERE id = ?",
                (to_status, now_iso(), item_id),
            )
    conn.commit()
    return batch_action_id


def compute_new_price(current_price: Optional[float], mode: str, value: float) -> float:
    base = current_price or 0.0
    if mode == "set":
        return round(value, 2)
    if mode == "inc_pct":
        return round(base * (1 + value / 100), 2)
    if mode == "dec_pct":
        return round(base * (1 - value / 100), 2)
    if mode == "inc_amt":
        return round(base + value, 2)
    if mode == "dec_amt":
        return round(base - value, 2)
    raise ValueError(f"Unknown price mode: {mode}")


def apply_bulk_price(conn: sqlite3.Connection, item_ids: list[int], mode: str, value: float) -> str:
    batch_action_id = new_batch_action_id()
    for item_id in item_ids:
        current = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if current is None:
            continue
        new_price = compute_new_price(current["default_price"], mode, value)
        if new_price == current["default_price"]:
            continue
        conn.execute(
            "INSERT INTO changelog(item_id, field, old_value, new_value, changed_at, batch_action_id) VALUES (?, 'default_price', ?, ?, ?, ?)",
            (item_id, current["default_price"], new_price, now_iso(), batch_action_id),
        )
        first_edit = current["change_flag"] == 0
        if first_edit:
            conn.execute(
                "UPDATE inventory_items SET default_price = ?, change_flag = 1, last_modified_ts = ?, original_price = ? WHERE id = ?",
                (new_price, now_iso(), current["default_price"], item_id),
            )
        else:
            conn.execute(
                "UPDATE inventory_items SET default_price = ?, change_flag = 1, last_modified_ts = ? WHERE id = ?",
                (new_price, now_iso(), item_id),
            )
    conn.commit()
    return batch_action_id


def last_batch_action_id(conn: sqlite3.Connection, store_pk: int) -> Optional[str]:
    row = conn.execute(
        """SELECT c.batch_action_id, MAX(c.changed_at) AS latest
           FROM changelog c JOIN inventory_items i ON i.id = c.item_id
           WHERE i.store_id = ?
           GROUP BY c.batch_action_id
           ORDER BY latest DESC LIMIT 1""",
        (store_pk,),
    ).fetchone()
    return row["batch_action_id"] if row else None


def undo_batch(conn: sqlite3.Connection, batch_action_id: str) -> int:
    """Revert every changelog entry in a batch, in reverse order. Returns the
    number of field-changes undone."""
    entries = conn.execute(
        "SELECT * FROM changelog WHERE batch_action_id = ? ORDER BY id DESC", (batch_action_id,)
    ).fetchall()
    for entry in entries:
        field = entry["field"]
        if field not in ("status", "default_price"):
            continue
        conn.execute(f"UPDATE inventory_items SET {field} = ? WHERE id = ?", (entry["old_value"], entry["item_id"]))
    conn.execute("DELETE FROM changelog WHERE batch_action_id = ?", (batch_action_id,))

    touched_item_ids = {e["item_id"] for e in entries}
    for item_id in touched_item_ids:
        remaining = conn.execute("SELECT COUNT(*) AS n FROM changelog WHERE item_id = ?", (item_id,)).fetchone()["n"]
        if remaining == 0:
            conn.execute(
                "UPDATE inventory_items SET change_flag = 0, original_price = NULL, original_status = NULL WHERE id = ?",
                (item_id,),
            )
    conn.commit()
    return len(entries)


# -------------------------------------------------------------- new items --

REQUIRED_NEW_SKU_FIELDS = ["item_name", "category_l1", "category_l2", "default_price", "status", "currency"]


def update_new_item_fields(conn: sqlite3.Connection, item_id: int, fields: dict) -> None:
    allowed = {"item_name", "category_l1", "category_l2", "default_price", "status", "currency", "sku_id"}
    set_clauses = []
    params = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        set_clauses.append(f"{k} = ?")
        params.append(v)
    if not set_clauses:
        return
    set_clauses.append("last_modified_ts = ?")
    params.append(now_iso())
    params.append(item_id)
    conn.execute(f"UPDATE inventory_items SET {', '.join(set_clauses)} WHERE id = ?", params)
    conn.commit()


def new_items_missing_required(conn: sqlite3.Connection, store_pk: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM inventory_items WHERE store_id = ? AND match_status = 'new'", (store_pk,)
    ).fetchall()
    missing = []
    for r in rows:
        if any(r[f] in (None, "") for f in REQUIRED_NEW_SKU_FIELDS):
            missing.append(r)
    return missing


# --------------------------------------------------------------- dashboard --

def dashboard_stats(conn: sqlite3.Connection, store_pk: int) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM inventory_items WHERE store_id = ?", (store_pk,)).fetchone()["n"]
    active = conn.execute("SELECT COUNT(*) AS n FROM inventory_items WHERE store_id = ? AND status = 'active'", (store_pk,)).fetchone()["n"]
    inactive = total - active
    pending = conn.execute("SELECT COUNT(*) AS n FROM inventory_items WHERE store_id = ? AND change_flag = 1", (store_pk,)).fetchone()["n"]
    new_count = conn.execute("SELECT COUNT(*) AS n FROM inventory_items WHERE store_id = ? AND match_status = 'new'", (store_pk,)).fetchone()["n"]
    last_import = conn.execute(
        "SELECT imported_at FROM import_batches WHERE store_id = ? ORDER BY imported_at DESC LIMIT 1", (store_pk,)
    ).fetchone()
    return {
        "total": total, "active": active, "inactive": inactive,
        "pending_changes": pending, "new_items": new_count,
        "last_import": last_import["imported_at"] if last_import else None,
    }


# ----------------------------------------------------------- mapping presets

def save_mapping_preset(conn: sqlite3.Connection, header_fingerprint: str, kind: str, mapping: dict) -> None:
    import json

    conn.execute(
        """INSERT INTO column_mapping_presets(header_fingerprint, kind, mapping_json) VALUES (?, ?, ?)
           ON CONFLICT(header_fingerprint) DO UPDATE SET mapping_json = excluded.mapping_json""",
        (header_fingerprint, kind, json.dumps(mapping)),
    )
    conn.commit()


def load_mapping_preset(conn: sqlite3.Connection, header_fingerprint: str) -> Optional[dict]:
    import json

    row = conn.execute(
        "SELECT mapping_json FROM column_mapping_presets WHERE header_fingerprint = ?", (header_fingerprint,)
    ).fetchone()
    return json.loads(row["mapping_json"]) if row else None
