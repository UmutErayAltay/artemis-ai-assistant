"""Panelin yazı kutusu: yazılan komut, sesli komutla AYNI hattan geçer ve balon çifti olur.

Kapsam:
  * geçmiş satırı etiketi (`tool_label`) ve ham adın tooltip'te kalması;
  * yazılı komutun balon çifti olarak hemen görünmesi ve log'a yazılması;
  * onay diyaloğunun tool adı VE argümanları alması (Qt köprüsü dahil);
  * hata / beklenmeyen hata için kırmızı balon ve kilidin açılması;
  * gerçek bir iş parçacığında çalışma (arayüz donmadan).

LLM sahtedir; komut hattı (`CommandRunner`) ve dispatcher gerçektir. Pencere
`QT_QPA_PLATFORM=offscreen` ile başlıksız açılır.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QTextBrowser

import ui.panel as panel_module
from config.settings import Settings
from core.command_runner import CommandOutcome, CommandRunner
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError
from ui import theme
from ui.panel import ArtemisPanel, HistoryTurn, parse_history, show_panel, tool_label


class FakeLLM:
    """Sabit tool-call listesi döndüren sahte beyin (bkz. `test_command_runner.py`)."""

    def __init__(self, tool_calls: list[dict[str, Any]] | None = None, *, error: Exception | None = None) -> None:
        self._tool_calls = tool_calls or []
        self._error = error

    def get_tool_calls(self, system_prompt: str, user_input: str, max_retries: int = 2) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return list(self._tool_calls)

    def should_engage(self, user_input: str) -> bool:
        return True


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _panel(
    qapp: QApplication,
    settings: Settings,
    dispatcher: ToolDispatcher | None,
    llm: Any,
    *,
    background: bool = False,
) -> ArtemisPanel:
    runner = CommandRunner(dispatcher, llm, settings, system_prompt="sistem") if dispatcher is not None else None
    return ArtemisPanel(settings, command_runner=runner, background=background, hide_on_close=True)


def _type_and_send(panel: ArtemisPanel, text: str) -> None:
    panel._input.setText(text)
    panel._submit()


def _history_text(panel: ArtemisPanel) -> str:
    browser = panel.findChild(QTextBrowser)
    assert browser is not None
    return browser.toPlainText()


def _tooltips(panel: ArtemisPanel) -> set[str]:
    """Geçmiş belgesindeki tüm karakter tooltip'lerini toplar (Qt `title` → toolTip)."""

    browser = panel.findChild(QTextBrowser)
    assert browser is not None
    found: set[str] = set()
    block = browser.document().begin()
    while block.isValid():
        fragments = block.begin()
        while not fragments.atEnd():
            fragment = fragments.fragment()
            if fragment.isValid() and fragment.charFormat().toolTip():
                found.add(fragment.charFormat().toolTip())
            fragments += 1
        block = block.next()
    return found


# --------------------------------------------------------------------------
# Etiketler
# --------------------------------------------------------------------------


def test_known_tools_get_a_human_label_and_unknown_ones_keep_their_name() -> None:
    assert tool_label("windows.close_app") == "Uygulamayı kapatma"
    assert tool_label("web.search") == "Web'de arama"
    assert tool_label("kayip.arac") == "kayip.arac", "bilinmeyen ad uydurulmadan olduğu gibi kalmalı"


def test_history_shows_the_label_and_keeps_the_raw_name_as_tooltip(
    qapp: QApplication, settings: Settings, tmp_path: Path
) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    (settings.log_dir / "artemis.log").write_text(
        "2026-07-26 03:23:40,000 | INFO     | core.voice_loop | Duyulan komut: 'kapat'\n"
        "2026-07-26 03:23:52,100 | INFO     | core.dispatcher | Tool çalıştırıldı: windows.close_app -> success=False\n",
        encoding="utf-8",
    )
    panel = _panel(qapp, settings, None, None)

    text = _history_text(panel)

    assert "Uygulamayı kapatma" in text
    assert "windows.close_app" not in text, "ham ad görünen metinde olmamalı; tooltip'te kalır"
    assert "windows.close_app" in _tooltips(panel)


# --------------------------------------------------------------------------
# Gönderme ve balon çifti
# --------------------------------------------------------------------------


def test_typed_command_appears_as_a_bubble_pair_immediately(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher
) -> None:
    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "Merhaba, buradayım."}}])
    panel = _panel(qapp, settings, dispatcher, llm)

    _type_and_send(panel, "merhaba")

    assert panel._turns, "yanıt geçmişe hemen eklenmeliydi"
    last = panel._turns[-1]
    assert last.typed is True
    assert last.said == "merhaba"
    assert last.reply == "Merhaba, buradayım."
    text = _history_text(panel)
    assert "merhaba" in text and "Merhaba, buradayım." in text
    assert panel._input.text() == "", "gönderilen metin kutudan silinmeli"


def test_input_is_locked_while_busy_and_unlocked_after(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher
) -> None:
    observed: list[tuple[bool, bool, bool]] = []
    panel = _panel(qapp, settings, dispatcher, FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "ok"}}]))
    real_run = panel._command_runner.run  # type: ignore[union-attr]

    def watching_run(text: str, confirm: Any, **kwargs: Any) -> CommandOutcome:
        # Komut sürerken: kilit, meşgul satırı ve düğme durumu.
        observed.append((panel._is_busy, not panel._busy_line.isHidden(), panel._input.isEnabled()))
        return real_run(text, confirm, **kwargs)

    panel._command_runner.run = watching_run  # type: ignore[union-attr,method-assign]

    _type_and_send(panel, "merhaba")

    assert observed == [(True, True, False)], "komut sürerken kutu kilitli ve 'Düşünüyorum…' görünür olmalı"
    assert panel._is_busy is False
    assert panel._busy_line.isHidden()
    assert panel._input.isEnabled()
    assert panel._send.isEnabled()


def test_empty_input_sends_nothing(qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher) -> None:
    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "x"}}])
    panel = _panel(qapp, settings, dispatcher, llm)

    _type_and_send(panel, "   ")

    assert panel._turns == []


def test_panel_without_a_brain_disables_the_input_with_a_reason(qapp: QApplication, settings: Settings) -> None:
    panel = _panel(qapp, settings, None, None)

    assert panel._input.isEnabled() is False
    assert panel._send.isEnabled() is False
    assert "gerekli" in panel._input.placeholderText()


# --------------------------------------------------------------------------
# Onay: tool adı + argümanlar, Qt köprüsü üzerinden
# --------------------------------------------------------------------------


def test_confirmation_dialog_receives_the_tool_name_and_arguments(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = settings.desktop_path / "eski.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    llm = FakeLLM([{"tool": "filesystem.delete", "arguments": {"target": "eski.txt"}}])
    panel = _panel(qapp, settings, dispatcher, llm)
    asked: list[tuple[str, dict[str, Any]]] = []

    def record_and_refuse(self: ArtemisPanel, tool_name: str, arguments: dict[str, Any]) -> bool:
        asked.append((tool_name, arguments))
        return False

    monkeypatch.setattr(ArtemisPanel, "_ask_confirmation", record_and_refuse)

    _type_and_send(panel, "eski dosyayı sil")

    assert asked == [("filesystem.delete", {"target": "eski.txt"})]
    assert target.exists(), "reddedilen silme gerçekleşmemeli"
    assert panel._turns[-1].is_failed(), "reddedilen adım başarısız olarak işaretlenmeli"


def test_confirmation_round_trip_works_on_the_worker_thread(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gerçek iş parçacığı: onay GUI'de sorulur, komut iş parçacığı cevabı bekler, kilit açılır."""

    target = settings.desktop_path / "onayli.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    llm = FakeLLM([{"tool": "filesystem.delete", "arguments": {"target": "onayli.txt"}}])
    panel = _panel(qapp, settings, dispatcher, llm, background=True)
    monkeypatch.setattr(ArtemisPanel, "_ask_confirmation", lambda self, tool, args: False)

    _type_and_send(panel, "onaylı dosyayı sil")
    _wait_until_idle(qapp, panel)

    assert panel._turns and panel._turns[-1].typed
    assert target.exists()


# --------------------------------------------------------------------------
# Hatalar: kırmızı balon, çökme yok
# --------------------------------------------------------------------------


def test_llm_failure_shows_a_red_error_bubble(qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher) -> None:
    panel = _panel(qapp, settings, dispatcher, FakeLLM(error=ConnectionError("ollama yok")))

    _type_and_send(panel, "merhaba")

    last = panel._turns[-1]
    assert last.kind == "error"
    assert last.reply == "Yerel modele ulaşamıyorum."
    assert _has_red_border(panel), "hata balonu kırmızı kenarlıkla çizilmeli"
    assert panel._input.isEnabled(), "hata sonrası kutu yeniden kullanılabilir olmalı"


def test_unexpected_exception_never_crashes_the_panel(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _panel(qapp, settings, dispatcher, FakeLLM())

    def boom(text: str, confirm: Any, **kwargs: Any) -> CommandOutcome:
        raise RuntimeError("beklenmeyen")

    monkeypatch.setattr(panel._command_runner, "run", boom)  # type: ignore[union-attr]

    _type_and_send(panel, "merhaba")

    last = panel._turns[-1]
    assert last.kind == "error"
    assert "beklenmeyen" in last.reply
    assert panel._input.isEnabled()
    assert panel._is_busy is False


def test_parse_failure_is_an_error_bubble_not_a_success(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher
) -> None:
    panel = _panel(qapp, settings, dispatcher, FakeLLM(error=LLMResponseParseError("bozuk")))

    _type_and_send(panel, "merhaba")

    assert panel._turns[-1].kind == "error"


def _has_red_border(panel: ArtemisPanel) -> bool:
    return theme.ACCENT_RED.name().lower() in panel._history.toHtml().lower()


# --------------------------------------------------------------------------
# Proje bildirimi balonu
# --------------------------------------------------------------------------


def test_project_reply_is_kept_as_a_project_bubble(qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher) -> None:
    panel = _panel(qapp, settings, dispatcher, FakeLLM())
    panel.append_exchange("FastAPI olsun", CommandOutcome(reply="Hangi veritabanı?", kind="project"))

    assert panel._turns[-1].kind == "project"
    assert panel._turns[-1].is_failed() is False


# --------------------------------------------------------------------------
# Log: yazılı turlar geri okunabilir ve "Yenile" aynı balonları getirir
# --------------------------------------------------------------------------


def test_typed_turns_are_logged_and_read_back_as_typed_bubbles(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ui.panel")
    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "Açtım."}}])
    panel = _panel(qapp, settings, dispatcher, llm)

    _type_and_send(panel, "hesap makinesini aç")

    messages = [record.getMessage() for record in caplog.records if record.name == "ui.panel"]
    assert "Yazılı komut: hesap makinesini aç" in messages
    assert "Yazılı cevap: Açtım." in messages

    log_lines = [
        f"2026-07-26 03:13:04,740 | INFO     | {record.name} | {record.getMessage()}"
        for record in caplog.records
        if record.name == "ui.panel"
    ]
    path = settings.log_dir / "artemis.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    turns = parse_history(path)
    assert [(t.typed, t.said, t.reply, t.kind) for t in turns] == [
        (True, "hesap makinesini aç", "Açtım.", "done")
    ]


def test_error_reply_keeps_its_kind_when_read_back(tmp_path: Path) -> None:
    path = tmp_path / "artemis.log"
    path.write_text(
        "2026-07-26 03:14:00,000 | INFO     | ui.panel | Yazılı komut: kapat\n"
        "2026-07-26 03:14:01,000 | INFO     | ui.panel | Yazılı cevap (hata): Yerel modele ulaşamıyorum.\n"
        "2026-07-26 03:15:00,000 | INFO     | ui.panel | Yazılı komut: proje aç\n"
        "2026-07-26 03:15:01,000 | INFO     | ui.panel | Yazılı cevap (proje): Hangi dil?\n",
        encoding="utf-8",
    )

    turns = parse_history(path)

    assert [t.kind for t in turns] == ["error", "project"]
    assert turns[0].is_failed() and not turns[1].is_failed()


def test_a_typed_turn_does_not_steal_the_tool_lines_of_a_voice_turn(tmp_path: Path) -> None:
    """Sesli tur, yazılı komutla başlayan satırdan sonra kapanmalı; araç satırı yanlış turda kalmamalı."""

    path = tmp_path / "artemis.log"
    path.write_text(
        "2026-07-26 03:10:00,000 | INFO     | core.voice_loop | Duyulan komut: 'merhaba'\n"
        "2026-07-26 03:10:01,000 | INFO     | ui.panel | Yazılı komut: yazılı şey\n"
        "2026-07-26 03:10:02,000 | INFO     | core.dispatcher | Tool çalıştırıldı: web.search -> success=True\n",
        encoding="utf-8",
    )

    turns = parse_history(path)

    voice, typed = turns
    assert voice.tools == []
    assert typed.tools == [("web.search", True)]


# --------------------------------------------------------------------------
# Görünüm: balonlar ve kart birlikte, boş durum
# --------------------------------------------------------------------------


def test_three_bubble_pairs_render_success_failure_and_project(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher
) -> None:
    panel = _panel(qapp, settings, dispatcher, FakeLLM())
    panel.append_exchange("Not defterini aç", CommandOutcome(reply="Açtım.", kind="done"))
    panel.append_exchange(
        "Chrome'u kapat",
        CommandOutcome(reply="Kapatılamadı.", kind="done", step_results=_failed_step("windows.close_app")),
    )
    panel.append_exchange("Yeni proje", CommandOutcome(reply="Proje adı ne olsun?", kind="project"))

    assert [t.kind for t in panel._turns] == ["done", "done", "project"]
    assert [t.is_failed() for t in panel._turns] == [False, True, False]
    text = _history_text(panel)
    for needle in ("Not defterini aç", "Açtım.", "Chrome'u kapat", "Kapatılamadı.", "Yeni proje", "Proje adı ne olsun?"):
        assert needle in text
    assert "Uygulamayı kapatma" in text


def _failed_step(tool: str):  # type: ignore[no-untyped-def]
    from core.planner import StepResult
    from models.tool_models import ToolResult

    return (StepResult(1, tool, {}, ToolResult(success=False, message="Kapatılamadı.")),)


def test_history_turn_failure_rule() -> None:
    assert HistoryTurn(timestamp="t", kind="error").is_failed()
    assert HistoryTurn(timestamp="t", tools=[("web.search", False)]).is_failed()
    assert not HistoryTurn(timestamp="t", tools=[("web.search", True)]).is_failed()


# --------------------------------------------------------------------------
# Arka plan iş parçacığı ve tek örnek pencere
# --------------------------------------------------------------------------


def _wait_until_idle(qapp: QApplication, panel: ArtemisPanel, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while panel._is_busy and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not panel._is_busy, "komut zaman aşımında bitmedi"


def test_background_command_completes_and_updates_the_panel(
    qapp: QApplication, settings: Settings, dispatcher: ToolDispatcher
) -> None:
    llm = FakeLLM([{"tool": "assistant.reply", "arguments": {"message": "Arka planda tamam."}}])
    panel = _panel(qapp, settings, dispatcher, llm, background=True)

    _type_and_send(panel, "arka planda dene")
    _wait_until_idle(qapp, panel)

    assert panel._turns[-1].reply == "Arka planda tamam."
    assert panel._input.isEnabled()


def test_show_panel_keeps_one_instance_and_close_only_hides_it(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_module, "_open_panel", None)

    first = show_panel(settings, None, None)
    second = show_panel(settings, None, None)
    assert first is second, "panel bir kez kurulmalı, tekrar açılışta yeniden yaratılmamalı"

    first.close()
    assert first.isHidden(), "kapatma paneli gizlemeli"
    assert show_panel(settings, None, None) is first
