"""`voice.stt_azure.AzureSpeechToText` testleri.

Gerçek ağ isteği ASLA atılmaz: `requests.post`, her testte `monkeypatch`
ile sahte bir fonksiyonla değiştirilir — `tests/test_voice_stt_cloud.py`
ile aynı desen (CLAUDE.md'deki "ollama modülünü sahte bir modülle
değiştir" deseninin HTTP eşleniği). Sentetik PCM üretimi de aynı
dosyadaki yardımcı fonksiyonla birebir aynı yaklaşımı izler.
"""

from __future__ import annotations

import logging
import wave
from io import BytesIO

import numpy as np
import pytest
import requests

from voice.audio import SAMPLE_RATE
from voice.stt import MIN_TRANSCRIBE_SECONDS
from voice.stt_azure import AzureSpeechToText, CloudSpeechUnavailableError

_FAKE_SECRET_KEY = "azsk_test_super_secret_value_should_never_leak_98765"


def _pcm_bytes(duration: float = 1.0) -> bytes:
    """Belirtilen sürede (saniye) rastgele içerikli 16-bit PCM üretir.

    `AzureSpeechToText` (yerel `SpeechRecorder`'ın aksine) genliğe hiç
    bakmaz — burada tek önemli olan örnek SAYISI, süre eşiği hesaplarını
    (`MIN_TRANSCRIBE_SECONDS`, 60 saniyelik azami sınır) doğru tetiklemektir.
    İçerik bilinçli olarak rastgele seçilir ki testler yanlışlıkla "hep
    sıfır bir PCM de yeterli" gibi kör bir noktaya dayanmasın.
    """

    frame_count = max(1, int(SAMPLE_RATE * duration))
    rng = np.random.default_rng(7)
    return rng.integers(-12_000, 12_000, size=frame_count, dtype=np.int16).tobytes()


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
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
) -> list[tuple[tuple, dict]]:
    """`requests.post`'u sahte bir çağrıyla değiştirir; yapılan çağrıları biriktirip döner.

    `voice/stt_azure.py` `requests`'i lazy import ettiği için (`import
    requests` fonksiyon içinde), burada `requests` modülünün `post`
    özniteliğini değiştirmek yeterlidir — `sys.modules` üzerinden aynı
    modül nesnesi paylaşılır, gerçek bir ağ isteği ASLA atılmaz.
    """

    calls: list[tuple[tuple, dict]] = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


# --- Anahtar/bölge yönetimi -------------------------------------------------


def test_constructor_does_not_raise_without_api_key_or_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    AzureSpeechToText()  # patlamamalı: anahtar/bölge doğrulaması yalnızca transcribe()'da olur


def test_transcribe_without_key_or_region_raises_clear_turkish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = AzureSpeechToText()
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())

    message = str(exc_info.value)
    assert "AZURE_SPEECH_KEY" in message
    assert "AZURE_SPEECH_REGION" in message
    assert "portal.azure.com" in message
    assert calls == []  # anahtar/bölge kontrolü ağ isteğinden ÖNCE yapılmalı


def test_transcribe_with_key_but_missing_region_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yalnızca bölge eksikse de aynı hata fırlatılmalı (anahtar VEYA bölge eksikse)."""

    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = AzureSpeechToText(api_key="fake-key")
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())

    assert "AZURE_SPEECH_REGION" in str(exc_info.value)
    assert calls == []


def test_transcribe_uses_env_vars_when_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SPEECH_KEY", "env-key-123")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "merhaba"})
    )

    stt = AzureSpeechToText()
    text = stt.transcribe(_pcm_bytes())

    assert text == "merhaba"
    args, kwargs = calls[0]
    assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "env-key-123"
    assert "westeurope" in args[0]


# --- Başarılı yanıt ayrıştırma ------------------------------------------


def test_transcribe_parses_successful_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "merhaba"})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    text = stt.transcribe(_pcm_bytes())

    assert text == "merhaba"


def test_transcribe_strips_whitespace_from_display_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(
        monkeypatch,
        response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "  merhaba dünya  "}),
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    text = stt.transcribe(_pcm_bytes())

    assert text == "merhaba dünya"


# --- URL / sorgu parametreleri / header'lar -------------------------------


def test_transcribe_url_contains_region_and_language_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "..."})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="eastus")
    stt.transcribe(_pcm_bytes())

    assert len(calls) == 1
    args, kwargs = calls[0]
    url = args[0]
    assert url == "https://eastus.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    assert kwargs["params"]["language"] == "tr-TR"


def test_transcribe_uses_custom_language(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "..."})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="eastus", language="en-US")
    stt.transcribe(_pcm_bytes())

    _, kwargs = calls[0]
    assert kwargs["params"]["language"] == "en-US"


def test_transcribe_headers_contain_subscription_key_and_samplerate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "..."})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    stt.transcribe(_pcm_bytes())

    _, kwargs = calls[0]
    headers = kwargs["headers"]
    assert headers["Ocp-Apim-Subscription-Key"] == "fake-key"
    assert "samplerate=16000" in headers["Content-Type"]


def test_transcribe_sends_valid_wav_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "..."})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    stt.transcribe(_pcm_bytes(duration=1.0))

    _, kwargs = calls[0]
    body = kwargs["data"]
    assert isinstance(body, (bytes, bytearray))

    with wave.open(BytesIO(body), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getframerate() == SAMPLE_RATE
        assert wav_file.getsampwidth() == 2


# --- "Konuşma yok" durumları: hata DEĞİL, boş string ----------------------


def test_transcribe_no_match_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "NoMatch"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    assert stt.transcribe(_pcm_bytes()) == ""


def test_transcribe_initial_silence_timeout_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "InitialSilenceTimeout"})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    assert stt.transcribe(_pcm_bytes()) == ""


def test_transcribe_babble_timeout_returns_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "BabbleTimeout"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    assert stt.transcribe(_pcm_bytes()) == ""


# --- Gerçek arızalar: her biri CloudSpeechUnavailableError -----------------


def test_transcribe_error_status_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Error"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_unknown_recognition_status_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belgelenmemiş bir `RecognitionStatus` değeri de sessizce yutulmamalı."""

    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "SomethingNew"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_success_without_display_text_raises_cloud_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RecognitionStatus: Success` ama `DisplayText` alanı yoksa da dürüstçe hata verilmeli."""

    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_missing_recognition_status_key_raises_cloud_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"unexpected": "shape"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_timeout_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, exc=requests.exceptions.Timeout("zaman aşımı"))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_connection_error_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, exc=requests.exceptions.ConnectionError("bağlantı kurulamadı"))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_http_401_gives_clear_key_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(401, {"error": "invalid key"}))

    stt = AzureSpeechToText(api_key="gecersiz-anahtar", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())

    assert "anahtar" in str(exc_info.value).lower()


def test_transcribe_http_403_gives_clear_key_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(403, {"error": "missing key"}))

    stt = AzureSpeechToText(api_key="eksik-yetki", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())

    assert "anahtar" in str(exc_info.value).lower()


def test_transcribe_http_500_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(500, {"error": "server_error"}))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_malformed_json_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, json_raises=True))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


# --- Kısa/uzun ses kısayolları --------------------------------------------


def test_transcribe_empty_bytes_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    assert stt.transcribe(b"") == ""
    assert calls == []


def test_transcribe_too_short_audio_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    too_short = _pcm_bytes(duration=MIN_TRANSCRIBE_SECONDS / 2)
    assert stt.transcribe(too_short) == ""
    assert calls == []


def test_transcribe_audio_longer_than_60_seconds_raises_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    too_long = _pcm_bytes(duration=61.0)
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(too_long)

    assert "60" in str(exc_info.value)
    assert calls == []  # 60 saniye sınırı ağa çıkmadan ÖNCE kontrol edilmeli


def test_transcribe_audio_exactly_60_seconds_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sınır dahildir: tam 60 saniye reddedilmemeli, yalnızca ÜZERİ reddedilmeli."""

    _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "merhaba"})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    exactly_60 = _pcm_bytes(duration=60.0)
    assert stt.transcribe(exactly_60) == "merhaba"


# --- hotwords: kabul edilir ama isteğe hiç dahil edilmez -------------------


def test_transcribe_hotwords_ignored_and_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(
        monkeypatch, response=_FakeResponse(200, {"RecognitionStatus": "Success", "DisplayText": "..."})
    )

    stt = AzureSpeechToText(api_key="fake-key", region="westeurope")
    # Patlamamalı (arayüz uyumluluğu için parametre kabul edilir):
    text = stt.transcribe(_pcm_bytes(), hotwords="Artemis, Riot Client")

    assert text == "..."
    _, kwargs = calls[0]
    haystacks = [repr(kwargs.get("params")), repr(kwargs.get("headers")), repr(kwargs.get("data"))]
    for haystack in haystacks:
        assert "Artemis" not in haystack
        assert "Riot Client" not in haystack


# --- Anahtar hiçbir hata mesajında/log'da geçmemeli -----------------------


def test_api_key_never_appears_in_error_messages_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stt = AzureSpeechToText(api_key=_FAKE_SECRET_KEY, region="westeurope")
    messages: list[str] = []

    _install_fake_post(monkeypatch, exc=requests.exceptions.Timeout("zaman aşımı"))
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())
    messages.append(str(exc_info.value))

    _install_fake_post(monkeypatch, exc=requests.exceptions.ConnectionError("bağlantı yok"))
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())
    messages.append(str(exc_info.value))

    _install_fake_post(monkeypatch, response=_FakeResponse(401, {"error": "invalid"}))
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())
    messages.append(str(exc_info.value))

    _install_fake_post(monkeypatch, response=_FakeResponse(500, {"error": "server"}))
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())
    messages.append(str(exc_info.value))

    _install_fake_post(monkeypatch, response=_FakeResponse(200, json_raises=True))
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())
    messages.append(str(exc_info.value))

    for message in messages:
        assert _FAKE_SECRET_KEY not in message
    assert _FAKE_SECRET_KEY not in caplog.text
