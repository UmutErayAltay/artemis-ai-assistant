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


def test_prepare_cuda_libraries_returns_true_without_ctypes_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gerçek işletim sistemi ne olursa olsun `sys.platform` sahtelenerek
    # Windows-dışı dal her ortamda doğrulanabiliyor.
    monkeypatch.setattr(sys, "platform", "linux")

    calls = []
    monkeypatch.setattr("ctypes.CDLL", lambda *args, **kwargs: calls.append(args) or object())

    result = gpu.prepare_cuda_libraries()

    assert result is True
    assert calls == []  # Windows dışında DLL yükleme hiç denenmedi
