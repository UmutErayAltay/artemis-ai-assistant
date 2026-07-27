"""Yerel Ollama modeliyle iletişim katmanı.

LLM burada da başka hiçbir yere dokunmaz: yalnızca (sistem promptu +
kullanıcı metni) alır, ham metin veya yapılandırılmış tool-call döndürür.
Modelin "kirli" çıktısını (markdown code-fence, fazladan açıklama cümlesi
vb.) temizleyip geçerli bir tool-call listesine çevirme işi de burada
yapılır — dispatcher'a her zaman temiz, doğrulanmış bir liste ulaşır.

GÜVENİLİRLİK STRATEJİLERİ (sırayla denenir, ilk başarılı olan kullanılır;
küçük/yerel modeller talimatlara %100 sadık kalmayabildiği için tek bir
önleme güvenilmez):

    1) Native tool-calling (opsiyonel, `use_native_tool_calling=True`):
       Ollama'nın `tools=[...]` (OpenAI-tarzı function-calling)
       parametresiyle model, destekliyorsa doğrudan yapılandırılmış
       `message.tool_calls` alanı döndürür — metin ayrıştırmaya hiç
       gerek kalmaz. Model desteklemiyorsa (parametreyi yok sayıp düz
       metinle cevap verirse) otomatik olarak metin tabanlı akışa düşülür.
    2) Şema-kısıtlı format: Ollama'nın "structured outputs" özelliğiyle
       (`format=<json-schema>`) modelin çıktısı, geçerli tool adlarını
       (registry'den enum olarak) ve `{"tool": ..., "arguments": {...}}`
       zarfını dayatan bir JSON şemasına göre KISITLANIR.
    3) Genel `format="json"` moduna düşüş (şema formatını desteklemeyen
       eski Ollama/model kombinasyonları için).
    4) Hiçbir kısıtlama olmadan son çare denemesi (yalnızca sistem
       promptundaki talimata güvenir).
    5) Düzeltme-istekli retry: Ayrıştırılamayan bir çıktı gelirse, modele
       "bu geçersizdi, düzelt" mesajıyla sınırlı sayıda (varsayılan 2)
       tekrar hakkı verilir.

Ayrıca tüm denemelerde `temperature=0` kullanılır (tool-calling
belirleyici/deterministic olmalı, yaratıcılık gerekmez).

DÜŞÜNME MODU KAPALI (`think=False`): Ollama, düşünme yeteneği olan
modellerde (gemma4, qwen3.5, gpt-oss...) bu alan gönderilmezse varsayılan
olarak `think=True` uygular. Tool seçimi düşünme gerektirmeyen bir
sınıflandırma işidir; model cevaptan önce boşuna token yakar ve
kullanıcı bunu doğrudan bekleme süresi olarak hisseder. Ölçüm (10 gerçek
Türkçe sesli komut, sıcak model, RTX 4050):

    gemma4:e4b   4.14 sn -> 1.17 sn   doğruluk 10/10 (değişmedi)
    qwen3.5:4b   4.57 sn -> 1.07 sn   doğruluk 10/10 (değişmedi)

Yani ~3.5 kat hızlanma, doğruluktan hiçbir şey kaybetmeden. Düşünme
yeteneği OLMAYAN modeller (llama3.1, phi4-mini, qwen2.5) `think=False`
alanını sorunsuz kabul edip yok sayar, bu yüzden koşulsuz gönderilir.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.manifest import build_tool_manifest
from core.plugin_loader import TOOL_REGISTRY

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)

_GATE_ENGAGE = "YONELIK"
_GATE_NOISE = "GURULTU"

_COMMAND_GATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"karar": {"type": "string", "enum": [_GATE_ENGAGE, _GATE_NOISE]}},
    "required": ["karar"],
}

_COMMAND_GATE_PROMPT = f"""Sen bir sesli asistanın filtresisin. Sana kullanıcının \
mikrofonundan geçen bir metin verilecek. Görevin TEK bir şeye karar vermek: bu metin \
ASİSTANA YÖNELİK mi, yoksa asistanın hiç karışmaması gereken bir GÜRÜLTÜ mü?

{_GATE_ENGAGE} = kullanıcı açıkça asistanla konuşuyor: bir İŞLEM istiyor (uygulama \
aç/kapat, dosya/klasör oluştur/sil/aç, site aç, ara, ekran görüntüsü, ses/parlaklık), \
bir SORU soruyor ("sen kimsin?"), SOHBET ediyor ("teşekkürler") ya da isteği \
BELİRSİZ ama yine de açıkça asistana söylenmiş ("bir şeyler aç", "evi kapat").

{_GATE_NOISE} = yarım/anlamsız cümle, arka planda konuşma, başkasıyla konuşma — \
asistana söylendiği belli bile değil.

Şüphedeysen {_GATE_NOISE} de."""
"""Komut kapısının sistem promptu (bkz. `should_engage`).

Kasıtlı olarak TEK bir soru sorar ve yalnızca iki cevaba izin verir; şema
kısıtı sayesinde model başka bir şey üretemez. "Şüphedeysen GURULTU de"
satırı önemlidir: rastgele bir tool seçmek, sessiz kalmaktan kötüdür.

NEDEN "KOMUT mu?" DEĞİL "ASİSTANA YÖNELİK Mİ?" (v3.3, README §20f):
    Önceki sürüm burada KOMUT/KOMUT_DEGIL diye ayırıyordu ve "soru,
    sohbet"i de KOMUT_DEGIL sayıp tool seçimine hiç göndermiyordu.
    Sonuç: "sen kimsin?" -> "Bir komut duymadım." — halbuki
    `prompts/system_prompt.md` bu GİRDİ İÇİN AÇIK bir örnek içeriyor
    (assistant.reply -> "Ben Artemis..."). Aynı kırık sınır "evi kapat"
    ve "sosyal medyayı aç" örneklerini de vuruyordu (o örneklerde de
    system_prompt.md açıkça assistant.reply ile netleştirici bir soru
    bekliyor). Yani sınır YANLIŞ yerdeydi: "işlem mi" değil, "asistana
    yönelik mi" olmalıydı.

NEDEN ÜÇE BÖLÜNMEDİ (komut / sohbet / gürültü):
    `assistant.reply` sıradan bir tool gibi normal tool seçiminden
    çıkıyor (bkz. system_prompt.md kural 3) — yani "işlem" ile "sohbet"
    downstream'de AYNI yola gidiyor, hiçbir davranış farkı yok. Bu
    ayrımı kapıya taşımak, modele üçüncü belirsiz bir kategori
    ekleyip yalnızca doğruluğu düşürürdü: küçük modeller ikili
    sınıflandırmada çok daha iyi (aşağıdaki not, 11/12). Gerçek ayrım
    tek bir yerde gerekli: asistana yönelik mi, değil mi."""

MAX_PLAN_STEPS = 5
"""Tek bir komuttan üretilebilecek en fazla tool çağrısı sayısı.

Güvenlik sınırıdır (bkz. `_response_schema`): gürültülü/anlaşılamayan bir
girdide model kaçağa geçip onlarca adımlık yıkıcı bir plan üretebiliyor.
Gerçek bir sesli komut bu sayıyı pratikte aşmaz.
"""

_CORRECTION_MESSAGE = (
    "Bu çıktı geçerli değildi. YALNIZCA şu formatta, başka hiçbir açıklama "
    'olmadan bir JSON listesi döndür: [{"tool": "<tool_adı>", "arguments": {...}}]'
)


class LLMResponseParseError(Exception):
    """LLM çıktısından geçerli bir tool-call JSON'u çıkarılamadığında fırlatılır."""


class OllamaLLMClient:
    """Yerel Ollama modeliyle konuşan ince (thin) istemci.

    Attributes:
        model: Kullanılacak Ollama model adı (örn. "llama3.1").
            `config/config.yaml` içindeki `ollama_model` alanından gelir.
        use_native_tool_calling: True ise önce Ollama'nın native
            `tools=[...]` mekanizması denenir (bkz. modül dokümantasyonu,
            strateji 1). `config/config.yaml::use_native_tool_calling`
            alanından gelir.
        keep_alive: Modelin son kullanımdan sonra RAM'de tutulacağı süre
            (örn. "5m", "30s", "0"). RAM tasarrufu için kısa tutulabilir;
            bkz. `config/config.yaml::ollama_keep_alive`.
    """

    def __init__(self, model: str, use_native_tool_calling: bool = False, keep_alive: str = "5m") -> None:
        self.model = model
        self.use_native_tool_calling = use_native_tool_calling
        self.keep_alive = keep_alive

    def get_raw_response(self, system_prompt: str, user_input: str) -> str:
        """Ollama'ya sistem promptu + kullanıcı mesajını gönderir, ham metni döndürür.

        Native tool-calling aktifse ve model bunu desteklerse, dönen
        `tool_calls` yoksayılır; bu metot her zaman `message.content`'i
        döndürür (ham metin ayrıştırma senaryoları için).

        Raises:
            ConnectionError: Ollama sunucusuna ulaşılamazsa veya model yüklü değilse.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
        raw_text, _ = self._chat(messages)
        return raw_text

    def get_tool_calls(self, system_prompt: str, user_input: str, max_retries: int = 2) -> list[dict[str, Any]]:
        """Modelden tool-call(lar)ı alır ve bir sözlük listesine ayrıştırır.

        Args:
            system_prompt: Tam sistem promptu.
            user_input: Kullanıcının ham komutu.
            max_retries: Metin ayrıştırma başarısız olursa yapılacak ek deneme sayısı.

        Returns:
            Her biri `{"tool": ..., "arguments": {...}}` olan bir liste.

        Raises:
            LLMResponseParseError: Tüm denemelerden sonra da geçerli JSON
                çıkarılamazsa.
            ConnectionError: Ollama'ya ulaşılamazsa.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        last_error: LLMResponseParseError | None = None
        for attempt in range(max_retries + 1):
            raw_text, native_calls = self._chat(messages)

            if native_calls is not None:
                return native_calls

            try:
                return self.extract_tool_calls(raw_text)
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
        """Verilen metnin ASİSTANA YÖNELİK olup olmadığına karar verir.

        NEDEN AYRI BİR ADIM — ölçülmüş bir arızanın çözümü:

            Tool seçimi sırasında model bir tool üretmek ZORUNDADIR
            (şema `minItems: 1`). Bu yüzden asistana yönelik olmayan
            girdilerde rastgele bir tool seçiyordu. Gerçek kullanımdan:

                "Sen kimsin?"        -> masaüstünde example.txt oluşturdu
                "Abi insanlarız ki?" -> dosya oluşturdu + tarayıcı açtı
                (arka plan sohbeti)  -> 25 adımlık plan, içinde delete vardı

            Prompt'a kural ve örnek eklemek yetmedi (llama3.1:8b ile 5/10),
            daha büyük model de çözmedi (qwen2.5:14b ile 4/10 ve 10 kat
            yavaş). Çünkü sorun bilgi eksikliği değil, modelin "hiçbir şey
            yapma" seçeneğini seçmekte zorlanması.

            Aynı modele TEK ve İKİLİ bir soru sorulduğunda ise sonuç
            11/12 (soru başına ~1.4 sn). Küçük modeller ikili
            sınıflandırmada, açık uçlu tool seçiminden çok daha iyidir —
            bu yüzden karar ayrı bir çağrıya alındı.

        SINIR "KOMUT MU" DEĞİL "YÖNELİK Mİ" (v3.3): bkz.
        `_COMMAND_GATE_PROMPT` üzerindeki not — bu metot bir dönem
        `is_actionable_command` adıyla ikili "komut/komut değil" sorusu
        soruyordu ve "sen kimsin?" gibi soruları da bloke ediyordu.

        Args:
            user_input: Konuşmadan çevrilmiş ham metin.

        Returns:
            True ise metin asistana yöneliktir ve tam tool seçimine
            geçilebilir (işlem de olsa, `assistant.reply` gerektiren bir
            soru/sohbet de olsa — ikisi de aynı yoldan geçer). Karar
            verilemezse (ağ/model hatası) True döner — kapı AÇIK
            başarısız olur: kapının bozulması asistanı tamamen
            kullanılamaz hale getirmemeli, yalnızca bu korumayı kaybeder.
        """

        import ollama  # lazy import

        text = user_input.strip()
        if not text:
            return False

        try:
            response = ollama.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": _COMMAND_GATE_PROMPT},
                    {"role": "user", "content": text},
                ],
                format=_COMMAND_GATE_SCHEMA,
                options={"temperature": 0},
                keep_alive=self.keep_alive,
                think=False,
            )
            decision = json.loads(response["message"]["content"]).get("karar")
        except Exception as exc:  # noqa: BLE001 - kapı bozulursa akış durmasın
            logger.warning("Komut kapısı çalıştırılamadı, girdi yönelik sayılıyor: %s", exc)
            return True

        engage = decision == _GATE_ENGAGE
        logger.info("Komut kapısı: %r -> %s", text, decision)
        return engage

    def _chat(self, messages: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]] | None]:
        """Ollama'ya, sırayla farklı stratejiler deneyerek tek bir "tur" chat isteği atar.

        Returns:
            (ham_metin, native_tool_calls) çifti. Native tool-calling
            devrede değilse veya model onu desteklemiyorsa `native_tool_calls`
            None döner ve çağıran taraf `extract_tool_calls` ile metni
            kendi ayrıştırmalıdır.
        """

        import ollama  # lazy import: bu modül olmadan da proje import edilebilsin

        strategies: list[dict[str, Any]] = []
        if self.use_native_tool_calling:
            strategies.append({"tools": self._build_native_tools()})
        strategies.append({"format": self._response_schema()})
        strategies.append({"format": "json"})
        strategies.append({})  # son çare: hiçbir kısıtlama yok, yalnızca prompt talimatına güven

        last_exc: Exception | None = None
        for extra in strategies:
            try:
                response = ollama.chat(
                    model=self.model,
                    messages=messages,
                    options={"temperature": 0},
                    keep_alive=self.keep_alive,
                    think=False,
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 - ollama/model bu stratejiyi desteklemeyebilir
                last_exc = exc
                logger.debug("Strateji basarisiz (%s): %s", extra, exc)
                continue

            message = response["message"]
            native_calls = self._extract_native_tool_calls(message) if "tools" in extra else None
            return message.get("content") or "", native_calls

        raise ConnectionError(f"Ollama'ya bağlanılamadı ('{self.model}'): {last_exc}")

    @staticmethod
    def _extract_native_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Ollama'nın native `tool_calls` alanını iç formatımıza çevirir.

        Model, `tools` parametresini desteklemiyorsa veya bu turda hiç
        tool çağırmadıysa `tool_calls` boş/yok olur; bu durumda None
        döner ve çağıran taraf düz metne (`content`) geri düşer.
        """

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return None

        result: list[dict[str, Any]] = []
        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            result.append({"tool": name, "arguments": arguments})

        return result or None

    @staticmethod
    def _build_native_tools() -> list[dict[str, Any]]:
        """TOOL_REGISTRY'den Ollama'nın native `tools=[...]` parametresi için liste üretir."""

        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.arguments_schema,
                },
            }
            for definition in build_tool_manifest()
        ]

    @staticmethod
    def _response_schema() -> dict[str, Any]:
        """Ollama'nın "structured outputs" özelliğine geçirilecek JSON şeması.

        Şema HER ZAMAN bir tool-call DİZİSİ dayatır (`type: object` DEĞİL).
        Bunun nedeni `core/planner.py::TaskPlanner`'ın çok adımlı planları
        yürütebilmesi: grammar-constrained decoding, şemanın izin vermediği
        hiçbir çıktıyı üretmeye modeli İZİN VERMEZ — şema `type: object`
        olsaydı model bu strateji aktifken bir JSON listesi asla üretemez,
        `prompts/system_prompt.md`'nin "birden fazla işlem istiyorsa liste
        üret" talimatı bu strateji altında etkisiz kalırdı. Tek işlemlik
        komutlar artık tek elemanlı bir liste olarak üretilir; ayrı bir
        "ya obje ya dizi" (`anyOf`/`oneOf`) şeması KULLANILMIYOR çünkü tek
        biçim modeller için daha az kafa karıştırıcı ve `anyOf`/`oneOf`'un
        grammar'a çevrilmesi her Ollama/llama.cpp sürümünde garanti değil.
        `extract_tool_calls` zaten hem tek obje hem listeyi kabul ettiği
        için (şema devre dışı kalan strateji 3/4'e düşüldüğünde model yine
        de düz bir obje dönebilir) bu değişiklik geriye dönük uyumu bozmaz.

        `tool` alanı yine TOOL_REGISTRY'deki GÜNCEL tool adlarının `enum`'u
        olarak kısıtlanır — model, var olmayan bir tool ismi UYDURAMAZ
        (grammar-constrained decoding sayesinde, prompt talimatına değil).
        """

        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": sorted(TOOL_REGISTRY.keys())},
                    "arguments": {"type": "object"},
                },
                "required": ["tool", "arguments"],
            },
            "minItems": 1,
            # Bir sesli komut gerçekçi olarak en fazla birkaç adım sürer
            # ("Chrome'u aç, GitHub'a git, ara" = 3). Bu sınır bir kalite
            # tercihi değil, GÜVENLİK sınırıdır: anlaşılamayan/gürültülü bir
            # girdide model kaçağa geçip uzun bir plan üretebiliyor. Gerçek
            # bir olayda tek bir anlamsız cümle 25 tool çağrısı ürettti ve
            # içinde `filesystem.delete` vardı. Grammar-constrained decoding
            # sayesinde bu sınır bir talimat değil, fiziksel bir kısıttır.
            "maxItems": MAX_PLAN_STEPS,
        }

    @staticmethod
    def extract_tool_calls(raw_text: str) -> list[dict[str, Any]]:
        """Ham LLM metninden tool-call sözlük listesi çıkarır.

        Sırasıyla dener: (1) metnin tamamını doğrudan JSON olarak parse et,
        (2) ```json ... ``` code-fence'lerini temizleyip tekrar dene,
        (3) metin içinde ilk `{...}` veya `[...]` bloğunu regex ile bulup
        onu parse et.

        Args:
            raw_text: Modelin ham metin çıktısı.

        Returns:
            Her biri `{"tool": ..., "arguments": {...}}` şeklinde bir liste.

        Raises:
            LLMResponseParseError: Hiçbir denemede geçerli JSON bulunamazsa.
        """

        candidates = [raw_text.strip()]

        fenced = re.sub(r"^```(json)?", "", raw_text.strip(), flags=re.IGNORECASE).strip()
        fenced = re.sub(r"```$", "", fenced).strip()
        if fenced not in candidates:
            candidates.append(fenced)

        match = _JSON_BLOCK_RE.search(raw_text)
        if match:
            candidates.append(match.group(0))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return parsed

        raise LLMResponseParseError(f"LLM çıktısından geçerli tool-call JSON'u çıkarılamadı: {raw_text!r}")
