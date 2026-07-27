"""Zaten açık olan bir tarayıcı penceresini kontrol eden tool'lar.

`plugins/web_plugin.py` YENİ bir URL/arama AÇAR (`webbrowser.open`); bu
plugin ise halihazırda açık bir tarayıcı penceresini kontrol eder: yeni
sekme, sekme kapatma, geri/ileri gitme, sayfayı yenileme, sekmeler arası
geçiş. Bu, tam bir tarayıcı otomasyon protokolü (CDP/Selenium/WebDriver)
DEĞİLDİR — kasıtlı olarak mekanik kalır: işletim sistemi seviyesinde,
her büyük masaüstü tarayıcısının (Chrome, Edge, Firefox, Brave, Opera,
Vivaldi) tanıdığı standart klavye kısayollarını (`pyautogui.hotkey`)
ön plandaki tarayıcı penceresine gönderir. Sayfa içeriğini okumak,
DOM'a erişmek ya da belirli bir sayfa öğesine tıklamak gibi gerçek bir
protokol istemcisi gerektiren ihtiyaçlar `plugins/mcp_plugin.py`
kapsamındadır (bu teslimatın kasıtlı olarak dışında).

Güvenlik notu: klavye kısayolları YALNIZCA gerçekten bir tarayıcı
penceresi ön plandaysa (ya da açık pencereler arasında bulunup öne
getirilebiliyorsa) gönderilir; bulunamazsa dürüstçe `success=False`
döner. Bu doğrulama, "koşulsuz success=True yasak" kuralının bu
plugin'deki somut karşılığıdır: aksi halde ör. `Ctrl+W`, kullanıcının o
an kullandığı BAŞKA bir uygulamaya (bir kod editörüne, bir sohbet
penceresine...) gidip istenmeyen bir sekme/pencere kapatabilirdi.

`danger_level` kararı: tüm tool'lar `DangerLevel.SAFE`. Gerekçe
`plugins/mouse_keyboard_plugin.py`'deki ile aynıdır: bu kısayollar
kullanıcının kendi klavyesinden basabileceği sıradan tuşlardır ve proje
genelindeki CONFIRM_REQUIRED eşiği "geri alınabilirlik"e göre çizilmiş
(bkz. CLAUDE.md: delete/shutdown/restart/format/registry/service).
`browser.close_tab` bile çoğu tarayıcıda `Ctrl+Shift+T` ile geri
açılabilir — tıpkı `windows.close_app`'ın (SAFE) bir uygulamayı
kapatması gibi, geri dönüşü olmayan bir sistem işlemi değildir.

Tasarım notu: `pyautogui`/`pywin32`/`psutil`, diğer Windows'a özgü
bağımlılıklar gibi (bkz. `plugins/windows_plugin.py`) dosya başında
değil, yalnızca ilgili yardımcı fonksiyon/​`execute()` içinde "lazy"
olarak import edilir; böylece Linux/CI'da bu plugin import edilirken
hata verilmez.
"""

from __future__ import annotations

import time
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult

# Bilinen masaüstü tarayıcı süreç adları (küçük harf). Yeni bir tarayıcı
# desteklemek yalnızca bu kümeye tek satır eklemek demektir.
_BROWSER_PROCESS_NAMES = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "vivaldi.exe",
        "iexplore.exe",
    }
)

_FOCUS_SETTLE_SECONDS = 0.15
"""Bir pencere öne getirildikten sonra, kısayolun doğru pencereye gitmesi
için Windows'un odak değişikliğini işlemesine tanınan kısa süre (saniye).
Testler bu süreyi hızlandırmak isterse modülü monkeypatch'leyebilir."""


def _get_foreground_process_name() -> str | None:
    """Şu an ön plandaki (aktif) pencerenin süreç adını (küçük harf) döndürür.

    `pywin32`/`psutil` kurulu değilse ya da pencere sorgulanamazsa (ör.
    Linux/CI, ya da pencere bu sırada kapanmışsa) sessizce None döner —
    çağıran taraf (`_ensure_browser_focused`) bunu "tarayıcı ön planda
    değil" olarak yorumlayıp yedek yola (pencere arama) düşer.
    """

    try:
        import psutil
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:  # noqa: BLE001 - kasıtlı olarak geniş: her hata "bilinmiyor" demektir
        return None


def _focus_any_browser_window() -> str | None:
    """Açık pencereler arasında bilinen bir tarayıcı bulup öne getirir.

    `windows_plugin.WindowsFocusWindowTool` ile aynı `EnumWindows`
    deseni kullanılır, ancak başlık sorgusu yerine süreç adı
    `_BROWSER_PROCESS_NAMES` kümesiyle eşleştirilir.

    Returns:
        Öne getirilen tarayıcının süreç adı; hiçbiri bulunamazsa
        (ya da pywin32/psutil kurulu değilse) None.
    """

    try:
        import psutil
        import win32gui
        import win32process
    except Exception:  # noqa: BLE001 - Linux/CI'da pywin32 hiç kurulu olmayabilir
        return None

    match: dict[str, Any] = {}

    def _on_window(hwnd: int, _extra: Any) -> None:
        if "hwnd" in match or not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(pid).name().lower()
        except Exception:  # noqa: BLE001 - pencere/süreç bu sırada kapanmış olabilir
            return
        if name in _BROWSER_PROCESS_NAMES:
            match["hwnd"] = hwnd
            match["name"] = name

    try:
        win32gui.EnumWindows(_on_window, None)
    except Exception:  # noqa: BLE001 - numaralandırma ortama göre başarısız olabilir
        return None

    if "hwnd" not in match:
        return None

    try:
        win32gui.SetForegroundWindow(match["hwnd"])
    except Exception:  # noqa: BLE001 - Windows'un ön plan kısıtlaması engelleyebilir
        return None

    time.sleep(_FOCUS_SETTLE_SECONDS)
    return match["name"]


def _ensure_browser_focused() -> tuple[bool, str | None]:
    """Ön planda bir tarayıcı penceresi olmasını sağlar.

    Ön planda zaten bir tarayıcı varsa dokunmaz; yoksa açık pencereler
    arasında bir tarayıcı arayıp öne getirir. Hiçbiri bulunamazsa (ya da
    hiç tarayıcı açık değilse) kısayol GÖNDERİLMEZ ve dürüstçe başarısız
    dönülür — bkz. modül dokümantasyonundaki güvenlik notu.

    Returns:
        (başarılı_mı, hata_mesajı) ikilisi. Başarılıysa hata_mesajı None'dır.
    """

    current = _get_foreground_process_name()
    if current in _BROWSER_PROCESS_NAMES:
        return True, None

    focused = _focus_any_browser_window()
    if focused is None:
        return False, "Açık bir tarayıcı penceresi bulunamadı."
    return True, None


def _send_shortcut(*keys: str) -> tuple[bool, str | None]:
    """`_ensure_browser_focused` ile doğrulanmış ön plandaki pencereye bir
    klavye kısayolu gönderir (tek tuşsa `press`, birden fazlaysa `hotkey`)."""

    try:
        import pyautogui

        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
    except Exception as exc:  # noqa: BLE001 - girdi erişimi bu oturumda kısıtlı olabilir
        return False, f"Kısayol gönderilemedi: {exc}"

    return True, None


def _run_browser_shortcut(*keys: str) -> ToolResult:
    """Bir tarayıcı kısayolu için ortak akış: önce odağı doğrula, sonra gönder."""

    ok, error = _ensure_browser_focused()
    if not ok:
        return ToolResult(success=False, message=error or "Tarayıcı penceresine odaklanılamadı.")

    ok, error = _send_shortcut(*keys)
    if not ok:
        return ToolResult(success=False, message=error or "Kısayol gönderilemedi.")

    return ToolResult(success=True, message="")  # her tool kendi mesajını üstüne yazar


@register_tool
class BrowserNewTabTool(BaseTool):
    """Ön plandaki tarayıcıda yeni bir sekme açar."""

    name = "browser.new_tab"
    description = "Ön plandaki (veya bulunabilen) tarayıcı penceresinde yeni bir sekme açar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        result = _run_browser_shortcut("ctrl", "t")
        if not result.success:
            return result
        return ToolResult(success=True, message="Yeni sekme açıldı.")


@register_tool
class BrowserCloseTabTool(BaseTool):
    """Ön plandaki tarayıcının mevcut sekmesini kapatır."""

    name = "browser.close_tab"
    description = "Ön plandaki (veya bulunabilen) tarayıcı penceresinde mevcut sekmeyi kapatır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        result = _run_browser_shortcut("ctrl", "w")
        if not result.success:
            return result
        return ToolResult(success=True, message="Sekme kapatıldı.")


@register_tool
class BrowserGoBackTool(BaseTool):
    """Ön plandaki tarayıcıda bir önceki sayfaya döner."""

    name = "browser.go_back"
    description = "Ön plandaki (veya bulunabilen) tarayıcı penceresinde bir önceki sayfaya döner."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        result = _run_browser_shortcut("alt", "left")
        if not result.success:
            return result
        return ToolResult(success=True, message="Bir önceki sayfaya dönüldü.")


@register_tool
class BrowserGoForwardTool(BaseTool):
    """Ön plandaki tarayıcıda bir sonraki sayfaya gider."""

    name = "browser.go_forward"
    description = "Ön plandaki (veya bulunabilen) tarayıcı penceresinde bir sonraki sayfaya gider."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        result = _run_browser_shortcut("alt", "right")
        if not result.success:
            return result
        return ToolResult(success=True, message="Bir sonraki sayfaya gidildi.")


@register_tool
class BrowserRefreshTool(BaseTool):
    """Ön plandaki tarayıcıda geçerli sayfayı yeniler."""

    name = "browser.refresh"
    description = "Ön plandaki (veya bulunabilen) tarayıcı penceresinde geçerli sayfayı yeniler."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        result = _run_browser_shortcut("f5")
        if not result.success:
            return result
        return ToolResult(success=True, message="Sayfa yenilendi.")


@register_tool
class BrowserSwitchTabTool(BaseTool):
    """Ön plandaki tarayıcıda bir sonraki/önceki sekmeye geçer."""

    name = "browser.switch_tab"
    description = "Ön plandaki (veya bulunabilen) tarayıcı penceresinde bir sonraki/önceki sekmeye geçer."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["next", "previous"],
                    "default": "next",
                    "description": "Geçilecek yön.",
                }
            },
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        direction = arguments.get("direction", "next")
        if direction not in ("next", "previous"):
            return ToolResult(success=False, message=f"Geçersiz yön: '{direction}'.")

        keys = ("ctrl", "tab") if direction == "next" else ("ctrl", "shift", "tab")
        result = _run_browser_shortcut(*keys)
        if not result.success:
            return result

        direction_label = "sonraki" if direction == "next" else "önceki"
        return ToolResult(success=True, message=f"{direction_label.capitalize()} sekmeye geçildi.")
