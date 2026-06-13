"""Simple Solar Guider — application entry point.

Run from INSIDE the solar_guider/ directory so sibling modules resolve via
plain imports (sys.path[0] is this package dir):

    python main.py
"""

import sys

from PyQt6.QtWidgets import QApplication

from gui import MainWindow


def main() -> None:
    """Create the Qt application, show the main window, and run the event loop."""
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
