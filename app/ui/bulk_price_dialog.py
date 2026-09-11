"""Bulk price-change dialog: explicit mode dropdown (spec section 5, price
bulk-edit ambiguity) so "set to $X" is never confused with "+X%"."""
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout,
)

MODES = [
    ("set", "Set exact price to"),
    ("inc_pct", "Increase by %"),
    ("dec_pct", "Decrease by %"),
    ("inc_amt", "Increase by amount"),
    ("dec_amt", "Decrease by amount"),
]


class BulkPriceDialog(QDialog):
    def __init__(self, affected_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bulk price change")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"This will affect {affected_count} item(s)."))

        form = QFormLayout()
        self.mode_combo = QComboBox()
        for key, label in MODES:
            self.mode_combo.addItem(label, key)
        form.addRow("Action", self.mode_combo)

        self.value_spin = QDoubleSpinBox()
        self.value_spin.setDecimals(2)
        self.value_spin.setRange(-100000, 100000)
        form.addRow("Value", self.value_spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def mode(self) -> str:
        return self.mode_combo.currentData()

    def value(self) -> float:
        return self.value_spin.value()
