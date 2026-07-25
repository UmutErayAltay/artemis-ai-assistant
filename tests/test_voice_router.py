"""`voice/router.py` için testler: bulut/yerel otomatik geçişi.

Bu dosya, hibrit ses katmanının KARAR mantığını doğrular — gerçek bulut
veya yerel sağlayıcıya hiç ihtiyaç duymadan, sahte sağlayıcılarla.
Önemli olan hangi sağlayıcının çağrıldığı ve hata durumunda ne olduğudur.
"""

from __future__ import annotations

import logging

import pytest

from voice.router import ProviderMode, SpeechToTextRouter, TextToSpeechRouter


class FakeSTT:
    """Sabit metin döndüren ya da hata fırlatan sahte tanıyıcı."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[bytes, str | None]] = []

    def transcribe(self, pcm_bytes: bytes, hotwords: str | None = None) -> str:
        self.calls.append((pcm_bytes, hotwords))
        if self.error is not None:
            raise self.error
        return self.text


class FakeTTS:
    """Söylenenleri kaydeden ya da hata fırlatan sahte konuşucu."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.spoken: list[str] = []
        self.stops = 0

    def speak(self, text: str, on_amplitude=None) -> None:
        if self.error is not None:
            raise self.error
        self.spoken.append(text)

    def stop(self) -> None:
        self.stops += 1


def _stt_router(cloud: FakeSTT, local: FakeSTT, **kwargs) -> SpeechToTextRouter:
    return SpeechToTextRouter(lambda: cloud, lambda: local, **kwargs)


# --------------------------------------------------------------------------
# Temel yönlendirme
# --------------------------------------------------------------------------


def test_auto_mode_prefers_cloud_when_it_works() -> None:
    cloud, local = FakeSTT("bulut"), FakeSTT("yerel")

    assert _stt_router(cloud, local).transcribe(b"ses") == "bulut"
    assert local.calls == [], "Bulut çalışırken yerel hiç çağrılmamalı"


def test_auto_mode_falls_back_to_local_on_cloud_failure() -> None:
    cloud = FakeSTT(error=RuntimeError("ağ yok"))
    local = FakeSTT("yerel")

    assert _stt_router(cloud, local).transcribe(b"ses") == "yerel"


def test_local_mode_never_touches_cloud() -> None:
    """GİZLİLİK: 'local' modda ses kesinlikle dışarı çıkmamalı."""

    cloud, local = FakeSTT("bulut"), FakeSTT("yerel")
    router = _stt_router(cloud, local, mode=ProviderMode.LOCAL)

    assert router.transcribe(b"ses") == "yerel"
    assert cloud.calls == [], "'local' modda bulut sağlayıcı ÇAĞRILMAMALI"


def test_cloud_mode_propagates_error_instead_of_silently_falling_back() -> None:
    """Kullanıcı 'yalnızca bulut' dediyse sessizce yerele düşmek tercihini çiğner."""

    cloud = FakeSTT(error=RuntimeError("500"))
    local = FakeSTT("yerel")
    router = _stt_router(cloud, local, mode=ProviderMode.CLOUD)

    with pytest.raises(RuntimeError):
        router.transcribe(b"ses")
    assert local.calls == []


def test_invalid_mode_is_rejected_early() -> None:
    with pytest.raises(ValueError):
        _stt_router(FakeSTT(), FakeSTT(), mode="yarim-bulut")


# --------------------------------------------------------------------------
# Soğuma (cooldown) — internet kapalıyken her komutta zaman aşımı beklenmemeli
# --------------------------------------------------------------------------


def test_cloud_is_skipped_during_cooldown_after_a_failure() -> None:
    """Bir hatadan sonra bulut bir süre HİÇ denenmemeli.

    Soğuma olmasaydı, internet kapalıyken her komut önce bulutun zaman
    aşımını (10-15 sn) beklerdi ve asistan kullanılamaz hale gelirdi.
    """

    cloud = FakeSTT(error=RuntimeError("ağ yok"))
    local = FakeSTT("yerel")
    router = _stt_router(cloud, local, failure_cooldown=300.0)

    router.transcribe(b"bir")
    router.transcribe(b"iki")
    router.transcribe(b"uc")

    assert len(cloud.calls) == 1, "Soğuma sırasında bulut tekrar denenmemeliydi"
    assert len(local.calls) == 3


def test_cloud_is_retried_after_cooldown_expires() -> None:
    """Soğuma dolunca bulut yeniden denenmeli (internet geri gelmiş olabilir)."""

    cloud = FakeSTT(error=RuntimeError("ağ yok"))
    local = FakeSTT("yerel")
    router = _stt_router(cloud, local, failure_cooldown=0.0)  # anında dolan soğuma

    router.transcribe(b"bir")
    router.transcribe(b"iki")

    assert len(cloud.calls) == 2, "Soğuma dolduğunda bulut yeniden denenmeliydi"


# --------------------------------------------------------------------------
# Tembellik (lazy) — bulut çalışırken yerel modelin RAM maliyeti ödenmemeli
# --------------------------------------------------------------------------


def test_local_provider_is_never_constructed_while_cloud_works() -> None:
    """Bulut çalışıyorsa yerel Whisper HİÇ yüklenmemeli (RAM kazancı)."""

    built: list[str] = []

    def local_factory() -> FakeSTT:
        built.append("yerel")
        return FakeSTT("yerel")

    router = SpeechToTextRouter(lambda: FakeSTT("bulut"), local_factory)
    router.transcribe(b"ses")
    router.transcribe(b"ses")

    assert built == [], "Bulut çalışırken yerel sağlayıcı oluşturulmamalıydı"


def test_hotwords_reach_the_selected_provider() -> None:
    """Özel isim ipucu yolda düşmemeli — hem bulut hem yerel yolda."""

    cloud = FakeSTT(error=RuntimeError("yok"))
    local = FakeSTT("tamam")
    _stt_router(cloud, local).transcribe(b"ses", hotwords="discord, steam")

    assert cloud.calls[0][1] == "discord, steam"
    assert local.calls[0][1] == "discord, steam"


# --------------------------------------------------------------------------
# TTS yönlendirici
# --------------------------------------------------------------------------


def test_tts_falls_back_to_local_and_still_speaks() -> None:
    cloud = FakeTTS(error=RuntimeError("ağ yok"))
    local = FakeTTS()

    TextToSpeechRouter(lambda: cloud, lambda: local).speak("merhaba")

    assert local.spoken == ["merhaba"]


def test_tts_stop_does_not_construct_unused_providers() -> None:
    """Yalnızca durdurmak için Piper modelini belleğe almak anlamsız olurdu."""

    built: list[str] = []

    def local_factory() -> FakeTTS:
        built.append("yerel")
        return FakeTTS()

    router = TextToSpeechRouter(lambda: FakeTTS(), local_factory)
    router.stop()  # hiç konuşulmadı; hiçbir sağlayıcı kurulmamalı

    assert built == []


def test_tts_stop_reaches_already_constructed_providers() -> None:
    cloud, local = FakeTTS(), FakeTTS()
    router = TextToSpeechRouter(lambda: cloud, lambda: local)

    router.speak("merhaba")  # bulut kurulur
    router.stop()

    assert cloud.stops == 1


# --------------------------------------------------------------------------
# Sağlayıcı değişimi logu — TEŞHİS: log'a bakınca bulut mu yerel mi
# kullanıldığı anlaşılabilmeli, ama her çağrıda değil (log şişmesin).
# --------------------------------------------------------------------------


def _provider_log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Yalnızca sağlayıcı-kullanımı log satırlarının metnini döndürür."""

    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "Ses sağlayıcı" in record.getMessage()
    ]


def test_first_successful_cloud_call_logs_the_provider(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    cloud, local = FakeSTT("bulut"), FakeSTT("yerel")

    _stt_router(cloud, local).transcribe(b"ses")

    messages = _provider_log_messages(caplog)
    assert len(messages) == 1
    assert "bulut" in messages[0]


def test_same_provider_used_repeatedly_is_logged_only_once(caplog: pytest.LogCaptureFixture) -> None:
    """AYNI sağlayıcı arka arkaya kullanılırsa log TEKRARLANMAMALI — yoksa
    bu satır her komutta tekrarlanıp log dosyasını şişirir."""

    caplog.set_level(logging.INFO)
    cloud, local = FakeSTT("bulut"), FakeSTT("yerel")
    router = _stt_router(cloud, local)

    router.transcribe(b"bir")
    router.transcribe(b"iki")
    router.transcribe(b"uc")

    assert len(_provider_log_messages(caplog)) == 1


def test_fallback_from_cloud_to_local_logs_the_new_provider(caplog: pytest.LogCaptureFixture) -> None:
    """Bulut→yerel düşüşünde yerel sağlayıcının kullanıldığı loglanmalı."""

    caplog.set_level(logging.INFO)
    cloud = FakeSTT(error=RuntimeError("ağ yok"))
    local = FakeSTT("yerel")

    _stt_router(cloud, local).transcribe(b"ses")

    messages = _provider_log_messages(caplog)
    assert len(messages) == 1
    assert "yerel" in messages[0]


def test_provider_switch_from_cloud_to_local_is_logged_again(caplog: pytest.LogCaptureFixture) -> None:
    """Önce bulut başarılı olup loglandıktan sonra bulut bozulup yerele
    düşülürse — sağlayıcı GERÇEKTEN değiştiği için ikinci bir log satırı
    yazılmalı (birinci testteki 'tekrarlanmamalı' kuralıyla çelişmez:
    burada sağlayıcı AYNI değil, FARKLI)."""

    caplog.set_level(logging.INFO)
    cloud = FakeSTT("bulut")
    local = FakeSTT("yerel")
    router = _stt_router(cloud, local)

    router.transcribe(b"bir")  # bulut çalışıyor -> loglanır
    cloud.error = RuntimeError("aniden koptu")
    router.transcribe(b"iki")  # bulut bozuldu -> yerele düşer -> tekrar loglanır

    messages = _provider_log_messages(caplog)
    assert len(messages) == 2
    assert "bulut" in messages[0]
    assert "yerel" in messages[1]


def test_stt_and_tts_routers_use_distinguishable_labels(caplog: pytest.LogCaptureFixture) -> None:
    """Yönlendirici hem STT hem TTS için kullanılır; log satırı hangisi
    olduğunu ayırt edebilmeli (`label` parametresi bunun için var)."""

    caplog.set_level(logging.INFO)
    stt_router = SpeechToTextRouter(lambda: FakeSTT("bulut"), lambda: FakeSTT("yerel"))
    tts_router = TextToSpeechRouter(lambda: FakeTTS(), lambda: FakeTTS())

    stt_router.transcribe(b"ses")
    tts_router.speak("merhaba")

    messages = _provider_log_messages(caplog)
    assert len(messages) == 2
    assert any("STT" in m for m in messages)
    assert any("TTS" in m for m in messages)
