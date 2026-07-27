"""`OllamaLLMClient.extract_tool_calls` ve `build_system_prompt` testleri.

Bu testler gerçek bir Ollama sunucusuna ihtiyaç duymaz: yalnızca "model
şunu söyleseydi, doğru ayrıştırır mıydık?" sorusunu, sentetik (elle
yazılmış) örnek çıktılarla sınar.
"""

from __future__ import annotations

import pytest

from core.llm_client import LLMResponseParseError, OllamaLLMClient
from core.plugin_loader import TOOL_REGISTRY, load_plugins
from core.prompt_builder import build_system_prompt


@pytest.fixture(autouse=True)
def _load_all_plugins() -> None:
    load_plugins()


def test_extract_plain_json_object() -> None:
    raw = '{"tool": "filesystem.open", "arguments": {"target": "app.py"}}'
    calls = OllamaLLMClient.extract_tool_calls(raw)
    assert calls == [{"tool": "filesystem.open", "arguments": {"target": "app.py"}}]


def test_extract_json_list_for_multi_step() -> None:
    raw = (
        '[{"tool": "windows.launch_app", "arguments": {"name": "chrome"}}, '
        '{"tool": "filesystem.search", "arguments": {"query": "rapor"}}]'
    )
    calls = OllamaLLMClient.extract_tool_calls(raw)
    assert len(calls) == 2
    assert calls[0]["tool"] == "windows.launch_app"
    assert calls[1]["tool"] == "filesystem.search"


def test_extract_json_wrapped_in_markdown_fence() -> None:
    raw = '```json\n{"tool": "filesystem.delete", "arguments": {"target": "x.txt"}}\n```'
    calls = OllamaLLMClient.extract_tool_calls(raw)
    assert calls == [{"tool": "filesystem.delete", "arguments": {"target": "x.txt"}}]


def test_extract_json_with_surrounding_explanation_text() -> None:
    # Küçük/yerel modeller bazen talimata rağmen JSON'un etrafına
    # açıklama ekler; bu durumda regex ile JSON bloğu bulunmalı.
    raw = 'Tabii, işte tool çağrısı:\n{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}\nUmarım yardımcı olur.'
    calls = OllamaLLMClient.extract_tool_calls(raw)
    assert calls == [{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]


def test_extract_garbage_raises_parse_error() -> None:
    with pytest.raises(LLMResponseParseError):
        OllamaLLMClient.extract_tool_calls("Açıyorum, bir saniye lütfen...")


def test_build_system_prompt_contains_registered_tools() -> None:
    prompt = build_system_prompt()
    assert "filesystem.open" in prompt
    assert "windows.shutdown" in prompt
    assert "{tool_manifest}" not in prompt  # yer tutucu doldurulmuş olmalı


def test_response_schema_is_always_an_array_of_tool_calls() -> None:
    """`_response_schema` HER ZAMAN bir dizi şeması döndürmeli (tek obje
    DEĞİL) — aksi halde grammar-constrained decoding, modelin çok adımlı
    bir plan (JSON listesi) üretmesini fiziksel olarak engeller ve
    `core/planner.py::TaskPlanner` her zaman tek adımlık bir plan alır."""

    schema = OllamaLLMClient._response_schema()
    assert schema["type"] == "array"
    assert schema["minItems"] == 1

    item_schema = schema["items"]
    assert item_schema["type"] == "object"
    assert item_schema["properties"]["tool"]["enum"]
    assert "filesystem.open" in item_schema["properties"]["tool"]["enum"]
    assert set(item_schema["required"]) == {"tool", "arguments"}


def test_response_schema_tool_enum_matches_tool_registry() -> None:
    schema = OllamaLLMClient._response_schema()
    assert schema["items"]["properties"]["tool"]["enum"] == sorted(TOOL_REGISTRY.keys())


def test_get_tool_calls_retries_after_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """İlk cevap geçersiz, ikinci cevap (düzeltme sonrası) geçerli olmalı."""

    import types

    responses = iter(
        [
            "Açıyorum, bir saniye...",  # geçersiz
            '{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}',  # düzeltme sonrası geçerli
        ]
    )

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = lambda model, messages, format=None, options=None, **extra: {
        "message": {"content": next(responses)}
    }
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model")
    calls = client.get_tool_calls("sistem promptu", "Orbit'i aç", max_retries=2)
    assert calls == [{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]


def test_get_tool_calls_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = lambda model, messages, format=None, options=None, **extra: {
        "message": {"content": "hep gecersiz kalacak"}
    }
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model")
    with pytest.raises(LLMResponseParseError):
        client.get_tool_calls("sistem promptu", "bir seyler yap", max_retries=1)


def test_get_tool_calls_returns_multi_step_plan_under_schema_constrained_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESYON: `_response_schema` eskiden `type: object` döndürdüğü için,
    varsayılan (şema-kısıtlı) stratejide model bir JSON listesi ASLA
    üretemiyordu ve `planner.py::TaskPlanner`'a her zaman tek adımlık bir
    plan ulaşıyordu — `prompts/system_prompt.md`'nin "birden fazla işlem
    istiyorsa liste üret" talimatı bu strateji altında etkisizdi. Şema artık
    her zaman bir dizi dayattığı için, model çok adımlı bir plan (2 elemanlı
    JSON listesi) döndürdüğünde bu, `get_tool_calls()`'dan gerçekten 2
    elemanlı bir liste olarak çıkmalı."""

    import types

    def _fake_chat(model, messages, options=None, **extra):
        # Bu, ilk (ve burada tek başarılı) strateji olmalı: şema-kısıtlı
        # format, ve şema HER ZAMAN bir dizi olmalı — bkz. _response_schema.
        assert isinstance(extra.get("format"), dict)
        assert extra["format"]["type"] == "array"
        return {
            "message": {
                "content": (
                    '[{"tool": "windows.launch_app", "arguments": {"name": "chrome"}}, '
                    '{"tool": "filesystem.search", "arguments": {"query": "rapor"}}]'
                )
            }
        }

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model")
    calls = client.get_tool_calls("sistem promptu", "chrome'u ac ve rapor dosyasini bul")

    assert len(calls) == 2
    assert calls[0] == {"tool": "windows.launch_app", "arguments": {"name": "chrome"}}
    assert calls[1] == {"tool": "filesystem.search", "arguments": {"query": "rapor"}}


def test_chat_falls_back_to_plain_json_format_on_schema_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Şema-kısıtlı format hata verirse, genel format='json' moduna düşülmeli."""

    import types

    calls_made = []

    def _fake_chat(model, messages, options=None, **extra):
        calls_made.append(extra.get("format"))
        if isinstance(extra.get("format"), dict):
            raise RuntimeError("bu ollama surumu json-schema format desteklemiyor")
        return {"message": {"content": '{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}'}}

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model")
    raw = client.get_raw_response("sistem promptu", "Orbit'i ac")
    assert "filesystem.open" in raw
    assert isinstance(calls_made[0], dict)  # once semali format denendi
    assert calls_made[1] == "json"  # sonra genel json'a dusuldu


def test_native_tool_calling_returns_structured_calls_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model native tools parametresini destekliyorsa, metin ayrıştırmaya hiç gerek kalmamalı."""

    import types

    def _fake_chat(model, messages, options=None, **extra):
        assert "tools" in extra  # native mod acikken 'tools' parametresi gonderilmis olmali
        return {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "filesystem.create_folder", "arguments": {"name": "Orbit"}}}
                ],
            }
        }

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model", use_native_tool_calling=True)
    calls = client.get_tool_calls("sistem promptu", "Orbit klasoru olustur")
    assert calls == [{"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}}]


def test_native_tool_calling_falls_back_to_text_when_model_ignores_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model 'tools' parametresini desteklemiyorsa (yok sayıp düz metin dönerse) metne düşülmeli."""

    import types

    def _fake_chat(model, messages, options=None, **extra):
        return {"message": {"content": '{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}'}}

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model", use_native_tool_calling=True)
    calls = client.get_tool_calls("sistem promptu", "Orbit'i ac")
    assert calls == [{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}]


def test_keep_alive_is_passed_to_ollama_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAM tasarrufu için keep_alive değeri her isteğe gerçekten iletilmeli."""

    import types

    received = {}

    def _fake_chat(model, messages, options=None, keep_alive=None, **extra):
        received["keep_alive"] = keep_alive
        return {"message": {"content": '{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}'}}

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    client = OllamaLLMClient(model="fake-model", keep_alive="2m")
    client.get_raw_response("sistem promptu", "Orbit'i ac")
    assert received["keep_alive"] == "2m"


def test_thinking_is_disabled_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESYON: `think=False` her tool-seçim isteğine gitmeli.

    Ollama, düşünme yeteneği olan modellerde bu alan gönderilmezse
    varsayılan olarak düşünmeyi AÇIK kabul eder. Tool seçimi düşünme
    gerektirmeyen bir sınıflandırma işi olduğundan bu, doğruluğa hiçbir
    şey katmadan gecikmeyi ~3.5 katına çıkarır (ölçüm için bkz.
    `core/llm_client.py` modül dokümantasyonu) — ve kullanıcı bunu
    doğrudan bekleme süresi olarak hisseder.
    """

    import types

    received = {}

    def _fake_chat(model, messages, options=None, keep_alive=None, think=None, **extra):
        received["think"] = think
        return {"message": {"content": '{"tool": "filesystem.open", "arguments": {"target": "Orbit"}}'}}

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    OllamaLLMClient(model="fake-model").get_raw_response("sistem promptu", "Orbit'i ac")
    assert received["think"] is False


def test_command_gate_also_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Komut kapısı da her duyulan cümlede çalışır; orada da düşünme kapalı olmalı.

    Kapı, uyandırma sonrası duyulan HER cümle için çağrılır. Burada
    unutulan bir `think` alanı, gecikmeyi tool seçiminden bile önce
    geri getirir.
    """

    import types

    received = {}

    def _fake_chat(model, messages, format=None, options=None, keep_alive=None, think=None, **extra):
        received["think"] = think
        return {"message": {"content": '{"karar": "YONELIK"}'}}

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = _fake_chat
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    OllamaLLMClient(model="fake-model").should_engage("masaustunde klasor olustur")
    assert received["think"] is False


def test_gate_engages_for_questions_not_only_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESYON (README §20f): kapı yalnızca İŞLEMLERİ değil, asistana

    yönelik SORU/SOHBETİ de tool seçimine göndermeli. Eski ikili kapı
    "komut mu?" diye soruyordu ve "sen kimsin?" gibi soruları
    KOMUT_DEGIL sayıp tool seçimine hiç ulaştırmıyordu — halbuki
    `prompts/system_prompt.md` bu girdi için AÇIK bir `assistant.reply`
    örneği içeriyor. Sınır artık "işlem mi" değil "asistana yönelik mi";
    bu testte modelin YONELIK dediği bir soru True dönmeli.
    """

    import types

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = lambda **kw: {"message": {"content": '{"karar": "YONELIK"}'}}
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    assert OllamaLLMClient(model="fake-model").should_engage("sen kimsin?") is True


def test_gate_rejects_background_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gerçek gürültü (asistana yönelik olmayan arka plan konuşması) hâlâ engellenmeli."""

    import types

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = lambda **kw: {"message": {"content": '{"karar": "GURULTU"}'}}
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)

    assert OllamaLLMClient(model="fake-model").should_engage("bilmiyorum ya öyle bir şey işte") is False
