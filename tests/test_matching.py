from app.matching import match_worklist


def test_worklist_item_matches_existing_inventory():
    inventory = [{"upc_id": "012345678901", "item_name": "Red Bull 8.4oz"}]
    worklist = [{"upc_id": "12345678901", "new_price": "3.99"}]
    result = match_worklist(inventory, worklist)
    assert len(result.existing) == 1
    assert len(result.new) == 0
    assert result.existing[0].proposed_changes == {"new_price": "3.99"}


def test_worklist_item_with_no_inventory_match_is_new():
    inventory = [{"upc_id": "111111111111", "item_name": "Something Else"}]
    worklist = [{"upc_id": "222222222222", "item_name": "New Thing", "new_price": "1.99"}]
    result = match_worklist(inventory, worklist)
    assert len(result.existing) == 0
    assert len(result.new) == 1
    assert result.new[0].upc_normalized == "222222222222"


def test_worklist_with_only_upcs_still_matches():
    inventory = [{"upc_id": "555555555555", "item_name": "Chips"}]
    worklist = [{"upc_id": "555555555555"}]
    result = match_worklist(inventory, worklist)
    assert len(result.existing) == 1
    assert result.existing[0].proposed_changes == {}


def test_blank_upc_rows_are_skipped_not_crashed():
    inventory = [{"upc_id": "1", "item_name": "X"}]
    worklist = [{"upc_id": "", "item_name": "no upc"}, {"upc_id": None, "item_name": "also none"}]
    result = match_worklist(inventory, worklist)
    assert len(result.skipped_blank_upc) == 2
    assert len(result.existing) == 0
    assert len(result.new) == 0


def test_duplicate_upcs_in_inventory_are_flagged():
    inventory = [
        {"upc_id": "999", "item_name": "First"},
        {"upc_id": "999", "item_name": "Duplicate"},
    ]
    worklist = [{"upc_id": "999", "new_price": "5.00"}]
    result = match_worklist(inventory, worklist)
    assert len(result.inventory_duplicates) == 1
    assert result.inventory_duplicates[0].upc_normalized == "999"
    # still resolves a match (keep_first policy) rather than dropping the row
    assert len(result.existing) == 1
    assert result.existing[0].inventory_row["item_name"] == "First"


def test_duplicate_upcs_in_worklist_are_flagged():
    inventory = [{"upc_id": "42", "item_name": "Thing"}]
    worklist = [
        {"upc_id": "42", "new_price": "1.00"},
        {"upc_id": "42", "new_price": "2.00"},
    ]
    result = match_worklist(inventory, worklist)
    assert len(result.worklist_duplicates) == 1
    assert len(result.existing) == 2


def test_leading_zero_upc_still_matches():
    inventory = [{"upc_id": "12345"}]
    worklist = [{"upc_id": "000012345"}]
    result = match_worklist(inventory, worklist)
    assert len(result.existing) == 1
    assert len(result.new) == 0
