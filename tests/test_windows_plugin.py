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
