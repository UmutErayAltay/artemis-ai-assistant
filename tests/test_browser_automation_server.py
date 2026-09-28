"""`mcp_servers/browser_automation_server.py` testleri.

GERÇEK, MOCK'SUZ. `tests/fixtures/mcp_echo_server.py` ile aynı desen:
sunucu `python -m mcp_servers.browser_automation_server` komutuyla AYRI bir
alt süreç olarak başlatılır, stdio üzerinden gerçek MCP protokolü
konuşulur, tool'lar `discover_and_register_mcp_tools` ile keşfedilir ve
`ToolDispatcher` üzerinden çağrılır. Tarayıcı da gerçektir — chromium
başlatılır, `tests/fixtures/sample_page.html` `file://` üzerinden açılır.

Ağ KULLANILMAZ: hedef `file://` URL'idir, `http://example.com` değil. Test
deterministiktir; sayfada ne zaman aşımı ne de yarış durumu vardır.

ÖNEMLİ İZOLASYON KURALI (tests/test_mcp_plugin.py'nin başındaki kural):
`TOOL_REGISTRY` süreç genelinde paylaşılan bir global'dir. Burada kaydedilen
`mcp.browser.*` tool'ları test bitince SİLİNMESE `tests/test_prompt_
builder.py`'nin 14.000 karakter sınırını (gerçek tool'lar 13.533 karakterde)
şişirip ilgisiz bir testi flaky biçimde kırar. Teardown bu yüzden zorunlu.

PLAYWRIGHT_BROWSERS_PATH: `mcp` SDK'sı alt sürece YALNIZCA
`HOME/LOGNAME/PATH/SHELL/TERM/USER` değişkenlerini miras aldırır
(`mcp.client.stdio.get_default_environment`). Yani `PLAYWRIGHT_
BROWSERS_PATH` gibi bir ortam değişkeni, kullanıcı bunu export etmiş olsa
bile MCP alt sürecine ULAŞMAZ. Chromium standart önbellek konumunda
(`~/.cache/ms-playwright`) kurulu değilse bu testler "sunucu tarayıcıyı
bulamadı" mesajıyla düşer — kodla ilgisi olmayan bir kurulum arızası.
`MCPServerConfig.env` tam olarak bu boşluk içindir: gerekli değişkeni test
ortamından açıkça geçiriyoruz.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import MCPServerConfig, Settings
from core.dispatcher import ToolDispatcher
from core.enums import DangerLevel
from core.plugin_loader import TOOL_REGISTRY
from core.tool_base import ToolContext
from memory.context_memory import ContextMemory
from plugins.mcp_plugin import discover_and_register_mcp_tools

_FIXTURE_PAGE = Path(__file__).parent / "fixtures" / "sample_page.html"
_TOOL = "mcp.browser.run_browser_task"

# Chromium ikilisi kurulu değilse testler "sunucu tarayıcıyı açamadı" mesajıyla
# düşer. Bu bir KOD arızası değil, bir KURULUM arızasıdır; ama kullanıcıya
# `python -m playwright install chromium` adımını söyleyemeyen bir test de
# işe yaramaz — bu yüzden yalnızca "ikili gerçekten var mı" diye bakılır.
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as _p:
        _p.chromium.launch(headless=True).close()
    _CHROMIUM_AVAILABLE = True
except Exception:  # noqa: BLE001 - kurulum yoksa/yoksa sürüm uyuşmuyorsa
    _CHROMIUM_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _CHROMIUM_AVAILABLE,
    reason=("Chromium ikilisi kurulu değil. Bir kez çalıştırın: `python -m playwright install chromium`"),
)


def _make_server(name: str, **overrides) -> MCPServerConfig:
    fields = {
        "name": name,
        "command": sys.executable,
        # `python -m ...` REPO KÖKÜNDEN çalıştırılmalı; alt süreç çalışma
        # dizinini miras alır (pytest repo kökünden çalışır).
        "args": ["-m", "mcp_servers.browser_automation_server"],
        "timeout_seconds": 120.0,
        # `mcp` SDK'sı yalnızca HOME/PATH/... miras aldırır; bu olmadan test
        # ortamındaki özel Chromium önbelleği alt sürece ulaşmaz.
        "env": {k: v for k, v in os.environ.items() if k in {"PLAYWRIGHT_BROWSERS_PATH"}},
    }
    fields.update(overrides)
    return MCPServerConfig(**fields)


def _deregister(prefix: str) -> None:
    for tool_name in [n for n in TOOL_REGISTRY if n.startswith(prefix)]:
        del TOOL_REGISTRY[tool_name]


def _context(tmp_path: Path) -> ToolContext:
    """`execute()`'a elle çağrı yapabilmek için gerçek bir ToolContext.

    MCP tool'ları normalde dispatcher üzerinden çağrılır; bu yardımcı
    yalnızca dispatcher'ın AŞILMASI gereken senaryolarda (sunucu tarafı
    şema reddi gibi) kullanılır.
    """

    settings = Settings(
        desktop_path=tmp_path / "Desktop",
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolContext(
        settings=settings,
        memory=ContextMemory(settings.db_path),
        logger=logging.getLogger(__name__),
    )


@pytest.fixture
def browser_server() -> MCPServerConfig:
    """Gerçek tarayıcı sunucusunu keşfeder, kaydeder; test bitince TEMİZLER."""

    server = _make_server("browser", trusted=True)
    registered = discover_and_register_mcp_tools([server])
    if registered == 0:
        pytest.fail(f"{_TOOL} keşfedilemedi — sunucu başlamadı ya da tool listesini döndürmedi.")
    yield server
    _deregister(f"mcp.{server.name}.")


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path / "Desktop",
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


@pytest.fixture
def page_url() -> str:
    """Test edilecek statik sayfanın `file://` URL'i (ağ gerektirmez)."""

    return _FIXTURE_PAGE.resolve().as_uri()


# --------------------------------------------------------------------------
# Keşif + şema (mock yok: gerçek alt süreç konuşuyor)
# --------------------------------------------------------------------------


def test_discovers_the_browser_tool(browser_server: MCPServerConfig) -> None:
    assert _TOOL in TOOL_REGISTRY


def test_registered_tool_is_safe_because_server_is_trusted(browser_server: MCPServerConfig) -> None:
    """`trusted: true` -> `DangerLevel.SAFE`. `plugins/mcp_plugin.py`
    güvenlik notu: üçüncü taraf süreçler varsayılan olarak CONFIRM_REQUIRED
    kaydedilir; bu sunucu Artemis'in KENDİ kodu olduğu için istisnadır."""

    assert TOOL_REGISTRY[_TOOL]().danger_level == DangerLevel.SAFE


def test_schema_exposes_actions_and_step_shape(browser_server: MCPServerConfig) -> None:
    """Modele gösterilen şema, `steps` içindeki `action` seçeneklerini
    İÇERMEZSE LLM geçersiz bir action uydurur ve çağrı başarısız olur.

    `steps` bir pydantic modeli (`Step`) olduğu için şema dizinin
    `items` alanında `$ref` ile gösterir; tanım `$defs` altındadır. Bu,
    sunucunun `list[dict]` yerine model kullanmasının somut sonucu: çıplak
    `dict` listesinden yalnızca `{"type": "object"}` çıkardı ve `action`
    seçenekleri modele HİÇ ulaşamıyordu."""

    schema = TOOL_REGISTRY[_TOOL]().get_arguments_schema()
    properties = schema["properties"]

    assert "url" in properties and "steps" in properties
    step_ref = properties["steps"]["items"]["$ref"]
    step_props = schema["$defs"][step_ref.rsplit("/", 1)[-1]]["properties"]

    assert set(step_props["action"]["enum"]) == {"click", "fill", "read_text", "wait_for"}
    assert "selector" in step_props
    # `selector` zorunlu, `value`/`timeout_ms` opsiyonel:
    required = set(schema["$defs"][step_ref.rsplit("/", 1)[-1]]["required"])
    assert required == {"action", "selector"}


def test_dispatcher_rejects_missing_url_before_reaching_the_server(
    dispatcher: ToolDispatcher, browser_server: MCPServerConfig
) -> None:
    result = dispatcher.dispatch({"tool": _TOOL, "arguments": {}})

    assert result.success is False
    assert "url" in result.message


def test_server_rejects_an_unknown_action_in_steps(browser_server: MCPServerConfig, tmp_path: Path) -> None:
    """Bilinmeyen `action` sunucu tarafında reddedilmelidir: `steps` dizisinin
    İÇİNDEKİ alanları dispatcher'ın merkezi şema kontrolünden geçmez
    (`core/dispatcher.py::_validate_arguments` yalnızca üst düzey alanlara
    bakar). Doğrulamayı `Step` modeli yapar."""

    tool = TOOL_REGISTRY[_TOOL]()
    result = tool.execute(
        {"url": "file:///tmp/yok.html", "steps": [{"action": "teleport", "selector": "#x"}]},
        context=_context(tmp_path),
    )

    assert result.success is False


# --------------------------------------------------------------------------
# Uçtan uca: gerçek sayfa, gerçek tarayıcı, gerçek DOM
# --------------------------------------------------------------------------


def test_multi_step_flow_fills_clicks_and_reads(dispatcher: ToolDispatcher, browser_server, page_url: str) -> None:
    """Başarılı çok adımlı akış: git + fill + click + read_text.

    `read_text` sonucu, `#greet` düğmesine basıldıktan SONRA okunur; yani
    Playwright oturumunun ADIMLAR ARASI state koruduğu ancak burada
    kanıtlanır (iki ayrı tool çağrısı olsaydı bu sonuç boş gelirdi —
    `plugins/mcp_plugin.py` "HER TOOL ÇAĞRISI TAZE BİR BAĞLANTI AÇAR")."""

    result = dispatcher.dispatch(
        {
            "tool": _TOOL,
            "arguments": {
                "url": page_url,
                "steps": [
                    {"action": "fill", "selector": "#name", "value": "Artemis"},
                    {"action": "click", "selector": "#greet"},
                    {"action": "read_text", "selector": "#greeting"},
                ],
            },
        }
    )

    assert result.success is True
    # Sunucu hata FIRLATMADI; sadece tool çağrısı başarılı döndü.
    assert result.data["success"] is True
    assert result.data["steps_executed"] == 3
    assert result.data["read_texts"] == {"#greeting": "Merhaba Artemis!"}


def test_dispatcher_reports_honest_failure_with_partial_results(
    dispatcher: ToolDispatcher, browser_server, page_url: str
) -> None:
    """Var olmayan selector'da adım BAŞARISIZ olur — ama istisna değil.

    "Koşulsuz success=True yasak" (CLAUDE.md): adım hatası dürüstçe
    `success=False` ve `failed_step` ile bildirilir, `partial_results`
    ise o ana kadar toplanan okumaları KORUR."""

    result = dispatcher.dispatch(
        {
            "tool": _TOOL,
            "arguments": {
                "url": page_url,
                "steps": [
                    {"action": "read_text", "selector": "#status"},
                    {"action": "click", "selector": "#boyle-bir-ey-yok", "timeout_ms": 1000},
                ],
            },
        }
    )

    assert result.data["success"] is False
    assert result.data["failed_step"] == 1
    assert "#boyle-bir-ey-yok" in result.data["error"]
    # İlk adımın okuması kaybolmadı:
    assert result.data["partial_results"] == {"#status": "hazir"}


def test_empty_steps_only_navigates_and_returns_title(
    dispatcher: ToolDispatcher, browser_server, page_url: str
) -> None:
    """`steps: []` geçerli bir kullanımdır: sadece git + title/url döndür."""

    result = dispatcher.dispatch({"tool": _TOOL, "arguments": {"url": page_url, "steps": []}})

    assert result.data["success"] is True
    assert result.data["steps_executed"] == 0
    assert result.data["read_texts"] == {}
    assert result.data["title"] == "Artemis Test Sayfası"


def test_unreachable_url_fails_honestly_without_crashing(dispatcher: ToolDispatcher, browser_server) -> None:
    """Olmayan bir dosya: sunucu çökmemeli, `success=False` dönmeli."""

    result = dispatcher.dispatch(
        {"tool": _TOOL, "arguments": {"url": "file:///tmp/artemis-boyle-bir-dosya-yok.html", "steps": []}}
    )

    assert result.data["success"] is False
    assert "Sayfa açılamadı" in result.data["error"]


def test_plain_python_step_model_validates_and_defaults_timeout() -> None:
    """`Step` saf Python'dur: alt süreç/tarayıcı gerekmeden doğrulanır ve
    varsayılan bekleme süresi burada hesaplanır."""

    from mcp_servers.browser_automation_server import _DEFAULT_TIMEOUT_MS, Step

    step = Step(action="click", selector="#a")
    assert step.timeout() == _DEFAULT_TIMEOUT_MS
    assert Step(action="fill", selector="#b", value="x", timeout_ms=500).timeout() == 500

    with pytest.raises(ValidationError):
        Step(action="teleport", selector="#x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Step(action="click")  # type: ignore[call-arg]  # selector zorunlu
