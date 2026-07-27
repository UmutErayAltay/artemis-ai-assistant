"""`plugins/mcp_plugin.py` testleri için GERÇEK, minik bir stdio MCP sunucusu.

Bilerek mock DEĞİL — `python bu_dosya.py` ile stdio üzerinden gerçek MCP
protokolünü konuşan bağımsız bir süreçtir. Testler bunu `MCPServerConfig`
ile (`command=sys.executable, args=[bu_dosyanın_yolu]`) gerçekten
başlatıp gerçek bir keşif+çağrı turu yapar. Yalnızca stdio taşımasını
sınamak için üç kasıtlı basit tool içerir; başka hiçbir amaca hizmet
etmez.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo-test-server")


@server.tool()
def echo(text: str) -> str:
    """Verilen metni aynen döndürür."""

    return text


@server.tool()
def add(a: int, b: int) -> int:
    """İki sayıyı toplar."""

    return a + b


@server.tool()
def always_fails() -> str:
    """Testler için kasıtlı olarak her zaman hata fırlatır."""

    raise RuntimeError("kasıtlı test hatası")


if __name__ == "__main__":
    server.run()
