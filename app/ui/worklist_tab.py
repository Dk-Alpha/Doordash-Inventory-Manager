"""Work Lists tab: review and edit a work list's staged changes to existing
inventory before pushing them, and track each work list's lifecycle
(open -> completed). Unmatched (new) rows from the same import still live in
the New Items tab -- this tab only handles the matched/existing side, which
previously had no review step at all (matches got written straight to
inventory during import).

Search/category/price filters here work exactly like Existing Items: they
narrow the current work list's staged rows (the "view"), and every bulk
action (select-all, push-all) acts only on what's currently in that
narrowed view, never on the whole work list regardless of what's on screen."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app import repo
from app.ui.bulk_price_dialog import BulkPriceDialog
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

DEBOUNCE_MS = 200


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

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._reload_items)

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
        self.pending_only.stateChanged.connect(self._debounce.start)
        picker_row.addWidget(self.pending_only)
        picker_row.addStretch()
        layout.addLayout(picker_row)

        filter_row = QHBoxLayout()
        self.filter1 = QLineEdit(placeholderText="Search item name / UPC / SKU…")
        self.filter1.textChanged.connect(self._debounce.start)
        filter_row.addWidget(self.filter1)
        self.filter2 = QLineEdit(placeholderText="Narrow further…")
        self.filter2.textChanged.connect(self._debounce.start)
        filter_row.addWidget(self.filter2)
        layout.addLayout(filter_row)

        adv_box = QGroupBox("Advanced filters")
        adv_layout = QHBoxLayout(adv_box)
        adv_layout.addWidget(QLabel("Price min"))
        self.price_min = QDoubleSpinBox(); self.price_min.setRange(0, 1_000_000); self.price_min.setSpecialValueText("")
        self.price_min.valueChanged.connect(self._debounce.start)
        adv_layout.addWidget(self.price_min)
        adv_layout.addWidget(QLabel("Price max"))
        self.price_max = QDoubleSpinBox(); self.price_max.setRange(0, 1_000_000); self.price_max.setValue(1_000_000)
        self.price_max.valueChanged.connect(self._debounce.start)
        adv_layout.addWidget(self.price_max)
        adv_layout.addWidget(QLabel("Category"))
        self.category_filter = QComboBox()
        self.category_filter.addItem("All")
        self.category_filter.currentIndexChanged.connect(self._debounce.start)
        adv_layout.addWidget(self.category_filter)
        layout.addWidget(adv_box)

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
        select_all_btn = QPushButton("Select All Shown")
        select_all_btn.setToolTip("Selects every pending row currently shown (i.e. matching the search/filters above).")
        select_all_btn.clicked.connect(self._select_all)
        bulk_row.addWidget(select_all_btn)
        clear_btn = QPushButton("Clear Selection")
        clear_btn.clicked.connect(self._clear_selection)
        bulk_row.addWidget(clear_btn)
        push_selected_btn = QPushButton("Push Selected to Inventory")
        push_selected_btn.clicked.connect(self._push_selected)
        bulk_row.addWidget(push_selected_btn)
        push_all_btn = QPushButton("Push All Matching Filter")
        push_all_btn.setToolTip("Pushes every pending row matching the current search/filters -- not the whole work list.")
        push_all_btn.clicked.connect(self._push_all)
        bulk_row.addWidget(push_all_btn)
        layout.addLayout(bulk_row)

        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("Set proposed values for the selected rows:"))
        activate_btn = QPushButton("Activate Selected")
        activate_btn.clicked.connect(lambda: self._bulk_status("active"))
        edit_row.addWidget(activate_btn)
        deactivate_btn = QPushButton("Deactivate Selected")
        deactivate_btn.clicked.connect(lambda: self._bulk_status("inactive"))
        edit_row.addWidget(deactivate_btn)
        price_btn = QPushButton("Bulk Price Change…")
        price_btn.clicked.connect(self._bulk_price)
        edit_row.addWidget(price_btn)
        layout.addLayout(edit_row)

        lifecycle_row = QHBoxLayout()
        self.complete_btn = QPushButton("Mark Work List Completed")
        self.complete_btn.clicked.connect(self._mark_completed)
        lifecycle_row.addWidget(self.complete_btn)
        self.reopen_btn = QPushButton("Reopen Work List")
        self.reopen_btn.clicked.connect(self._reopen)
        lifecycle_row.addWidget(self.reopen_btn)
        lifecycle_row.addStretch()
        layout.addLayout(lifecycle_row)

    # -- filters ----------------------------------------------------------

    def _current_filters(self) -> dict:
        category = self.category_filter.currentText()
        return {
            "pending_only": self.pending_only.isChecked(),
            "text1": self.filter1.text(),
            "text2": self.filter2.text(),
            "price_min": self.price_min.value() if self.price_min.value() > 0 else None,
            "price_max": self.price_max.value() if self.price_max.value() < 1_000_000 else None,
            "category_l1": category if category and category != "All" else None,
        }

    def _refresh_category_filter_options(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        categories = repo.distinct_category_values(self.conn, store_pk, "category_l1")
        current = self.category_filter.currentText()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All")
        self.category_filter.addItems(categories)
        idx = self.category_filter.findText(current)
        self.category_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.category_filter.blockSignals(False)

    # -- category dropdowns (proposed-value cell editors) -------------------

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
        self._refresh_category_filter_options()
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
        self.model.set_worklist(worklist_id, self._current_filters())
        self._update_status_label()

    def _update_status_label(self):
        w = self._current_worklist_row()
        if w is None:
            self.status_label.setText("No work lists yet -- import one from the Import tab.")
            return
        self.status_label.setText(
            f"Showing {self.model.total_count()} item(s) matching the current filter — "
            f"status: {w['status']}, {w['pending_items']} pending / {w['total_items']} staged overall, "
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

    # -- bulk edit (staged proposed values, pre-push) -------------------------

    def _confirm(self, ids: list[int], action_desc: str) -> bool:
        if not ids:
            QMessageBox.information(self, "No selection", "Check one or more rows first.")
            return False
        rows = self.model.rows_by_ids(set(ids))
        sample = ", ".join((r["item_name"] or "(unnamed)") for r in rows[:5])
        more = f" and {len(ids) - 5} more" if len(ids) > 5 else ""
        resp = QMessageBox.question(
            self, "Confirm bulk change",
            f"{action_desc} for {len(ids)} item(s) in this work list?\n\nSample: {sample}{more}",
        )
        return resp == QMessageBox.Yes

    def _bulk_status(self, to_status: str):
        ids = self.model.checked_ids()
        if not self._confirm(ids, f"Set proposed status to '{to_status}'"):
            return
        repo.bulk_set_worklist_items_status(self.conn, ids, to_status)
        self._reload_items()

    def _bulk_price(self):
        ids = self.model.checked_ids()
        if not ids:
            QMessageBox.information(self, "No selection", "Check one or more rows first.")
            return
        dialog = BulkPriceDialog(len(ids), parent=self)
        if dialog.exec() != BulkPriceDialog.Accepted:
            return
        if not self._confirm(ids, f"Set proposed price ({dialog.mode()}, {dialog.value()})"):
            return
        repo.bulk_set_worklist_items_price(self.conn, ids, dialog.mode(), dialog.value())
        self._reload_items()

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
        """Pushes every pending row matching the *current* search/filters --
        deliberately not repo.push_worklist_items(worklist_id, None), which
        would reach past whatever's actually on screen and push the entire
        work list regardless of an active search/filter."""
        worklist_id = self._current_worklist_id()
        if worklist_id is None:
            return
        filters = self._current_filters()
        filters["pending_only"] = True
        ids = repo.list_worklist_item_ids(self.conn, worklist_id, filters)
        if not ids:
            QMessageBox.information(self, "Nothing to push", "No pending changes match the current filter.")
            return
        if QMessageBox.question(
            self, "Confirm push", f"Push {len(ids)} pending change(s) matching the current filter to inventory?"
        ) != QMessageBox.Yes:
            return
        repo.push_worklist_items(self.conn, worklist_id, ids)
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
