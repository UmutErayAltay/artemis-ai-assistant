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

BİLİNÇLİ SINIRLAMA (v1): Adımlar arasında veri AKTARIMI yoktur — yani
"1. adımda bulunan dosya yolunu 2. adıma argüman olarak ver" gibi bir
zincirleme desteklenmez. Her adımın argümanları LLM tarafından tek
seferde, baştan üretilir. Bu, küçük/yerel modellerin "adım 1'in
çıktısını bekleyip adım 2'yi ona göre üret" gibi çok adımlı bir
akıl yürütmeyi güvenilir yapamayabileceği göz önüne alınarak bilinçli
bir basitleştirmedir; ileride ihtiyaç olursa `execute_plan` içine bir
"önceki adımın `data` alanını sonraki adımın argümanlarına enjekte et"
mekanizması eklenebilir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from core.dispatcher import ToolDispatcher
from models.tool_models import ToolResult

logger = logging.getLogger(__name__)


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
            arguments = raw_call.get("arguments", {})

            result = self.dispatcher.dispatch(raw_call)

            if result.requires_confirmation:
                if self.confirm_callback(tool_name, arguments):
                    result = self.dispatcher.dispatch(raw_call, confirmed=True)
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
