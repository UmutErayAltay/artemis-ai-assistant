"""Web arama ve URL açma ile ilgili tool'lar.

Bu plugin, sistemin varsayılan tarayıcısını (stdlib `webbrowser` modülü
üzerinden) kullanarak bir arama motorunda sorgu açar veya doğrudan bir
URL'yi açar. LLM hiçbir zaman tarayıcıyı kendisi tetiklemez; yalnızca
`query`/`url` argümanlarını üretir, gerçek `webbrowser.open()` çağrısını
bu dosya yapar.

Güvenlik notu: `web.open_url`'e gelen URL, LLM tarafından üretildiği için
körü körüne güvenilmez — yalnızca `http`/`https` şemalarına izin verilir;
`file:`, `javascript:`, `data:` gibi yerel dosya okuma veya script
çalıştırma riski taşıyan şemalar reddedilir (bkz. aşağıdaki
`WebOpenUrlTool` docstring'i).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus, urlparse

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult

# Her arama motoru için URL şablonu tek bir yerde toplanır; yeni bir
# motor eklemek yalnızca buraya tek satır eklemek demektir. Wikipedia
# için Türkçe alan adı (tr.wikipedia.org) kullanılır — proje Türkçe
# konuşan bir asistan.
_SEARCH_ENGINE_URL_TEMPLATES: dict[str, str] = {
    "google": "https://www.google.com/search?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "wikipedia": "https://tr.wikipedia.org/w/index.php?search={query}",
    "github": "https://github.com/search?q={query}",
}

# Yalnızca bu şemalara sahip URL'lerin açılmasına izin verilir.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _open_in_browser(url: str) -> tuple[bool, str | None]:
    """`webbrowser.open()`'ı çağırır ve sonucu dürüstçe raporlar.

    v0.7'de `windows.launch_app`'te öğrenilen "yanlış-pozitif başarı"
    dersine uyar (bkz. README.md bölüm 11): bir çağrının exception
    fırlatmaması başarı anlamına gelmez. `webbrowser.open()` uygun bir
    tarayıcı bulamazsa sessizce False döner; bu yüzden dönüş değeri
    kontrol edilir ve olası exception'lar da ayrıca yakalanır.

    Returns:
        (başarılı_mı, hata_mesajı) ikilisi. Başarılıysa hata_mesajı None'dır.
    """

    import webbrowser  # Lazy import: yalnızca gerçekten tarayıcı açılacaksa yüklenir.

    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - platforma göre değişen tarayıcı hatalarını da yakala
        return False, f"Tarayıcı açılırken hata oluştu: {exc}"

    if not opened:
        return False, "Tarayıcı açılamadı (webbrowser.open başarısız döndü)."

    return True, None


@register_tool
class WebSearchTool(BaseTool):
    """Varsayılan tarayıcıda, seçilen arama motorunda bir arama açar."""

    name = "web.search"
    description = "Varsayılan tarayıcıda bir arama açar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Aranacak metin."},
                "engine": {
                    "type": "string",
                    "description": "Kullanılacak arama motoru.",
                    "enum": sorted(_SEARCH_ENGINE_URL_TEMPLATES),
                    "default": "google",
                },
            },
            "required": ["query"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = arguments["query"]
        engine = arguments.get("engine", "google")

        template = _SEARCH_ENGINE_URL_TEMPLATES.get(engine)
        if template is None:
            valid = ", ".join(sorted(_SEARCH_ENGINE_URL_TEMPLATES))
            return ToolResult(
                success=False,
                message=f"Bilinmeyen arama motoru: '{engine}'. Geçerli motorlar: {valid}.",
            )

        url = template.format(query=quote_plus(query))
        success, error = _open_in_browser(url)
        if not success:
            return ToolResult(success=False, message=error or "Arama açılamadı.")

        return ToolResult(
            success=True, message=f"'{query}' için {engine} araması açıldı.", data={"url": url}
        )


@register_tool
class WebOpenUrlTool(BaseTool):
    """Verilen URL'yi varsayılan tarayıcıda açar.

    Yalnızca `http`/`https` şemalarına izin verilir. URL'yi LLM ürettiği
    için `file:`, `javascript:`, `data:` gibi yerel dosya/script şemaları
    reddedilir ve bu durumda tarayıcı hiç açılmaz (bkz. modül
    dokümantasyonundaki güvenlik notu). Şeması olmayan bir girdiye
    (örn. "github.com") otomatik olarak "https://" ön eki eklenir.
    """

    name = "web.open_url"
    description = "Verilen URL'yi varsayılan tarayıcıda açar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Açılacak http(s) adresi."}},
            "required": ["url"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_url = arguments["url"].strip()
        parsed = urlparse(raw_url)

        if not parsed.scheme:
            raw_url = f"https://{raw_url}"
            parsed = urlparse(raw_url)

        if parsed.scheme not in _ALLOWED_URL_SCHEMES:
            return ToolResult(
                success=False,
                message=(
                    f"'{parsed.scheme}' şeması desteklenmiyor. Yalnızca http/https "
                    "adresleri açılabilir."
                ),
            )

        success, error = _open_in_browser(raw_url)
        if not success:
            return ToolResult(success=False, message=error or "URL açılamadı.")

        return ToolResult(success=True, message=f"'{raw_url}' açıldı.", data={"url": raw_url})
