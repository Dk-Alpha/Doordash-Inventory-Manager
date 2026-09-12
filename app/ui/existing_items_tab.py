"""Existing Items tab: two-stage filter, Active/Inactive segmentation,
inline edit, bulk actions with confirmation + undo (spec section 3.3/3.5/3.6)."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app import repo
from app.ui.bulk_price_dialog import BulkPriceDialog
from app.ui.delegates import ComboBoxDelegate
from app.ui.models import InventoryTableModel

COLUMNS = [
    ("upc_padded", "UPC"),
    ("sku_id", "SKU"),
    ("item_name", "Item Name"),
    ("category_l1", "Category 1"),
    ("category_l2", "Category 2"),
    ("default_price", "Price"),
    ("status", "Status"),
    ("change_flag", "Changed"),
]

# +1 to account for the checkbox column the grid always prepends.
CATEGORY_L1_COL = [f for f, _ in COLUMNS].index("category_l1") + 1
CATEGORY_L2_COL = [f for f, _ in COLUMNS].index("category_l2") + 1

DEBOUNCE_MS = 200


class ExistingItemsTab(QWidget):
    def __init__(self, conn, get_active_store_pk, on_data_changed, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.get_active_store_pk = get_active_store_pk
        self.on_data_changed = on_data_changed

        self.model = InventoryTableModel(
            conn, None, COLUMNS,
            editable_fields=frozenset({"status", "default_price", "category_l1", "category_l2"}),
            on_cell_edit=self._on_cell_edit,
        )
        self.model.dataChanged.connect(lambda *args: self._update_status_label())

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.apply_filters)

        layout = QVBoxLayout(self)

        filter_row = QHBoxLayout()
        self.status_combo = QComboBox()
        self.status_combo.addItems(["All", "Active", "Inactive"])
        self.status_combo.currentIndexChanged.connect(self._debounce.start)
        filter_row.addWidget(QLabel("Status:"))
        filter_row.addWidget(self.status_combo)

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
        self.changed_only = QCheckBox("Changed this session only")
        self.changed_only.stateChanged.connect(self._debounce.start)
        adv_layout.addWidget(self.changed_only)
        adv_layout.addWidget(QLabel("Category"))
        self.category_filter = QComboBox()
        self.category_filter.addItem("All")
        self.category_filter.currentIndexChanged.connect(self._debounce.start)
        adv_layout.addWidget(self.category_filter)
        layout.addWidget(adv_box)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.l1_delegate = ComboBoxDelegate(self._category_l1_options, self.table)
        self.l2_delegate = ComboBoxDelegate(self._category_l2_options, self.table)
        self.table.setItemDelegateForColumn(CATEGORY_L1_COL, self.l1_delegate)
        self.table.setItemDelegateForColumn(CATEGORY_L2_COL, self.l2_delegate)

        layout.addWidget(self.table)

        bulk_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All Loaded")
        select_all_btn.clicked.connect(self._select_all_loaded)
        bulk_row.addWidget(select_all_btn)
        select_all_filtered_btn = QPushButton("Select All Matching Filter")
        select_all_filtered_btn.setToolTip(
            "Selects every item matching the current filter, even ones not yet scrolled into view."
        )
        select_all_filtered_btn.clicked.connect(self._select_all_matching_filter)
        bulk_row.addWidget(select_all_filtered_btn)
        clear_btn = QPushButton("Clear Selection")
        clear_btn.clicked.connect(self._clear_selection)
        bulk_row.addWidget(clear_btn)

        activate_btn = QPushButton("Activate Selected")
        activate_btn.clicked.connect(lambda: self._bulk_status("active"))
        bulk_row.addWidget(activate_btn)
        deactivate_btn = QPushButton("Deactivate Selected")
        deactivate_btn.clicked.connect(lambda: self._bulk_status("inactive"))
        bulk_row.addWidget(deactivate_btn)
        price_btn = QPushButton("Bulk Price Change…")
        price_btn.clicked.connect(self._bulk_price)
        bulk_row.addWidget(price_btn)
        undo_btn = QPushButton("Undo Last Action")
        undo_btn.clicked.connect(self._undo_last)
        bulk_row.addWidget(undo_btn)
        layout.addLayout(bulk_row)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    # -- filters --------------------------------------------------------

    def _current_filters(self) -> dict:
        status_map = {"All": "all", "Active": "active", "Inactive": "inactive"}
        category = self.category_filter.currentText()
        return {
            "status": status_map[self.status_combo.currentText()],
            "text1": self.filter1.text(),
            "text2": self.filter2.text(),
            "price_min": self.price_min.value() if self.price_min.value() > 0 else None,
            "price_max": self.price_max.value() if self.price_max.value() < 1_000_000 else None,
            "changed_only": self.changed_only.isChecked(),
            "match_status": "existing",
            "category_l1": category if category and category != "All" else None,
        }

    def apply_filters(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        self.model.store_pk = store_pk
        self.model.set_filters(self._current_filters())
        self._update_status_label()

    def _update_status_label(self):
        self.status_label.setText(
            f"Showing {self.model.rowCount()} of {self.model.total_count()} items — "
            f"{self.model.checked_count()} selected in total (across any filter)"
        )

    def _select_all_loaded(self):
        self.model.select_all_loaded()
        self._update_status_label()

    def _select_all_matching_filter(self):
        self.model.select_all_matching_filter()
        self._update_status_label()

    def _clear_selection(self):
        self.model.clear_selection()
        self._update_status_label()

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

    def _category_l1_options(self, index) -> list[str]:
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return []
        return repo.distinct_category_values(self.conn, store_pk, "category_l1")

    def _category_l2_options(self, index) -> list[str]:
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return []
        l1_value = index.sibling(index.row(), CATEGORY_L1_COL).data(Qt.EditRole) or None
        return repo.distinct_category_values(self.conn, store_pk, "category_l2", parent_l1=l1_value)

    def refresh(self):
        self._refresh_category_filter_options()
        self.apply_filters()

    # -- inline edit ------------------------------------------------------

    def _on_cell_edit(self, item_id: int, field: str, raw_value) -> bool:
        if field == "status":
            new_status = repo.normalize_status(raw_value)
            repo.apply_bulk_status(self.conn, [item_id], new_status)
        elif field == "default_price":
            price = repo.parse_price(raw_value)
            if price is None:
                QMessageBox.warning(self, "Invalid price", f"'{raw_value}' is not a valid price.")
                return False
            repo.apply_bulk_price(self.conn, [item_id], "set", price)
        elif field in ("category_l1", "category_l2"):
            repo.update_new_item_fields(self.conn, item_id, {field: raw_value})
        else:
            return False
        self.on_data_changed()
        return True

    # -- bulk actions -----------------------------------------------------

    def _confirm(self, ids: list[int], action_desc: str) -> bool:
        if not ids:
            QMessageBox.information(self, "No selection", "Check one or more rows first.")
            return False
        placeholders = ",".join("?" * len(ids))
        names = [r["item_name"] or "(unnamed)" for r in self.conn.execute(
            f"SELECT item_name FROM inventory_items WHERE id IN ({placeholders}) LIMIT 5", ids
        ).fetchall()]
        sample = ", ".join(names)
        more = f" and {len(ids) - 5} more" if len(ids) > 5 else ""
        resp = QMessageBox.question(
            self, "Confirm bulk action",
            f"{action_desc} for {len(ids)} item(s)?\n\nSample: {sample}{more}",
        )
        return resp == QMessageBox.Yes

    def _bulk_status(self, to_status: str):
        ids = self.model.checked_ids()
        if not self._confirm(ids, f"Set status to '{to_status}'"):
            return
        repo.apply_bulk_status(self.conn, ids, to_status)
        self.on_data_changed()
        self.refresh()

    def _bulk_price(self):
        ids = self.model.checked_ids()
        if not ids:
            QMessageBox.information(self, "No selection", "Check one or more rows first.")
            return
        dialog = BulkPriceDialog(len(ids), parent=self)
        if dialog.exec() != BulkPriceDialog.Accepted:
            return
        if not self._confirm(ids, f"Apply price change ({dialog.mode()}, {dialog.value()})"):
            return
        repo.apply_bulk_price(self.conn, ids, dialog.mode(), dialog.value())
        self.on_data_changed()
        self.refresh()

    def _undo_last(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        batch_id = repo.last_batch_action_id(self.conn, store_pk)
        if batch_id is None:
            QMessageBox.information(self, "Nothing to undo", "No bulk actions recorded this session.")
            return
        count = repo.undo_batch(self.conn, batch_id)
        QMessageBox.information(self, "Undone", f"Reverted {count} field change(s).")
        self.on_data_changed()
        self.refresh()
