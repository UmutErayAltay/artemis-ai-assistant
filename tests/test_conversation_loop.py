"""`core/conversation_loop.py` testleri — `python main.py --chat` yolu.

Bu dosya bir boşluğu kapatıyor: `conversation_loop` `tests/` içinde HİÇ
referans edilmiyordu. Yani metin modunun tamamı (girdi okuma, çıkış
komutları, onay ekranı, hata mesajları, adım raporlama) test edilmemişti.

Sahte olan tek şey terminal G/Ç ve LLM. Dispatcher, planner ve
`filesystem_plugin` GERÇEK: testin sonunda `tmp_path` altında gerçekten
bir klasör oluşur ve onay kapısı gerçekten işler.
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import Settings
from core.conversation_loop import _confirm_with_user, run
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError


class FakeLLM:
    """Sırayla verilen planları döndüren sahte istemci.

    Bir eleman `Exception` ise fırlatılır — böylece hata yolları da
    gerçek `run()` üzerinden sınanabilir.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def get_tool_calls(self, system_prompt: str, user_input: str) -> list[dict[str, Any]]:
        self.prompts.append(user_input)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _run_with_inputs(
    dispatcher: ToolDispatcher,
    monkeypatch: pytest.MonkeyPatch,
    inputs: list[str],
    llm: FakeLLM,
) -> list[str]:
    """`run()`'ı sahte bir terminalle çalıştırır ve yazdırılanları döndürür."""

    kalan = list(inputs)
    ciktilar: list[str] = []

    monkeypatch.setattr("builtins.input", lambda prompt="": kalan.pop(0))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: ciktilar.append(" ".join(str(a) for a in args)))

    run(dispatcher, llm)  # type: ignore[arg-type]
    return ciktilar


@pytest.mark.parametrize("exit_word", ["çıkış", "cikis", "exit", "quit", "ÇIKIŞ", "Exit"])
def test_exit_commands_end_the_loop(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch, exit_word: str
) -> None:
    llm = FakeLLM([])

    ciktilar = _run_with_inputs(dispatcher, monkeypatch, [exit_word], llm)

    assert llm.prompts == [], "çıkış komutu modele hiç gönderilmemeli"
    assert any("Görüşürüz" in c for c in ciktilar)


def test_empty_input_is_skipped_without_calling_the_model(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boş satır bir komut değildir; modeli meşgul etmemeli."""

    llm = FakeLLM([])

    _run_with_inputs(dispatcher, monkeypatch, ["", "   ", "çıkış"], llm)

    assert llm.prompts == []


def test_a_real_command_runs_end_to_end(
    dispatcher: ToolDispatcher, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GERÇEK dispatcher ve GERÇEK filesystem tool'u ile uçtan uca."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    llm = FakeLLM([[{"tool": "filesystem.create_folder", "arguments": {"name": "Orbit", "location": "desktop"}}]])

    ciktilar = _run_with_inputs(dispatcher, monkeypatch, ["orbit klasörü oluştur", "çıkış"], llm)

    assert (settings.desktop_path / "Orbit").is_dir()
    assert any("Orbit" in c for c in ciktilar)


def test_unparseable_model_output_does_not_kill_the_loop(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ayrıştırılamayan çıktı kullanıcıya söylenmeli, döngü DEVAM etmeli."""

    llm = FakeLLM([LLMResponseParseError("bozuk"), [{"tool": "assistant.reply", "arguments": {"message": "merhaba"}}]])

    ciktilar = _run_with_inputs(dispatcher, monkeypatch, ["anlaşılmaz", "merhaba", "çıkış"], llm)

    assert any("Anlayamadım" in c for c in ciktilar)
    assert any("merhaba" in c for c in ciktilar), "döngü ikinci komutu işlemeliydi"


def test_connection_error_does_not_kill_the_loop(
    dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama'ya ulaşılamaması, sohbeti sonlandırmak için bir sebep değil."""

    llm = FakeLLM([ConnectionError("sunucu kapalı")])

    ciktilar = _run_with_inputs(dispatcher, monkeypatch, ["bir şey yap", "çıkış"], llm)

    assert any("ulaşılamıyor" in c for c in ciktilar)


def test_multi_step_plan_is_reported_with_step_numbers(
    dispatcher: ToolDispatcher, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    llm = FakeLLM(
        [
            [
                {"tool": "filesystem.create_folder", "arguments": {"name": "Bir", "location": "desktop"}},
                {"tool": "filesystem.create_folder", "arguments": {"name": "Iki", "location": "desktop"}},
            ]
        ]
    )

    ciktilar = _run_with_inputs(dispatcher, monkeypatch, ["iki klasör oluştur", "çıkış"], llm)

    assert any("[1/2]" in c for c in ciktilar)
    assert any("[2/2]" in c for c in ciktilar)


def test_steps_never_reached_are_reported_honestly(
    dispatcher: ToolDispatcher, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan erken durduysa kullanıcı KAÇ adımın çalışmadığını öğrenmeli.

    Sessizce yarım kalan bir plan, kullanıcının hepsinin olduğunu
    sanmasına yol açar.
    """

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    llm = FakeLLM(
        [
            [
                {"tool": "filesystem.open", "arguments": {"target": "olmayan", "location": "desktop"}},
                {"tool": "filesystem.create_folder", "arguments": {"name": "Sonraki", "location": "desktop"}},
            ]
        ]
    )

    ciktilar = _run_with_inputs(dispatcher, monkeypatch, ["önce aç sonra oluştur", "çıkış"], llm)

    assert any("çalıştırılmadı" in c for c in ciktilar)
    assert not (settings.desktop_path / "Sonraki").exists()


# --- Onay ekranı ---------------------------------------------------------
#
# Onay, bu projenin tek güvenlik bariyeri. README §16b: "bir onay
# mekanizması, onaylanan şeyi göstermiyorsa güvenlik sağlamaz — yalnızca
# güvenlik hissi verir."


@pytest.mark.parametrize("answer", ["e", "evet", "y", "yes", "  Evet  ", "YES"])
def test_affirmative_answers_confirm(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)

    assert _confirm_with_user("filesystem.delete", {"target": "x"}) is True


@pytest.mark.parametrize("answer", ["h", "hayır", "n", "no", "", "belki", "eee"])
def test_anything_else_is_a_refusal(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    """ŞÜPHEDE REDDET: yalnızca net bir onay kabul edilir."""

    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)

    assert _confirm_with_user("filesystem.delete", {"target": "x"}) is False


def test_confirmation_shows_what_is_being_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kullanıcı NEYİ onayladığını görmeli — yalnızca tool adını değil.

    `target` argümanı olmadan `filesystem.delete` hangi dosyayı sileceğini
    söylemez; kör onay bir güvenlik açığıdır.
    """

    yazilanlar: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": "h")
    monkeypatch.setattr("builtins.print", lambda *a, **k: yazilanlar.append(" ".join(str(x) for x in a)))

    _confirm_with_user("filesystem.delete", {"target": "onemli.txt", "location": "desktop"})

    metin = "\n".join(yazilanlar)
    assert "filesystem.delete" in metin
    assert "onemli.txt" in metin, "silinecek dosyanın adı GÖSTERİLMELİ"
    assert "desktop" in metin


def test_dangerous_step_is_refused_and_the_file_survives(
    dispatcher: ToolDispatcher, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uçtan uca: kullanıcı "h" derse dosya GERÇEKTEN durmalı."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    kurban = settings.desktop_path / "onemli.txt"
    kurban.write_text("silinmemeli", encoding="utf-8")

    llm = FakeLLM([[{"tool": "filesystem.delete", "arguments": {"target": "onemli.txt", "location": "desktop"}}]])

    _run_with_inputs(dispatcher, monkeypatch, ["onemli.txt sil", "h", "çıkış"], llm)

    assert kurban.exists()
    assert kurban.read_text(encoding="utf-8") == "silinmemeli"


def test_dangerous_step_runs_after_explicit_approval(
    dispatcher: ToolDispatcher, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    kurban = settings.desktop_path / "gecici.txt"
    kurban.write_text("silinebilir", encoding="utf-8")

    llm = FakeLLM([[{"tool": "filesystem.delete", "arguments": {"target": "gecici.txt", "location": "desktop"}}]])

    _run_with_inputs(dispatcher, monkeypatch, ["gecici.txt sil", "e", "çıkış"], llm)

    assert not kurban.exists()
