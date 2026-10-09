"""`ui/tray.py`: tek tık paneli açar; sesli asistan kapalıyken "Şimdi dinle" yoktur."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from ui.tray import ArtemisTray


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _menu_texts(tray: ArtemisTray) -> list[str]:
    return [action.text() for action in tray._menu.actions()]


def test_panel_entry_is_labelled_for_the_user(qapp: QApplication) -> None:
    tray = ArtemisTray(lambda: None, lambda: None, on_panel=lambda: None)

    assert any(text.startswith("Paneli aç") for text in _menu_texts(tray))


def test_single_click_opens_the_panel(qapp: QApplication) -> None:
    opened: list[str] = []
    tray = ArtemisTray(lambda: None, lambda: None, on_panel=lambda: opened.append("panel"))

    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)

    assert opened == ["panel"]


def test_without_voice_there_is_no_listen_entry_and_double_click_is_harmless(qapp: QApplication) -> None:
    """`on_listen=None`: dinlemeyi başlatacak bir şey yokken düğme gösterilmez."""

    opened: list[str] = []
    tray = ArtemisTray(None, lambda: None, on_panel=lambda: opened.append("panel"))

    assert not any("Şimdi dinle" in text for text in _menu_texts(tray))
    tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert opened == [], "çift tık da yalnızca dinlemeyi tetikler, panel açılmaz"


def test_double_click_still_starts_listening_when_voice_is_on(qapp: QApplication) -> None:
    listened: list[str] = []
    tray = ArtemisTray(lambda: listened.append("dinle"), lambda: None, on_panel=lambda: None)

    tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)

    assert listened == ["dinle"]
