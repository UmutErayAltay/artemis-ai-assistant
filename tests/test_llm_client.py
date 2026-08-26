"""`OllamaLLMClient.extract_tool_calls` ve `build_system_prompt` testleri.

Bu testler gerçek bir Ollama sunucusuna ihtiyaç duymaz: yalnızca "model
şunu söyleseydi, doğru ayrıştırır mıydık?" sorusunu, sentetik (elle
yazılmış) örnek çıktılarla sınar.
"""

from __future__ import annotations

import pytest

from core.llm_client import MAX_PLAN_STEPS, LLMResponseParseError, OllamaLLMClient
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


# --- Grammar düştüğünde Python'ın devraldığı denetimler (README §35) -----
#
# `_response_schema()` üç şeyi grammar ile FİZİKSEL olarak dayatır: her
# eleman `tool`/`arguments` alanlı bir nesnedir, `tool` gerçek bir tool
# adıdır, plan en fazla `MAX_PLAN_STEPS` adımdır. Ama `_chat` dört
# stratejiyi sırayla dener ve son ikisinde (`format="json"`, kısıtsız)
# şema hiç GÖNDERİLMEZ — o anda üç dayatma da düşer. Aşağıdaki testler,
# o düşüşte Python tarafının aynı sözleşmeyi koruduğunu doğrular.


def test_plan_longer_than_the_limit_is_rejected_not_silently_truncated() -> None:
    """Sınırı aşan plan REDDEDİLİR; sessizce ilk N adıma indirilmez.

    Sessiz kırpma, kullanıcıyı "5 şey istedim, 3'ü oldu" durumunda
    bırakırdı — bu, dürüstçe başarısız olmaktan kötüdür.
    """

    one = '{"tool": "assistant.reply", "arguments": {"message": "x"}}'
    raw = "[" + ", ".join([one] * (MAX_PLAN_STEPS + 1)) + "]"

    with pytest.raises(LLMResponseParseError) as exc:
        OllamaLLMClient.extract_tool_calls(raw)

    assert str(MAX_PLAN_STEPS) in str(exc.value)


def test_plan_exactly_at_the_limit_is_accepted() -> None:
    """Sınırın KENDİSİ geçerli olmalı — off-by-one bir regresyon olurdu."""

    one = '{"tool": "assistant.reply", "arguments": {"message": "x"}}'
    raw = "[" + ", ".join([one] * MAX_PLAN_STEPS) + "]"

    assert len(OllamaLLMClient.extract_tool_calls(raw)) == MAX_PLAN_STEPS


def test_list_of_strings_fails_cleanly_instead_of_crashing_the_loop() -> None:
    """ÇÖKME DÜZELTMESİ: dize listesi geçerli JSON'dur ama tool-call değil.

    Bu çıktı `format="json"` altında olasıdır. Denetim olmadan
    `planner.execute_plan` bir `str` üzerinde `.get()` çağırır,
    `AttributeError` fırlar ve onu ne `conversation_loop.run` ne de
    `VoiceAssistant._handle_one_command` yakalar — sohbet döngüsü ya da
    ses işçisi ÖLÜR. Artık düzgün bir ayrıştırma hatası olur.
    """

    with pytest.raises(LLMResponseParseError):
        OllamaLLMClient.extract_tool_calls('["filesystem.delete", "windows.shutdown"]')


def test_hallucinated_tool_name_is_rejected() -> None:
    """Şemasız stratejide model var olmayan bir tool adı uydurabilir.

    Grammar'daki `enum` kısıtının Python karşılığı budur.
    """

    with pytest.raises(LLMResponseParseError) as exc:
        OllamaLLMClient.extract_tool_calls('[{"tool": "filesystem.format_disk", "arguments": {}}]')

    assert "filesystem.format_disk" in str(exc.value)


def test_non_dict_arguments_are_rejected() -> None:
    with pytest.raises(LLMResponseParseError):
        OllamaLLMClient.extract_tool_calls('[{"tool": "assistant.reply", "arguments": "merhaba"}]')


def test_empty_list_is_rejected() -> None:
    """Şema `minItems: 1` der; şemasız stratejide boş liste gelebilir."""

    with pytest.raises(LLMResponseParseError):
        OllamaLLMClient.extract_tool_calls("[]")


def test_validation_also_covers_the_native_tool_calling_path() -> None:
    """Native yol da AYNI denetimden geçmeli — yarım güvence güvence değil.

    `_extract_native_tool_calls` Ollama'nın yapısını çevirir ama içeriğini
    doğrulamaz; adı `None` olan bir çağrı bile üretebilir.
    """

    client = OllamaLLMClient(model="test")
    uydurma = [{"tool": "windows.format_c", "arguments": {}}]

    def _fake_chat(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, object]] | None]:
        return "", uydurma

    client._chat = _fake_chat  # type: ignore[method-assign]

    with pytest.raises(LLMResponseParseError):
        client.get_tool_calls("sistem", "bir şey yap", max_retries=0)


# --- Şema <-> prompt sözleşmesi (CLAUDE.md'nin açık kuralı) --------------
#
# README §16a'nın dersi: bir güvenilirlik katmanı eklerken o katmanın başka
# bir özelliğin sözleşmesini daraltıp daraltmadığı kontrol edilmeli.
# Grammar-constrained decoding, şemanın YASAKLADIĞI bir biçimi modelin
# ÜRETMESİNİ fiziksel olarak imkânsız kılar — yani ikisi aynı şeyi
# söylemek ZORUNDA. Aşağıdakiler o eşitliğin bekçileridir.


def test_schema_forbids_extra_fields_just_like_the_prompt_does() -> None:
    """Prompt `"response"` gibi fazladan alanı YASAKLIYOR; şema da yasaklamalı.

    Bir dönem prompt yasaklıyor, şema izin veriyordu: kural yalnızca bir
    talimattı, fiziksel bir kısıt değildi.
    """

    schema = OllamaLLMClient._response_schema()

    assert schema["items"]["additionalProperties"] is False
    assert "tool" in schema["items"]["properties"]
    assert "arguments" in schema["items"]["properties"]


def test_step_limit_is_stated_in_the_prompt_not_only_in_the_schema() -> None:
    """`maxItems` prompta yazılmazsa model sınırı bilmeden aşar.

    Grammar üretimi ortadan keser; ne model ne kullanıcı sebebini öğrenir.
    """

    prompt = build_system_prompt()

    assert str(MAX_PLAN_STEPS) in prompt, (
        f"Sistem promptu {MAX_PLAN_STEPS} adım sınırından hiç söz etmiyor; "
        "şema onu dayatıyor ama model bilmiyor."
    )


def test_schema_step_limit_matches_the_constant() -> None:
    assert OllamaLLMClient._response_schema()["maxItems"] == MAX_PLAN_STEPS


def test_command_gate_schema_enum_matches_the_gate_constants() -> None:
    """Kapının şeması ve sabitleri ayrı ayrı yazılmış İKİNCİ bir sözleşme.

    `should_engage`, modelin cevabını `_GATE_ENGAGE` ile karşılaştırır;
    şema ise `enum` ile neyin üretilebileceğini belirler. İkisi ayrışırsa
    kapı SESSİZCE hep aynı cevabı verir — üstelik "karar verilemezse
    True" kuralı yüzünden hep AÇIK tarafa düşerek.
    """

    from core.llm_client import _COMMAND_GATE_SCHEMA, _GATE_ENGAGE, _GATE_NOISE

    enum_values = _COMMAND_GATE_SCHEMA["properties"]["karar"]["enum"]

    assert set(enum_values) == {_GATE_ENGAGE, _GATE_NOISE}
    assert _GATE_ENGAGE != _GATE_NOISE
