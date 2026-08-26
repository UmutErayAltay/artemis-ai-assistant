"""Bulut tabanlı konuşma tanıma: Azure Cognitive Services Speech-to-Text REST API'si.

`voice.stt.SpeechToText` ile TAMAMEN aynı arayüze sahiptir
(`transcribe(pcm_bytes, hotwords=None) -> str`) — `voice/stt_cloud.py`daki
`GroqSpeechToText` ile birlikte "internet varsa bulut, yoksa yerel" hibrit
stratejisinde (`voice/router.py`) şeffafça takas edilebilecek üçüncü bir
sağlayıcı sunar. Hangi bulut sağlayıcısının (Groq/Azure) kullanılacağına
yönlendiriciyi kuran kod karar verir; bu modül yalnızca arayüz sözleşmesini
sağlar.

Bu yüzden burada fırlatılan HER başarısızlık (ağ hatası, zaman aşımı, HTTP
4xx/5xx, eksik/geçersiz anahtar ya da bölge, 60 saniyelik API sınırının
aşılması, bozuk/beklenmeyen yanıt) TEK bir istisna tipinde toplanır:
`CloudSpeechUnavailableError`. ÖNEMLİ: bu istisna burada yeniden
TANIMLANMAZ, `voice.stt_cloud`'dan İÇE AKTARILIR — böylece yönlendirici
hangi bulut sağlayıcısı kullanılırsa kullanılsın TEK bir
`except CloudSpeechUnavailableError` ile yerele düşebilir. Sessiz
başarısızlık YOKTUR: bir şey ters giderse her zaman dürüstçe bu istisna
fırlatılır, asla boş string ile örtbas edilmez — TEK istisna: Azure'un
"konuşma algılandı ama tanınamadı/hiç konuşma yok" anlamına gelen
durumları (`NoMatch`, `InitialSilenceTimeout`, `BabbleTimeout`) gerçek bir
ARIZA değildir, bu yüzden boş string döner (bkz. `transcribe`).

Yerel STT'den (`voice/stt.py`) farkı `GroqSpeechToText` ile birebir aynı:
burada yüklenecek bir model YOKTUR, gerçek tanıma işi Azure'un sunucusunda
yapılır; "lazy model yükleme"nin karşılığı burada da "lazy `requests`
import"udur. Ses hiçbir zaman diske yazılmaz; PCM->WAV dönüşümü tamamen
bellekte (`io.BytesIO`) yapılır — bkz. `voice/audio.py` modül
dokümantasyonundaki "ses verisi hiçbir zaman diske yazılmaz" ilkesi.

HOTWORDS NEDEN YOK SAYILIYOR: Azure'un REST kısa-ses tanıma uç noktası
ifade önceliklendirme (phrase list / hotwords) DESTEKLEMEZ; bu özellik
yalnızca ağır, WebSocket tabanlı Speech SDK'sında (`azure-cognitiveservices-
speech`) mevcuttur. Bu proje o SDK'yı KASITLI olarak kullanmıyor —
`GroqSpeechToText` ile aynı "tek HTTP isteği, ağır bağımlılık yok"
basitliği burada da tercih edildi. Bu yüzden `transcribe()` arayüz
uyumluluğu için `hotwords` parametresini kabul eder ama isteğe hiçbir
şekilde dahil etmez (sessizce değil — bkz. `transcribe` dokstringi ve
debug log satırı). İleride ağır SDK'ya geçilirse bu parametre orada
gerçek bir phrase list'e (`PhraseListGrammar`) dönüştürülüp
değerlendirilebilir.
"""

from __future__ import annotations

import logging
import os

from voice.audio import SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from voice.stt import MIN_TRANSCRIBE_SECONDS
from voice.stt_cloud import CloudSpeechUnavailableError
from voice.wav import pcm_to_wav

logger = logging.getLogger(__name__)

_RECOGNITION_URL_TEMPLATE = (
    "https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
)
"""Azure'un bölgesel kısa-ses tanıma uç noktası. `{region}` her istekte
gerçek bölge adıyla (örn. "westeurope") doldurulur — bölge, uç nokta
URL'sinin bir parçası olduğu için anahtar kadar zorunludur."""

_MAX_AUDIO_SECONDS = 60.0
"""Azure REST API'sinin tek istekte kabul ettiği azami ses uzunluğu. Bunun
üzerindeki ses sessizce kesilip yanlış/eksik bir sonuç döndürmek yerine
dürüstçe reddedilir (bkz. `transcribe`)."""

_NO_SPEECH_STATUSES = frozenset({"NoMatch", "InitialSilenceTimeout", "BabbleTimeout"})
"""`RecognitionStatus` bu değerlerden biriyse bu bir ARIZA değildir, yalnızca
"bir şey duyulmadı/anlaşılmadı" demektir — bkz. `transcribe`."""


class AzureSpeechToText:
    """Azure Cognitive Services Speech-to-Text REST API'siyle 16 kHz PCM sesi Türkçe metne çevirir.

    `voice.stt.SpeechToText` ile AYNI arayüze sahiptir
    (`transcribe(pcm_bytes, hotwords=None) -> str`) — yönlendirici bunu
    ve/ya da `GroqSpeechToText`'i şeffafça takas edebilir.

    Burada yüklenecek/bellekte tutulacak bir model YOKTUR: her
    `transcribe()` çağrısı, sesi bellekte bir WAV'a sarıp Azure'un
    bölgesel uç noktasına tek bir HTTP isteğiyle gönderir; gerçek tanıma
    işi tamamen uzakta yapılır.

    Args:
        api_key: Azure Speech kaynağının anahtarı. Verilmezse
            `AZURE_SPEECH_KEY` ortam değişkeninden okunur. ÖNEMLİ: anahtar
            burada, constructor'da HİÇ doğrulanmaz — yönlendiricinin bu
            nesneyi anahtar/bölge olmadan da (örn. henüz ayarlanmamışken)
            oluşturabilmesi için eksik/geçersiz anahtar ya da bölge
            yalnızca `transcribe()` çağrıldığında bir hataya dönüşür (bkz.
            o metodun dokstringi).
        region: Azure Speech kaynağının bölgesi (örn. "westeurope",
            "eastus"). Verilmezse `AZURE_SPEECH_REGION` ortam
            değişkeninden okunur.
        timeout: HTTP isteği için azami bekleme süresi (saniye).
        language: Tanıma dili (BCP-47 kodu). Azure'da bu ZORUNLU bir sorgu
            parametresidir (yoksa 4xx döner) — `voice.stt.SpeechToText.
            transcribe`'daki "dil algılamaya güvenilmez, sabit dil
            geçilir" kararıyla aynı gerekçeyle burada da varsayılan sabit
            "tr-TR"dir.
    """

    def __init__(
        self,
        api_key: str | None = None,
        region: str | None = None,
        timeout: float = 15.0,
        language: str = "tr-TR",
    ) -> None:
        self._api_key = api_key or os.environ.get("AZURE_SPEECH_KEY")
        self._region = region or os.environ.get("AZURE_SPEECH_REGION")
        self._timeout = timeout
        self._language = language

    def transcribe(self, pcm_bytes: bytes, hotwords: str | None = None) -> str:
        """16 kHz, tek kanal, 16-bit PCM sesi Azure Speech-to-Text REST API'siyle Türkçe metne çevirir.

        Args:
            pcm_bytes: `SpeechRecorder.audio_bytes` ile üretilmiş ham ses
                (bkz. `voice.stt.SpeechRecorder`).
            hotwords: KABUL EDİLİR ama YOK SAYILIR. Azure'un REST kısa-ses
                uç noktası ifade önceliklendirme (phrase list) DESTEKLEMEZ;
                bu yalnızca ağır Speech SDK'sında (`azure-cognitiveservices-
                speech`, WebSocket tabanlı) mevcuttur ve bu proje o SDK'yı
                kasıtlı olarak kullanmıyor (bkz. modül dokstringi). Parametre
                yalnızca `GroqSpeechToText`/`SpeechToText` ile arayüz
                uyumluluğu için burada durur; isteğe hiçbir şekilde dahil
                edilmez (verilirse debug seviyesinde loglanıp yok sayılır).
                İleride ağır SDK'ya geçilirse burada gerçek bir phrase
                list'e dönüştürülüp değerlendirilebilir.

        Returns:
            Tanınan metin, baş/son boşlukları temizlenmiş. Şu durumlarda
            ağa hiç istek atılmadan ya da hata fırlatılmadan boş string
            döner:
              - Ses boş ya da `MIN_TRANSCRIBE_SECONDS`'tan kısaysa (ağa
                hiç çıkılmaz).
              - Azure `RecognitionStatus` olarak `NoMatch`,
                `InitialSilenceTimeout` ya da `BabbleTimeout` döndürürse
                — bunlar ARIZA değil, "bir şey duyulmadı/anlaşılmadı"
                demektir; yönlendiricinin bu yüzden gereksiz yere yerele
                düşmesi istenmez.

        Raises:
            CloudSpeechUnavailableError: Ses 60 saniyeden uzunsa (Azure
                REST API'sinin sabit sınırı — bkz. modül dokstringi),
                `AZURE_SPEECH_KEY`/`AZURE_SPEECH_REGION` eksikse, ağ hatası
                ya da zaman aşımı olursa, Azure HTTP 4xx/5xx döndürürse,
                yanıt gövdesi ayrıştırılamazsa/beklenmeyen bir şekildeyse
                ya da `RecognitionStatus` olarak `Error` (ya da yukarıda
                sayılanlar dışında bilinmeyen bir değer) dönerse. Orijinal
                hata (varsa) `from exc` ile zincirlenir. Anahtar bu
                istisnanın hiçbir mesajında (ne tam ne kısmi) yer almaz.
                `voice.stt_cloud`'dan içe aktarılan TEK istisna tipidir
                (burada yeniden tanımlanmaz) — yönlendirici hangi bulut
                sağlayıcısı kullanılırsa kullanılsın tek bir `except` ile
                yerele düşebilir.
        """

        if not pcm_bytes:
            return ""

        sample_count = len(pcm_bytes) // SAMPLE_WIDTH_BYTES
        duration_seconds = sample_count / SAMPLE_RATE
        if duration_seconds < MIN_TRANSCRIBE_SECONDS:
            logger.debug("Ses çok kısa (%d örnek), Azure'a istek atılmadı.", sample_count)
            return ""

        if duration_seconds > _MAX_AUDIO_SECONDS:
            raise CloudSpeechUnavailableError(
                f"Ses {duration_seconds:.1f} saniye uzunluğunda; Azure Speech REST "
                f"API'si tek istekte en fazla {_MAX_AUDIO_SECONDS:.0f} saniyelik sesi "
                "kabul ediyor. Daha kısa bir parça gönderin."
            )

        if not self._api_key or not self._region:
            raise CloudSpeechUnavailableError(
                "AZURE_SPEECH_KEY ve/veya AZURE_SPEECH_REGION ortam değişkeni tanımlı "
                "değil; buluttan (Azure) konuşma tanıma kullanılamıyor. "
                "https://portal.azure.com adresinden bir Speech kaynağı oluşturup "
                "anahtarı AZURE_SPEECH_KEY, bölgeyi AZURE_SPEECH_REGION ortam "
                "değişkenlerine atayın."
            )

        if hotwords:
            logger.debug("hotwords Azure REST STT'de desteklenmiyor, yok sayıldı: %r", hotwords)

        wav_bytes = pcm_to_wav(pcm_bytes)

        import requests  # lazy import: bu modül olmadan da proje import edilebilsin

        url = _RECOGNITION_URL_TEMPLATE.format(region=self._region)

        try:
            response = requests.post(
                url,
                params={"language": self._language, "format": "simple"},
                headers={
                    "Ocp-Apim-Subscription-Key": self._api_key,
                    "Content-Type": f"audio/wav; codecs=audio/pcm; samplerate={SAMPLE_RATE}",
                    "Accept": "application/json",
                },
                data=wav_bytes,
                timeout=self._timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise CloudSpeechUnavailableError(
                f"Azure Speech API zaman aşımına uğradı ({self._timeout:g} sn)."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise CloudSpeechUnavailableError(f"Azure Speech API'ye ulaşılamadı: {exc}") from exc

        if response.status_code in (401, 403):
            raise CloudSpeechUnavailableError(
                f"Azure Speech API anahtarı geçersiz ya da eksik (HTTP {response.status_code}). "
                "AZURE_SPEECH_KEY ve AZURE_SPEECH_REGION değerlerini kontrol edin."
            )
        if response.status_code != 200:
            raise CloudSpeechUnavailableError(
                f"Azure Speech API hata döndürdü (HTTP {response.status_code})."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudSpeechUnavailableError("Azure Speech API'den geçersiz JSON yanıtı geldi.") from exc

        if not isinstance(payload, dict) or "RecognitionStatus" not in payload:
            raise CloudSpeechUnavailableError(
                f"Azure Speech API beklenmeyen bir yanıt döndürdü: {payload!r}"
            )

        status = payload["RecognitionStatus"]

        if status in _NO_SPEECH_STATUSES:
            logger.debug("Azure Speech API: konuşma algılanmadı/anlaşılmadı (RecognitionStatus=%s).", status)
            return ""

        if status == "Success":
            if "DisplayText" not in payload:
                raise CloudSpeechUnavailableError(
                    f"Azure Speech API beklenmeyen bir yanıt döndürdü: {payload!r}"
                )
            return payload["DisplayText"].strip()

        raise CloudSpeechUnavailableError(
            f"Azure Speech API tanıma hatası döndürdü (RecognitionStatus={status!r})."
        )

