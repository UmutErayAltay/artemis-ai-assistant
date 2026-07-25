"""`voice.tts.TextToSpeech` testleri.

Gerçek Piper modeli veya gerçek hoparlör GEREKTİRMEZ: `piper` ve
`sounddevice` modülleri sahte modüllerle değiştirilir (bkz.
`tests/test_llm_client.py`'deki ollama deseni). Model dosyası varlık
kontrolü ise gerçek `tmp_path` dosya sistemiyle test edilir.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from voice.tts import TextToSpeech, TextToSpeechModelMissingError

_PLAYBACK_SAMPLE_RATE = 16_000


def _tone_block(duration: float, amplitude: int = 10_000, sample_rate: int = _PLAYBACK_SAMPLE_RATE) -> bytes:
    """Belirtilen sürede sahte (gürültü) 16-bit PCM ses üretir."""

    frame_count = int(sample_rate * duration)
    rng = np.random.default_rng(7)
    samples = rng.integers(-amplitude, amplitude, size=frame_count, dtype=np.int16)
    return samples.tobytes()


def _write_fake_model_files(tmp_path: Path) -> tuple[Path, Path]:
    """Var olması yeterli sahte `.onnx` + `.onnx.json` dosyaları oluşturur.

    İçerik önemsizdir: `TextToSpeech.__init__` yalnızca dosyaların VAR
    OLDUĞUNU kontrol eder; gerçek yükleme aşağıdaki testlerde `piper`
    sahte modülüyle yapılır ve dosya içeriği hiç okunmaz.
    """

    model_path = tmp_path / "tr_TR-test-medium.onnx"
    config_path = tmp_path / "tr_TR-test-medium.onnx.json"
    model_path.write_bytes(b"")
    config_path.write_text("{}", encoding="utf-8")
    return model_path, config_path


def _make_fake_piper_module(sample_rate: int, sentence_chunks: list[bytes]) -> types.ModuleType:
    """`PiperVoice.load(...)` çağrıldığında `sentence_chunks`'ı sırayla
    `AudioChunk` benzeri nesneler olarak "sentezleyen" sahte `piper` modülü."""

    load_calls: list[dict] = []

    class _FakeAudioChunk:
        def __init__(self, pcm_bytes: bytes) -> None:
            self.audio_int16_bytes = pcm_bytes

    class _FakeConfig:
        def __init__(self, rate: int) -> None:
            self.sample_rate = rate

    class _FakeVoice:
        def __init__(self, rate: int, chunks: list[bytes]) -> None:
            self.config = _FakeConfig(rate)
            self._chunks = chunks

        def synthesize(self, text: str):
            for chunk_bytes in self._chunks:
                yield _FakeAudioChunk(chunk_bytes)

    class _FakePiperVoice:
        @staticmethod
        def load(model_path, config_path=None, **kwargs):
            load_calls.append({"model_path": model_path, "config_path": config_path})
            return _FakeVoice(sample_rate, sentence_chunks)

    module = types.ModuleType("piper")
    module.PiperVoice = _FakePiperVoice
    module._load_calls = load_calls  # test tarafından okunur
    return module


def _make_fake_sounddevice_module(stop_after_n_writes: int | None = None, on_should_stop=None) -> types.ModuleType:
    """`RawOutputStream`'i taklit eden, tüm `write()` çağrılarını
    kaydeden sahte `sounddevice` modülü.

    `stop_after_n_writes` verilirse, o kadar `write()` çağrısından sonra
    `on_should_stop()` tetiklenir — `TextToSpeech.stop()`'un çalma
    ortasında araya girmesini gerçek threading olmadan, deterministik
    biçimde simüle etmek için kullanılır.
    """

    instances: list[_FakeRawOutputStream] = []
    write_count = {"n": 0}

    class _FakeRawOutputStream:
        def __init__(self, samplerate=None, channels=None, dtype=None, **kwargs) -> None:
            self.samplerate = samplerate
            self.channels = channels
            self.dtype = dtype
            self.written = bytearray()
            self.started = False
            self.stopped = False
            self.aborted = False
            self.closed = False
            instances.append(self)

        def start(self) -> None:
            self.started = True

        def write(self, data) -> None:
            self.written += bytes(data)
            write_count["n"] += 1
            if stop_after_n_writes is not None and write_count["n"] == stop_after_n_writes and on_should_stop:
                on_should_stop()

        def stop(self) -> None:
            self.stopped = True

        def abort(self) -> None:
            self.aborted = True

        def close(self) -> None:
            self.closed = True

    module = types.ModuleType("sounddevice")
    module.RawOutputStream = _FakeRawOutputStream
    module._instances = instances  # test tarafından okunur
    return module


# --- Model dosyası kontrolü ------------------------------------------------


def test_missing_onnx_file_raises_model_missing_error(tmp_path: Path) -> None:
    missing_model = tmp_path / "does-not-exist.onnx"
    with pytest.raises(TextToSpeechModelMissingError):
        TextToSpeech(missing_model)


def test_missing_config_json_raises_model_missing_error(tmp_path: Path) -> None:
    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"")  # .onnx var ama yanındaki .onnx.json yok
    with pytest.raises(TextToSpeechModelMissingError):
        TextToSpeech(model_path)


# --- speak() davranışı ------------------------------------------------


def test_speak_with_blank_text_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_path, _ = _write_fake_model_files(tmp_path)

    class _ExplodingPiperVoice:
        @staticmethod
        def load(*args, **kwargs):
            raise AssertionError("Boş/beyaz-boşluk metinde ses modeli hiç yüklenmemeli")

    fake_piper = types.ModuleType("piper")
    fake_piper.PiperVoice = _ExplodingPiperVoice
    monkeypatch.setitem(sys.modules, "piper", fake_piper)

    tts = TextToSpeech(model_path)
    tts.speak("   \n\t  ")  # yalnızca boşluk


def test_speak_plays_synthesized_audio_and_reports_amplitude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path, _ = _write_fake_model_files(tmp_path)

    sentence_1 = _tone_block(0.3, amplitude=10_000)
    sentence_2 = _tone_block(0.2, amplitude=8_000)
    fake_piper = _make_fake_piper_module(sample_rate=22_050, sentence_chunks=[sentence_1, sentence_2])
    monkeypatch.setitem(sys.modules, "piper", fake_piper)

    fake_sd = _make_fake_sounddevice_module()
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    amplitudes: list[float] = []
    tts = TextToSpeech(model_path)
    tts.speak("Merhaba dünya, nasılsın?", on_amplitude=amplitudes.append)

    assert len(fake_sd._instances) == 1
    stream = fake_sd._instances[0]

    # Piper'ın kendi örnekleme hızı kullanılmalı (voice.audio.SAMPLE_RATE
    # olan 16 kHz'e sabitlenmemeli) — bkz. tts.py modül dokümantasyonu.
    assert stream.samplerate == 22_050
    assert stream.started is True
    assert stream.stopped is True  # normal bitiş -> stop() (abort değil)
    assert stream.aborted is False
    assert stream.closed is True
    assert bytes(stream.written) == sentence_1 + sentence_2

    assert len(amplitudes) > 0
    assert all(0.0 <= a <= 1.0 for a in amplitudes)
    assert any(a > 0.0 for a in amplitudes)  # gürültü bloklarının genliği 0 olmamalı


def test_stop_aborts_playback_before_it_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_path, _ = _write_fake_model_files(tmp_path)

    # _PLAYBACK_BLOCK_FRAMES (2000 örnek) sınırını aşan, birden çok
    # write() çağrısı gerektirecek kadar uzun bir "cümle".
    long_sentence = _tone_block(1.0, amplitude=10_000)
    fake_piper = _make_fake_piper_module(sample_rate=_PLAYBACK_SAMPLE_RATE, sentence_chunks=[long_sentence])
    monkeypatch.setitem(sys.modules, "piper", fake_piper)

    tts = TextToSpeech(model_path)
    fake_sd = _make_fake_sounddevice_module(stop_after_n_writes=1, on_should_stop=tts.stop)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    tts.speak("çok uzun bir cümle söylüyorum ama kullanıcı beni durduracak")

    stream = fake_sd._instances[0]
    assert stream.aborted is True  # kesilince abort() ile HEMEN susmalı
    assert stream.stopped is False
    assert 0 < len(stream.written) < len(long_sentence)  # tamamı çalınmadan kesildi


def test_voice_is_loaded_only_once_across_multiple_speak_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path, config_path = _write_fake_model_files(tmp_path)

    chunk = _tone_block(0.05)
    fake_piper = _make_fake_piper_module(sample_rate=_PLAYBACK_SAMPLE_RATE, sentence_chunks=[chunk])
    monkeypatch.setitem(sys.modules, "piper", fake_piper)

    fake_sd = _make_fake_sounddevice_module()
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    tts = TextToSpeech(model_path)
    tts.speak("bir")
    tts.speak("iki")

    assert len(fake_piper._load_calls) == 1
    assert fake_piper._load_calls[0]["model_path"] == str(model_path)
    assert fake_piper._load_calls[0]["config_path"] == str(config_path)
    assert len(fake_sd._instances) == 2  # her speak() kendi stream'ini açar/kapatır
