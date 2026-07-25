"""`plugins._app_resolver.AppResolver` testleri.

Registry (winreg) ve Başlat Menüsü taraması (win32com) yalnızca Windows'ta
çalışır; bu testler platformdan bağımsız olsun diye `_shortcut_index`'i
doğrudan enjekte ederek gerçek taramayı atlar — yalnızca alias
normalizasyonu ve fuzzy-eşleştirme MANTIĞINI sınar (gerçek dosya sistemi
taraması Windows'ta ayrıca doğrulanmalıdır).
"""

from __future__ import annotations

from pathlib import Path

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
