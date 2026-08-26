"""Web plugin testleri.

`webbrowser.open` gerçek bir tarayıcı penceresi açar; bu yüzden her
testte monkeypatch ile sahte bir sürümle değiştirilir ve testler yalnızca
hangi URL ile çağrıldığını (veya HİÇ çağrılmadığını) doğrular — gerçekte
hiçbir tarayıcı açılmaz.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from memory.context_memory import ContextMemory


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path,
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


@pytest.fixture
def opened_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`webbrowser.open` çağrılarını gerçek tarayıcı açmadan yakalar.

    Plugin `execute()` içinde `import webbrowser` yapıyor olsa da bu,
    zaten `sys.modules`'ta önbelleklenmiş aynı modül nesnesine bağlanır;
    bu yüzden burada modül seviyesinde import edilip `open` attribute'u
    monkeypatch'lenmesi, plugin'in lazy import'unu da etkiler (bkz.
    tests/test_dispatcher.py'daki `os.startfile` monkeypatch deseni).
    """

    calls: list[str] = []

    def fake_open(url: str, *args: object, **kwargs: object) -> bool:
        calls.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    return calls


# --- web.search ------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "expected_prefix"),
    [
        ("google", "https://www.google.com/search?q="),
        ("youtube", "https://www.youtube.com/results?search_query="),
        ("wikipedia", "https://tr.wikipedia.org/w/index.php?search="),
        ("github", "https://github.com/search?q="),
    ],
)
def test_search_builds_correct_url_per_engine(
    dispatcher: ToolDispatcher,
    opened_urls: list[str],
    engine: str,
    expected_prefix: str,
) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.search", "arguments": {"query": "artemis", "engine": engine}}
    )

    assert result.success is True
    assert opened_urls == [expected_prefix + "artemis"]
    assert result.data == {"url": expected_prefix + "artemis"}


def test_search_defaults_to_google_when_engine_omitted(
    dispatcher: ToolDispatcher, opened_urls: list[str]
) -> None:
    result = dispatcher.dispatch({"tool": "web.search", "arguments": {"query": "artemis"}})

    assert result.success is True
    assert opened_urls == ["https://www.google.com/search?q=artemis"]


def test_search_encodes_query_with_spaces_and_turkish_characters(
    dispatcher: ToolDispatcher, opened_urls: list[str]
) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.search", "arguments": {"query": "yapay zeka öğren", "engine": "google"}}
    )

    assert result.success is True
    assert opened_urls == ["https://www.google.com/search?q=yapay+zeka+%C3%B6%C4%9Fren"]


def test_search_unknown_engine_fails_gracefully_without_keyerror(
    dispatcher: ToolDispatcher, opened_urls: list[str]
) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.search", "arguments": {"query": "artemis", "engine": "bing"}}
    )

    assert result.success is False
    assert "bing" in result.message
    for engine_name in ("google", "youtube", "wikipedia", "github"):
        assert engine_name in result.message
    assert opened_urls == []  # tarayıcı hiç açılmamalı


def test_search_returns_failure_when_browser_open_returns_false(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webbrowser, "open", lambda url: False)

    result = dispatcher.dispatch({"tool": "web.search", "arguments": {"query": "artemis"}})

    assert result.success is False


# --- web.open_url ------------------------------------------------------------


def test_open_url_accepts_http_scheme(dispatcher: ToolDispatcher, opened_urls: list[str]) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.open_url", "arguments": {"url": "http://example.com"}}
    )

    assert result.success is True
    assert opened_urls == ["http://example.com"]


def test_open_url_accepts_https_scheme(dispatcher: ToolDispatcher, opened_urls: list[str]) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.open_url", "arguments": {"url": "https://example.com"}}
    )

    assert result.success is True
    assert opened_urls == ["https://example.com"]


def test_open_url_adds_https_prefix_when_scheme_missing(
    dispatcher: ToolDispatcher, opened_urls: list[str]
) -> None:
    result = dispatcher.dispatch({"tool": "web.open_url", "arguments": {"url": "github.com"}})

    assert result.success is True
    assert opened_urls == ["https://github.com"]
    assert result.data == {"url": "https://github.com"}


def test_open_url_rejects_file_scheme(dispatcher: ToolDispatcher, opened_urls: list[str]) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.open_url", "arguments": {"url": "file:///C:/Windows/System32/config.sys"}}
    )

    assert result.success is False
    assert opened_urls == []  # tarayıcı hiç çağrılmamalı


def test_open_url_rejects_javascript_scheme(
    dispatcher: ToolDispatcher, opened_urls: list[str]
) -> None:
    result = dispatcher.dispatch(
        {"tool": "web.open_url", "arguments": {"url": "javascript:alert(1)"}}
    )

    assert result.success is False
    assert opened_urls == []


def test_open_url_rejects_data_scheme(dispatcher: ToolDispatcher, opened_urls: list[str]) -> None:
    result = dispatcher.dispatch(
        {
            "tool": "web.open_url",
            "arguments": {"url": "data:text/html,<script>alert(1)</script>"},
        }
    )

    assert result.success is False
    assert opened_urls == []


def test_open_url_returns_failure_when_browser_open_returns_false(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webbrowser, "open", lambda url: False)

    result = dispatcher.dispatch(
        {"tool": "web.open_url", "arguments": {"url": "https://example.com"}}
    )

    assert result.success is False


def test_open_url_end_to_end_via_dispatcher_with_scheme_less_input(
    dispatcher: ToolDispatcher, opened_urls: list[str]
) -> None:
    """Uçtan uca: dispatcher -> web.open_url -> https:// eklenip tarayıcı çağrılır."""

    result = dispatcher.dispatch({"tool": "web.open_url", "arguments": {"url": "example.com"}})

    assert result.success is True
    assert result.requires_confirmation is False
    assert result.data == {"url": "https://example.com"}
    assert opened_urls == ["https://example.com"]
