"""Qt binding imports and UI constants."""
from __future__ import annotations

try:
    from PySide6.QtCore import QPointF, QSize, Qt, QTimer
    from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontDatabase, QPainter, QPolygonF, QSyntaxHighlighter, QTextCharFormat
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QRadioButton,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QStyle,
        QStyledItemDelegate,
        QTabWidget,
        QTextEdit,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "PySide6 is required for Codex Context Tool. "
        "Run run.bat (it installs PySide6 into .venv) or: python -m pip install PySide6-Essentials"
    ) from exc

__all__ = [name for name in dir() if not name.startswith("_")]
QT_BINDING = "PySide6"
USER_ROLE = Qt.ItemDataRole.UserRole
YES = QMessageBox.StandardButton.Yes
SINGLE_SELECTION = QListWidget.SelectionMode.SingleSelection
EXTENDED_SELECTION = QListWidget.SelectionMode.ExtendedSelection
SPLITTER_HANDLE_WIDTH = 12
TREE_INDENTATION = 10
SCROLLBAR_WIDTH = 24
