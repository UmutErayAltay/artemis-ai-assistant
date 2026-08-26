"""Bulut/yerel ses sağlayıcıları arasında otomatik geçiş.

Artemis'in ses katmanı hibrittir: internet varken bulut modelleri
(belirgin biçimde daha doğru, özellikle yabancı özel isimlerde), internet
yokken tamamen yerel modeller kullanılır. Bu modül o kararı veren tek
yerdir; `core/voice_loop.py` hangi sağlayıcının çalıştığını bilmez,
yalnızca `transcribe()` / `speak()` çağırır.

TASARIM KARARI — neden "önce ping atıp internet var mı bak" YAPILMIYOR:
    Bir bağlantı testi (a) her komuta gecikme ekler, (b) yanıltıcıdır —
    ping geçse bile API anahtarı geçersiz olabilir, servis 500 dönebilir,
    DNS çalışıp TLS patlayabilir. Bu yüzden yaklaşım "sor" değil "dene":
    doğrudan bulut denenir, HERHANGİ bir hata olursa sessizce yerele
    düşülür. Kullanıcı farkı yalnızca doğrulukta hisseder, akış hiç
    kesilmez.

TASARIM KARARI — neden SOĞUMA (cooldown) süresi var:
    Soğuma olmasaydı, internet tamamen kapalıyken HER komut önce buluta
    gidip zaman aşımını (10-15 sn) beklerdi; asistan kullanılamaz hale
    gelirdi. Bir bulut hatasından sonra bulut kısa süreliğine devre dışı
    bırakılır ve doğrudan yerele gidilir; süre dolunca bulut yeniden
    denenir (internet geri geldiyse kendiliğinden buluta döner).

YAN FAYDA: Bulut çalışırken yerel model HİÇ yüklenmez (sağlayıcılar lazy
kurulur). Yani internet varken Whisper'ın RAM maliyeti hiç ödenmez.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_COOLDOWN_SECONDS = 60.0
"""Bir bulut hatasından sonra bulutun devre dışı kalacağı süre."""


class ProviderMode:
    """`stt_provider` / `tts_provider` ayarının alabileceği değerler."""

    AUTO = "auto"  # önce bulut, hata olursa yerel (varsayılan)
    CLOUD = "cloud"  # yalnızca bulut; başarısız olursa dürüstçe başarısız ol
    LOCAL = "local"  # yalnızca yerel; hiçbir ses/metin dışarı çıkmaz

    ALL = (AUTO, CLOUD, LOCAL)


class _FallbackRouter:
    """Bulut→yerel geçiş mantığının ortak gövdesi.

    STT ve TTS yönlendiricileri yalnızca "hangi metodu çağıracağı"
    konusunda ayrışır; karar mantığı (mod, soğuma, hata yakalama) aynıdır
    ve burada tek bir yerde tutulur.

    TEŞHİS: Hangi sağlayıcının (bulut/yerel) kullanıldığı yalnızca hata
    durumunda değil, DEĞİŞTİĞİNDE de loglanır (bkz. `_note_provider_used`)
    — yoksa "ses neden robotik/yanlış" gibi sorular araştırılırken log'a
    bakıp bulut mu yerel mi kullanıldığını anlamanın hiçbir yolu olmuyordu.

    Args:
        cloud_factory: Bulut sağlayıcısını üreten fonksiyon (lazy —
            yalnızca gerçekten gerekince çağrılır).
        local_factory: Yerel sağlayıcıyı üreten fonksiyon (lazy).
        mode: `ProviderMode` değerlerinden biri.
        failure_cooldown: Bulut hatasından sonra bulutun atlanacağı süre.
        label: Log satırlarında bu yönlendiricinin STT mi TTS mi
            olduğunu ayırt etmek için kullanılan kısa etiket (örn.
            "STT", "TTS"). Boş bırakılırsa log satırına eklenmez.
    """

    def __init__(
        self,
        cloud_factory: Callable[[], Any],
        local_factory: Callable[[], Any],
        mode: str = ProviderMode.AUTO,
        failure_cooldown: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
        label: str = "",
    ) -> None:
        if mode not in ProviderMode.ALL:
            raise ValueError(f"Geçersiz sağlayıcı modu: {mode!r}. Beklenen: {ProviderMode.ALL}")

        self._cloud_factory = cloud_factory
        self._local_factory = local_factory
        self._mode = mode
        self._failure_cooldown = failure_cooldown
        self._label = label

        self._cloud: Any = None
        self._local: Any = None
        self._cloud_blocked_until = 0.0
        # Son BAŞARIYLA kullanılan sağlayıcı ("bulut"/"yerel"). Yalnızca
        # bundan FARKLI bir sağlayıcı kullanıldığında log yazılır — her
        # çağrıda yazmak, her komutta aynı satırı tekrarlayıp log'u şişirir.
        self._last_used_provider: str | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def _cloud_allowed(self) -> bool:
        """Bu an bulut denenmeli mi?"""

        if self._mode == ProviderMode.LOCAL:
            return False
        if self._mode == ProviderMode.CLOUD:
            return True
        return time.monotonic() >= self._cloud_blocked_until

    def _note_cloud_failure(self, exc: Exception) -> None:
        """Bulut hatasını kaydeder ve bulutu geçici olarak devre dışı bırakır."""

        self._cloud_blocked_until = time.monotonic() + self._failure_cooldown
        logger.warning(
            "Bulut sağlayıcı başarısız (%s); %.0f saniye boyunca yerel kullanılacak. Sebep: %s",
            type(exc).__name__,
            self._failure_cooldown,
            exc,
        )

    def _note_provider_used(self, provider_name: str) -> None:
        """Bir sağlayıcı BAŞARIYLA kullanıldığında, önceki kullanılan
        sağlayıcıdan FARKLIYSA bir INFO log satırı yazar.

        Bilinçli olarak her çağrıda değil, yalnızca DEĞİŞİMDE loglanır:
        aksi halde bu satır her komutta tekrarlanıp log dosyasını şişirir.
        Amaç, "ses neden robotik/yanlış" gibi sorular araştırılırken hangi
        sağlayıcının (bulut/yerel) devrede olduğunun log'dan hemen
        anlaşılabilmesidir — önceden bu yalnızca hata durumunda görünürdü.

        Args:
            provider_name: "bulut" ya da "yerel".
        """

        if provider_name == self._last_used_provider:
            return
        self._last_used_provider = provider_name

        if self._label:
            logger.info("Ses sağlayıcı (%s): %s", self._label, provider_name)
        else:
            logger.info("Ses sağlayıcı: %s", provider_name)

    def _get_cloud(self) -> Any:
        if self._cloud is None:
            self._cloud = self._cloud_factory()
        return self._cloud

    def _get_local(self) -> Any:
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def _run(self, call: Callable[[Any], Any]) -> Any:
        """Önce buluta, gerekirse yerele aynı işi yaptırır.

        Args:
            call: Bir sağlayıcı alıp asıl işi yapan fonksiyon.

        Returns:
            İlk başarılı sağlayıcının sonucu.

        Raises:
            Exception: `mode == CLOUD` iken bulut başarısız olursa hata
                yutulmaz — kullanıcı "yalnızca bulut" dediyse sessizce
                yerele düşmek onun tercihini çiğnemek olurdu.
        """

        if self._cloud_allowed():
            try:
                result = call(self._get_cloud())
            except Exception as exc:
                if self._mode == ProviderMode.CLOUD:
                    raise
                self._note_cloud_failure(exc)
            else:
                self._note_provider_used("bulut")
                return result

        result = call(self._get_local())
        self._note_provider_used("yerel")
        return result


class SpeechToTextRouter(_FallbackRouter):
    """Konuşma tanımayı bulut veya yerel sağlayıcıya yönlendirir.

    Her iki sağlayıcı da `transcribe(pcm_bytes, hotwords=None) -> str`
    sözleşmesini uygular (bkz. `voice/stt.py` ve `voice/stt_cloud.py`),
    bu yüzden burada tip kontrolü değil, ördek tiplemesi yeterlidir.
    """

    def __init__(
        self,
        cloud_factory: Callable[[], Any],
        local_factory: Callable[[], Any],
        mode: str = ProviderMode.AUTO,
        failure_cooldown: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
    ) -> None:
        # Etiket burada SABİT olarak "STT" verilir (çağırana bırakılmaz):
        # bir SpeechToTextRouter her zaman STT'dir, bunu her çağrı
        # noktasında hatırlatmak gereksiz tekrar olurdu.
        super().__init__(cloud_factory, local_factory, mode=mode, failure_cooldown=failure_cooldown, label="STT")

    def transcribe(self, pcm_bytes: bytes, hotwords: str | None = None) -> str:
        """Sesi metne çevirir; bulut başarısız olursa yerele düşer."""

        return self._run(lambda provider: provider.transcribe(pcm_bytes, hotwords=hotwords))


class TextToSpeechRouter(_FallbackRouter):
    """Sesli okumayı bulut veya yerel sağlayıcıya yönlendirir.

    Her iki sağlayıcı da `speak(text, on_amplitude=None)` ve `stop()`
    sözleşmesini uygular (bkz. `voice/tts.py` ve `voice/tts_cloud.py`).
    """

    def __init__(
        self,
        cloud_factory: Callable[[], Any],
        local_factory: Callable[[], Any],
        mode: str = ProviderMode.AUTO,
        failure_cooldown: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(cloud_factory, local_factory, mode=mode, failure_cooldown=failure_cooldown, label="TTS")

    def speak(self, text: str, on_amplitude: Callable[[float], None] | None = None) -> None:
        """Metni sesli okur; bulut başarısız olursa yerele düşer."""

        self._run(lambda provider: provider.speak(text, on_amplitude=on_amplitude))

    def stop(self) -> None:
        """Çalmayı keser. Hangi sağlayıcılar kurulduysa hepsine iletilir.

        Kurulmamış (hiç kullanılmamış) sağlayıcı için fabrika ÇAĞRILMAZ —
        yalnızca durdurmak için Piper modelini belleğe almak anlamsız olurdu.
        """

        for provider in (self._cloud, self._local):
            if provider is None:
                continue
            try:
                provider.stop()
            except Exception:
                logger.debug("Sağlayıcı durdurulurken hata yoksayıldı.", exc_info=True)
