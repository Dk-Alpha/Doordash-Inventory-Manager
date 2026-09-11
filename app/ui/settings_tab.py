"""Settings tab: store (business_id/store_id/currency) CRUD for multi-store
support, plus import history (spec section 4)."""
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from app import repo


class SettingsTab(QWidget):
    def __init__(self, conn, on_stores_changed, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.on_stores_changed = on_stores_changed

        layout = QVBoxLayout(self)

        store_box = QGroupBox("Stores")
        store_layout = QVBoxLayout(store_box)
        self.store_list = QListWidget()
        store_layout.addWidget(self.store_list)

        form = QFormLayout()
        self.business_id_input = QLineEdit()
        self.store_id_input = QLineEdit()
        self.display_name_input = QLineEdit()
        self.currency_input = QLineEdit("USD")
        form.addRow("Business ID", self.business_id_input)
        form.addRow("Store ID", self.store_id_input)
        form.addRow("Display name", self.display_name_input)
        form.addRow("Currency", self.currency_input)
        store_layout.addLayout(form)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Store")
        add_btn.clicked.connect(self._add_store)
        btn_row.addWidget(add_btn)
        remove_btn = QPushButton("Remove Selected Store")
        remove_btn.clicked.connect(self._remove_store)
        btn_row.addWidget(remove_btn)
        store_layout.addLayout(btn_row)
        layout.addWidget(store_box)

        history_box = QGroupBox("Import history (all stores)")
        history_layout = QVBoxLayout(history_box)
        self.history_list = QListWidget()
        history_layout.addWidget(self.history_list)
        layout.addWidget(history_box)

        self._store_ids: list[int] = []

    def refresh(self):
        self.store_list.clear()
        self._store_ids = []
        for store in repo.list_stores(self.conn):
            self.store_list.addItem(f"{store['display_name']} — {store['business_id']}/{store['store_id']} ({store['currency']})")
            self._store_ids.append(store["id"])

        self.history_list.clear()
        rows = self.conn.execute(
            """SELECT ib.*, s.display_name FROM import_batches ib
               JOIN stores s ON s.id = ib.store_id
               ORDER BY ib.imported_at DESC LIMIT 50"""
        ).fetchall()
        for r in rows:
            self.history_list.addItem(f"[{r['imported_at']}] {r['display_name']} — {r['kind']} — {r['filename']}")

    def _add_store(self):
        business_id = self.business_id_input.text().strip()
        store_id = self.store_id_input.text().strip()
        display_name = self.display_name_input.text().strip() or f"{business_id}/{store_id}"
        currency = self.currency_input.text().strip() or "USD"
        if not business_id or not store_id:
            QMessageBox.warning(self, "Missing fields", "Business ID and Store ID are required.")
            return
        try:
            repo.create_store(self.conn, business_id, store_id, display_name, currency)
        except Exception as e:
            QMessageBox.critical(self, "Could not add store", str(e))
            return
        self.business_id_input.clear()
        self.store_id_input.clear()
        self.display_name_input.clear()
        self.currency_input.setText("USD")
        self.refresh()
        self.on_stores_changed()

    def _remove_store(self):
        row = self.store_list.currentRow()
        if row < 0:
            return
        store_pk = self._store_ids[row]
        resp = QMessageBox.question(
            self, "Remove store",
            "This deletes the store and all of its inventory data. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        repo.delete_store(self.conn, store_pk)
        self.refresh()
        self.on_stores_changed()
