import sys
import traceback
import logging

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt, QtMsgType, qInstallMessageHandler
from packsmith.gui.main_window import MainWindow

log = logging.getLogger(__name__)


def _qt_message_handler(mode, context, message):
    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(mode, logging.WARNING)
    loc = f"{context.file}:{context.line}" if context.file else "qt"
    log.log(level, "[%s] %s", loc, message)


def _exception_hook(exc_type, exc_value, exc_tb):
    log.critical("Uncaught exception:", exc_info=(exc_type, exc_value, exc_tb))
    traceback.print_exception(exc_type, exc_value, exc_tb)


def apply_dark_mode(app: QApplication):
    app.setStyle("Fusion")
    palette = QPalette()

    # Base colors
    palette.setColor(QPalette.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.WindowText, QColor(208, 208, 208))
    palette.setColor(QPalette.Base, QColor(22, 22, 22))
    palette.setColor(QPalette.AlternateBase, QColor(35, 35, 35))
    palette.setColor(QPalette.ToolTipBase, QColor(40, 40, 40))
    palette.setColor(QPalette.ToolTipText, QColor(208, 208, 208))
    palette.setColor(QPalette.Text, QColor(208, 208, 208))
    palette.setColor(QPalette.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ButtonText, QColor(208, 208, 208))
    palette.setColor(QPalette.BrightText, QColor(255, 50, 50))
    palette.setColor(QPalette.Link, QColor(90, 150, 220))
    palette.setColor(QPalette.Highlight, QColor(70, 120, 190))
    palette.setColor(QPalette.HighlightedText, QColor(240, 240, 240))

    # Disabled colors
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(110, 110, 110))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(110, 110, 110))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(110, 110, 110))

    app.setPalette(palette)

    # Extra stylesheet for things palette doesn't cover
    app.setStyleSheet("""
        QTableView {
            gridline-color: #3a3a3a;
            font-size: 13px;
        }
        QHeaderView::section {
            background-color: #2d2d2d;
            color: #d0d0d0;
            padding: 4px 8px;
            border: 1px solid #3a3a3a;
            font-weight: bold;
            font-size: 13px;
        }
        QScrollBar:vertical {
            background: #1e1e1e;
            width: 12px;
        }
        QScrollBar::handle:vertical {
            background: #555555;
            border-radius: 4px;
            min-height: 20px;
        }
        QScrollBar::handle:vertical:hover {
            background: #666666;
        }
        QScrollBar:horizontal {
            background: #1e1e1e;
            height: 12px;
        }
        QScrollBar::handle:horizontal {
            background: #555555;
            border-radius: 4px;
            min-width: 20px;
        }
        QScrollBar::handle:horizontal:hover {
            background: #666666;
        }
        QScrollBar::add-line, QScrollBar::sub-line {
            height: 0px;
            width: 0px;
        }
    """)


def run():
    sys.excepthook = _exception_hook
    qInstallMessageHandler(_qt_message_handler)

    app = QApplication(sys.argv)
    apply_dark_mode(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
