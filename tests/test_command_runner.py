"""`core/command_runner.py` testleri: sesli ve yazılı komutun ORTAK işlem hattı.

Gerçek dispatcher ve gerçek planlayıcı çalışır; yalnızca LLM sahtedir (CLAUDE.md:
LLM'e bağlı testler gerçek sunucu gerektirmez). Hiçbir test gerçek bir masaüstüne
dokunmaz: dosya işlemleri `settings` fixture'ının tmp_path altındaki masaüstünde
yapılır.
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import Settings
from core.command_runner import CommandOutcome, CommandRunner, summarize_steps
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError
from core.planner import StepResult
from models.tool_models import ToolResult


class FakeLLM:
    """Sabit tool-call listesi döndüren / hata fırlatan sahte beyin."""

    def __init__(
        self,
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        gate: bool = True,
    ) -> None:
        self._tool_calls = tool_calls or []
        self._error = error
        self._gate = gate
        self.tool_requests: list[str] = []
        self.gate_requests: list[str] = []

    def get_tool_calls(self, system_prompt: str, user_input: str, max_retries: int = 2) -> list[dict[str, Any]]:
        self.tool_requests.append(user_input)
        if self._error is not None:
            raise self._error
        return list(self._tool_calls)

    def should_engage(self, user_input: str) -> bool:
        self.gate_requests.append(user_input)
        return self._gate


def _never_confirm(tool_name: str, arguments: dict[str, Any]) -> bool:
    raise AssertionError(f"Onay istenmemeliydi: {tool_name}")


def _runner(dispatcher: ToolDispatcher, llm: FakeLLM, settings: Settings) -> CommandRunner:
    return CommandRunner(dispatcher, llm, settings, system_prompt="sistem")


def test_a_tool_call_runs_and_its_message_becomes_the_reply(dispatcher: ToolDispatcher, settings: Settings) -> None:
    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "Merhaba!"}}])

    outcome = _runner(dispatcher, llm, settings).run("merhaba", confirm=_never_confirm)

    assert outcome.kind == "done"
    assert outcome.reply == "Merhaba!"
    assert [step.tool_name for step in outcome.step_results] == ["assistant.reply"]
    assert llm.tool_requests == ["merhaba"]


def test_gate_turns_background_noise_into_an_ignored_outcome(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """Sesli girdide kapı açıkken "asistana yönelik değil" bulunursa tool çalışmaz."""

    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "x"}}], gate=False)

    outcome = _runner(dispatcher, llm, settings).run("arka plan konuşması", confirm=_never_confirm, gate=True)

    assert outcome.kind == "ignored"
    assert outcome.reply == "Bir komut duymadım."
    assert llm.tool_requests == [], "kapı reddettiyse tool seçimi hiç istenmemeli"


def test_gate_is_skipped_for_typed_input(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """Yazılı girdi (`gate=False`) kapıya girmez: kullanıcı bilerek yazdı."""

    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "Tamam"}}], gate=False)

    outcome = _runner(dispatcher, llm, settings).run("merhaba", confirm=_never_confirm, gate=False)

    assert outcome.kind == "done"
    assert llm.gate_requests == []
    assert llm.tool_requests == ["merhaba"]


def test_disabled_gate_setting_is_respected_even_for_voice(dispatcher: ToolDispatcher, settings: Settings) -> None:
    settings = settings.model_copy(update={"command_gate_enabled": False})
    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "ok"}}], gate=False)

    outcome = _runner(dispatcher, llm, settings).run("merhaba", confirm=_never_confirm, gate=True)

    assert outcome.kind == "done"
    assert llm.gate_requests == []


def test_connection_error_is_an_error_outcome_not_an_exception(dispatcher: ToolDispatcher, settings: Settings) -> None:
    llm = FakeLLM(error=ConnectionError("ollama yok"))

    outcome = _runner(dispatcher, llm, settings).run("merhaba", confirm=_never_confirm, gate=False)

    assert outcome.kind == "error"
    assert outcome.reply == "Yerel modele ulaşamıyorum."
    assert outcome.step_results == ()


def test_unparseable_model_output_is_an_error_outcome(dispatcher: ToolDispatcher, settings: Settings) -> None:
    llm = FakeLLM(error=LLMResponseParseError("bozuk çıktı"))

    outcome = _runner(dispatcher, llm, settings).run("merhaba", confirm=_never_confirm, gate=False)

    assert outcome.kind == "error"
    assert outcome.reply == "Bunu anlayamadım, başka türlü söyler misiniz?"


def test_confirmation_callback_receives_the_tool_name_and_its_arguments(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Onay NEYİ onayladığını göstermeli: tool adı ve argümanlar birlikte gelir (CLAUDE.md)."""

    target = settings.desktop_path / "rapor.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("veri", encoding="utf-8")
    llm = FakeLLM([{"tool": "filesystem.delete", "arguments": {"target": "rapor.txt"}}])
    asked: list[tuple[str, dict[str, Any]]] = []

    def reject(tool_name: str, arguments: dict[str, Any]) -> bool:
        asked.append((tool_name, arguments))
        return False

    outcome = _runner(dispatcher, llm, settings).run("raporu sil", confirm=reject, gate=False)

    assert asked == [("filesystem.delete", {"target": "rapor.txt"})]
    assert target.exists(), "reddedilen silme dosyayı silmemeli"
    assert outcome.kind == "done"
    assert outcome.step_results[0].result.success is False


def test_planner_progress_is_forwarded_when_asked(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """`progress` verilirse plan adımlarını bildirir (panelin adım listesi için)."""

    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "tamam"}}])
    events: list[object] = []

    CommandRunner(dispatcher, llm, settings, system_prompt="s").run(
        "merhaba", confirm=_never_confirm, gate=False, progress=events.append
    )

    assert events, "ilerleme geri çağrısı en az bir olay almalıydı"


def test_summarize_steps_matches_the_voice_wording() -> None:
    ok = StepResult(1, "assistant.reply", {}, ToolResult(success=True, message="Bir"))
    bad = StepResult(2, "windows.close_app", {}, ToolResult(success=False, message="Hata"))

    assert summarize_steps([], 1) == "Yapılacak bir işlem bulamadım."
    assert summarize_steps([ok], 1) == "Bir"
    assert summarize_steps([ok, ok], 2) == "2 işlemin hepsini tamamladım."
    assert summarize_steps([ok, bad], 2) == "2 işlemden 1 tanesini tamamladım; kalanında sorun oldu."


def test_outcome_is_immutable() -> None:
    outcome = CommandOutcome(reply="x", kind="done")
    with pytest.raises(AttributeError):
        outcome.reply = "y"  # type: ignore[misc]
