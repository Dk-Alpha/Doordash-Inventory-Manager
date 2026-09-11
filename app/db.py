"""SQLite schema + connection management. Single working-state store for the
whole app (spec section 2.3), scoped per store for multi-store support."""
import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("DOORDASH_MANAGER_DB", "doordash_manager.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    UNIQUE(business_id, store_id)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    kind TEXT NOT NULL CHECK(kind IN ('master', 'worklist')),
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    column_mapping_json TEXT
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    upc_normalized TEXT NOT NULL,
    upc_padded TEXT,
    sku_id TEXT,
    item_name TEXT,
    category_l1 TEXT,
    category_l2 TEXT,
    default_price REAL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'inactive')),
    currency TEXT,
    match_status TEXT NOT NULL DEFAULT 'existing' CHECK(match_status IN ('existing', 'new', 'unmatched_in_worklist')),
    source TEXT NOT NULL DEFAULT 'master' CHECK(source IN ('master', 'worklist')),
    last_modified_ts TEXT,
    change_flag INTEGER NOT NULL DEFAULT 0,
    original_price REAL,
    original_status TEXT,
    import_batch_id INTEGER REFERENCES import_batches(id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_store_upc ON inventory_items(store_id, upc_normalized);
CREATE INDEX IF NOT EXISTS idx_inventory_store_match ON inventory_items(store_id, match_status);

CREATE TABLE IF NOT EXISTS changelog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL,
    batch_action_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changelog_batch ON changelog(batch_action_id);

CREATE TABLE IF NOT EXISTS column_mapping_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    header_fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('master', 'worklist')),
    mapping_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
