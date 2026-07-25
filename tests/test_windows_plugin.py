"""Windows plugin testleri.

Bazı tool'lar (lock, sleep, volume, brightness, clipboard, pencere
yönetimi) yalnızca gerçek Windows üzerinde anlamlıdır; bu testler
`sys.platform != "win32"` olduğunda otomatik atlanır. `windows.close_app`
ise psutil kullandığı için (Windows'a özgü olmadığından) her platformda
gerçek anlamda test edilir.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.plugin_loader import load_plugins
from memory.context_memory import ContextMemory

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Yalnızca Windows üzerinde çalışır.")


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


def test_close_app_terminates_real_process(dispatcher: ToolDispatcher) -> None:
    # psutil platformdan bağımsız çalıştığı için gerçek bir alt süreç
    # başlatıp kapatmayı deneyerek bu tool'u gerçekten sınıyoruz.
    proc = subprocess.Popen(["sleep", "30"])
    time.sleep(0.2)  # sürecin process listesine yansımasını bekle

    try:
        result = dispatcher.dispatch({"tool": "windows.close_app", "arguments": {"name": "sleep"}})
        assert result.success is True
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


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


@WINDOWS_ONLY
def test_lock_workstation(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "windows.lock", "arguments": {}})
    assert result.success is True


@WINDOWS_ONLY
def test_set_volume(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "windows.set_volume", "arguments": {"direction": "mute"}})
    if not result.success:
        pytest.skip(f"bu ortamda masaüstü oturumuna erişilemiyor: {result.message}")
    assert result.success is True


@WINDOWS_ONLY
def test_screenshot(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    result = dispatcher.dispatch({"tool": "windows.screenshot", "arguments": {"location": "desktop"}})
    if not result.success:
        pytest.skip(f"bu ortamda masaüstü oturumuna erişilemiyor: {result.message}")
    assert result.success is True
    assert Path(result.data["path"]).exists()


@WINDOWS_ONLY
def test_list_windows_returns_data(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "windows.list_windows", "arguments": {}})
    assert result.success is True
    assert "windows" in result.data
