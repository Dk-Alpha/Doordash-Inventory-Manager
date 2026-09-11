"""Import tab: pick Master Inventory or Work List, confirm column mapping,
write to the DB. Reused for both import kinds (spec section 4)."""
from PySide6.QtWidgets import (
    QFileDialog, QLabel, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from app import importers, repo
from app.ui.column_mapping_dialog import ColumnMappingDialog


class ImportTab(QWidget):
    def __init__(self, conn, get_active_store_pk, on_import_done, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.get_active_store_pk = get_active_store_pk
        self.on_import_done = on_import_done

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Import Master Inventory</b> — your full current DoorDash export."
        ))
        master_btn = QPushButton("Import Master Inventory…")
        master_btn.clicked.connect(lambda: self._run_import("master"))
        layout.addWidget(master_btn)

        layout.addWidget(QLabel(
            "<b>Import Work List</b> — a CSV/XLSX of items to change, keyed by UPC. "
            "Matches will update existing items; unmatched rows go to New Items."
        ))
        worklist_btn = QPushButton("Import Work List…")
        worklist_btn.clicked.connect(lambda: self._run_import("worklist"))
        layout.addWidget(worklist_btn)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

    def _require_store(self) -> int | None:
        store_pk = self.get_active_store_pk()
        if store_pk is None:
            QMessageBox.warning(self, "No store selected", "Create/select a store in Settings first.")
            return None
        return store_pk

    def _run_import(self, kind: str):
        store_pk = self._require_store()
        if store_pk is None:
            return

        if kind == "master" and repo.has_unsaved_changes(self.conn, store_pk):
            resp = QMessageBox.question(
                self, "Unsaved changes",
                "This store has unsaved edits (price/status changes not yet exported). "
                "Re-importing the master inventory may overwrite them. Continue?",
            )
            if resp != QMessageBox.Yes:
                return

        path, _ = QFileDialog.getOpenFileName(self, "Select file", "", "CSV/Excel (*.csv *.xlsx *.xls)")
        if not path:
            return

        try:
            df = importers.read_table(path)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", f"Could not read file:\n{e}")
            return

        header_row_skipped = df.attrs.get("header_row_skipped")
        if header_row_skipped:
            self.log.append(
                f"Detected column headers on row {header_row_skipped + 1} of {path} "
                f"(skipped {header_row_skipped} instruction/annotation row(s) above it)."
            )

        skipped_lines = df.attrs.get("skipped_lines")
        if skipped_lines:
            self.log.append(
                f"WARNING: {len(skipped_lines)} malformed row(s) in {path} could not be parsed and were skipped:"
            )
            for line in skipped_lines:
                self.log.append(f"  {line.strip()}")

        headers = list(df.columns)
        fingerprint = importers.header_fingerprint(headers)
        mapping = repo.load_mapping_preset(self.conn, fingerprint)

        if mapping is None:
            suggested = importers.detect_columns(headers, kind=kind)
            dialog = ColumnMappingDialog(headers, suggested, kind=kind, parent=self)
            if dialog.exec() != ColumnMappingDialog.Accepted:
                return
            mapping = dialog.result_mapping()
            repo.save_mapping_preset(self.conn, fingerprint, kind, mapping)

        rename = {source_header: field for field, source_header in mapping.items()}
        renamed = df.rename(columns=rename)
        rows = renamed.to_dict(orient="records")

        try:
            if kind == "master":
                repo.import_master(self.conn, store_pk, rows, path, mapping)
                self.log.append(f"Imported {len(rows)} master inventory rows from {path}")
            else:
                _, result = repo.import_worklist(self.conn, store_pk, rows, path, mapping)
                self.log.append(
                    f"Work list import: {len(result.existing)} matched existing items, "
                    f"{len(result.new)} new items, {len(result.skipped_blank_upc)} skipped (no UPC)."
                )
                if result.inventory_duplicates:
                    self.log.append(f"WARNING: {len(result.inventory_duplicates)} duplicate UPC(s) in inventory.")
                if result.worklist_duplicates:
                    self.log.append(f"WARNING: {len(result.worklist_duplicates)} duplicate UPC(s) in work list.")
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return

        self.on_import_done()
