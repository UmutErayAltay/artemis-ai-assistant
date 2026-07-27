"""Fare ve klavye kontrolü ile ilgili tool'lar.

Bu plugin `pyautogui` ile ekrana doğrudan fare/klavye girdisi gönderir:
imleci taşımak, tıklamak, metin yazmak, tek bir tuşa/kısayola basmak ve
sayfa/pencere kaydırmak. `plugins/windows_plugin.py`'daki `windows.
set_volume`/`windows.screenshot` zaten `pyautogui`'yi benzer amaçla
kullanıyordu; bu plugin o kullanımı genelleştirilmiş, doğrudan kullanıcı
tarafından yönlendirilen bir fare/klavye kontrol setine çıkarır.

Tasarım notu: `pyautogui`, diğer Windows'a özgü bağımlılıklar gibi
(`windows_plugin.py`'ye bkz.) dosya başında değil, yalnızca ilgili
`execute()` içinde "lazy" olarak import edilir; böylece Linux/CI'da bu
plugin import edilirken hata verilmez.

Güvenlik notu (danger_level kararı): bu plugin'deki tüm tool'lar
`DangerLevel.SAFE`'tir. Bir tıklamanın veya tuş basışının ekranda
teorik olarak istenmeyen bir sonuca yol açabilmesi (örn. yanlışlıkla bir
"Tümünü Sil" düğmesine denk gelmek) doğru ama projedeki SAFE/CONFIRM_
REQUIRED ayrımı "geri alınabilirlik" eksenine göre çizilmiş (bkz.
CLAUDE.md: delete/shutdown/restart/format/registry/service). Bir fare
tıklaması veya tuş basışı KENDİSİ geri alınamaz bir sistem işlemi
değildir — tıpkı bir insanın fare/klavye kullanması gibi, sonucu
tıklanan/odaklanan uygulamaya bağlıdır. Ayrıca `windows.close_app` ve
`windows.set_volume` gibi zaten kayıtlı emsal tool'lar da dolaylı ve
geri alınması güç sonuçlar doğurabildiği halde SAFE'tir. Riski asıl
azaltan mekanizma, LLM'in koordinat/metin üretirken kullanıcının açık
talimatı dışına çıkmaması gerektiğidir (sistem promptu sorumluluğu);
bu tool'lara CONFIRM_REQUIRED eklemek gerçek bir güvenlik kazandırmaz,
yalnızca her sıradan tıklamayı/yazmayı onay diyaloğuna boğar.
"""

from __future__ import annotations

from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult

# pyautogui'nin tanıdığı fare düğmesi adları.
_VALID_MOUSE_BUTTONS = frozenset({"left", "right", "middle"})


def _validate_coordinates(x: int, y: int) -> str | None:
    """Verilen koordinatların ekran sınırları içinde olup olmadığını denetler.

    LLM'in ürettiği koordinatlar körü körüne güvenilmez: negatif ya da
    ekran boyutunu aşan bir değer, imleci ikinci bir monitöre ya da
    tamamen anlamsız bir noktaya taşıyabilir. `pyautogui.size()`
    çağrılamıyorsa (ör. ekran/oturum erişimi yok) doğrulama atlanır ve
    asıl `pyautogui` çağrısının kendi hatası dürüstçe raporlanır.

    Returns:
        Hata mesajı (koordinat geçersizse), geçerliyse None.
    """

    if x < 0 or y < 0:
        return f"Koordinatlar negatif olamaz: ({x}, {y})."

    try:
        import pyautogui

        width, height = pyautogui.size()
    except Exception:  # noqa: BLE001 - ekran boyutu okunamıyorsa doğrulamayı atla
        return None

    if x > width or y > height:
        return f"Koordinatlar ekran sınırlarının dışında: ({x}, {y}), ekran {width}x{height}."

    return None


@register_tool
class MouseMoveTool(BaseTool):
    """Fare imlecini belirtilen koordinata taşır."""

    name = "mouse_keyboard.move_mouse"
    description = "Fare imlecini ekranda belirtilen (x, y) koordinatına taşır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Hedef X koordinatı (piksel)."},
                "y": {"type": "integer", "description": "Hedef Y koordinatı (piksel)."},
                "duration": {
                    "type": "number",
                    "default": 0.0,
                    "description": "Hareketin süreceği saniye (0 = anında).",
                },
            },
            "required": ["x", "y"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        x, y = int(arguments["x"]), int(arguments["y"])
        duration = float(arguments.get("duration", 0.0))

        error = _validate_coordinates(x, y)
        if error is not None:
            return ToolResult(success=False, message=error)

        try:
            import pyautogui

            pyautogui.moveTo(x, y, duration=duration)
        except Exception as exc:  # noqa: BLE001 - ekran/girdi erişimi bu oturumda kısıtlı olabilir
            return ToolResult(success=False, message=f"İmleç taşınamadı: {exc}")

        # Koşulsuz success=True yasak: imlecin gerçekten hedefe ulaştığını,
        # `pyautogui.position()` ile son konumu okuyup doğrula.
        final_x, final_y = pyautogui.position()
        if (final_x, final_y) != (x, y):
            return ToolResult(
                success=False,
                message=f"İmleç hedefe ulaşmadı: istenen ({x}, {y}), gerçek ({final_x}, {final_y}).",
            )

        return ToolResult(success=True, message=f"İmleç ({x}, {y}) konumuna taşındı.")


@register_tool
class MouseClickTool(BaseTool):
    """Belirtilen koordinatta (veya mevcut konumda) fare tıklaması yapar."""

    name = "mouse_keyboard.click"
    description = (
        "Ekranda belirtilen (x, y) koordinatında fare tıklaması yapar; "
        "koordinat verilmezse imlecin mevcut konumunda tıklar."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Hedef X koordinatı (opsiyonel)."},
                "y": {"type": "integer", "description": "Hedef Y koordinatı (opsiyonel)."},
                "button": {
                    "type": "string",
                    "enum": sorted(_VALID_MOUSE_BUTTONS),
                    "default": "left",
                    "description": "Kullanılacak fare düğmesi.",
                },
                "double": {
                    "type": "boolean",
                    "default": False,
                    "description": "True ise çift tıklama yapılır.",
                },
            },
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        x = arguments.get("x")
        y = arguments.get("y")
        button = arguments.get("button", "left")
        double = bool(arguments.get("double", False))

        if (x is None) != (y is None):
            return ToolResult(success=False, message="'x' ve 'y' birlikte verilmeli (yalnızca biri değil).")

        if button not in _VALID_MOUSE_BUTTONS:
            return ToolResult(success=False, message=f"Geçersiz fare düğmesi: '{button}'.")

        if x is not None and y is not None:
            x, y = int(x), int(y)
            error = _validate_coordinates(x, y)
            if error is not None:
                return ToolResult(success=False, message=error)

        try:
            import pyautogui

            click_kwargs: dict[str, Any] = {"button": button}
            if x is not None and y is not None:
                click_kwargs["x"] = x
                click_kwargs["y"] = y

            if double:
                pyautogui.doubleClick(**click_kwargs)
            else:
                pyautogui.click(**click_kwargs)
        except Exception as exc:  # noqa: BLE001 - ekran/girdi erişimi bu oturumda kısıtlı olabilir
            return ToolResult(success=False, message=f"Tıklama gerçekleştirilemedi: {exc}")

        # Tıklamanın kendisinin "başarılı" olup olmadığını gözlemlemenin bir
        # yolu yok (hangi uygulamanın tepki verdiğini bilemeyiz); en azından
        # hedef koordinat verildiyse imlecin gerçekten oraya ulaştığını
        # doğrulayarak "koşulsuz success=True" yasağına uyuyoruz. Koordinat
        # verilmediyse (mevcut konumda tıklama) exception fırlamaması tek
        # doğrulama sinyalidir.
        if x is not None and y is not None:
            final_x, final_y = pyautogui.position()
            if (final_x, final_y) != (x, y):
                return ToolResult(
                    success=False,
                    message=f"İmleç hedefe ulaşmadı, tıklama şüpheli: istenen ({x}, {y}), gerçek ({final_x}, {final_y}).",
                )

        click_label = "çift tıklama" if double else "tıklama"
        location_label = f"({x}, {y})" if x is not None else "mevcut konumda"
        return ToolResult(success=True, message=f"{location_label} {button} {click_label} yapıldı.")


@register_tool
class KeyboardTypeTextTool(BaseTool):
    """Odaklanmış alana verilen metni tuş tuş yazdırır."""

    name = "mouse_keyboard.type_text"
    description = "Odaktaki alana (ör. bir metin kutusuna) verilen metni yazar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Yazılacak metin."},
                "interval": {
                    "type": "number",
                    "default": 0.0,
                    "description": "Karakterler arası gecikme (saniye).",
                },
            },
            "required": ["text"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        text = arguments["text"]
        interval = float(arguments.get("interval", 0.0))

        if not text:
            return ToolResult(success=False, message="Yazılacak metin boş olamaz.")

        try:
            import pyautogui

            pyautogui.write(text, interval=interval)
        except Exception as exc:  # noqa: BLE001 - ekran/girdi erişimi bu oturumda kısıtlı olabilir
            return ToolResult(success=False, message=f"Metin yazılamadı: {exc}")

        # `pyautogui.write` hangi alana yazıldığını doğrulayamaz (odak
        # kontrolümüzde değil); exception fırlamaması bu tool için elde
        # edilebilecek tek dürüst başarı sinyalidir (bkz. modül dokümanı).
        return ToolResult(success=True, message=f"{len(text)} karakter yazıldı.")


@register_tool
class KeyboardPressKeyTool(BaseTool):
    """Tek bir tuşa veya '+' ile ayrılmış bir kısayol kombinasyonuna basar."""

    name = "mouse_keyboard.press_key"
    description = (
        "Tek bir tuşa (ör. 'enter', 'esc', 'f5') veya '+' ile ayrılmış bir "
        "kısayol kombinasyonuna (ör. 'ctrl+c', 'alt+tab') basar."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Tuş adı ya da '+' ile ayrılmış kısayol (ör. 'ctrl+shift+esc').",
                }
            },
            "required": ["keys"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_keys = arguments["keys"].strip()
        if not raw_keys:
            return ToolResult(success=False, message="'keys' boş olamaz.")

        key_parts = [part.strip().lower() for part in raw_keys.split("+") if part.strip()]
        if not key_parts:
            return ToolResult(success=False, message=f"Geçersiz tuş/kısayol: '{raw_keys}'.")

        try:
            import pyautogui

            if len(key_parts) == 1:
                pyautogui.press(key_parts[0])
            else:
                pyautogui.hotkey(*key_parts)
        except Exception as exc:  # noqa: BLE001 - bilinmeyen tuş adı ya da girdi erişimi kısıtlı olabilir
            return ToolResult(success=False, message=f"'{raw_keys}' tuşuna basılamadı: {exc}")

        return ToolResult(success=True, message=f"'{raw_keys}' tuşuna basıldı.")


@register_tool
class MouseScrollTool(BaseTool):
    """Fareyi dikey olarak kaydırır (sayfa/pencere kaydırma)."""

    name = "mouse_keyboard.scroll"
    description = (
        "Sayfayı/pencereyi dikey olarak kaydırır. Pozitif 'amount' yukarı, "
        "negatif 'amount' aşağı kaydırır."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "Kaydırma miktarı (pozitif=yukarı, negatif=aşağı).",
                },
                "x": {"type": "integer", "description": "Kaydırmanın yapılacağı X konumu (opsiyonel)."},
                "y": {"type": "integer", "description": "Kaydırmanın yapılacağı Y konumu (opsiyonel)."},
            },
            "required": ["amount"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        amount = int(arguments["amount"])
        x = arguments.get("x")
        y = arguments.get("y")

        if amount == 0:
            return ToolResult(success=False, message="Kaydırma miktarı 0 olamaz.")

        if (x is None) != (y is None):
            return ToolResult(success=False, message="'x' ve 'y' birlikte verilmeli (yalnızca biri değil).")

        scroll_kwargs: dict[str, Any] = {}
        if x is not None and y is not None:
            x, y = int(x), int(y)
            error = _validate_coordinates(x, y)
            if error is not None:
                return ToolResult(success=False, message=error)
            scroll_kwargs = {"x": x, "y": y}

        try:
            import pyautogui

            pyautogui.scroll(amount, **scroll_kwargs)
        except Exception as exc:  # noqa: BLE001 - ekran/girdi erişimi bu oturumda kısıtlı olabilir
            return ToolResult(success=False, message=f"Kaydırma yapılamadı: {exc}")

        direction_label = "yukarı" if amount > 0 else "aşağı"
        return ToolResult(success=True, message=f"{abs(amount)} birim {direction_label} kaydırıldı.")
