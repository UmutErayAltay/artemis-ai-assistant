"""Bulut tabanlı konuşma tanıma: Groq'un barındırdığı Whisper API'si.

`voice.stt.SpeechToText` ile TAMAMEN aynı arayüze sahiptir
(`transcribe(pcm_bytes, hotwords=None) -> str`) — amaç, "internet varsa
bulut, yoksa yerel" hibrit stratejisinde yönlendirici katmanının
(`voice/router.py`) ikisini şeffafça takas edebilmesidir: internet
varken bu sınıf kullanılır, yokken (ya da bu sınıf başarısız olduğunda)
yerel `SpeechToText`'e düşülür.

Bu yüzden burada fırlatılan HER başarısızlık (ağ hatası, zaman aşımı,
HTTP 4xx/5xx, eksik/geçersiz anahtar, bozuk yanıt) TEK bir istisna
tipinde (`CloudSpeechUnavailableError`) toplanır — yönlendirici tek bir
`except` ile yerele düşebilsin diye. Sessiz başarısızlık YOKTUR: bir
şey ters giderse her zaman dürüstçe bu istisna fırlatılır, asla boş
string ile örtbas edilmez (boş string YALNIZCA ses gerçekten çok
kısaysa döner, bkz. `transcribe`).

Yerel STT'den (`voice/stt.py`) farkı: burada yüklenecek bir model YOKTUR,
gerçek tanıma işi Groq'un sunucusunda yapılır. "Lazy model yükleme"nin
karşılığı burada "lazy `requests` import"udur (bkz. modül harici
bağımlılıkların metod içinde import edilmesi kuralı). Ses hiçbir zaman
diske yazılmaz; PCM->WAV dönüşümü tamamen bellekte (`io.BytesIO`) yapılır
— bkz. `voice/audio.py` modül dokümantasyonundaki "ses verisi hiçbir
zaman diske yazılmaz" ilkesi.
"""

from __future__ import annotations

import logging
import os

from voice.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from voice.stt import MIN_TRANSCRIBE_SECONDS
from voice.wav import pcm_to_wav

logger = logging.getLogger(__name__)

_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
"""Groq'un OpenAI-uyumlu ses transkripsiyon uç noktası."""


class CloudSpeechUnavailableError(Exception):
    """Groq Whisper API kullanılamadığında fırlatılır.

    Tek bir şemsiye istisna: eksik/geçersiz anahtar, ağ hatası, zaman
    aşımı, HTTP 4xx/5xx ve bozuk yanıt gövdesi HEPSİ bu tipte toplanır.
    Yönlendirici katmanı bunu yakalayıp yerel `voice.stt.SpeechToText`'e
    düşecek şekilde tasarlanmıştır; bu yüzden çağıran taraf tek bir
    `except CloudSpeechUnavailableError` ile tüm bulut arızalarını
    yakalayabilir.
    """


class GroqSpeechToText:
    """Groq'un barındırdığı Whisper API'siyle 16 kHz PCM sesi Türkçe metne çevirir.

    `voice.stt.SpeechToText` ile AYNI arayüze sahiptir
    (`transcribe(pcm_bytes, hotwords=None) -> str`) — yönlendirici
    ikisini şeffafça takas edebilir.

    Burada yüklenecek/bellekte tutulacak bir model YOKTUR: her
    `transcribe()` çağrısı, sesi bellekte bir WAV'a sarıp Groq'un
    sunucusuna tek bir HTTP isteğiyle gönderir; gerçek tanıma işi
    tamamen uzakta yapılır.

    Args:
        api_key: Groq API anahtarı. Verilmezse `GROQ_API_KEY` ortam
            değişkeninden okunur. ÖNEMLİ: anahtar burada, constructor'da
            HİÇ doğrulanmaz — yönlendiricinin bu nesneyi anahtar
            olmadan da (örn. henüz ayarlanmamışken) oluşturabilmesi
            için eksik/geçersiz anahtar yalnızca `transcribe()`
            çağrıldığında bir hataya dönüşür (bkz. o metodun dokstringi).
        model: Groq'taki Whisper model adı (örn. "whisper-large-v3-turbo",
            "whisper-large-v3").
        timeout: HTTP isteği için azami bekleme süresi (saniye).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "whisper-large-v3-turbo",
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("GROQ_API_KEY")
        self._model = model
        self._timeout = timeout

    def transcribe(self, pcm_bytes: bytes, hotwords: str | None = None) -> str:
        """16 kHz, tek kanal, 16-bit PCM sesi Groq Whisper API'siyle Türkçe metne çevirir.

        Dil algılamaya GÜVENİLMEZ: `language="tr"` sabit gönderilir —
        `voice.stt.SpeechToText.transcribe` ile aynı gerekçeyle (bu proje
        yalnızca Türkçe konuşulacağını zaten bilir).

        Args:
            pcm_bytes: `SpeechRecorder.audio_bytes` ile üretilmiş ham ses
                (bkz. `voice.stt.SpeechRecorder`).
            hotwords: Groq'a `prompt` alanı olarak geçirilen bağlam
                ipucu. Yabancı özel isimler (Discord, Riot Client) için
                kritiktir — bkz. `voice.stt.SpeechToText.transcribe`
                dokstringindeki ölçülmüş örnekler; aynı mantık burada da
                geçerlidir, tek fark Whisper'ın kendi `hotwords`
                parametresi yerine Groq'un `prompt` alanının kullanılmasıdır.

        Returns:
            Tanınan metin, baş/son boşlukları temizlenmiş. Ses boş ya da
            `MIN_TRANSCRIBE_SECONDS`'tan kısaysa (bkz. `voice.stt`) ağa
            hiç istek atılmadan boş string döner — boşuna istek atmanın
            alemi yok.

        Raises:
            CloudSpeechUnavailableError: `GROQ_API_KEY` yoksa/boşsa, ağ
                hatası ya da zaman aşımı olursa, Groq HTTP 4xx/5xx
                döndürürse ya da yanıt gövdesi ayrıştırılamaz/beklenmeyen
                bir JSON'sa. Orijinal hata (varsa) `from exc` ile
                zincirlenir. Anahtar bu istisnanın hiçbir mesajında
                (ne tam ne kısmi) yer almaz.
        """

        if not pcm_bytes:
            return ""

        sample_count = len(pcm_bytes) // SAMPLE_WIDTH_BYTES
        if sample_count / SAMPLE_RATE < MIN_TRANSCRIBE_SECONDS:
            logger.debug("Ses çok kısa (%d örnek), Groq'a istek atılmadı.", sample_count)
            return ""

        if not self._api_key:
            raise CloudSpeechUnavailableError(
                "GROQ_API_KEY ortam değişkeni tanımlı değil; buluttan konuşma "
                "tanıma kullanılamıyor. https://console.groq.com/keys adresinden "
                "bir API anahtarı oluşturup GROQ_API_KEY ortam değişkenine atayın."
            )

        wav_bytes = pcm_to_wav(pcm_bytes)

        import requests  # lazy import: bu modül olmadan da proje import edilebilsin

        try:
            response = requests.post(
                _TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={
                    "model": self._model,
                    "language": "tr",
                    "prompt": hotwords or "",
                    "response_format": "json",
                },
                timeout=self._timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise CloudSpeechUnavailableError(
                f"Groq Whisper API zaman aşımına uğradı ({self._timeout:g} sn)."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise CloudSpeechUnavailableError(f"Groq Whisper API'ye ulaşılamadı: {exc}") from exc

        if response.status_code == 401:
            raise CloudSpeechUnavailableError(
                "Groq API anahtarı geçersiz (HTTP 401). GROQ_API_KEY değerini kontrol edin."
            )
        if response.status_code != 200:
            raise CloudSpeechUnavailableError(
                f"Groq Whisper API hata döndürdü (HTTP {response.status_code})."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudSpeechUnavailableError("Groq Whisper API'den geçersiz JSON yanıtı geldi.") from exc

        if not isinstance(payload, dict) or "text" not in payload:
            raise CloudSpeechUnavailableError(
                f"Groq Whisper API beklenmeyen bir yanıt döndürdü: {payload!r}"
            )

        return payload["text"].strip()

