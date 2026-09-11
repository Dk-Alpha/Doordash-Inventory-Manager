"""The core matching engine: work-list rows vs. master inventory, by UPC.

Pure functions, no DB/Qt imports, so this is unit-testable in isolation —
this is the single most important piece of logic in the app (a bug here
silently misroutes items between "existing" and "new").
"""
from dataclasses import dataclass, field
from typing import Any, Optional

from app.upc import normalize_upc

# Optional worklist columns that represent an intended change to apply
# during the bulk-review step, if present.
PROPOSED_CHANGE_FIELDS = ["item_name", "new_price", "new_status", "category_l1", "category_l2"]


@dataclass
class MatchedRow:
    upc_normalized: str
    inventory_row: dict
    proposed_changes: dict


@dataclass
class NewRow:
    upc_normalized: str
    worklist_row: dict


@dataclass
class DuplicateGroup:
    upc_normalized: str
    rows: list
    source: str  # 'inventory' | 'worklist'


@dataclass
class MatchResult:
    existing: list = field(default_factory=list)      # list[MatchedRow]
    new: list = field(default_factory=list)            # list[NewRow]
    skipped_blank_upc: list = field(default_factory=list)  # worklist rows with no usable UPC
    inventory_duplicates: list = field(default_factory=list)   # list[DuplicateGroup]
    worklist_duplicates: list = field(default_factory=list)    # list[DuplicateGroup]


def _find_duplicates(rows: list, upc_field: str, source: str) -> list:
    by_key: dict[str, list] = {}
    for row in rows:
        key = normalize_upc(row.get(upc_field))
        if not key:
            continue
        by_key.setdefault(key, []).append(row)
    return [
        DuplicateGroup(upc_normalized=key, rows=group, source=source)
        for key, group in by_key.items()
        if len(group) > 1
    ]


def _extract_proposed_changes(worklist_row: dict) -> dict:
    changes = {}
    for f in PROPOSED_CHANGE_FIELDS:
        if f in worklist_row and worklist_row[f] not in (None, ""):
            changes[f] = worklist_row[f]
    return changes


def match_worklist(
    inventory_rows: list[dict],
    worklist_rows: list[dict],
    inventory_upc_field: str = "upc_id",
    worklist_upc_field: str = "upc_id",
    duplicate_policy: str = "keep_first",
) -> MatchResult:
    """Match each worklist row against inventory by normalized UPC.

    duplicate_policy applies to inventory duplicates only (which row wins
    the lookup): 'keep_first' or 'keep_last'. Duplicates are always reported
    in the result regardless of policy, so the caller can flag them for
    manual review instead of silently dropping data.
    """
    result = MatchResult()

    result.inventory_duplicates = _find_duplicates(inventory_rows, inventory_upc_field, "inventory")
    result.worklist_duplicates = _find_duplicates(worklist_rows, worklist_upc_field, "worklist")

    inventory_by_key: dict[str, dict] = {}
    for row in inventory_rows:
        key = normalize_upc(row.get(inventory_upc_field))
        if not key:
            continue
        if key not in inventory_by_key or duplicate_policy == "keep_last":
            inventory_by_key[key] = row

    for row in worklist_rows:
        key = normalize_upc(row.get(worklist_upc_field))
        if not key:
            result.skipped_blank_upc.append(row)
            continue
        inv_row = inventory_by_key.get(key)
        if inv_row is not None:
            result.existing.append(
                MatchedRow(
                    upc_normalized=key,
                    inventory_row=inv_row,
                    proposed_changes=_extract_proposed_changes(row),
                )
            )
        else:
            result.new.append(NewRow(upc_normalized=key, worklist_row=row))

    return result
