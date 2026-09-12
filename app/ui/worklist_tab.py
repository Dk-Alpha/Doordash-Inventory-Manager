"""Work Lists tab: review and edit a work list's staged changes to existing
inventory before pushing them, and track each work list's lifecycle
(open -> completed). Unmatched (new) rows from the same import still live in
the New Items tab -- this tab only handles the matched/existing side, which
previously had no review step at all (matches got written straight to
inventory during import)."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app import repo
from app.ui.delegates import ComboBoxDelegate
from app.ui.models import WorklistItemsTableModel

COLUMNS = [
    ("upc_padded", "UPC"),
    ("item_name", "Item Name"),
    ("current_category_l1", "Category 1"),
    ("current_category_l2", "Category 2"),
    ("current_price", "Price"),
    ("current_status", "Status"),
    ("proposed_category_l1", "New Category 1"),
    ("proposed_category_l2", "New Category 2"),
    ("proposed_price", "New Price"),
    ("proposed_status", "New Status"),
    ("applied", "Pushed"),
]

EDITABLE_FIELDS = frozenset({"proposed_price", "proposed_status", "proposed_category_l1", "proposed_category_l2"})

# +1 to account for the checkbox column the grid always prepends.
PROPOSED_L1_COL = [f for f, _ in COLUMNS].index("proposed_category_l1") + 1
PROPOSED_L2_COL = [f for f, _ in COLUMNS].index("proposed_category_l2") + 1
PROPOSED_STATUS_COL = [f for f, _ in COLUMNS].index("proposed_status") + 1


class WorklistTab(QWidget):
    def __init__(self, conn, get_active_store_pk, on_data_changed, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.get_active_store_pk = get_active_store_pk
        self.on_data_changed = on_data_changed
        self._worklist_rows: list = []

        self.model = WorklistItemsTableModel(
            conn, COLUMNS, editable_fields=EDITABLE_FIELDS, on_cell_edit=self._on_cell_edit,
        )

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Review and adjust each work list's proposed changes before pushing them to "
            "inventory. \"Item Name\"/current values always come from inventory -- a work "
            "list only locates the item by UPC, it never overrides the name."
        ))

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Work list:"))
        self.worklist_combo = QComboBox()
        self.worklist_combo.setMinimumWidth(380)
        self.worklist_combo.currentIndexChanged.connect(self._on_worklist_selected)
        picker_row.addWidget(self.worklist_combo)
        self.pending_only = QCheckBox("Pending only")
        self.pending_only.setChecked(True)
        self.pending_only.stateChanged.connect(self._reload_items)
        picker_row.addWidget(self.pending_only)
        picker_row.addStretch()
        layout.addLayout(picker_row)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.status_delegate = ComboBoxDelegate(lambda index: ["active", "inactive"], self.table)
        self.table.setItemDelegateForColumn(PROPOSED_STATUS_COL, self.status_delegate)
        self.l1_delegate = ComboBoxDelegate(self._category_l1_options, self.table)
        self.l2_delegate = ComboBoxDelegate(self._category_l2_options, self.table)
        self.table.setItemDelegateForColumn(PROPOSED_L1_COL, self.l1_delegate)
        self.table.setItemDelegateForColumn(PROPOSED_L2_COL, self.l2_delegate)

        layout.addWidget(self.table)

        bulk_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All Pending")
        select_all_btn.clicked.connect(self._select_all)
        bulk_row.addWidget(select_all_btn)
        clear_btn = QPushButton("Clear Selection")
        clear_btn.clicked.connect(self._clear_selection)
        bulk_row.addWidget(clear_btn)
        push_selected_btn = QPushButton("Push Selected to Inventory")
        push_selected_btn.clicked.connect(self._push_selected)
        bulk_row.addWidget(push_selected_btn)
        push_all_btn = QPushButton("Push All Pending")
        push_all_btn.clicked.connect(self._push_all)
        bulk_row.addWidget(push_all_btn)
        layout.addLayout(bulk_row)

        lifecycle_row = QHBoxLayout()
        self.complete_btn = QPushButton("Mark Work List Completed")
        self.complete_btn.clicked.connect(self._mark_completed)
        lifecycle_row.addWidget(self.complete_btn)
        self.reopen_btn = QPushButton("Reopen Work List")
        self.reopen_btn.clicked.connect(self._reopen)
        lifecycle_row.addWidget(self.reopen_btn)
        lifecycle_row.addStretch()
        layout.addLayout(lifecycle_row)

    # -- category dropdowns --------------------------------------------------

    def _category_l1_options(self, index) -> list[str]:
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return []
        return repo.distinct_category_values(self.conn, store_pk, "category_l1")

    def _category_l2_options(self, index) -> list[str]:
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return []
        row = self.model.row_at(index.row())
        l1_value = row["proposed_category_l1"] or row["current_category_l1"] or None
        return repo.distinct_category_values(self.conn, store_pk, "category_l2", parent_l1=l1_value)

    # -- worklist picker ------------------------------------------------------

    def refresh(self):
        store_pk = self.get_active_store_pk()
        current_id = self._current_worklist_id()
        self.worklist_combo.blockSignals(True)
        self.worklist_combo.clear()
        self._worklist_rows = list(repo.list_worklists(self.conn, store_pk)) if store_pk is not None else []
        for w in self._worklist_rows:
            extra = f", {w['new_items']} new" if w["new_items"] else ""
            label = f"{w['filename']} — {w['status']} ({w['pending_items']} pending / {w['total_items']} staged{extra})"
            self.worklist_combo.addItem(label, w["id"])
        self.worklist_combo.blockSignals(False)

        target_index = 0
        if current_id is not None:
            for i, w in enumerate(self._worklist_rows):
                if w["id"] == current_id:
                    target_index = i
                    break
        if self._worklist_rows:
            self.worklist_combo.setCurrentIndex(target_index)
        self._on_worklist_selected(target_index)

    def select_worklist(self, worklist_id: int):
        """Jump straight to a specific work list -- used right after a
        work-list import so the user lands on what they just uploaded."""
        self.refresh()
        for i, w in enumerate(self._worklist_rows):
            if w["id"] == worklist_id:
                self.worklist_combo.setCurrentIndex(i)
                break

    def _current_worklist_id(self):
        idx = self.worklist_combo.currentIndex()
        if 0 <= idx < len(self._worklist_rows):
            return self._worklist_rows[idx]["id"]
        return None

    def _current_worklist_row(self):
        idx = self.worklist_combo.currentIndex()
        if 0 <= idx < len(self._worklist_rows):
            return self._worklist_rows[idx]
        return None

    def _on_worklist_selected(self, _index: int):
        self._reload_items()
        self._update_lifecycle_buttons()

    def _reload_items(self):
        worklist_id = self._current_worklist_id()
        self.model.set_worklist(worklist_id, pending_only=self.pending_only.isChecked())
        self._update_status_label()

    def _update_status_label(self):
        w = self._current_worklist_row()
        if w is None:
            self.status_label.setText("No work lists yet -- import one from the Import tab.")
            return
        self.status_label.setText(
            f"Showing {self.model.total_count()} item(s) — status: {w['status']}, "
            f"{w['pending_items']} pending / {w['total_items']} staged, "
            f"{w['new_items']} new item(s) from this import (see New Items tab)."
        )

    def _update_lifecycle_buttons(self):
        w = self._current_worklist_row()
        self.complete_btn.setEnabled(w is not None and w["status"] == "open")
        self.reopen_btn.setEnabled(w is not None and w["status"] == "completed")

    # -- inline edit (pre-push) -----------------------------------------------

    def _on_cell_edit(self, worklist_item_id: int, field: str, raw_value) -> bool:
        if field == "proposed_price":
            if raw_value in (None, ""):
                repo.update_worklist_item_proposed(self.conn, worklist_item_id, {"proposed_price": None})
                return True
            price = repo.parse_price(raw_value)
            if price is None:
                QMessageBox.warning(self, "Invalid price", f"'{raw_value}' is not a valid price.")
                return False
            repo.update_worklist_item_proposed(self.conn, worklist_item_id, {"proposed_price": price})
        elif field == "proposed_status":
            value = repo.normalize_status(raw_value) if raw_value not in (None, "") else None
            repo.update_worklist_item_proposed(self.conn, worklist_item_id, {"proposed_status": value})
        else:
            repo.update_worklist_item_proposed(self.conn, worklist_item_id, {field: raw_value or None})
        return True

    # -- selection / push ------------------------------------------------------

    def _select_all(self):
        self.model.select_all_loaded()
        self._update_status_label()

    def _clear_selection(self):
        self.model.clear_selection()
        self._update_status_label()

    def _push_reminder(self):
        QMessageBox.information(
            self, "Pushed to inventory",
            "Changes pushed to inventory.\n\n"
            "Before uploading to DoorDash, export the FULL latest inventory "
            "(Dashboard -> Export Updated Inventory) -- never upload a partial file. "
            "After DoorDash approves the update, re-import the fresh master inventory "
            "export here so this app stays in sync.",
        )

    def _push_selected(self):
        worklist_id = self._current_worklist_id()
        if worklist_id is None:
            return
        ids = self.model.checked_ids()
        if not ids:
            QMessageBox.information(self, "No selection", "Check one or more rows first.")
            return
        if QMessageBox.question(self, "Confirm push", f"Push {len(ids)} change(s) to inventory?") != QMessageBox.Yes:
            return
        repo.push_worklist_items(self.conn, worklist_id, ids)
        self.refresh()
        self.on_data_changed()
        self._push_reminder()

    def _push_all(self):
        worklist_id = self._current_worklist_id()
        w = self._current_worklist_row()
        if worklist_id is None or w is None:
            return
        if w["pending_items"] == 0:
            QMessageBox.information(self, "Nothing to push", "No pending changes in this work list.")
            return
        if QMessageBox.question(
            self, "Confirm push", f"Push all {w['pending_items']} pending change(s) to inventory?"
        ) != QMessageBox.Yes:
            return
        repo.push_worklist_items(self.conn, worklist_id, None)
        self.refresh()
        self.on_data_changed()
        self._push_reminder()

    # -- lifecycle --------------------------------------------------------------

    def _mark_completed(self):
        worklist_id = self._current_worklist_id()
        if worklist_id is None:
            return
        summary = repo.worklist_pending_summary(self.conn, worklist_id)
        if summary["pending_changes"] or summary["incomplete_new_items"]:
            resp = QMessageBox.question(
                self, "Unfinished work in this work list",
                f"This work list still has {summary['pending_changes']} unpushed change(s) and "
                f"{summary['incomplete_new_items']} new item(s) missing required fields. "
                "Mark it completed anyway?",
            )
            if resp != QMessageBox.Yes:
                return
        repo.mark_worklist_completed(self.conn, worklist_id)
        self.refresh()

    def _reopen(self):
        worklist_id = self._current_worklist_id()
        if worklist_id is None:
            return
        repo.reopen_worklist(self.conn, worklist_id)
        self.refresh()
