"""`plugins/mcp_plugin.py` testleri için GERÇEK, minik bir stdio MCP sunucusu.

Bilerek mock DEĞİL — `python bu_dosya.py` ile stdio üzerinden gerçek MCP
protokolünü konuşan bağımsız bir süreçtir. Testler bunu `MCPServerConfig`
ile (`command=sys.executable, args=[bu_dosyanın_yolu]`) gerçekten
başlatıp gerçek bir keşif+çağrı turu yapar. Yalnızca stdio taşımasını
sınamak için üç kasıtlı basit tool içerir; başka hiçbir amaca hizmet
etmez.
"""

from __future__ import annotations

# `mcp` 2.0'da `FastMCP` -> `MCPServer` olarak yeniden adlandırıldı.
# İkisini de deneriz: bu depo `mcp>=1.20` diyor ve kullanıcının
# ortamındaki sürümü kontrol edemiyoruz. Bir TEST fixture'ının, testi
# çalıştıran kişinin kurulu sürümü yüzünden kırılması, sınadığı şeyle
# ilgisiz bir başarısızlıktır.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server

server = _Server("echo-test-server")


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
