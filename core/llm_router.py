"""Bulut (OpenRouter) ve yerel (Ollama) LLM sağlayıcıları arasında geçiş.

Artemis'in beyni artık hibrittir: sesin (STT/TTS) hibrit olduğu gibi
konuşma da iki yerden beslenebilir — buluttaki OpenRouter (isterseniz
ücretsiz bir modelle) ya da yereldeki Ollama. Bu modül o kararı veren
tek yerdir; `core/conversation_loop.py` ve `core/voice_loop.py` hangi
sağlayıcının konuştuğunu BİLMEZ, yalnızca `get_tool_calls()` /
`should_engage()` / `get_raw_response()` çağırır (bkz. `core/llm_types.py`).

Neden `voice/router.py::_FallbackRouter` KOPYALANMADI, ayrı bir sınıf
yazıldı: `_FallbackRouter`, `transcribe(pcm, hotwords=...)` ve
`speak(text, on_amplitude=...)` imzaları etrafında kurulmuş iki metotluk
(artı `stop`) bir gövde; LLM'in sözleşmesi üç metot ve üçü de
argümanını kendi imzasına taşıyor. Ortak taban sınıfa zorla bir "call"
parametresi sokmak, kazanç sağlamadan üç metotlu sözleşmeyi tek satıra
indirgirdi. Karar mantığı (mod, soğuma, hata yakalama, değişim logu)
burada AYNI düşünceyle, aynı adlarla tekrar edilmiştir — bu kasıtlı bir
tekrar: sesin kararı ile beynin kararı aynı soruyu sorarlar ama
farklı yerde yaşarlar, birleştirmek ikisini de zorlaştırırdı.

TASARIM KARARI — neden "önce ping atıp internet var mı bak" YAPILMIYOR:
    `voice/router.py`'deki gerekçenin aynısı, ama burada daha da sert:
    bir LLM'e giden istekin "erdoğruluk" testi (ping) hiçbir şey
    kanıtlamaz; anahtar olmayabilir, model slug'ı yazım hatası olabilir,
    kota tükenmiş olabilir. Yaklaşım yine "sor" değil "dene".

TASARIM KARARI — neden SOĞUMA (cooldown) süresi var:
    İnternet tamamen kapalıyken HER komut önce buluta gidip
    `openrouter_timeout_seconds` (30 sn) beklerdi; asistan kullanılamaz
    hale gelirdi. Bir bulut hatasından sonra bulut kısa süreliğine devre
    dışı bırakılır ve doğrudan yerele gidilir; süre dolunca bulut yeniden
    denenir (internet geri geldiyse kendiliğinden buluta döner).

YAN FAYDA — "bilgisayarı kastırmadan kullanma": Bulut çalışırken yerel
model HİÇ yüklenmez. Ollama'nın ağırlıkları ilk gerçek `chat()` çağrısında
VRAM/RAM'e alındığı için (bkz. `core/ollama_manager.py`), yönlendirici
bulutu bir kez dener ve işe yararsa `local_factory` HİÇ ÇAĞRILMAZ —
tek bir `llama`-sınıfı model belleğe hiç dokunulmadan gün boyu
konuşulabilir. (Model SEÇME menüsü yine de sorulur; o bir saniyelik
konsol etkileşimidir, bellek maliyeti taşımaz — gerekçesi `main.py`.)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from core.llm_types import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_COOLDOWN_SECONDS = 60.0
"""Bir bulut hatasından sonra bulutun devre dışı kalacağı süre."""


class LLMProviderMode:
    """`llm_provider` ayarının alabileceği değerler."""

    AUTO = "auto"  # önce bulut, hata olursa yerel (varsayılan)
    CLOUD = "cloud"  # yalnızca bulut; başarısız olursa dürüstçe başarısız ol
    LOCAL = "local"  # yalnızca yerel; hiçbir metin dışarı çıkmaz

    ALL = (AUTO, CLOUD, LOCAL)


class LLMRouter:
    """LLM çağrılarını bulut (OpenRouter) veya yerel (Ollama) sağlayıcıya yönlendirir.

    Kendisi de `core/llm_types.py::LLMClient` sözleşmesini UYGULAR: dolaşım
    döngüleri bu sınıfı `OllamaLLMClient`'in yerine geçen bir şey olarak
    alabilir ve aradaki farkı (bulut/yerel) asla öğrenmez.

    Args:
        cloud_factory: Bulut istemcisini üreten fonksiyon (lazy — yalnızca
            gerçekten bulut denendiğinde çağrılır).
        local_factory: Yerel istemciyi üreten fonksiyon (lazy).
        mode: `LLMProviderMode` değerlerinden biri.
        failure_cooldown: Bulut hatasından sonra bulutun atlanacağı süre.
    """

    def __init__(
        self,
        cloud_factory: Callable[[], LLMClient],
        local_factory: Callable[[], LLMClient],
        mode: str = LLMProviderMode.AUTO,
        failure_cooldown: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
    ) -> None:
        if mode not in LLMProviderMode.ALL:
            raise ValueError(f"Geçersiz sağlayıcı modu: {mode!r}. Beklenen: {LLMProviderMode.ALL}")

        self._cloud_factory = cloud_factory
        self._local_factory = local_factory
        self._mode = mode
        self._failure_cooldown = failure_cooldown

        self._cloud: LLMClient | None = None
        self._local: LLMClient | None = None
        self._cloud_blocked_until = 0.0
        # Son BAŞARIYLA kullanılan sağlayıcı. Yalnızca bundan FARKLI bir
        # sağlayıcı kullanıldığında log yazılır — her çağrıda yazmak, her
        # komutta aynı satırı tekrarlayıp log'u şişirir.
        self._last_used_provider: str | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def get_raw_response(self, system_prompt: str, user_input: str) -> str:
        """Düz metin cevap ister; bulut başarısız olursa yerele düşer."""

        return self._run(lambda provider: provider.get_raw_response(system_prompt, user_input))

    def get_structured_response(
        self, system_prompt: str, user_input: str, schema: dict[str, Any], schema_name: str = "cevap"
    ) -> dict[str, Any]:
        """Şemalı tek bir JSON nesnesi ister; bulut başarısız olursa yerele düşer."""

        return self._run(
            lambda provider: provider.get_structured_response(system_prompt, user_input, schema, schema_name)
        )

    def get_tool_calls(
        self, system_prompt: str, user_input: str, max_retries: int = 2
    ) -> list[dict[str, Any]]:
        """Tool-call listesi ister; bulut başarısız olursa yerele düşer."""

        return self._run(
            lambda provider: provider.get_tool_calls(system_prompt, user_input, max_retries=max_retries)
        )

    def should_engage(self, user_input: str) -> bool:
        """Komut kapısını sorar; bulut başarısız olursa yerele düşer."""

        return self._run(lambda provider: provider.should_engage(user_input))

    def _cloud_allowed(self) -> bool:
        """Bu an bulut denenmeli mi?"""

        if self._mode == LLMProviderMode.LOCAL:
            return False
        if self._mode == LLMProviderMode.CLOUD:
            return True
        return time.monotonic() >= self._cloud_blocked_until

    def _note_cloud_failure(self, exc: Exception) -> None:
        """Bulut hatasını kaydeder ve bulutu geçici olarak devre dışı bırakır."""

        self._cloud_blocked_until = time.monotonic() + self._failure_cooldown
        logger.warning(
            "Bulut LLM başarısız (%s); %.0f saniye boyunca yerel model kullanılacak. Sebep: %s",
            type(exc).__name__,
            self._failure_cooldown,
            exc,
        )

    def _note_provider_used(self, provider_name: str) -> None:
        """Bir sağlayıcı BAŞARIYLA kullanıldığında, önceki kullanılan
        sağlayıcıdan FARKLIYSA bir INFO log satırı yazar.

        Bilinçli olarak her çağrıda değil, yalnızca DEĞİŞİMDE loglanır:
        aksi halde bu satır her komutta tekrarlanıp log dosyasını şişirir.
        Amaç, "cevap neden bekledi/kötü geldi" gibi sorular araştırılırken
        hangi sağlayıcının devrede olduğunun log'dan hemen anlaşılabilmesi.
        `voice/router.py` ile birebir aynı gerekçe, aynı davranış.

        Args:
            provider_name: "bulut (OpenRouter)" ya da "yerel (Ollama)".
        """

        if provider_name == self._last_used_provider:
            return
        self._last_used_provider = provider_name
        logger.info("LLM sağlayıcı: %s", provider_name)

    def _get_cloud(self) -> LLMClient:
        if self._cloud is None:
            self._cloud = self._cloud_factory()
        return self._cloud

    def _get_local(self) -> LLMClient:
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    def _run(self, call: Callable[[LLMClient], Any]) -> Any:
        """Önce buluta, gerekirse yerele aynı işi yaptırır.

        `except` KAPSAMI BİLEREK GENİŞTİR: burada `LLMResponseParseError`
        (ücretsiz model, kendi düzeltme denemelerinden sonra da geçerli
        JSON üretemedi) da bir bağlantı hatası GİBİ ele alınır ve AUTO
        modunda yerele düşülür. Bu bilinçlidir: kullanıcı "şunu yap"
        dediğinde soru, cevabın nereden geldiği değil, bir şekilde doğru
        cevabı almaktır; ayrıştırılamayan bir bulut cevabı üzerinde
        üstelik bir de hata mesajı göstermek, aynı komutu yerelde
        çalıştırmaktan kötüdür. CLOUD modunda bu yutulmaz — hata
        dürüstçe yukarı çıkar (aşağıdaki "Raises" bölümü).

        Args:
            call: Bir istemci alıp asıl işi yapan fonksiyon.

        Returns:
            İlk başarılı sağlayıcının sonucu.

        Raises:
            Exception: `mode == CLOUD` iken bulut başarısız olursa hata
                yutulmaz — kullanıcı "yalnızca bulut" dediyse sessizce
                yerele düşmek onun tercihini çiğnemek olurdu. Aynı şekilde
                `mode == LOCAL` iken yerelin kendi hatası da yutulmaz.
        """

        if self._cloud_allowed():
            try:
                result = call(self._get_cloud())
            except Exception as exc:
                if self._mode == LLMProviderMode.CLOUD:
                    raise
                self._note_cloud_failure(exc)
            else:
                self._note_provider_used("bulut (OpenRouter)")
                return result

        result = call(self._get_local())
        self._note_provider_used("yerel (Ollama)")
        return result
