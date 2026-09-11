"""QAbstractTableModel implementations backing the two data grids. Rows are
fetched from SQLite in pages (fetchMore) instead of loading everything into
memory up front, so a 20k+ row inventory stays responsive."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from app import repo

PAGE_SIZE = 200


class InventoryTableModel(QAbstractTableModel):
    def __init__(self, conn, store_pk: int, columns: list[tuple[str, str]], parent=None,
                 editable_fields: frozenset = frozenset(), on_cell_edit=None):
        """columns: list of (db_field, header_label).
        editable_fields: db_field names the grid allows double-click editing on.
        on_cell_edit(item_id, field, raw_text) -> bool: performs the actual
        write (via repo) and returns whether it succeeded; the model re-reads
        that row from the DB afterwards rather than guessing the new value."""
        super().__init__(parent)
        self.conn = conn
        self.store_pk = store_pk
        self.columns = columns
        self.filters: dict = {}
        self._rows: list = []
        self._total = 0
        self._checked: set[int] = set()
        self.editable_fields = editable_fields
        self.on_cell_edit = on_cell_edit

    # -- data loading -----------------------------------------------------

    def set_store(self, store_pk: int):
        self.store_pk = store_pk
        self.refresh()

    def set_filters(self, filters: dict):
        self.filters = filters
        self.refresh()

    def refresh(self):
        self.beginResetModel()
        self._rows = []
        self._checked = set()
        self._total = repo.count_items(self.conn, self.store_pk, self.filters)
        self._rows = list(repo.list_items(self.conn, self.store_pk, self.filters, limit=PAGE_SIZE, offset=0))
        self.endResetModel()

    def canFetchMore(self, parent=QModelIndex()):
        return len(self._rows) < self._total

    def fetchMore(self, parent=QModelIndex()):
        more = repo.list_items(self.conn, self.store_pk, self.filters, limit=PAGE_SIZE, offset=len(self._rows))
        if not more:
            return
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows) + len(more) - 1)
        self._rows.extend(more)
        self.endInsertRows()

    # -- Qt model interface -------------------------------------------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns) + 1  # +1 for the checkbox column

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if index.column() == 0:
            if role == Qt.CheckStateRole:
                return Qt.Checked if row["id"] in self._checked else Qt.Unchecked
            return None
        field, _ = self.columns[index.column() - 1]
        if role in (Qt.DisplayRole, Qt.EditRole):
            value = row[field]
            if field == "default_price" and value is not None:
                return f"{value:.2f}"
            if field == "change_flag":
                return "Yes" if value else ""
            return value if value is not None else ""
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        row = self._rows[index.row()]
        if index.column() == 0 and role == Qt.CheckStateRole:
            if value == Qt.Checked:
                self._checked.add(row["id"])
            else:
                self._checked.discard(row["id"])
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            return True

        if index.column() > 0 and role == Qt.EditRole:
            field, _ = self.columns[index.column() - 1]
            if field not in self.editable_fields or self.on_cell_edit is None:
                return False
            ok = self.on_cell_edit(row["id"], field, value)
            if not ok:
                return False
            fresh = self.conn.execute("SELECT * FROM inventory_items WHERE id = ?", (row["id"],)).fetchone()
            if fresh is not None:
                self._rows[index.row()] = fresh
                self.dataChanged.emit(
                    self.index(index.row(), 1), self.index(index.row(), self.columnCount() - 1)
                )
            return True
        return False

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            return base | Qt.ItemIsUserCheckable
        field, _ = self.columns[index.column() - 1]
        if field in self.editable_fields:
            return base | Qt.ItemIsEditable
        return base

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        if section == 0:
            return ""
        return self.columns[section - 1][1]

    # -- helpers ------------------------------------------------------------

    def row_at(self, row_index: int):
        return self._rows[row_index]

    def checked_ids(self) -> list[int]:
        return list(self._checked)

    def check_all_loaded(self, checked: bool):
        self.beginResetModel()
        if checked:
            self._checked = {r["id"] for r in self._rows}
        else:
            self._checked = set()
        self.endResetModel()

    def total_count(self) -> int:
        return self._total
