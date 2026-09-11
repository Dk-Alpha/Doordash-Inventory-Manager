import sys

from PySide6.QtWidgets import QApplication

from app import db
from app.ui.main_window import MainWindow


def main():
    conn = db.get_connection()
    db.init_db(conn)

    app = QApplication(sys.argv)
    window = MainWindow(conn)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
