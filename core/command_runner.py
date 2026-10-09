"""Bir metin komutunun (sesli ya da yazılı) ortak işlem hattı.

NEDEN AYRI BİR MODÜL: sesli asistan (`core/voice_loop.py`) ile panelin yazı
kutusu aynı beyne, aynı tool'lara ve aynı planlayıcıya gitmeli. Bu hattı iki
yerde ayrı ayrı yazmak, bir gün birinde proje görüşmesi ya da onay mantığının
değişip diğerinde unutulması demekti. Hat burada TEK kez yaşar:

    1. Açık bir proje görüşmesi varsa cevap ona gider (komut kapısından ÖNCE).
    2. Komut kapısı (`llm.should_engage`) — yalnızca sesli girdide açık.
    3. LLM tool-call listesini üretir (`llm.get_tool_calls`).
    4. `TaskPlanner` listeyi sırayla yürütür; onay gereken adım için
       çağıranın verdiği `confirm` geri çağrısı çalışır.

Hat hiçbir arayüz (ses, Qt, log biçimi) bilmez: sonucu `CommandOutcome`
olarak döner, sunum çağıranın işidir. Sesli yol bunu sesle okur, panel balon
olarak gösterir.

ONAY: `confirm` imzası `(tool_name, arguments) -> bool` ve burada yalnızca
iletilir. Onayın KENDİSİ `core/dispatcher.py`'dadır; bu modül onay kuralı
koymaz (CLAUDE.md: onay mantığı tek yerde yaşar).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError
from core.llm_types import LLMClient
from core.planner import PlanProgress, StepResult, TaskPlanner
from core.prompt_builder import build_system_prompt
from projects.session import route_to_interview

logger = logging.getLogger(__name__)

ConfirmCallback = Callable[[str, dict[str, Any]], bool]
"""Onay geri çağrısı: `(tool_name, arguments) -> bool`. Argümanlar her zaman
geçirilir çünkü kullanıcı yalnızca tool adına bakarak neyi onayladığını
bilemez (bkz. `core/planner.py::TaskPlanner`)."""

CommandKind = Literal["done", "project", "ignored", "error"]
"""Sonucun türü. Çağıran taraf bunu sunuma çevirir:

    done:    tool planı çalıştı (başarılı ya da başarısız adımlar olabilir).
    project: açık bir proje görüşmesinin cevabı.
    ignored: komut kapısı bunu asistana yönelik bulmadı; tool çalışmadı.
    error:   LLM'e ulaşılamadı ya da cevap anlaşılamadı.
"""


@dataclass(frozen=True)
class CommandOutcome:
    """Bir komutun sonucu. Sunum bilgisini taşır, sunumun kendisini değil.

    Attributes:
        reply: Kullanıcıya gösterilecek / sesli okunacak metin.
        kind: Sonucun türü (bkz. `CommandKind`).
        step_results: Çalıştırılan adımların sonuçları (geçmiş ekranında
            tool satırlarını ve başarı durumunu göstermek için).
    """

    reply: str
    kind: CommandKind
    step_results: tuple[StepResult, ...] = ()


def summarize_steps(step_results: list[StepResult], total: int) -> str:
    """Plan sonuçlarını tek bir cümleye indirger.

    Terminalde adımlar tek tek yazdırılabilir ama sesli asistanda uzun listeler
    dinlemesi yorucudur; bu yüzden tek adımda doğrudan sonucun mesajı, çok
    adımda kısa bir özet döner. Panel de aynı özeti kullanır: iki kanalın
    aynı komuta aynı cümleyle cevap vermesi tutarlılık sağlar.
    """

    if not step_results:
        return "Yapılacak bir işlem bulamadım."

    if total == 1:
        return step_results[0].result.message

    successful = sum(1 for step in step_results if step.result.success)
    if successful == total:
        return f"{total} işlemin hepsini tamamladım."

    return f"{total} işlemden {successful} tanesini tamamladım; kalanında sorun oldu."


class CommandRunner:
    """Metin komutunu baştan sona işleyen hat.

    Bir örnek birden çok komut için yeniden kullanılabilir; her `run` çağrısı
    kendi `TaskPlanner`'ını kurar, çünkü onay geri çağrısı çağrıya göre değişir
    (sesli onay ya da Qt diyaloğu).

    Args:
        dispatcher: Tool'ları çalıştıracak ToolDispatcher.
        llm: Beyin (`get_tool_calls`, `should_engage`, proje görüşmesi için).
        settings: `projeler` ve `command_gate_enabled` ayarları buradan okunur.
        system_prompt: Verilmezse tool manifestiyle birlikte kurulur.
    """

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        llm: LLMClient,
        settings: Settings,
        system_prompt: str | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._llm = llm
        self._settings = settings
        self._system_prompt = build_system_prompt() if system_prompt is None else system_prompt

    def run(
        self,
        text: str,
        confirm: ConfirmCallback,
        *,
        gate: bool = True,
        progress: Callable[[PlanProgress], None] | None = None,
    ) -> CommandOutcome:
        """Metni işler ve sonucu döndürür. Hiçbir zaman arayüz çizmez.

        Args:
            text: Kullanıcının söylediği ya da yazdığı metin.
            confirm: Onay gereken bir adımda çağrılan geri çağrı.
            progress: İsteğe bağlı. Plan adımlarının durumu değiştikçe çağrılır
                (bkz. `core/planner.py::PlanProgress`); adım listesini çizmek için.
                None ise bildirim yapılmaz.
            gate: Komut kapısı uygulansın mı. Sesli girdide True (mikrofon
                arka plan gürültüsü de duyar); yazılı girdi kullanıcının
                bilerek yazdığı bir şeydir, panel bunu False geçer ve
                "bir komut duymadım" cevabıyla boşuna karşılaşmaz.
                Ayar `command_gate_enabled: false` ise yine uygulanmaz.

        Returns:
            Komutun sonucu. LLM hataları `error` türü olarak döner (istisna
            fırlatılmaz); beklenmeyen hatalar çağıranın sorumluluğundadır.
        """

        planner = TaskPlanner(self._dispatcher, confirm_callback=confirm, progress_callback=progress)

        # Açık bir proje görüşmesi varsa cevap ona gider — komut kapısından
        # ÖNCE: "FastAPI olsun" tek başına bir komut gibi durmaz ve kapı onu
        # gürültü sayabilirdi (bkz. projects/session.py).
        turn = route_to_interview(self._settings.projeler, self._llm, text)
        if turn is not None:
            reply = turn.message
            step_results: list[StepResult] = []
            if turn.tool_call is not None:
                step_results = planner.execute_plan([turn.tool_call])
                reply = f"{reply} {summarize_steps(step_results, 1)}"
            return CommandOutcome(reply=reply, kind="project", step_results=tuple(step_results))

        # KOMUT KAPISI: her duyulan şey asistana yönelik değildir. Uyandırma
        # sözcüğü gürültüyle de tetiklenebiliyor (bkz. `llm_client.should_engage`).
        if gate and self._settings.command_gate_enabled and not self._llm.should_engage(text):
            return CommandOutcome(reply="Bir komut duymadım.", kind="ignored")

        try:
            tool_calls = self._llm.get_tool_calls(self._system_prompt, text)
        except LLMResponseParseError:
            return CommandOutcome(reply="Bunu anlayamadım, başka türlü söyler misiniz?", kind="error")
        except ConnectionError as exc:
            logger.error("Ollama bağlantı hatası: %s", exc)
            return CommandOutcome(reply="Yerel modele ulaşamıyorum.", kind="error")

        # Hangi tool'un neden seçildiğini sonradan anlayabilmek için, LLM'in
        # ürettiği planı logla (sesli ve yazılı yollar için aynı kayıt).
        logger.info(
            "LLM planı: %s",
            [(call.get("tool"), call.get("arguments")) for call in tool_calls],
        )

        step_results = planner.execute_plan(tool_calls)
        return CommandOutcome(
            reply=summarize_steps(step_results, len(tool_calls)),
            kind="done",
            step_results=tuple(step_results),
        )
