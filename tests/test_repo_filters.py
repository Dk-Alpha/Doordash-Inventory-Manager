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
        {"upc_id": "111", "new_price": "2.99"},
        {"upc_id": "999", "item_name": "Brand New Item", "new_price": "4.00"},
    ]
    batch_id, result = repo.import_worklist(conn, store, worklist_rows, "worklist.csv", {})

    assert len(result.existing) == 1
    assert len(result.new) == 1

    updated = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '111'").fetchone()
    assert updated["default_price"] == 2.99
    assert updated["change_flag"] == 1
    assert updated["original_price"] == 2.5

    new_item = conn.execute("SELECT * FROM inventory_items WHERE upc_normalized = '999'").fetchone()
    assert new_item["match_status"] == "new"
    assert new_item["item_name"] == "Brand New Item"


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
