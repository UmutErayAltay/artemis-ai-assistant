"""`main.py` komut satırı: hangi seçenek hangi uygulamayı açar.

Asıl kural: seçenek yokken birleşik uygulama (tepsi + sesli asistan + panel)
açılır; `--demo` eski LLM'siz demo'dur; `--voice` ve `--chat-gui` birleşik
uygulamanın takma adlarıdır. Gerçek Qt döngüsü ve Ollama ÇALIŞTIRILMAZ: giriş
noktaları sahte fonksiyonlarla değiştirilir.
"""

from __future__ import annotations

from typing import Any

import pytest

import main as main_module
from config.settings import Settings


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Her giriş noktasını bir isim kaydeden sahteyle değiştirir."""

    calls: list[str] = []
    for name in ("main", "main_demo", "main_voice", "main_chat", "main_chat_gui", "main_settings", "main_stop_ollama"):
        monkeypatch.setattr(main_module, name, lambda name=name: calls.append(name))
    return calls


def _route(argv: list[str]) -> None:
    main_module._dispatch(main_module._parse_args(argv))


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], "main"),
        (["--voice"], "main_voice"),
        (["--chat-gui"], "main_chat_gui"),
        (["--chat"], "main_chat"),
        (["--demo"], "main_demo"),
        (["--settings"], "main_settings"),
        (["--stop-ollama"], "main_stop_ollama"),
    ],
)
def test_each_flag_reaches_its_entry_point(routed: list[str], argv: list[str], expected: str) -> None:
    _route(argv)

    assert routed == [expected]


def test_unknown_flag_is_an_error_not_a_silent_fallback(routed: list[str]) -> None:
    """Yazım hatası (`--voise`) sessizce birleşik uygulamaya düşmemeli."""

    with pytest.raises(SystemExit):
        _route(["--voise"])
    assert routed == []


def test_conflicting_modes_are_rejected(routed: list[str]) -> None:
    with pytest.raises(SystemExit):
        _route(["--chat", "--voice"])
    assert routed == []


def _fake_bootstrap(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> object:
    """`bootstrap()` yerine, yalnızca ayar taşıyan sahte bir dağıtıcı döndürür."""

    class _Dispatcher:
        pass

    dispatcher = _Dispatcher()
    dispatcher.settings = settings  # type: ignore[attr-defined]
    monkeypatch.setattr(main_module, "bootstrap", lambda: dispatcher)
    return dispatcher


def test_no_flag_opens_the_unified_app_with_the_panel_hidden(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    dispatcher = _fake_bootstrap(monkeypatch, settings)
    seen: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        main_module, "_run_unified", lambda d, *, show_panel_at_start: seen.append((d, show_panel_at_start))
    )

    main_module.main()

    assert seen == [(dispatcher, False)]


def test_chat_gui_is_the_unified_app_with_the_panel_shown_at_start(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    dispatcher = _fake_bootstrap(monkeypatch, settings)
    seen: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        main_module, "_run_unified", lambda d, *, show_panel_at_start: seen.append((d, show_panel_at_start))
    )

    main_module.main_chat_gui()

    assert seen == [(dispatcher, True)]


def test_voice_is_the_same_unified_app_when_voice_is_enabled(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    dispatcher = _fake_bootstrap(monkeypatch, settings.model_copy(update={"voice_enabled": True}))
    seen: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        main_module, "_run_unified", lambda d, *, show_panel_at_start: seen.append((d, show_panel_at_start))
    )

    main_module.main_voice()

    assert seen == [(dispatcher, False)]


def test_voice_with_voice_disabled_warns_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--voice` sesli modu açamıyorsa dürüstçe söyler; sessizce panel açmaz."""

    _fake_bootstrap(monkeypatch, settings.model_copy(update={"voice_enabled": False}))
    started: list[str] = []
    monkeypatch.setattr(main_module, "_run_unified", lambda *a, **k: started.append("app"))
    monkeypatch.setattr(main_module, "_start_llm_session", lambda s: started.append("llm"))

    main_module.main_voice()

    assert started == []
    assert "voice_enabled: false" in capsys.readouterr().out


def test_unified_app_with_voice_disabled_still_opens_for_typed_commands(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Birleşik uygulama (seçeneksiz) sesli asistan kapalıyken de çalışır: tepsi + panel.

    Burada yalnızca yönlendirme kilitlenir; `_run_unified`'ın kendisi Qt döngüsü
    açtığı için bu testte çağrılmaz.
    """

    dispatcher = _fake_bootstrap(monkeypatch, settings.model_copy(update={"voice_enabled": False}))
    seen: list[object] = []
    monkeypatch.setattr(main_module, "_run_unified", lambda d, *, show_panel_at_start: seen.append(d))

    main_module.main()

    assert seen == [dispatcher]


def test_demo_does_not_start_the_llm(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    _fake_bootstrap(monkeypatch, settings)
    started: list[str] = []
    monkeypatch.setattr(main_module, "_start_llm_session", lambda s: started.append("llm"))
    monkeypatch.setattr(main_module, "_run_unified", lambda *a, **k: started.append("app"))

    class _Demo:
        def dispatch(self, call: dict[str, Any]) -> Any:
            started.append("dispatch")
            from models.tool_models import ToolResult

            return ToolResult(success=True, message="ok")

    monkeypatch.setattr(main_module, "bootstrap", lambda: _Demo())

    main_module.main_demo()

    assert started == ["dispatch"]
