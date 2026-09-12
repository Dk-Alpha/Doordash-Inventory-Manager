"""New Items tab: unmatched work-list rows, inline completion fields, and
the New SKU CSV export (spec section 3.4)."""
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableView, QVBoxLayout, QWidget,
)

from PySide6.QtCore import Qt

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


class NewItemsTab(QWidget):
    def __init__(self, conn, get_active_store_pk, on_data_changed, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.get_active_store_pk = get_active_store_pk
        self.on_data_changed = on_data_changed

        self.model = InventoryTableModel(
            conn, None, COLUMNS, editable_fields=EDITABLE_FIELDS, on_cell_edit=self._on_cell_edit,
        )

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Items from your work list with no matching UPC in inventory. "
            "Fill in the required fields below, then export as a new-SKU CSV."
        ))

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
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        self.model.store_pk = store_pk
        self.model.set_filters({"match_status": "new"})
        missing = repo.new_items_missing_required(self.conn, store_pk)
        if missing:
            self.status_label.setText(
                f"{self.model.total_count()} new item(s). {len(missing)} still missing required fields."
            )
        else:
            self.status_label.setText(f"{self.model.total_count()} new item(s). All required fields complete.")

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
