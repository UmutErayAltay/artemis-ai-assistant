"""`voice/wake_word.py` için testler: `WakeWordDetector`.

Gerçek mikrofon veya gerçek Whisper modeli GEREKTİRMEZ:
    - `normalize_turkish()` ve `matches()` saf metin fonksiyonlarıdır;
      hiçbir ses/model katmanına dokunmaz, doğrudan test edilir.
    - Enerji kapısı (`feed()`'in "konuşma var mı?" kısmı) numpy ile
      üretilmiş sentetik PCM (sessizlik/"konuşma" gürültüsü) ile test
      edilir — `voice.audio.rms_amplitude` gerçek bir fonksiyondur, mock'a
      gerek yoktur.
    - Whisper'a gerçekten gidecek yol (`_recognize`), `faster_whisper`
      modülü sahte bir modülle değiştirilerek test edilir (bkz.
      `tests/test_voice_stt.py`'deki aynı desen); gerçek model asla
      indirilmez/yüklenmez.

Eski Vosk tabanlı tasarımda `__init__` bir model KLASÖRÜNÜN varlığını
kontrol ediyordu (`WakeWordModelMissingError`). Yeni tasarımda model
kavramı (Whisper) tamamen LAZY'dir: nesne oluşturmak hiçbir şey
yüklemez, yükleme yalnızca biriken ses gerçekten bir tanıma denemesine
gönderildiğinde (`_ensure_model`) tetiklenir. Bu dosyadaki
`test_construction_never_touches_whisper` ve
`test_model_load_failure_raises_wake_word_unavailable_error` testleri,
eski model-klasörü testlerinin bu yeni tasarıma uyarlanmış halidir.
"""

from __future__ import annotations

import difflib
import sys
import types

import numpy as np
import pytest

from voice.audio import SAMPLE_RATE
from voice.wake_word import (
    DEFAULT_FUZZY_THRESHOLD,
    DEFAULT_WAKE_WORDS,
    WakeWordDetector,
    WakeWordUnavailableError,
    normalize_turkish,
)


# --------------------------------------------------------------------------
# Sentetik PCM üretimi (bkz. tests/test_voice_stt.py'deki aynı desen)
# --------------------------------------------------------------------------


def _silence_block(duration: float = 0.125) -> bytes:
    """Belirtilen sürede sessizlik (sıfır genlik) PCM bloğu üretir."""

    frame_count = int(SAMPLE_RATE * duration)
    return np.zeros(frame_count, dtype=np.int16).tobytes()


def _speech_block(duration: float = 0.125, amplitude: int = 12_000) -> bytes:
    """Belirtilen sürede yüksek genlikli sahte "konuşma" gürültüsü üretir.

    `amplitude=12_000`, `voice.audio.rms_amplitude`'ın normal konuşma için
    kullandığı RMS tavanının (5000) belirgin biçimde üzerinde bir RMS'e
    karşılık gelir; bu yüzden varsayılan `energy_threshold` (0.06) ile
    kıyaslandığında rahatça "konuşma" sayılır.
    """

    frame_count = int(SAMPLE_RATE * duration)
    rng = np.random.default_rng(42)
    samples = rng.integers(-amplitude, amplitude, size=frame_count, dtype=np.int16)
    return samples.tobytes()


# --------------------------------------------------------------------------
# Sahte faster_whisper modülü (bkz. tests/test_voice_stt.py'deki aynı desen)
# --------------------------------------------------------------------------


class _FakeSegment:
    """Gerçek faster_whisper Segment nesnesinin yerini tutar (yalnızca `.text`)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    """`faster_whisper.WhisperModel`'in gözlemlenebilir sahte sürümü.

    Her `transcribe()` çağrısını (verilen sesin örnek sayısı, `language`,
    `hotwords` dahil) `env.transcribe_calls`'a kaydeder; testler böylece
    "Whisper hiç çağrılmadı mı" ve "ön-tampon gerçekten sese dahil oldu
    mu" sorularını gözlemlenen davranışa bakarak doğrulayabilir.
    """

    def __init__(self, env: "_FakeWhisperEnvironment") -> None:
        self._env = env

    def transcribe(self, audio_array, language=None, hotwords=None, **kwargs):
        self._env.transcribe_calls.append(
            {
                "audio_length": len(audio_array),
                "language": language,
                "hotwords": hotwords,
            }
        )
        return [_FakeSegment(self._env.next_text)], None


class _FakeWhisperEnvironment:
    """Sahte `faster_whisper` modülünü kuran ve çağrı kayıtlarını taşıyan yardımcı.

    `WakeWordDetector._ensure_model()` içindeki `from faster_whisper import
    WhisperModel` çağrısı, `monkeypatch.setitem(sys.modules, ...)` sayesinde
    burada üretilen sahte modülü bulur; gerçek model asla indirilmez.
    """

    def __init__(self) -> None:
        self.next_text = ""
        self.model_load_calls: list[dict[str, object]] = []
        self.transcribe_calls: list[dict[str, object]] = []

    @property
    def call_count(self) -> int:
        return len(self.transcribe_calls)

    def _make_model(self, model_size, compute_type: str = "int8", **kwargs) -> _FakeWhisperModel:
        self.model_load_calls.append({"model_size": model_size, "compute_type": compute_type})
        return _FakeWhisperModel(self)


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> _FakeWhisperEnvironment:
    env = _FakeWhisperEnvironment()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = env._make_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    return env


@pytest.fixture
def detector() -> WakeWordDetector:
    """`matches()` testleri için: Whisper'a hiç dokunmayan düz bir örnek."""

    return WakeWordDetector()


# --------------------------------------------------------------------------
# __init__ — artık model klasörü YOK, tamamen lazy (eski model_path testlerinin uyarlanmışı)
# --------------------------------------------------------------------------


def test_construction_never_touches_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nesne oluşturmak hiçbir model yüklemesi tetiklememeli (tam lazy).

    Eski tasarımda `__init__` model KLASÖRÜNÜN varlığını kontrol ediyordu
    (`WakeWordModelMissingError`). Yeni tasarımda model_path kavramı
    tamamen kalktı; bunun yerini alan garanti şu: yükleme ilk gerçek
    tanıma denemesine kadar hiç tetiklenmez. Bunu kanıtlamak için
    `faster_whisper` burada BİLEREK `None` ile değiştiriliyor —
    `sys.modules["faster_whisper"] = None` Python'da "bu importu deneme,
    doğrudan ImportError fırlat" anlamına gelir; nesne oluşturma yine de
    patlamazsa modül gerçekten hiç import edilmemiş demektir.
    """

    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    WakeWordDetector()  # patlamamalı
    WakeWordDetector(wake_words=["jarvis"], model_size="base")  # patlamamalı


def test_model_load_failure_raises_wake_word_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whisper modeli yüklenemezse anlaşılır bir Türkçe hata fırlatılmalı.

    Eski tasarımda bu hata (`WakeWordModelMissingError`) nesne oluşturulurken
    fırlatılırdı. Yeni tasarımda model LAZY yüklendiği için hata artık
    ancak biriken ses gerçekten bir tanıma denemesine gönderildiğinde ortaya
    çıkar (bkz. `voice/stt.py`'deki aynı desen).
    """

    class _FailingWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("ağ hatası")

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FailingWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    # silence_blocks=1: konuşmadan sonra TEK bir sessiz blok tanımayı tetikler.
    wake_detector = WakeWordDetector(silence_blocks=1, max_buffer_seconds=5.0)
    wake_detector.feed(_speech_block())

    with pytest.raises(WakeWordUnavailableError) as exc_info:
        wake_detector.feed(_silence_block())

    assert "setup_voice.py" in str(exc_info.value)


# --------------------------------------------------------------------------
# matches() — doğrudan alt-dizge ve boş/alakasız girdi
# --------------------------------------------------------------------------


def test_matches_exact_wake_word(detector: WakeWordDetector) -> None:
    assert detector.matches("artemis") is True


def test_matches_wake_word_as_substring_within_sentence(detector: WakeWordDetector) -> None:
    assert detector.matches("artemis masaüstünü aç") is True


def test_matches_empty_string_is_false(detector: WakeWordDetector) -> None:
    assert detector.matches("") is False


def test_matches_unrelated_sentence_is_false(detector: WakeWordDetector) -> None:
    assert detector.matches("bugün hava çok güzel") is False


# --------------------------------------------------------------------------
# matches() — bilinen varyantlar (DEFAULT_WAKE_WORDS içinde, alt-dizge yoluyla)
# --------------------------------------------------------------------------


def test_matches_known_variants(detector: WakeWordDetector) -> None:
    assert detector.matches("artemiz") is True
    assert detector.matches("arte mis") is True


# --------------------------------------------------------------------------
# matches() — bulanık (fuzzy) eşleşme
# --------------------------------------------------------------------------
#
# Eşiğin (0.82) hangi girdilerde tutup tutmadığını TAHMİN ETMİYORUZ; her
# iki testte de gerçek oranı `difflib.SequenceMatcher` ile burada hesaplayıp
# assert ediyoruz, sonra `matches()`'in gerçekten aynı sonuca vardığını
# doğruluyoruz. Böylece eşik veya varsayılan sözcük listesi ileride
# değişirse test sessizce yanlış bir varsayıma dayanmış olmaz.


def test_fuzzy_match_accepts_close_misspelling_not_in_default_list(
    detector: WakeWordDetector,
) -> None:
    # "artemus": "artemis"in tek harf değişikliğiyle türetilmiş, listede
    # YOK — yalnızca bulanık eşleşme yoluyla kabul edilmeli.
    candidate = "artemus"
    assert candidate not in DEFAULT_WAKE_WORDS

    best_ratio = max(
        difflib.SequenceMatcher(None, candidate, wake_word).ratio() for wake_word in DEFAULT_WAKE_WORDS
    )
    assert best_ratio >= DEFAULT_FUZZY_THRESHOLD  # seçilen aday gerçekten eşiği geçiyor

    assert detector.matches(candidate) is True


def test_fuzzy_match_rejects_dissimilar_word_below_threshold(detector: WakeWordDetector) -> None:
    candidate = "otobüs"

    best_ratio = max(
        difflib.SequenceMatcher(None, candidate, wake_word).ratio() for wake_word in DEFAULT_WAKE_WORDS
    )
    assert best_ratio < DEFAULT_FUZZY_THRESHOLD  # eşiğin gerçekten ALTINDA kaldığını doğrula

    assert detector.matches(candidate) is False


# --------------------------------------------------------------------------
# matches() — büyük/küçük harf, Türkçe 'İ' özel durumu
# --------------------------------------------------------------------------


def test_python_lower_alone_would_break_turkish_dotted_i() -> None:
    """Neden `normalize_turkish()` var: Python'un `.lower()`'ı tek başına yetmez.

    `"İ".lower()` düz `'i'` üretmez; `'i'` + BİRLEŞEN NOKTA (U+0307)
    üretir. Yani `"ARTEMİS".lower()` 8 karakterdir ve `"artemis"`e EŞİT
    DEĞİLDİR. Bu test, düzeltmenin gerekçesini (dış dünyanın davranışını)
    sabitler — regresyon olursa `normalize_turkish` gereksiz sanılıp
    kaldırılmasın.
    """

    naive = "ARTEMİS".lower()

    assert naive != "artemis"
    assert "artemis" not in naive
    assert len(naive) == 8  # 7 harf + birleşen nokta


@pytest.mark.parametrize(
    "written",
    ["artemis", "Artemis", "ARTEMIS", "ARTEMİS", "artemıs", "ArTeMiS"],
)
def test_matches_turkish_casing_via_direct_path_not_fuzzy_luck(
    detector: WakeWordDetector, written: str
) -> None:
    """Türkçe büyük/küçük harf varyantları DOĞRUDAN alt-dizge yoluyla eşleşmeli.

    Kritik nokta sadece `matches()`'in True dönmesi DEĞİL; bunu bulanık
    eşleşmenin şansına kalmadan yapması. Eskiden `"ARTEMİS"` yalnızca
    fuzzy yedeği (oran ~0.93) sayesinde yakalanıyordu; eşik yükseltilse
    sessizce bozulurdu. `normalize_turkish()` bunu doğrudan yola taşıdı,
    bu test de orada kalmasını garanti eder.
    """

    normalized = normalize_turkish(written).strip()

    # Bulanık katmanı hiç devreye sokmadan, alt-dizge yolu tek başına yetmeli.
    assert any(wake_word in normalized for wake_word in DEFAULT_WAKE_WORDS)
    assert detector.matches(written) is True


# --------------------------------------------------------------------------
# özel wake_words
# --------------------------------------------------------------------------


def test_custom_wake_words_override_defaults() -> None:
    custom_detector = WakeWordDetector(wake_words=["jarvis"])

    assert custom_detector.matches("jarvis") is True
    assert custom_detector.matches("artemis") is False


# --------------------------------------------------------------------------
# feed() — enerji kapısı: sessiz odada Whisper HİÇ çağrılmamalı
# --------------------------------------------------------------------------


def test_feed_never_calls_whisper_while_silent(fake_whisper: _FakeWhisperEnvironment) -> None:
    wake_detector = WakeWordDetector()

    for _ in range(30):
        assert wake_detector.feed(_silence_block()) is False

    assert fake_whisper.call_count == 0
    assert fake_whisper.model_load_calls == []  # model bir kez bile yüklenmedi


# --------------------------------------------------------------------------
# feed() — konuşma + sessizlik dizisi Whisper'ı tetikler
# --------------------------------------------------------------------------


def test_feed_returns_true_when_whisper_hears_wake_word(
    fake_whisper: _FakeWhisperEnvironment,
) -> None:
    fake_whisper.next_text = "artemis"
    wake_detector = WakeWordDetector(silence_blocks=2, max_buffer_seconds=5.0)

    result = False
    for block in (_speech_block(), _speech_block(), _silence_block(), _silence_block()):
        result = wake_detector.feed(block)
        if result:
            break

    assert result is True
    assert fake_whisper.call_count == 1


def test_feed_returns_false_when_whisper_hears_unrelated_text(
    fake_whisper: _FakeWhisperEnvironment,
) -> None:
    fake_whisper.next_text = "bugün hava çok güzel"
    wake_detector = WakeWordDetector(silence_blocks=2, max_buffer_seconds=5.0)

    result = None
    for block in (_speech_block(), _speech_block(), _silence_block(), _silence_block()):
        result = wake_detector.feed(block)

    assert result is False
    assert fake_whisper.call_count == 1


# --------------------------------------------------------------------------
# feed() — max_buffer_seconds: kullanıcı hiç susmasa bile tetiklenir
# --------------------------------------------------------------------------


def test_max_buffer_seconds_forces_recognition_even_without_silence(
    fake_whisper: _FakeWhisperEnvironment,
) -> None:
    fake_whisper.next_text = "artemis"
    # silence_blocks çok yüksek tutuluyor ki bu test YALNIZCA max_buffer_seconds
    # yolunu sınasın; sessizlik yoluyla yanlışlıkla tetiklenmesin.
    wake_detector = WakeWordDetector(max_buffer_seconds=0.3, silence_blocks=1_000)

    result = False
    for _ in range(10):  # hiç susmadan sürekli "konuşma" besleniyor
        result = wake_detector.feed(_speech_block(duration=0.125))
        if result:
            break

    assert result is True
    assert fake_whisper.call_count == 1


# --------------------------------------------------------------------------
# feed() — ön-tampon: konuşmadan ÖNCEKİ sessiz bloklar da Whisper'a gider
# --------------------------------------------------------------------------


def test_preroll_includes_silence_blocks_before_speech(
    fake_whisper: _FakeWhisperEnvironment,
) -> None:
    """Ön-tamponun gerçekten çalıştığını, Whisper'a giden ses uzunluğuyla doğrula.

    Konuşmadan önce birkaç sessiz blok beslenir (ön-tampon bunların
    SONuncularını tutar), sonra tek bir konuşma bloğu ve onu bitiren tek
    bir sessizlik bloğu gelir. Ön-tampon devrede DEĞİLSE Whisper'a yalnızca
    bu son 2 blok (konuşma + bitiren sessizlik) giderdi; ön-tampon
    devredeyse en az bir sessiz blok DAHA (konuşmadan önceki) sese dahil
    edilmiş olmalı — bunu örnek sayısını karşılaştırarak doğruluyoruz.
    """

    fake_whisper.next_text = "artemis"
    silence_block = _silence_block()
    speech_block = _speech_block()

    # silence_blocks=1: konuşmadan sonraki TEK sessiz blok tanımayı tetikler.
    wake_detector = WakeWordDetector(silence_blocks=1, max_buffer_seconds=5.0)

    for _ in range(5):  # ön-tampon yalnızca SON birkaçını tutar (bkz. _PREROLL_BLOCKS)
        assert wake_detector.feed(silence_block) is False

    assert wake_detector.feed(speech_block) is False  # konuşma başladı, henüz tetiklenmedi
    result = wake_detector.feed(silence_block)  # bitiren sessizlik: tetiklenmeli

    assert result is True
    assert fake_whisper.call_count == 1

    sent_samples = fake_whisper.transcribe_calls[0]["audio_length"]
    speech_block_samples = len(speech_block) // 2  # int16 => 2 bayt/örnek
    silence_block_samples = len(silence_block) // 2
    minimum_without_preroll = speech_block_samples + silence_block_samples

    assert sent_samples > minimum_without_preroll


# --------------------------------------------------------------------------
# reset() — durumu temizler
# --------------------------------------------------------------------------


def test_reset_clears_buffered_state(fake_whisper: _FakeWhisperEnvironment) -> None:
    """`reset()` sonrası konuşma hiç başlamamış gibi davranmalı.

    `silence_blocks=5` ile: reset öncesi başlatılan konuşmanın durumu
    (`_speech_started`) gerçekten temizlenmezse, reset sonrası beslenen 5
    sessiz blok "konuşma bitti" sayılıp Whisper'ı tetiklerdi (çünkü
    sessizlik yalnızca konuşma BAŞLADIKTAN sonra sayılır). Temizlenmişse
    bu 5 blok yalnızca ön-tampona girer, Whisper hiç çağrılmaz.
    """

    wake_detector = WakeWordDetector(silence_blocks=5, max_buffer_seconds=100.0)

    wake_detector.feed(_silence_block())  # ön-tampona girer
    wake_detector.feed(_speech_block())  # konuşma başlar, henüz bitmez

    wake_detector.reset()

    for _ in range(5):
        assert wake_detector.feed(_silence_block()) is False

    assert fake_whisper.call_count == 0
