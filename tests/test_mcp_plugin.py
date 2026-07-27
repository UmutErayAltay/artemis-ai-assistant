"""`plugins/mcp_plugin.py` testleri.

Buradaki testlerin çoğu GERÇEK bir MCP sunucusu kullanır
(`tests/fixtures/mcp_echo_server.py`) — mock DEĞİL: stdio üzerinden
gerçek bir alt süreç başlatılır, gerçek MCP protokolü konuşulur. Bu,
mimarinin asıl riskli kısmını (süreç yönetimi, asenkron köprü, protokol
çevirisi) gerçekten sınar; sahte bir `ClientSession` bu hataların
çoğunu göstermezdi.

ÖNEMLİ İZOLASYON KURALI: `TOOL_REGISTRY` süreç genelinde PAYLAŞILAN bir
global'dir (bkz. `core/plugin_loader.py`). Burada kaydedilen test
tool'ları, testler bitince MUTLAKA `TOOL_REGISTRY`'den silinmelidir —
aksi halde `tests/test_prompt_builder.py`'nin 14.000 karakter sınırını
(gerçek tool'lar zaten 13.533 karakterde, bkz. README §26) yanlışlıkla
şişirip ilgisiz bir testi flaky biçimde kırabilir.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from config.settings import MCPServerConfig, Settings
from core.dispatcher import ToolDispatcher
from core.enums import DangerLevel
from core.planner import TaskPlanner
from core.plugin_loader import TOOL_REGISTRY, load_plugins
from memory.context_memory import ContextMemory
from plugins.mcp_plugin import discover_and_register_mcp_tools

load_plugins()

_FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def _make_server(name: str, **overrides) -> MCPServerConfig:
    fields = {
        "name": name,
        "command": sys.executable,
        "args": [str(_FIXTURE_SERVER.resolve())],
        "timeout_seconds": 15.0,
    }
    fields.update(overrides)
    return MCPServerConfig(**fields)


def _deregister(prefix: str) -> None:
    for tool_name in [n for n in TOOL_REGISTRY if n.startswith(prefix)]:
        del TOOL_REGISTRY[tool_name]


@pytest.fixture
def echo_server() -> MCPServerConfig:
    """Gerçek fixture sunucusunu keşfeder, kaydeder; test bitince TEMİZLER."""

    server = _make_server("echotest", trusted=True)
    discover_and_register_mcp_tools([server])
    yield server
    _deregister(f"mcp.{server.name}.")


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(desktop_path=tmp_path, db_path=tmp_path / "memory.db", log_dir=tmp_path / "logs")
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


# --------------------------------------------------------------------------
# Boş yapılandırma: sıfır I/O sözleşmesi
# --------------------------------------------------------------------------


def test_empty_server_list_registers_nothing_and_needs_no_io() -> None:
    before = dict(TOOL_REGISTRY)
    count = discover_and_register_mcp_tools([])
    assert count == 0
    assert TOOL_REGISTRY == before


def test_real_repo_config_registers_zero_mcp_tools() -> None:
    """`config/config.yaml`'daki gerçek varsayılan (`mcp_servers: []`) ile
    modül import edilirken hiçbir tool kaydedilmemeli — bu proje herkese
    açık bir depo, varsayılan davranış her zaman sıfır I/O olmalı."""

    assert not [n for n in TOOL_REGISTRY if n.startswith("mcp.") and "test" not in n]


# --------------------------------------------------------------------------
# Gerçek stdio sunucusuyla keşif + çağrı (mock değil)
# --------------------------------------------------------------------------


def test_discovers_real_server_and_registers_all_tools(echo_server: MCPServerConfig) -> None:
    assert "mcp.echotest.echo" in TOOL_REGISTRY
    assert "mcp.echotest.add" in TOOL_REGISTRY
    assert "mcp.echotest.always_fails" in TOOL_REGISTRY


def test_registered_tool_schema_comes_from_mcp_server(echo_server: MCPServerConfig) -> None:
    """`get_arguments_schema()`, sunucunun GERÇEKTEN açıkladığı şemayı yansıtmalı."""

    schema = TOOL_REGISTRY["mcp.echotest.echo"]().get_arguments_schema()
    assert schema["required"] == ["text"]
    assert schema["properties"]["text"]["type"] == "string"


def test_dispatcher_calls_real_mcp_tool_end_to_end(
    dispatcher: ToolDispatcher, echo_server: MCPServerConfig
) -> None:
    result = dispatcher.dispatch({"tool": "mcp.echotest.echo", "arguments": {"text": "merhaba mcp"}})

    assert result.success is True
    assert "merhaba mcp" in result.message
    assert result.data == {"result": "merhaba mcp"}


def test_dispatcher_calls_real_mcp_tool_with_numeric_arguments(
    dispatcher: ToolDispatcher, echo_server: MCPServerConfig
) -> None:
    result = dispatcher.dispatch({"tool": "mcp.echotest.add", "arguments": {"a": 3, "b": 4}})

    assert result.success is True
    assert result.data == {"result": 7}


def test_mcp_tool_exception_becomes_a_failed_tool_result_not_a_crash(
    dispatcher: ToolDispatcher, echo_server: MCPServerConfig
) -> None:
    result = dispatcher.dispatch({"tool": "mcp.echotest.always_fails", "arguments": {}})

    assert result.success is False
    assert "kasıtlı test hatası" in result.message


def test_missing_required_argument_is_caught_before_reaching_server(
    dispatcher: ToolDispatcher, echo_server: MCPServerConfig
) -> None:
    """Eksik zorunlu argüman, MCP sunucusuna hiç gitmeden dispatcher'ın
    KENDİ `_validate_arguments`'ında yakalanmalı (bkz. README §24) —
    MCP tool'ları da diğer tüm tool'larla AYNI merkezi güvenceden geçer.
    """

    result = dispatcher.dispatch({"tool": "mcp.echotest.echo", "arguments": {}})

    assert result.success is False
    assert "eksik zorunlu argüman" in result.message
    assert "text" in result.message


def test_planner_can_chain_mcp_tool_output_to_next_step(
    dispatcher: ToolDispatcher, echo_server: MCPServerConfig
) -> None:
    """`{{step_N.alan}}` referansları MCP tool'larıyla da ÖZEL bir kod
    olmadan çalışmalı — mimarinin asıl amacı buydu (bkz. modül
    dokümantasyonu, `core/planner.py` §25). `echo`'nun `data["result"]`'ı
    string olduğu için ikinci adımın `text` (string) alanına tip
    uyuşmazlığı olmadan akar (bkz. `_type_matches` — int'in string
    alana akması AYRI ve bilerek reddedilen bir durumdur, farklı testte
    doğrulanıyor)."""

    planner = TaskPlanner(dispatcher, confirm_callback=lambda t, a: True)

    plan = [
        {"tool": "mcp.echotest.echo", "arguments": {"text": "zincirlenen deger"}},
        {"tool": "mcp.echotest.echo", "arguments": {"text": "{{step_1.result}}"}},
    ]
    results = planner.execute_plan(plan)

    assert all(step.result.success for step in results)
    assert results[1].arguments["text"] == "zincirlenen deger"


def test_planner_rejects_type_mismatched_mcp_chain(
    dispatcher: ToolDispatcher, echo_server: MCPServerConfig
) -> None:
    """Bir sayısal MCP çıktısı (`add` -> `data["result"]`, int) bir string
    şemasına (`echo`'nun `text`'i) akıtılırsa, dispatcher'ın merkezi
    argüman doğrulaması bunu REDDETMELİ — zincirleme, tip güvenliğini
    atlamamalı."""

    planner = TaskPlanner(dispatcher, confirm_callback=lambda t, a: True)

    plan = [
        {"tool": "mcp.echotest.add", "arguments": {"a": 10, "b": 5}},
        {"tool": "mcp.echotest.echo", "arguments": {"text": "{{step_1.result}}"}},
    ]
    results = planner.execute_plan(plan)

    assert results[0].result.success is True
    assert results[1].result.success is False
    assert "string" in results[1].result.message


# --------------------------------------------------------------------------
# Güven modeli: `trusted` bayrağı danger_level'i belirler
# --------------------------------------------------------------------------


def test_untrusted_server_tools_require_confirmation() -> None:
    server = _make_server("untrusted_test", trusted=False)
    try:
        discover_and_register_mcp_tools([server])
        tool = TOOL_REGISTRY["mcp.untrusted_test.echo"]()
        assert tool.danger_level == DangerLevel.CONFIRM_REQUIRED
    finally:
        _deregister(f"mcp.{server.name}.")


def test_trusted_server_tools_are_safe() -> None:
    server = _make_server("trusted_test", trusted=True)
    try:
        discover_and_register_mcp_tools([server])
        tool = TOOL_REGISTRY["mcp.trusted_test.echo"]()
        assert tool.danger_level == DangerLevel.SAFE
    finally:
        _deregister(f"mcp.{server.name}.")


def test_untrusted_mcp_tool_actually_asks_for_confirmation_via_dispatcher(
    dispatcher: ToolDispatcher,
) -> None:
    """Uçtan uca: güvenilmeyen bir MCP tool'u, gerçekten dispatcher'ın
    onay akışını (`requires_confirmation`) tetiklemeli."""

    server = _make_server("untrusted_e2e", trusted=False)
    try:
        discover_and_register_mcp_tools([server])
        result = dispatcher.dispatch({"tool": "mcp.untrusted_e2e.echo", "arguments": {"text": "x"}})
        assert result.requires_confirmation is True
        assert result.success is False
    finally:
        _deregister(f"mcp.{server.name}.")


# --------------------------------------------------------------------------
# Hata toleransı: bir sunucunun bozukluğu diğerlerini/uygulamayı etkilemez
# --------------------------------------------------------------------------


def test_unreachable_server_fails_gracefully_without_crashing() -> None:
    server = _make_server("broken", command="bu-komut-hicbir-yerde-yok-artemis-test")
    count = discover_and_register_mcp_tools([server])
    assert count == 0
    assert not [n for n in TOOL_REGISTRY if n.startswith("mcp.broken.")]


def test_slow_server_times_out_without_hanging_the_whole_app() -> None:
    """Yanıt vermeyen bir sunucu, diğer sunucuların/uygulamanın açılışını
    KİLİTLEMEMELİDİR — gerçekçi olmayan kısa bir zaman aşımıyla bunu
    doğrudan tetikleriz (gerçek sunucu süreci bu sürede asla ayağa
    kalkıp cevap veremez)."""

    server = _make_server("timeout_test", timeout_seconds=0.001)
    count = discover_and_register_mcp_tools([server])
    assert count == 0


def test_one_broken_server_does_not_block_another_working_server() -> None:
    broken = _make_server("broken2", command="bu-da-yok-artemis-test")
    working = _make_server("works", trusted=True)
    try:
        count = discover_and_register_mcp_tools([broken, working])
        assert count == 3  # working sunucusunun 3 tool'u
        assert "mcp.works.echo" in TOOL_REGISTRY
        assert not [n for n in TOOL_REGISTRY if n.startswith("mcp.broken2.")]
    finally:
        _deregister("mcp.works.")


# --------------------------------------------------------------------------
# Saf Python mantığı — alt süreç GEREKMEZ (hızlı testler)
# --------------------------------------------------------------------------


def test_call_tool_result_translates_success_with_structured_data() -> None:
    from mcp import types

    from plugins.mcp_plugin import _call_tool_result_to_tool_result

    call_result = types.CallToolResult(
        content=[types.TextContent(type="text", text="42")],
        structuredContent={"result": 42},
        isError=False,
    )

    result = _call_tool_result_to_tool_result(call_result)

    assert result.success is True
    assert result.message == "42"
    assert result.data == {"result": 42}


def test_call_tool_result_translates_error() -> None:
    from mcp import types

    from plugins.mcp_plugin import _call_tool_result_to_tool_result

    call_result = types.CallToolResult(
        content=[types.TextContent(type="text", text="bir şeyler ters gitti")],
        isError=True,
    )

    result = _call_tool_result_to_tool_result(call_result)

    assert result.success is False
    assert "ters gitti" in result.message


def test_ensure_no_running_event_loop_raises_inside_a_loop() -> None:
    from plugins.mcp_plugin import _ensure_no_running_event_loop

    async def _inside() -> None:
        _ensure_no_running_event_loop("test.execute()")

    with pytest.raises(RuntimeError, match="asyncio"):
        asyncio.run(_inside())


def test_ensure_no_running_event_loop_passes_outside_a_loop() -> None:
    from plugins.mcp_plugin import _ensure_no_running_event_loop

    _ensure_no_running_event_loop("test.execute()")  # exception atmamalı
