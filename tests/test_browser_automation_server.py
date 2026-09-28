"""`mcp_servers/browser_automation_server.py` testleri.

GERÇEK, MOCK'SUZ. `tests/fixtures/mcp_echo_server.py` ile aynı desen:
sunucu `python -m mcp_servers.browser_automation_server` komutuyla AYRI bir
alt süreç olarak başlatılır, stdio üzerinden gerçek MCP protokolü
konuşulur, tool'lar `discover_and_register_mcp_tools` ile keşfedilir ve
`ToolDispatcher` üzerinden çağrılır. Tarayıcı da gerçektir — chromium
başlatılır, `tests/fixtures/sample_page.html` bir HTTP sunucusundan
gerçekten indirilir.

AĞ KULLANILMAZ: hedef, bu dosyanın kendi `_page_server` fixture'ıdır —
`http.server` ile 127.0.0.1 üzerinde açılan, TEK dosyayı servis eden yerel
bir sunucu. İnternet'e çıkılmaz. Determinizm bozulmamıştır: sunucu sabit
bir porttan değil, port 0 (kernel seçsin) üzerinden açılır.

NEDEN `file://` DEĞİL (v3.7 güvenlik düzeltmesi): `run_browser_task` artık
`file://`'yi reddediyor — `page.goto("file:///home/kullanici/.ssh/id_rsa")`
o dosyayı tarayıcıya okutup `read_text` ile geri döndürebiliyordu. Testler
de bu davranışa göre yazıldı: mutlu yol (uçtan uca akış) artık HTTP
üzerinden, reddedilen yollar ise ayrı testler.

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
import socket
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_FIXTURE_PAGE = _FIXTURE_DIR / "sample_page.html"
_TOOL = "mcp.browser.run_browser_task"
_ALLOW_LOCAL_ENV = "ARTEMIS_BROWSER_ALLOW_LOCAL"

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
        # `_NAVIGATION_TIMEOUT_MS` 30 sn; bu değer ondan BÜYÜK olmalı, yoksa
        # üst katman önce döner ve kullanıcı gerçek hatayı göremez
        # (bkz. `_NAVIGATION_TIMEOUT_MS` dokümantasyonu).
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
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(
        desktop_path=tmp_path / "Desktop",
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


@pytest.fixture
def _page_server():
    """`sample_page.html`'yi 127.0.0.1 üzerinden servis eden gerçek HTTP
    sunucusu. İnternet'e çıkmaz; `ThreadingHTTPServer` + `daemon_threads`
    sayesinde test bittikten sonra süreç çıkışını bekletmez.

    Port 0 verildiği için sabit port çakışması da yok; kernel boş bir port
    seçer, `server.server_address` gerçek portu verir.
    """

    handler = partial(SimpleHTTPRequestHandler, directory=str(_FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def browser_server(_page_server: str) -> MCPServerConfig:
    """Gerçek tarayıcı sunucusunu keşfeder, kaydeder; test bitince TEMİZLER.

    `_ALLOW_LOCAL_ENV=1` SADECE bu fixture'ın sunucusuna verilir: yerel HTTP
    sunucusu loopback'te olduğu için mutlu yol testleri ancak bu şekilde
    geçebilir. Bu, o değişkenin ne işe yaradığını da kanıtlar; ama reddedilen
    adresler (aşağıdaki testler) bu değişken OLMADAN, sıfırdan bir sunucuyla
    denenir — aksi halde "reddediliyor" testi kendi kuralını ihlal eden bir
    sunucuyu test ediyor olurdu.
    """

    base = _make_server("browser", trusted=True)
    server = _make_server(
        "browser",
        trusted=True,
        env={**base.env, _ALLOW_LOCAL_ENV: "1"},
    )
    registered = discover_and_register_mcp_tools([server])
    if registered == 0:
        pytest.fail(f"{_TOOL} keşfedilemedi — sunucu başlamadı ya da tool listesini döndürmedi.")
    yield server
    _deregister(f"mcp.{server.name}.")


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
        {"url": "http://example.invalid/yok.html", "steps": [{"action": "teleport", "selector": "#x"}]},
        context=_context(tmp_path),
    )

    assert result.success is False


# --------------------------------------------------------------------------
# Uçtan uca: gerçek sayfa, gerçek tarayıcı, gerçek DOM
# --------------------------------------------------------------------------


def test_multi_step_flow_fills_clicks_and_reads(dispatcher: ToolDispatcher, browser_server, _page_server: str) -> None:
    """Başarılı çok adımlı akış: git + fill + click + read_text.

    `read_text` sonucu, `#greet` düğmesine basıldıktan SONRA okunur; yani
    Playwright oturumunun ADIMLAR ARASI state koruduğu ancak burada
    kanıtlanır (iki ayrı tool çağrısı olsaydı bu sonuç boş gelirdi —
    `plugins/mcp_plugin.py` "HER TOOL ÇAĞRISI TAZE BİR BAĞLANTI AÇAR")."""

    result = dispatcher.dispatch(
        {
            "tool": _TOOL,
            "arguments": {
                "url": f"{_page_server}/sample_page.html",
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
    dispatcher: ToolDispatcher, browser_server, _page_server: str
) -> None:
    """Var olmayan selector'da adım BAŞARISIZ olur — ama istisna değil.

    "Koşulsuz success=True yasak" (CLAUDE.md): adım hatası dürüstçe
    `success=False` ve `failed_step` ile bildirilir, `partial_results`
    ise o ana kadar toplanan okumaları KORUR."""

    result = dispatcher.dispatch(
        {
            "tool": _TOOL,
            "arguments": {
                "url": f"{_page_server}/sample_page.html",
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
    dispatcher: ToolDispatcher, browser_server, _page_server: str
) -> None:
    """`steps: []` geçerli bir kullanımdır: sadece git + title/url döndür."""

    result = dispatcher.dispatch(
        {"tool": _TOOL, "arguments": {"url": f"{_page_server}/sample_page.html", "steps": []}}
    )

    assert result.data["success"] is True
    assert result.data["steps_executed"] == 0
    assert result.data["read_texts"] == {}
    assert result.data["title"] == "Artemis Test Sayfası"


def test_unreachable_url_fails_honestly_without_crashing(
    dispatcher: ToolDispatcher, browser_server
) -> None:
    """Erişilemeyen adres: sunucu çökmemeli, `success=False` dönmeli.

    İKİ NEDENLE bu test `file:///tmp/yok.html` DEĞİL:
      1. `file://` artık şema kısıtına takılır; "Sayfa açılamadı" yoluna hiç
         ulaşamaz.
      2. Bir HTTP 404 de o yola ulaşmaz — `page.goto` 404 gövdesini başarıyla
         "yükler" (`wait_until="domcontentloaded"` sağlanır), yani 404 bir
         BAŞARISIZ navigasyon DEĞİLDİR. Gerçekten erişilemeyen adres =
         hiçbir sürecin dinlemediği bir port."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    result = dispatcher.dispatch(
        {"tool": _TOOL, "arguments": {"url": f"http://127.0.0.1:{closed_port}/yok.html", "steps": []}}
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


# --------------------------------------------------------------------------
# GÜVENLİK: url doğrulaması (v3.7)
#
# Bu blok iki düzeyde test eder:
#   1. Saf fonksiyon (`_validate_url`) — hızlı, tüm adres sınıfları.
#   2. Uçtan uca, GERÇEK alt süreç + gerçek chromium — reddedilen bir
#      adresin tool çağrısında istisna değil `{"success": false, ...}`
#      olarak döndüğünü kanıtlar.
#
# Uçtan uca testler `_ALLOW_LOCAL_ENV` VERİLMEYEN ayrı bir sunucu
# kullanır; yoksa "reddediliyor" testi, reddi açık olan bir sunucuyu
# test ediyor olurdu.
# --------------------------------------------------------------------------


def test_file_url_is_rejected_so_local_files_cannot_be_read() -> None:
    """ÖLÇÜLMÜŞ AÇIK: `file:///home/kullanici/.ssh/id_rsa` verildiğinde
    headless chromium o dosyayı okuyup `read_text` ile TAMAMINI döndürüyordu.
    Şema artık `http`/`https` ile sınırlı."""

    from mcp_servers.browser_automation_server import _validate_url

    rejection = _validate_url("file:///home/user/.ssh/id_rsa")

    assert rejection is not None
    assert "file" in rejection


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>Merhaba</h1>",
        "javascript:alert(1)",
        "ftp://example.com/x",
        "",
    ],
)
def test_non_http_schemes_are_all_rejected(url: str) -> None:
    """`file://` kadar özel durum DEĞİLDİR: `data:` ve `javascript:` de
    sayfa yüklemeden içerik üretir. `_ALLOWED_SCHEMES` beyaz listesi sayesinde
    yeni şemalar da otomatik reddedilir (varsayılan-kabul yok)."""

    from mcp_servers.browser_automation_server import _validate_url

    assert _validate_url(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        # loopback
        "http://127.0.0.1:8000/",
        "http://127.5.6.7/",
        "https://[::1]:8080/",
        "http://localhost:3000/",
        # private
        "http://10.0.0.5/admin",
        "http://172.16.4.1/",  # 172.16.0.0/12
        "http://172.31.255.254/",  # 172.16.0.0/12 üst sınırı
        "http://192.168.1.1/router",
        # link-local — bulut metadata uç noktası da buraya düşer
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/",
    ],
)
def test_internal_addresses_are_rejected_by_default(url: str) -> None:
    """SSRF koruması: tarayıcı kullanıcının kendi servislerine
    yönlendirilmemeli (Docker portları, geliştirici sunucuları, yönlendirici
    panelleri, bulut metadata). `169.254.169.254` özellikle önemli: bulut
    sağlayıcılarının kimlik bilgisi endpoint'i orada."""

    from mcp_servers.browser_automation_server import _validate_url

    assert _validate_url(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        # DNS'e girmeyen, "decimal IP" ile yazılmış loopback atlatmaları.
        "http://2130706433/",  # 127.0.0.1
        "http://0177.0.0.1/",  # oktal
        "http://[::ffff:127.0.0.1]/",  # IPv4-eşlenmiş IPv6
    ],
)
def test_encoded_loopback_bypasses_are_still_rejected(url: str) -> None:
    """Atlatma denemeleri: `ipaddress.ip_address` bu metinleri IP literal
    olarak tanıyıp DOĞRUDAN sınıflandırdığı için, DNS'e hiç uğramadan
    loopback'e düşerler. Bu yüzden şema kontrolünden SONRA ama host
    sınıflandırmasından ÖNCE konurlar."""

    from mcp_servers.browser_automation_server import _validate_url

    assert _validate_url(url) is not None


def test_allow_local_env_reopens_local_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kullanıcı kendi localhost servisini otomatikleştirmek isteyebilir:
    `ARTEMIS_BROWSER_ALLOW_LOCAL=1` host kısıtını kaldırır."""

    from mcp_servers.browser_automation_server import _validate_url

    monkeypatch.setenv(_ALLOW_LOCAL_ENV, "1")

    assert _validate_url("http://localhost:3000/") is None
    assert _validate_url("http://127.0.0.1:8000/") is None


def test_allow_local_env_still_refuses_non_http_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    """ÖNEMLİ: anahtar kelime "local" olsa da bu değişken `file://`'yi
    AÇMAZ. "localhost'u açmak" ile "dosya sistemini okumayı açmak" farklı
    risklerdir; ikincisini açmanın bir kullanım senaryosu yoktur."""

    from mcp_servers.browser_automation_server import _validate_url

    monkeypatch.setenv(_ALLOW_LOCAL_ENV, "1")

    assert _validate_url("file:///home/user/.ssh/id_rsa") is not None
    assert _validate_url("data:text/html,x") is not None
    assert _validate_url("javascript:alert(1)") is not None


def test_normal_public_urls_are_accepted() -> None:
    """Kısıt çok dar olmamalı: normal genel http(s) adresleri geçmeli.
    Burada `example.com` kullanılır çünkü `.invalid` gibi alanlar her
    ortamda çözülmez ve "DNS çözülemedi" meselesi karışır."""

    from mcp_servers.browser_automation_server import _validate_url

    assert _validate_url("https://example.com/") is None
    assert _validate_url("http://example.com/bir/yol?x=1") is None
    assert _validate_url("https://kullanici:parola@example.com/") is None


def test_rejected_url_fails_the_tool_call_without_raising(tmp_path: Path) -> None:
    """Uçtan uca: gerçek alt süreç + gerçek chromium, `_ALLOW_LOCAL_ENV`
    VERİLMEDİN. Reddedilen adres istisna fırlatmaz, diğer hata yollarıyla
    AYNI şekilde döner (`success: false`, `failed_step: None`) — yani
    dispatcher/LLM tarafında özel bir durum yoktur.

    `file://` dosyası GERÇEKTEN diskte oluşturulur: reddedilen şey
    "var olmayan bir yol" değil, dosya sistemini okuma yeteneğidir.

    `ToolResult.success` burada True kalır ve bu doğrudur: `plugins/
    mcp_plugin.py::_call_tool_result_to_tool_result` alanı MCP'nin
    `is_error` BAYRAGINDAN türetir, yani "tool çağrısı sonuç üretti mi"
    demektir — tool'un İÇİNDE ne döndüğü değil. Tool seviyesindeki sonuç
    `data["success"]` alanındadır; bu ayrım MCP köprüsünün mevcut
    sözleşmesidir ve TÜM MCP tool'larında böyledir."""

    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nGIZLI\n", encoding="utf-8")

    server = _make_server("browser-guard", trusted=True)
    registered = discover_and_register_mcp_tools([server])
    if registered == 0:
        pytest.fail("Sunucu başlamadı; reddedim testi çalıştırılamıyor.")
    try:
        tool = TOOL_REGISTRY[f"mcp.{server.name}.run_browser_task"]()
        result = tool.execute(
            {
                "url": secret.resolve().as_uri(),
                "steps": [{"action": "read_text", "selector": "body"}],
            },
            context=_context(tmp_path),
        )

        # Çağrı istisna fırlatmadı; tool seviyesinde dürüst bir hata döndü.
        assert result.data["success"] is False
        assert result.data["failed_step"] is None
        assert "şemasına izin verilmiyor" in result.data["error"]
        # Dosyanın içeriği NE okundu NE de sızdı:
        assert "GIZLI" not in str(result.data)
    finally:
        _deregister(f"mcp.{server.name}.")
