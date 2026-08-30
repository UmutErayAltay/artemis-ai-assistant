"""Konuşmayı metne çevirme: konuşma sonu algılama + faster-whisper ile STT.

İki sorumluluk kasıtlı olarak aynı dosyada ama ayrı sınıflarda tutulur:

    SpeechRecorder — "kullanıcı konuşmayı bitirdi mi?" sorusuna cevap verir.
        Ses biriktirir, RMS genliğe bakarak sessizliği algılar. Whisper'dan
        tamamen bağımsızdır; hiçbir ağır kütüphane kullanmaz.
    SpeechToText   — biriken sesi gerçekten metne çevirir (faster-whisper).

Ayrım bilinçli: `core/voice_loop.py` önce `SpeechRecorder.feed()` ile
mikrofon bloklarını besler (bu çok ucuzdur, sürekli çalışabilir), konuşma
bittiğine karar verilince BİR KEZ `SpeechToText.transcribe()` çağrılır
(bu pahalıdır, Whisper modelini belleğe alır). Bu, `voice/__init__.py`'daki
"iki ayrı tanıma motoru" tasarım notuyla aynı felsefe: pahalı işi yalnızca
gerektiğinde çalıştır.
"""

from __future__ import annotations

import logging

from voice.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES, rms_amplitude

logger = logging.getLogger(__name__)

DEFAULT_SILENCE_THRESHOLD = 0.06
"""Bu genliğin (0-1, bkz. `voice.audio.rms_amplitude`) altı sessizlik sayılır."""

DEFAULT_SILENCE_TIMEOUT = 1.2
"""Konuşma başladıktan sonra bu kadar saniye sessizlik olursa kayıt biter."""

DEFAULT_MAX_DURATION = 12.0
"""Kullanıcı hiç susmasa bile kayıt bu saniyeden uzun sürmez."""

_CPU_FALLBACK_MODEL_SIZE = "small"
"""GPU istenip bulunamadığında düşülecek model boyutu.

`large-v3-turbo` GPU'da ~0.3 saniyede tanır ama CPU'da 10 saniyeyi
aşabilir. Sessizce yavaşlamak yerine küçük modele inmek, asistanı
kullanılabilir tutar (bkz. `_ensure_model`)."""

MIN_TRANSCRIBE_SECONDS = 0.2
"""Bundan kısa ses Whisper'a hiç gönderilmez — anlamlı bir söz olamayacak
kadar kısadır ve modeli boşuna belleğe/çalışmaya zorlamanın alemi yok."""

MAX_NO_SPEECH_PROB = 0.40
"""Whisper'ın "burada konuşma yok" olasılığı bu değeri aşarsa segment yok sayılır.

`voice/wake_word.py::MAX_NO_SPEECH_PROB` ile AYNI, orada ÖLÇÜLMÜŞ değer
(gürültü: 0.59, gerçek "Artemis": 0.11) — burada tekrarlanıyor çünkü bu
dosya `wake_word.py`'yi import ETMEZ (ayrı, bağımsız iki tanıma motoru,
bkz. modül dokümantasyonu). Kullanıcı raporu ("Spotify'ı açar mısın" ->
"izlediğiniz için teşekkür ederim"): `transcribe()`'ın `vad_filter=False`
YEDEK denemesi bu filtre OLMADAN çalışıyordu — tam olarak `wake_word.py`'nin
kendi yorumunda belgelediği tehlikeli yol ("vad_filter=False -> ortam
gürültüsünden 'Hızlı, hızlı, hızlı'"). Whisper kendi şüphesini bildiriyor
ama bu bilgi kullanılmıyordu."""


class SpeechRecognitionUnavailableError(Exception):
    """faster-whisper modeli yüklenemediğinde/indirilemediğinde fırlatılır."""


class SpeechRecorder:
    """Beslenen ses bloklarını biriktirir, konuşmanın ne zaman bittiğine karar verir.

    TASARIM: `wake_word.py::WakeWordDetector` ile aynı desen — mikrofonu
    KENDİ AÇMAZ, dışarıdan `feed()` ile beslenir. Sebep aynı: hem test
    edilebilirlik (gerçek mikrofon olmadan sentetik PCM ile test edilebilir)
    hem cihaz sahipliği (mikrofonu tek bir yer açar, bkz. o dosyadaki not).

    Sessizlik algılama iki aşamalıdır: önce genlik `silence_threshold`
    üzerine çıkıp konuşmanın gerçekten başladığı anlaşılmalı — aksi halde
    kaydın başındaki doğal sessizlik "konuşma bitti" sanılırdı. Konuşma
    başladıktan SONRA genlik `silence_timeout` saniye boyunca eşiğin
    altında kalırsa kayıt biter. Kullanıcı hiç susmazsa (ya da eşik hiç
    aşılmazsa) `max_duration` üst sınır görevi görür.

    Args:
        silence_threshold: Bu genliğin (0-1) altı sessizlik sayılır.
        silence_timeout: Konuşma başladıktan sonra izin verilen azami
            sessizlik süresi (saniye).
        max_duration: Kaydın üst sınırı (saniye) — konuşma hiç bitmese
            bile bu süre aşılmaz.
    """

    def __init__(
        self,
        silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
        silence_timeout: float = DEFAULT_SILENCE_TIMEOUT,
        max_duration: float = DEFAULT_MAX_DURATION,
    ) -> None:
        self._silence_threshold = silence_threshold
        self._silence_timeout = silence_timeout
        self._max_duration = max_duration

        self._buffer = bytearray()
        self._speech_started = False
        self._silence_elapsed = 0.0
        self._total_elapsed = 0.0

    def feed(self, block: bytes) -> bool:
        """Bir ses bloğunu biriktirir; konuşma bittiyse True döner.

        Args:
            block: 16 kHz, tek kanal, 16-bit PCM ses verisi
                (bkz. `voice.audio.SAMPLE_RATE`).

        Returns:
            Konuşma (sessizlik nedeniyle ya da azami süre dolduğu için)
            bittiyse True; kayıt devam ediyorsa False.
        """

        self._buffer += block

        block_duration = (len(block) // SAMPLE_WIDTH_BYTES) / SAMPLE_RATE
        self._total_elapsed += block_duration

        amplitude = rms_amplitude(block)
        if amplitude >= self._silence_threshold:
            self._speech_started = True
            self._silence_elapsed = 0.0
        elif self._speech_started:
            self._silence_elapsed += block_duration

        if self._speech_started and self._silence_elapsed >= self._silence_timeout:
            logger.debug("Konuşma sessizlik nedeniyle bitti (%.2f sn).", self._total_elapsed)
            return True

        if self._total_elapsed >= self._max_duration:
            logger.debug("Kayıt azami süreye ulaştığı için bitirildi (%.2f sn).", self._max_duration)
            return True

        return False

    @property
    def audio_bytes(self) -> bytes:
        """Şu ana kadar biriken ham 16-bit PCM veriyi döndürür."""

        return bytes(self._buffer)

    def reset(self) -> None:
        """Arabelleği ve iç durumu temizler; nesneyi yeni bir kayıt için hazırlar."""

        self._buffer.clear()
        self._speech_started = False
        self._silence_elapsed = 0.0
        self._total_elapsed = 0.0


class SpeechToText:
    """faster-whisper ile 16 kHz PCM sesi Türkçe metne çevirir.

    Model LAZY yüklenir: nesne oluşturulurken hiçbir şey belleğe alınmaz,
    ilk `transcribe()` çağrısında (`_ensure_model`) yüklenir — model
    yükleme saniyeler sürebildiği için bu iş yalnızca gerçekten
    gerektiğinde yapılır (bkz. `wake_word.py::_ensure_recognizer`).

    Args:
        model_size: faster-whisper model boyutu/adı (örn. "tiny", "small",
            "medium"). Yerelde yoksa Hugging Face Hub'dan indirilir.
        compute_type: Çıkarımın sayısal hassasiyeti (örn. "int8", "float16").
            CPU'da "int8" en az RAM'i kullanır ve en hızlıdır.
        device: "cpu" ya da "cuda".
    """

    def __init__(
        self,
        model_size: str = "small",
        compute_type: str = "int8",
        device: str = "cpu",
    ) -> None:
        self._model_size = model_size
        self._compute_type = compute_type
        self._device = device
        self._model = None

    def _ensure_model(self):
        """Whisper modelini ilk kullanımda oluşturur (lazy).

        Returns:
            Yüklenmiş `faster_whisper.WhisperModel` örneği.

        Raises:
            SpeechRecognitionUnavailableError: Model indirilemez/yüklenemezse
                (internet yok, disk/RAM yetersiz, geçersiz model adı vb.).
        """

        if self._model is not None:
            return self._model

        from voice.gpu import resolve_compute_type, resolve_device

        # "cuda" istendiği hâlde CUDA kütüphaneleri yoksa sessizce çökmek
        # yerine CPU'ya düşülür (bkz. `voice/gpu.py`). Bu kontrol modelin
        # OLUŞTURULMASINDAN önce yapılmalı: eksik cuBLAS hatası model
        # yüklenirken değil İLK TANIMA sırasında patlıyor.
        device = resolve_device(self._device)
        model_size = self._model_size

        # GPU istenip CPU'ya düşüldüyse büyük modeli CPU'da çalıştırmak
        # komut başına 10+ saniye demektir — asistanı kullanılamaz yapar.
        # Böyle bir durumda küçük modele inip DURUMU AÇIKÇA söyle.
        if self._device == "cuda" and device == "cpu" and model_size.startswith("large"):
            logger.warning(
                "GPU kullanılamadığı için '%s' yerine '%s' modeline düşülüyor "
                "(büyük model CPU'da çok yavaş). CUDA'yı kurmak için: "
                "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12",
                model_size,
                _CPU_FALLBACK_MODEL_SIZE,
            )
            model_size = _CPU_FALLBACK_MODEL_SIZE

        from faster_whisper import WhisperModel  # lazy import

        try:
            self._model = WhisperModel(
                model_size,
                device=device,
                compute_type=resolve_compute_type(device, self._compute_type),
            )
        except Exception as exc:
            raise SpeechRecognitionUnavailableError(
                f"Whisper modeli yüklenemedi ('{self._model_size}'). İnternet "
                "bağlantınızı kontrol edin ya da modeli önceden indirin: "
                "python scripts/setup_voice.py"
            ) from exc

        logger.info(
            "Whisper modeli yüklendi: %s (device=%s, compute_type=%s).",
            self._model_size,
            self._device,
            self._compute_type,
        )
        return self._model

    def transcribe(self, pcm_bytes: bytes, hotwords: str | None = None) -> str:
        """16 kHz, tek kanal, 16-bit PCM sesi Türkçe metne çevirir.

        Dil algılamaya GÜVENİLMEZ: `language="tr"` sabit geçilir. Otomatik
        algılama kısa Türkçe komutlarda sık sık İngilizce/Almanca gibi
        yakın dillere savrulur; bu proje yalnızca Türkçe konuşulacağını
        zaten bildiği için algılamayı devre dışı bırakmak hem daha doğru
        hem daha hızlıdır (ilk 30 saniyeyi dil algılamak için harcamaz).

        Args:
            pcm_bytes: `SpeechRecorder.audio_bytes` ile üretilmiş ham ses.
            hotwords: Modele "bu sözcükler geçebilir" diye verilen ipucu
                sözlüğü (virgülle ayrılmış). Bu projede en kritik
                parametredir: yabancı özel isimler (Discord, Riot Client,
                Valorant) Türkçe bir model için sözlük dışıdır ve ipucu
                verilmezse "Dischord", "Biyot Cilent" gibi çevrilir.
                Ölçülen fark:

                    ipucu YOK : "Artemis"          -> "Haydi Nis."
                    ipucu VAR : "Artemis"          -> "Artemis"
                    ipucu YOK : "Riot Client'ı aç" -> "Biyot Cilent'e aç."
                    ipucu VAR : "Riot Client'ı aç" -> "Riot Client'e aç."

                Listeyi `plugins._app_resolver.AppResolver.known_app_names()`
                üretir (kullanıcının GERÇEKTEN kurulu uygulamaları).

        Returns:
            Tanınan metin, baş/son boşlukları temizlenmiş. Ses boş ya da
            `MIN_TRANSCRIBE_SECONDS`'tan kısaysa model hiç yüklenmeden/
            çalıştırılmadan boş string döner.

        Raises:
            SpeechRecognitionUnavailableError: Model yüklenemezse.
        """

        if not pcm_bytes:
            return ""

        sample_count = len(pcm_bytes) // SAMPLE_WIDTH_BYTES
        if sample_count / SAMPLE_RATE < MIN_TRANSCRIBE_SECONDS:
            logger.debug("Ses çok kısa (%d örnek), Whisper çalıştırılmadı.", sample_count)
            return ""

        import numpy as np  # lazy import: ses kullanılmadan proje import edilebilsin

        # Whisper float32, -1.0..1.0 aralığında bekler; int16 PCM bunun 32768 katıdır.
        audio_array = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        model = self._ensure_model()

        # `SpeechRecorder` konuşma bitene karar vermek için kullanıcı
        # sustuktan sonra `silence_timeout` (varsayılan 1.2 sn) kadar
        # dinlemeye devam eder ve bu süre boyunca toplanan ses (oda
        # gürültüsü, nefes, mikrofon taban gürültüsü) de arabelleğe
        # eklenir (bkz. `SpeechRecorder.feed`) — yani buraya gelen
        # `audio_array` neredeyse HER ZAMAN konuşmanın arkasında bir
        # gürültü kuyruğu taşır. Whisper'ın bilinen bir zaafı, sessizlik/
        # düşük seviyeli gürültü verildiğinde halüsinasyon yapması
        # (var olmayan kelime uydurması) ve bunun cümlenin geri kalanının
        # çözülme biçimini de bozabilmesidir.
        #
        # Bu proje aynı sorunu bir kez, başka bir bağlamda ölçüp çözmüştü:
        # `voice/wake_word.py::WakeWordDetector._recognize` uyandırma
        # sözcüğü tanımasında `vad_filter=True` kullanıyor ve kod
        # yorumunda ölçülmüş kanıt var ("vad_filter=False -> ortam
        # gürültüsünden 'Hızlı, hızlı, hızlı'; vad_filter=True -> boş").
        # O düzeltme yalnızca uyandırma sözcüğüne uygulanmış, asıl komut
        # tanımasına hiç taşınmamıştı. BURADA (ana `vad_filter=True`
        # denemesinde) yine taşınmıyor, bilinçli olarak: bu tek klip zaten
        # "kullanıcı konuştu" kararı verilmiş bir kayıt (bkz. §36c), VAD
        # gürültüyü büyük ölçüde eledi; no_speech_prob eşiğini burada da
        # uygulamak kısık/sessiz ama GERÇEK konuşan bir kullanıcı için
        # yanlış-negatif riski taşır — tam olarak §36c'nin önlemek
        # istediği "Sizi duyamadım" hatası. Özel `vad_parameters` de
        # GEÇİLMEZ: faster-whisper'ın Silero VAD varsayılanları
        # (`min_silence_duration_ms=2000` vb.) zaten muhafazakâr —
        # yalnızca ≥2 sn'lik sessizlikleri keser, cümle içi doğal
        # duraklamalara dokunmaz.
        segments, _info = model.transcribe(
            audio_array, language="tr", hotwords=hotwords or None, vad_filter=True
        )
        text = "".join(segment.text for segment in segments).strip()

        if not text:
            # Koşulsuz boş sonucu kabul etmek yasak (CLAUDE.md'nin
            # "koşulsuz success=True yasak" ilkesiyle aynı ruhta): VAD
            # tüm klibi "sessizlik" sayıp silmiş olabilir (kısık konuşma,
            # düşük mikrofon kazancı). Bu durumda kullanıcı "Sizi
            # duyamadım" duyar (bkz. `core/voice_loop.py::_handle_one_
            # command`) — bu, wake-word'deki bir yanlış negatiften daha
            # pahalıdır (orada kullanıcı basitçe tekrar dener). Bu yüzden
            # BİR KEZ, VAD olmadan tekrar denenir. Bu ikinci deneme,
            # ana denemenin aksine, `no_speech_prob` filtresi ALIR:
            # `vad_filter=False` burada VAD'ın hiç bakmadığı ham gürültüyü
            # de kabul ediyor (bkz. MAX_NO_SPEECH_PROB dokümantasyonu,
            # gerçek olay: "Spotify'ı açar mısın" -> "izlediğiniz için
            # teşekkür ederim") — ana denemedeki yanlış-negatif riski
            # burada YOK, çünkü filtre uygulanmazsa zaten kabul edilecek
            # bir metin, filtre uygulanınca en kötü ihtimalle "Sizi
            # duyamadım" olur; bu, halüsine edilmiş bir komutu sessizce
            # çalıştırmaktan daha ucuz bir hata.
            def _extract_text(segments):
                kept = []
                for segment in segments:
                    no_speech_prob = getattr(segment, "no_speech_prob", 0.0)
                    if no_speech_prob > MAX_NO_SPEECH_PROB:
                        logger.debug(
                            "Segment yok sayıldı (no_speech_prob=%.2f): %r",
                            no_speech_prob, segment.text,
                        )
                        continue
                    kept.append(segment.text)
                return "".join(kept).strip()

            segments, _info = model.transcribe(
                audio_array, language="tr", hotwords=hotwords or None, vad_filter=False
            )
            text = _extract_text(segments)

        return text
