"""Proje atölyesi tool'ları: sohbetle proje netleştirme + arka planda kodlama.

YALNIZCA İKİ TOOL, BİLEREK: her tool manifest olarak HER istekte sistem
promptuna girer ve prompt boyutu doğrudan yerel modelin tool seçme
gecikmesidir (bkz. `tests/test_prompt_builder.py`). Altı işlemi altı
tool yapmak ~3.000 karakter eklerdi; bunun yerine işlemler `islem`
alanıyla iki tool'a toplandı. Ayrım ONAY sınırıdır, çünkü onay tool
düzeyinde (`danger_level`) uygulanır:

    proje.sor    (SAFE)              yeni | durum | liste
    proje.islem  (CONFIRM_REQUIRED)  baslat | cevapla | durdur

`proje.islem`'in üç işlemi de ya para harcar (Claude kodlayıcısı, bütçe
tavanıyla) ya da çalışan bir işi keser; onay ekranı `islem`, `ad` ve
`metin`i gösterir, yani kullanıcı NEYİ onayladığını görür.

Görüşmenin kendisi tool değildir: açık bir görüşme varken girdiler tool
seçimine hiç gitmez (bkz. `projects/session.py`).
"""

from __future__ import annotations

from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult
from projects import jobs
from projects.interview import first_question


@register_tool
class ProjeSorTool(BaseTool):
    """Yeni proje görüşmesi başlatır ya da proje işlerinin durumunu söyler."""

    name = "proje.sor"
    description = (
        "Proje atölyesi. islem=yeni: kullanıcı yeni bir yazılım projesi istiyor (metin=fikir); "
        "durum: kodlanan projenin durumu (metin=ad); liste: tüm proje işleri."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "islem": {"type": "string", "enum": ["yeni", "durum", "liste"]},
                "metin": {"type": "string"},
            },
            "required": ["islem"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings.projeler
        islem = arguments["islem"]
        metin = str(arguments.get("metin") or "").strip()
        if islem == "yeni":
            return jobs.begin_interview(settings, metin, first_question())
        if islem == "durum":
            return jobs.status(settings, metin or None)
        return jobs.listing(settings)


@register_tool
class ProjeIslemTool(BaseTool):
    """Kodlamayı başlatır, kodlayıcının sorusunu cevaplar ya da işi durdurur."""

    name = "proje.islem"
    description = (
        "Proje kodlaması. islem=baslat: spec'i hazır projeyi kodlat; cevapla: kodlayıcının "
        "sorusuna kullanıcının cevabını ilet (metin=cevap); durdur: işi durdur. ad=proje adı."
    )
    danger_level = DangerLevel.CONFIRM_REQUIRED

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "islem": {"type": "string", "enum": ["baslat", "cevapla", "durdur"]},
                "ad": {"type": "string"},
                "metin": {"type": "string"},
                "kodlayici": {"type": "string", "enum": ["claude", "ucretsiz"]},
            },
            "required": ["islem", "ad"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings.projeler
        islem = arguments["islem"]
        ad = str(arguments.get("ad") or "").strip()
        if islem == "cevapla":
            return jobs.answer(settings, ad or None, str(arguments.get("metin") or ""))
        if not ad:
            return ToolResult(success=False, message="Hangi proje? Proje adını söylemelisin.")
        if islem == "baslat":
            return jobs.start(settings, ad, arguments.get("kodlayici"))
        return jobs.stop(settings, ad)
