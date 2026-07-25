"""Sistem promptu şablonunu doldurur.

`prompts/system_prompt.md`, statik kuralları ve `{tool_manifest}` yer
tutucusunu içerir. Bu modül, yer tutucuyu `core.manifest` üzerinden
GÜNCEL registry ile doldurur — yeni bir tool eklendiğinde şablon
dosyasına asla dokunulmaz.
"""

from __future__ import annotations

from pathlib import Path

from core.manifest import build_tool_manifest_json

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"


def build_system_prompt(template_path: Path | None = None) -> str:
    """Şablonu okur ve `{tool_manifest}` yer tutucusunu doldurup döndürür.

    Args:
        template_path: Alternatif şablon yolu (testler için).

    Returns:
        Ollama'ya `system` rolüyle gönderilecek tam metin.
    """

    path = template_path or _TEMPLATE_PATH
    template = path.read_text(encoding="utf-8")
    return template.replace("{tool_manifest}", build_tool_manifest_json())
