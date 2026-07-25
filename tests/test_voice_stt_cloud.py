"""`voice.stt_cloud.GroqSpeechToText` testleri.

Gerçek ağ isteği ASLA atılmaz: `requests.post`, her testte `monkeypatch`
ile sahte bir fonksiyonla değiştirilir — CLAUDE.md'deki "ollama modülünü
sahte bir modülle değiştir" deseninin (bkz. `tests/test_llm_client.py`,
`tests/test_ollama_manager.py`) HTTP eşleniği. Sentetik PCM üretimi
`tests/test_voice_stt.py`'daki yardımcı fonksiyonlarla aynı yaklaşımı
izler.
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
from voice.stt_cloud import CloudSpeechUnavailableError, GroqSpeechToText

_FAKE_SECRET_KEY = "gsk_test_super_secret_value_should_never_leak_12345"


def _pcm_bytes(duration: float = 1.0) -> bytes:
    """Belirtilen sürede (saniye) rastgele içerikli 16-bit PCM üretir.

    `GroqSpeechToText` (yerel `SpeechRecorder`'ın aksine) genliğe hiç
    bakmaz — burada tek önemli olan örnek SAYISI, süre eşiği hesabını
    (`MIN_TRANSCRIBE_SECONDS`) doğru tetiklemektir. İçerik bilinçli
    olarak rastgele seçilir ki testler yanlışlıkla "hep sıfır bir PCM
    de yeterli" gibi kör bir noktaya dayanmasın.
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

    `voice/stt_cloud.py` `requests`'i lazy import ettiği için (`import
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


# --- Anahtar yönetimi -------------------------------------------------


def test_constructor_does_not_raise_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    GroqSpeechToText()  # patlamamalı: anahtar doğrulaması yalnızca transcribe()'da olur


def test_transcribe_without_api_key_raises_clear_turkish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = GroqSpeechToText()
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())

    assert "GROQ_API_KEY" in str(exc_info.value)
    assert calls == []  # anahtar kontrolü ağ isteğinden ÖNCE yapılmalı


def test_transcribe_uses_env_var_when_api_key_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "env-key-123")
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(200, {"text": "merhaba"}))

    stt = GroqSpeechToText()
    text = stt.transcribe(_pcm_bytes())

    assert text == "merhaba"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer env-key-123"


# --- Başarılı yanıt ayrıştırma ------------------------------------------


def test_transcribe_parses_successful_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"text": "merhaba"}))

    stt = GroqSpeechToText(api_key="fake-key")
    text = stt.transcribe(_pcm_bytes())

    assert text == "merhaba"


def test_transcribe_sends_model_language_and_hotwords_as_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(200, {"text": "..."}))

    stt = GroqSpeechToText(api_key="fake-key", model="whisper-large-v3")
    stt.transcribe(_pcm_bytes(), hotwords="Artemis, Riot Client")

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["data"]["model"] == "whisper-large-v3"
    assert kwargs["data"]["language"] == "tr"
    assert kwargs["data"]["prompt"] == "Artemis, Riot Client"


def test_transcribe_sends_valid_wav_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, response=_FakeResponse(200, {"text": "..."}))

    stt = GroqSpeechToText(api_key="fake-key")
    stt.transcribe(_pcm_bytes(duration=1.0))

    _, kwargs = calls[0]
    filename, content, mime = kwargs["files"]["file"]
    assert filename == "audio.wav"
    assert mime == "audio/wav"

    with wave.open(BytesIO(content), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getframerate() == SAMPLE_RATE
        assert wav_file.getsampwidth() == 2


# --- Ağ / HTTP hataları ---------------------------------------------------


def test_transcribe_timeout_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, exc=requests.exceptions.Timeout("zaman aşımı"))

    stt = GroqSpeechToText(api_key="fake-key")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_connection_error_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, exc=requests.exceptions.ConnectionError("bağlantı kurulamadı"))

    stt = GroqSpeechToText(api_key="fake-key")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_http_401_gives_clear_key_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(401, {"error": "invalid_api_key"}))

    stt = GroqSpeechToText(api_key="gecersiz-anahtar")
    with pytest.raises(CloudSpeechUnavailableError) as exc_info:
        stt.transcribe(_pcm_bytes())

    assert "anahtar" in str(exc_info.value).lower()


def test_transcribe_http_500_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(500, {"error": "server_error"}))

    stt = GroqSpeechToText(api_key="fake-key")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_malformed_json_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_post(monkeypatch, response=_FakeResponse(200, json_raises=True))

    stt = GroqSpeechToText(api_key="fake-key")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


def test_transcribe_unexpected_json_shape_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yanıt geçerli JSON ama beklenen `{"text": ...}` şeklinde değilse de
    sessizce boş string dönmek yerine dürüstçe hata fırlatılmalı."""

    _install_fake_post(monkeypatch, response=_FakeResponse(200, {"unexpected": "shape"}))

    stt = GroqSpeechToText(api_key="fake-key")
    with pytest.raises(CloudSpeechUnavailableError):
        stt.transcribe(_pcm_bytes())


# --- Kısa ses kısayolu (ağa hiç gitmemeli) --------------------------------


def test_transcribe_empty_bytes_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = GroqSpeechToText(api_key="fake-key")
    assert stt.transcribe(b"") == ""
    assert calls == []


def test_transcribe_too_short_audio_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_post(monkeypatch, exc=AssertionError("ağa hiç istek atılmamalıydı"))

    stt = GroqSpeechToText(api_key="fake-key")
    too_short = _pcm_bytes(duration=MIN_TRANSCRIBE_SECONDS / 2)
    assert stt.transcribe(too_short) == ""
    assert calls == []


# --- Anahtar hiçbir hata mesajında/log'da geçmemeli -----------------------


def test_api_key_never_appears_in_error_messages_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stt = GroqSpeechToText(api_key=_FAKE_SECRET_KEY)
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
