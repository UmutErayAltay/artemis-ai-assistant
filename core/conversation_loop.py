"""Metin tabanlı konuşma döngüsü — `python main.py --chat`.

DÜZELTME (v3.14, README §33): bu dosyanın docstring'i uzun süre "ses
katmanı hazır olana kadar geçici arayüz" diyordu. Bu artık yanlış: ses
katmanı v2.0'da geldi (`core/voice_loop.py`, `python main.py --voice`)
ve bu modül GEÇİCİ olarak yerini ona bırakmadı — ikisi de KALICI, ayrı
birer giriş noktası. `--chat` sesin uygun olmadığı (paylaşımlı bir ofis,
mikrofon yok, betikleme/otomasyon) durumlar için terminal tabanlı bir
alternatif sağlamaya devam ediyor.

Çok adımlı komutların ("Chrome'u aç, GitHub'a git, ara") sıralı ve güvenli
yürütülmesi `core.planner.TaskPlanner`'a devredilmiştir; bu modül yalnızca
terminal I/O'sunu (girdi alma, onay sorma, sonuç yazdırma) sağlar — bu
sayede yeni bir tool eklendiğinde (örn. bu oturumdaki `filesystem.rename`,
`memory.*`, `windows.arrange_window`) burada HİÇBİR değişiklik gerekmez;
akış zaten aynı `ToolDispatcher`/`TaskPlanner`'dan geçer (bkz. README §33
— uçtan uca doğrulandı, varsayılmadı).
"""

from __future__ import annotations

import logging
from typing import Any

from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError, OllamaLLMClient
from core.planner import TaskPlanner
from core.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

_EXIT_COMMANDS = {"çıkış", "cikis", "exit", "quit"}


def _confirm_with_user(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Tehlikeli bir işlem için terminalden onay ister.

    Kullanıcıya yalnızca tool adı değil, işlemin argümanları da okunaklı
    biçimde gösterilir — aksi halde kullanıcı NEYİ onayladığını göremez
    (örn. `filesystem.delete` hangi dosyayı/klasörü sileceğini yalnızca
    argümanlardan bellidir; kör onay güvenlik açığıdır).
    """

    print(f"  '{tool_name}' işlemi onay gerektiriyor.")
    if arguments:
        for key, value in arguments.items():
            print(f"    {key}: {value}")
    else:
        print("    (argüman yok)")
    answer = input("  Devam edilsin mi? (e/h): ")
    return answer.strip().lower() in {"e", "evet", "y", "yes"}


def run(dispatcher: ToolDispatcher, llm_client: OllamaLLMClient) -> None:
    """Terminalden komut alıp LLM üzerinden tool'lara dağıtan ana döngü.

    Args:
        dispatcher: Kullanıma hazır ToolDispatcher (tool'lar yüklenmiş olmalı).
        llm_client: Yerel Ollama modeliyle konuşacak istemci.
    """

    system_prompt = build_system_prompt()
    planner = TaskPlanner(dispatcher, confirm_callback=_confirm_with_user)
    print("Artemis hazır. Çıkmak için 'çıkış' yazın.\n")

    while True:
        user_input = input("Siz: ").strip()
        if not user_input:
            continue
        if user_input.lower() in _EXIT_COMMANDS:
            print("Artemis: Görüşürüz.")
            break

        try:
            tool_calls = llm_client.get_tool_calls(system_prompt, user_input)
        except LLMResponseParseError as exc:
            logger.warning("LLM çıktısı ayrıştırılamadı: %s", exc)
            print("Artemis: Anlayamadım, farklı bir şekilde ifade eder misiniz?")
            continue
        except ConnectionError as exc:
            logger.error("Ollama bağlantı hatası: %s", exc)
            print(f"Artemis: Yerel modele ulaşılamıyor ('ollama serve' çalışıyor mu?). Detay: {exc}")
            continue

        step_results = planner.execute_plan(tool_calls)
        total = len(tool_calls)
        for step in step_results:
            prefix = f"[{step.index}/{total}] " if total > 1 else ""
            print(f"Artemis: {prefix}{step.result.message}")

        completed = len(step_results)
        if completed < total:
            skipped = total - completed
            print(f"Artemis: ({skipped} adım, önceki bir adımın durdurulması nedeniyle çalıştırılmadı.)")
