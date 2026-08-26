"""Tek komuttan üretilen çoklu tool çağrısını sırayla yürütür.

Örnek: "Chrome'u aç, GitHub'a git ve OpenAI reposunu ara" gibi bir komut,
LLM tarafından zaten bir JSON listesine bölünüyor (bkz.
`core.llm_client.get_tool_calls`). Bu modülün asıl işi, o listeyi
GÜVENLİ ve ANLAMLI bir sırada yürütmek:

    - Adımlar sırayla çalıştırılır (paralel değil) — çünkü sonraki bir
      adım genellikle bir öncekinin tamamlanmasına bağlıdır (örn. önce
      klasör oluşturulmalı ki içine dosya konabilsin).
    - Bir adım GERÇEKTEN başarısız olursa (`stop_on_failure=True`,
      varsayılan), kalan adımlar ÇALIŞTIRILMAZ — "Orbit klasörünü
      oluştur ve içine app.py koy" gibi bir planda, klasör oluşturma
      başarısız olduysa dosya oluşturmaya devam etmek anlamsız/tehlikeli
      olurdu.
    - Bir adım onay gerektiriyorsa (`requires_confirmation`), plan
      DURUR ve kullanıcıya sorulur; kullanıcı reddederse KALAN TÜM
      adımlar da iptal edilir (yalnızca o adım değil) — çünkü kullanıcı
      "hayır" dediğinde, aynı planın bir parçası olan sonraki adımların
      hâlâ istenip istenmediği belirsizdir; güvenli taraf durmaktır.

VERİ AKTARIMI (v3.5): adımlar arasında `{{step_N.alan}}` biçiminde bir
REFERANS ile veri aktarılabilir — örn. `filesystem.create_folder`'ın
oluşturduğu klasörün yolu, sonraki bir `filesystem.create_file` adımına
`location` olarak verilebilir. Bilinçli olarak "önceki adımın çıktısını
bekleyip sonraki adımı ona göre YENİDEN ÜRET" gibi bir tasarım
SEÇİLMEDİ: bu, küçük/yerel modelin adım başına ayrı bir LLM çağrısı
gerektirir (yavaş) ve modelin ara sonucu okuyup akıl yürütmesini
gerektirir (küçük modeller için güvenilir değil — bkz.
`llm_client.should_engage` docstring'indeki ölçülmüş kanıt). Bunun
yerine model TÜM adımları TEK seferde, referans YER TUTUCULARIYLA
üretir; gerçek değerler yalnızca ÇALIŞMA ZAMANINDA, `_resolve_step_references`
tarafından önceki adımın GERÇEK `ToolResult.data`'sından okunarak
yerleştirilir. Model hiçbir zaman "tahmin edilmiş" bir dosya yolu
üretmez — yalnızca "N. adımın X alanı" der, gerçek değeri asla görmez/
uydurmaz.

GÜVENLİK NOTU: referans çözümlemesi, onay (`confirm_callback`) ve
raporlama (`StepResult`) çağrılarından ÖNCE yapılır. Yani `filesystem.
delete` gibi tehlikeli bir adım `target: {{step_1.path}}` alırsa, onay
diyaloğunda kullanıcı YER TUTUCUYU değil ÇÖZÜLMÜŞ GERÇEK yolu görür —
aksi halde "onay NEYİ onayladığını göstermek zorunda" ilkesi (bkz.
CLAUDE.md, README §16b) ihlal edilirdi: yer tutucu gösteren bir onay,
güvenlik hissi verir ama sağlamaz.

Yalnızca DAHA ÖNCEKİ ve BAŞARILI bir adıma referans verilebilir; kendine/
ileriye referans ya da başarısız/var olmayan bir adıma referans, o adımı
normal bir tool hatası gibi başarısız kılar (zincirleme kural — bkz.
`_resolve_step_references`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.dispatcher import ToolDispatcher
from models.tool_models import ToolResult

logger = logging.getLogger(__name__)

_STEP_REFERENCE_RE = re.compile(r"^\{\{step_(\d+)\.([a-zA-Z_]+)\}\}$")
"""`{{step_N.alan}}` biçimindeki referansları tanır. BİLEREK tam eşleşme
(`^...$`) ister — bir argümanın İÇİNDE gömülü kısmi bir referans (örn.
"Rapor/{{step_1.path}}/x") desteklenmez; bu hem çözümlemeyi hem olası
hata mesajlarını basit tutar ve gerçek ihtiyaç zaten "bütün değeri
önceki adımdan al" biçiminde (bkz. modül dokümantasyonu örneği)."""


def _resolve_step_references(
    arguments: dict[str, Any], step_results: list[StepResult]
) -> tuple[dict[str, Any], str | None]:
    """Argümanlardaki `{{step_N.alan}}` referanslarını önceki adımların
    GERÇEK `ToolResult.data`'sıyla değiştirir.

    Args:
        arguments: Henüz dispatch edilmemiş adımın ham argümanları.
        step_results: O ana kadar çalıştırılmış (ve `execute_plan`'ın
            sırayla doldurduğu) önceki adımların sonuçları. Yalnızca bu
            listede GERÇEKTEN bulunan bir adıma referans verilebilir —
            liste sırayla dolduğu için kendine/ileriye referans burada
            hiç "bulunamaz" ve doğal olarak reddedilir.

    Returns:
        (çözümlenmiş_argümanlar, hata_mesajı) çifti. `hata_mesajı`
        `None` değilse çözümleme başarısız olmuştur; bu durumda
        `çözümlenmiş_argümanlar` orijinal (yer tutuculu) argümanlardır
        ve adım hiç dispatch EDİLMEMELİDİR.
    """

    resolved = dict(arguments)
    for key, value in arguments.items():
        if not isinstance(value, str):
            continue
        match = _STEP_REFERENCE_RE.match(value.strip())
        if match is None:
            continue

        ref_index, field = int(match.group(1)), match.group(2)
        source = next((s for s in step_results if s.index == ref_index), None)

        if source is None:
            return arguments, (
                f"'{key}' argümanı henüz çalışmamış ya da var olmayan {ref_index}. "
                "adıma referans veriyor"
            )
        if not source.result.success:
            return arguments, f"'{key}' argümanı başarısız olan {ref_index}. adıma referans veriyor"
        if not source.result.data or field not in source.result.data:
            return arguments, (
                f"'{key}' argümanı {ref_index}. adımın sonucunda olmayan bir alana "
                f"('{field}') referans veriyor"
            )

        resolved[key] = source.result.data[field]

    return resolved, None


@dataclass
class StepResult:
    """Bir plan adımının çalıştırma sonucu.

    Attributes:
        index: Plandaki sırası (1'den başlar).
        tool_name: Çalıştırılan (veya çalıştırılamayan) tool'un adı.
        arguments: Bu adım için kullanılan argümanlar.
        result: Dispatcher'dan dönen ToolResult.
        skipped: True ise bu adım hiç çalıştırılmadı (önceki bir adımın
            başarısızlığı/onay reddi yüzünden planın durdurulmasından sonra
            eklenmiştir — yalnızca raporlama amaçlı, kullanıcıya "bu adıma
            hiç sıra gelmedi" demek için).
    """

    index: int
    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult
    skipped: bool = False


class TaskPlanner:
    """LLM'den gelen bir tool-call listesini sırayla, güvenli şekilde yürütür.

    Attributes:
        dispatcher: Adımları gerçekten çalıştıracak ToolDispatcher.
        confirm_callback: Onay gerektiren bir adım geldiğinde çağrılır;
            `(tool_name: str, arguments: dict[str, Any]) -> bool` imzasında
            olmalı. Argümanlar da geçirilir çünkü kullanıcı yalnızca tool
            adına bakarak NEYİ onayladığını göremez — örn. `filesystem.delete`
            hangi dosyayı/klasörü sileceğini yalnızca `arguments` içinden
            bilir; bu olmadan onay sorusu anlamsız (hatta tehlikeli) bir
            "kör onay"a dönüşür. Terminal/ses/GUI arayüzü ne olursa olsun bu
            fonksiyon değişir, planner'ın kendisi hiçbir I/O varsayımı
            yapmaz (test edilebilirlik ve arayüzden bağımsızlık için).
        stop_on_failure: True ise (varsayılan) bir adım gerçekten
            başarısız olduğunda kalan adımlar çalıştırılmaz.
    """

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        confirm_callback: Callable[[str, dict[str, Any]], bool],
        stop_on_failure: bool = True,
    ) -> None:
        self.dispatcher = dispatcher
        self.confirm_callback = confirm_callback
        self.stop_on_failure = stop_on_failure

    def execute_plan(self, tool_calls: list[dict[str, Any]]) -> list[StepResult]:
        """Verilen tool-call listesini sırayla yürütür.

        Args:
            tool_calls: `core.llm_client.OllamaLLMClient.get_tool_calls`'dan
                gelen, her biri `{"tool": ..., "arguments": {...}}` olan liste.

        Returns:
            Her adım için bir `StepResult`. Plan erken durdurulduysa,
            liste yalnızca gerçekten çalıştırılan (ve durma sebebi olan)
            adımları içerir — kalan adımlar hiç `StepResult` üretmez.
        """

        step_results: list[StepResult] = []

        for index, raw_call in enumerate(tool_calls, start=1):
            tool_name = raw_call.get("tool", "?")
            raw_arguments = raw_call.get("arguments", {})

            # Referans çözümlemesi DISPATCH'TEN ÖNCE yapılır — hem onay
            # diyaloğu hem StepResult raporlaması yer tutucuyu değil
            # ÇÖZÜLMÜŞ gerçek değeri görsün diye (bkz. modül dokümantasyonu
            # GÜVENLİK NOTU).
            arguments, ref_error = _resolve_step_references(raw_arguments, step_results)

            if ref_error is not None:
                result: ToolResult = ToolResult(success=False, message=f"Adım {index}: {ref_error}")
            else:
                result = self.dispatcher.dispatch({"tool": tool_name, "arguments": arguments})

                if result.requires_confirmation:
                    if self.confirm_callback(tool_name, arguments):
                        result = self.dispatcher.dispatch(
                            {"tool": tool_name, "arguments": arguments}, confirmed=True
                        )
                    else:
                        step_results.append(StepResult(index, tool_name, arguments, result))
                        logger.info(
                            "Kullanıcı onayı reddetti (adım %d/%d: '%s'); kalan %d adım iptal edildi.",
                            index,
                            len(tool_calls),
                            tool_name,
                            len(tool_calls) - index,
                        )
                        break

            step_results.append(StepResult(index, tool_name, arguments, result))

            if not result.success and self.stop_on_failure:
                logger.warning(
                    "Adım %d/%d ('%s') başarısız; kalan %d adım durduruldu.",
                    index,
                    len(tool_calls),
                    tool_name,
                    len(tool_calls) - index,
                )
                break

        return step_results
