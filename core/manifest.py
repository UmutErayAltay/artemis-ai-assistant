"""Tool manifest üretimi.

TOOL_REGISTRY'deki tüm tool'ları, yerel LLM'e (Ollama) tanıtılacak
yapılandırılmış bir listeye çevirir. Bu sayede yeni bir tool eklendiğinde
`prompts/system_prompt.md` şablonu elle güncellenmez; manifest her
çalıştırmada registry'den taze olarak üretilir. 100 tool'a ölçeklenmenin
kilit noktalarından biri budur.
"""

from __future__ import annotations

import json

from core.plugin_loader import TOOL_REGISTRY
from models.tool_models import ToolDefinition


def build_tool_manifest() -> list[ToolDefinition]:
    """Kayıtlı tüm tool'lardan LLM'e sunulacak tanım listesini üretir.

    Returns:
        Her biri bir tool'u tanımlayan ToolDefinition listesi, tool
        adına göre alfabetik sıralı.
    """

    manifest: list[ToolDefinition] = []
    for tool_name, tool_cls in sorted(TOOL_REGISTRY.items()):
        tool = tool_cls()
        manifest.append(
            ToolDefinition(
                name=tool_name,
                description=tool.description,
                arguments_schema=tool.get_arguments_schema(),
                danger_level=tool.danger_level,
            )
        )
    return manifest


def build_tool_manifest_json() -> str:
    """Manifest'i, sistem promptuna `{tool_manifest}` yerine gömülecek
    JSON string olarak döndürür.
    """

    manifest = build_tool_manifest()
    entries = [m.model_dump() for m in manifest]

    # `danger_level` prompttan ÇIKARILIR: onayı `core/dispatcher.py`
    # uygular, modelin bilmesine gerek yoktur ve her tool için fazladan
    # token harcar (bkz. `prompts/system_prompt.md` — model yalnızca
    # doğru tool çağrısını üretmekle sorumlu).
    for entry in entries:
        entry.pop("danger_level", None)

    # Girintisiz ve boşluksuz: bu metin her istekte modele gönderilir,
    # girinti yalnızca token harcar. İnsan okunabilirliği burada
    # gerekmiyor — geliştirici manifesti `build_tool_manifest()` ile
    # nesne olarak inceleyebilir.
    return json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
