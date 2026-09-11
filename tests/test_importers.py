from app.importers import detect_columns, header_fingerprint, read_table


def test_detect_columns_standard_doordash_headers():
    headers = ["business_id", "store_id", "upc_id", "sku_id", "item_name",
               "category_l1", "category_l2", "default_price", "status", "currency"]
    mapping = detect_columns(headers, kind="master")
    assert mapping["upc_id"] == "upc_id"
    assert mapping["item_name"] == "item_name"
    assert mapping["default_price"] == "default_price"


def test_detect_columns_fuzzy_variants():
    headers = ["UPC Code", "Product Name", "List Price", "Active"]
    mapping = detect_columns(headers, kind="master")
    assert mapping["upc_id"] == "UPC Code"
    assert mapping["item_name"] == "Product Name"
    assert mapping["default_price"] == "List Price"


def test_detect_columns_worklist_minimal_upc_only():
    headers = ["UPC"]
    mapping = detect_columns(headers, kind="worklist")
    assert mapping["upc_id"] == "UPC"
    assert "new_price" not in mapping


def test_header_fingerprint_is_order_independent():
    a = header_fingerprint(["upc_id", "item_name", "status"])
    b = header_fingerprint(["status", "upc_id", "item_name"])
    assert a == b


def test_header_fingerprint_differs_on_different_headers():
    a = header_fingerprint(["upc_id", "item_name"])
    b = header_fingerprint(["upc_id", "sku_id"])
    assert a != b


def test_read_table_not_fooled_by_literal_pipes_in_upc_cells(tmp_path):
    """Regression: some exports wrap UPC/SKU values as "|012345|" to force
    text formatting. csv.Sniffer can mistake '|' for the delimiter since it
    never appears in the comma-delimited header, causing a baffling
    "expected 1 field, found 5" crash on the first data row. Comma must
    win when it's clearly the real delimiter (produces >1 column)."""
    csv_text = (
        "business_id,store_id,upc_id,sku_id,item_name,default_price,status,currency\n"
        '14373565,34507329,|000000002097|,|000000002097|,BC Powder Aspirin,2.29,Inactive,USD\n'
        '14373565,34507329,|000002794280|,|000002794280|,Powerade Melon,2.69,Inactive,USD\n'
    )
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8")

    df = read_table(str(path))
    assert list(df.columns) == [
        "business_id", "store_id", "upc_id", "sku_id", "item_name", "default_price", "status", "currency"
    ]
    assert len(df) == 2
    assert df.iloc[0]["item_name"] == "BC Powder Aspirin"


def test_read_table_skips_leading_annotation_row(tmp_path):
    """Regression: Google-Sheets-style exports sometimes have a frozen
    'permissions' row above the real header (e.g. "Read only" / "Can
    update" per column). The real column names must still be picked up
    from row 2, not treated as the header themselves."""
    csv_text = (
        "Read only,Read only,Read only,Read only,Read only,Read only,Read only,Can update,Can update,Read only\n"
        "business_id,store_id,upc_id,sku_id,item_name,category_l1,category_l2,default_price,status,currency\n"
        "14373565,34507329,000000002097,000000002097,BC Powder Aspirin,Medicine,Pain Reliever,2.29,Inactive,USD\n"
    )
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8")

    df = read_table(str(path))
    assert list(df.columns) == [
        "business_id", "store_id", "upc_id", "sku_id", "item_name",
        "category_l1", "category_l2", "default_price", "status", "currency",
    ]
    assert len(df) == 1
    assert df.iloc[0]["item_name"] == "BC Powder Aspirin"
    assert df.attrs.get("header_row_skipped") == 1


def test_read_table_normal_header_on_row_one_is_unaffected(tmp_path):
    csv_text = "upc_id,item_name\n111,Chips\n"
    path = tmp_path / "normal.csv"
    path.write_text(csv_text, encoding="utf-8")

    df = read_table(str(path))
    assert list(df.columns) == ["upc_id", "item_name"]
    assert not df.attrs.get("header_row_skipped")


def test_read_table_skips_malformed_row_instead_of_crashing(tmp_path):
    """A genuinely malformed row (stray unquoted comma) should be skipped
    with a warning attached, not crash the whole import."""
    csv_text = (
        "upc_id,item_name,default_price\n"
        "111,Chips,2.50\n"
        "222,Bad, Row, With, Extra, Commas,1.00\n"
        "333,Soda,1.50\n"
    )
    path = tmp_path / "worklist.csv"
    path.write_text(csv_text, encoding="utf-8")

    df = read_table(str(path))
    assert len(df) == 2
    assert set(df["upc_id"]) == {"111", "333"}
    assert df.attrs.get("skipped_lines")
