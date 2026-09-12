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
    """Match worklist rows against current inventory for this store.

    Matched rows are NOT written to inventory_items here -- they're staged
    as worklist_items for review/editing in the Work Lists tab, and only
    applied when the user pushes them (see push_worklist_items). Unmatched
    rows are inserted as match_status='new', same as before.

    Returns (worklist_id, batch_id, MatchResult).
    """
    import json

    store = conn.execute("SELECT * FROM stores WHERE id = ?", (store_pk,)).fetchone()

    batch_id = conn.execute(
        "INSERT INTO import_batches(store_id, kind, filename, imported_at, column_mapping_json) VALUES (?, 'worklist', ?, ?, ?)",
        (store_pk, filename, now_iso(), json.dumps(mapping)),
    ).lastrowid
    worklist_id = create_worklist(conn, store_pk, batch_id, filename)

    inventory_rows = _fetch_inventory_as_dicts(conn, store_pk)
    result = match_worklist(
        inventory_rows, rows,
        inventory_upc_field="upc_normalized", worklist_upc_field="upc_id",
    )

    for matched in result.existing:
        item_id = matched.inventory_row["id"]
        changes = matched.proposed_changes
        proposed_price = parse_price(changes["new_price"]) if "new_price" in changes else None
        proposed_status = normalize_status(changes["new_status"]) if "new_status" in changes else None
        conn.execute(
            """INSERT INTO worklist_items(
                worklist_id, inventory_item_id, proposed_price, proposed_status,
                proposed_category_l1, proposed_category_l2, applied, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (worklist_id, item_id, proposed_price, proposed_status,
             changes.get("category_l1"), changes.get("category_l2"), now_iso()),
        )

    for new_row in result.new:
        wl = new_row.worklist_row
        price = parse_price(wl.get("new_price"))
        status = normalize_status(wl.get("new_status")) if wl.get("new_status") else "active"
        conn.execute(
            """INSERT INTO inventory_items(
                store_id, upc_normalized, upc_padded, sku_id, item_name, category_l1, category_l2,
                default_price, status, currency, match_status, source, last_modified_ts,
                change_flag, original_price, original_status, import_batch_id, worklist_id
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'new', 'worklist', ?, 0, NULL, NULL, ?, ?)""",
            (store_pk, new_row.upc_normalized, pad_upc(new_row.upc_normalized), wl.get("item_name"),
             wl.get("category_l1"), wl.get("category_l2"), price, status,
             store["currency"] if store else "USD", now_iso(), batch_id, worklist_id),
        )

    conn.commit()
    return worklist_id, batch_id, result


# -------------------------------------------------------------- worklists --

def create_worklist(conn: sqlite3.Connection, store_pk: int, import_batch_id: int, filename: str) -> int:
    cur = conn.execute(
        "INSERT INTO worklists(store_id, import_batch_id, filename, status, created_at) VALUES (?, ?, ?, 'open', ?)",
        (store_pk, import_batch_id, filename, now_iso()),
    )
    return cur.lastrowid


def list_worklists(conn: sqlite3.Connection, store_pk: int) -> list[sqlite3.Row]:
    """Worklists for this store, newest first, with staged/applied/new-item
    counts for the picker label."""
    return conn.execute(
        """SELECT w.*,
                  (SELECT COUNT(*) FROM worklist_items wi WHERE wi.worklist_id = w.id) AS total_items,
                  (SELECT COUNT(*) FROM worklist_items wi WHERE wi.worklist_id = w.id AND wi.applied = 0) AS pending_items,
                  (SELECT COUNT(*) FROM inventory_items i WHERE i.worklist_id = w.id AND i.match_status = 'new') AS new_items
           FROM worklists w
           WHERE w.store_id = ?
           ORDER BY w.created_at DESC""",
        (store_pk,),
    ).fetchall()


def get_worklist(conn: sqlite3.Connection, worklist_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM worklists WHERE id = ?", (worklist_id,)).fetchone()


_WORKLIST_ITEM_SELECT = """
    SELECT wi.id AS worklist_item_id, wi.worklist_id, wi.applied, wi.applied_at,
           wi.proposed_price, wi.proposed_status, wi.proposed_category_l1, wi.proposed_category_l2,
           i.id AS inventory_item_id, i.upc_padded, i.sku_id, i.item_name,
           i.category_l1 AS current_category_l1, i.category_l2 AS current_category_l2,
           i.default_price AS current_price, i.status AS current_status
    FROM worklist_items wi JOIN inventory_items i ON i.id = wi.inventory_item_id
"""


def build_worklist_item_filter_query(worklist_id: int, filters: dict) -> tuple[str, list]:
    """Same search/category/price refinement as build_filter_query, scoped
    to one work list's staged rows joined with their inventory data (the
    "view") -- so search/filter/push here can never reach outside that work
    list. filters keys: pending_only (bool), text1, text2, match_mode,
    price_min, price_max, category_l1 (same meaning as build_filter_query,
    just matched against the row's current inventory values)."""
    clauses = ["wi.worklist_id = ?"]
    params: list = [worklist_id]
    if filters.get("pending_only"):
        clauses.append("wi.applied = 0")

    text_clauses, text_params = _text_search_clauses(filters, "i.item_name", "i.upc_padded", "i.sku_id")
    clauses += text_clauses
    params += text_params

    pc_clauses, pc_params = _price_category_clauses(filters, "i.default_price", "i.category_l1")
    clauses += pc_clauses
    params += pc_params

    return " AND ".join(clauses), params


def list_worklist_items(conn: sqlite3.Connection, worklist_id: int, filters: Optional[dict] = None) -> list[sqlite3.Row]:
    """Worklist staging rows joined with their inventory row's current
    values, so the caller gets "current" (from inventory, authoritative)
    and "proposed" (staged, editable) side by side. filters: see
    build_worklist_item_filter_query; omit/None for "every staged row"."""
    where, params = build_worklist_item_filter_query(worklist_id, filters or {})
    return conn.execute(
        f"{_WORKLIST_ITEM_SELECT} WHERE {where} ORDER BY i.item_name COLLATE NOCASE", params,
    ).fetchall()


def list_worklist_item_ids(conn: sqlite3.Connection, worklist_id: int, filters: Optional[dict] = None) -> list[int]:
    """Every worklist_item id matching a filter, for bulk actions ("push all
    pending") that must act on the current filtered view, not every staged
    row in the work list regardless of what's on screen."""
    where, params = build_worklist_item_filter_query(worklist_id, filters or {})
    rows = conn.execute(
        f"SELECT wi.id FROM worklist_items wi JOIN inventory_items i ON i.id = wi.inventory_item_id WHERE {where}",
        params,
    ).fetchall()
    return [r[0] for r in rows]


def get_worklist_item(conn: sqlite3.Connection, worklist_item_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(f"{_WORKLIST_ITEM_SELECT} WHERE wi.id = ?", (worklist_item_id,)).fetchone()


def update_worklist_item_proposed(conn: sqlite3.Connection, worklist_item_id: int, fields: dict) -> None:
    """Edit a staged (not-yet-applied) worklist item's proposed values
    before push -- the review/editing step."""
    allowed = {"proposed_price", "proposed_status", "proposed_category_l1", "proposed_category_l2"}
    set_clauses = []
    params = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        set_clauses.append(f"{k} = ?")
        params.append(v)
    if not set_clauses:
        return
    params.append(worklist_item_id)
    conn.execute(f"UPDATE worklist_items SET {', '.join(set_clauses)} WHERE id = ? AND applied = 0", params)
    conn.commit()


def push_worklist_items(conn: sqlite3.Connection, worklist_id: int, worklist_item_ids: Optional[list[int]] = None) -> str:
    """Apply staged proposed changes onto real inventory rows -- the moment
    a work list's edits actually take effect. worklist_item_ids=None targets
    every not-yet-applied item in the work list. Returns a batch_action_id
    (same changelog/undo mechanism as the other bulk actions), with every
    changelog row tagged with worklist_id for future per-work-list history."""
    batch_action_id = new_batch_action_id()

    where = "worklist_id = ? AND applied = 0"
    params: list = [worklist_id]
    if worklist_item_ids is not None:
        if not worklist_item_ids:
            return batch_action_id
        where += f" AND id IN ({','.join('?' * len(worklist_item_ids))})"
        params += worklist_item_ids

    staged_items = conn.execute(f"SELECT * FROM worklist_items WHERE {where}", params).fetchall()

    for staged in staged_items:
        item_id = staged["inventory_item_id"]
        current = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if current is None:
            continue

        field_changes = []
        if staged["proposed_price"] is not None and staged["proposed_price"] != current["default_price"]:
            field_changes.append(("default_price", current["default_price"], staged["proposed_price"]))
        if staged["proposed_status"] is not None and staged["proposed_status"] != current["status"]:
            field_changes.append(("status", current["status"], staged["proposed_status"]))
        if staged["proposed_category_l1"] not in (None, "") and staged["proposed_category_l1"] != current["category_l1"]:
            field_changes.append(("category_l1", current["category_l1"], staged["proposed_category_l1"]))
        if staged["proposed_category_l2"] not in (None, "") and staged["proposed_category_l2"] != current["category_l2"]:
            field_changes.append(("category_l2", current["category_l2"], staged["proposed_category_l2"]))

        if field_changes:
            _apply_field_changes(conn, item_id, current, field_changes, batch_action_id, worklist_id=worklist_id)

        conn.execute(
            "UPDATE worklist_items SET applied = 1, applied_at = ? WHERE id = ?",
            (now_iso(), staged["id"]),
        )

    conn.commit()
    return batch_action_id


def mark_worklist_completed(conn: sqlite3.Connection, worklist_id: int) -> None:
    conn.execute(
        "UPDATE worklists SET status = 'completed', completed_at = ? WHERE id = ?",
        (now_iso(), worklist_id),
    )
    conn.commit()


def reopen_worklist(conn: sqlite3.Connection, worklist_id: int) -> None:
    conn.execute("UPDATE worklists SET status = 'open', completed_at = NULL WHERE id = ?", (worklist_id,))
    conn.commit()


def worklist_pending_summary(conn: sqlite3.Connection, worklist_id: int) -> dict:
    """Counts used to warn before marking a work list completed."""
    pending_changes = conn.execute(
        "SELECT COUNT(*) AS n FROM worklist_items WHERE worklist_id = ? AND applied = 0", (worklist_id,)
    ).fetchone()["n"]
    new_items = conn.execute(
        "SELECT * FROM inventory_items WHERE worklist_id = ? AND match_status = 'new'", (worklist_id,)
    ).fetchall()
    incomplete_new_items = sum(
        1 for r in new_items if any(r[f] in (None, "") for f in REQUIRED_NEW_SKU_FIELDS)
    )
    return {"pending_changes": pending_changes, "incomplete_new_items": incomplete_new_items}


# --------------------------------------------------------------- filters ---
#
# The "layer 2 operates on layer 1" pattern used everywhere a grid needs
# search/category/price refinement: a caller first builds a scope clause
# that defines *the view* (e.g. "this store's existing items", "this store's
# new items", "this one work list's staged rows") and ANDs the shared
# refinement clauses below onto it. Because it's all one WHERE clause, a
# search/filter/bulk-action can never reach outside the scope it started
# from -- there's no separate "layer" to accidentally query around.

def _text_search_clauses(filters: dict, name_col: str, upc_col: str, sku_col: str) -> tuple[list, list]:
    """The two-stage tokenized text search, reusable against either bare
    inventory_items columns or aliased/joined ones (e.g. "i.item_name")."""
    match_mode = filters.get("match_mode", "AND")
    clauses = []
    params: list = []
    for text_key in ("text1", "text2"):
        text = (filters.get(text_key) or "").strip()
        if not text:
            continue
        tokens = text.split()
        token_clauses = []
        for token in tokens:
            like = f"%{token}%"
            token_clauses.append(f"({name_col} LIKE ? OR {upc_col} LIKE ? OR {sku_col} LIKE ?)")
            params += [like, like, like]
        joiner = " AND " if match_mode == "AND" else " OR "
        clauses.append("(" + joiner.join(token_clauses) + ")")
    return clauses, params


def _price_category_clauses(filters: dict, price_col: str, category_col: str) -> tuple[list, list]:
    clauses = []
    params: list = []
    if filters.get("price_min") is not None:
        clauses.append(f"{price_col} >= ?")
        params.append(filters["price_min"])
    if filters.get("price_max") is not None:
        clauses.append(f"{price_col} <= ?")
        params.append(filters["price_max"])
    if filters.get("category_l1"):
        clauses.append(f"{category_col} = ?")
        params.append(filters["category_l1"])
    return clauses, params


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

    text_clauses, text_params = _text_search_clauses(filters, "item_name", "upc_padded", "sku_id")
    clauses += text_clauses
    params += text_params

    pc_clauses, pc_params = _price_category_clauses(filters, "default_price", "category_l1")
    clauses += pc_clauses
    params += pc_params

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


def list_item_ids(conn: sqlite3.Connection, store_pk: int, filters: dict) -> list[int]:
    """All item ids matching a filter, unpaginated -- backs "select all
    matching this filter" for bulk actions, as distinct from only the rows
    a paginated grid happens to have fetched so far."""
    where, params = build_filter_query(store_pk, filters)
    rows = conn.execute(f"SELECT id FROM inventory_items WHERE {where}", params).fetchall()
    return [r["id"] for r in rows]


def distinct_category_values(conn: sqlite3.Connection, store_pk: int, field: str, parent_l1: Optional[str] = None) -> list[str]:
    """Unique, non-blank category values already used in this store's
    inventory -- backs both the category filter and the category dropdown
    editors, so users pick from what already exists instead of retyping
    (and mistyping) category names.

    For field='category_l2', passing parent_l1 narrows the list to the L2
    values that already co-occur with that L1 (a lightweight cascading
    dropdown), since DoorDash's taxonomy is a real L1->L2 hierarchy.
    """
    if field not in ("category_l1", "category_l2"):
        raise ValueError(f"Unsupported category field: {field}")

    if field == "category_l2" and parent_l1:
        rows = conn.execute(
            """SELECT DISTINCT category_l2 FROM inventory_items
               WHERE store_id = ? AND category_l1 = ? AND category_l2 IS NOT NULL AND category_l2 != ''
               ORDER BY category_l2 COLLATE NOCASE""",
            (store_pk, parent_l1),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT DISTINCT {field} FROM inventory_items
                WHERE store_id = ? AND {field} IS NOT NULL AND {field} != ''
                ORDER BY {field} COLLATE NOCASE""",
            (store_pk,),
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------- bulk actions ---

def _apply_field_changes(
    conn: sqlite3.Connection, item_id: int, current: sqlite3.Row, field_changes: list[tuple[str, Any, Any]],
    batch_action_id: str, worklist_id: Optional[int] = None,
) -> None:
    """Shared write path for every kind of inventory field edit (bulk status,
    bulk price, a work-list push): log each (field, old, new) to changelog
    -- tagged with worklist_id when the change came from pushing a work list,
    so its history stays traceable -- snapshot original_price/original_status
    on the item's first-ever edit, and set change_flag."""
    first_edit = current["change_flag"] == 0
    set_clauses = []
    params: list = []
    for field, old_value, new_value in field_changes:
        conn.execute(
            """INSERT INTO changelog(item_id, field, old_value, new_value, changed_at, batch_action_id, worklist_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, field, old_value, new_value, now_iso(), batch_action_id, worklist_id),
        )
        set_clauses.append(f"{field} = ?")
        params.append(new_value)

    if first_edit:
        set_clauses += ["original_price = ?", "original_status = ?"]
        params += [current["default_price"], current["status"]]

    set_clauses += ["change_flag = 1", "last_modified_ts = ?"]
    params.append(now_iso())
    params.append(item_id)
    conn.execute(f"UPDATE inventory_items SET {', '.join(set_clauses)} WHERE id = ?", params)


def apply_bulk_status(conn: sqlite3.Connection, item_ids: list[int], to_status: str) -> str:
    batch_action_id = new_batch_action_id()
    for item_id in item_ids:
        current = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if current is None or current["status"] == to_status:
            continue
        _apply_field_changes(conn, item_id, current, [("status", current["status"], to_status)], batch_action_id)
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
        _apply_field_changes(
            conn, item_id, current, [("default_price", current["default_price"], new_price)], batch_action_id
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
    open_worklists = conn.execute(
        "SELECT COUNT(*) AS n FROM worklists WHERE store_id = ? AND status = 'open'", (store_pk,)
    ).fetchone()["n"]
    last_import = conn.execute(
        "SELECT imported_at FROM import_batches WHERE store_id = ? ORDER BY imported_at DESC LIMIT 1", (store_pk,)
    ).fetchone()
    return {
        "total": total, "active": active, "inactive": inactive,
        "pending_changes": pending, "new_items": new_count,
        "open_worklists": open_worklists,
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
