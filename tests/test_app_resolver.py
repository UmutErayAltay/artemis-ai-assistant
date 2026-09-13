"""`plugins._app_resolver.AppResolver` testleri.

Registry (winreg) ve Başlat Menüsü taraması (win32com) yalnızca Windows'ta
çalışır; bu testler platformdan bağımsız olsun diye `_shortcut_index`'i
doğrudan enjekte ederek gerçek taramayı atlar — yalnızca alias
normalizasyonu ve fuzzy-eşleştirme MANTIĞINI sınar (gerçek dosya sistemi
taraması Windows'ta ayrıca doğrulanmalıdır).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from plugins._app_resolver import AppResolver


def _resolver_with_fake_shortcuts(index: dict[str, str]) -> AppResolver:
    resolver = AppResolver()
    resolver._shortcut_index = {name: Path(path) for name, path in index.items()}
    return resolver


def test_known_system_command_resolves_without_shortcut_search() -> None:
    resolver = AppResolver()
    system_command, path = resolver.resolve("notepad")
    assert system_command == "notepad.exe"
    assert path is None


def test_alias_lol_matches_league_of_legends_shortcut() -> None:
    resolver = _resolver_with_fake_shortcuts(
        {
            "league of legends": r"C:\Riot Games\League of Legends\LeagueClient.exe",
            "riot client": r"C:\Riot Games\Riot Client\RiotClientServices.exe",
            "opera": r"C:\Users\Artemis\AppData\Local\Programs\Opera\opera.exe",
        }
    )
    system_command, path = resolver.resolve("lol")
    assert system_command is None
    assert path is not None
    assert "LeagueClient.exe" in str(path)


def test_alias_riot_matches_riot_client_shortcut() -> None:
    resolver = _resolver_with_fake_shortcuts(
        {
            "league of legends": r"C:\Riot Games\League of Legends\LeagueClient.exe",
            "riot client": r"C:\Riot Games\Riot Client\RiotClientServices.exe",
        }
    )
    system_command, path = resolver.resolve("riot client i")  # kullanıcı tam olarak böyle söylemiş olabilir
    # Not: "riot client i" tam eşleşmez; fuzzy/alt-dizge araması devreye girmeli.
    assert path is not None
    assert "RiotClientServices.exe" in str(path)


def test_exact_shortcut_name_match() -> None:
    resolver = _resolver_with_fake_shortcuts({"discord": r"C:\Users\Artemis\AppData\Local\Discord\Discord.exe"})
    system_command, path = resolver.resolve("Discord")
    assert path is not None
    assert "Discord.exe" in str(path)


def test_unresolvable_name_returns_none_none() -> None:
    resolver = _resolver_with_fake_shortcuts({"discord": r"C:\Discord.exe"})
    system_command, path = resolver.resolve("kesinlikle-var-olmayan-bir-uygulama-xyz")
    assert system_command is None and path is None


def test_suggestions_returns_close_matches_for_typo() -> None:
    resolver = _resolver_with_fake_shortcuts(
        {
            "league of legends": r"C:\LeagueClient.exe",
            "riot client": r"C:\RiotClientServices.exe",
            "opera": r"C:\opera.exe",
        }
    )
    suggestions = resolver.suggestions("leage of legens")  # kasıtlı yazım hatası
    assert "league of legends" in suggestions



def test_ensure_shortcut_index_retries_after_a_failed_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresyon testi: `win32com` ithalatı BAŞARISIZ olduğunda
    `_shortcut_index` bir dönem `{}` (BOŞ ama None DEĞİL) olarak
    kalıcı önbelleğe alınıyordu — sonraki HİÇBİR çağrı gerçek taramayı
    tekrar denemiyordu. `pywin32` geç yüklenirse (ya da COM kısa
    süreliğine kullanılamazsa), `launch_app` Başlat Menüsü çözümlemesini
    süreç ömrü boyunca sessizce kaybediyordu."""

    resolver = AppResolver()

    monkeypatch.setitem(sys.modules, "win32com.client", None)  # ImportError'ı taklit eder
    resolver._ensure_shortcut_index()
    assert resolver._shortcut_index is None  # {} DEĞİL — sonraki çağrı tekrar denemeli

    fake_shell = types.SimpleNamespace()

    class _FakeShortcut:
        Targetpath = r"C:\Discord\Discord.exe"

    fake_shell.CreateShortcut = lambda path: _FakeShortcut()

    fake_client_module = types.ModuleType("win32com.client")
    fake_client_module.Dispatch = lambda name: fake_shell
    fake_win32com_package = types.ModuleType("win32com")
    fake_win32com_package.client = fake_client_module  # `import win32com.client` bunu bekler
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com_package)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client_module)

    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: True,
    )
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter([Path("C:/Start Menu/Discord.lnk")]))

    resolver._ensure_shortcut_index()

    assert resolver._shortcut_index is not None
    assert "discord" in resolver._shortcut_index
