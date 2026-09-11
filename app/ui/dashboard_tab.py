"""Dashboard tab: quick stats + the two big import actions (spec section 4)."""
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app import export as export_mod
from app import repo


class DashboardTab(QWidget):
    def __init__(self, conn, get_active_store_pk, go_to_import, go_to_existing, go_to_new, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.get_active_store_pk = get_active_store_pk

        layout = QVBoxLayout(self)
        self.store_label = QLabel("")
        layout.addWidget(self.store_label)

        grid = QGridLayout()
        self.total_label = QLabel("-")
        self.active_label = QLabel("-")
        self.inactive_label = QLabel("-")
        self.pending_label = QLabel("-")
        self.new_label = QLabel("-")
        self.last_import_label = QLabel("-")
        for row, (caption, widget) in enumerate([
            ("Total SKUs", self.total_label),
            ("Active", self.active_label),
            ("Inactive", self.inactive_label),
            ("Pending unsaved changes", self.pending_label),
            ("New items awaiting SKU export", self.new_label),
            ("Last import", self.last_import_label),
        ]):
            grid.addWidget(QLabel(caption + ":"), row, 0)
            grid.addWidget(widget, row, 1)
        layout.addLayout(grid)

        action_row = QVBoxLayout()
        import_master_btn = QPushButton("Import Inventory")
        import_master_btn.clicked.connect(go_to_import)
        action_row.addWidget(import_master_btn)
        import_worklist_btn = QPushButton("Import Work List")
        import_worklist_btn.clicked.connect(go_to_import)
        action_row.addWidget(import_worklist_btn)
        view_existing_btn = QPushButton("View Existing Items")
        view_existing_btn.clicked.connect(go_to_existing)
        action_row.addWidget(view_existing_btn)
        view_new_btn = QPushButton("View New Items")
        view_new_btn.clicked.connect(go_to_new)
        action_row.addWidget(view_new_btn)
        layout.addLayout(action_row)

        export_row = QVBoxLayout()
        export_inventory_btn = QPushButton("Export Updated Inventory…")
        export_inventory_btn.clicked.connect(self._export_inventory)
        export_row.addWidget(export_inventory_btn)
        layout.addLayout(export_row)

        layout.addStretch()

    def refresh(self):
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            self.store_label.setText("No store selected — create one in Settings.")
            return
        store = self.conn.execute("SELECT * FROM stores WHERE id = ?", (store_pk,)).fetchone()
        self.store_label.setText(f"<b>Store:</b> {store['display_name']} ({store['business_id']}/{store['store_id']})")
        stats = repo.dashboard_stats(self.conn, store_pk)
        self.total_label.setText(str(stats["total"]))
        self.active_label.setText(str(stats["active"]))
        self.inactive_label.setText(str(stats["inactive"]))
        self.pending_label.setText(str(stats["pending_changes"]))
        self.new_label.setText(str(stats["new_items"]))
        self.last_import_label.setText(stats["last_import"] or "never")

    def _export_inventory(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        store_pk = self.get_active_store_pk()
        if store_pk is None:
            return
        summary = export_mod.export_summary(self.conn, store_pk)
        proceed = QMessageBox.question(
            self, "Confirm export",
            f"{summary['activated']} activated, {summary['deactivated']} deactivated, "
            f"{summary['price_changes']} price change(s). {summary['new_skus']} new SKU(s) will need a separate export.\n\n"
            "Continue?",
        )
        if proceed != QMessageBox.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Updated Inventory", "inventory_export.csv", "CSV (*.csv)")
        if not path:
            return
        out_path = export_mod.export_updated_inventory(self.conn, store_pk, path)
        QMessageBox.information(self, "Exported", f"Updated inventory exported to:\n{out_path}")
