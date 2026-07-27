"""Uyandırma sözcüğü ("Artemis") algılama — konuşma kapısı (Vosk/enerji) + Whisper tiny.

NEDEN VOSK SÖZCÜĞÜ TANIMAK İÇİN KULLANILMIYOR (ölçüldü, varsayılmadı)
    Piper ile üretilmiş Türkçe "Artemis" sesi hem Vosk'un küçük Türkçe
    modeline hem Whisper tiny'ye (hotwords ile) verildi:

        Söylenen  | Vosk küçük TR modeli   | Whisper tiny + hotwords
        ----------|-------------------------|-------------------------
        "Artemis" | "akdeniz"      (YANLIŞ) | "Artemis"       (DOĞRU)

    Üstelik Vosk'un dilbilgisi (grammar) kısıtlaması HER girdide boş
    string döndürüyordu ("artemiz sözlükte yok" uyarısıyla) — yani Vosk'u
    doğrudan uyandırma sözcüğü tanıyıcısı yapan eski tasarım fiilen hiçbir
    zaman uyanamıyordu. Bu yüzden **Vosk uyandırma sözcüğünü ASLA
    tanımaya çalışmaz** — sözcüğü tanımak hâlâ tamamen Whisper'ın işidir
    (bkz. `_recognize`). Vosk'un buradaki tek görevi aşağıda anlatılıyor.

NEDEN VOSK GERİ GELDİ — "KONUŞMA VAR MI?" KAPISI OLARAK (ölçüldü)
    Ham enerji eşiği (`energy_threshold`) sabit bir sayı olduğu için
    ortam gürültüsündeki değişime dayanamıyor. Aynı odada iki ayrı ölçüm:

        ölçüm 1: ortalama genlik 0.075 -> blokların %65'i eşiği AŞIYOR
                 (kimse konuşmuyorken)
        ölçüm 2: ortalama genlik 0.019 -> hiç aşmıyor

    Yani sabit bir enerji eşiği ilkesel olarak çalışamaz: kullanıcının
    gerçek ortamında Whisper sürekli boşuna tetiklenip `'artemiz 맛이'`,
    `'temiz'`, `'As if it is artemiz...'` gibi saçma "uyanmalar" üretti.
    Bu sorunun ASIL çözümü eşiği ORTAMA GÖRE KALİBRE ETMEKTİR: açılışta
    ortam gürültüsü ölçülüp eşik ona göre ayarlanır (bkz.
    `voice.audio.measure_noise_floor` ve `set_energy_threshold`).

    VOSK'UN ROLÜ: YALNIZCA DUYARLILIK EKLER, ASLA ENGELLEMEZ
    Vosk bir dönem TEK karar verici yapıldı ve bu ciddi bir hataydı:
    Vosk yalnızca KENDİ SÖZLÜĞÜNDEKİ sözcükler için metin üretir,
    "Artemis" o sözlükte yok. Ölçüm: sentezlenmiş bir "Artemis" klibinin
    7 bloğunun 7'sinde de "konuşma yok" dedi — yani kapı tam da
    dinlediğimiz sözcüğü susturdu ve uyandırma HİÇ çalışmadı.

    Bu yüzden karar artık VEYA'dır (bkz. `_is_speech`):

        konuşma = (enerji eşiği aşıldı) VEYA (Vosk bir sözcük duydu)

    Enerji eşiği aşıldıysa Vosk ne derse desin blok geçer; Vosk yalnızca
    eşiğin ALTINDA kalan kısık konuşmayı kurtarır.

NEDEN ENERJİ/VOSK KAPISI + WHISPER (ikisi neden ayrı)
    Whisper tiny, uyandırma sözcüğünü tanımakta hem Vosk'un küçük
    modelinden hem ham enerji ölçümünden çok daha doğru ama SÜREKLİ
    çalıştırılabilecek kadar ucuz değildir. Çözüm: Whisper'ı sürekli
    çalıştırmak yerine önce ucuz bir konuşma kapısıyla (Vosk ya da enerji
    ölçümü, bkz. `voice.audio.rms_amplitude`) konuşma olup olmadığına
    bakılır; Whisper yalnızca gerçekten konuşma algılandığında, biriken
    sesin TAMAMI üzerinde BİR KEZ çalıştırılır. Sessiz/gürültülü bir
    odada Whisper hiç çağrılmaz — CPU maliyeti sıfırdır, bu tasarımın
    ana kazancı budur.

    Ölçülen gecikme bunu doğruluyor: 0.81 saniyelik bir klip için 0.24
    saniye tanıma süresi; model yükleme (bir kez, lazy) ~4 saniye. Bu
    kapıyla tetiklenen bir uyandırma algılaması için fazlasıyla yeterli.

NEDEN ÖN-TAMPON (pre-roll) ŞART
    Enerji eşiği aşıldığı ANDA konuşma aslında bir-iki blok önce başlamış
    olur; yani ölçüm bunu fark ettiğinde "Ar-" hecesi çoktan geçmiştir.
    Ön-tampon olmadan Whisper'a yalnızca "-temis" gibi kesik bir parça
    gider ve tanıma bozulur. Bu yüzden enerjinin eşiğin ALTINDA olduğu
    son birkaç blok küçük bir döngüsel tamponda tutulur ve konuşma
    başladığı anda biriken sesin BAŞINA eklenir.

TASARIM (değişmedi): Bu sınıf mikrofonu KENDİ AÇMAZ; dışarıdan `feed()`
ile ses bloğu beslenir. Sebep iki yönlü:
    1. Test edilebilirlik: gerçek mikrofon olmadan, sentetik ses
       verisiyle test edilebilir.
    2. Cihaz sahipliği: mikrofonu tek bir yer (`core/voice_loop.py`) açar;
       uyandırma ve komut tanıma aynı akışı paylaşır. Aksi halde her
       uyanışta cihazın kapatılıp yeniden açılması gerekirdi — bu hem
       yavaştır hem bazı sürücülerde hataya yol açar.

SÖZCÜK EŞLEŞTİRME (değişmedi): "Artemis" bir özel isimdir; model bunu
"artemiz", "arte mis", "art emis" gibi çevirebilir. Bu yüzden eşleştirme
hem bir varyant listesine hem bulanık (fuzzy) benzerliğe dayanır (bkz.
`matches()`), varyantlar `config.yaml`'dan genişletilebilir.
"""

from __future__ import annotations

import difflib
import json
import logging
from collections import deque
from pathlib import Path

from voice.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES, rms_amplitude

logger = logging.getLogger(__name__)

DEFAULT_WAKE_WORDS = ("artemis", "artemiz", "arte mis", "art emis", "hartemis")
"""Varsayılan kabul edilen varyantlar. `config.yaml::wake_words` ile değiştirilebilir."""

DEFAULT_FUZZY_THRESHOLD = 0.85
"""Bulanık eşleşme eşiği (0-1). ÖLÇÜLEREK seçildi, tahminle değil.

Bu makinede Whisper tiny'nin gerçek çıktıları ve ayırt edilmesi gereken
Türkçe sözcükler (noktalama temizlendikten sonra):

    UYANMALI    'artemis' 1.000   'artemiz' 1.000   'arteniz' 0.857
    UYANMAMALI  'temiz'   0.833   'artık'   0.667   'tenis'   0.667

0.82 (eski değer) `temiz` sözcüğünü kabul ediyordu — kullanıcının
log'undaki tek gerçek yanlış uyanma buydu. 0.86 ve üstü ise `arteniz`
gibi makul çevirileri kaybediyor. 0.85 ikisini de doğru tarafta bırakır.
"""

_PUNCTUATION_TO_STRIP = ".,!?;:…\"'()[]{}"
"""Bulanık eşleştirmeden önce sözcüklerden atılan noktalama.

Whisper çıktıya noktalama ekler ve bu, sözcüğe YAPIŞIK geldiği için
benzerlik oranını düşürür — ölçüldü: 'arteniz' 0.857 iken 'arteniz.'
0.800'e düşüyor ve eşiğin altında kalıyor. Yani asistan, doğru duyduğu
hâlde yalnızca nokta yüzünden uyanmıyordu.
"""

MAX_NO_SPEECH_PROB = 0.40
"""Whisper'ın "burada konuşma yok" olasılığı bu değeri aşarsa çıktı yok sayılır.

Whisper tiny gürültüde kelime UYDURUR (ölçüldü: ortam gürültüsünden
'Hızlı, hızlı, hızlı' üretti). Ama kendi güvensizliğini de bildirir:

    ortam gürültüsü : no_speech_prob 0.59
    gerçek "Artemis": no_speech_prob 0.11

Bu eşik, modelin kendi şüphesini uyandırma kararına dahil eder."""

_COMBINING_DOT_ABOVE = "̇"

_PREROLL_BLOCKS = 2
"""Ön-tamponda tutulan, konuşmadan hemen önceki sessiz blok sayısı.

Bkz. modül dokümantasyonundaki "NEDEN ÖN-TAMPON ŞART" notu: enerji eşiği
aşıldığında sözcüğün başı çoktan geçmiş olur, bu tampon o kaybı telafi eder.
"""


def normalize_turkish(text: str) -> str:
    """Metni, Türkçe'ye özgü büyük/küçük harf tuzaklarını gidererek küçültür.

    Python'un `str.lower()`'ı Türkçe için doğru çalışmaz:

        "ARTEMİS".lower()  ->  "artemi̇s"   (i + U+0307 BİRLEŞEN NOKTA)

    Yani noktalı İ küçültüldüğünde düz "i" değil, "i" + ayrı bir birleşen
    nokta karakteri üretir; sonuç "artemis" ile EŞİT DEĞİLDİR. Aynı
    şekilde noktasız "ı" ile "i" de farklı karakterlerdir.

    Uyandırma sözcüğü eşleştirmesinde bu ayrımların hiçbiri anlamlı
    değildir (kullanıcı "artemis" diyor, yazımın inceliği önemsiz), bu
    yüzden burada ikisi de tek bir biçime indirgenir.
    """

    return text.lower().replace(_COMBINING_DOT_ABOVE, "").replace("ı", "i")


class WakeWordUnavailableError(Exception):
    """Whisper modeli yüklenemediğinde/indirilemediğinde fırlatılır."""


class WakeWordDetector:
    """Konuşma kapısı (Vosk ya da enerji eşiği) + Whisper ile ses bloklarında uyandırma sözcüğünü arar.

    Whisper SÜREKLİ çalışmaz: yalnızca konuşma kapısı "konuşma var"
    deyip konuşmanın bittiğine karar verildiğinde, biriken sesin TAMAMI
    üzerinde bir kez çalıştırılır (bkz. modül dokümantasyonu). Konuşma
    kapısı `vosk_model_path` verilmişse Vosk'un ürettiği metnin boş olup
    olmadığına bakar; verilmemişse ya da Vosk kurulamazsa eski davranışa,
    ham enerji eşiğine düşülür (bkz. `_ensure_vosk`, `_is_speech`).

    Args:
        wake_words: Kabul edilecek sözcük varyantları (küçük harf).
        model_size: faster-whisper model boyutu/adı (bkz. modül notu —
            "tiny" ölçülüp bu iş için hem yeterince doğru hem yeterince
            hızlı bulundu).
        compute_type: Çıkarımın sayısal hassasiyeti (örn. "int8"). CPU'da
            "int8" en az RAM'i kullanır ve en hızlı çalışır.
        fuzzy_threshold: Bulanık eşleşme eşiği (0-1).
        energy_threshold: Bu genliğin (0-1, bkz. `voice.audio.rms_amplitude`)
            altı sessizlik sayılır. Yalnızca Vosk yapılandırılmamışsa ya
            da kurulamazsa kullanılan YEDEK kapıdır (bkz. modül notu
            "NEDEN VOSK GERİ GELDİ").
        vosk_model_path: Vosk konuşma-tanıma model KLASÖRÜ. `None` ise
            (varsayılan) Vosk hiç kullanılmaz ve davranış eskisiyle
            birebir aynı kalır (geri uyumluluk). Verilirse "konuşma var
            mı?" kararı Vosk'un metin çıktısına dayanır — ama Vosk
            uyandırma sözcüğünü TANIMAK için kullanılmaz, bkz. modül
            dokümantasyonu.
        max_buffer_seconds: Kullanıcı hiç susmasa bile biriken ses bu
            süreyi (saniye) aşınca zorla tanımaya gönderilir.
        silence_blocks: Konuşma başladıktan sonra bu kadar ardışık sessiz
            blok gelirse konuşmanın bittiği kabul edilir ve tanıma tetiklenir.
    """

    def __init__(
        self,
        wake_words: tuple[str, ...] | list[str] = DEFAULT_WAKE_WORDS,
        model_size: str = "tiny",
        compute_type: str = "int8",
        device: str = "cpu",
        fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
        energy_threshold: float = 0.06,
        vosk_model_path: Path | None = None,
        max_buffer_seconds: float = 2.5,
        silence_blocks: int = 3,
    ) -> None:
        self._wake_words = tuple(normalize_turkish(w).strip() for w in wake_words if w.strip())
        self._model_size = model_size
        self._compute_type = compute_type
        self._device = device
        self._fuzzy_threshold = fuzzy_threshold
        self._energy_threshold = energy_threshold
        self._max_buffer_seconds = max_buffer_seconds
        self._silence_blocks = silence_blocks

        # Whisper'a verilecek ipucu dizgesi bir kez hesaplanır (bkz. _recognize).
        self._hotwords = " ".join(self._wake_words)

        self._model = None  # lazy yüklenir, bkz. _ensure_model

        self._vosk_model_path = vosk_model_path
        self._vosk_recognizer = None  # lazy kurulur, bkz. _ensure_vosk
        self._vosk_ready: bool | None = None  # None=henüz denenmedi, False=kullanılamıyor

        self._preroll: deque[bytes] = deque(maxlen=_PREROLL_BLOCKS)
        self._speech_blocks: list[bytes] = []
        self._speech_started = False
        self._silence_run = 0
        self._buffered_seconds = 0.0

    def _ensure_model(self):
        """Whisper modelini ilk gerçek tanıma denemesinde oluşturur (lazy).

        Model yüklemek saniyeler sürebildiği için (ölçüm: ~4 sn) bu iş
        nesne oluşturulurken değil, konuşma algılanıp arabellek gerçekten
        Whisper'a gönderilmesi gerektiğinde yapılır — sessiz bir odada hiç
        yüklenmeyebilir bile.

        Returns:
            Yüklenmiş `faster_whisper.WhisperModel` örneği.

        Raises:
            WakeWordUnavailableError: Model indirilemez/yüklenemezse
                (internet yok, disk/RAM yetersiz, geçersiz model adı vb.).
        """

        if self._model is not None:
            return self._model

        from voice.gpu import resolve_compute_type, resolve_device

        # `device` MUTLAKA açıkça geçilir ve ÖNCE `resolve_device`'tan
        # geçirilir. faster-whisper'ın varsayılanı "auto"dur; CUDA
        # kütüphaneleri yokken model YÜKLENİRKEN değil ilk TANIMA sırasında
        # "Library cublas64_12.dll is not found" ile çöker. `resolve_device`
        # hem DLL'leri sürece yükler hem yükleyemezse CPU'ya düşer.
        device = resolve_device(self._device)
        compute_type = resolve_compute_type(device, self._compute_type)

        from faster_whisper import WhisperModel  # lazy import

        try:
            self._model = WhisperModel(
                self._model_size,
                device=device,
                compute_type=compute_type,
            )
        except Exception as exc:  # noqa: BLE001 - model/ağ/donanım kaynaklı her hata
            raise WakeWordUnavailableError(
                f"Uyandırma sözcüğü modeli yüklenemedi "
                f"('{self._model_size}', device={device}). "
                "İnternet bağlantınızı kontrol edin ya da modeli önceden indirin: "
                "python scripts/setup_voice.py"
            ) from exc

        logger.info(
            "Uyandırma sözcüğü modeli yüklendi: %s (compute_type=%s).",
            self._model_size,
            self._compute_type,
        )
        return self._model

    def _ensure_vosk(self):
        """Vosk tanıyıcısını ilk `feed()` çağrısında kurar (lazy, Whisper ile aynı desen).

        `vosk_model_path` verilmemişse hiçbir şey yapmadan `None` döner —
        yani Vosk paketine dosya başında da, burada da hiç dokunulmaz.
        Model klasörü yoksa/bozuksa ya da `vosk` paketi kurulu değilse
        HATA FIRLATILMAZ: bir uyarı loglanır ve akış kalıcı olarak enerji
        eşiğine düşer (bkz. modül dokümantasyonu "NEDEN VOSK GERİ GELDİ").

        Returns:
            Kurulu bir `vosk.KaldiRecognizer` örneği, ya da Vosk
            yapılandırılmamışsa/kurulamıyorsa `None`.
        """

        if self._vosk_model_path is None:
            return None

        if self._vosk_recognizer is not None or self._vosk_ready is False:
            return self._vosk_recognizer

        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel  # lazy import

            SetLogLevel(-1)  # Vosk'un ayrıntılı konsol çıktısını sustur
            model = Model(str(self._vosk_model_path))
            self._vosk_recognizer = KaldiRecognizer(model, SAMPLE_RATE)
            self._vosk_ready = True
        except Exception as exc:  # noqa: BLE001 - paket/model/disk kaynaklı her hata
            logger.warning(
                "Vosk konuşma kapısı kurulamadı (%s); enerji eşiğine düşülüyor.",
                exc,
            )
            self._vosk_ready = False
            self._vosk_recognizer = None

        return self._vosk_recognizer

    def set_energy_threshold(self, threshold: float) -> None:
        """Konuşma kapısının enerji eşiğini günceller.

        `core/voice_loop.py` açılışta ortam gürültüsünü ölçüp bu değeri
        buraya verir. Sabit bir eşik değişken ortam gürültüsünde
        çalışmıyordu (bkz. `voice.audio.measure_noise_floor`).
        """

        self._energy_threshold = threshold
        logger.info("Uyandırma enerji eşiği güncellendi: %.3f", threshold)

    def _is_speech(self, block: bytes) -> bool:
        """Bir ses bloğunda gerçek konuşma olup olmadığına karar verir.

        Karar İKİ göstergenin BİRLEŞİMİDİR (VEYA, kesişim değil):

            konuşma = (enerji eşiği aşıldı) VEYA (Vosk bir sözcük duydu)

        VOSK NEDEN TEK BAŞINA KARAR VEREMEZ (ölçüldü, acı bir dersle):
            Vosk yalnızca KENDİ SÖZLÜĞÜNDEKİ sözcükler için metin üretir.
            "Artemis" o sözlükte yok. Vosk tek karar verici yapıldığında,
            sentezlenmiş bir "Artemis" klibinin 7 bloğunun 7'sinde de
            "konuşma yok" dedi — yani kapı, tam da dinlediğimiz sözcüğü
            susturdu ve uyandırma HİÇ çalışmadı.

            Bu yüzden Vosk burada yalnızca DUYARLILIK EKLER: enerji
            eşiğinin altında kalan ama gerçekten konuşma olan blokları
            kurtarır. Hiçbir zaman bir bloğu ENGELLEYEMEZ.

        Gürültü sorununu çözen şey Vosk değil, eşiğin ORTAMA GÖRE
        kalibre edilmesidir (bkz. `voice.audio.measure_noise_floor` ve
        `core.voice_loop.VoiceAssistant._calibrate_silence_threshold`).

        ÖNEMLİ — Vosk'un rolü SINIRLI: burada okunan metin ("artemis" mi
        dedi mi?) HİÇ SORULMAZ, yalnızca metnin varlığına bakılır. Sözcüğü
        tanımak hâlâ tamamen `_recognize()` üzerinden Whisper'a aittir.
        """

        if rms_amplitude(block) >= self._energy_threshold:
            return True

        recognizer = self._ensure_vosk()
        if recognizer is None:
            return False

        if recognizer.AcceptWaveform(block):
            text = json.loads(recognizer.Result()).get("text", "")
        else:
            text = json.loads(recognizer.PartialResult()).get("partial", "")
        return bool(text.strip())

    def feed(self, block: bytes) -> bool:
        """Bir ses bloğunu konuşma kapısından geçirir; uyandırma sözcüğü algılanırsa True döner.

        Konuşma kapısı olmayan (kapının "konuşma yok" dediği) bloklar
        Whisper'ı hiç tetiklemez, yalnızca küçük bir ön-tampona eklenir
        (bkz. modül dokümantasyonu — "NEDEN ÖN-TAMPON ŞART"). Kapı
        "konuşma var" deyince konuşma başladı sayılır ve ön-tampon dahil
        bloklar biriktirilir; `silence_blocks` kadar ardışık konuşmasız
        blok gelene ya da `max_buffer_seconds` aşılana kadar birikim
        sürer. O noktada biriken sesin TAMAMI Whisper'a verilir.

        Args:
            block: 16 kHz, tek kanal, 16-bit PCM ses verisi.

        Returns:
            Uyandırma sözcüğü algılandıysa True.

        Raises:
            WakeWordUnavailableError: Whisper modeli yüklenemezse (yalnızca
                birikmiş ses gerçekten bir tanıma denemesine gönderildiğinde
                ortaya çıkar).
        """

        is_speech = self._is_speech(block)

        if not self._speech_started:
            if not is_speech:
                self._preroll.append(block)
                return False
            self._start_speech()

        self._speech_blocks.append(block)
        self._buffered_seconds += self._block_seconds(block)
        self._silence_run = 0 if is_speech else self._silence_run + 1

        speech_ended = self._silence_run >= self._silence_blocks
        buffer_full = self._buffered_seconds >= self._max_buffer_seconds
        if not (speech_ended or buffer_full):
            return False

        audio = b"".join(self._speech_blocks)
        self.reset()
        return self._recognize(audio)

    def _start_speech(self) -> None:
        """Konuşmanın başladığını işaretler, ön-tamponu birikime aktarır.

        Ön-tampon ŞART: enerji eşiği aşıldığında konuşmanın ilk hecesi
        ("Ar-" gibi) çoktan geçmiş olur; bu blok es geçilip yalnızca ondan
        SONRAKİ bloklarla başlanırsa kelimenin başı kesilir ve Whisper'a
        giden ses eksik bir parça olur, tanıma bozulur. Bu yüzden enerjinin
        eşiğin altında olduğu son birkaç blok (ön-tampon) biriken konuşmanın
        BAŞINA eklenir.
        """

        self._speech_started = True
        self._speech_blocks = list(self._preroll)
        self._buffered_seconds = sum(self._block_seconds(b) for b in self._speech_blocks)
        self._preroll.clear()

    @staticmethod
    def _block_seconds(block: bytes) -> float:
        """Bir ses bloğunun süresini saniye cinsinden hesaplar."""

        return (len(block) // SAMPLE_WIDTH_BYTES) / SAMPLE_RATE

    def _recognize(self, audio: bytes) -> bool:
        """Birikmiş sesi Whisper'a verir, çıktıyı `matches()` ile karşılaştırır."""

        import numpy as np  # lazy import: ses kullanılmadan proje import edilebilsin

        # Whisper float32, -1.0..1.0 aralığında bekler; int16 PCM bunun 32768
        # katıdır (bkz. `voice/stt.py::SpeechToText.transcribe` — aynı dönüşüm).
        audio_array = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        model = self._ensure_model()

        # DİKKAT: `transcribe()` TEMBEL bir generator döndürür — asıl
        # çıkarım, segmentler TÜKETİLDİĞİNDE çalışır. Bu yüzden yalnızca
        # `transcribe()` çağrısını sarmak yetmez; hatalar (örn. eksik CUDA
        # kütüphaneleri) tam olarak aşağıdaki `join` satırında yüzeye çıkar.
        # İkisi de aynı blokta olmalı.
        try:
            segments, _info = model.transcribe(
                audio_array,
                language="tr",
                # Ölçümde "Haydi Nis" -> "Artemis" farkını yaratan tam olarak
                # buydu: hotwords kod çözücüyü bu sözcüklere doğru yönlendirir.
                # Atlanırsa Whisper tiny özel isimlerde sık sık yakın-ses bir
                # kelimeye savrulur.
                hotwords=self._hotwords,
                # Silero VAD ile konuşma OLMAYAN kısımlar tanımaya hiç
                # girmez. Whisper tiny gürültüde kelime uydurur; ölçüldü:
                #     vad_filter=False -> ortam gürültüsünden 'Hızlı, hızlı, hızlı'
                #     vad_filter=True  -> ''
                # Yanlış uyanmaların en büyük kaynağı buydu.
                vad_filter=True,
            )
            kept: list[str] = []
            for segment in segments:
                # Modelin KENDİ şüphesini de hesaba kat: gürültüde
                # no_speech_prob 0.59, gerçek konuşmada 0.11 ölçüldü.
                if getattr(segment, "no_speech_prob", 0.0) > MAX_NO_SPEECH_PROB:
                    logger.debug(
                        "Segment yok sayıldı (no_speech_prob=%.2f): %r",
                        segment.no_speech_prob,
                        segment.text,
                    )
                    continue
                kept.append(segment.text)
            text = "".join(kept).strip()
        except Exception as exc:  # noqa: BLE001 - donanım/kütüphane kaynaklı her hata
            raise WakeWordUnavailableError(
                f"Uyandırma sözcüğü tanınamadı (device={self._device}, "
                f"compute_type={self._compute_type}): {exc}"
            ) from exc

        if not text:
            return False

        if self.matches(text):
            logger.info("Uyandırma sözcüğü algılandı: %r", text)
            return True

        return False

    def matches(self, text: str) -> bool:
        """Verilen metnin bir uyandırma sözcüğü içerip içermediğini söyler.

        Önce doğrudan alt-dizge araması yapılır (en ucuz ve en kesin yol);
        bulunamazsa metindeki her sözcük, varyantlara karşı bulanık
        benzerlikle karşılaştırılır.

        Karşılaştırma `normalize_turkish()` üzerinden yapılır — aksi halde
        "ARTEMİS" gibi noktalı İ içeren bir girdi, alt-dizge aşamasında
        SESSİZCE ıskalanır ve yalnızca bulanık eşleşmenin şansına kalırdı.
        """

        normalized = normalize_turkish(text).strip()
        if not normalized:
            return False

        for wake_word in self._wake_words:
            if wake_word in normalized:
                return True

        for raw_token in normalized.split():
            # Noktalama MUTLAKA atılır: sözcüğe yapışık bir nokta benzerlik
            # oranını belirgin biçimde düşürüyor ve doğru duyulmuş bir
            # uyandırmayı eşiğin altına itiyordu (bkz. `_PUNCTUATION_TO_STRIP`).
            token = raw_token.strip(_PUNCTUATION_TO_STRIP)
            if not token:
                continue

            for wake_word in self._wake_words:
                similarity = difflib.SequenceMatcher(None, token, wake_word).ratio()
                if similarity >= self._fuzzy_threshold:
                    logger.debug("Bulanık eşleşme: %r ~ %r (%.2f)", token, wake_word, similarity)
                    return True

        return False

    def reset(self) -> None:
        """Ön-tamponu ve konuşma birikimini temizler.

        Whisper modeli YÜKLÜ kalır — yeniden yüklemek saniyeler sürer
        (bkz. `_ensure_model`), bu yüzden yalnızca akış durumu sıfırlanır.
        Hem bir tanıma denemesi tamamlandığında `feed()` içinden hem
        uyandıktan sonra `core/voice_loop.py`'den çağrılır.
        """

        self._preroll.clear()
        self._speech_blocks = []
        self._speech_started = False
        self._silence_run = 0
        self._buffered_seconds = 0.0
