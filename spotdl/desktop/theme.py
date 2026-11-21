"""High-contrast native widget themes."""

# Qt classes are provided by extension modules.
# pylint: disable=no-name-in-module

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication, theme: str):
    """Keep native menus, disabled text and inputs readable in both themes."""
    dark = theme == "dark"
    bg, panel, text, muted, border, accent, on_accent = (
        ("#10141c", "#1a2130", "#f1f5fc", "#b6c2d7", "#39465c", "#91b4ff", "#101d36")
        if dark
        else (
            "#f3f5fa",
            "#ffffff",
            "#18243a",
            "#52627c",
            "#c8d2e2",
            "#285cc4",
            "#ffffff",
        )
    )
    palette = QPalette()
    for role, color in {
        QPalette.ColorRole.Window: bg,
        QPalette.ColorRole.WindowText: text,
        QPalette.ColorRole.Base: panel,
        QPalette.ColorRole.AlternateBase: bg,
        QPalette.ColorRole.Text: text,
        QPalette.ColorRole.Button: panel,
        QPalette.ColorRole.ButtonText: text,
        QPalette.ColorRole.Highlight: accent,
        QPalette.ColorRole.HighlightedText: on_accent,
        QPalette.ColorRole.ToolTipBase: panel,
        QPalette.ColorRole.ToolTipText: text,
        QPalette.ColorRole.PlaceholderText: muted,
    }.items():
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(muted))
    app.setPalette(palette)
    app.setStyleSheet(f"""
        QWidget {{ font-size: 13px; }}
        QMainWindow, QDialog {{ background: {bg}; color: {text}; }}
        QFrame#card {{ background: {panel}; border: 1px solid {border}; border-radius: 12px; }}
        QLabel#heading {{ font-size: 28px; font-weight: 700; }}
        QLabel#section {{ font-size: 16px; font-weight: 650; }}
        QLabel#muted {{ color: {muted}; }}
        QLabel#error {{ color: {'#ffadb7' if dark else '#af2440'}; }}
        QPushButton {{ background: {panel}; color: {text}; border: 1px solid {border};
                       border-radius: 7px; padding: 9px 14px; min-height: 18px; }}
        QPushButton:hover {{ border-color: {accent}; }}
        QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
        QComboBox:focus {{ border: 2px solid {accent}; }}
        QPushButton#primary {{ background: {accent}; color: {on_accent}; font-weight: 650; }}
        QPushButton:disabled {{ color: {muted}; background: {bg}; }}
        QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
            background: {bg}; color: {text}; border: 1px solid {border};
            border-radius: 6px; padding: 7px; min-height: 20px; selection-background-color: {accent}; }}
        QComboBox QAbstractItemView {{ background: {panel}; color: {text}; }}
        QTreeWidget {{ background: {panel}; border: 1px solid {border}; border-radius: 8px;
                       alternate-background-color: {bg}; outline: 0; }}
        QTreeWidget::item {{ min-height: 36px; padding: 3px 5px; }}
        QTreeWidget::item:selected {{ background: {'#304b78' if dark else '#dce8ff'}; color: {text}; }}
        QHeaderView::section {{ background: {bg}; color: {muted}; padding: 10px 7px;
                                border: 0; border-bottom: 1px solid {border}; font-weight: 600; }}
        QProgressBar {{ background: {bg}; border: 1px solid {border}; border-radius: 5px;
                        text-align: center; min-height: 20px; color: {text}; }}
        QProgressBar::chunk {{ background: {'#365ea4' if dark else '#aac4ff'}; border-radius: 4px; }}
        QGroupBox {{ border: 1px solid {border}; border-radius: 8px; margin-top: 12px;
                     padding: 16px 10px 10px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; }}
        QSplitter::handle {{ background: {border}; height: 2px; width: 2px; }}
        QToolTip {{ background: {panel}; color: {text}; border: 1px solid {border}; padding: 7px; }}
    """)
