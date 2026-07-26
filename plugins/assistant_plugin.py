"""Asistanın "hiçbir şey yapma, sadece konuş" seçeneği.

NEDEN BU TOOL VAR — gerçek bir arızanın çözümü:

    Sistem promptu modelden "asla düz metin cevap verme, hep bir tool
    çağrısı üret" diye istiyor ve çıktı şeması bunu `minItems: 1` ile
    ZORUNLU kılıyor (bkz. `core/llm_client.py::_response_schema`). Yani
    modelin hiçbir şey yapmama seçeneği YOKTU. Sonuç, gerçek kullanımda
    şuydu:

        "Sen kimsin?"          -> masaüstünde example.txt oluşturuldu
        "Abi insanlarız ki?"   -> dosya oluşturuldu + tarayıcı açıldı
        (arka planda sohbet)   -> yine dosya oluşturuldu
        "Evi kapat"            -> windows.shutdown denendi

    Model anlamadığı ya da komut olmayan her girdide rastgele bir tool
    seçiyordu, çünkü seçmemek elinde değildi. Bu, yanlış tanımanın
    bedelini "garip cevap"tan "istenmeyen eylem"e çıkarıyordu.

Bu tool o boşluğu kapatır: sistemde HİÇBİR ŞEY değiştirmez, yalnızca
verilen metni kullanıcıya söyler. Böylece model, emin olmadığında
zararsız bir çıkış kapısına sahip olur.
"""

from __future__ import annotations

from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult


@register_tool
class AssistantReplyTool(BaseTool):
    """Kullanıcıya yalnızca konuşarak cevap verir; hiçbir yan etkisi yoktur."""

    name = "assistant.reply"
    description = (
        "Kullanıcıya SADECE konuşarak cevap verir; bilgisayarda hiçbir şey "
        "değiştirmez, hiçbir dosya/uygulama/site açmaz. ŞU DURUMLARDA BUNU "
        "KULLAN: (1) kullanıcı bir soru sordu veya sohbet ediyor, (2) ne "
        "dediği anlaşılmadı ya da cümle yarım/anlamsız, (3) istediği şey "
        "mevcut tool'lardan hiçbiriyle yapılamıyor. Emin olmadığında "
        "rastgele bir tool seçmek yerine HER ZAMAN bunu kullan."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Kullanıcıya söylenecek kısa cevap. Sesli okunacağı için "
                        "bir-iki cümleyi geçmesin."
                    ),
                }
            },
            "required": ["message"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        message = str(arguments.get("message", "")).strip()

        if not message:
            # Model boş bir cevap ürettiyse sessiz kalmak yerine dürüstçe
            # anlamadığını söyle — kullanıcı en azından duyulduğunu bilsin.
            message = "Bunu anlayamadım."

        context.logger.info("Asistan yalnızca cevap verdi (yan etki yok): %r", message)
        return ToolResult(success=True, message=message, data={"reply": message})
