"""`core/openrouter_client.py` testleri.

Gerçek ağ isteği ASLA atılmaz ve gerçek bir API anahtarı ASLA
kullanılmaz: `requests.post`, her testte `monkeypatch` ile sahte bir
fonksiyona bağlanır — `tests/test_voice_stt_cloud.py`'in HTTP eşleniğiyle
birebir aynı desen. Anahtar değerleri de `_FAKE_SECRET_KEY` dışında
kullanılmaz; sızma testi (aşağıda) bunu denetler.

Buradaki asıl söz, "bulut da aynı işi yapar" sözüdür: tool-call şeması,
ayrıştırma ve düzeltme döngüsü `core/llm_client`'dan doğrudan alınır
(bkz. README §16a), dolayısıyla bu testler aslında O SÖZLEŞMENİN bulut
tarafında da geçerli olduğunu doğrular.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest
import requests

from core.llm_client import LLMResponseParseError, OllamaLLMClient
from core.openrouter_client import OpenRouterLLMClient, OpenRouterUnavailableError

_FAKE_SECRET_KEY = "sk-or-test_super_secret_value_should_never_leak_12345"

_VALID_CALLS = (
    '[{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]'
)


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class _FakeResponse:
    """`requests.Response`'un testler için gereken minimal sahte hali."""

    def __init__(self, status_code: int = 200, json_payload: object = None, json_raises: bool = False) -> None:
        self.status_code = status_code
        self._json_payload = json_payload
        self._json_raises = json_raises

    def json(self) -> object:
        if self._json_raises:
            raise ValueError("geçersiz JSON gövdesi")
        return self._json_payload


def _install_fake_post(
    monkeypatch: pytest.MonkeyPatch,
    response: object = None,
    exc: Exception | None = None,
) -> list[tuple[tuple, dict]]:
    """`requests.post`'u sahte bir çağrıyla değiştirir; yapılan çağrıları
    biriktirip döner (bkz. `tests/test_voice_stt_cloud.py`)."""

    calls: list[tuple[tuple, dict]] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def _install_sequential_post(
    monkeypatch: pytest.MonkeyPatch, responses: list[object]
) -> list[tuple[tuple, dict]]:
    """Her çağrıda listedeki SIRADAKİ yanıtı döndüren sahte `post`.

    Aynı test içinde "önce şu hata, sonra şu yanıt" senaryolarını
    kurmak için gerekir; `iter(...)` + `next` yerine liste kullanılır ki
    listeden tüketilip hata fırlatması testin kendi hatası olsun, sessiz
    bir `StopIteration` değil.
    """

    calls: list[tuple[tuple, dict]] = []
    remaining = list(responses)

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return remaining.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def _client(**kwargs) -> OpenRouterLLMClient:
    kwargs.setdefault("model", "meta-llama/llama-3.1-8b-instruct:free")
    kwargs.setdefault("api_key", _FAKE_SECRET_KEY)
    return OpenRouterLLMClient(**kwargs)


# --- Anahtar yönetimi -----------------------------------------------------


def test_constructor_does_not_raise_without_api_key() -> None:
    """Yönlendirici, anahtar tanımlı değilken de bu nesneyi kurabilmelidir
    (bkz. `main.py::_start_llm_session`); eksik anahtar yalnızca gerçek
    bir çağrıda hataya dönüşür."""

    OpenRouterLLMClient(model="herhangi/bir-model:free")  # patlamamalı


def test_call_without_api_key_raises_before_any_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anahtar kontrolü ağ isteğinden ÖNCE yapılmalı — yoksa "anahtar
    yok" durumunda bile kullanıcının komutu dışarı fırlatılırdı."""

    calls = _install_fake_post(
        monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı")
    )

    with pytest.raises(OpenRouterUnavailableError) as exc_info:
        OpenRouterLLMClient(model="x/y:free").get_raw_response("s", "u")

    assert "OPENROUTER_API_KEY" in str(exc_info.value)
    assert calls == []


def test_blank_api_key_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boşluk dolu bir anahtar "tanımlı" sayılmaz; `secrets.yaml`dan okunan
    bir değerin sonuna yanlışlıkla boşluk gelmesi yaygın bir hata."""

    calls = _install_fake_post(
        monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı")
    )

    with pytest.raises(OpenRouterUnavailableError):
        OpenRouterLLMClient(model="x/y:free", api_key="   ").get_raw_response("s", "u")

    assert calls == []


def test_api_key_is_sent_as_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, _chat_payload('{"karar": "GURULTU"}'))
    )

    _client().should_engage("hmm")

    _, kwargs = calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {_FAKE_SECRET_KEY}"
    assert kwargs["json"]["model"] == "meta-llama/llama-3.1-8b-instruct:free"


# --- Sızma testi ---------------------------------------------------------


def test_api_key_never_appears_in_error_messages_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Anahtar hiçbir hata mesajına ya da log satırına sızmamalı.

    Hata mesajları kullanıcıya (ve destek talebinde ekran görüntüsü
    olarak) gösterilir; `f"...{self._api_key}..."` yazan bir mesaj
    anahtarı bütün log dosyalarına yayardı.
    """

    caplog.set_level(logging.DEBUG)
    _install_fake_post(monkeypatch, response=_FakeResponse(401, {"error": "invalid"}))

    with pytest.raises(OpenRouterUnavailableError) as exc_info:
        _client().get_raw_response("s", "u")

    assert _FAKE_SECRET_KEY not in str(exc_info.value)
    assert _FAKE_SECRET_KEY not in caplog.text


# --- Başarılı yanıt ------------------------------------------------------


def test_get_raw_response_returns_model_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload("Merhaba!")))

    assert _client().get_raw_response("sistem promptu", "selam") == "Merhaba!"


def test_get_raw_response_sends_system_and_user_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload("tamam")))

    _client().get_raw_response("sistem promptu", "selam")

    _, kwargs = calls[0]
    assert kwargs["json"]["messages"] == [
        {"role": "system", "content": "sistem promptu"},
        {"role": "user", "content": "selam"},
    ]


def test_get_tool_calls_parses_ollama_compatible_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bulut istemcisi, `OllamaLLMClient.extract_tool_calls` ile AYNI
    ayrıştırıcıyı kullanır (README §16a: iki sağlayıcı, iki ayrı ayrıştırıcı
    DEĞİL)."""

    _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload(_VALID_CALLS)))

    calls = _client().get_tool_calls("sistem promptu", "Orbit'i aç")

    assert calls == [{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]


def test_get_tool_calls_returns_multi_step_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prompts/system_prompt.md` çok adımlı plan isteyebiliyor; bulut
    yolunda da iki adımlık plan iki adım olarak dönmeli."""

    two_steps = (
        '[{"tool": "windows.launch_app", "arguments": {"name": "chrome"}}, '
        '{"tool": "filesystem.search", "arguments": {"query": "rapor"}}]'
    )
    _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload(two_steps)))

    calls = _client().get_tool_calls("sistem promptu", "chrome'u aç ve raporu bul")

    assert len(calls) == 2
    assert calls[0]["tool"] == "windows.launch_app"


def test_first_strategy_asks_for_the_same_json_schema_as_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """İlk strateji OLLAMA İLE AYNI şemayı göndermelidir.

    `OllamaLLMClient._response_schema()` güncel `TOOL_REGISTRY` tool
    adlarını `enum` olarak dayattığı için, iki sağlayıcı aynı araç kümesini
    görüyor. Şemalar ayrışırsa README §16a'da anlatılan sessiz arıza
    (modele "hangi biçim geçerli" diye iki farklı cevap verilmesi) geri gelir.
    """

    calls = _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload(_VALID_CALLS)))

    _client().get_tool_calls("s", "u")

    response_format = calls[0][1]["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == OllamaLLMClient._response_schema()


def test_working_strategy_is_tried_first_on_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Şema desteklenmeyen bir modelde iki hatalı deneme gerekir; ikinci
    çağrıda bilinen çalışan strateji BAŞA alınmalı (Ollama'daki
    `_working_strategy_key` fikri) — yoksa her komut aynı iki reddi
    tekrarlar, kullanıcı her seferinde iki kat gecikme öder.

    Saklanan şey POZİSYON DEĞİL KİMLİKTİR: liste her çağrıda yeniden
    sıralandığı için "ikinci eleman" bir sonraki çağrıda başka bir
    stratejiye denk gelirdi.
    """

    calls = _install_sequential_post(
        monkeypatch,
        [
            _FakeResponse(400, {"error": "response_format desteklenmiyor"}),
            _FakeResponse(200, _chat_payload(_VALID_CALLS)),
            _FakeResponse(200, _chat_payload(_VALID_CALLS)),
        ],
    )
    client = _client()

    client.get_tool_calls("s", "u")
    client.get_tool_calls("s", "u")

    assert calls[2][1]["json"]["response_format"]["type"] == "json_object", (
        "Çalışan strateji ikinci çağrıda başa alınmalıydı"
    )


# --- Strateji düşüşü -----------------------------------------------------


def test_schema_error_falls_back_to_plain_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`response_format: json_schema` desteklenmeyen bir model/sağlayıcı
    HTTP 400 döndüğünde (Ollama'daki `except Exception: continue`
    ayrımının aynısı) bir sonraki seviyeye düşülmeli."""

    calls = _install_sequential_post(
        monkeypatch,
        [
            _FakeResponse(400, {"error": "unsupported response_format"}),
            _FakeResponse(200, _chat_payload(_VALID_CALLS)),
        ],
    )

    calls_result = _client().get_tool_calls("s", "u")

    assert calls_result == [{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]
    assert calls[0][1]["json"]["response_format"]["type"] == "json_schema"
    assert calls[1][1]["json"]["response_format"]["type"] == "json_object"


def test_unparseable_body_counts_as_a_strategy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 200 ama gövde bozuksa bu da "bu strateji işe yaramadı"
    sayılmalı — bozuk bir yanıt genelde o formatı desteklemeyen bir
    modelden gelir."""

    _install_sequential_post(
        monkeypatch,
        [
            _FakeResponse(200, {"choices": []}),
            _FakeResponse(200, _chat_payload(_VALID_CALLS)),
        ],
    )

    assert _client().get_tool_calls("s", "u") == [
        {"tool": "filesystem.open", "arguments": {"target": "Orbit"}}
    ]


def test_all_strategies_failing_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(400, {"error": "kotu istek"}))

    with pytest.raises(OpenRouterUnavailableError):
        _client().get_tool_calls("s", "u")


# --- Kimlik doğrulama / kota / hız sınırı --------------------------------
#
# Bu üçü strateji denemesi YAPMADAN yükseltilir: anahtar yanlışsa, kota
# bittiyse ya da hız sınırı varsa sorun FORMAT değildir; aynı isteği
# üç farklı biçimde göndermek yalnızca aynı reddi üç kez alıp kullanıcıyı
# bekletir.


def test_http_401_raises_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(401, {"error": "invalid_api_key"}))

    with pytest.raises(OpenRouterUnavailableError) as exc_info:
        _client().get_raw_response("s", "u")

    assert "anahtar" in str(exc_info.value).lower()
    assert len(calls) == 1, "Kimlik doğrulama hatası strateji denemesi GEREKTİRMEZ"


def test_http_402_explains_the_free_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """402, kullanıcının TAM OLARAK karşılaşacağı senaryodur (ücretsiz
    kota tükendi / model artık ücretsiz değil) — mesaj somut ve yönlendirici
    olmalıdır."""

    calls = _install_fake_post(monkeypatch, response=_FakeResponse(402, {"error": "credits"}))

    with pytest.raises(OpenRouterUnavailableError) as exc_info:
        _client().get_raw_response("s", "u")

    message = str(exc_info.value)
    assert "402" in message
    assert "ücretsiz" in message
    assert len(calls) == 1


def test_http_429_raises_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(429, {"error": "rate_limited"}))

    with pytest.raises(OpenRouterUnavailableError):
        _client().get_raw_response("s", "u")

    assert len(calls) == 1


# --- Ağ hataları ---------------------------------------------------------


def test_timeout_raises_unavailable_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zaman aşımında BAŞKA STRATEJİ DENEMEK ANLAMSIZDIR.

    Ollama tarafındaki gerçek olay: sunucu kapalıyken düz bir bağlantı
    reddi dört stratejinin dördünde tekrarlanıyor, `get_tool_calls` de üç
    kez daha denediği için TEK bir söz 12 çağrıya çıkabiliyordu. Burada
    süre de eklendiği için (30 sn) 3 strateji = 90 saniyelik bekleme,
    `openrouter_timeout_seconds`'in tüm varlık sebebini (yarım saniyede
    yerele düşmek) çürütürdü.
    """

    calls = _install_fake_post(monkeypatch, exc=requests.exceptions.Timeout("zaman aşımı"))

    with pytest.raises(OpenRouterUnavailableError):
        _client().get_raw_response("s", "u")

    assert len(calls) == 1, "Zaman aşımı strateji denemesi GEREKTİRMEZ"


def test_connection_error_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, exc=requests.exceptions.ConnectionError("DNS çözülemedi"))

    with pytest.raises(OpenRouterUnavailableError):
        _client().get_raw_response("s", "u")


def test_timeout_error_mentions_the_configured_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mesajde sürenin geçmesi, kullanıcının "30 saniye bekliyorum" diye
    config'i açıp `openrouter_timeout_seconds`'i bulmasını sağlar."""

    _install_fake_post(monkeypatch, exc=requests.exceptions.Timeout("zaman aşımı"))

    with pytest.raises(OpenRouterUnavailableError) as exc_info:
        _client(timeout_seconds=12.5).get_raw_response("s", "u")

    assert "12.5" in str(exc_info.value)


# --- Düzeltme döngüsü ---------------------------------------------------


def test_get_tool_calls_retries_after_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Geçersiz ilk cevaptan sonra düzeltme mesajı EKLENİP tekrar
    denemeli (Ollama ile birebir aynı döngü)."""

    calls = _install_sequential_post(
        monkeypatch,
        [
            _FakeResponse(200, _chat_payload("Açıyorum, bir saniye...")),
            _FakeResponse(200, _chat_payload(_VALID_CALLS)),
        ],
    )

    result = _client().get_tool_calls("sistem promptu", "Orbit'i aç", max_retries=2)

    assert result == [{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]
    second_messages = calls[1][1]["json"]["messages"]
    assert second_messages[-1]["role"] == "user"
    assert "tool" in second_messages[-1]["content"].lower()


def test_get_tool_calls_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_post(
        monkeypatch, response=_FakeResponse(200, _chat_payload("hep gecersiz kalacak"))
    )

    with pytest.raises(LLMResponseParseError):
        _client().get_tool_calls("sistem promptu", "bir seyler yap", max_retries=1)


# --- Komut kapısı: KAPI AÇIK BAŞARISIZ OLUR -----------------------------


def test_gate_engages_for_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload('{"karar": "YONELIK"}')))

    assert _client().should_engage("sen kimsin?") is True


def test_gate_rejects_background_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, _chat_payload('{"karar": "GURULTU"}')))

    assert _client().should_engage("bilmiyorum ya öyle bir şey işte") is False


def test_gate_rejects_empty_input_without_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_post(
        monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı")
    )

    assert _client().should_engage("   ") is False
    assert calls == []


def test_gate_fails_open_when_the_request_blows_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ağ hatasında kapı BOZULMAZ, açık kalır.

    Kapının tek işi gürültü filtresi; kapalı kalsa bir ağ hatası tüm
    sesli asistanı körleştirirdi. Ses işçisi yalnızca bu metodu
    kullandığı için, istisna yukarı sıksa işçi ölürdü.
    """

    _install_fake_post(monkeypatch, exc=requests.exceptions.ConnectionError("ağ yok"))

    assert _client().should_engage("bilmiyorum ya") is True


def test_gate_fails_open_when_the_api_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anahtar yoksa kapı yine AÇILIR. `auto` modda bu kritiktir: kapı
    bulut anahtarsız çalışsa yönlendirici hiçbir zaman bu hatayı
    yakalayamaz, ama asistan da çalışmaz hale gelirdi — oysa yerel
    modelle konuşmak mümkün."""

    _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    assert OpenRouterLLMClient(model="x/y:free").should_engage("merhaba") is True


def test_gate_fails_open_on_unparseable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(
        monkeypatch, response=_FakeResponse(200, _chat_payload("bu bir JSON değil"))
    )

    assert _client().should_engage("merhaba") is True


def test_gate_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sözleşme: `should_engage` HİÇBİR koşulda istisna yükseltmez.

    Bu test kasıtlı olarak çok sayıda bozuk yanıt dener; amaç tek bir
    senaryoyu doğrulamak değil, sözleşmenin "her girdi altında geçerli
    olduğunu" göstermek.
    """

    _install_fake_post(
        monkeypatch,
        response=_FakeResponse(200, {"choices": [{"message": {"content": None}}]}),
    )

    assert _client().should_engage("merhaba") is True


# --- Modül import'unun bağımlılık gerektirmemesi -------------------------


def test_module_imports_without_requests_installed() -> None:
    """`requests` yalnızca `_chat` içinde lazy import edilir; bu modül,
    `requests` kurulu olmayan bir ortamda da import edilebilmelidir
    (bkz. `voice/stt_cloud.py` deseni)."""

    module = types.ModuleType("requests")
    module.exceptions = types.SimpleNamespace()
    fake_sys_modules = {"requests": module}
    original = sys.modules.get("requests")
    sys.modules.update(fake_sys_modules)
    try:
        import importlib

        import core.openrouter_client as module_under_test

        reloaded = importlib.reload(module_under_test)
        assert reloaded.OpenRouterLLMClient is not None
    finally:
        if original is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = original


# --- get_structured_response (ARCHITECTURE.md §42) -------------------------


def test_structured_response_uses_named_strict_json_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"type": "object", "properties": {"durum": {"type": "string"}}, "required": ["durum"]}
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(json_payload=_chat_payload('{"durum": "soru"}')))

    result = _client().get_structured_response("sistem", "girdi", schema, "proje_gorusmesi")

    assert result == {"durum": "soru"}
    response_format = calls[0][1]["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"] == {"name": "proje_gorusmesi", "strict": True, "schema": schema}


def test_structured_response_keeps_learned_tool_call_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """json_schema reddedilip json_object çalışsa da tool-call yolunun öğrendiği strateji değişmez."""

    _install_sequential_post(
        monkeypatch,
        [_FakeResponse(status_code=400), _FakeResponse(json_payload=_chat_payload('{"durum": "hazir"}'))],
    )
    client = _client()
    client._working_strategy_key = "json_schema"

    assert client.get_structured_response("s", "g", {"type": "object"}) == {"durum": "hazir"}
    assert client._working_strategy_key == "json_schema"
