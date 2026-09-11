"""Reusable column-mapping confirmation dialog, shown before any import is
written to the database (spec section 6.1 — this is the fix for "column
names differ across exports")."""
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import QFormLayout

from app import importers

NONE_OPTION = "-- none --"

MASTER_FIELDS = [f for f in importers.MASTER_FIELD_ALIASES if f not in ("business_id", "store_id")]
WORKLIST_FIELDS = list(importers.WORKLIST_FIELD_ALIASES.keys())

FIELD_LABELS = {
    "upc_id": "UPC (required)",
    "sku_id": "SKU",
    "item_name": "Item name",
    "category_l1": "Category 1",
    "category_l2": "Category 2",
    "default_price": "Price",
    "status": "Status",
    "currency": "Currency",
    "new_price": "New price",
    "new_status": "New status",
}


class ColumnMappingDialog(QDialog):
    def __init__(self, headers: list[str], suggested: dict[str, str], kind: str = "master", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm column mapping")
        self.setMinimumWidth(420)
        self.headers = headers
        self.kind = kind
        self.fields = MASTER_FIELDS if kind == "master" else WORKLIST_FIELDS
        self.required = importers.REQUIRED_MASTER_FIELDS if kind == "master" else importers.REQUIRED_WORKLIST_FIELDS
        self.combo_by_field: dict[str, QComboBox] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "We matched these columns automatically. Confirm or fix them before import."
        ))

        form = QFormLayout()
        for field in self.fields:
            combo = QComboBox()
            combo.addItem(NONE_OPTION)
            combo.addItems(headers)
            guess = suggested.get(field)
            if guess and guess in headers:
                combo.setCurrentText(guess)
            label = FIELD_LABELS.get(field, field)
            form.addRow(label, combo)
            self.combo_by_field[field] = combo
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._result_mapping: dict[str, str] = {}

    def _on_accept(self):
        mapping = {}
        for field, combo in self.combo_by_field.items():
            value = combo.currentText()
            if value != NONE_OPTION:
                mapping[field] = value
        missing_required = [f for f in self.required if f not in mapping]
        if missing_required:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Missing required column",
                f"Please map a column for: {', '.join(missing_required)}",
            )
            return
        self._result_mapping = mapping
        self.accept()

    def result_mapping(self) -> dict[str, str]:
        return self._result_mapping
