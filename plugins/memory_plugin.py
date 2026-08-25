"""Kullanıcının Artemis'e hatırlatmasını istediği serbest bilgiler.

`memory/context_memory.py` genel bir SQLite anahtar-değer deposudur ama
şimdiye kadar yalnızca İÇ kullanım içindi (`remember_last_path`/
`get_last_path`, `location: "last"` özelliğinin dayandığı yer). Hiçbir
tool bu depoyu LLM'e AÇMIYORDU — kullanıcı "WiFi şifremi hatırla" dese
bile bunu kaydedecek/geri çağıracak hiçbir yol yoktu.

Bu plugin o boşluğu kapatır: `memory.remember`, `memory.recall`,
`memory.forget`. Ham `ContextMemory.set/get/delete` metotları DOĞRUDAN
kullanılmaz — bunun yerine `fact:` önekli `remember_fact`/`recall_fact`/
`forget_fact` convenience metotları çağrılır (bkz. context_memory.py).
NEDEN: model (ya da kullanıcı) yanlışlıkla `key="last_path"` gönderirse,
önek olmadan bu, `location: "last"` özelliğinin dayandığı İÇ anahtarı
sessizce ezerdi. `fact:` öneği, kullanıcı verisiyle sistemin kendi
durumunu aynı ad alanında asla çakıştırmaz.

GÜVENLİK NOTU — DÜZ METİN DEPOLAMA: bu depo şifrelenmemiş, yerel bir
SQLite dosyasıdır (`config/settings.py::Settings.db_path`). Gerçek
parola/kart numarası gibi hassas veriler için ÖNERİLMEZ; kullanıcı bunu
kullanırken uyarılır (tool açıklamasında), ama engellenmez — Artemis'in
kendi notlarını tutan bir defter olarak düşünülmeli, bir kasa değil.
"""

from __future__ import annotations

from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult


@register_tool
class MemoryRememberTool(BaseTool):
    """Kullanıcının söylediği bir bilgiyi kalıcı olarak hatırlar."""

    name = "memory.remember"
    description = (
        "Kullanıcının söylediği bir bilgiyi kalıcı olarak hatırlar (örn. "
        "'WiFi şifresi 12345' -> key='wifi şifresi', value='12345'). "
        "DÜZ METİN olarak saklanır, şifrelenmez — gerçek parola/kart "
        "numarası gibi çok hassas veriler için kullanma."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Neyin hatırlandığı (kısa bir etiket)."},
                "value": {"type": "string", "description": "Hatırlanacak bilginin kendisi."},
            },
            "required": ["key", "value"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        key = str(arguments["key"]).strip()
        value = str(arguments["value"]).strip()

        if not key:
            return ToolResult(success=False, message="Hatırlanacak bilginin bir adı (key) olmalı.")
        if not value:
            return ToolResult(success=False, message="Hatırlanacak bilgi boş olamaz.")

        context.memory.remember_fact(key, value)
        return ToolResult(success=True, message=f"'{key}' hatırlandı.", data={"key": key, "value": value})


@register_tool
class MemoryRecallTool(BaseTool):
    """Daha önce hatırlanmış bir bilgiyi geri çağırır."""

    name = "memory.recall"
    description = "Daha önce `memory.remember` ile hatırlanmış bir bilgiyi geri çağırır."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Hangi bilginin istendiği."}},
            "required": ["key"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        key = str(arguments["key"]).strip()
        value = context.memory.recall_fact(key)

        if value is None:
            # Dürüstçe "bilmiyorum" — CLAUDE.md'nin "koşulsuz success=True
            # yasak" kuralının bu tool'daki karşılığı: model, olmayan bir
            # bilgiyi UYDURMAMALI, gerçek durumu (bulunamadı) görmeli.
            return ToolResult(success=False, message=f"'{key}' diye bir şey hatırlamıyorum.")

        return ToolResult(success=True, message=value, data={"key": key, "value": value})


@register_tool
class MemoryForgetTool(BaseTool):
    """Daha önce hatırlanmış bir bilgiyi siler."""

    name = "memory.forget"
    description = "Daha önce `memory.remember` ile hatırlanmış bir bilgiyi unutur."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Hangi bilginin unutulacağı."}},
            "required": ["key"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        key = str(arguments["key"]).strip()
        existed = context.memory.forget_fact(key)

        if not existed:
            return ToolResult(success=True, message=f"'{key}' diye bir şey zaten hatırlamıyordum.")

        return ToolResult(success=True, message=f"'{key}' unutuldu.")
