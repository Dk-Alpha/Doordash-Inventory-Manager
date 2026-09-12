import sqlite3

import pytest

from app import db, repo, export


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


@pytest.fixture
def store(conn):
    store_pk = repo.create_store(conn, "biz1", "store1", "Test Store")
    return store_pk


def test_build_filter_query_status_only():
    where, params = repo.build_filter_query(1, {"status": "active"})
    assert "status = ?" in where
    assert params == [1, "active"]


def test_build_filter_query_two_stage_text_narrows(conn, store):
    rows = [
        {"upc_id": "1", "item_name": "Red Bull 8.4oz", "default_price": "3.00", "status": "active"},
        {"upc_id": "2", "item_name": "Red Bull 12oz", "default_price": "3.50", "status": "active"},
        {"upc_id": "3", "item_name": "Coke 12oz", "default_price": "2.00", "status": "active"},
    ]
    repo.import_master(conn, store, rows, "master.csv", {})

    stage1 = repo.list_items(conn, store, {"text1": "red bull"})
    assert len(stage1) == 2

    stage2 = repo.list_items(conn, store, {"text1": "red bull", "text2": "12oz"})
    assert len(stage2) == 1
    assert stage2[0]["item_name"] == "Red Bull 12oz"


def test_master_import_then_worklist_match_and_new_items(conn, store):
    master_rows = [
        {"upc_id": "111", "item_name": "Chips", "default_price": "2.50", "status": "active"},
        {"upc_id": "222", "item_name": "Soda", "default_price": "1.50", "status": "inactive"},
    ]
    repo.import_master(conn, store, master_rows, "master.csv", {})

    worklist_rows = [
        {"upc_id": "111", "item_name": "Stale Name From Worklist", "new_price": "2.99"},
        {"upc_id": "999", "item_name": "Brand New Item", "new_price": "4.00"},
    ]
    worklist_id, batch_id, result = repo.import_worklist(conn, store, worklist_rows, "worklist.csv", {})

    assert len(result.existing) == 1
    assert len(result.new) == 1

    # Matched rows are staged, not written to inventory yet -- the name and
    # price must be untouched, and untouched by the worklist's own (possibly
    # stale) item name in particular.
    matched = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '111'").fetchone()
    assert matched["default_price"] == 2.5
    assert matched["item_name"] == "Chips"
    assert matched["change_flag"] == 0

    staged = repo.list_worklist_items(conn, worklist_id)
    assert len(staged) == 1
    assert staged[0]["proposed_price"] == 2.99
    assert staged[0]["item_name"] == "Chips"  # always from inventory, never the worklist file

    new_item = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '999'").fetchone()
    assert new_item["match_status"] == "new"
    assert new_item["item_name"] == "Brand New Item"
    assert new_item["worklist_id"] == worklist_id

    # Pushing applies the staged change and records it against the work list.
    repo.push_worklist_items(conn, worklist_id, None)
    pushed = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '111'").fetchone()
    assert pushed["default_price"] == 2.99
    assert pushed["change_flag"] == 1
    assert pushed["original_price"] == 2.5

    changelog_row = conn.execute(
        "SELECT * FROM changelog WHERE item_id = ? AND field = 'default_price'", (pushed["id"],)
    ).fetchone()
    assert changelog_row["worklist_id"] == worklist_id


def test_worklist_lifecycle_mark_completed_and_reopen(conn, store):
    repo.import_master(conn, store, [{"upc_id": "1", "item_name": "A", "default_price": "1.00", "status": "active"}], "m.csv", {})
    worklist_id, _batch_id, _result = repo.import_worklist(
        conn, store, [{"upc_id": "1", "new_price": "2.00"}], "w.csv", {}
    )

    assert repo.get_worklist(conn, worklist_id)["status"] == "open"
    summary = repo.worklist_pending_summary(conn, worklist_id)
    assert summary["pending_changes"] == 1

    repo.mark_worklist_completed(conn, worklist_id)
    assert repo.get_worklist(conn, worklist_id)["status"] == "completed"

    repo.reopen_worklist(conn, worklist_id)
    assert repo.get_worklist(conn, worklist_id)["status"] == "open"


def test_legacy_worklist_import_batches_are_backfilled_as_completed_worklists(conn, store):
    import json

    batch_id = conn.execute(
        "INSERT INTO import_batches(store_id, kind, filename, imported_at, column_mapping_json) VALUES (?, 'worklist', ?, ?, ?)",
        (store, "legacy.csv", "2024-01-01T00:00:00+00:00", json.dumps({})),
    ).lastrowid
    conn.commit()

    db.init_db(conn)  # re-running the migration should backfill the legacy batch once

    rows = conn.execute("SELECT * FROM worklists WHERE import_batch_id = ?", (batch_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"

    db.init_db(conn)  # idempotent -- must not duplicate on a second run
    rows_again = conn.execute("SELECT * FROM worklists WHERE import_batch_id = ?", (batch_id,)).fetchall()
    assert len(rows_again) == 1


def test_bulk_status_and_undo(conn, store):
    repo.import_master(conn, store, [{"upc_id": "1", "item_name": "A", "default_price": "1.00", "status": "active"}], "m.csv", {})
    item = conn.execute("SELECT id FROM inventory_items WHERE upc_normalized = '1'").fetchone()

    batch = repo.apply_bulk_status(conn, [item["id"]], "inactive")
    row = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "inactive"
    assert row["change_flag"] == 1

    repo.undo_batch(conn, batch)
    row = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item["id"],)).fetchone()
    assert row["status"] == "active"
    assert row["change_flag"] == 0


def test_bulk_price_modes(conn, store):
    repo.import_master(conn, store, [{"upc_id": "1", "item_name": "A", "default_price": "10.00", "status": "active"}], "m.csv", {})
    item_id = conn.execute("SELECT id FROM inventory_items WHERE upc_normalized = '1'").fetchone()["id"]

    repo.apply_bulk_price(conn, [item_id], "inc_pct", 10)
    assert conn.execute("SELECT default_price FROM inventory_items WHERE id = ?", (item_id,)).fetchone()[0] == 11.0

    repo.apply_bulk_price(conn, [item_id], "set", 5.0)
    assert conn.execute("SELECT default_price FROM inventory_items WHERE id = ?", (item_id,)).fetchone()[0] == 5.0


def test_list_item_ids_returns_all_matches_unpaginated(conn, store):
    rows = [{"upc_id": str(i), "item_name": f"Item {i}", "default_price": "1", "status": "active"} for i in range(1, 6)]
    repo.import_master(conn, store, rows, "m.csv", {})
    ids = repo.list_item_ids(conn, store, {"status": "active"})
    assert len(ids) == 5
    assert all(isinstance(i, int) for i in ids)


def test_distinct_category_values_dedupes_and_sorts(conn, store):
    rows = [
        {"upc_id": "1", "item_name": "Chips", "category_l1": "Snacks", "category_l2": "Chips", "default_price": "1", "status": "active"},
        {"upc_id": "2", "item_name": "Pretzels", "category_l1": "Snacks", "category_l2": "Pretzels", "default_price": "1", "status": "active"},
        {"upc_id": "3", "item_name": "Soda", "category_l1": "Beverages", "category_l2": "Soda", "default_price": "1", "status": "active"},
        {"upc_id": "4", "item_name": "No category", "category_l1": "", "category_l2": "", "default_price": "1", "status": "active"},
    ]
    repo.import_master(conn, store, rows, "m.csv", {})

    l1_values = repo.distinct_category_values(conn, store, "category_l1")
    assert l1_values == ["Beverages", "Snacks"]

    all_l2 = repo.distinct_category_values(conn, store, "category_l2")
    assert set(all_l2) == {"Chips", "Pretzels", "Soda"}

    snacks_l2 = repo.distinct_category_values(conn, store, "category_l2", parent_l1="Snacks")
    assert set(snacks_l2) == {"Chips", "Pretzels"}


def test_export_new_skus_blocks_on_missing_required_fields(conn, store, tmp_path):
    repo.import_master(conn, store, [{"upc_id": "1", "item_name": "A", "default_price": "1.00", "status": "active"}], "m.csv", {})
    repo.import_worklist(conn, store, [{"upc_id": "999", "item_name": "Incomplete"}], "w.csv", {})

    with pytest.raises(export.ExportValidationError):
        export.export_new_skus(conn, store, str(tmp_path / "new_skus.csv"))

    item_id = conn.execute("SELECT id FROM inventory_items WHERE upc_normalized = '999'").fetchone()["id"]
    repo.update_new_item_fields(conn, item_id, {
        "category_l1": "Beverages", "category_l2": "Energy", "default_price": 3.99,
        "status": "active", "currency": "USD",
    })
    out_path = export.export_new_skus(conn, store, str(tmp_path / "new_skus.csv"))
    content = open(out_path).read()
    assert "Incomplete" in content
    assert content.splitlines()[0] == ",".join(export.DOORDASH_COLUMNS)


def test_worklist_items_can_be_searched_and_filtered_like_existing_items(conn, store):
    """The Work Lists tab's search/category/price filters must narrow the
    same way Existing Items' do -- and "push all" must only ever reach rows
    matching the current filter, never the whole work list."""
    repo.import_master(conn, store, [
        {"upc_id": "1", "item_name": "Red Bull 8.4oz", "category_l1": "Beverages", "default_price": "3.00", "status": "active"},
        {"upc_id": "2", "item_name": "Red Bull 12oz", "category_l1": "Beverages", "default_price": "3.50", "status": "active"},
        {"upc_id": "3", "item_name": "Chips", "category_l1": "Snacks", "default_price": "2.00", "status": "active"},
    ], "master.csv", {})
    worklist_id, _batch_id, _result = repo.import_worklist(conn, store, [
        {"upc_id": "1", "new_price": "3.25"},
        {"upc_id": "2", "new_price": "3.75"},
        {"upc_id": "3", "new_price": "2.25"},
    ], "worklist.csv", {})

    red_bull_only = repo.list_worklist_items(conn, worklist_id, {"text1": "red bull"})
    assert len(red_bull_only) == 2

    snacks_only = repo.list_worklist_items(conn, worklist_id, {"category_l1": "Snacks"})
    assert len(snacks_only) == 1
    assert snacks_only[0]["item_name"] == "Chips"

    snacks_ids = repo.list_worklist_item_ids(conn, worklist_id, {"category_l1": "Snacks", "pending_only": True})
    assert len(snacks_ids) == 1

    # "Push all matching filter" must only push the filtered subset.
    repo.push_worklist_items(conn, worklist_id, snacks_ids)
    chips = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '3'").fetchone()
    assert chips["default_price"] == 2.25
    red_bull_1 = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '1'").fetchone()
    assert red_bull_1["default_price"] == 3.00  # untouched -- wasn't in the filtered/pushed set

    still_pending = repo.list_worklist_item_ids(conn, worklist_id, {"pending_only": True})
    assert len(still_pending) == 2


def test_bulk_set_worklist_items_status_and_price(conn, store):
    repo.import_master(conn, store, [
        {"upc_id": "1", "item_name": "A", "default_price": "10.00", "status": "active"},
        {"upc_id": "2", "item_name": "B", "default_price": "20.00", "status": "active"},
    ], "master.csv", {})
    worklist_id, _batch_id, _result = repo.import_worklist(conn, store, [
        {"upc_id": "1"}, {"upc_id": "2"},
    ], "worklist.csv", {})

    staged = repo.list_worklist_items(conn, worklist_id)
    ids = [r["worklist_item_id"] for r in staged]

    affected = repo.bulk_set_worklist_items_status(conn, ids, "inactive")
    assert affected == 2
    staged = repo.list_worklist_items(conn, worklist_id)
    assert all(r["proposed_status"] == "inactive" for r in staged)

    affected = repo.bulk_set_worklist_items_price(conn, ids, "inc_pct", 10)
    assert affected == 2
    staged = {r["item_name"]: r for r in repo.list_worklist_items(conn, worklist_id)}
    assert staged["A"]["proposed_price"] == 11.0
    assert staged["B"]["proposed_price"] == 22.0

    # inventory itself is untouched until push
    a = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '1'").fetchone()
    assert a["default_price"] == 10.0
    assert a["status"] == "active"

    repo.push_worklist_items(conn, worklist_id, ids)
    a = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '1'").fetchone()
    assert a["default_price"] == 11.0
    assert a["status"] == "inactive"


def test_export_updated_inventory_excludes_new_items(conn, store, tmp_path):
    """Regression: new items (match_status='new') have no real sku_id yet --
    they get one from DoorDash via the separate New SKU export. Including
    them in the "Updated Inventory" re-upload produced a blank sku_id cell
    that DoorDash's importer rejected as "unknown sku id: undefined"."""
    repo.import_master(conn, store, [
        {"upc_id": "1", "sku_id": "SKU-1", "item_name": "Chips", "default_price": "2.50", "status": "active"},
    ], "master.csv", {})
    repo.import_worklist(conn, store, [
        {"upc_id": "999", "item_name": "Brand New Item", "new_price": "4.00"},
    ], "worklist.csv", {})

    out_path = export.export_updated_inventory(conn, store, str(tmp_path / "updated.csv"))
    content = open(out_path).read()
    assert "Chips" in content
    assert "SKU-1" in content
    assert "Brand New Item" not in content
