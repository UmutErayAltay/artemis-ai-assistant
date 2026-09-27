"""`core/llm_router.py` için testler: bulut/yerel otomatik geçişi.

Bu dosya, hibrit BEYİN katmanının KARAR mantığını doğrular — gerçek
OpenRouter'a ya da Ollama'ya hiç ihtiyaç duymadan, sahte istemcilerle.
`tests/test_voice_router.py`'in karşılığıdır: orada `transcribe`/`speak`,
burada `get_tool_calls`/`should_engage`/`get_raw_response` taşınır; karar
mantığı kasıtlı olarak aynı olduğu için TESTLERİ de aynı düşünceyle
sıralanmıştır.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from core.llm_client import LLMResponseParseError
from core.llm_router import LLMProviderMode, LLMRouter


class FakeLLM:
    """Sabit cevap döndüren ya da hata fırlatan sahte LLM istemcisi.

    Üç metodun da kaydını tutar, çünkü karar mantığı metodun İSMİNİ
    görmez — hangi metodun çağrıldığını ayırt etmek, yönlendiricinin
    "her çağrıyı aynı biçimde sarmaladığı" iddiasının kanıtıdır.
    """

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    def _answer(self, method: str) -> str:
        self.calls.append(method)
        if self.error is not None:
            raise self.error
        return self.text

    def get_raw_response(self, system_prompt: str, user_input: str) -> str:
        return self._answer("raw")

    def get_tool_calls(
        self, system_prompt: str, user_input: str, max_retries: int = 2
    ) -> list[dict[str, Any]]:
        return self._answer("tools")

    def should_engage(self, user_input: str) -> bool:
        return self._answer("gate")


def _router(cloud: FakeLLM, local: FakeLLM, **kwargs: Any) -> LLMRouter:
    return LLMRouter(lambda: cloud, lambda: local, **kwargs)


# --------------------------------------------------------------------------
# Temel yönlendirme
# --------------------------------------------------------------------------


def test_auto_mode_prefers_cloud_when_it_works() -> None:
    cloud, local = FakeLLM("bulut"), FakeLLM("yerel")

    assert _router(cloud, local).get_raw_response("s", "u") == "bulut"
    assert local.calls == [], "Bulut çalışırken yerel hiç çağrılmamalı"


def test_auto_mode_falls_back_to_local_on_cloud_failure() -> None:
    cloud = FakeLLM(error=RuntimeError("ağ yok"))
    local = FakeLLM("yerel")

    assert _router(cloud, local).get_raw_response("s", "u") == "yerel"


def test_parse_failure_is_also_a_reason_to_fall_back() -> None:
    """Bulut "ulaşıldı" ama anlaşılmaz bir cevap verdiyse bu da başarısızlıktır.

    Yönlendirici yalnızca ağ hatalarına değil, modelin kendi düzeltme
    denemelerinden sonra da üretemediği ayrıştırılamayan yanıtlara da
    düşer: kullanıcı "şunu yap" dediğinde soru cevabın nereden geldiği
    değil, bir şekilde doğru cevabı almaktır.
    """

    cloud = FakeLLM(error=LLMResponseParseError("JSON değil"))
    local = FakeLLM("yerel")

    assert _router(cloud, local).get_tool_calls("s", "u") == "yerel"


def test_local_mode_never_touches_cloud() -> None:
    """GİZLİLİK: 'local' modda komut kesinlikle dışarı çıkmamalı."""

    cloud, local = FakeLLM("bulut"), FakeLLM("yerel")
    router = _router(cloud, local, mode=LLMProviderMode.LOCAL)

    assert router.get_raw_response("s", "u") == "yerel"
    assert cloud.calls == [], "'local' modda bulut istemcisi ÇAĞRILMAMALI"


def test_cloud_mode_propagates_error_instead_of_silently_falling_back() -> None:
    """Kullanıcı 'yalnızca bulut' dediyse sessizce yerele düşmek tercihini çiğner."""

    cloud = FakeLLM(error=RuntimeError("402"))
    local = FakeLLM("yerel")
    router = _router(cloud, local, mode=LLMProviderMode.CLOUD)

    with pytest.raises(RuntimeError):
        router.get_raw_response("s", "u")
    assert local.calls == []


def test_invalid_mode_is_rejected_early() -> None:
    with pytest.raises(ValueError):
        _router(FakeLLM(), FakeLLM(), mode="yarim-bulut")


# --------------------------------------------------------------------------
# Üç metodun tamamı aynı karar mantığından geçmeli — sözleşmenin tamamı
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,args",
    [
        ("get_raw_response", ("s", "u")),
        ("get_tool_calls", ("s", "u")),
        ("should_engage", ("u",)),
    ],
)
def test_every_method_falls_back_to_local(method: str, args: tuple[str, ...]) -> None:
    """Yönlendirici bir "call" sarmalayıcısıdır: hangi metod olursa olsun
    aynı kararı verir. Metod başına ayrı dallanma yazılsa, ileride
    eklenen bir metot sessizce bulutsuz kalsın diye bu test var."""

    cloud = FakeLLM(error=RuntimeError("ağ yok"))
    local = FakeLLM("yerel")
    router = _router(cloud, local)

    assert getattr(router, method)(*args) == "yerel"
    assert local.calls, f"{method} yerele düşmedi"


@pytest.mark.parametrize("method", ["get_raw_response", "get_tool_calls", "should_engage"])
def test_every_method_skips_cloud_in_local_mode(method: str) -> None:
    cloud, local = FakeLLM("bulut"), FakeLLM("yerel")
    router = _router(cloud, local, mode=LLMProviderMode.LOCAL)

    args = ("u",) if method == "should_engage" else ("s", "u")
    getattr(router, method)(*args)

    assert cloud.calls == []


# --------------------------------------------------------------------------
# Soğuma (cooldown) — internet kapalıyken her komutta 30 sn beklememeli
# --------------------------------------------------------------------------


def test_cloud_is_skipped_during_cooldown_after_a_failure() -> None:
    """Bir hatadan sonra bulut bir süre HİÇ denenmemeli.

    Soğuma olmasaydı, internet kapalıyken her komut önce
    `openrouter_timeout_seconds` (30 sn) beklerdi ve asistan kullanılamaz
    hale gelirdi — bir LLM'nin 30 saniye boyunca hiç yanıt vermemesi,
    sesin 10 saniye beklemesinden çok daha rahatsız edicidir.
    """

    cloud = FakeLLM(error=RuntimeError("ağ yok"))
    local = FakeLLM("yerel")
    router = _router(cloud, local, failure_cooldown=300.0)

    router.get_raw_response("s", "bir")
    router.get_raw_response("s", "iki")
    router.get_raw_response("s", "uc")

    assert len(cloud.calls) == 1, "Soğuma sırasında bulut tekrar denenmemeliydi"
    assert len(local.calls) == 3


def test_cloud_is_retried_after_cooldown_expires() -> None:
    """Soğuma dolunca bulut yeniden denenmeli (internet geri gelmiş olabilir)."""

    cloud = FakeLLM(error=RuntimeError("ağ yok"))
    local = FakeLLM("yerel")
    router = _router(cloud, local, failure_cooldown=0.0)  # anında dolan soğuma

    router.get_raw_response("s", "bir")
    router.get_raw_response("s", "iki")

    assert len(cloud.calls) == 2, "Soğuma dolduğunda bulut yeniden denenmeliydi"


# --------------------------------------------------------------------------
# Tembellik (lazy) — bulut çalışırken yerel model belleğe hiç girmemeli
# --------------------------------------------------------------------------


def test_local_client_is_never_constructed_while_cloud_works() -> None:
    """Bu, "bilgisayarı kastırmadan kullanma" vaadinin EN SOMUT testidir.

    Yönlendiricinin `local_factory`'si çağrılmadığında `OllamaLLMClient`
    hiç kurulmaz; modelin ağırlıkları da hiç yüklenmez.
    """

    built: list[str] = []

    def local_factory() -> FakeLLM:
        built.append("yerel")
        return FakeLLM("yerel")

    router = LLMRouter(lambda: FakeLLM("bulut"), local_factory)
    router.get_raw_response("s", "bir")
    router.get_raw_response("s", "iki")

    assert built == [], "Bulut çalışırken yerel istemci oluşturulmamalıydı"


def test_factories_are_called_at_most_once() -> None:
    """Her çağrıda fabrika yeniden çalıştırılırsa istemci (ve dolayısıyla
    bağlantı/ayar durumu) her seferinde baştan kurulur."""

    built: list[str] = []

    def cloud_factory() -> FakeLLM:
        built.append("bulut")
        return FakeLLM("bulut")

    def local_factory() -> FakeLLM:
        built.append("yerel")
        return FakeLLM("yerel")

    router = LLMRouter(cloud_factory, local_factory)
    for _ in range(3):
        router.get_raw_response("s", "u")

    assert built == ["bulut"]


# --------------------------------------------------------------------------
# Sağlayıcı değişimi logu — TEŞHİS: log'a bakınca bulut mu yerel mi
# kullanıldığı anlaşılabilmeli, ama her çağrıda değil (log şişmesin).
# --------------------------------------------------------------------------


def _provider_log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Yalnızca sağlayıcı-kullanımı log satırlarının metnini döndürür."""

    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "LLM sağlayıcı" in record.getMessage()
    ]


def test_first_successful_cloud_call_logs_the_provider(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    cloud, local = FakeLLM("bulut"), FakeLLM("yerel")

    _router(cloud, local).get_raw_response("s", "u")

    messages = _provider_log_messages(caplog)
    assert len(messages) == 1
    assert "bulut" in messages[0]


def test_same_provider_used_repeatedly_is_logged_only_once(caplog: pytest.LogCaptureFixture) -> None:
    """AYNI sağlayıcı arka arkaya kullanılırsa log TEKRARLANMAMALI — yoksa
    bu satır her komutta tekrarlanıp log dosyasını şişirir."""

    caplog.set_level(logging.INFO)
    cloud, local = FakeLLM("bulut"), FakeLLM("yerel")
    router = _router(cloud, local)

    router.get_raw_response("s", "bir")
    router.get_raw_response("s", "iki")
    router.get_raw_response("s", "uc")

    assert len(_provider_log_messages(caplog)) == 1


def test_fallback_from_cloud_to_local_logs_the_new_provider(caplog: pytest.LogCaptureFixture) -> None:
    """Bulut→yerel düşüşünde yerel sağlayıcının kullanıldığı loglanmalı."""

    caplog.set_level(logging.INFO)
    cloud = FakeLLM(error=RuntimeError("ağ yok"))
    local = FakeLLM("yerel")

    _router(cloud, local).get_raw_response("s", "u")

    messages = _provider_log_messages(caplog)
    assert len(messages) == 1
    assert "yerel" in messages[0]


def test_provider_switch_from_cloud_to_local_is_logged_again(caplog: pytest.LogCaptureFixture) -> None:
    """Önce bulut başarılı olup loglandıktan sonra bulut bozulup yerele
    düşülürse — sağlayıcı GERÇEKTEN değiştiği için ikinci bir log satırı
    yazılmalı (yukarıdaki 'tekrarlanmamalı' kuralıyla çelişmez: burada
    sağlayıcı AYNI değil, FARKLI)."""

    caplog.set_level(logging.INFO)
    cloud = FakeLLM("bulut")
    local = FakeLLM("yerel")
    router = _router(cloud, local, failure_cooldown=0.0)

    router.get_raw_response("s", "bir")  # bulut çalışıyor -> loglanır
    cloud.error = RuntimeError("aniden koptu")
    router.get_raw_response("s", "iki")  # bulut bozuldu -> yerele düşer -> tekrar loglanır

    messages = _provider_log_messages(caplog)
    assert len(messages) == 2
    assert "bulut" in messages[0]
    assert "yerel" in messages[1]


def test_local_mode_never_logs_a_cloud_provider(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    cloud, local = FakeLLM("bulut"), FakeLLM("yerel")
    router = _router(cloud, local, mode=LLMProviderMode.LOCAL)

    router.get_raw_response("s", "bir")
    router.get_raw_response("s", "iki")

    messages = _provider_log_messages(caplog)
    assert len(messages) == 1
    assert "yerel" in messages[0]
