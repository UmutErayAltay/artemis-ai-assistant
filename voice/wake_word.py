"""Uyandırma sözcüğü ("Artemis") algılama — enerji kapısı + Whisper tiny.

NEDEN VOSK ATILDI (ölçüldü, varsayılmadı)
    Piper ile üretilmiş Türkçe "Artemis" sesi hem Vosk'un küçük Türkçe
    modeline hem Whisper tiny'ye (hotwords ile) verildi:

        Söylenen  | Vosk küçük TR modeli   | Whisper tiny + hotwords
        ----------|-------------------------|-------------------------
        "Artemis" | "akdeniz"      (YANLIŞ) | "Artemis"       (DOĞRU)

    Üstelik Vosk'un dilbilgisi (grammar) kısıtlaması HER girdide boş
    string döndürüyordu ("artemiz sözlükte yok" uyarısıyla) — yani eski
    kod fiilen hiçbir zaman uyanamıyordu. Bu bir tahmin değil, ölçülmüş
    bir arıza; bu yüzden Vosk'a hiçbir referans bırakılmadan bu dosya
    baştan yazıldı.

NEDEN ENERJİ KAPISI + WHISPER
    Whisper tiny, Vosk'un küçük modelinden çok daha doğru ama Vosk kadar
    "sürekli bedava çalıştırılabilir" değildir. Çözüm: Whisper'ı SÜREKLİ
    çalıştırmak yerine önce ucuz bir enerji ölçümüyle (bkz.
    `voice.audio.rms_amplitude`) konuşma olup olmadığına bakılır; Whisper
    yalnızca gerçekten konuşma algılandığında, biriken sesin TAMAMI
    üzerinde BİR KEZ çalıştırılır. Sessiz bir odada Whisper hiç
    çağrılmaz — CPU maliyeti sıfırdır, bu tasarımın ana kazancı budur.

    Ölçülen gecikme bunu doğruluyor: 0.81 saniyelik bir klip için 0.24
    saniye tanıma süresi; model yükleme (bir kez, lazy) ~4 saniye. Enerji
    kapısıyla tetiklenen bir uyandırma algılaması için fazlasıyla yeterli.

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
import logging
from collections import deque

from voice.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES, rms_amplitude

logger = logging.getLogger(__name__)

DEFAULT_WAKE_WORDS = ("artemis", "artemiz", "arte mis", "art emis", "hartemis")
"""Varsayılan kabul edilen varyantlar. `config.yaml::wake_words` ile değiştirilebilir."""

DEFAULT_FUZZY_THRESHOLD = 0.82
"""Bulanık eşleşme eşiği (0-1). Düşürmek yanlış uyanmaları artırır."""

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
    """Enerji kapısı + Whisper ile beslenen ses bloklarında uyandırma sözcüğünü arar.

    Whisper SÜREKLİ çalışmaz: yalnızca enerji eşiği aşılıp konuşma
    algılandığında ve konuşmanın bittiğine karar verildiğinde, biriken
    sesin TAMAMI üzerinde bir kez çalıştırılır (bkz. modül dokümantasyonu).

    Args:
        wake_words: Kabul edilecek sözcük varyantları (küçük harf).
        model_size: faster-whisper model boyutu/adı (bkz. modül notu —
            "tiny" ölçülüp bu iş için hem yeterince doğru hem yeterince
            hızlı bulundu).
        compute_type: Çıkarımın sayısal hassasiyeti (örn. "int8"). CPU'da
            "int8" en az RAM'i kullanır ve en hızlı çalışır.
        fuzzy_threshold: Bulanık eşleşme eşiği (0-1).
        energy_threshold: Bu genliğin (0-1, bkz. `voice.audio.rms_amplitude`)
            altı sessizlik sayılır; Whisper yalnızca bu eşik aşıldığında
            devreye girer.
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

        from faster_whisper import WhisperModel  # lazy import

        try:
            # `device` MUTLAKA açıkça geçilir. faster-whisper'ın varsayılanı
            # "auto"dur ve makinede bir NVIDIA GPU görürse CUDA'yı seçer;
            # ama CUDA çalışma kütüphaneleri (cuBLAS/cuDNN) kurulu değilse
            # bu, model YÜKLENİRKEN değil ilk TANIMA sırasında
            # "Library cublas64_12.dll is not found" ile çöker — yani
            # sessizce kurulup ilk komutta patlar. Varsayılanı "cpu"
            # tutmak bu tuzağı tamamen kapatır.
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
        except Exception as exc:  # noqa: BLE001 - model/ağ/donanım kaynaklı her hata
            raise WakeWordUnavailableError(
                f"Uyandırma sözcüğü modeli yüklenemedi "
                f"('{self._model_size}', device={self._device}). "
                "İnternet bağlantınızı kontrol edin ya da modeli önceden indirin: "
                "python scripts/setup_voice.py"
            ) from exc

        logger.info(
            "Uyandırma sözcüğü modeli yüklendi: %s (compute_type=%s).",
            self._model_size,
            self._compute_type,
        )
        return self._model

    def feed(self, block: bytes) -> bool:
        """Bir ses bloğunu enerji kapısından geçirir; uyandırma sözcüğü algılanırsa True döner.

        Enerji eşiğin altında kalan bloklar Whisper'ı hiç tetiklemez, yalnızca
        küçük bir ön-tampona eklenir (bkz. modül dokümantasyonu — "NEDEN
        ÖN-TAMPON ŞART"). Enerji eşiği aşılınca konuşma başladı sayılır ve
        ön-tampon dahil bloklar biriktirilir; `silence_blocks` kadar ardışık
        sessiz blok gelene ya da `max_buffer_seconds` aşılana kadar birikim
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

        amplitude = rms_amplitude(block)
        is_speech = amplitude >= self._energy_threshold

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
            )
            text = "".join(segment.text for segment in segments).strip()
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

        for token in normalized.split():
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
