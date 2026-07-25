"""Windows genelinde çalışan kısayol tuşu (global hotkey).

Uyandırma sözcüğü kapalıyken (veya gürültülü ortamda çalışmadığında)
Artemis'i elle çağırmak için kullanılır. Kullanıcı hangi uygulamada
olursa olsun çalışır — bu yüzden Qt'nin normal `QShortcut`'ı yetmez;
işletim sisteminin `RegisterHotKey` API'si gerekir.

Uygulama: `RegisterHotKey` tetiklendiğinde Windows, uygulamanın mesaj
kuyruğuna bir `WM_HOTKEY` mesajı bırakır. Qt'nin olay döngüsü bu kuyruğu
zaten işlediği için, araya bir `QAbstractNativeEventFilter` koyup
mesajı yakalamak yeterlidir — ayrı bir iş parçacığı veya mesaj döngüsü
kurmaya gerek yoktur.

Bu modül yalnızca Windows'ta anlamlıdır; başka bir platformda
`register()` sessizce False döner ve uygulama kısayolsuz çalışmaya
devam eder (uyandırma sözcüğü zaten birincil yol).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable

# DİKKAT: `import ctypes` tek başına `ctypes.wintypes`'ı GETİRMEZ — alt modül
# açıkça import edilmeli, yoksa `ctypes.wintypes.MSG` çalışma anında
# AttributeError verir (kısayola her basıldığında). Öte yandan bu alt modül
# Windows dışında import edilemez (ValueError fırlatır), bu yüzden koşullu:
# proje Linux/CI'da da import edilebilir kalmalı.
if sys.platform == "win32":
    import ctypes.wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter

logger = logging.getLogger(__name__)

_WM_HOTKEY = 0x0312

_MODIFIERS = {
    "alt": 0x0001,
    "ctrl": 0x0002,
    "control": 0x0002,
    "shift": 0x0004,
    "win": 0x0008,
    "super": 0x0008,
}

_NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},  # F1-F12
}


class HotkeyParseError(ValueError):
    """Kısayol metni çözümlenemediğinde fırlatılır."""


def parse_hotkey(text: str) -> tuple[int, int]:
    """"ctrl+alt+a" gibi bir metni (değiştirici_maskesi, sanal_tuş_kodu) çiftine çevirir.

    Args:
        text: Artı ile ayrılmış kısayol tanımı (büyük/küçük harf duyarsız).

    Returns:
        `RegisterHotKey`'e verilecek (modifiers, virtual key code) çifti.

    Raises:
        HotkeyParseError: Metin boşsa, tuş kısmı yoksa veya tanınmıyorsa.
    """

    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise HotkeyParseError(f"Kısayol boş: {text!r}")

    modifiers = 0
    key_code: int | None = None

    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        elif part in _NAMED_KEYS:
            key_code = _NAMED_KEYS[part]
        elif len(part) == 1:
            key_code = ord(part.upper())
        else:
            raise HotkeyParseError(f"Tanınmayan tuş: {part!r} ({text!r} içinde)")

    if key_code is None:
        raise HotkeyParseError(f"Kısayolda asıl tuş yok, yalnızca değiştirici var: {text!r}")

    return modifiers, key_code


class GlobalHotkey(QAbstractNativeEventFilter):
    """Sistem genelinde bir kısayol tuşunu dinler ve tetiklenince geri çağırır.

    Kullanım::

        hotkey = GlobalHotkey("ctrl+alt+a", assistant.trigger)
        app.installNativeEventFilter(hotkey)
        hotkey.register()

    Args:
        hotkey: "ctrl+alt+a" biçiminde kısayol tanımı.
        callback: Kısayol basıldığında çağrılacak fonksiyon. Qt'nin ana
            iş parçacığında çalışır; uzun süren iş yapmamalıdır.
        hotkey_id: Aynı süreçte birden fazla kısayol kaydedilecekse
            benzersiz olmalıdır.
    """

    def __init__(self, hotkey: str, callback: Callable[[], None], hotkey_id: int = 1) -> None:
        super().__init__()
        self._modifiers, self._key_code = parse_hotkey(hotkey)
        self._callback = callback
        self._hotkey_id = hotkey_id
        self._registered = False
        self._text = hotkey

    def register(self) -> bool:
        """Kısayolu işletim sistemine kaydeder.

        Returns:
            Başarılıysa True. Kısayol başka bir uygulama tarafından zaten
            kullanılıyorsa veya platform Windows değilse False — bu bir
            hata değildir, uygulama kısayolsuz çalışmaya devam eder.
        """

        if sys.platform != "win32":
            logger.info("Kısayol yalnızca Windows'ta desteklenir; atlanıyor.")
            return False

        if ctypes.windll.user32.RegisterHotKey(None, self._hotkey_id, self._modifiers, self._key_code):
            self._registered = True
            logger.info("Kısayol kaydedildi: %s", self._text)
            return True

        logger.warning(
            "Kısayol kaydedilemedi: %s (başka bir uygulama kullanıyor olabilir).", self._text
        )
        return False

    def unregister(self) -> None:
        """Kısayol kaydını kaldırır. Birden fazla kez çağrılması güvenlidir."""

        if not self._registered:
            return

        ctypes.windll.user32.UnregisterHotKey(None, self._hotkey_id)
        self._registered = False
        logger.info("Kısayol kaydı kaldırıldı: %s", self._text)

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt'nin metot adı
        """Qt'nin işlediği ham Windows mesajlarını süzer.

        Returns:
            (işlendi_mi, sonuç) çifti. Qt6 bu iki elemanlı demeti bekler;
            False dönmek "mesajı normal akışına bırak" demektir.
        """

        if self._registered and event_type == "windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY and msg.wParam == self._hotkey_id:
                logger.debug("Kısayol tetiklendi: %s", self._text)
                self._callback()

        return False, 0
