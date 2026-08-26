"""Windows plugin testleri.

Bazı tool'lar (lock, sleep, volume, brightness, clipboard, pencere
yönetimi) yalnızca gerçek Windows üzerinde anlamlıdır; bu testler
`sys.platform != "win32"` olduğunda otomatik atlanır. `windows.close_app`
ise psutil kullandığı için (Windows'a özgü olmadığından) her platformda
gerçek anlamda test edilir. Nazik kapatma (Adım 2-3: kök seçimi, WM_CLOSE)
mantığı ise gerçek bir uygulama öldürmeden `psutil.process_iter`'ı sahte
süreçlerle (`_FakeProcess`) monkeypatch'leyerek test edilir; pywin32'ye
gerçekten bağımlı olan tek test (`test_send_graceful_close_*`) da
`sys.modules`'a sahte `win32gui`/`win32con`/`win32process` koyarak
platformdan bağımsız çalışır.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.plugin_loader import load_plugins
from memory.context_memory import ContextMemory

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Yalnızca Windows üzerinde çalışır.")


class _FakeProcess:
    """`psutil.Process` yerine geçen, davranışı senaryoya göre ayarlanabilen sahte süreç.

    Gerçek bir uygulama başlatıp öldürmeden `windows.close_app`'ın kök
    seçimi, nazik-kapatma-sonrası kontrolü ve hata toleransı mantığını
    sınamak için kullanılır.
    """

    def __init__(
        self,
        pid: int,
        name: str,
        parent: "_FakeProcess | None" = None,
        still_running: bool = True,
        terminate_error: Exception | None = None,
    ) -> None:
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._parent = parent
        # Nazik kapatma (WM_CLOSE) + bekleme sonrası hâlâ ayakta mı?
        # False ise "kendiliğinden kapandı" (nazik kapatma başarılı) demektir.
        self._still_running = still_running
        self._terminate_error = terminate_error
        self.terminate_called = False

    def parent(self) -> "_FakeProcess | None":
        return self._parent

    def is_running(self) -> bool:
        return self._still_running

    def terminate(self) -> None:
        self.terminate_called = True
        if self._terminate_error is not None:
            raise self._terminate_error
        self._still_running = False


@pytest.fixture(autouse=True)
def _load_all_plugins() -> None:
    load_plugins()


@pytest.fixture
def _fast_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """`windows.close_app` testlerini gerçek pencere sistemine ve
    `_GRACEFUL_CLOSE_WAIT_SECONDS` kadar gerçekten beklemeye bağımlı
    olmaktan kurtarır: Adım 3'ü (nazik kapatma) no-op yapar, Adım 4'ün
    bekleme süresini sıfırlar. Kök seçimi / terminate-çağrıldı-mı gibi
    Adım 2 ve 4 mantığını sınayan testler bu fixture'ı kullanır."""

    import plugins.windows_plugin as windows_plugin

    monkeypatch.setattr(windows_plugin, "_send_graceful_close", lambda pids, logger: None)
    monkeypatch.setattr(windows_plugin, "_GRACEFUL_CLOSE_WAIT_SECONDS", 0)


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path,
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


def test_shutdown_requires_confirmation(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "windows.shutdown", "arguments": {}})
    assert result.requires_confirmation is True
    assert result.success is False


def test_restart_requires_confirmation(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "windows.restart", "arguments": {}})
    assert result.requires_confirmation is True
    assert result.success is False


def test_close_app_finds_no_match_gracefully(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch(
        {"tool": "windows.close_app", "arguments": {"name": "kesinlikle-var-olmayan-bir-surec-xyz"}}
    )
    assert result.success is False
    assert result.requires_confirmation is False


@WINDOWS_ONLY
def test_close_app_terminates_real_process(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gerçek bir alt süreç başlatıp `windows.close_app` ile kapatır.

    NEDEN `waitfor` KULLANILIYOR (eskiden `sleep` idi): `sleep` bir Windows
    komutu DEĞİLDİR — yalnızca Git Bash/MSYS PATH'inde bulunur. Bu test
    Git Bash'ten çalıştırıldığında geçiyor, düz PowerShell'den
    çalıştırıldığında `FileNotFoundError` ile patlıyordu; yani testi
    yazan kişi hatayı hiç görmüyor, kullanıcı ise sürekli görüyordu.
    `waitfor.exe` her Windows kurulumunda System32'de bulunur.

    Ayrıca süreç ADI benzersiz olmalı: tool eşleşmeyi süreç adının ALT
    DİZGESİYLE yapar, dolayısıyla "python" gibi genel bir adla test etmek
    pytest'in kendi sürecini öldürebilirdi.

    `waitfor` sürecinin görünür bir penceresi olmadığından nazik WM_CLOSE
    onu kapatmaz; akış Adım 4'teki zorla kapatmaya düşer. Testin gerçekten
    2 saniye beklememesi için bekleme sabiti kısaltılır.
    """

    import plugins.windows_plugin as windows_plugin

    monkeypatch.setattr(windows_plugin, "_GRACEFUL_CLOSE_WAIT_SECONDS", 0.1)

    # /t 30: en fazla 30 sn bekle. Sinyal adı benzersiz, kimse göndermez.
    proc = subprocess.Popen(
        ["waitfor.exe", "/t", "30", "ArtemisTestSignal"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Sürecin işletim sisteminin süreç listesine yansımasını BEKLE — sabit
    # bir `sleep` yerine yoklama kullanılır: sabit süre, makine yüklüyken
    # (örn. tüm test paketi koşarken) yetmeyip testi kararsız yapıyordu.
    for _ in range(50):
        if any("waitfor" in (p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])):
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("waitfor süreci process listesinde görünmedi")

    try:
        result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "waitfor"}})
        assert result.success is True
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_close_app_only_targets_root_process_not_child(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, _fast_close: None
) -> None:
    """İki eşleşen sahte süreçten biri diğerinin çocuğuysa (`parent()` eşleşenler
    arasında) yalnızca KÖK hedeflenmeli; çocuğa ayrıca `terminate()` çağrılmamalı
    (bkz. gerçek olay: Brave'in 12 alt süreci tek tek öldürülmüştü)."""

    root = _FakeProcess(pid=100, name="brave.exe", still_running=True)
    child = _FakeProcess(pid=101, name="brave.exe", parent=root, still_running=True)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [root, child])

    result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "brave"}})

    assert result.success is True
    assert root.terminate_called is True
    assert child.terminate_called is False


def test_close_app_skips_terminate_when_graceful_close_succeeds(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, _fast_close: None
) -> None:
    """Nazik kapatmadan (WM_CLOSE) sonra süreç kendiliğinden ölmüşse
    `terminate()` hiç çağrılmamalı — uygulamaya kaydetme şansı tanımak
    bu tool'un artık `danger_level = SAFE` kalmasının gerekçesi."""

    proc = _FakeProcess(pid=200, name="notepad.exe", still_running=False)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [proc])

    result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "notepad"}})

    assert result.success is True
    assert proc.terminate_called is False
    assert result.data["closed"] == [{"pid": 200, "name": "notepad.exe", "method": "graceful"}]
    assert "nazikçe" in result.message


def test_close_app_terminates_when_graceful_close_fails(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, _fast_close: None
) -> None:
    """Nazik kapatma işe yaramazsa (süreç hâlâ ayaktaysa) `terminate()`
    çağrılarak zorla kapatılmalı."""

    proc = _FakeProcess(pid=201, name="notepad.exe", still_running=True)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [proc])

    result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "notepad"}})

    assert result.success is True
    assert proc.terminate_called is True
    assert result.data["closed"] == [{"pid": 201, "name": "notepad.exe", "method": "forced"}]
    assert "zorla" in result.message


def test_close_app_survives_access_denied_and_closes_others(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, _fast_close: None
) -> None:
    """Korumalı bir yardımcı süreç (örn. crash handler) `AccessDenied`
    fırlatsa bile tool çökmemeli; diğer eşleşen kök süreçler kapatılmaya
    devam etmeli (bkz. gerçek olay: BraveCrashHandler.exe erişim reddi
    verip log'u WARNING ile kirletiyordu — artık DEBUG'a düşürüldü)."""

    protected = _FakeProcess(
        pid=300,
        name="BraveCrashHandler.exe",
        still_running=True,
        terminate_error=psutil.AccessDenied(300),
    )
    normal = _FakeProcess(pid=301, name="brave.exe", still_running=True)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [protected, normal])

    result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "brave"}})

    assert result.success is True
    assert protected.terminate_called is True  # denendi, erişim reddedildi
    assert normal.terminate_called is True
    assert result.data["closed"] == [{"pid": 301, "name": "brave.exe", "method": "forced"}]


def test_close_app_reports_honest_failure_when_all_matches_are_access_denied(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, _fast_close: None
) -> None:
    """Eşleşen süreç(ler) var ama hiçbiri gerçekten kapatılamıyorsa (hepsi
    erişim reddi) sessiz/yanlış bir 'başarılı' yerine dürüstçe
    `success=False` dönmeli."""

    protected = _FakeProcess(
        pid=400,
        name="ProtectedService.exe",
        still_running=True,
        terminate_error=psutil.AccessDenied(400),
    )
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [protected])

    result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "protected"}})

    assert result.success is False


def test_send_graceful_close_posts_wm_close_only_to_visible_matching_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_send_graceful_close`'ın hedef PID'lere ait GÖRÜNÜR pencerelere
    `WM_CLOSE` gönderdiğini, görünmez veya eşleşmeyen pencerelere
    dokunmadığını sınar. `pywin32` bu ortamda olmayabileceği için
    `win32gui`/`win32con`/`win32process` sahte modüllerle değiştirilir
    (bkz. `tests/test_llm_client.py`'deki sahte modül deseni)."""

    import logging
    import types

    import plugins.windows_plugin as windows_plugin

    posted: list[tuple[int, int]] = []
    # hwnd -> pid eşlemesi. hwnd=4 hedef PID'e sahip ama görünmez; hedefe
    # ait olsa da WM_CLOSE almamalı.
    window_pids = {1: 100, 2: 200, 3: 100, 4: 100}
    visible_hwnds = {1, 2, 3}

    fake_win32gui = types.ModuleType("win32gui")
    fake_win32gui.IsWindowVisible = lambda hwnd: hwnd in visible_hwnds
    fake_win32gui.EnumWindows = lambda callback, extra: [callback(hwnd, extra) for hwnd in window_pids]
    fake_win32gui.PostMessage = lambda hwnd, msg, w, l: posted.append((hwnd, msg))

    fake_win32process = types.ModuleType("win32process")
    fake_win32process.GetWindowThreadProcessId = lambda hwnd: (0, window_pids[hwnd])

    fake_win32con = types.ModuleType("win32con")
    fake_win32con.WM_CLOSE = 0x0010

    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)

    windows_plugin._send_graceful_close({100}, logging.getLogger("test"))

    # yalnızca hwnd=1 ve hwnd=3: görünür VE pid=100 (hedef). hwnd=2 görünür
    # ama pid=200 (hedef değil); hwnd=4 pid=100 ama görünmez.
    assert sorted(posted) == [(1, 0x0010), (3, 0x0010)]


def test_send_graceful_close_noop_when_pywin32_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """pywin32 kurulu değilse (ImportError) sessizce hiçbir şey yapmamalı
    (Adım 4'ün zorla kapatmayla devam edebilmesi için çökmemeli)."""

    import logging

    import plugins.windows_plugin as windows_plugin

    monkeypatch.setitem(sys.modules, "win32gui", None)
    monkeypatch.setitem(sys.modules, "win32con", None)
    monkeypatch.setitem(sys.modules, "win32process", None)

    # Exception fırlatmaması yeterli; dönüş değeri zaten yok (None).
    windows_plugin._send_graceful_close({100}, logging.getLogger("test"))


def test_launch_app_resolves_alias_via_shortcut_index(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresyon testi: 'lol' gibi takma adlar artık gerçek bir yola çözülüp
    os.startfile ile başlatılmalı (eskiden sessizce başarısız olan 'cmd /c
    start' yoluna hiç düşmemeli)."""

    import plugins.windows_plugin as windows_plugin

    monkeypatch.setattr(
        windows_plugin._app_resolver,
        "_shortcut_index",
        {
            "league of legends": Path(r"C:\Riot Games\League of Legends\LeagueClient.exe"),
            "riot client": Path(r"C:\Riot Games\Riot Client\RiotClientServices.exe"),
        },
    )

    started_paths = []
    monkeypatch.setattr("os.startfile", lambda p: started_paths.append(str(p)), raising=False)

    result = dispatcher.dispatch({"tool": "windows.launch_app", "arguments": {"name": "lol"}})
    assert result.success is True
    assert "LeagueClient.exe" in started_paths[-1]


def test_launch_app_reports_honest_failure_when_unresolvable(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresyon testi: bulunamayan bir uygulama artık YANLIŞ-POZİTİF
    'başlatıldı' demek yerine dürüstçe başarısız dönmeli."""

    import plugins.windows_plugin as windows_plugin

    monkeypatch.setattr(windows_plugin._app_resolver, "_shortcut_index", {})

    result = dispatcher.dispatch(
        {"tool": "windows.launch_app", "arguments": {"name": "kesinlikle-var-olmayan-bir-uygulama-xyz"}}
    )
    assert result.success is False


@pytest.mark.disruptive
@WINDOWS_ONLY
def test_lock_workstation(dispatcher: ToolDispatcher) -> None:
    """DİKKAT: bu test bilgisayarı GERÇEKTEN kilitler.

    `disruptive` işaretlidir ve varsayılan `pytest` çalıştırmasında
    ATLANIR (bkz. `pyproject.toml::addopts`). Bilinçli olarak çalıştırmak
    için: `pytest -m disruptive`.

    Neden işaretli: bir dönem varsayılan olarak çalışıyordu ve geliştirme
    sırasında her test turunda kullanıcının ekranı kilitleniyordu.
    """

    result = dispatcher.dispatch({"tool": "windows.lock", "arguments": {}})
    assert result.success is True


@pytest.mark.disruptive
@WINDOWS_ONLY
def test_set_volume(dispatcher: ToolDispatcher) -> None:
    """DİKKAT: bu test hoparlörü GERÇEKTEN sessize alır.

    `test_lock_workstation` ile aynı sebeple `disruptive` işaretlidir.
    """

    result = dispatcher.dispatch({"tool": "windows.set_volume", "arguments": {"direction": "mute"}})
    if not result.success:
        pytest.skip(f"bu ortamda masaüstü oturumuna erişilemiyor: {result.message}")
    assert result.success is True

    # Testi çalıştıran kişinin sesini KAPALI bırakmayalım: mute bir
    # geçiştir (toggle), ikinci kez göndermek eski duruma döndürür.
    dispatcher.dispatch({"tool": "windows.set_volume", "arguments": {"direction": "mute"}})


@WINDOWS_ONLY
def test_screenshot(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    result = dispatcher.dispatch({"tool": "windows.screenshot", "arguments": {"location": "desktop"}})
    if not result.success:
        pytest.skip(f"bu ortamda masaüstü oturumuna erişilemiyor: {result.message}")
    assert result.success is True
    assert Path(result.data["path"]).exists()

    # Mesaj sesli okunacağı için dosya adını (zaman damgalı, anlamsız)
    # İÇERMEMELİ, yalnızca konumu söylemeli; tam yol yalnızca `data`'da kalır.
    assert Path(result.data["path"]).name not in result.message
    assert "masaüstüne" in result.message


@WINDOWS_ONLY
def test_list_windows_returns_data(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "windows.list_windows", "arguments": {}})
    assert result.success is True
    assert "windows" in result.data


# --- windows.focus_window / windows.arrange_window ---
#
# win32gui/win32con/win32api sahte modüllerle değiştirilir (bkz.
# test_send_graceful_close_* deseni) - gerçek bir pencere hiç
# açılmaz/taşınmaz, platformdan bağımsız çalışır.


class _FakeWindow:
    """Sahte bir Windows penceresinin durumu (görünürlük, başlık,
    ShowWindow/MoveWindow'un etkilediği alanlar)."""

    def __init__(self, title: str, visible: bool = True, foreground_refused: bool = False) -> None:
        self.title = title
        self.visible = visible
        self.show_cmd = 1  # SW_SHOWNORMAL
        self.rect = (0, 0, 800, 600)
        # GERÇEK Windows davranışı: `SetForegroundWindow` bir istektir ve
        # ön plan kilidi kuralları onu reddedebilir. Bu bayrak o reddi
        # taklit eder — tool'un doğrulama mantığı ancak böyle sınanabilir.
        self.foreground_refused = foreground_refused


def _install_fake_win32_for_window_management(
    monkeypatch: pytest.MonkeyPatch, windows: dict
) -> dict:
    """focus_window/arrange_window için gerçekçi, DURUMLU bir sahte
    win32gui/win32con/win32api kurar: ShowWindow/MoveWindow çağrıları
    windows sözlüğündeki durumu GERÇEKTEN değiştirir,
    GetWindowPlacement/GetWindowRect o güncel durumu okur - tool'un
    "gerçekten uygulandı mı" doğrulamasını (koşulsuz success=True yasağı)
    anlamlı biçimde sınayabilmek için.

    Returns:
        Çağrıları kaydeden listeler sözlüğü, test asertleri için.
    """

    import types

    calls = {"foreground": [], "show": [], "move": []}

    fake_win32gui = types.ModuleType("win32gui")
    fake_win32gui.IsWindowVisible = lambda hwnd: windows[hwnd].visible
    fake_win32gui.GetWindowText = lambda hwnd: windows[hwnd].title
    fake_win32gui.EnumWindows = lambda callback, extra: [callback(h, extra) for h in windows]
    fake_win32gui.GetWindowPlacement = lambda hwnd: (0, windows[hwnd].show_cmd, (0, 0), (0, 0), windows[hwnd].rect)
    fake_win32gui.GetWindowRect = lambda hwnd: windows[hwnd].rect

    # ÖN PLAN, KOMUT DEĞİL DURUMDUR (aynı ders, bkz. aşağıdaki
    # ShowWindow notu): GERÇEK `SetForegroundWindow` bir İSTEKTİR ve
    # Windows'un ön plan kilidi kuralları yüzünden RUTİN OLARAK
    # reddedilir; hangi pencerenin gerçekten önde olduğunu yalnızca
    # `GetForegroundWindow` söyler. Fake bu ikisini ayırmazsa,
    # `windows.focus_window`'un DOĞRU olan doğrulama mantığını sınayamaz.
    # `foreground_refused` ile reddi de taklit edebiliyoruz.
    state = {"foreground": None}

    def _set_foreground(hwnd):
        calls["foreground"].append(hwnd)
        if not getattr(windows[hwnd], "foreground_refused", False):
            state["foreground"] = hwnd
        return not getattr(windows[hwnd], "foreground_refused", False)

    fake_win32gui.SetForegroundWindow = _set_foreground
    fake_win32gui.GetForegroundWindow = lambda: state["foreground"]
    # ShowWindow'a verilen KOMUT (SW_MINIMIZE=6) ile GetWindowPlacement'ın
    # döndürdüğü SONUÇ DURUMU (SW_SHOWMINIMIZED=2) GERÇEK Windows API'sinde
    # FARKLI sabitlerdir (yalnızca MAXIMIZE/SHOWMAXIMIZED değeri tesadüfen
    # aynı, 3). Fake bu eşlemeyi doğru yapmazsa tool'un kendi doğrulama
    # mantığını (koşulsuz success=True yasağı) yanlış-negatif kırar.
    _command_to_resulting_state = {6: 2, 3: 3, 9: 1}  # MINIMIZE/MAXIMIZE/RESTORE -> gerçek durum

    def _show_window(hwnd, cmd):
        calls["show"].append((hwnd, cmd))
        windows[hwnd].show_cmd = _command_to_resulting_state.get(cmd, cmd)

    def _move_window(hwnd, x, y, w, h, repaint):
        calls["move"].append((hwnd, x, y, w, h))
        windows[hwnd].rect = (x, y, x + w, y + h)
        windows[hwnd].show_cmd = 1  # taşındıktan sonra "normal" kabul edilir

    fake_win32gui.ShowWindow = _show_window
    fake_win32gui.MoveWindow = _move_window

    fake_win32con = types.ModuleType("win32con")
    fake_win32con.SW_MINIMIZE = 6
    fake_win32con.SW_MAXIMIZE = 3
    fake_win32con.SW_RESTORE = 9
    fake_win32con.SW_SHOWMINIMIZED = 2
    fake_win32con.SW_SHOWMAXIMIZED = 3
    fake_win32con.SW_SHOWNORMAL = 1
    fake_win32con.MONITOR_DEFAULTTONEAREST = 2

    fake_win32api = types.ModuleType("win32api")
    fake_win32api.MonitorFromWindow = lambda hwnd, flag: "sahte-monitor"
    fake_win32api.GetMonitorInfo = lambda monitor: {"Work": (0, 0, 1920, 1032)}

    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
    monkeypatch.setitem(sys.modules, "win32api", fake_win32api)

    return calls


def test_focus_window_brings_matching_window_to_foreground(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = {1: _FakeWindow("Discord"), 2: _FakeWindow("Not Defteri")}
    calls = _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch({"tool": "windows.focus_window", "arguments": {"title_query": "discord"}})

    assert result.success is True
    assert calls["foreground"] == [1]


def test_focus_window_no_match_fails_without_touching_anything(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = {1: _FakeWindow("Discord")}
    calls = _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch({"tool": "windows.focus_window", "arguments": {"title_query": "olmayan"}})

    assert result.success is False
    assert calls["foreground"] == []


def test_focus_window_ignores_invisible_windows(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = {1: _FakeWindow("Gizli Pencere", visible=False)}
    _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch({"tool": "windows.focus_window", "arguments": {"title_query": "gizli"}})

    assert result.success is False


def test_arrange_window_minimize(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = {1: _FakeWindow("Discord")}
    calls = _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch(
        {"tool": "windows.arrange_window", "arguments": {"title_query": "discord", "position": "minimize"}}
    )

    assert result.success is True
    assert calls["show"] == [(1, 6)]  # SW_MINIMIZE


def test_arrange_window_maximize(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = {1: _FakeWindow("Discord")}
    _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch(
        {"tool": "windows.arrange_window", "arguments": {"title_query": "discord", "position": "maximize"}}
    )

    assert result.success is True
    assert windows[1].show_cmd == 3  # SW_SHOWMAXIMIZED


def test_arrange_window_no_match_fails(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = {1: _FakeWindow("Discord")}
    _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch(
        {"tool": "windows.arrange_window", "arguments": {"title_query": "olmayan", "position": "minimize"}}
    )

    assert result.success is False


def test_arrange_window_snap_left_uses_work_area_not_full_screen(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Görev çubuğu HARİÇ çalışma alanı kullanılmalı (tam ekran değil) -
    sahte GetMonitorInfo'daki Work (0,0,1920,1032) bununla sınanır."""

    windows = {1: _FakeWindow("Discord")}
    calls = _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch(
        {"tool": "windows.arrange_window", "arguments": {"title_query": "discord", "position": "snap_left"}}
    )

    assert result.success is True
    hwnd, x, y, w, h = calls["move"][0]
    assert (x, y, w, h) == (0, 0, 960, 1032)  # 1920/2, görev çubuğu hariç yükseklik


def test_arrange_window_snap_right(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = {1: _FakeWindow("Discord")}
    calls = _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch(
        {"tool": "windows.arrange_window", "arguments": {"title_query": "discord", "position": "snap_right"}}
    )

    assert result.success is True
    hwnd, x, y, w, h = calls["move"][0]
    assert x == 960  # ekranın sağ yarısı


def test_arrange_window_snap_restores_minimized_window_first(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Küçültülmüş bir pencere doğrudan taşınamaz/boyutlandırılamaz -
    snap önce onu eski haline getirmeli, sonra yerleştirmeli."""

    windows = {1: _FakeWindow("Discord")}
    windows[1].show_cmd = 2  # SW_SHOWMINIMIZED
    calls = _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch(
        {"tool": "windows.arrange_window", "arguments": {"title_query": "discord", "position": "snap_left"}}
    )

    assert result.success is True
    assert (1, 9) in calls["show"]  # SW_RESTORE
    assert len(calls["move"]) == 1


# --- "Koşulsuz success=True yasak" (CLAUDE.md) regresyon testleri --------
#
# Aşağıdaki tool'lar bir dönem altta yatan çağrının SONUCUNU hiç
# denetlemeden success=True dönüyordu. Bu, projede üç kez tekrarlanmış
# bir hata sınıfı (README §11, §16c) — bu yüzden her biri için hem
# başarı hem BAŞARISIZLIK yolu test edilir. Bir doğrulamanın anlamlı
# olması, testteki sahte davranışın GERÇEK API semantiğini doğru taklit
# etmesine bağlıdır (bkz. `.context` §6.16, `_FakeWindow` notları).


def _install_fake_ctypes(
    monkeypatch: pytest.MonkeyPatch,
    user32_lock_result: int = 1,
    powrprof_suspend_result: int = 1,
):
    """`ctypes.windll`'i, GERÇEK API gibi BOOL döndüren sahte bir sürümle
    değiştirir.

    Kritik nokta: `LockWorkStation` ve `SetSuspendState` gerçek Windows'ta
    bir BOOL döndürür ve başarısızlıkta 0'dır. Yalnızca "çağrıldı mı"
    kaydeden bir fake, tool'un doğrulama mantığını hiç sınamazdı.
    """

    import types

    class _User32:
        def LockWorkStation(self) -> int:  # noqa: N802 - Windows API adı
            return user32_lock_result

    class _Powrprof:
        def SetSuspendState(self, hibernate, force, wakeup_events_disabled) -> int:  # noqa: N802
            return powrprof_suspend_result

    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(user32=_User32(), powrprof=_Powrprof())
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)


def _install_fake_clipboard(monkeypatch: pytest.MonkeyPatch, readback: str | None = None):
    """Sahte `win32clipboard`: yazılanı saklar ve geri okutur.

    `readback` verilirse, geri okuma YAZILANDAN FARKLI bir değer döndürür —
    yani "başka bir uygulama panoyu değiştirdi" durumunu taklit eder.
    """

    import types

    written: dict[str, Any] = {"text": None, "opened": 0, "closed": 0}

    fake = types.ModuleType("win32clipboard")
    fake.CF_UNICODETEXT = 13

    def _open() -> None:
        written["opened"] += 1

    def _close() -> None:
        written["closed"] += 1

    def _set(text, fmt):
        written["text"] = text

    fake.OpenClipboard = _open
    fake.CloseClipboard = _close
    fake.EmptyClipboard = lambda: None
    fake.SetClipboardText = _set
    fake.GetClipboardData = lambda fmt: (readback if readback is not None else written["text"])

    monkeypatch.setitem(sys.modules, "win32clipboard", fake)
    return written


def _install_fake_pyautogui_screenshot(monkeypatch: pytest.MonkeyPatch):
    """Sahte `pyautogui.screenshot()`: GERÇEKTEN bir dosya yazar ama
    ekrana dokunmaz (bkz. CLAUDE.md: gerçek makineyi etkileyen yan etki
    varsayılan koşuda çalışmamalı)."""

    import types

    class _FakeImage:
        def save(self, path) -> None:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    fake = types.ModuleType("pyautogui")
    fake.screenshot = lambda: _FakeImage()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)


def _install_fake_subprocess(monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str = ""):
    """`plugins.windows_plugin.subprocess.run`'ı, gerçekçi bir
    `CompletedProcess` döndüren sahte bir sürümle değiştirir."""

    import subprocess as real_subprocess

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return real_subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr("plugins.windows_plugin.subprocess.run", _fake_run)
    return calls


def test_shutdown_reports_failure_when_the_command_fails(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`shutdown.exe` hata döndürdüğünde "kapatılıyor" DENMEMELİ.

    Gerçek hata kodları: 1190 (zaten planlanmış kapanma), 1314 (gerekli
    ayrıcalık yok), 5 (erişim reddedildi). Bu tool CONFIRM_REQUIRED —
    yani kullanıcının bilinçli onay verdiği bir işlemde yalan söylemek
    özellikle kötü.
    """

    _install_fake_subprocess(monkeypatch, returncode=1314, stderr="Access is denied.")

    result = dispatcher.dispatch({"tool": "windows.shutdown", "arguments": {}}, confirmed=True)

    assert result.success is False
    assert "1314" in result.message


def test_shutdown_succeeds_when_the_command_succeeds(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake_subprocess(monkeypatch, returncode=0)

    result = dispatcher.dispatch({"tool": "windows.shutdown", "arguments": {}}, confirmed=True)

    assert result.success is True
    assert calls == [["shutdown", "/s", "/t", "0"]]


def test_restart_reports_failure_when_the_command_fails(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_subprocess(monkeypatch, returncode=1190, stderr="A system shutdown is already scheduled.")

    result = dispatcher.dispatch({"tool": "windows.restart", "arguments": {}}, confirmed=True)

    assert result.success is False


def test_restart_succeeds_when_the_command_succeeds(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake_subprocess(monkeypatch, returncode=0)

    result = dispatcher.dispatch({"tool": "windows.restart", "arguments": {}}, confirmed=True)

    assert result.success is True
    assert calls == [["shutdown", "/r", "/t", "0"]]


def test_shutdown_does_not_hang_forever(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asılı bir `shutdown.exe` dispatcher'ı (ve ses işçisini) kilitlememeli."""

    import subprocess as real_subprocess

    def _fake_run(cmd, **kwargs):
        assert kwargs.get("timeout"), "subprocess.run'a timeout geçilmiyor"
        raise real_subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr("plugins.windows_plugin.subprocess.run", _fake_run)

    result = dispatcher.dispatch({"tool": "windows.shutdown", "arguments": {}}, confirmed=True)

    assert result.success is False
    assert "cevap vermedi" in result.message


def test_focus_window_reports_failure_when_windows_refuses(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows ön plan kilidi isteği reddettiğinde "öne getirildi" DENMEMELİ.

    `SetForegroundWindow` bir İSTEKTİR; çağıran süreç ön planda değilse
    sistem onu sessizce reddeder. Gerçek durumu `GetForegroundWindow`
    söyler — kardeş tool `windows.arrange_window` zaten bu deseni
    kullanıyordu, `focus_window` tek istisnaydı.
    """

    windows = {1: _FakeWindow("Discord", foreground_refused=True)}
    _install_fake_win32_for_window_management(monkeypatch, windows)

    result = dispatcher.dispatch({"tool": "windows.focus_window", "arguments": {"title_query": "discord"}})

    assert result.success is False
    assert "öne getirilemedi" in result.message


def test_lock_reports_failure_when_the_api_returns_zero(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LockWorkStation` bir BOOL döner; 0 = kilitlenemedi."""

    _install_fake_ctypes(monkeypatch, user32_lock_result=0)

    result = dispatcher.dispatch({"tool": "windows.lock", "arguments": {}})

    assert result.success is False


def test_lock_succeeds_when_the_api_returns_nonzero(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ctypes(monkeypatch, user32_lock_result=1)

    result = dispatcher.dispatch({"tool": "windows.lock", "arguments": {}})

    assert result.success is True


def test_sleep_reports_failure_when_suspend_is_disabled(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SetSuspendState` 0 döndüğünde "uykuya alınıyor" DENMEMELİ.

    Bu, geliştiricinin makinesinde GERÇEKTEN olan durum (`.context` §6.6:
    hall sensörü arızası yüzünden uyku BIOS seviyesinde kapalı) —
    asistan bugüne kadar orada "uykuya alınıyor" deyip hiçbir şey
    yapmıyordu.
    """

    _install_fake_ctypes(monkeypatch, powrprof_suspend_result=0)

    result = dispatcher.dispatch({"tool": "windows.sleep", "arguments": {}})

    assert result.success is False


def test_sleep_succeeds_when_suspend_is_accepted(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ctypes(monkeypatch, powrprof_suspend_result=1)

    result = dispatcher.dispatch({"tool": "windows.sleep", "arguments": {}})

    assert result.success is True


def test_clipboard_copy_reports_failure_when_readback_differs(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pano PAYLAŞILAN bir kaynak: yazdıktan sonra başkası üzerine yazabilir."""

    _install_fake_clipboard(monkeypatch, readback="baska bir sey")

    result = dispatcher.dispatch({"tool": "windows.clipboard_copy", "arguments": {"text": "merhaba"}})

    assert result.success is False


def test_clipboard_copy_succeeds_when_readback_matches(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _install_fake_clipboard(monkeypatch)

    result = dispatcher.dispatch({"tool": "windows.clipboard_copy", "arguments": {"text": "merhaba"}})

    assert result.success is True
    assert written["text"] == "merhaba"


def test_screenshot_honours_the_same_location_contract_as_filesystem_tools(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`location` tam yol alabilmeli — sessizce indirilenlere düşmemeli.

    Bir dönem `"desktop"` dışındaki HER değer (tam yol dahil, `"last"`
    dahil) sessizce `downloads_path`'e yönlendiriliyordu: aynı argüman
    adı, filesystem tool'larından FARKLI bir sözleşme konuşuyordu.
    """

    _install_fake_pyautogui_screenshot(monkeypatch)
    hedef = tmp_path / "ss_hedef"

    result = dispatcher.dispatch({"tool": "windows.screenshot", "arguments": {"location": str(hedef)}})

    assert result.success is True
    kaydedilen = Path(result.data["path"])
    assert kaydedilen.parent == hedef, f"'{kaydedilen}' istenen konumda değil"
    assert kaydedilen.parent != dispatcher.settings.downloads_path
