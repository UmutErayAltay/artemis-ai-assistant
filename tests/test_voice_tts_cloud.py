"""`voice.tts_cloud.EdgeTextToSpeech` testleri.

Gerçek ağ ya da gerçek hoparlör GEREKTİRMEZ: `edge_tts`, `av` ve
`sounddevice` modüllerinin üçü de sahte modüllerle değiştirilir (bkz.
`tests/test_voice_tts.py`'deki piper/sounddevice deseni ve
`tests/test_llm_client.py`'deki ollama deseni). `EdgeTextToSpeech`
Piper'daki gibi bir model dosyasına bağımlı olmadığı için (API anahtarı
gerekmez, hiçbir yerel dosya doğrulanmaz) `__init__` testleri yok —
doğrudan `speak()`/`stop()` davranışına odaklanılır.
"""

from __future__ import annotations

import asyncio
import sys
import types

import numpy as np
import pytest

from voice.audio import SAMPLE_RATE
from voice.tts_cloud import CloudTextToSpeechUnavailableError, EdgeTextToSpeech

_ALIGNMENT_PADDING = b"\x7f\x7f" * 64
"""PyAV'ın düzlem tamponuna eklediği hizalama dolgusunu taklit eder.

Gerçek `frame.planes[0]` gerçek ses verisinden büyük olabilir; bu fazlalık
çalınırsa hoparlörden CIZIRTI duyulur (ölçüldü: 3.5 sn'lik bir cevapta
%5.6 çöp). Yüksek genlikli (0x7f7f) seçilmiştir ki sızması hâlinde
testlerde sessizce kaybolmayıp gözle görülür bir fark yaratsın.
"""

_EDGE_NATIVE_SAMPLE_RATE = 24_000
"""Edge TTS'in gerçekte ürettiği örnekleme hızı (ölçüldü).

Bilinçli olarak `voice.audio.SAMPLE_RATE`'ten (16 kHz) FARKLI seçildi:
16 kHz yalnızca Whisper'a GİRDİ verirken zorunludur, konuşma ÇIKTISI için
değil. Sesi 16 kHz'e indirmek bant genişliğinin üçte birini atıp sesi
"telefon gibi" yapıyordu; bu sabitin farklı olması, kodun hızı gerçekten
akıştan okuduğunu (sabit varsaymadığını) testlerle kanıtlar.
"""


def _tone_block(duration: float, amplitude: int = 10_000, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Belirtilen sürede sahte (gürültü) 16-bit PCM ses üretir.

    İçeriğin gerçek mp3/PCM olması önemli değildir: sahte `edge_tts` bunu
    "mp3 baytı" olarak üretir, sahte `av` da (gerçek codec çözmeden) aynen
    PCM'e "çözer" (bkz. `_make_fake_av_module`) — amaç yalnızca
    `EdgeTextToSpeech`'in bu baytları doğru sırada/boyutlarda
    `sounddevice`'a ilettiğini ve genlik hesaplamasının (`rms_amplitude`)
    sıfırdan farklı, 0-1 aralığında değerler ürettiğini doğrulamaktır.
    """

    frame_count = int(sample_rate * duration)
    rng = np.random.default_rng(7)
    samples = rng.integers(-amplitude, amplitude, size=frame_count, dtype=np.int16)
    return samples.tobytes()


def _make_fake_edge_tts_module(
    chunks: list[dict] | None = None, raise_error: Exception | None = None
) -> types.ModuleType:
    """`Communicate(text, voice, ...).stream()` çağrıldığında `chunks`'ı
    sırayla üreten (ya da `raise_error` verilmişse akış başlar başlamaz
    onu fırlatan) sahte `edge_tts` modülü.

    Her `Communicate(...)` çağrısı, testin argümanları doğrulayabilmesi
    için `module._communicate_calls`'a kaydedilir.
    """

    communicate_calls: list[dict] = []

    class _FakeCommunicate:
        def __init__(
            self,
            text,
            voice,
            *,
            connect_timeout=None,
            receive_timeout=None,
            rate=None,
            pitch=None,
            **kwargs,
        ) -> None:
            communicate_calls.append(
                {
                    "text": text,
                    "voice": voice,
                    "connect_timeout": connect_timeout,
                    "receive_timeout": receive_timeout,
                    "rate": rate,
                    "pitch": pitch,
                }
            )

        async def stream(self):
            if raise_error is not None:
                raise raise_error
            for chunk in chunks or []:
                yield chunk

    module = types.ModuleType("edge_tts")
    module.Communicate = _FakeCommunicate
    module._communicate_calls = communicate_calls  # test tarafından okunur
    return module


def _make_fake_av_module(corrupt_marker: bytes | None = None) -> types.ModuleType:
    """`av.open(...)`'ı, verilen bayt dizisini TEK bir "kare" olarak taşıyan
    sahte bir konteynıra çeviren; `av.AudioResampler`'ı ise bu kareyi
    (gerçek örnek/codec dönüşümü yapmadan) aynen geri veren sahte bir
    yeniden örnekleyiciye çeviren sahte `av` modülü.

    Gerçek mp3 çözme YOK: bu fake yalnızca `EdgeTextToSpeech._decode_mp3_to_pcm`'in
    `av.open`/`AudioResampler` API'sini DOĞRU sırayla/argümanlarla
    kullandığını doğrular — gerçek codec'in kendisi test edilmez (PyAV'ın
    işi). `corrupt_marker` verilirse ve açılan bayt dizisi bu değerle
    EŞİTSE `av.open`, bozuk veriyi simüle etmek için hata fırlatır.
    """

    open_calls: list[bytes] = []

    class _FakePlane:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def __bytes__(self) -> bytes:
            return self._data

    class _FakeOutFrame:
        """Çözülmüş bir ses karesi.

        `to_ndarray()` GERÇEK veriyi, `planes[0]` ise sonuna hizalama
        DOLGUSU eklenmiş hâlini döndürür — gerçek PyAV davranışı budur ve
        `EdgeTextToSpeech`'in `planes[0]` kullanmadığını kanıtlayan şey de
        bu farktır. Dolgu çalınırsa hoparlörden cızırtı duyulur; bu yüzden
        sahte nesne o tuzağı kasten kurar.
        """

        def __init__(self, data: bytes) -> None:
            self._data = data
            self.planes = [_FakePlane(data + _ALIGNMENT_PADDING)]

        def to_ndarray(self):
            return np.frombuffer(self._data, dtype=np.int16).reshape(1, -1)

    class _FakeFrame:
        def __init__(self, data: bytes) -> None:
            self.data = data

    class _FakeResampler:
        def __init__(self, format=None, layout=None, rate=None) -> None:
            self.format = format
            self.layout = layout
            self.rate = rate

        def resample(self, frame: _FakeFrame) -> list[_FakeOutFrame]:
            return [_FakeOutFrame(frame.data)]

    class _FakeCodecContext:
        """Gerçek `av` akışının örnekleme hızını taşıyan kısmı."""

        sample_rate = _EDGE_NATIVE_SAMPLE_RATE

    class _FakeAudioStream:
        codec_context = _FakeCodecContext()

    class _FakeStreams:
        audio = [_FakeAudioStream()]

    class _FakeContainer:
        """Sahte `av` konteyneri.

        `streams.audio[0].codec_context.sample_rate` gerçek `av` API'sini
        yansıtır: `EdgeTextToSpeech` çıktının hızını buradan okur ve sesi
        16 kHz'e DÜŞÜRMEDEN, kendi hızında çalar (bkz. `_decode_mp3_to_pcm`).
        """

        streams = _FakeStreams()

        def __init__(self, data: bytes) -> None:
            self._data = data

        def decode(self, audio: int = 0):
            yield _FakeFrame(self._data)

    def _fake_open(file_obj, **kwargs):
        data = file_obj.read()
        open_calls.append(data)
        if corrupt_marker is not None and data == corrupt_marker:
            raise ValueError("bozuk mp3 verisi (sahte av hatası)")
        return _FakeContainer(data)

    module = types.ModuleType("av")
    module.open = _fake_open
    module.AudioResampler = _FakeResampler
    module._open_calls = open_calls  # test tarafından okunur
    return module


def _make_fake_sounddevice_module(stop_after_n_writes: int | None = None, on_should_stop=None) -> types.ModuleType:
    """`RawOutputStream`'i taklit eden, tüm `write()` çağrılarını kaydeden
    sahte `sounddevice` modülü (bkz. `tests/test_voice_tts.py` — aynı desen).

    `stop_after_n_writes` verilirse, o kadar `write()` çağrısından sonra
    `on_should_stop()` tetiklenir — `EdgeTextToSpeech.stop()`'un çalma
    ortasında araya girmesini gerçek threading olmadan, deterministik
    biçimde simüle etmek için kullanılır.
    """

    instances: list[_FakeRawOutputStream] = []
    write_count = {"n": 0}

    class _FakeRawOutputStream:
        def __init__(self, samplerate=None, channels=None, dtype=None, latency=None, **kwargs) -> None:
            self.samplerate = samplerate
            self.channels = channels
            self.dtype = dtype
            self.latency = latency
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


# --- speak() ile blank metin -------------------------------------------


def test_speak_with_blank_text_never_calls_edge_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": b"x"}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    tts = EdgeTextToSpeech()
    tts.speak("   \n\t  ")  # yalnızca boşluk

    assert len(fake_edge._communicate_calls) == 0


# --- Başarılı akış -------------------------------------------------------


def test_speak_plays_synthesized_audio_and_reports_amplitude(monkeypatch: pytest.MonkeyPatch) -> None:
    audio_1 = _tone_block(0.3, amplitude=10_000)
    audio_2 = _tone_block(0.2, amplitude=8_000)
    fake_edge = _make_fake_edge_tts_module(
        chunks=[
            {"type": "audio", "data": audio_1},
            # gerçek edge_tts akışına meta olaylar da karışır; bunların
            # süzülüp yalnızca "audio" olanların kullanıldığı doğrulanmalı.
            {"type": "WordBoundary", "offset": 0, "duration": 1.0, "text": "merhaba"},
            {"type": "audio", "data": audio_2},
        ]
    )
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    fake_av = _make_fake_av_module()
    monkeypatch.setitem(sys.modules, "av", fake_av)

    fake_sd = _make_fake_sounddevice_module()
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    amplitudes: list[float] = []
    tts = EdgeTextToSpeech(timeout=10.0)
    tts.speak("Merhaba dünya, nasılsın?", on_amplitude=amplitudes.append)

    assert len(fake_edge._communicate_calls) == 1
    call = fake_edge._communicate_calls[0]
    assert call["text"] == "Merhaba dünya, nasılsın?"
    assert call["voice"] == "tr-TR-EmelNeural"  # varsayılan Türkçe kadın ses
    assert call["connect_timeout"] == 10
    assert call["receive_timeout"] == 10
    assert isinstance(call["connect_timeout"], int)  # edge_tts float'ı REDDEDER (TypeError)
    assert isinstance(call["receive_timeout"], int)

    # av.open'a yalnızca "audio" tipli parçaların baytları gitmeli (WordBoundary hariç)
    assert len(fake_av._open_calls) == 1
    assert fake_av._open_calls[0] == audio_1 + audio_2

    assert len(fake_sd._instances) == 1
    stream = fake_sd._instances[0]
    # Ses, akışın KENDİ hızında çalınmalı — 16 kHz'e düşürülmemeli.
    # 16 kHz yalnızca Whisper'a girdi verirken zorunludur; çıktıda bant
    # genişliğinin üçte birini atıp sesi "telefon gibi" yapıyordu.
    assert stream.samplerate == _EDGE_NATIVE_SAMPLE_RATE
    assert stream.samplerate != SAMPLE_RATE, "çıktı hızı girdi hızına sabitlenmiş"
    # Küçük tampon, yazmalar arasında iş yapıldığı için kekemeliğe yol açıyordu.
    assert stream.latency == "high"
    assert stream.started is True
    assert stream.stopped is True  # normal bitiş -> stop() (abort değil)
    assert stream.aborted is False
    assert stream.closed is True
    # sahte av gerçek codec çözmeden aynen geçirdiği için PCM == birleşik "mp3" baytı
    assert bytes(stream.written) == audio_1 + audio_2

    assert len(amplitudes) > 0
    assert all(0.0 <= a <= 1.0 for a in amplitudes)
    assert any(a > 0.0 for a in amplitudes)  # gürültü bloklarının genliği 0 olmamalı


def test_custom_voice_is_passed_to_communicate(monkeypatch: pytest.MonkeyPatch) -> None:
    audio = _tone_block(0.1)
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": audio}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setitem(sys.modules, "av", _make_fake_av_module())
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sounddevice_module())

    tts = EdgeTextToSpeech(voice="tr-TR-AhmetNeural")
    tts.speak("merhaba")

    assert fake_edge._communicate_calls[0]["voice"] == "tr-TR-AhmetNeural"


def test_default_rate_and_pitch_are_passed_to_communicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Varsayılan `rate`/`pitch` ("+0%"/"+0Hz" = değişiklik yok) da
    `Communicate`'e STRING olarak ulaşmalı — parametre eklenirken
    varsayılan davranış (sesin değişmemesi) sessizce bozulmamalı."""

    audio = _tone_block(0.1)
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": audio}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setitem(sys.modules, "av", _make_fake_av_module())
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sounddevice_module())

    tts = EdgeTextToSpeech()
    tts.speak("merhaba")

    call = fake_edge._communicate_calls[0]
    assert call["rate"] == "+0%"
    assert call["pitch"] == "+0Hz"
    assert isinstance(call["rate"], str)
    assert isinstance(call["pitch"], str)


def test_custom_rate_and_pitch_are_passed_to_communicate_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rate`/`pitch` sayıya çevrilmeden, verildiği gibi (STRING) iletilmeli.

    "Robotik" algısının yaygın sebebi fazla hızlı/düz okumadır; kullanıcının
    bunu denemesi için `-10%` gibi bir değerin bozulmadan `Communicate`'e
    ulaşması gerekir (`connect_timeout`/`receive_timeout`'un aksine, bunlar
    int'e ÇEVRİLMEMELİ)."""

    audio = _tone_block(0.1)
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": audio}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setitem(sys.modules, "av", _make_fake_av_module())
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sounddevice_module())

    tts = EdgeTextToSpeech(rate="-10%", pitch="-5Hz")
    tts.speak("merhaba")

    call = fake_edge._communicate_calls[0]
    assert call["rate"] == "-10%"
    assert call["pitch"] == "-5Hz"
    assert isinstance(call["rate"], str)
    assert isinstance(call["pitch"], str)


def test_timeout_is_truncated_to_int_for_communicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """`edge_tts.Communicate`, `connect_timeout`/`receive_timeout` için gerçekte
    `isinstance(x, int)` şartı koşar (float verilirse TypeError fırlatır) —
    bu yüzden saniye cinsinden float `timeout` mutlaka int'e çevrilmelidir."""

    audio = _tone_block(0.1)
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": audio}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setitem(sys.modules, "av", _make_fake_av_module())
    monkeypatch.setitem(sys.modules, "sounddevice", _make_fake_sounddevice_module())

    tts = EdgeTextToSpeech(timeout=7.9)
    tts.speak("merhaba")

    call = fake_edge._communicate_calls[0]
    assert call["connect_timeout"] == 7
    assert call["receive_timeout"] == 7
    assert isinstance(call["connect_timeout"], int)
    assert isinstance(call["receive_timeout"], int)


# --- Hata durumları: CloudTextToSpeechUnavailableError -------------------


def test_network_error_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    original_error = ConnectionError("bağlantı reddedildi")
    fake_edge = _make_fake_edge_tts_module(raise_error=original_error)
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    tts = EdgeTextToSpeech()
    with pytest.raises(CloudTextToSpeechUnavailableError) as excinfo:
        tts.speak("merhaba")

    assert excinfo.value.__cause__ is original_error  # "from exc" ile zincirlenmiş olmalı


def test_timeout_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    original_error = TimeoutError("zaman aşımı")
    fake_edge = _make_fake_edge_tts_module(raise_error=original_error)
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    tts = EdgeTextToSpeech()
    with pytest.raises(CloudTextToSpeechUnavailableError) as excinfo:
        tts.speak("merhaba")

    assert excinfo.value.__cause__ is original_error


def test_empty_audio_from_edge_tts_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`edge_tts` hiç "audio" tipli parça döndürmezse (yalnızca meta olaylar
    gelirse) mp3 baytı boş kalır; bu, `av` hiç çağrılmadan erken yakalanmalı."""

    fake_edge = _make_fake_edge_tts_module(
        chunks=[{"type": "WordBoundary", "offset": 0, "duration": 1.0, "text": "merhaba"}]
    )
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    tts = EdgeTextToSpeech()
    with pytest.raises(CloudTextToSpeechUnavailableError):
        tts.speak("merhaba")


def test_corrupt_mp3_raises_cloud_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    corrupt_bytes = b"boyle bir mp3 yok"
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": corrupt_bytes}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    fake_av = _make_fake_av_module(corrupt_marker=corrupt_bytes)
    monkeypatch.setitem(sys.modules, "av", fake_av)

    tts = EdgeTextToSpeech()
    with pytest.raises(CloudTextToSpeechUnavailableError) as excinfo:
        tts.speak("merhaba")

    assert isinstance(excinfo.value.__cause__, ValueError)


# --- stop() ---------------------------------------------------------------


def test_stop_aborts_playback_before_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    # _PLAYBACK_BLOCK_FRAMES (2000 örnek) sınırını aşan, birden çok
    # write() çağrısı gerektirecek kadar uzun bir ses.
    long_audio = _tone_block(1.0, amplitude=10_000)
    fake_edge = _make_fake_edge_tts_module(chunks=[{"type": "audio", "data": long_audio}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    monkeypatch.setitem(sys.modules, "av", _make_fake_av_module())

    tts = EdgeTextToSpeech()
    fake_sd = _make_fake_sounddevice_module(stop_after_n_writes=1, on_should_stop=tts.stop)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    tts.speak("çok uzun bir cümle söylüyorum ama kullanıcı beni durduracak")

    stream = fake_sd._instances[0]
    assert stream.aborted is True  # kesilince abort() ile HEMEN susmalı
    assert stream.stopped is False
    assert 0 < len(stream.written) < len(long_audio)  # tamamı çalınmadan kesildi


# --- asyncio.run() savunması ----------------------------------------------


def test_speak_inside_running_event_loop_raises_plain_runtime_error() -> None:
    """`asyncio.run()` zaten çalışan bir döngü içinden çağrılamaz; `speak()`
    bunu anlaşılır bir `RuntimeError` ile karşılamalı — ve bu, bir kullanım
    hatası olduğu için (bulutun erişilemezliğiyle ilgisi yok)
    `CloudTextToSpeechUnavailableError`'a SARILMAMALI (yönlendirici bunu
    yutup sessizce Piper'a düşerse gerçek hata gizlenir)."""

    tts = EdgeTextToSpeech()

    async def _call_speak_from_inside_a_running_loop() -> None:
        tts.speak("merhaba")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_call_speak_from_inside_a_running_loop())

    assert not isinstance(excinfo.value, CloudTextToSpeechUnavailableError)


def test_decode_uses_real_samples_not_padded_plane_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    """CIZIRTI REGRESYONU: çözme, hizalama dolgusunu sese KATMAMALI.

    Gerçek olay: `bytes(frame.planes[0])` kullanılıyordu. PyAV'ın düzlem
    tamponu hizalama nedeniyle gerçek veriden büyük olabiliyor ve fazlalık
    doğrudan hoparlöre gidip cızırtı olarak duyuluyordu. Ölçüm: 3.5
    saniyelik bir cevapta 9408 bayt (%5.6) çöp.

    Bu test, sahte karenin `planes[0]`'ına kasten dolgu koyar ve çözülmüş
    PCM'in TAM OLARAK gerçek veri kadar olduğunu doğrular; kod `planes[0]`'a
    geri dönerse test kırılır.
    """

    audio = _tone_block(0.05, sample_rate=_EDGE_NATIVE_SAMPLE_RATE)
    monkeypatch.setitem(sys.modules, "av", _make_fake_av_module())

    pcm, sample_rate = EdgeTextToSpeech._decode_mp3_to_pcm(audio)

    assert sample_rate == _EDGE_NATIVE_SAMPLE_RATE
    assert pcm == audio, "çözülen ses gerçek örneklerle birebir aynı olmalı"
    assert _ALIGNMENT_PADDING not in pcm, "hizalama dolgusu sese sızmış (cızırtı sebebi)"
