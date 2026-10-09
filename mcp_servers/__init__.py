"""Artemis'in KENDİ YAZDIĞI MCP sunucuları.

`plugins/` bir "tool kategorisi" klasörüdür: içindeki modüller ANA Artemis
sürecinde import edilir, `@register_tool` ile `TOOL_REGISTRY`'ye kaydedilir
ve LLM'e manifest olarak tanıtılır. Buradaki modüller ise bunun TAM TERSİ
olan iki ayrı süreçtir:

  * `config.yaml::mcp_servers` bir girdi olarak verildiğinde
    `plugins/mcp_plugin.py` onu AYRI bir alt süreç olarak başlatır,
  * stdio üzerinden MCP protokolünü konuşurlar, tool'larını kendileri
    AÇIKLAR (Artemis'e gömülü değildirler),
  * kullanıcı onayını `trusted: true` verdiği için her çağrıda geçer
    (bkz. `config/settings.py::MCPServerConfig`).

Klasör adının `plugins/` ile kardeş olması bilinçlidir: `core/plugin_loader.py`
yalnızca `plugins/` klasörünü tarar, yani buradaki hiçbir modül Artemis'i
başlatırken import edilmez.

LAZY IMPORT KURALI BURADA GEÇERLİ DEĞİLDİR: `plugins/*.py`'de ağır/Windows'a
özgü bağımlılıklar (pyautogui, pywin32, psutil) `execute()` içinde tembel
import edilir; bunun sebebi ana süreçte gereksiz RAM/açılış yükü ve Linux'ta
import hatasıdır. Bu modüller zaten AYRI bir süreçte yaşar ve bu süreç
konfigürasyonda etkinleştirilene kadar hiç başlatılmaz — yani ağır
import'un tek maliyeti etkin bir MCP sunucusu olduğunda ödenir.
"""
