"""SQLite schema + connection management. Single working-state store for the
whole app (spec section 2.3), scoped per store for multi-store support."""
import os
import sqlite3
from pathlib import Path

# Anchored to the project root (parent of this `app` package), not the
# process's current working directory -- a bare relative filename here would
# resolve differently (and silently create a fresh empty DB) depending on
# where/how the app happens to be launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = Path(os.environ.get("DOORDASH_MANAGER_DB", str(_PROJECT_ROOT / "doordash_manager.db")))

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

CREATE TABLE IF NOT EXISTS worklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    import_batch_id INTEGER REFERENCES import_batches(id),
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_worklists_store ON worklists(store_id);

CREATE TABLE IF NOT EXISTS worklist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worklist_id INTEGER NOT NULL REFERENCES worklists(id),
    inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id),
    proposed_price REAL,
    proposed_status TEXT,
    proposed_category_l1 TEXT,
    proposed_category_l2 TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worklist_items_worklist ON worklist_items(worklist_id);
CREATE INDEX IF NOT EXISTS idx_worklist_items_inventory ON worklist_items(inventory_item_id);

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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema changes for DBs created before this
    version -- SQLite has no `ADD COLUMN IF NOT EXISTS`, so guard each one
    with a PRAGMA table_info check. Safe to run on every startup."""
    if not _column_exists(conn, "changelog", "worklist_id"):
        conn.execute("ALTER TABLE changelog ADD COLUMN worklist_id INTEGER REFERENCES worklists(id)")
    if not _column_exists(conn, "inventory_items", "worklist_id"):
        conn.execute("ALTER TABLE inventory_items ADD COLUMN worklist_id INTEGER REFERENCES worklists(id)")

    # Backfill a `worklists` row for any work-list import that predates this
    # table, so history stays browsable. Those matched-row changes were
    # already written straight to inventory under the old behavior, so
    # 'completed' accurately reflects their state -- there's no staging data
    # to reconstruct.
    legacy_batches = conn.execute(
        """SELECT ib.id, ib.store_id, ib.filename, ib.imported_at
           FROM import_batches ib
           WHERE ib.kind = 'worklist'
             AND NOT EXISTS (SELECT 1 FROM worklists w WHERE w.import_batch_id = ib.id)"""
    ).fetchall()
    for batch in legacy_batches:
        conn.execute(
            """INSERT INTO worklists(store_id, import_batch_id, filename, status, created_at, completed_at)
               VALUES (?, ?, ?, 'completed', ?, ?)""",
            (batch["store_id"], batch["id"], batch["filename"], batch["imported_at"], batch["imported_at"]),
        )
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
