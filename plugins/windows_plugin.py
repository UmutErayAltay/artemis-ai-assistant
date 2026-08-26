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

import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult
from plugins._app_resolver import AppResolver
from plugins.filesystem_plugin import _resolve_location

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
    description = (
        "Bilgisayarda KURULU bir uygulamayı veya oyunu başlatır "
        "(örn. Discord, Steam, Valorant, League of Legends, Chrome, not defteri). "
        "Kullanıcı bir program/oyun adı söyleyip 'aç' veya 'başlat' dediğinde BU kullanılır."
    )
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


_GRACEFUL_CLOSE_WAIT_SECONDS: float = 2.0
"""`_send_graceful_close` ile `WM_CLOSE` gönderildikten sonra, hâlâ ayakta
olan kök süreçleri zorla kapatmadan (`terminate()`) önce beklenecek süre
(saniye). Uygulamaya kendi kapanma akışını (oturum/sekme kaydetme gibi)
tamamlaması için makul bir pay tanır. Testler bu süreyi hızlandırmak
isterse modülü monkeypatch'leyebilir."""


def _is_root_process(proc: Any, matched_pids: set[int]) -> bool:
    """`proc`, `matched_pids` kümesindeki başka bir sürecin ÇOCUĞU mu?

    Modern uygulamalar (örn. Chromium tabanlı tarayıcılar) tek başlatmada
    10+ süreç açabilir; hepsini tek tek öldürmek yerine yalnızca KÖK
    süreç hedeflenir (ana süreç kapanınca çocukları da kendiliğinden
    kapanır). Bir sürecin üst sürecinin PID'i de `matched_pids` içindeyse
    bu süreç çocuktur. Üst sürece erişilemiyorsa (zaten kapanmış ya da
    yetki yok) güvenli tarafta kalınıp KÖK sayılır.
    """

    import psutil

    try:
        parent = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True

    if parent is None:
        return True

    return parent.pid not in matched_pids


def _send_graceful_close(pids: set[int], logger: logging.Logger) -> None:
    """Verilen PID'lere ait görünür ana pencerelere `WM_CLOSE` gönderir.

    Bu, `terminate()` (TerminateProcess) ile zorla öldürmek yerine
    uygulamanın kendi kapanma akışını (oturum kaydetme, "kaydetmek ister
    misiniz?" diyaloğu gibi) çalıştırmasına izin verir. `pywin32` kurulu
    değilse (örn. Windows dışı bir test ortamı) sessizce hiçbir şey
    yapmadan döner — çağıran taraf (Adım 4) hâlâ ayakta kalan süreçleri
    zaten zorla kapatacaktır.
    """

    if not pids:
        return

    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return

    def _on_window(hwnd: int, _extra: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001 - pencere bu sırada kapanmış olabilir
            return
        if pid in pids:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    try:
        win32gui.EnumWindows(_on_window, None)
    except Exception as exc:  # noqa: BLE001 - numaralandırma ortama göre başarısız olabilir
        logger.debug("Pencereler numaralandırılırken hata oluştu (zorla kapatmaya geçilecek): %s", exc)


@register_tool
class WindowsCloseAppTool(BaseTool):
    """Adı eşleşen çalışan süreçleri önce NAZİKÇE, gerekirse ZORLA kapatır.

    Akış:
      1) İsim alt-dizgesiyle eşleşen tüm süreçler bulunur (hemen
         öldürülmez, önce bir listede toplanır).
      2) Yalnızca KÖK süreçler hedeflenir (bkz. `_is_root_process`) —
         üst süreci de eşleşenler arasında olan bir süreç ÇOCUKTUR ve
         ayrıca hedeflenmez.
      3) Kök süreçlerin görünür ana pencerelerine `WM_CLOSE` gönderilir
         (bkz. `_send_graceful_close`); bu, uygulamanın kendi kapanma
         akışını (oturum/sekme kaydetme gibi) çalıştırmasına izin verir.
         `pywin32` yoksa bu adım sessizce atlanır.
      4) `_GRACEFUL_CLOSE_WAIT_SECONDS` kadar beklenir; hâlâ ayakta olan
         kök süreçler `terminate()` ile zorla kapatılır. Erişim reddi/
         süreç zaten yoksa DEBUG seviyesinde loglanır (korumalı yardımcı
         süreçler için bu normaldir, log'u WARNING ile kirletmez).
      5) Sonuç dürüstçe raporlanır: kaç uygulamanın nazikçe/zorla
         kapandığı, benzersiz uygulama adlarıyla özetlenir (ham PID/süreç
         listesi kullanıcıya basılmaz, `data` alanında ayrıntı olarak
         kalır).

    Neden `danger_level = SAFE`: eskiden bu tool doğrudan `terminate()`
    (TerminateProcess) çağırıyordu; bu, uygulamaya kaydetme şansı
    tanımadığı için sessiz veri kaybına yol açabiliyordu. Artık önce
    nazik kapatma (`WM_CLOSE`) denendiği için uygulama -destekliyorsa-
    kendi kaydetme/onay akışını çalıştırabiliyor; zorla kapatma yalnızca
    uygulama makul bir süre içinde yanıt vermediğinde devreye giriyor.
    Bu yüzden kullanıcı onayı gerektirmiyor.
    """

    name = "windows.close_app"
    description = (
        "Bir uygulamayı kapatır: önce ana penceresine kapanma sinyali "
        "gönderip uygulamanın kendi kapanma akışını (kaydetme/onay "
        "diyaloğu gibi) çalıştırmasına fırsat tanır, kısa bir "
        "bekleyişin ardından hâlâ açık kalan süreçleri zorla kapatır."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Kapatılacak process/uygulama adı."}},
            "required": ["name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import psutil

        query_name = arguments["name"]
        query = query_name.lower()

        # Adım 1: eşleşen tüm süreçleri topla, hemen öldürme.
        matches = [
            proc
            for proc in psutil.process_iter(["pid", "name"])
            if query in (proc.info.get("name") or "").lower()
        ]

        if not matches:
            return ToolResult(success=False, message=f"'{query_name}' ile eşleşen çalışan süreç bulunamadı.")

        # Adım 2: yalnızca kök süreçleri hedefle (çocukları ayrıca öldürme).
        matched_pids = {proc.pid for proc in matches}
        roots = [proc for proc in matches if _is_root_process(proc, matched_pids)]
        root_pids = {proc.pid for proc in roots}

        # Adım 3: nazikçe kapatmayı dene (pywin32 yoksa sessizce atlanır).
        _send_graceful_close(root_pids, context.logger)

        # Adım 4: kısa bir süre bekle, hâlâ ayakta olanları zorla kapat.
        time.sleep(_GRACEFUL_CLOSE_WAIT_SECONDS)

        closed: list[dict[str, Any]] = []
        for proc in roots:
            proc_name = proc.info.get("name") or str(proc.pid)
            try:
                if not proc.is_running():
                    closed.append({"pid": proc.pid, "name": proc_name, "method": "graceful"})
                    continue
                proc.terminate()
                closed.append({"pid": proc.pid, "name": proc_name, "method": "forced"})
            except psutil.NoSuchProcess:
                closed.append({"pid": proc.pid, "name": proc_name, "method": "graceful"})
            except psutil.AccessDenied as exc:
                context.logger.debug(
                    "Süreç kapatılamadı (korumalı bir yardımcı süreç olabilir): %s (pid=%s): %s",
                    proc_name,
                    proc.pid,
                    exc,
                )

        if not closed:
            return ToolResult(
                success=False,
                message=f"'{query_name}' eşleşen süreçler bulundu ama hiçbiri kapatılamadı (erişim reddedildi).",
                data={"matched_pids": sorted(matched_pids)},
            )

        # Adım 5: dürüst ve okunabilir bir özet üret (ham süreç listesi değil).
        graceful = [c for c in closed if c["method"] == "graceful"]
        forced = [c for c in closed if c["method"] == "forced"]

        parts = []
        if graceful:
            names = ", ".join(sorted({c["name"] for c in graceful}))
            parts.append(f"{len(graceful)} uygulama nazikçe ({names})")
        if forced:
            names = ", ".join(sorted({c["name"] for c in forced}))
            parts.append(f"{len(forced)} uygulama zorla ({names})")

        message = f"'{query_name}' kapatıldı: {', '.join(parts)}."
        return ToolResult(success=True, message=message, data={"closed": closed})


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

        # Koşulsuz success=True yasak: `LockWorkStation` bir BOOL döner ve
        # etkileşimli olmayan bir oturumdan (servis, uzak masaüstü oturumu
        # kapalıyken) çağrıldığında 0 dönerek SESSİZCE başarısız olur.
        try:
            locked = ctypes.windll.user32.LockWorkStation()
        except (AttributeError, OSError) as exc:
            return ToolResult(success=False, message=f"Bilgisayar kilitlenemedi: {exc}")

        if not locked:
            return ToolResult(
                success=False,
                message="Bilgisayar kilitlenemedi (bu oturumdan kilitleme izni olmayabilir).",
            )
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

        # Koşulsuz success=True yasak: `SetSuspendState` bir BOOLEAN döner.
        # Uyku BIOS/donanım seviyesinde devre dışıysa (bkz. `.context`
        # §6.6 — geliştirici makinesinde tam olarak bu durum var) çağrı
        # 0 döner ve hiçbir şey olmaz. "Uykuya alınıyor" deyip uyanık
        # kalmak, dürüstçe başarısız olmaktan kötüdür.
        try:
            suspended = ctypes.windll.powrprof.SetSuspendState(False, True, True)
        except (AttributeError, OSError) as exc:
            return ToolResult(success=False, message=f"Uyku moduna geçilemedi: {exc}")

        if not suspended:
            return ToolResult(
                success=False,
                message="Uyku moduna geçilemedi (bu makinede uyku devre dışı olabilir).",
            )
        return ToolResult(success=True, message="Bilgisayar uyku moduna alınıyor.")


_SHUTDOWN_TIMEOUT_SECONDS = 10.0
"""`shutdown.exe`'nin cevap vermesi için tanınan süre.

Komut kapanmayı PLANLAR ve hemen döner (`/t 0` ile bile), yani normalde
saniyenin altında biter. Zaman aşımı, komutun bir sebeple asılı kalması
durumunda dispatcher'ı (ve ses işçisini) süresiz kilitlememek içindir.
"""


def _run_shutdown_command(flag: str, success_message: str, failure_prefix: str) -> ToolResult:
    """`shutdown.exe`'yi çalıştırır ve SONUCUNU doğrular.

    Koşulsuz success=True yasak (CLAUDE.md): burada bir dönem
    `subprocess.run([...], check=False)` vardı ve hemen ardından
    koşulsuz `success=True` dönülüyordu — yani çıkış kodu AÇIKÇA
    atılıyordu. `shutdown.exe` başarısız olabilir ve olur:

        1190 - zaten planlanmış bir kapanma var
        1314 - gerekli ayrıcalık (SeShutdownPrivilege) yok
        5    - erişim reddedildi (grup ilkesi/etki alanı kısıtı)

    Bu durumda kullanıcı "Bilgisayar kapatılıyor." mesajını duyup
    bilgisayarın kapanmasını beklerdi. Üstelik bu tool CONFIRM_REQUIRED —
    yani kullanıcının bilinçli olarak onayladığı bir işlemde yalan
    söyleniyordu.
    """

    try:
        completed = subprocess.run(
            ["shutdown", flag, "/t", "0"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            success=False,
            message=f"{failure_prefix}: 'shutdown' komutu {_SHUTDOWN_TIMEOUT_SECONDS:.0f} sn içinde cevap vermedi.",
        )
    except (OSError, FileNotFoundError) as exc:
        return ToolResult(success=False, message=f"{failure_prefix}: 'shutdown' komutu çalıştırılamadı ({exc}).")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        suffix = f": {detail}" if detail else "."
        return ToolResult(
            success=False,
            message=f"{failure_prefix} (shutdown çıkış kodu {completed.returncode}){suffix}",
        )

    return ToolResult(success=True, message=success_message)


@register_tool
class WindowsShutdownTool(BaseTool):
    """Bilgisayarı kapatır. GERİ ALINAMAZ -> onay gerektirir."""

    name = "windows.shutdown"
    description = "Bilgisayarı kapatır."
    danger_level = DangerLevel.CONFIRM_REQUIRED

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return _run_shutdown_command("/s", "Bilgisayar kapatılıyor.", "Bilgisayar kapatılamadı")


@register_tool
class WindowsRestartTool(BaseTool):
    """Bilgisayarı yeniden başlatır. GERİ ALINAMAZ -> onay gerektirir."""

    name = "windows.restart"
    description = "Bilgisayarı yeniden başlatır."
    danger_level = DangerLevel.CONFIRM_REQUIRED

    def get_arguments_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return _run_shutdown_command(
            "/r", "Bilgisayar yeniden başlatılıyor.", "Bilgisayar yeniden başlatılamadı"
        )


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
            "properties": {
                "location": {
                    "type": "string",
                    "default": "desktop",
                    "description": "desktop / downloads / last ya da tam yol.",
                }
            },
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import pyautogui

        location = arguments.get("location", "desktop")

        # `location` çözümlemesi filesystem tool'larıyla AYNI yerden gelir.
        # Burada bir dönem elle yazılmış bir üçlü koşul vardı
        # (`desktop_path if location == "desktop" else downloads_path`) ve
        # `"desktop"` DIŞINDAKİ HER değeri — `"last"`ı da, tam bir yolu da
        # — sessizce indirilenler klasörüne yönlendiriyordu. Yani aynı
        # argüman adı bu tool'da başka bir sözleşme konuşuyordu:
        # kullanıcı `location: "C:/temp"` dediğinde dosya sessizce başka
        # bir yere yazılıyordu.
        base_path = _resolve_location(location, context)
        try:
            base_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(success=False, message=f"'{base_path}' klasörü oluşturulamadı: {exc}")

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
        return ToolResult(
            success=True,
            message=f"Ekran görüntüsü '{full_path}' olarak kaydedildi.",
            data={"path": str(full_path)},
        )


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

        # Koşulsuz success=True yasak. Pano PAYLAŞILAN bir kaynaktır: başka
        # bir uygulama onu kilitlemiş olabilir (`OpenClipboard` hata verir)
        # ya da yazdıktan hemen sonra üzerine yazabilir. Yazılan değeri
        # GERİ OKUMAK, bu tool için elde edilebilecek gerçek başarı
        # sinyalidir ve API bunu bedava sunuyor.
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                written = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:  # noqa: BLE001 - pano başka bir uygulama tarafından kilitli olabilir
            return ToolResult(success=False, message=f"Panoya kopyalanamadı: {exc}")

        if written != text:
            return ToolResult(
                success=False,
                message="Panoya yazıldı ama geri okunan değer farklı (başka bir uygulama panoyu değiştirmiş olabilir).",
            )

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


def _find_window_by_title(title_query: str) -> int | None:
    """Başlığında `title_query` (küçük/büyük harf duyarsız) geçen İLK
    görünür pencerenin `hwnd`'ini bulur.

    `WindowsFocusWindowTool` ve `WindowsArrangeWindowTool` arasında
    paylaşılan ortak arama mantığı — üçüncü kullanımda (bkz.
    `plugins/_app_resolver.py`'nin "ortak yardımcı" deseni) inline
    tekrardan buraya çıkarıldı.

    Returns:
        Eşleşen pencerenin `hwnd`'i; hiçbiri bulunamazsa None.
    """

    import win32gui

    query = title_query.lower()
    match: dict[str, int] = {}

    def _find(hwnd: int, _extra: Any) -> None:
        if win32gui.IsWindowVisible(hwnd) and query in win32gui.GetWindowText(hwnd).lower():
            match.setdefault("hwnd", hwnd)

    win32gui.EnumWindows(_find, None)
    return match.get("hwnd")


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

        title_query = arguments["title_query"]
        hwnd = _find_window_by_title(title_query)
        if hwnd is None:
            return ToolResult(success=False, message=f"'{title_query}' ile eşleşen pencere bulunamadı.")

        # Koşulsuz success=True yasak: `SetForegroundWindow` Windows'un ön
        # plan kilidi (foreground lock) kuralları yüzünden RUTİN OLARAK
        # başarısız olur — çağıran süreç ön planda değilse, kullanıcı
        # başka bir pencereyle etkileşimdeyse ya da hedef pencere
        # minimize ise sistem isteği sessizce reddedip 0 döndürür.
        # Kardeş tool `windows.arrange_window` sonucu zaten
        # `GetWindowPlacement` ile doğruluyor; bu tool tek istisnaydı.
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as exc:  # noqa: BLE001 - ön plan kilidi pywin32'de istisna da fırlatabilir
            return ToolResult(
                success=False,
                message=f"'{title_query}' penceresi öne getirilemedi (Windows ön plan kısıtı): {exc}",
            )

        if win32gui.GetForegroundWindow() != hwnd:
            return ToolResult(
                success=False,
                message=f"'{title_query}' penceresi öne getirilemedi (Windows isteği reddetti).",
            )

        return ToolResult(success=True, message=f"'{title_query}' penceresi öne getirildi.")


@register_tool
class WindowsArrangeWindowTool(BaseTool):
    """Bir pencereyi küçültür/büyütür/eski haline getirir ya da ekranın
    yarısına yerleştirir (snap).

    Beş ayrı tool yerine TEK tool + `position` enum (bkz. `browser.
    switch_tab`'ın `direction` enum deseni): her yeni tool sistem
    promptu bütçesine ekleniyor (README §26/§28b), enum konsolidasyonu
    bunu tek bir manifest girdisinde tutar.
    """

    name = "windows.arrange_window"
    description = "Bir pencereyi küçültür, büyütür, eski haline getirir veya ekranın yarısına yerleştirir."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title_query": {"type": "string", "description": "Pencere başlığında aranacak metin."},
                "position": {
                    "type": "string",
                    "enum": ["minimize", "maximize", "restore", "snap_left", "snap_right"],
                },
            },
            "required": ["title_query", "position"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import win32con
        import win32gui

        title_query = arguments["title_query"]
        position = arguments["position"]

        hwnd = _find_window_by_title(title_query)
        if hwnd is None:
            return ToolResult(success=False, message=f"'{title_query}' ile eşleşen pencere bulunamadı.")

        if position == "minimize":
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        elif position == "maximize":
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        elif position == "restore":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        elif position in ("snap_left", "snap_right"):
            error = _snap_window(hwnd, position)
            if error is not None:
                return ToolResult(success=False, message=error)
        else:
            return ToolResult(success=False, message=f"Geçersiz konum: '{position}'.")

        # Koşulsuz success=True yasak: `ShowWindow`/`MoveWindow` İSTEK
        # gönderir ama pencere yöneticisi reddedebilir (örn. bazı
        # uygulamalar küçültülmeyi engeller); GERÇEK durumu
        # `GetWindowPlacement` ile okuyup doğrula.
        show_cmd = win32gui.GetWindowPlacement(hwnd)[1]
        if position == "minimize" and show_cmd != win32con.SW_SHOWMINIMIZED:
            return ToolResult(success=False, message="Pencere küçültülemedi (uygulama reddetmiş olabilir).")
        if position == "maximize" and show_cmd != win32con.SW_SHOWMAXIMIZED:
            return ToolResult(success=False, message="Pencere büyütülemedi.")

        labels = {
            "minimize": "küçültüldü",
            "maximize": "büyütüldü",
            "restore": "eski haline getirildi",
            "snap_left": "ekranın soluna yerleştirildi",
            "snap_right": "ekranın sağına yerleştirildi",
        }
        return ToolResult(success=True, message=f"'{title_query}' penceresi {labels[position]}.")


def _snap_window(hwnd: int, position: str) -> str | None:
    """Pencereyi, bulunduğu monitörün çalışma alanının (görev çubuğu
    HARİÇ) sol ya da sağ yarısına yerleştirir.

    Returns:
        Hata mesajı (başarısızsa), başarılıysa None.
    """

    import win32api
    import win32con
    import win32gui

    monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    work_left, work_top, work_right, work_bottom = win32api.GetMonitorInfo(monitor)["Work"]
    half_width = (work_right - work_left) // 2
    height = work_bottom - work_top
    x = work_left if position == "snap_left" else work_left + half_width

    # Küçültülmüşse önce eski haline getir — küçük bir pencereyi
    # taşımak/boyutlandırmak anlamsız/görünmez bir sonuç verirdi.
    if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    win32gui.MoveWindow(hwnd, x, work_top, half_width, height, True)

    # Doğrulama: pencere gerçekten hedeflenen yarıya mı gitti (birkaç
    # pikselllik pencere-çerçevesi payı toleransla).
    left, _top, right, _bottom = win32gui.GetWindowRect(hwnd)
    if abs(left - x) > 20 or abs((right - left) - half_width) > 40:
        return "Pencere taşınamadı ya da beklenen konuma ulaşmadı."
    return None
