"""Dispatcher ve plugin loader için temel testler.

ToolContext dependency-injection sayesinde tool'lar gerçek masaüstü/
işletim sistemi yan etkilerine ihtiyaç duymadan izole test edilebilir;
burada `tmp_path` fixture'ı ile geçici bir "desktop" klasörü simüle edilir.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.plugin_loader import load_plugins
from memory.context_memory import ContextMemory


@pytest.fixture(autouse=True)
def _load_all_plugins() -> None:
    load_plugins()


@pytest.fixture
def dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path,
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    memory = ContextMemory(settings.db_path)
    # os.startfile Windows'a özgüdür; Linux/CI ortamında test edilebilmesi
    # için sahte (no-op) bir sürüm ekleniyor.
    monkeypatch.setattr(os, "startfile", lambda path: None, raising=False)
    return ToolDispatcher(settings=settings, memory=memory)


def test_unknown_tool_returns_failure(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "nonexistent.tool", "arguments": {}})
    assert result.success is False


def test_create_folder_then_open(dispatcher: ToolDispatcher) -> None:
    create_result = dispatcher.dispatch(
        {"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}}
    )
    assert create_result.success is True

    open_result = dispatcher.dispatch({"tool": "filesystem.open", "arguments": {"target": "Orbit"}})
    assert open_result.success is True


def test_delete_requires_confirmation(dispatcher: ToolDispatcher) -> None:
    dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "temp.txt"}})

    unconfirmed = dispatcher.dispatch({"tool": "filesystem.delete", "arguments": {"target": "temp.txt"}})
    assert unconfirmed.requires_confirmation is True
    assert unconfirmed.success is False

    confirmed = dispatcher.dispatch(
        {"tool": "filesystem.delete", "arguments": {"target": "temp.txt"}},
        confirmed=True,
    )
    assert confirmed.success is True


def test_invalid_call_format_is_handled_gracefully(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"missing_tool_key": True})
    assert result.success is False


def test_search_finds_created_file(dispatcher: ToolDispatcher) -> None:
    dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "rapor.txt"}})

    result = dispatcher.dispatch({"tool": "filesystem.search", "arguments": {"query": "rapor"}})
    assert result.success is True
    assert result.data is not None
    assert len(result.data["matches"]) == 1


# --------------------------------------------------------------------------
# Argüman şeması doğrulaması
#
# REGRESYON: `InvalidToolArgumentsError` tanımlıydı ama hiçbir yerde
# fırlatılmıyordu. Eksik zorunlu argüman, tool'un içinde ham bir
# `KeyError` olup dispatcher'ın genel yakalayıcısına düşüyor ve kullanıcı
# "Beklenmeyen hata: 'target'" gibi anlaşılmaz bir mesaj görüyordu
# (bkz. README §24). Bu bloktaki testler dispatcher'ın artık tool
# ÇALIŞMADAN ÖNCE, anlaşılır bir mesajla durduğunu doğrular.
# --------------------------------------------------------------------------


def test_missing_required_argument_fails_before_tool_runs(dispatcher: ToolDispatcher) -> None:
    """Eksik zorunlu argüman ham `KeyError` DEĞİL, anlaşılır bir mesaj üretmeli."""

    result = dispatcher.dispatch({"tool": "filesystem.create_folder", "arguments": {}})

    assert result.success is False
    assert "name" in result.message
    assert "Beklenmeyen hata" not in result.message, "KeyError'a düşmüş, merkezi doğrulamayı atlamış"


def test_missing_required_argument_reports_all_missing_fields(dispatcher: ToolDispatcher) -> None:
    """Birden fazla alan eksikse mesaj TÜMÜNÜ listelemeli, yalnızca ilkini değil."""

    result = dispatcher.dispatch({"tool": "filesystem.copy", "arguments": {}})

    assert result.success is False
    assert "target" in result.message
    assert "destination_location" in result.message


def test_wrong_enum_value_is_rejected(dispatcher: ToolDispatcher) -> None:
    """Şemadaki `enum`'da olmayan bir değer (örn. yanlış arama motoru) reddedilmeli."""

    result = dispatcher.dispatch(
        {"tool": "web.search", "arguments": {"query": "test", "engine": "bing"}}
    )

    assert result.success is False
    assert "engine" in result.message


def test_leniently_coerces_numeric_string_like_the_tools_already_do(dispatcher: ToolDispatcher) -> None:
    """`level: "50"` (string) reddedilmemeli — tool zaten `int(...)` ile bunu kabul ediyor.

    Doğrulama, tool'ların kendisinden DAHA KATI olmamalı: model küçük
    olduğu için sayısal bir alanı string üretmesi olası bir hata sınıfı.
    Standart bir JSON Schema doğrulayıcısı burada BİLEREK kullanılmadı,
    bkz. `core/dispatcher.py::_type_matches` dokümantasyonu.
    """

    result = dispatcher.dispatch({"tool": "windows.set_brightness", "arguments": {"level": "50"}})

    assert result.message != "'level' alanı integer türünde olmalı, str geldi"


def test_extra_unknown_argument_is_ignored(dispatcher: ToolDispatcher) -> None:
    """Şemada olmayan fazladan bir alan reddedilmemeli — tool zaten kullanmayacak."""

    result = dispatcher.dispatch(
        {"tool": "filesystem.create_folder", "arguments": {"name": "Orbit", "gereksiz_alan": "x"}}
    )

    assert result.success is True


def test_dangerous_tool_with_missing_argument_never_asks_for_confirmation(
    dispatcher: ToolDispatcher,
) -> None:
    """Bozuk bir tehlikeli çağrı, kullanıcıya BOŞ argümanla onay sormamalı.

    Doğrulama, tehlike/onay kontrolünden ÖNCE çalışır: `target` eksikse,
    kullanıcının "??? dosyasını silmek istiyor musunuz" gibi anlamsız bir
    onay sorusuyla karşılaşması yerine dispatcher net bir hata döndürür.
    """

    result = dispatcher.dispatch({"tool": "filesystem.delete", "arguments": {}})

    assert result.success is False
    assert result.requires_confirmation is False
    assert "target" in result.message
