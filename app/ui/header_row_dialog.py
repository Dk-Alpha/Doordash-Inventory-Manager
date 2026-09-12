"""Header-row confirmation dialog, shown before column mapping. Uploaded
files aren't guaranteed to have column names on row 1 or 2 -- this shows a
raw preview of the file and lets the user point at (or accept an
auto-detected guess of) which row the real column names are on, before any
column-name matching happens."""
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QSpinBox, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)


class HeaderRowDialog(QDialog):
    def __init__(self, rows: list[list[str]], guessed_row: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm header row")
        self.setMinimumSize(640, 420)
        self.rows = rows

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "This is a preview of the raw file. Column names don't have to be on the "
            "first row -- pick which row they're actually on below. Everything below "
            "that row is treated as data."
        ))

        max_cols = max((len(r) for r in rows), default=0)
        table = QTableWidget(len(rows), max_cols)
        table.setHorizontalHeaderLabels([f"Col {i + 1}" for i in range(max_cols)])
        for r, row in enumerate(rows):
            table.setVerticalHeaderItem(r, QTableWidgetItem(f"Row {r + 1}"))
            for c in range(max_cols):
                value = row[c] if c < len(row) else ""
                table.setItem(r, c, QTableWidgetItem(value))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.cellClicked.connect(lambda r, _c: self.row_spin.setValue(r + 1))
        layout.addWidget(table)

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Column names start at row:"))
        self.row_spin = QSpinBox()
        self.row_spin.setMinimum(1)
        self.row_spin.setMaximum(max(len(rows), 1))
        self.row_spin.setValue(min(guessed_row + 1, max(len(rows), 1)))
        picker_row.addWidget(self.row_spin)
        picker_row.addWidget(QLabel("(auto-detected guess is pre-filled; click a row above or change the number)"))
        picker_row.addStretch()
        layout.addLayout(picker_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def header_row_index(self) -> int:
        """0-based index of the confirmed header row."""
        return self.row_spin.value() - 1
