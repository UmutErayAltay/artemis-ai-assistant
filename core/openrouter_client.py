"""OpenRouter'ın OpenAI-uyumlu API'siyle konuşan ince LLM istemcisi.

`core/llm_client.py::OllamaLLMClient` ile AYNI genel sözleşmeyi sunar
(`get_raw_response`, `get_tool_calls`, `should_engage`) — amaç, bulut
LLM'i ile yerel LLM'i `core/llm_router.py` katmanında şeffafça
takas edebilmek: OpenRouter çalışırken yerel model hiç yüklenmez.

İKİ KONU AYRI TUTULUR:

1. **Bu bir ikinci tool-call UYGULAMASI DEĞİLDİR.** Şema, düzeltme
   mesajı, doğrulama — hepsi `core.llm_client`'dan DOĞRUDAN import
   edilir (`OllamaLLMClient._response_schema()`,
   `OllamaLLMClient.extract_tool_calls`, `_COMMAND_GATE_*`,
   `_CORRECTION_MESSAGE`). README §16a'nın öğrettiği ders budur: JSON
   şeması ile sistem promptu AYNI sözleşmeyi konuşmak zorundadır; iki
   sağlayıcı için iki ayrı şema/ayrıştırıcı yazmak, ikisinin sessizce
   ayrışmasını ve modele "hangi biçim geçerli" diye iki farklı cevap
   verilmesini kaçınılmaz kılar. Ollama'nın `_response_schema()`'sı
   ayrıca `TOOL_REGISTRY`'nin güncel tool adlarını `enum` olarak
   dayattığı için iki sağlayıcıda da aynı araç kümesi görünür.

2. **Native tool-calling burada YOKTUR ve BİLEREK YOKTUR.** Ollama
   tarafındaki `tools=[...]` stratejisi, sağlayıcının/modelin
   fonksiyon-çağırma desteğine dayanır; OpenRouter'ın ücretsiz model
   kataloğunda bunun hangi modellerde çalıştığı, bu ortamda test
   edilemeyecek kadar değişken bir şeydir. Dördüncü bir strateji
   eklemek, çalışmayan bir yolu da "destekliyormuş gibi" denemekten
   ibaret olurdu. Bu yüzden yalnızca üç `response_format` seviyesi
   denenir; üçü de metin üzerinden AYNI ayrıştırıcıya düşer.

BULUT İSTEMCİSİ OLMANIN İKİ KURALI (voice/stt_cloud.py ile aynı):
    * Anahtar constructor'da DOĞRULANMAZ. Yönlendirici, anahtar
      tanımlı değilken de bu nesneyi kurabilmelidir (bkz.
      `config.settings.get_openrouter_api_key`); eksik/geçersiz anahtar
      yalnızca gerçek bir çağrıda hataya dönüşür.
    * Sessiz başarısızlık YOK. Ağ hatası, zaman aşımı, HTTP 4xx/5xx,
      eksik anahtar ve bozuk gövde HEPSİ tek bir istisnada
      (`OpenRouterUnavailableError`) toplanır ki `LLMRouter` tek bir
      `except` ile yerele düşebilsin. Boş string ile örtbas etmek
      dürüstçe başarısız olmaktan kötüdür (CLAUDE.md: koşulsuz
      `success=True` yasak).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.llm_client import (
    _COMMAND_GATE_PROMPT,
    _CORRECTION_MESSAGE,
    _GATE_ENGAGE,
    LLMResponseParseError,
    OllamaLLMClient,
)

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
"""OpenRouter'ın OpenAI-uyumlu sohbet uç noktası."""


class OpenRouterUnavailableError(Exception):
    """OpenRouter API kullanılamadığında fırlatılır (bkz. voice/stt_cloud.py::
    CloudSpeechUnavailableError — aynı 'tek şemsiye istisna' deseni: eksik/geçersiz
    anahtar, ağ hatası, zaman aşımı, HTTP 4xx/5xx hepsi burada toplanır ki
    core/llm_router.py tek bir except ile yerele düşebilsin)."""


class OpenRouterLLMClient:
    """OpenRouter'ın OpenAI-uyumlu API'siyle konuşan ince istemci.

    `OllamaLLMClient` ile AYNI genel sözleşmeye (`get_raw_response`,
    `get_tool_calls`, `should_engage`) sahiptir — `core/llm_router.py`
    ikisini şeffafça takas edebilir.

    Args:
        model: OpenRouter model slug'ı (örn. "meta-llama/llama-3.1-8b-instruct:free").
            `config/config.yaml::openrouter_model`'den gelir.
        api_key: Verilmezse constructor'da HİÇ doğrulanmaz (bkz. GroqSpeechToText'in
            aynı gerekçesi) — yönlendiricinin bu nesneyi anahtarsız da kurabilmesi için;
            eksik/geçersiz anahtar yalnızca gerçek bir çağrıda hataya dönüşür.
        timeout_seconds: HTTP isteği zaman aşımı (`openrouter_timeout_seconds`'den).
    """

    def __init__(self, model: str, api_key: str | None = None, timeout_seconds: float = 30.0) -> None:
        self.model = model
        self._api_key = (api_key or "").strip() or None
        self.timeout_seconds = timeout_seconds
        self._working_strategy_key: str | None = None

    def get_raw_response(self, system_prompt: str, user_input: str) -> str:
        """Ollama'nınkiyle birebir aynı imza/davranış: _chat çağırır, ham metni döner.

        Raises:
            OpenRouterUnavailableError: Anahtar yoksa, ağ hatası/zaman aşımı
                olursa ya da tüm format stratejileri tükenirse.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
        raw_text, _ = self._chat(messages)
        return raw_text

    def get_tool_calls(self, system_prompt: str, user_input: str, max_retries: int = 2) -> list[dict[str, Any]]:
        """OllamaLLMClient.get_tool_calls'ın BİREBİR aynı retry/correction döngüsü
        (_CORRECTION_MESSAGE'ı aynı şekilde ekleyip tekrar dener), ama native tool-calling
        yolu YOK — her zaman `extract_tool_calls(raw_text)`.

        Args:
            system_prompt: Tam sistem promptu (Ollama ile AYNI metin).
            user_input: Kullanıcının ham komutu.
            max_retries: Ayrıştırma başarısız olursa yapılacak ek deneme sayısı.

        Returns:
            Her biri `{"tool": ..., "arguments": {...}}` olan bir liste.

        Raises:
            LLMResponseParseError: Tüm denemelerden sonra da geçerli JSON
                çıkarılamazsa. `LLMRouter` bunu "bulut başarısız" sayıp
                (auto modda) yerele düşer.
            OpenRouterUnavailableError: Anahtar yoksa ya da API ulaşılamıyorsa.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        last_error: LLMResponseParseError | None = None
        for attempt in range(max_retries + 1):
            raw_text, _ = self._chat(messages)

            try:
                return OllamaLLMClient.extract_tool_calls(raw_text)
            except LLMResponseParseError as exc:
                last_error = exc
                logger.warning(
                    "Tool-call ayrıştırma denemesi %d/%d başarısız: %s", attempt + 1, max_retries + 1, exc
                )
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({"role": "user", "content": _CORRECTION_MESSAGE})

        assert last_error is not None  # döngü en az bir kez çalıştığı için garantili
        raise last_error

    def should_engage(self, user_input: str) -> bool:
        """Ollama'nınkiyle AYNI davranış sözleşmesi: boş girdi -> False; herhangi bir
        hata (anahtar yok, ağ hatası, geçersiz yanıt) -> KAPI AÇIK BAŞARISIZ OLUR, True
        döner (bkz. Ollama sürümünün docstring'indeki gerekçe — bu sözleşme değişmez).

        Kendi içinde try/except ile bunu garanti eder; hiçbir hatayı yukarı
        sızdırmaz. Bu yüzden `core/llm_router.py`'ın `_run`'ı bu metotta
        pratikte hiç devreye girmeyecektir — bu BİLİNÇLİDİR ve tersine bir
        arıza değildir: kapının bozulması asistanı tamamen kullanılamaz hale
        getirmemeli, yalnızca bu gürültü filtresini kaybetmeli. Ses işçisi
        (`core/voice_loop.py::_handle_one_command`) yalnızca bu metodu
        kullandığı için, istisna yukarı sızsa işçi ölürdü.

        Args:
            user_input: Konuşmadan çevrilmiş ham metin.

        Returns:
            True ise metin asistana yöneliktir, False ise gürültüdür.
        """

        text = user_input.strip()
        if not text:
            return False

        try:
            raw, _ = self._chat(
                [
                    {"role": "system", "content": _COMMAND_GATE_PROMPT},
                    {"role": "user", "content": text},
                ]
            )
            decision = json.loads(raw).get("karar")
        except Exception as exc:  # noqa: BLE001 - kapı bozulursa akış durmasın
            logger.warning("Komut kapısı çalıştırılamadı, girdi yönelik sayılıyor: %s", exc)
            return True

        engage = decision == _GATE_ENGAGE
        logger.info("Komut kapısı: %r -> %s", text, decision)
        return engage

    def _chat(self, messages: list[dict[str, str]]) -> tuple[str, None]:
        """Sırayla farklı stratejiler deneyerek tek bir "tur" chat isteği atar.

        Denenenler (bilinen çalışanı başa alma numarası dahil — OllamaLLMClient'ın
        `_strategies()` + `_working_strategy_key` fikri birebir aynı mantıkla
        kopyalanmıştır):

          1) "json_schema": response_format={"type":"json_schema","json_schema":
             {"name":"tool_calls","strict":True,"schema": OllamaLLMClient._response_schema()}}
          2) "json_object": response_format={"type":"json_object"}
          3) "none": response_format hiç gönderilmez (yalnızca prompt talimatı)

        HATA SINIFLANDIRMASI — bu ayrım kritiktir, `core/llm_router.py`'nin
        doğru çalışması (ve kullanıcının 60 saniye beklemekten kurtulması) buna
        bağlı:

          - Anahtar yok (`self._api_key` boş/None) -> HEMEN
            `OpenRouterUnavailableError`, hiçbir HTTP isteği atılmadan
            (Groq deseniyle aynı).
          - `requests.exceptions.Timeout` / `RequestException` (DNS, bağlantı
            reddi, vb.) -> `OpenRouterUnavailableError` — strateji denemeye
            devam ETMEZ. Ollama'nın `_CONNECTION_ERRORS` ayrımıyla AYNI
            mantık: sunucuya ulaşılamıyorsa başka bir format denemek
            anlamsız, sadece gecikmeyi katlar. (Ollama tarafındaki gerçek
            olay: sunucu kapalıyken düz bir bağlantı reddi dört stratejinin
            dördünde tekrarlanıyor, `get_tool_calls` de üç kez daha
            denediği için tek bir söz 12 çağrıya çıkabiliyordu.)
          - HTTP 401/403 -> `OpenRouterUnavailableError` (anahtar geçersiz),
            strateji denemeye devam ETMEZ (aynı sebep: format değil,
            kimlik doğrulama sorunu).
          - HTTP 402 -> `OpenRouterUnavailableError`, "ücretsiz kota tükendi
            ya da bu model artık ücretsiz değil" mesajıyla — bu, kullanıcının
            TAM OLARAK karşılaşacağı senaryo, mesaj somut ve yönlendirici
            olmalıdır. Strateji denemeye devam ETMEZ.
          - HTTP 429 -> `OpenRouterUnavailableError` (hız sınırı), strateji
            denemeye devam ETMEZ. v1 kapsamında geri çekilme (backoff) YOK;
            zaten yönlendiricinin soğuma mekanizması devrede.
          - HTTP 400/422 (bu strateji, örn. json_schema, bu model/sağlayıcı
            tarafından desteklenmiyor) -> STRATEJİ HATASI, sıradaki stratejiyi
            dene (Ollama'nın `except Exception: continue` deseniyle AYNI
            ayrım).
          - Diğer HTTP 4xx/5xx -> strateji hatası olarak say, devam et; TÜM
            stratejiler bitirse `OpenRouterUnavailableError("OpenRouter'a
            bağlanılamadı: ...")`.
          - Başarılı yanıt ama gövde ayrıştırılamıyor / `choices` yok /
            beklenmeyen şekil -> strateji hatası olarak say, devam et (bozuk
            bir yanıt genelde o formatı desteklemeyen bir modelden gelir).

        `requests` lazy import edilir (bkz. voice/stt_cloud.py deseni):
        bu modül, `requests` kurulu olmayan bir ortamda da import edilebilir
        kalmalıdır.

        Returns:
            (ham_metin, native_calls) çifti. `native_calls` bu istemcide her
            zaman `None` — OpenRouter yolunda native tool-calling YOKTUR.
            İkinci eleman yalnızca Ollama istemcisiyle imza simetrisi için
            vardır ve çağıran taraf daima yok sayar.

        Raises:
            OpenRouterUnavailableError: Anahtar eksikse, sunucuya ulaşılamıyorsa,
                kimlik doğrulama/kota/hız sınırı hatası alınırsa ya da tüm
                stratejiler tükenirse.
        """

        if not self._api_key:
            raise OpenRouterUnavailableError(
                "OPENROUTER_API_KEY tanımlı değil; bulut LLM kullanılamıyor. "
                "https://openrouter.ai/keys adresinden bir API anahtarı oluşturup "
                'OPENROUTER_API_KEY ortam değişkenine atayın (ya da config.yaml\'da '
                'llm_provider: "local" yapın).'
            )

        import requests  # lazy import: bu modül olmadan da proje import edilebilsin

        strategies = self._strategies()
        known = self._working_strategy_key
        if known is not None:
            # KİMLİK (`key`) saklanır, POZİSYON DEĞİL: liste her çağrıda
            # yeniden sıralanıyor, yani bir pozisyon farklı çağrılarda
            # farklı stratejilere karşılık gelebilir (bkz. Ollama'daki
            # `_working_strategy_key` gerekçesi). Karşılaştırma `item[0]`
            # ÜZERİNDEDİR: `strategies` (anahtar, ek_parametreler) ÇİFTLERİ
            # listeler, dolayısıyla `item != known` (tüm çifti dizeyle
            # karşılaştırmak) her zaman True döner ve liste hiç
            # sıralanmaz — sessizce "her çağrı şemayı yeniden dener"
            # durumuna düşer, yani bu satır olmasa da çalışıyor gibi
            # görünür.
            strategies.sort(key=lambda item: item[0] != known)

        last_exc: Exception | None = None
        for key, extra in strategies:
            try:
                response = requests.post(
                    _OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        # OpenRouter'ın isteğe bağlı uygulama tanımlama
                        # başlıkları: sağlayıcının panelinde "hangi uygulama"
                        # olarak görünmemizi ve trafiği bu projeye ayırmasını
                        # sağlar. Kimlik doğrulama YİNE de `Authorization`
                        # başlığındadır.
                        "X-Title": "Artemis",
                    },
                    json={"model": self.model, "messages": messages, "temperature": 0, **extra},
                    timeout=self.timeout_seconds,
                )
            except requests.exceptions.Timeout as exc:
                raise OpenRouterUnavailableError(
                    f"OpenRouter zaman aşımına uğradı ({self.timeout_seconds:g} sn)."
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise OpenRouterUnavailableError(f"OpenRouter'a ulaşılamadı: {exc}") from exc

            status = response.status_code

            if status in (401, 403):
                raise OpenRouterUnavailableError(
                    f"OpenRouter API anahtarı geçersiz (HTTP {status}). "
                    "OPENROUTER_API_KEY değerini kontrol edin."
                )
            if status == 402:
                raise OpenRouterUnavailableError(
                    f"OpenRouter ücretsiz kotası tükendi ya da bu model artık ücretsiz "
                    f"değil (HTTP 402): {self.model!r}. Ücretsiz modelleri "
                    "https://openrouter.ai/models?max_price=0 adresinden kontrol edin."
                )
            if status == 429:
                raise OpenRouterUnavailableError("OpenRouter hız sınırı uyguladı (HTTP 429).")
            if status != 200:
                last_exc = OpenRouterUnavailableError(f"OpenRouter HTTP {status} döndürdü.")
                logger.debug("Strateji basarisiz (%s): %s", extra, last_exc)
                continue

            try:
                raw = self._extract_content(response.json())
            except (KeyError, TypeError, IndexError, ValueError) as exc:
                last_exc = exc
                logger.debug("Strateji basarisiz (%s) — yanit ayristirilamadi: %s", key, exc)
                continue

            self._working_strategy_key = key
            return raw, None

        raise OpenRouterUnavailableError(f"OpenRouter'a bağlanılamadı ('{self.model}'): {last_exc}")

    @staticmethod
    def _strategies() -> list[tuple[str, dict[str, Any]]]:
        """Denenecek istek stratejilerini üretir (sıra sabittir; bilinen
        çalışan strateji `_chat` içinde başa alınır).

        ÜCRE seviye vardır ve HİÇBİRİ native tool-calling DEĞİLDİR: üçü de
        metin üzerinden AYNI ayrıştırıcıya (`extract_tool_calls`) düşer. Şema
        seviyesi Ollama'dan BİREBİR aynı şemayı kullanır; `strict: True`
        bayrağı her sağlayıcıda aynı yorumlanmadığı için desteklenmeyen
        bir sağlayıcı 400 döner ve `_chat` bir sonraki (daha gevşek) seviyeye
        düşer.
        """

        return [
            (
                "json_schema",
                {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "tool_calls",
                            "strict": True,
                            "schema": OllamaLLMClient._response_schema(),
                        },
                    }
                },
            ),
            ("json_object", {"response_format": {"type": "json_object"}}),
            ("none", {}),  # son çare: hiçbir kısıtlama yok, yalnızca prompt talimatına güven
        ]

    @staticmethod
    def _extract_content(response_json: dict) -> str:
        """`choices[0].message.content` yolunu güvenle çıkarır; eksikse/None ise
        `KeyError`/`TypeError`/`IndexError` fırlatabilir — çağıran bunu 'strateji hatası'
        olarak yakalar, bir istisna sınıfı icat etmeyin."""

        return response_json["choices"][0]["message"]["content"] or ""
