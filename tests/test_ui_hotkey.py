"""`ui/hotkey.py` için testler: `parse_hotkey` ve `GlobalHotkey`.

`parse_hotkey` saf bir fonksiyondur, Windows API'sine dokunmaz. `GlobalHotkey`
Windows dışı platformlarda `register()`'ı erken döndürdüğü için gerçek
`ctypes.windll.user32.RegisterHotKey` çağrısı bu testlerde HİÇ tetiklenmez
(`sys.platform` `monkeypatch.setattr` ile geçici olarak "linux" yapılıyor —
bkz. `tests/test_dispatcher.py`'deki `os.startfile` monkeypatch deseni).
"""

from __future__ import annotations

import sys

import pytest

# `ui.hotkey` modül seviyesinde PyQt6 import eder. PyQt6 kurulu değilse
# (örn. başlıksız bir CI makinesi) bu satır TOPLAMA (collection) hatası
# verip tüm dosyayı düşürürdü. `importorskip` onu dürüst bir "atlandı"ya
# çevirir — testler yine kurulu olan her yerde çalışır.
pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

from ui.hotkey import GlobalHotkey, HotkeyParseError, parse_hotkey

# --------------------------------------------------------------------------
# parse_hotkey — bilinen kombinasyonlar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ctrl+alt+a", (3, 65)),
        ("win+space", (8, 32)),
        ("alt+enter", (1, 13)),
        ("ctrl+shift+f5", (6, 116)),
    ],
)
def test_parse_hotkey_known_combinations(text: str, expected: tuple[int, int]) -> None:
    assert parse_hotkey(text) == expected


def test_parse_hotkey_is_case_insensitive() -> None:
    assert parse_hotkey("CTRL+ALT+A") == parse_hotkey("ctrl+alt+a") == (3, 65)


def test_control_and_ctrl_are_the_same_modifier_mask() -> None:
    assert parse_hotkey("control+a") == parse_hotkey("ctrl+a")


def test_super_and_win_are_the_same_modifier_mask() -> None:
    assert parse_hotkey("super+a") == parse_hotkey("win+a")


# --------------------------------------------------------------------------
# parse_hotkey — hatalı girdiler
# --------------------------------------------------------------------------


def test_parse_hotkey_empty_string_raises() -> None:
    with pytest.raises(HotkeyParseError):
        parse_hotkey("")


def test_parse_hotkey_without_actual_key_raises() -> None:
    # Yalnızca değiştiriciler var, asıl tuş yok.
    with pytest.raises(HotkeyParseError):
        parse_hotkey("ctrl+alt")


def test_parse_hotkey_unknown_key_raises() -> None:
    with pytest.raises(HotkeyParseError):
        parse_hotkey("ctrl+bilinmeyen")


# --------------------------------------------------------------------------
# GlobalHotkey.register() — platform kontrolü
# --------------------------------------------------------------------------


def test_global_hotkey_register_returns_false_outside_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gerçek RegisterHotKey ASLA çağrılmamalı: sys.platform kontrolü onun
    # önüne geçip erken False döner.
    monkeypatch.setattr(sys, "platform", "linux")

    hotkey = GlobalHotkey("ctrl+alt+a", callback=lambda: None)

    assert hotkey.register() is False
