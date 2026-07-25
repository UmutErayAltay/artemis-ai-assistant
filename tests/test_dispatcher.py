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
