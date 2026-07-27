"""Mouse/keyboard plugin testleri.

`pyautogui`'nin gerçek fonksiyonları (moveTo/click/write/press/hotkey/
scroll) çağrıldığında GERÇEKTEN imleci taşır, tuş basar veya kaydırır —
bu, testi çalıştıran kişinin ekranını etkiler. Bu yüzden her testte
`fake_pyautogui` fixture'ı ile gerçek `pyautogui` modülünün fonksiyonları
sahte (no-op, çağrıları kaydeden) sürümlerle değiştirilir; varsayılan
`pytest` koşusunda hiçbir gerçek fare/klavye girdisi üretilmez (bkz.
`tests/test_web_plugin.py`'deki `webbrowser.open` monkeypatch deseni ve
CLAUDE.md'nin "gerçek makineyi etkileyen hiçbir şey varsayılan olarak
çalışmamalı" kuralı).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyautogui
import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.plugin_loader import load_plugins
from memory.context_memory import ContextMemory


@pytest.fixture(autouse=True)
def _load_all_plugins() -> None:
    load_plugins()


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path,
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


class _FakePyAutoGui:
    """`pyautogui`'nin çağrılarını kaydeden, gerçek girdi üretmeyen sahte sürüm.

    `moveTo` ve koordinatlı `click`/`doubleClick` çağrıları imlecin
    "mevcut konumunu" günceller; böylece plugin'in `pyautogui.position()`
    ile yaptığı doğrulama (bkz. `mouse_keyboard_plugin.py`) gerçekçi
    biçimde sınanabilir.
    """

    def __init__(self) -> None:
        self.pos: tuple[int, int] = (0, 0)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.screen_size: tuple[int, int] = (1920, 1080)

    def moveTo(self, x: int, y: int, duration: float = 0.0) -> None:  # noqa: N802 - pyautogui'nin kendi ismi
        self.calls.append(("moveTo", (x, y), {"duration": duration}))
        self.pos = (x, y)

    def click(self, **kwargs: Any) -> None:
        self.calls.append(("click", (), kwargs))
        if "x" in kwargs and "y" in kwargs:
            self.pos = (kwargs["x"], kwargs["y"])

    def doubleClick(self, **kwargs: Any) -> None:  # noqa: N802
        self.calls.append(("doubleClick", (), kwargs))
        if "x" in kwargs and "y" in kwargs:
            self.pos = (kwargs["x"], kwargs["y"])

    def write(self, text: str, interval: float = 0.0) -> None:
        self.calls.append(("write", (text,), {"interval": interval}))

    def press(self, key: str) -> None:
        self.calls.append(("press", (key,), {}))

    def hotkey(self, *keys: str) -> None:
        self.calls.append(("hotkey", keys, {}))

    def scroll(self, amount: int, **kwargs: Any) -> None:
        self.calls.append(("scroll", (amount,), kwargs))

    def position(self) -> tuple[int, int]:
        return self.pos

    def size(self) -> tuple[int, int]:
        return self.screen_size


@pytest.fixture
def fake_pyautogui(monkeypatch: pytest.MonkeyPatch) -> _FakePyAutoGui:
    fake = _FakePyAutoGui()
    for attr in ("moveTo", "click", "doubleClick", "write", "press", "hotkey", "scroll", "position", "size"):
        monkeypatch.setattr(pyautogui, attr, getattr(fake, attr))
    return fake


# --- mouse_keyboard.move_mouse ---------------------------------------------


def test_move_mouse_success(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.move_mouse", "arguments": {"x": 100, "y": 200}})

    assert result.success is True
    assert ("moveTo", (100, 200), {"duration": 0.0}) in fake_pyautogui.calls
    assert fake_pyautogui.pos == (100, 200)


def test_move_mouse_rejects_negative_coordinates(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui
) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.move_mouse", "arguments": {"x": -5, "y": 10}})

    assert result.success is False
    assert fake_pyautogui.calls == []  # pyautogui hiç çağrılmamalı


def test_move_mouse_rejects_out_of_bounds_coordinates(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui
) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.move_mouse", "arguments": {"x": 5000, "y": 5000}})

    assert result.success is False
    assert "ekran sınırlarının dışında" in result.message
    assert fake_pyautogui.calls == []


def test_move_mouse_reports_failure_when_position_does_not_match(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pyautogui.moveTo` sessizce beklenenden farklı bir konuma giderse
    (ör. ekran kilidi/erişim kısıtı) dürüstçe başarısız dönmeli."""

    monkeypatch.setattr(pyautogui, "moveTo", lambda x, y, duration=0.0: None)  # imleci hiç güncelleme

    result = dispatcher.dispatch({"tool": "mouse_keyboard.move_mouse", "arguments": {"x": 300, "y": 400}})

    assert result.success is False
    assert "ulaşmadı" in result.message


def test_move_mouse_reports_exception_honestly(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ekrana erişilemiyor")

    monkeypatch.setattr(pyautogui, "moveTo", _raise)

    result = dispatcher.dispatch({"tool": "mouse_keyboard.move_mouse", "arguments": {"x": 10, "y": 10}})

    assert result.success is False
    assert "taşınamadı" in result.message


# --- mouse_keyboard.click ----------------------------------------------------


def test_click_at_coordinates_success(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch(
        {"tool": "mouse_keyboard.click", "arguments": {"x": 50, "y": 60}}
    )

    assert result.success is True
    assert ("click", (), {"button": "left", "x": 50, "y": 60}) in fake_pyautogui.calls


def test_click_without_coordinates_clicks_current_position(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui
) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.click", "arguments": {}})

    assert result.success is True
    assert ("click", (), {"button": "left"}) in fake_pyautogui.calls
    assert "mevcut konumda" in result.message


def test_click_double_uses_double_click(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch(
        {"tool": "mouse_keyboard.click", "arguments": {"x": 10, "y": 10, "double": True}}
    )

    assert result.success is True
    assert ("doubleClick", (), {"button": "left", "x": 10, "y": 10}) in fake_pyautogui.calls


def test_click_requires_both_x_and_y_together(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui
) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.click", "arguments": {"x": 10}})

    assert result.success is False
    assert fake_pyautogui.calls == []


def test_click_rejects_invalid_button(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch(
        {"tool": "mouse_keyboard.click", "arguments": {"x": 1, "y": 1, "button": "banana"}}
    )

    assert result.success is False
    assert fake_pyautogui.calls == []


def test_click_reports_failure_when_position_does_not_match(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pyautogui, "click", lambda **kwargs: None)  # imleci hiç güncelleme

    result = dispatcher.dispatch(
        {"tool": "mouse_keyboard.click", "arguments": {"x": 500, "y": 500}}
    )

    assert result.success is False
    assert "şüpheli" in result.message


def test_click_does_not_require_confirmation(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui
) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.click", "arguments": {"x": 1, "y": 1}})

    assert result.requires_confirmation is False


# --- mouse_keyboard.type_text -------------------------------------------------


def test_type_text_success(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch(
        {"tool": "mouse_keyboard.type_text", "arguments": {"text": "merhaba dünya"}}
    )

    assert result.success is True
    assert ("write", ("merhaba dünya",), {"interval": 0.0}) in fake_pyautogui.calls


def test_type_text_rejects_empty_text(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.type_text", "arguments": {"text": ""}})

    assert result.success is False
    assert fake_pyautogui.calls == []


# --- mouse_keyboard.press_key -------------------------------------------------


def test_press_key_single_key_uses_press(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.press_key", "arguments": {"keys": "enter"}})

    assert result.success is True
    assert ("press", ("enter",), {}) in fake_pyautogui.calls


def test_press_key_combo_uses_hotkey(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.press_key", "arguments": {"keys": "Ctrl+C"}})

    assert result.success is True
    assert ("hotkey", ("ctrl", "c"), {}) in fake_pyautogui.calls


def test_press_key_rejects_empty_keys(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.press_key", "arguments": {"keys": "   "}})

    assert result.success is False
    assert fake_pyautogui.calls == []


# --- mouse_keyboard.scroll ----------------------------------------------------


def test_scroll_up_positive_amount(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.scroll", "arguments": {"amount": 5}})

    assert result.success is True
    assert "yukarı" in result.message
    assert ("scroll", (5,), {}) in fake_pyautogui.calls


def test_scroll_down_negative_amount(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.scroll", "arguments": {"amount": -3}})

    assert result.success is True
    assert "aşağı" in result.message


def test_scroll_rejects_zero_amount(dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.scroll", "arguments": {"amount": 0}})

    assert result.success is False
    assert fake_pyautogui.calls == []


def test_scroll_requires_both_x_and_y_together(
    dispatcher: ToolDispatcher, fake_pyautogui: _FakePyAutoGui
) -> None:
    result = dispatcher.dispatch({"tool": "mouse_keyboard.scroll", "arguments": {"amount": 5, "x": 10}})

    assert result.success is False
    assert fake_pyautogui.calls == []
