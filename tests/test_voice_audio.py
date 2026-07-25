"""`voice/audio.py` için testler: `rms_amplitude` ve `MicrophoneStream`.

Hiçbir test gerçek mikrofon donanımına ihtiyaç duymaz:
    - `rms_amplitude` saf bir fonksiyondur; `numpy` ile sentetik PCM
      bloklar üretip doğrudan çağırıyoruz.
    - `MicrophoneStream`, `sounddevice` modülünü yalnızca `__enter__`
      içinde (lazy) import ettiği için, `sys.modules["sounddevice"]`'ı
      `monkeypatch.setitem` ile sahte bir modülle değiştirip cihaza hiç
      dokunmadan `start()/stop()/close()` çağrılarını gözlemleyebiliyoruz
      (bkz. `tests/test_llm_client.py`'deki `fake_ollama` deseni).
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from voice.audio import MicrophoneStream, MicrophoneUnavailableError, SAMPLE_RATE, rms_amplitude

# --------------------------------------------------------------------------
# rms_amplitude
# --------------------------------------------------------------------------


def _silence_block(num_samples: int = 1_600) -> bytes:
    """Tamamen sıfır dolu (sessiz) bir int16 PCM bloğu."""

    return np.zeros(num_samples, dtype=np.int16).tobytes()


def _sine_block(amplitude: int, num_samples: int = 1_600, frequency: float = 440.0) -> bytes:
    """Belirli genlikte sentetik bir sinüs dalgası PCM bloğu üretir."""

    t = np.arange(num_samples) / SAMPLE_RATE
    wave = amplitude * np.sin(2 * np.pi * frequency * t)
    return wave.astype(np.int16).tobytes()


def _full_scale_block(num_samples: int = 1_600) -> bytes:
    """int16'nın tepe değeriyle (32767) dolu bir blok — mümkün olan en yüksek genlik."""

    return np.full(num_samples, 32_767, dtype=np.int16).tobytes()


def test_rms_amplitude_is_zero_for_full_silence() -> None:
    assert rms_amplitude(_silence_block()) == 0.0


def test_rms_amplitude_is_zero_for_empty_bytes() -> None:
    # Boş girdi çökmemeli; örn. akış henüz veri üretmeden çağrılırsa olabilir.
    assert rms_amplitude(b"") == 0.0


def test_rms_amplitude_is_clearly_larger_for_loud_sound_than_silence() -> None:
    loud = rms_amplitude(_sine_block(amplitude=8_000))
    silent = rms_amplitude(_silence_block())

    assert loud > silent
    assert loud > 0.5  # sessizlikle karışmayacak kadar belirgin biçimde büyük


def test_rms_amplitude_never_exceeds_one_even_at_full_scale() -> None:
    value = rms_amplitude(_full_scale_block())

    assert value <= 1.0
    assert value == pytest.approx(1.0)  # tam tepe değer normalize üst sınıra tam oturur


def test_rms_amplitude_does_not_decrease_when_volume_doubles() -> None:
    quiet = rms_amplitude(_sine_block(amplitude=500))
    loud = rms_amplitude(_sine_block(amplitude=1_000))

    assert loud >= quiet
    assert loud > quiet  # bu genlik aralığında doygunluk yok, gerçekten artmalı


# --------------------------------------------------------------------------
# MicrophoneStream
# --------------------------------------------------------------------------


class _FakeRawInputStream:
    """`sounddevice.RawInputStream`'in donanımsız test için sahte sürümü.

    Gerçek sınıfın arayüzünü (start/stop/close/read) taklit eder ve
    metotların kaç kez çağrıldığını sayar; testler böylece "mikrofon
    gerçekten kapatıldı mı" sorusunu gözlemlenen davranışa bakarak
    doğrulayabilir.
    """

    def __init__(
        self,
        *,
        samplerate: int,
        blocksize: int,
        device: object,
        dtype: str,
        channels: int,
        fail_on_start: bool = False,
    ) -> None:
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.device = device
        self.dtype = dtype
        self.channels = channels
        self._fail_on_start = fail_on_start
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        if self._fail_on_start:
            raise RuntimeError("sahte sürücü hatası: cihaz meşgul")
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def read(self, frames: int) -> tuple[bytes, bool]:
        return b"\x00\x00" * frames, False


def _install_fake_sounddevice(
    monkeypatch: pytest.MonkeyPatch, *, fail_on_start: bool = False
) -> list[_FakeRawInputStream]:
    """`sys.modules["sounddevice"]`'ı sahte bir modülle değiştirir.

    `MicrophoneStream.__enter__` içindeki `import sounddevice as sd` bu
    sahte modülü bulur; gerçek mikrofon donanımına hiç dokunulmaz.
    Döndürülen liste, oluşturulan her sahte akışı (çağrı sırasıyla) tutar.
    """

    created: list[_FakeRawInputStream] = []

    def _factory(**kwargs: object) -> _FakeRawInputStream:
        stream = _FakeRawInputStream(fail_on_start=fail_on_start, **kwargs)
        created.append(stream)
        return stream

    fake_module = types.ModuleType("sounddevice")
    fake_module.RawInputStream = _factory
    monkeypatch.setitem(sys.modules, "sounddevice", fake_module)
    return created


def test_context_manager_stops_and_closes_stream_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _install_fake_sounddevice(monkeypatch)

    with MicrophoneStream():
        pass

    assert len(created) == 1
    assert created[0].start_calls == 1
    assert created[0].stop_calls == 1
    assert created[0].close_calls == 1


def test_context_manager_closes_stream_even_if_exception_raised_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _install_fake_sounddevice(monkeypatch)

    with pytest.raises(ValueError):
        with MicrophoneStream():
            raise ValueError("blok işlenirken beklenmedik hata")

    # Mikrofon açık kalmamalı: istisna fırlasa bile stop()/close() çağrılmış olmalı.
    assert created[0].stop_calls == 1
    assert created[0].close_calls == 1


def test_read_block_raises_when_stream_is_not_open() -> None:
    mic = MicrophoneStream()

    with pytest.raises(MicrophoneUnavailableError):
        mic.read_block()


def test_enter_wraps_device_failure_in_microphone_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sounddevice(monkeypatch, fail_on_start=True)

    with pytest.raises(MicrophoneUnavailableError):
        with MicrophoneStream():
            pass


def test_close_called_twice_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_fake_sounddevice(monkeypatch)

    mic = MicrophoneStream()
    mic.__enter__()
    mic.close()
    mic.close()  # ikinci çağrı patlamamalı

    assert created[0].close_calls == 1  # alttaki akışın close()'u yalnızca bir kez tetiklendi


def test_close_without_ever_entering_is_safe() -> None:
    # Hiç açılmamış bir akışta close() çağrısı da güvenli olmalı.
    mic = MicrophoneStream()
    mic.close()
    mic.close()
