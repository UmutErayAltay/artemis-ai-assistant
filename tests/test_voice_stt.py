"""`voice.stt.SpeechRecorder` ve `voice.stt.SpeechToText` testleri.

Gerçek mikrofon veya gerçek Whisper modeli GEREKTİRMEZ:
    - `SpeechRecorder` numpy ile üretilmiş sentetik PCM (sessizlik/"konuşma"
      gürültüsü) ile test edilir.
    - `SpeechToText`, `faster_whisper` modülü sahte bir modülle
      değiştirilerek (bkz. `tests/test_llm_client.py`'deki ollama deseni)
      test edilir; gerçek model asla indirilmez/yüklenmez.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from voice.audio import SAMPLE_RATE
from voice.stt import (
    MIN_TRANSCRIBE_SECONDS,
    SpeechRecognitionUnavailableError,
    SpeechRecorder,
    SpeechToText,
)


def _silence_block(duration: float = 0.125) -> bytes:
    """Belirtilen sürede sessizlik (sıfır genlik) PCM bloğu üretir."""

    frame_count = int(SAMPLE_RATE * duration)
    return np.zeros(frame_count, dtype=np.int16).tobytes()


def _speech_block(duration: float = 0.125, amplitude: int = 12_000) -> bytes:
    """Belirtilen sürede yüksek genlikli sahte "konuşma" gürültüsü üretir.

    `amplitude=12_000`, `voice.audio.rms_amplitude`'ın normal konuşma için
    kullandığı RMS tavanının (5000) belirgin biçimde üzerinde bir RMS'e
    karşılık gelir; bu yüzden varsayılan `silence_threshold` (0.06) ile
    kıyaslandığında rahatça "konuşma" sayılır.
    """

    frame_count = int(SAMPLE_RATE * duration)
    rng = np.random.default_rng(42)
    samples = rng.integers(-amplitude, amplitude, size=frame_count, dtype=np.int16)
    return samples.tobytes()


# --- SpeechRecorder -------------------------------------------------------


def test_feed_returns_false_while_speech_continues() -> None:
    recorder = SpeechRecorder(silence_timeout=1.0, max_duration=10.0)
    for _ in range(4):
        assert recorder.feed(_speech_block()) is False


def test_feed_ends_after_silence_following_speech() -> None:
    recorder = SpeechRecorder(silence_timeout=0.3, max_duration=10.0)
    assert recorder.feed(_speech_block()) is False

    finished = False
    for _ in range(10):
        finished = recorder.feed(_silence_block())
        if finished:
            break
    assert finished is True


def test_leading_silence_before_speech_does_not_end_recording() -> None:
    """Kaydın başındaki doğal sessizlik "konuşma bitti" sayılmamalı —
    konuşma hiç başlamadıysa `silence_timeout` hiç işlemeye başlamaz."""

    recorder = SpeechRecorder(silence_timeout=0.3, max_duration=10.0)
    for _ in range(20):
        assert recorder.feed(_silence_block()) is False


def test_feed_ends_at_max_duration_even_without_silence() -> None:
    recorder = SpeechRecorder(silence_timeout=100.0, max_duration=0.5)

    finished = False
    for _ in range(10):
        finished = recorder.feed(_speech_block())
        if finished:
            break
    assert finished is True


def test_audio_bytes_accumulates_fed_blocks() -> None:
    recorder = SpeechRecorder()
    block = _speech_block()
    recorder.feed(block)
    recorder.feed(block)
    assert recorder.audio_bytes == block + block


def test_reset_clears_buffer() -> None:
    recorder = SpeechRecorder()
    recorder.feed(_speech_block())
    recorder.reset()
    assert recorder.audio_bytes == b""


def test_reset_clears_internal_speech_and_silence_state() -> None:
    """`reset()` yalnızca arabelleği değil, konuşma/sessizlik sayaçlarını da
    temizlemeli — aksi halde reset öncesi biriken kısmi sessizlik, reset
    sonrası yeni sessizlikle toplanıp kaydı erken bitirirdi."""

    recorder = SpeechRecorder(silence_timeout=0.3, max_duration=10.0)
    recorder.feed(_speech_block())  # konuşma başladı
    recorder.feed(_silence_block())  # ~0.125 sn sessizlik biriktirdi (henüz bitmedi)
    recorder.reset()

    # reset sonrası: konuşma hiç başlamamış gibi davranmalı, yalnızca
    # sessizlik beslemek kaydı bitirmemeli (5 blok = ~0.625 sn > 0.3 sn;
    # state doğru temizlenmeseydi bu erken biterdi).
    for _ in range(5):
        assert recorder.feed(_silence_block()) is False


def test_default_thresholds_end_recording_after_default_silence_timeout() -> None:
    """Varsayılan değerlerle (silence_timeout=1.2 sn) uçtan uca akış."""

    recorder = SpeechRecorder()
    assert recorder.feed(_speech_block(duration=0.5)) is False

    finished = False
    for _ in range(20):
        finished = recorder.feed(_silence_block(duration=0.125))
        if finished:
            break
    assert finished is True


# --- SpeechToText -----------------------------------------------------


class _ExplodingWhisperModel:
    """Yüklenmemesi gereken durumları yakalamak için: oluşturulursa patlar."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("Whisper modeli bu senaryoda hiç yüklenmemeliydi.")


def test_transcribe_empty_bytes_returns_empty_without_loading_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _ExplodingWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stt = SpeechToText()
    assert stt.transcribe(b"") == ""


def test_transcribe_too_short_audio_returns_empty_without_loading_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _ExplodingWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stt = SpeechToText()
    too_short = _silence_block(duration=MIN_TRANSCRIBE_SECONDS / 2)
    assert stt.transcribe(too_short) == ""


def test_transcribe_forces_turkish_and_joins_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    received = {}

    class _FakeSegment:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeWhisperModel:
        def __init__(self, model_size, device=None, compute_type=None, **kwargs) -> None:
            received["model_size"] = model_size
            received["device"] = device
            received["compute_type"] = compute_type

        def transcribe(self, audio, language=None, **kwargs):
            received["language"] = language
            received["audio_dtype"] = audio.dtype
            received["audio_range_ok"] = bool(audio.min() >= -1.0 and audio.max() <= 1.0)
            segments = [_FakeSegment("Merhaba"), _FakeSegment(" dünya.")]
            info = types.SimpleNamespace(language="tr", language_probability=1.0)
            return iter(segments), info

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stt = SpeechToText(model_size="small", compute_type="int8", device="cpu")
    text = stt.transcribe(_speech_block(duration=1.0))

    assert text == "Merhaba dünya."
    assert received["language"] == "tr"
    assert received["model_size"] == "small"
    assert received["device"] == "cpu"
    assert received["compute_type"] == "int8"
    assert received["audio_range_ok"] is True


def test_model_is_loaded_only_once_across_multiple_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    load_count = {"n": 0}

    class _FakeSegment:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            load_count["n"] += 1

        def transcribe(self, audio, language=None, **kwargs):
            return iter([_FakeSegment("merhaba")]), types.SimpleNamespace()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stt = SpeechToText()
    stt.transcribe(_speech_block(duration=1.0))
    stt.transcribe(_speech_block(duration=1.0))
    assert load_count["n"] == 1


def test_model_load_failure_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("ağ hatası")

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FailingWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stt = SpeechToText()
    with pytest.raises(SpeechRecognitionUnavailableError):
        stt.transcribe(_speech_block(duration=1.0))
