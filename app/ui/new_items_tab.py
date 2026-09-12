"""New Items tab: unmatched work-list rows, inline completion fields, and
the New SKU CSV export (spec section 3.4). Search/category/price filters
here work exactly like Existing Items: they narrow this tab's fixed view
(match_status='new'), never anything outside it -- select-all/export always
act on what's currently in view."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from app import export, repo
from app.ui.delegates import ComboBoxDelegate
from app.ui.models import InventoryTableModel

COLUMNS = [
    ("upc_padded", "UPC"),
    ("item_name", "Item Name"),
    ("category_l1", "Category 1"),
    ("category_l2", "Category 2"),
    ("default_price", "Price"),
    ("status", "Status"),
    ("currency", "Currency"),
]

EDITABLE_FIELDS = frozenset({"item_name", "category_l1", "category_l2", "default_price", "status", "currency"})

# +1 to account for the checkbox column the grid always prepends.
CATEGORY_L1_COL = [f for f, _ in COLUMNS].index("category_l1") + 1
CATEGORY_L2_COL = [f for f, _ in COLUMNS].index("category_l2") + 1

DEBOUNCE_MS = 200


class NewItemsTab(QWidget):
    def __init__(self, conn, get_active_store_pk, on_data_changed, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.get_active_store_pk = get_active_store_pk
        self.on_data_changed = on_data_changed

        self.model = InventoryTableModel(
            conn, None, COLUMNS, editable_fields=EDITABLE_FIELDS, on_cell_edit=self._on_cell_edit,
        )

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.apply_filters)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Items from your work list with no matching UPC in inventory. "
            "Fill in the required fields below, then export as a new-SKU CSV."
        ))

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

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.l1_delegate = ComboBoxDelegate(self._category_l1_options, self.table)
        self.l2_delegate = ComboBoxDelegate(self._category_l2_options, self.table)
        self.table.setItemDelegateForColumn(CATEGORY_L1_COL, self.l1_delegate)
        self.table.setItemDelegateForColumn(CATEGORY_L2_COL, self.l2_delegate)

        layout.addWidget(self.table)

        bottom_row = QHBoxLayout()
        self.status_label = QLabel("")
        bottom_row.addWidget(self.status_label)
        export_btn = QPushButton("Export New SKU CSV…")
        export_btn.clicked.connect(self._export)
        bottom_row.addWidget(export_btn)
        layout.addLayout(bottom_row)

    # -- filters ------------------------------------------------------------

    def _current_filters(self) -> dict:
        category = self.category_filter.currentText()
        return {
            "match_status": "new",
            "text1": self.filter1.text(),
            "text2": self.filter2.text(),
            "price_min": self.price_min.value() if self.price_min.value() > 0 else None,
            "price_max": self.price_max.value() if self.price_max.value() < 1_000_000 else None,
            "category_l1": category if category and category != "All" else None,
        }

    def apply_filters(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        self.model.store_pk = store_pk
        self.model.set_filters(self._current_filters())
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

    def _update_status_label(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        missing = repo.new_items_missing_required(self.conn, store_pk)
        self.status_label.setText(
            f"Showing {self.model.rowCount()} of {self.model.total_count()} new item(s) matching the current "
            f"filter. {len(missing)} new item(s) overall still missing required fields."
        )

    # -- inline edit --------------------------------------------------------

    def _on_cell_edit(self, item_id: int, field: str, raw_value) -> bool:
        if field == "default_price":
            price = repo.parse_price(raw_value)
            if price is None:
                QMessageBox.warning(self, "Invalid price", f"'{raw_value}' is not a valid price.")
                return False
            repo.update_new_item_fields(self.conn, item_id, {"default_price": price})
        elif field == "status":
            repo.update_new_item_fields(self.conn, item_id, {"status": repo.normalize_status(raw_value)})
        else:
            repo.update_new_item_fields(self.conn, item_id, {field: raw_value})
        self.on_data_changed()
        return True

    def _export(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export New SKU CSV", "new_skus.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            out_path = export.export_new_skus(self.conn, store_pk, path)
        except export.ExportValidationError as e:
            QMessageBox.warning(self, "Cannot export yet", str(e))
            return
        QMessageBox.information(self, "Exported", f"New SKUs exported to:\n{out_path}")
        self.refresh()
