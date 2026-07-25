"""Windows masaüstü kontrolü ile ilgili tool'lar.

Bu plugin, WINDOWS OTOMASYONU listesindeki maddeleri karşılar: program
aç/kapat, ses, parlaklık, kilitleme, uyku, ekran görüntüsü, pano,
pencere yönetimi.

Tasarım notu: `ctypes.windll`, `pywin32` (win32api/win32gui/win32clipboard/
win32com), `pyautogui` ve `psutil` yalnızca ilgili tool çağrıldığında
gerekir; bu yüzden dosya başında değil, her `execute()` içinde "lazy"
olarak import edilir. Bu hem ilgisiz bir tool çağrıldığında tüm plugin'in
yüklenmesini engellememeyi, hem de sesli asistan gibi hafif/idle
senaryolarda gereksiz RAM ayak izini önlemeyi sağlar.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult
from plugins._app_resolver import AppResolver

# AppResolver'ın Başlat Menüsü kısayol taraması (yavaş) yalnızca ilk
# ihtiyaç duyulduğunda yapılır ve modül ömrü boyunca (tüm launch_app
# çağrıları arasında) paylaşılan tek bir örnekte önbelleğe alınır.
_app_resolver = AppResolver()


@register_tool
class WindowsLaunchAppTool(BaseTool):
    """Bir uygulamayı adına göre başlatır.

    Çözümleme sırası (bkz. `plugins/_app_resolver.py`): bilinen sistem
    komutları -> Registry "App Paths" -> Başlat Menüsü kısayolları
    (fuzzy eşleşme). Bulunan gerçek bir dosya yoluysa `os.startfile()`
    ile başlatılır (bu, dosya yoksa gerçekten hata fırlatır — eskiden
    kullanılan `cmd /c start` sessizce başarısız olabiliyordu ve bu
    yanlış-pozitif "başlatıldı" mesajlarına yol açıyordu).
    """

    name = "windows.launch_app"
    description = "Bir uygulamayı açar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Uygulama adı."}},
            "required": ["name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        name = arguments["name"]
        system_command, resolved_path = _app_resolver.resolve(name)

        if resolved_path is not None:
            try:
                os.startfile(resolved_path)
            except OSError as exc:
                return ToolResult(
                    success=False, message=f"'{name}' bulundu ('{resolved_path}') ama başlatılamadı: {exc}"
                )
            context.memory.set("last_launched_app", str(resolved_path))
            return ToolResult(success=True, message=f"'{name}' başlatıldı.", data={"path": str(resolved_path)})

        if system_command is not None:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", system_command], shell=False)
            except (OSError, FileNotFoundError) as exc:
                return ToolResult(success=False, message=f"'{name}' başlatılamadı: {exc}")
            context.memory.set("last_launched_app", system_command)
            return ToolResult(success=True, message=f"'{name}' başlatıldı.", data={"executable": system_command})

        # Hiçbir kaynakta bulunamadı: körü körüne "başarılı" demek yerine
        # dürüstçe başarısız döndür + varsa en yakın kısayol önerilerini sun.
        suggestions = _app_resolver.suggestions(name)
        hint = f" Şunu mu demek istediniz: {', '.join(suggestions)}?" if suggestions else ""
        return ToolResult(
            success=False,
            message=(
                f"'{name}' ne bilinen bir sistem komutu ne Registry'de ne de Başlat Menüsü "
                f"kısayolları arasında bulunamadı.{hint}"
            ),
        )


@register_tool
class WindowsCloseAppTool(BaseTool):
    """Adı eşleşen çalışan süreçleri (process) sonlandırır."""

    name = "windows.close_app"
    description = "Bir uygulamayı kapatır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Kapatılacak process/uygulama adı."}},
            "required": ["name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import psutil

        query = arguments["name"].lower()
        closed = []

        for proc in psutil.process_iter(["pid", "name"]):
            proc_name = (proc.info.get("name") or "").lower()
            if query in proc_name:
                try:
                    proc.terminate()
                    closed.append(proc.info["name"])
                except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                    context.logger.warning("Süreç kapatılamadı (%s): %s", proc.info.get("name"), exc)

        if not closed:
            return ToolResult(success=False, message=f"'{arguments['name']}' ile eşleşen çalışan süreç bulunamadı.")

        return ToolResult(success=True, message=f"{len(closed)} süreç kapatıldı: {closed}", data={"closed": closed})


@register_tool
class WindowsLockTool(BaseTool):
    """Bilgisayarı kilitler (Windows oturum kilidi)."""

    name = "windows.lock"
    description = "Bilgisayarı kilitler."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import ctypes

        ctypes.windll.user32.LockWorkStation()
        return ToolResult(success=True, message="Bilgisayar kilitlendi.")


@register_tool
class WindowsSleepTool(BaseTool):
    """Bilgisayarı uyku (sleep) moduna alır."""

    name = "windows.sleep"
    description = "Bilgisayarı uyku moduna alır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import ctypes

        ctypes.windll.powrprof.SetSuspendState(False, True, True)
        return ToolResult(success=True, message="Bilgisayar uyku moduna alınıyor.")


@register_tool
class WindowsShutdownTool(BaseTool):
    """Bilgisayarı kapatır. GERİ ALINAMAZ -> onay gerektirir."""

    name = "windows.shutdown"
    description = "Bilgisayarı kapatır."
    danger_level = DangerLevel.CONFIRM_REQUIRED

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
        return ToolResult(success=True, message="Bilgisayar kapatılıyor.")


@register_tool
class WindowsRestartTool(BaseTool):
    """Bilgisayarı yeniden başlatır. GERİ ALINAMAZ -> onay gerektirir."""

    name = "windows.restart"
    description = "Bilgisayarı yeniden başlatır."
    danger_level = DangerLevel.CONFIRM_REQUIRED

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        subprocess.run(["shutdown", "/r", "/t", "0"], check=False)
        return ToolResult(success=True, message="Bilgisayar yeniden başlatılıyor.")


@register_tool
class WindowsSetVolumeTool(BaseTool):
    """Ses seviyesini yükseltir/azaltır/sessize alır (göreceli adım bazlı)."""

    name = "windows.set_volume"
    description = "Ses seviyesini artırır, azaltır veya sessize alır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "mute"]},
                "steps": {"type": "integer", "default": 5, "description": "up/down için tekrar sayısı."},
            },
            "required": ["direction"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        direction = arguments["direction"]
        steps = arguments.get("steps", 5)

        key_map = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}
        if direction not in key_map:
            return ToolResult(success=False, message=f"Geçersiz yön: '{direction}'")

        repeats = 1 if direction == "mute" else steps

        try:
            import pyautogui

            for _ in range(repeats):
                pyautogui.press(key_map[direction])
        except Exception as exc:  # noqa: BLE001 - pyautogui bu oturumda çalışamayabilir
            return ToolResult(
                success=False,
                message=f"Ses ayarlanamadı (bu oturumda ekran/girdi erişimi kısıtlı olabilir): {exc}",
            )

        return ToolResult(success=True, message=f"Ses '{direction}' olarak ayarlandı.")


@register_tool
class WindowsSetBrightnessTool(BaseTool):
    """Ekran parlaklığını WMI üzerinden ayarlar (DDC/CI destekleyen monitörlerde)."""

    name = "windows.set_brightness"
    description = "Ekran parlaklığını yüzde olarak ayarlar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"level": {"type": "integer", "description": "0-100 arası parlaklık yüzdesi."}},
            "required": ["level"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        level = max(0, min(100, int(arguments["level"])))

        try:
            import win32com.client

            wmi = win32com.client.GetObject(r"winmgmts:\\.\root\wmi")
            methods = wmi.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods")[0]
            methods.WmiSetBrightness(1, level)
        except Exception as exc:  # noqa: BLE001 - donanım/monitör desteği garanti değil
            return ToolResult(
                success=False,
                message=f"Parlaklık ayarlanamadı (monitör WMI/DDC-CI desteklemiyor olabilir): {exc}",
            )

        return ToolResult(success=True, message=f"Parlaklık %{level} olarak ayarlandı.")


@register_tool
class WindowsScreenshotTool(BaseTool):
    """Ekran görüntüsü alır ve dosyaya kaydeder."""

    name = "windows.screenshot"
    description = "Ekran görüntüsü alır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"location": {"type": "string", "default": "desktop"}},
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import pyautogui

        location = arguments.get("location", "desktop")
        base_path = context.settings.desktop_path if location == "desktop" else context.settings.downloads_path
        base_path.mkdir(parents=True, exist_ok=True)

        filename = f"artemis_screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
        full_path = base_path / filename

        try:
            image = pyautogui.screenshot()
            image.save(full_path)
        except Exception as exc:  # noqa: BLE001 - ekran kilitli/oturum masaüstüne erişilemiyor olabilir
            return ToolResult(
                success=False,
                message=(
                    "Ekran görüntüsü alınamadı (ekran kilitli olabilir ya da oturum masaüstüne "
                    f"erişilemiyor olabilir): {exc}"
                ),
            )

        context.memory.remember_last_path(str(full_path))
        return ToolResult(success=True, message=f"Ekran görüntüsü kaydedildi: '{full_path}'", data={"path": str(full_path)})


@register_tool
class WindowsClipboardCopyTool(BaseTool):
    """Verilen metni panoya kopyalar."""

    name = "windows.clipboard_copy"
    description = "Bir metni panoya kopyalar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import win32clipboard

        text = arguments["text"]
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

        return ToolResult(success=True, message="Metin panoya kopyalandı.")


@register_tool
class WindowsListWindowsTool(BaseTool):
    """Açık pencerelerin başlıklarını listeler."""

    name = "windows.list_windows"
    description = "Açık pencereleri listeler."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import win32gui

        titles: list[str] = []

        def _collect(hwnd: int, _extra: Any) -> None:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    titles.append(title)

        win32gui.EnumWindows(_collect, None)
        return ToolResult(success=True, message=f"{len(titles)} pencere bulundu.", data={"windows": titles})


@register_tool
class WindowsFocusWindowTool(BaseTool):
    """Başlığı verilen sorguyla eşleşen ilk pencereyi öne getirir."""

    name = "windows.focus_window"
    description = "Belirtilen pencereyi öne getirir."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"title_query": {"type": "string"}},
            "required": ["title_query"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import win32gui

        query = arguments["title_query"].lower()
        match: dict[str, int] = {}

        def _find(hwnd: int, _extra: Any) -> None:
            if win32gui.IsWindowVisible(hwnd) and query in win32gui.GetWindowText(hwnd).lower():
                match.setdefault("hwnd", hwnd)

        win32gui.EnumWindows(_find, None)

        if "hwnd" not in match:
            return ToolResult(success=False, message=f"'{arguments['title_query']}' ile eşleşen pencere bulunamadı.")

        win32gui.SetForegroundWindow(match["hwnd"])
        return ToolResult(success=True, message=f"'{arguments['title_query']}' penceresi öne getirildi.")
