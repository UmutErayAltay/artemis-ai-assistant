"""MCP (Model Context Protocol) sunucularının tool'larını Artemis'e bağlar.

MİMARİ SORUN VE ÇÖZÜMÜ: her plugin'deki tool'lar, `@register_tool` ile
İMPORT ZAMANINDA, STATİK olarak `TOOL_REGISTRY`'e kaydedilir; sistem
promptunun `{tool_manifest}`'i de bu sabit registry'den üretilir (bkz.
`core/manifest.py`). MCP sunucuları ise tool'larını ÇALIŞMA ZAMANINDA,
sunucuya bağlanıp `list_tools()` çağırarak DİNAMİK olarak açıklar — bu
iki model doğrudan uyuşmaz.

Çözüm: MCP tool'ları, keşfedildikleri anda (bu modül import edilirken)
gerçek bir `BaseTool` alt sınıfına DÖNÜŞTÜRÜLÜP normal registry'e
kaydedilir (`_make_mcp_tool_class`). Bu sayede dispatcher, planner
(zincir referansları dahil — bir MCP tool'unun `data`'sına da
`{{step_N.alan}}` ile referans verilebilir), manifest üretimi ve şema
doğrulaması hiçbir özel durum eklenmeden, MCP tool'larını sıradan bir
Artemis tool'undan AYIRT ETMEDEN çalışır.

NEDEN KEŞİF İMPORT ZAMANINDA (ve neden bu güvenli): LLM bir tool'u
YALNIZCA manifest'te görürse isteyebilir; manifest de yalnızca bir kez,
uygulama başlarken üretilir. Yani keşif gecikmeli/tembel olsaydı, MCP
tool'ları LLM'e hiç görünmezdi (tavuk-yumurta sorunu). Bu, VARSAYILAN
OLARAK zararsızdır: `config.yaml::mcp_servers` BOŞ listedir, yani bu
modülün import edilmesi hiçbir ağ/süreç I/O'su YAPMAZ — bu da test
paketinin (her test dosyası `load_plugins()` çağırır) hızlı ve
sunucusuz kalmasını garanti eder (bkz. CLAUDE.md test kuralları).
Kullanıcı bir sunucu YAPILANDIRIRSA, o sunucuya bağlanma denemesi kısa
bir zaman aşımıyla (`MCPServerConfig.timeout_seconds`) sarılıdır ve
`try/except` ile korunur: yanıt vermeyen/bozuk BİR sunucu, DİĞER
plugin'lerin veya tüm uygulamanın açılışını KİLİTLEMEZ/ÇÖKERTMEZ —
yalnızca o sunucunun tool'ları eksik kalır, uyarı loglanır.

v1 KAPSAMI — yalnızca stdio (yerel süreç) taşıması: MCP sunucularının
büyük çoğunluğu (`npx ...`, `python -m ...`) bu şekilde çalışır. Uzak/
HTTP sunucular (SSE) ayrı bir kimlik-doğrulama/gizli-anahtar yönetimi
gerektirir (bkz. `get_groq_api_key` deseni) ve hiçbir kullanıcı bunu
istemedi — hipotetik bir ihtiyaç için şimdiden eklenmedi. Aynı desen
(`mcp.client.sse`) izlenerek ileride eklenebilir.

GÜVENLİK — `danger_level`: bir MCP sunucusu Artemis'in kendi yazdığı/
denetlediği kod DEĞİLDİR, üçüncü taraf bir süreçtir. Bu yüzden bir
sunucunun tool'ları, o sunucu `config.yaml`'da `trusted: true`
İŞARETLENMEDİKÇE `DangerLevel.CONFIRM_REQUIRED` kaydedilir — projedeki
"delete/shutdown/... geri alınamaz işlemler onay ister" ilkesinin ÖTESİNDE
bir önlemdir, çünkü buradaki risk işlemin geri alınabilirliği değil,
KODUN KAYNAĞININ GÜVENİLİRLİĞİDİR.

HER TOOL ÇAĞRISI TAZE BİR BAĞLANTI AÇAR: `BaseTool` örnekleri her
dispatch'te sıfırdan oluşturulur (bkz. `core/dispatcher.py::_execute`,
`tool = tool_cls()`), yani örnekler arası kalıcı bir MCP oturumu
TUTULAMAZ. Bunun yerine her `execute()` çağrısı sunucuya yeniden bağlanır,
tek bir tool çağırır, bağlantıyı kapatır — `voice/stt_cloud.py`/
`stt_azure.py`'nin her istekte taze bir HTTP bağlantısı açması gibi aynı
desenin yerel-süreç karşılığı. Senkron `BaseTool.execute()` ile asenkron
`mcp` SDK'sı arasındaki köprü `asyncio.run()` ile kurulur
(`voice/tts_cloud.py::_ensure_no_running_event_loop` ile aynı desen).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult

logger = logging.getLogger(__name__)

_TOOL_NAME_PREFIX = "mcp"


def _ensure_no_running_event_loop(caller: str) -> None:
    """`asyncio.run()` çağrılmadan önce çalışan bir olay döngüsü olmadığını doğrular.

    `voice/tts_cloud.py`'deki aynı adlı fonksiyonla AYNI gerekçe: bu bir
    kullanım/programlama hatasıdır (yanlış thread'den çağrı), sunucunun
    erişilemez olmasıyla ilgisi yoktur ve sessizce yutulmamalıdır.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # çalışan döngü yok -> beklenen/normal durum

    raise RuntimeError(
        f"{caller}, zaten çalışan bir asyncio olay döngüsü içinden çağrılamaz "
        "(asyncio.run() iç içe çalışamaz)."
    )


async def _list_server_tools_async(server: Any) -> list[Any]:
    """Bir MCP sunucusuna bağlanıp `list_tools()` çağırır."""

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=server.command, args=server.args, env=server.env or None)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def _call_server_tool_async(server: Any, tool_name: str, arguments: dict[str, Any]) -> Any:
    """Bir MCP sunucusuna TAZE bir bağlantıyla bağlanıp tek bir tool çağırır."""

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=server.command, args=server.args, env=server.env or None)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, arguments)


def _call_tool_result_to_tool_result(call_result: Any) -> ToolResult:
    """MCP'nin `CallToolResult`'ını Artemis'in `ToolResult`'ına çevirir."""

    from mcp import types

    text_parts = [block.text for block in call_result.content if isinstance(block, types.TextContent)]
    message = "\n".join(text_parts).strip() or ("MCP tool hata döndürdü." if call_result.isError else "Tamamlandı.")

    return ToolResult(
        success=not call_result.isError,
        message=message,
        data=call_result.structuredContent,
    )


def _make_mcp_tool_class(server: Any, tool_def: Any) -> type[BaseTool]:
    """Keşfedilen tek bir MCP tool'unu gerçek bir `BaseTool` alt sınıfına çevirir.

    `get_arguments_schema()` doğrudan `tool_def.inputSchema`'yı döndürür
    — bu zaten geçerli bir JSON Schema'dır (MCP'nin kendi sözleşmesi
    gereği), yani `core/dispatcher.py::_validate_arguments` ve manifest
    üretimi hiçbir çeviri katmanı gerektirmeden onu doğrudan kullanabilir.
    """

    resolved_name = f"{_TOOL_NAME_PREFIX}.{server.name}.{tool_def.name}"
    resolved_description = tool_def.description or f"'{server.name}' MCP sunucusundan '{tool_def.name}' tool'u."
    resolved_danger_level = DangerLevel.SAFE if server.trusted else DangerLevel.CONFIRM_REQUIRED
    resolved_schema = dict(tool_def.inputSchema) if tool_def.inputSchema else {"type": "object", "properties": {}}

    class _MCPTool(BaseTool):
        # NOT: sağdaki adlar `resolved_*` — soldakiyle AYNI olsaydı (örn.
        # `name = name`), sınıf gövdesi bu satırı "önce oku sonra ata"
        # değil, RHS'yi de sınıfın KENDİ (henüz boş) ad alanında arayan
        # bir yerel değişken gibi ele alır ve `NameError` fırlatır — bu
        # ölçülerek doğrulandı, sezgiyle değil.
        name = resolved_name
        danger_level = resolved_danger_level

        def __init__(self) -> None:
            self.description = resolved_description

        def get_arguments_schema(self) -> dict[str, Any]:
            return resolved_schema

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            _ensure_no_running_event_loop(f"{resolved_name}.execute()")
            try:
                call_result = asyncio.run(
                    asyncio.wait_for(
                        _call_server_tool_async(server, tool_def.name, arguments),
                        timeout=server.timeout_seconds,
                    )
                )
            except TimeoutError:
                return ToolResult(
                    success=False,
                    message=f"'{server.name}' MCP sunucusu {server.timeout_seconds:.0f} sn içinde cevap vermedi.",
                )
            except Exception as exc:  # noqa: BLE001 - sunucu süreci/protokolü her türlü hatayı verebilir
                return ToolResult(success=False, message=f"'{server.name}' MCP sunucusuna ulaşılamadı: {exc}")

            return _call_tool_result_to_tool_result(call_result)

    _MCPTool.__name__ = f"MCPTool_{server.name}_{tool_def.name}"
    return _MCPTool


def _discover_server_tools(server: Any) -> list[Any]:
    """Tek bir MCP sunucusuna bağlanıp tool listesini döndürür.

    Sunucu yanıt vermezse/bozuksa BOŞ liste döner ve uyarı loglanır —
    bu sunucunun keşfi, diğer sunucuların veya uygulamanın geri kalanının
    açılışını ASLA engellemez (bkz. modül dokümantasyonu).
    """

    try:
        return asyncio.run(asyncio.wait_for(_list_server_tools_async(server), timeout=server.timeout_seconds))
    except TimeoutError:
        logger.warning(
            "MCP sunucusu '%s' %d sn içinde tool listesi döndürmedi, atlanıyor.",
            server.name,
            server.timeout_seconds,
        )
        return []
    except Exception as exc:  # noqa: BLE001 - sunucu süreci başlatılamayabilir/protokol hatası verebilir
        logger.warning("MCP sunucusu '%s' keşfedilemedi, atlanıyor: %s", server.name, exc)
        return []


def discover_and_register_mcp_tools(servers: list[Any]) -> int:
    """Verilen MCP sunucularının tümünü keşfedip `TOOL_REGISTRY`'e kaydeder.

    Modülden AYRI, doğrudan çağrılabilir bir fonksiyon olarak dışa
    açılmıştır ki testler gerçek `config.yaml`'a ya da `get_settings()`'in
    global `lru_cache`'ine bağımlı kalmadan, kendi sunucu listeleriyle bu
    mantığı sınayabilsin.

    Returns:
        Başarıyla kaydedilen tool sayısı.
    """

    registered = 0
    for server in servers:
        for tool_def in _discover_server_tools(server):
            register_tool(_make_mcp_tool_class(server, tool_def))
            registered += 1

    if registered:
        logger.info("%d MCP tool'u kaydedildi (%d sunucu).", registered, len(servers))

    return registered


def _bootstrap() -> None:
    """Import zamanında `config.yaml::mcp_servers`'ı okuyup keşfi tetikler.

    `mcp_servers` BOŞSA (varsayılan) bu fonksiyon hiçbir I/O yapmadan
    anında döner — bkz. modül dokümantasyonu, "NEDEN KEŞİF İMPORT
    ZAMANINDA" bölümü.
    """

    from config.settings import get_settings

    servers = get_settings().mcp_servers
    if not servers:
        return

    discover_and_register_mcp_tools(servers)


_bootstrap()
