"""`voice.gpu` için testler: `prepare_cuda_libraries` ve `resolve_device`.

Hiçbir test gerçek CUDA/GPU'ya ihtiyaç duymaz:
    - `resolve_device`, `prepare_cuda_libraries`'i çağırıp sonucuna göre
      dallanan saf bir fonksiyondur; sayaçlı sahte bir sürümle
      değiştirilerek doğrulanabilir.
    - `prepare_cuda_libraries`'in Windows dışı davranışı, `sys.platform`
      monkeypatch'lenerek gerçek `ctypes.CDLL`'e hiç dokunmadan test edilir.

DİKKAT: `voice/gpu.py` modül seviyesinde `_prepared` adında bir önbellek
bayrağı tutar (`prepare_cuda_libraries` bir kez başarılı olduysa bir daha
hiçbir şey yapmadan `True` döner). Bu bayrak testler arasında sızarsa bir
testin sonucu diğerini bozar; bu yüzden her testte açıkça `False`'a
sıfırlanır.
"""

from __future__ import annotations

import logging
import sys

import pytest

import voice.gpu as gpu
from voice.gpu import resolve_device


@pytest.fixture(autouse=True)
def _reset_prepared_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Her testten önce modül seviyesindeki `_prepared` önbelleğini sıfırlar."""

    monkeypatch.setattr(gpu, "_prepared", False)


def _install_fake_prepare_cuda_libraries(
    monkeypatch: pytest.MonkeyPatch, *, returns: bool
) -> list[None]:
    """`gpu.prepare_cuda_libraries`'i sayaçlı sahte bir sürümle değiştirir."""

    calls: list[None] = []

    def _fake_prepare() -> bool:
        calls.append(None)
        return returns

    monkeypatch.setattr(gpu, "prepare_cuda_libraries", _fake_prepare)
    return calls


def test_resolve_device_cpu_never_touches_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_prepare_cuda_libraries(monkeypatch, returns=True)

    device = resolve_device("cpu")

    assert device == "cpu"
    assert calls == []  # CUDA hazırlığına hiç dokunulmadı


def test_resolve_device_cuda_returns_cuda_when_libraries_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_prepare_cuda_libraries(monkeypatch, returns=True)

    device = resolve_device("cuda")

    assert device == "cuda"


def test_resolve_device_cuda_falls_back_to_cpu_and_logs_warning_when_libraries_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_fake_prepare_cuda_libraries(monkeypatch, returns=False)
    caplog.set_level(logging.WARNING)

    device = resolve_device("cuda")

    assert device == "cpu"  # sessizce çökmek yerine düşer
    assert any(
        record.levelno == logging.WARNING and "cuda" in record.message.lower()
        for record in caplog.records
    )


def test_prepare_cuda_libraries_does_not_load_dlls_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows dışında DLL yükleme denenmez — ama sonuç yine ÖLÇÜLÜR.

    Bu test bir dönem "Windows dışında her zaman True döner" diyordu;
    yani KODUN HATASINI sözleşme sanıp sabitlemişti. Doğru sözleşme:
    Windows'a özgü DLL kurulumu (`add_dll_directory` + `CDLL`) atlanır,
    ama CUDA'nın gerçekten var olup olmadığı yine de kontrol edilir.
    """

    # Gerçek işletim sistemi ne olursa olsun `sys.platform` sahtelenerek
    # Windows-dışı dal her ortamda doğrulanabiliyor.
    monkeypatch.setattr(sys, "platform", "linux")

    calls = []
    monkeypatch.setattr("ctypes.CDLL", lambda *args, **kwargs: calls.append(args) or object())
    monkeypatch.setattr("ctypes.util.find_library", lambda name: f"lib{name}.so")

    result = gpu.prepare_cuda_libraries()

    assert result is True  # kütüphaneler bulundu
    assert calls == []  # ama Windows'a özgü DLL yükleme hiç denenmedi


# --- Windows dışında CPU'ya geri düşüş (README §35) ----------------------


def test_prepare_returns_false_on_linux_without_cuda_libraries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CUDA kütüphanesi yoksa Windows DIŞINDA da `False` dönmeli.

    Burada bir dönem koşulsuz `True` vardı: "Linux'ta pip paketleri zaten
    linker yolundadır" gerekçesiyle. Ama "yol sorunu yok" ile "CUDA VAR"
    aynı şey değil. `Settings.whisper_device` varsayılanı `"cuda"` olduğu
    için sonuç, GPU'su olmayan her Linux makinesinde ses katmanının
    bu modülün VAAT ETTİĞİ CPU'ya düşüş yerine ilk tanımada
    `SpeechRecognitionUnavailableError` ile ölmesiydi — yani geri düşüş
    yalnızca Windows'ta çalışıyordu.
    """

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)

    assert gpu.prepare_cuda_libraries() is False


def test_prepare_returns_true_on_linux_when_cuda_libraries_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ctypes.util.find_library", lambda name: f"lib{name}.so")

    assert gpu.prepare_cuda_libraries() is True


def test_resolve_device_falls_back_to_cpu_on_linux_without_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uçtan uca: sahte `prepare` DEĞİL, gerçek zincir üzerinden.

    `resolve_device("cuda")` -> `prepare_cuda_libraries()` -> CUDA yok ->
    "cpu". Zincirin herhangi bir halkası koparsa bu test kırılır.
    """

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)

    assert resolve_device("cuda") == "cpu"
