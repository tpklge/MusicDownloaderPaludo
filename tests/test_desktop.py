"""Exercise actual native Qt controls and their connection to the queue."""

import os

import pytest

pytest.importorskip(
    "PySide6", reason="Install the desktop extra to test native widgets"
)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from spotdl.desktop.bootstrap import prepare_qt_plugins
from spotdl.desktop.window import MainWindow

QT_APPLICATION = None


@pytest.fixture(scope="session")
def app():
    global QT_APPLICATION
    prepare_qt_plugins()
    application = QApplication.instance() or QApplication([])
    QT_APPLICATION = application
    application.setStyle("Fusion")
    yield application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    preferences = QSettings(
        str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat
    )
    ui = MainWindow(tmp_path / "downloads", preferences=preferences)
    monkeypatch.setattr(ui.manager.executor, "submit", lambda *_: None)
    ui.show()
    app.processEvents()
    yield ui
    for job in ui.manager.jobs.values():
        job["status"] = "cancelled"
    ui.close()
    ui.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_native_controls_change_live_workers_and_save_preferences(window, app):
    assert window.worker_spin.value() == 12
    window.worker_spin.setValue(5)
    window.youtube_spin.setValue(3)
    window.retry_spin.setValue(2)
    app.processEvents()
    assert window.manager.workers == 5
    assert window.manager.limiter.concurrency == 3
    assert int(window.preferences.value("workers")) == 5
    assert window.manager.retries == 2
    window.theme_combo.setCurrentIndex(1)
    assert window.preferences.value("theme") == "light"
    assert app.palette().window().color().lightness() > 200
    window.theme_combo.setCurrentIndex(0)
    assert app.palette().window().color().lightness() < 50


def test_add_links_and_individual_progress_are_visible(window, app):
    window.urls.setPlainText(
        "https://youtu.be/BaW_jenozKc\nhttps://youtu.be/abcdefghijk"
    )
    button = next(
        b
        for b in window.findChildren(QPushButton)
        if b.text() == "Adicionar aos downloads"
    )
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert len(window.manager.jobs) == 2
    job = next(iter(window.manager.jobs.values()))
    items = [
        {
            "index": i,
            "title": f"Music {i}",
            "url": "https://youtu.be/BaW_jenozKc",
            "status": "running",
            "progress": progress,
            "message": "Convertendo",
            "attempt": 1,
            "worker": i + 1,
            "speed": 2048,
            "eta": 10,
            "file": "",
        }
        for i, progress in enumerate([25, 80])
    ]
    window.manager.update(
        job["id"], status="running", items=items, total=2, progress=52.5
    )
    window.refresh()
    app.processEvents()
    parent = window.parents[job["id"]]
    assert parent.childCount() == 2
    assert parent.child(0).data(2, Qt.ItemDataRole.UserRole) == 25
    assert parent.child(1).data(2, Qt.ItemDataRole.UserRole) == 80
    assert parent.child(0).text(4) == "1"
    assert parent.child(0).text(6) == "2 KB/s"
    assert parent.child(0).text(7) == "00:10"
    assert window.overall.value() == 52


def test_scope_and_video_quality_controls(window):
    assert not window.quality.isEnabled()
    window.format_combo.setCurrentIndex(3)
    assert window.quality.isEnabled()
    window.scope.setCurrentIndex(1)
    window.urls.setPlainText(
        "https://www.youtube.com/watch?v=BaW_jenozKc&list=PLtest123"
    )
    window.add_links()
    job = next(iter(window.manager.jobs.values()))
    assert job["kind"] == "playlist"
    assert job["url"] == "https://www.youtube.com/playlist?list=PLtest123"
    assert job["format"] == "mp4"


def test_preview_response_cannot_overwrite_newer_link(window):
    previous = window.preview_sequence
    window.urls.setPlainText("https://youtu.be/BaW_jenozKc")
    window.messages.put((previous, {"title": "Stale response"}, b"", ""))
    window.refresh()
    assert window.preview is None
    assert window.preview_title.text() != "Stale response"
