"""Browser plugin testleri.

Bu plugin, ön plandaki tarayıcı penceresini bulmak için `pywin32`
(`win32gui`/`win32process`) ve `psutil` kullanır, ardından `pyautogui`
ile gerçek bir klavye kısayolu gönderir — ikisi de testi çalıştıran
kişinin gerçek masaüstünü etkiler. Bu yüzden iki katmanlı test stratejisi
izlenir:

1. Dispatcher üzerinden uçtan uca testler (`browser_focused`/`sent_keys`
   fixture'ları): yardımcı fonksiyonlar (`_ensure_browser_focused`,
   `_send_shortcut`) doğrudan monkeypatch'lenir; her tool'un DOĞRU
   kısayolu gönderip göndermediği ve tarayıcı bulunamadığında dürüstçe
   başarısız döndüğü sınanır (bkz. `tests/test_windows_plugin.py`'deki
   `_send_graceful_close` monkeypatch deseni).
2. Pencere bulma/odaklama mantığının kendisi (`_get_foreground_process_name`,
   `_focus_any_browser_window`, `_ensure_browser_focused`), `pywin32`/
   `psutil`'i sahte modüllerle değiştirerek platformdan bağımsız test
   edilir (bkz. `tests/test_windows_plugin.py::test_send_graceful_close_*`).

Hiçbir testte gerçek `pyautogui` çağrısı veya gerçek pencere odaklama
yapılmaz; varsayılan `pytest` koşusu ekranı etkilemez.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

import plugins.browser_plugin as browser_plugin
from config.settings import Settings
from core.dispatcher import ToolDispatcher
from memory.context_memory import ContextMemory
from tests.conftest import install_fake_module


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path,
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


# --- Katman 1: dispatcher üzerinden uçtan uca (yardımcılar monkeypatch'li) ---


@pytest.fixture
def browser_focused(monkeypatch: pytest.MonkeyPatch) -> None:
    """'Ön planda zaten bir tarayıcı var' varsayımını kurar."""

    monkeypatch.setattr(browser_plugin, "_ensure_browser_focused", lambda: (True, None))


@pytest.fixture
def no_browser_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """'Açık hiçbir tarayıcı bulunamadı' senaryosunu kurar."""

    monkeypatch.setattr(
        browser_plugin,
        "_ensure_browser_focused",
        lambda: (False, "Açık bir tarayıcı penceresi bulunamadı."),
    )


@pytest.fixture
def sent_keys(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_send(*keys: str) -> tuple[bool, str | None]:
        calls.append(keys)
        return True, None

    monkeypatch.setattr(browser_plugin, "_send_shortcut", fake_send)
    return calls


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_keys"),
    [
        ("browser.new_tab", {}, ("ctrl", "t")),
        ("browser.close_tab", {}, ("ctrl", "w")),
        ("browser.go_back", {}, ("alt", "left")),
        ("browser.go_forward", {}, ("alt", "right")),
        ("browser.refresh", {}, ("f5",)),
        ("browser.switch_tab", {}, ("ctrl", "tab")),
        ("browser.switch_tab", {"direction": "next"}, ("ctrl", "tab")),
        ("browser.switch_tab", {"direction": "previous"}, ("ctrl", "shift", "tab")),
    ],
)
def test_tool_sends_correct_shortcut_when_browser_focused(
    dispatcher: ToolDispatcher,
    browser_focused: None,
    sent_keys: list[tuple[str, ...]],
    tool_name: str,
    arguments: dict[str, Any],
    expected_keys: tuple[str, ...],
) -> None:
    result = dispatcher.dispatch({"tool": tool_name, "arguments": arguments})

    assert result.success is True
    assert sent_keys == [expected_keys]
    assert result.requires_confirmation is False


@pytest.mark.parametrize(
    "tool_name",
    [
        "browser.new_tab",
        "browser.close_tab",
        "browser.go_back",
        "browser.go_forward",
        "browser.refresh",
        "browser.switch_tab",
    ],
)
def test_tool_fails_honestly_when_no_browser_found(
    dispatcher: ToolDispatcher,
    no_browser_found: None,
    sent_keys: list[tuple[str, ...]],
    tool_name: str,
) -> None:
    result = dispatcher.dispatch({"tool": tool_name, "arguments": {}})

    assert result.success is False
    assert "tarayıcı" in result.message.lower()
    assert sent_keys == []  # kısayol hiç gönderilmemeli


def test_switch_tab_rejects_invalid_direction(
    dispatcher: ToolDispatcher, browser_focused: None, sent_keys: list[tuple[str, ...]]
) -> None:
    result = dispatcher.dispatch(
        {"tool": "browser.switch_tab", "arguments": {"direction": "sideways"}}
    )

    assert result.success is False
    assert sent_keys == []


def test_tool_fails_honestly_when_shortcut_send_fails(
    dispatcher: ToolDispatcher, browser_focused: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        browser_plugin, "_send_shortcut", lambda *keys: (False, "Kısayol gönderilemedi: örnek hata")
    )

    result = dispatcher.dispatch({"tool": "browser.new_tab", "arguments": {}})

    assert result.success is False
    assert "örnek hata" in result.message


# --- Katman 2: pencere bulma/odaklama mantığı (sahte win32/psutil) ---------


class _FakeProcess:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


def _install_fake_win32(
    monkeypatch: pytest.MonkeyPatch,
    *,
    foreground_hwnd: int | None,
    window_pids: dict[int, int],
    visible_hwnds: set[int],
    pid_names: dict[int, str],
) -> dict[str, Any]:
    """`win32gui`/`win32process`/`psutil`'i sahte modüllerle değiştirir.

    Returns:
        Testin gözlemleyebileceği yan bilgiler (ör. `SetForegroundWindow`
        ile hangi hwnd'in öne getirildiği) için paylaşılan bir sözlük.
    """

    observed: dict[str, Any] = {"focused_hwnd": None}

    fake_win32gui = types.ModuleType("win32gui")
    fake_win32gui.GetForegroundWindow = lambda: foreground_hwnd or 0
    fake_win32gui.IsWindowVisible = lambda hwnd: hwnd in visible_hwnds
    fake_win32gui.EnumWindows = lambda callback, extra: [callback(hwnd, extra) for hwnd in window_pids]

    def _set_foreground(hwnd: int) -> None:
        observed["focused_hwnd"] = hwnd

    fake_win32gui.SetForegroundWindow = _set_foreground

    fake_win32process = types.ModuleType("win32process")
    fake_win32process.GetWindowThreadProcessId = lambda hwnd: (0, window_pids[hwnd])

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.Process = lambda pid: _FakeProcess(pid_names[pid])

    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    return observed


def test_get_foreground_process_name_returns_lowercased_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_win32(
        monkeypatch,
        foreground_hwnd=1,
        window_pids={1: 100},
        visible_hwnds={1},
        pid_names={100: "Chrome.EXE"},
    )

    assert browser_plugin._get_foreground_process_name() == "chrome.exe"


def test_get_foreground_process_name_returns_none_when_pywin32_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "win32gui", None)
    monkeypatch.setitem(sys.modules, "win32process", None)

    assert browser_plugin._get_foreground_process_name() is None


def test_focus_any_browser_window_finds_and_focuses_matching_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # hwnd=1: not defteri (tarayıcı değil). hwnd=2: msedge.exe (eşleşen, görünür).
    observed = _install_fake_win32(
        monkeypatch,
        foreground_hwnd=1,
        window_pids={1: 100, 2: 200},
        visible_hwnds={1, 2},
        pid_names={100: "notepad.exe", 200: "msedge.exe"},
    )

    result = browser_plugin._focus_any_browser_window()

    assert result == "msedge.exe"
    assert observed["focused_hwnd"] == 2


def test_focus_any_browser_window_ignores_invisible_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # hwnd=2 tarayıcı sürecine ait ama görünmez -> hedeflenmemeli.
    _install_fake_win32(
        monkeypatch,
        foreground_hwnd=1,
        window_pids={1: 100, 2: 200},
        visible_hwnds={1},
        pid_names={100: "notepad.exe", 200: "chrome.exe"},
    )

    assert browser_plugin._focus_any_browser_window() is None


def test_focus_any_browser_window_returns_none_when_no_browser_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_win32(
        monkeypatch,
        foreground_hwnd=1,
        window_pids={1: 100},
        visible_hwnds={1},
        pid_names={100: "notepad.exe"},
    )

    assert browser_plugin._focus_any_browser_window() is None


def test_focus_any_browser_window_returns_none_when_pywin32_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "win32gui", None)
    monkeypatch.setitem(sys.modules, "win32process", None)
    monkeypatch.setitem(sys.modules, "psutil", None)

    assert browser_plugin._focus_any_browser_window() is None


def test_ensure_browser_focused_skips_refocus_when_already_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_plugin, "_get_foreground_process_name", lambda: "chrome.exe")

    def _should_not_be_called() -> str | None:
        raise AssertionError("zaten ön planda olan tarayıcı için tekrar aranmamalı")

    monkeypatch.setattr(browser_plugin, "_focus_any_browser_window", _should_not_be_called)

    ok, error = browser_plugin._ensure_browser_focused()

    assert ok is True
    assert error is None


def test_ensure_browser_focused_falls_back_to_window_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_plugin, "_get_foreground_process_name", lambda: "notepad.exe")
    monkeypatch.setattr(browser_plugin, "_focus_any_browser_window", lambda: "chrome.exe")

    ok, error = browser_plugin._ensure_browser_focused()

    assert ok is True
    assert error is None


def test_ensure_browser_focused_fails_when_no_browser_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_plugin, "_get_foreground_process_name", lambda: "notepad.exe")
    monkeypatch.setattr(browser_plugin, "_focus_any_browser_window", lambda: None)

    ok, error = browser_plugin._ensure_browser_focused()

    assert ok is False
    assert error == "Açık bir tarayıcı penceresi bulunamadı."


# --- _send_shortcut (pyautogui çağrıları) ------------------------------------


def test_send_shortcut_single_key_uses_press(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    install_fake_module(
        monkeypatch,
        "pyautogui",
        press=lambda key: calls.append(("press", (key,))),
        hotkey=lambda *keys: calls.append(("hotkey", keys)),
    )

    ok, error = browser_plugin._send_shortcut("f5")

    assert ok is True
    assert error is None
    assert calls == [("press", ("f5",))]


def test_send_shortcut_multi_key_uses_hotkey(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    install_fake_module(
        monkeypatch,
        "pyautogui",
        press=lambda key: calls.append(("press", (key,))),
        hotkey=lambda *keys: calls.append(("hotkey", keys)),
    )

    ok, error = browser_plugin._send_shortcut("ctrl", "t")

    assert ok is True
    assert calls == [("hotkey", ("ctrl", "t"))]


def test_send_shortcut_reports_exception_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*keys: str) -> None:
        raise RuntimeError("girdi erişimi kısıtlı")

    install_fake_module(monkeypatch, "pyautogui", hotkey=_raise, press=_raise)

    ok, error = browser_plugin._send_shortcut("ctrl", "w")

    assert ok is False
    assert error is not None and "gönderilemedi" in error
