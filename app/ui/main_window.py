"""Main window: tab navigation shell + active-store selector in the toolbar,
so every tab/import/export is scoped to the currently selected store."""
from PySide6.QtWidgets import QComboBox, QLabel, QMainWindow, QTabWidget, QToolBar, QWidget

from app import repo
from app.ui.dashboard_tab import DashboardTab
from app.ui.existing_items_tab import ExistingItemsTab
from app.ui.import_tab import ImportTab
from app.ui.new_items_tab import NewItemsTab
from app.ui.settings_tab import SettingsTab

ACTIVE_STORE_SETTING_KEY = "active_store_id"


class MainWindow(QMainWindow):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setWindowTitle("DoorDash Bulk Inventory Manager")
        self.resize(1200, 800)

        self._store_ids: list[int] = []
        self._active_store_pk: int | None = None

        toolbar = QToolBar("Store")
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel(" Active store: "))
        self.store_combo = QComboBox()
        self.store_combo.setMinimumWidth(240)
        self.store_combo.currentIndexChanged.connect(self._on_store_selected)
        toolbar.addWidget(self.store_combo)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.dashboard_tab = DashboardTab(
            conn, self.get_active_store_pk,
            go_to_import=lambda: self.tabs.setCurrentWidget(self.import_tab),
            go_to_existing=lambda: self.tabs.setCurrentWidget(self.existing_tab),
            go_to_new=lambda: self.tabs.setCurrentWidget(self.new_tab),
        )
        self.existing_tab = ExistingItemsTab(conn, self.get_active_store_pk, self.refresh_all)
        self.new_tab = NewItemsTab(conn, self.get_active_store_pk, self.refresh_all)
        self.import_tab = ImportTab(conn, self.get_active_store_pk, self.refresh_all)
        self.settings_tab = SettingsTab(conn, self._reload_stores)

        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.existing_tab, "Existing Items")
        self.tabs.addTab(self.new_tab, "New Items")
        self.tabs.addTab(self.import_tab, "Import")
        self.tabs.addTab(self.settings_tab, "Settings")

        self._reload_stores()

    def get_active_store_pk(self):
        return self._active_store_pk

    def _reload_stores(self):
        stores = repo.list_stores(self.conn)
        self._store_ids = [s["id"] for s in stores]
        self.store_combo.blockSignals(True)
        self.store_combo.clear()
        for s in stores:
            self.store_combo.addItem(f"{s['display_name']} ({s['business_id']}/{s['store_id']})")
        self.store_combo.blockSignals(False)

        saved = repo.get_setting(self.conn, ACTIVE_STORE_SETTING_KEY)
        saved_pk = int(saved) if saved and saved.isdigit() else None
        if saved_pk in self._store_ids:
            index = self._store_ids.index(saved_pk)
            self.store_combo.setCurrentIndex(index)
            self._active_store_pk = saved_pk
        elif self._store_ids:
            self.store_combo.setCurrentIndex(0)
            self._active_store_pk = self._store_ids[0]
        else:
            self._active_store_pk = None

        self.settings_tab.refresh()
        self.refresh_all()

    def _on_store_selected(self, index: int):
        if 0 <= index < len(self._store_ids):
            self._active_store_pk = self._store_ids[index]
            repo.set_setting(self.conn, ACTIVE_STORE_SETTING_KEY, str(self._active_store_pk))
            self.refresh_all()

    def refresh_all(self):
        self.dashboard_tab.refresh()
        self.existing_tab.refresh()
        self.new_tab.refresh()
