"""Tool seçimi kıyası: doğruluk, HATANIN YÖNÜ ve gecikme.

    python scripts/bench_tool_selection.py
    python scripts/bench_tool_selection.py --model qwen3:8b
    python scripts/bench_tool_selection.py --model gemma4:e4b --tekrar 3

NEDEN BU BETİK VAR
------------------
README §21, §23 ve §34 bu projenin en önemli kararlarını ÖLÇÜME
dayandırıyor: hangi model varsayılan olacak, komut kapısının sınırı
nerede, yeni bir tool eklemenin bedeli ne.

ÖLÇÜT SKOR DEĞİL, HATANIN YÖNÜ
------------------------------
`.context` §6.9'un dersi: 20 zor senaryoda `gemma4:e4b` ve `qwen3.5:4b`
AYNI skoru aldı (18/20) ama biri adres uydurmuştu. `phi4-mini`
*"Evi kapat"*'ı `windows.shutdown` sandı — bilgisayar yalnızca tool
`CONFIRM_REQUIRED` olduğu için kapanmadı. Sesli bir asistanda yanlış
duyma kaçınılmaz olduğundan "emin değilsem dokunmam" diyen model
tercih edilir.

Bu yüzden rapor hataları İKİYE ayırır: ZARARSIZ (yanlış tool ama geri
alınabilir/etkisiz) ve TEHLİKELİ (onay gerektiren ya da yıkıcı bir tool
yanlışlıkla seçildi). Bir modeli 14/20 + 1 tehlikeli, başka birini
14/20 + 6 tehlikeli yapan fark budur ve toplam skor bunu GÖSTERMEZ.

MİMARİ: BU DOSYA ARTIK İNCE BİR ADAPTER
----------------------------------------
Asıl ölçüm mantığı (`Outcome`/`Scenario`/`Report` modelleri, `classify`,
`run_benchmark`, `format_report`, `load_scenarios`) `toolbench` adlı
bağımsız, registry-agnostik bir pakete taşındı (`~/toolbench`, kardeş
local proje — başka projelerde yeniden kullanılabilsin diye). Burada
kalan tek şey artemis'e özgü olan: `TOOL_REGISTRY`'den "tehlikeli" tool
kümesini üretmek (`dangerous_tools_from_registry`) ve gerçek
`OllamaLLMClient`'ı bağlamak (`main`).

NEDEN toolbench'TEN DEĞİL `_toolbench_vendor`'DAN IMPORT EDİYORUZ:
`toolbench` henüz PyPI'de değil; `requirements-dev.txt`'e `-e ../toolbench`
path bağımlılığı olarak eklenince GitHub Actions CI'yi kırdı (CI bu repoyu
İZOLE klonluyor, `../toolbench` orada hiç yok — run 35584430941, "../toolbench
is not a valid editable requirement"). Bu yüzden `scripts/
_toolbench_vendor.py`, `~/toolbench/src/toolbench/`'in BİREBİR elle
senkronize edilen bir kopyasıdır — kaynak orada değişirse buraya da
taşınmalı. `toolbench` PyPI'ye çıkınca (M3) bu vendor dosyası kaldırılıp
gerçek bir sürüm pin'i eklenecek. Kıyasın mantık testleri hem
`~/toolbench/tests/`'te (bağımsız paketin kendi suite'i) hem şu an
transitif olarak burada (`tests/test_bench_tool_selection.py`, vendor
kopyasına karşı) çalışıyor.

NASIL ÇALIŞIR
-------------
`scripts/smoke_voice.py` desenini izler: gerçek bileşen, sahte çevre
birimi. `main()` GERÇEK bir Ollama sunucusu ister ve bu yüzden `pytest`
koşusunda ÇALIŞMAZ (elle çağrılır). `dangerous_tools_from_registry` ve
toolbench'in kendi mantığı sahte bir istemciyle sınanır.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.enums import DangerLevel  # noqa: E402
from core.plugin_loader import TOOL_REGISTRY, load_plugins  # noqa: E402
from core.prompt_builder import build_system_prompt  # noqa: E402
from scripts._toolbench_vendor import (  # noqa: E402
    Outcome,
    Report,
    Scenario,
    ScenarioResult,
    classify,
    format_report,
    load_scenarios,
    run_benchmark,
)

DEFAULT_SCENARIO_PATH = REPO_ROOT / "tests" / "data" / "tool_selection_scenarios.json"

__all__ = [
    "DEFAULT_SCENARIO_PATH",
    "Outcome",
    "Report",
    "Scenario",
    "ScenarioResult",
    "classify",
    "dangerous_tools_from_registry",
    "format_report",
    "load_scenarios",
    "main",
    "run_benchmark",
]


def dangerous_tools_from_registry(extra_dangerous: frozenset[str]) -> frozenset[str]:
    """`TOOL_REGISTRY`'deki `CONFIRM_REQUIRED` tool'ları + senaryo
    dosyasındaki ek listeyi tek bir kümede birleştirir.

    toolbench'in `classify()`'ı artık kendi başına hiçbir registry lookup'ı
    yapmıyor — bu birleşimi ÖNCEDEN yapıp tek bir `frozenset[str]` olarak
    geçirmek çağıranın (yani bu adapter'ın) sorumluluğu. `extra_dangerous`
    senaryo dosyasındaki `CONFIRM_REQUIRED` olmasa da yanlış seçildiğinde
    zarar veren tool'lar içindir (`filesystem.move` bir dosyayı
    kaybettirebilir, `windows.sleep` çalışmayı keser).
    """

    confirm_required = {
        name for name, cls in TOOL_REGISTRY.items() if cls.danger_level is DangerLevel.CONFIRM_REQUIRED
    }
    return frozenset(confirm_required | extra_dangerous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Artemis tool seçimi kıyası")
    parser.add_argument("--model", default=None, help="Ollama model adı (varsayılan: config.yaml)")
    parser.add_argument("--senaryolar", type=Path, default=DEFAULT_SCENARIO_PATH)
    parser.add_argument("--tekrar", type=int, default=1, help="Her senaryo kaç kez çalıştırılsın")
    args = parser.parse_args(argv)

    from config.settings import get_settings
    from core.llm_client import OllamaLLMClient
    from core.ollama_manager import OllamaServerManager, OllamaUnavailableError

    load_plugins()
    settings = get_settings()
    model = args.model or settings.ollama_model

    scenarios, extra_dangerous = load_scenarios(args.senaryolar)
    dangerous_tools = dangerous_tools_from_registry(extra_dangerous)
    system_prompt = build_system_prompt()

    server = OllamaServerManager()
    try:
        server.ensure_running()
    except OllamaUnavailableError as exc:
        print(f"Kıyas çalıştırılamadı: {exc}")
        return 1

    client = OllamaLLMClient(
        model=model,
        use_native_tool_calling=settings.use_native_tool_calling,
        keep_alive=settings.ollama_keep_alive,
        timeout_seconds=settings.ollama_timeout_seconds,
    )

    try:
        report = run_benchmark(
            client,
            scenarios,
            system_prompt,
            dangerous_tools,
            model,
            tool_count=len(TOOL_REGISTRY),
            tekrar=args.tekrar,
        )
    finally:
        server.stop_if_we_started_it()

    print(format_report(report))
    # Tehlikeli hata varsa çıkış kodu 1: bir CI adımı ya da betik bunu
    # kapı olarak kullanabilsin.
    return 1 if report.tehlikeli else 0


if __name__ == "__main__":
    raise SystemExit(main())
