"""Reusable grid cell editors."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate


class ComboBoxDelegate(QStyledItemDelegate):
    """Dropdown cell editor backed by a dynamic option list. Editable, so a
    genuinely new category (or any other value) can still be typed in --
    the dropdown is a convenience, not a hard whitelist."""

    def __init__(self, get_options, parent=None):
        """get_options(index) -> list[str], called fresh each time the
        editor opens so the choices reflect current data (e.g. a category_l2
        list narrowed by that row's current category_l1)."""
        super().__init__(parent)
        self.get_options = get_options

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(True)
        options = self.get_options(index)
        combo.addItems(options)
        return combo

    def setEditorData(self, editor, index):
        value = index.data(Qt.EditRole) or ""
        pos = editor.findText(value)
        if pos >= 0:
            editor.setCurrentIndex(pos)
        else:
            editor.setEditText(value)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)
