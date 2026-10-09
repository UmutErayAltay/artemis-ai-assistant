"""`main.py::_start_llm_session` testleri: `llm_provider` üç modunun AÇILIŞTA
ne yaptığı.

Kullanıcı şikâyeti: `llm_provider: "auto"` iken uygulama her açılışta
yerel Ollama'yı hemen başlatıyor, 15 saniye bekliyor, sonra buluta
düşüyordu. Yani "önce bulut" vaadi sadece bir istek başına geçerliydi;
açılışta yerel sunucu YINE DE ayağa kalkıyordu.

Bu dosya O davranışı kilitler: "auto" modda açılışta Ollama'ya hiç
dokunulmaz, bulut çalışıyorsa hiç dokunulmaya devam edilmez ve sunucu
YALNIZCA bulut başarısız olduğunda (yedeğe düşüldüğünde) başlatılır.
Gerçek Ollama/OpenRouter'a AĞ ÇAĞRISI YAPILMAZ; hepsi sahte nesnelerdir.
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import Settings
from core.llm_client import LLMResponseParseError
from core.llm_router import LLMProviderMode, LLMRouter
from core.ollama_manager import OllamaUnavailableError

import main as main_module


class FakeLLM:
    """Sabit cevap döndüren / hata fırlatan sahte istemci (bkz. `test_llm_router.py`)."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    def _answer(self, method: str) -> str:
        self.calls.append(method)
        if self.error is not None:
            raise self.error
        return self.text

    def get_raw_response(self, system_prompt: str, user_input: str) -> str:
        return self._answer("raw")

    def get_tool_calls(
        self, system_prompt: str, user_input: str, max_retries: int = 2
    ) -> list[dict[str, Any]]:
        return self._answer("tools")

    def should_engage(self, user_input: str) -> bool:
        return self._answer("gate")


class FakeServerManager:
    """`OllamaServerManager` sahtesi: `ensure_running` çağrılarını SAYAR."""

    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self._events = events
        self._error = error

    def ensure_running(self) -> None:
        self._events.append("ensure_running")
        if self._error is not None:
            raise self._error

    def stop_if_we_started_it(self) -> None:
        self._events.append("stop")


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    cloud: FakeLLM,
    server_error: Exception | None = None,
) -> None:
    """`main` modülünün kullandığı HER dış bağımlılığı sahte nesneye bağlar."""

    monkeypatch.setattr(
        main_module, "OllamaServerManager", lambda: FakeServerManager(events, server_error)
    )
    monkeypatch.setattr(main_module, "OpenRouterLLMClient", lambda **kwargs: cloud)
    monkeypatch.setattr(main_module, "get_openrouter_api_key", lambda: "sahte-anahtar")
    monkeypatch.setattr(main_module, "OllamaLLMClient", lambda **kwargs: FakeLLM("yerel"))
    monkeypatch.setattr(main_module, "list_installed_models", lambda: ["llama3.1:latest"])
    monkeypatch.setattr(
        main_module, "prompt_user_to_select_model", lambda *a, **k: events.append("menu") or "llama3.1"
    )


# --------------------------------------------------------------------------
# "auto" — açılışta Ollama'ya dokunulmaz
# --------------------------------------------------------------------------


def test_auto_mode_does_not_start_ollama_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kullanıcının şikâyet edildiği tam olarak bu: açılışta `ollama serve`
    başlatılıp 15 saniye bekleniyordu, üstelik bulut HİÇ denenmeden."""

    events: list[str] = []
    _install_fakes(monkeypatch, events, cloud=FakeLLM("bulut"))
    settings = Settings(llm_provider="auto")

    server_manager, client = main_module._start_llm_session(settings)

    assert isinstance(client, LLMRouter)
    assert events == [], f"Açılışta Ollama'ya dokunulmamalıydı, görülen: {events}"
    assert server_manager is not None, "Çıkışta kapatılabilmesi için yönetici döndürülmeli"


def test_auto_mode_leaves_ollama_untouched_while_cloud_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bulut her istekte başarılıysa sunucu hiç BAŞLATILMAMALI (tembellik)."""

    events: list[str] = []
    cloud = FakeLLM("bulut")
    _install_fakes(monkeypatch, events, cloud=cloud)
    settings = Settings(llm_provider="auto")

    _, client = main_module._start_llm_session(settings)
    assert client.get_raw_response("s", "bir") == "bulut"
    assert client.get_raw_response("s", "iki") == "bulut"

    assert events == [], f"Bulut çalışırken Ollama'ya dokunulmamalıydı: {events}"
    assert cloud.calls == ["raw", "raw"]


def test_auto_mode_starts_ollama_only_when_cloud_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yedek yol: bulut hata verdiğinde sunucu O AN başlatılır."""

    events: list[str] = []
    cloud = FakeLLM(error=RuntimeError("ağ yok"))
    _install_fakes(monkeypatch, events, cloud=cloud)
    settings = Settings(llm_provider="auto")

    _, client = main_module._start_llm_session(settings)
    assert events == [], "Açılışta yine dokunulmamalıydı"

    assert client.get_raw_response("s", "bir") == "yerel"
    assert events == ["ensure_running"], f"Yedeğe düşüldüğünde sunucu başlatılmalıydı: {events}"


def test_auto_mode_asks_no_model_menu_on_the_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yedek yol `input()` çağırmaz.

    Menü yalnızca "local" modda sorulur: yedek yol ilk konuşmada, `--voice`
    gibi KONSOLSUZ bir yolda bile tetiklenebilir ve orada `input()`
    asistanı kilitlerdi. Yedek yol `config.yaml::ollama_model`'i kullanır.
    """

    events: list[str] = []
    _install_fakes(monkeypatch, events, cloud=FakeLLM(error=RuntimeError("ağ yok")))
    settings = Settings(llm_provider="auto")

    _, client = main_module._start_llm_session(settings)
    client.get_raw_response("s", "bir")

    assert "menu" not in events, f"Yedek yolda model menüsü sorulmamalı: {events}"


def test_auto_mode_reports_honestly_when_local_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bulut da yoksa yerel de yoksa hata YUTULMAZ (koşulsuz başarı yasak).

    Hata `ConnectionError` olarak yükselir: dolaşım döngüleri bu türü
    yakalayıp Türkçe mesaj gösteriyor; ham `OllamaUnavailableError`
    sızsaydı kullanıcı traceback basılırdı.
    """

    events: list[str] = []
    _install_fakes(
        monkeypatch,
        events,
        cloud=FakeLLM(error=RuntimeError("ağ yok")),
        server_error=OllamaUnavailableError("ollama yok"),
    )
    settings = Settings(llm_provider="auto")

    _, client = main_module._start_llm_session(settings)

    with pytest.raises(ConnectionError) as exc_info:
        client.get_raw_response("s", "bir")
    assert "ollama yok" in str(exc_info.value)


# --------------------------------------------------------------------------
# "cloud" ve "local" — DEĞİŞMEMELİ
# --------------------------------------------------------------------------


def test_cloud_mode_never_starts_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """'cloud' modu Ollama'ya dokunmaz — ne açılışta ne de hata halinde."""

    events: list[str] = []
    cloud = FakeLLM("bulut")
    _install_fakes(monkeypatch, events, cloud=cloud)
    settings = Settings(llm_provider="cloud")

    server_manager, client = main_module._start_llm_session(settings)

    assert server_manager is None, "'cloud' modunda sunucu yöneticisi dönmemeli"
    assert client.get_raw_response("s", "bir") == "bulut"
    assert events == []


def test_cloud_mode_does_not_fall_back_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """'cloud' modunda hata YEREL'e düşmez — kullanıcının tercihi çiğnenmez."""

    events: list[str] = []
    cloud = FakeLLM(error=RuntimeError("429"))
    _install_fakes(monkeypatch, events, cloud=cloud)
    settings = Settings(llm_provider="cloud")

    _, client = main_module._start_llm_session(settings)

    with pytest.raises(RuntimeError):
        client.get_raw_response("s", "bir")
    assert events == [], "'cloud' modunda Ollama başlatılmamalıydı"


def test_local_mode_still_starts_ollama_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """'local' modu DEĞİŞMEDİ: sunucu açılışta kurulur, menü sorulur."""

    events: list[str] = []
    cloud = FakeLLM("bulut")
    _install_fakes(monkeypatch, events, cloud=cloud)
    settings = Settings(llm_provider="local")

    server_manager, client = main_module._start_llm_session(settings)

    assert events == ["ensure_running", "menu"]
    assert server_manager is not None
    assert not isinstance(client, LLMRouter)
    assert cloud.calls == [], "'local' modda bulut istemcisi hiç kurulmamalı"


def test_local_mode_falls_back_to_cloud_when_ollama_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'local' modunda Ollama yoksa buluta düşülür — eski davranış korunur."""

    events: list[str] = []
    cloud = FakeLLM("bulut")
    _install_fakes(
        monkeypatch,
        events,
        cloud=cloud,
        server_error=OllamaUnavailableError("ollama kurulu değil"),
    )
    settings = Settings(llm_provider="local")

    _, client = main_module._start_llm_session(settings)

    assert isinstance(client, FakeLLM) and client is cloud


def test_invalid_provider_mode_is_rejected_by_the_router() -> None:
    """Yönlendirici modu doğrulamaya devam ediyor (regresyon değil)."""

    with pytest.raises(ValueError):
        LLMRouter(lambda: FakeLLM(), lambda: FakeLLM(), mode="yarim-bulut")

    assert LLMProviderMode.AUTO == "auto"
