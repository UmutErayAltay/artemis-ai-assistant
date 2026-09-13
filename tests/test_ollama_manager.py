"""`core.ollama_manager` testleri.

Gerçek bir Ollama sunucusu veya kurulu model gerektirmez; `ollama` modülü
ve `subprocess.Popen`/`time.sleep` sahte (mock) sürümlerle değiştirilir.
"""

from __future__ import annotations

import sys
import types

import pytest

from core.ollama_manager import (
    _PING_TIMEOUT_SECONDS,
    OllamaServerManager,
    OllamaUnavailableError,
    list_installed_models,
    prompt_user_to_select_model,
)


def _install_fake_ollama(monkeypatch: pytest.MonkeyPatch, list_return, *, raise_on_list: bool = False) -> None:
    fake_ollama = types.ModuleType("ollama")

    def _fake_list():
        if raise_on_list:
            raise ConnectionError("sunucu çalışmıyor (sandbox)")
        return list_return

    fake_ollama.list = _fake_list

    # GERÇEK kütüphanedeki `Client` sınıfı da taklit edilir:
    # `_is_server_responding` artık modül seviyesindeki `ollama.list()`'i
    # değil `ollama.Client(timeout=...).list()`'i çağırıyor, çünkü
    # varsayılan istemcinin OKUMA ZAMAN AŞIMI YOKTUR ve bu yoklama
    # `ensure_running`'in poll döngüsünün İÇİNDE çalışıyor.
    class _FakeClient:
        def __init__(self, timeout=None, **kwargs):
            self.timeout = timeout

        def list(self):
            return fake_ollama.list()

    fake_ollama.Client = _FakeClient
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_ollama)


def test_list_installed_models_handles_dict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ollama(monkeypatch, {"models": [{"model": "llama3.1:latest"}, {"model": "qwen2.5:7b"}]})
    names = list_installed_models()
    assert names == ["llama3.1:latest", "qwen2.5:7b"]


def test_list_installed_models_handles_object_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeModel:
        def __init__(self, model_name):
            self.model = model_name

    class _FakeListResponse:
        def __init__(self, models):
            self.models = models

    _install_fake_ollama(monkeypatch, _FakeListResponse([_FakeModel("llama3.1:latest"), _FakeModel("mistral:7b")]))
    names = list_installed_models()
    assert names == ["llama3.1:latest", "mistral:7b"]


def test_list_installed_models_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ollama(monkeypatch, {"models": []})
    assert list_installed_models() == []


def test_list_installed_models_uses_a_timeout_bound_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresyon testi: eskiden modül seviyesindeki `ollama.list()`
    (OKUMA ZAMAN AŞIMI OLMAYAN istemci) çağrılıyordu — `_is_server_
    responding`'in aynı sebeple KAÇINDIĞI çağrı. Bu fonksiyon `main.py`'de
    AÇILIŞTA çağrıldığı için kilitlenmiş bir sunucu, UI hiç açılmadan
    Artemis'i SÜRESİZ askıda bırakabiliyordu."""

    seen_timeouts: list[float | None] = []

    fake_ollama = types.ModuleType("ollama")

    class _FakeClient:
        def __init__(self, timeout=None, **kwargs):
            seen_timeouts.append(timeout)

        def list(self):
            return {"models": [{"model": "llama3.1:latest"}]}

    fake_ollama.Client = _FakeClient

    def _module_level_list_should_never_be_called():
        raise AssertionError("modül seviyesi ollama.list() ÇAĞRILDI — zaman aşımsız istemci kullanılıyor")

    fake_ollama.list = _module_level_list_should_never_be_called
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)

    names = list_installed_models()

    assert names == ["llama3.1:latest"]
    assert seen_timeouts == [pytest.approx(_PING_TIMEOUT_SECONDS)]


def test_prompt_user_to_select_model_raises_when_no_models() -> None:
    with pytest.raises(OllamaUnavailableError):
        prompt_user_to_select_model([])


def test_prompt_user_to_select_model_valid_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    selected = prompt_user_to_select_model(["llama3.1:latest", "qwen2.5:7b", "mistral:7b"])
    assert selected == "qwen2.5:7b"


def test_prompt_user_to_select_model_retries_on_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["abc", "99", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    selected = prompt_user_to_select_model(["llama3.1:latest", "qwen2.5:7b"])
    assert selected == "llama3.1:latest"


def test_prompt_user_to_select_model_falls_back_when_stdin_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresyon testi: `input()` bir dönem `EOFError`/`KeyboardInterrupt`
    yakalamıyordu — boru hattına bağlı stdin'de (servis/otomasyon/
    zamanlanmış başlatma) `main.py` ham bir traceback ile ölüyordu.
    `config/config.yaml::ollama_model` bunun için "yedek" diye
    DOKÜMANTE EDİLMİŞTİ ama hiç UYGULANMAMIŞTI."""

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    selected = prompt_user_to_select_model(
        ["llama3.1:latest", "qwen2.5:7b"], fallback_model="qwen2.5:7b"
    )

    assert selected == "qwen2.5:7b"


def test_prompt_user_to_select_model_raises_honestly_when_no_fallback_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdin yok VE yedek de kurulu modeller arasında değilse (ya da hiç
    verilmediyse), ham bir `EOFError` yerine açıklayıcı bir hata
    fırlatılmalı."""

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    with pytest.raises(OllamaUnavailableError):
        prompt_user_to_select_model(["llama3.1:latest"], fallback_model="kurulu-olmayan-model")


def test_ensure_running_does_nothing_if_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ollama(monkeypatch, {"models": []})  # ollama.list() hata vermiyor -> "calisiyor" sayilir

    popen_calls = []
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **kw: popen_calls.append((a, kw)) or None
    )

    manager = OllamaServerManager()
    manager.ensure_running()
    assert popen_calls == []  # sunucu zaten calisiyordu, yeni surec baslatilmadi


def test_ensure_running_raises_if_ollama_command_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ollama(monkeypatch, None, raise_on_list=True)  # sunucu yanit vermiyor
    monkeypatch.setattr("shutil.which", lambda cmd: None)  # 'ollama' komutu PATH'te yok

    manager = OllamaServerManager()
    with pytest.raises(OllamaUnavailableError):
        manager.ensure_running()


def test_ensure_running_starts_server_and_waits_until_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ilk 2 kontrol basarisiz (sunucu henuz ayakta degil), 3.'de basarili.
    responses = iter([False, False, True])

    def _fake_is_responding():
        return next(responses, True)

    monkeypatch.setattr(OllamaServerManager, "_is_server_responding", staticmethod(_fake_is_responding))
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ollama")
    monkeypatch.setattr("time.sleep", lambda seconds: None)  # testte gercekten beklemeyelim

    class _FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _FakeProcess())

    manager = OllamaServerManager()
    manager.ensure_running()  # exception firlatmamali
    assert manager._process is not None


def test_stop_if_we_started_it_does_nothing_when_we_did_not_start_server() -> None:
    manager = OllamaServerManager()  # ensure_running hic cagrilmadi -> _process None
    manager.stop_if_we_started_it()  # exception firlatmamali, hicbir sey yapmamali


class _FakeServerProcess:
    """`stop_if_we_started_it`'in kapatma sırasını sınamak için sahte alt süreç.

    `wait()` çağrılarını sayar: `terminate()`'ın hemen ölmesini
    simüle eden ilk sürüm, `kill()`'in ARDINDAN bir ikinci `wait()`
    ile REAP edildiğini doğrulamak için kullanılır (eskiden bu ikinci
    `wait()` hiç yoktu — süreç zombi kalabiliyordu).
    """

    def __init__(self, dies_on_terminate: bool) -> None:
        self._dies_on_terminate = dies_on_terminate
        self._killed = False
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = 0
        self.pid = 4242

    def poll(self):
        return None if not (self._dies_on_terminate or self._killed) else 0

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True
        self._killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        if not (self._dies_on_terminate or self._killed):
            import subprocess

            raise subprocess.TimeoutExpired(cmd="ollama serve", timeout=timeout)


def test_stop_if_we_started_it_clears_process_after_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresyon testi: `self._process` eskiden hiç `None` yapılmıyordu —
    ikinci bir çağrı bayat bir handle ile yeniden giriyordu."""

    manager = OllamaServerManager()
    manager._process = _FakeServerProcess(dies_on_terminate=True)

    manager.stop_if_we_started_it()

    assert manager._process is None
    manager.stop_if_we_started_it()  # ikinci çağrı GÜVENLE no-op olmalı


def test_stop_if_we_started_it_reaps_the_process_after_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresyon testi: `kill()`'den SONRA ikinci bir `wait()` yoktu —
    `terminate()` süresi dolup `kill()`e başvurulduğunda süreç hiç
    REAP edilmiyor, zombi kalabiliyordu."""

    process = _FakeServerProcess(dies_on_terminate=False)
    manager = OllamaServerManager()
    manager._process = process

    manager.stop_if_we_started_it()

    assert process.terminate_called is True
    assert process.kill_called is True
    assert process.wait_calls == 2  # terminate sonrası + kill sonrası
    assert manager._process is None


def _make_fake_psutil(monkeypatch: pytest.MonkeyPatch, procs):
    """GERÇEK psutil semantiğini taklit eden sahte modül.

    Kritik nokta (bkz. `.context` §6.16'nın dersi): `terminate()` bir
    sinyal GÖNDERİR, öldürmeyi GARANTİ ETMEZ. Gerçekten kimin öldüğünü
    yalnızca `wait_procs` söyler. Fake bu ayrımı yapmazsa,
    `stop_all_ollama_processes`'in dürüst sayım mantığını sınayamaz —
    yalnızca "terminate çağrıldı mı" diye sorardı ki asıl düzeltilen
    hata tam olarak buydu.
    """

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.process_iter = lambda attrs=None: procs

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    fake_psutil.NoSuchProcess = _NoSuchProcess
    fake_psutil.AccessDenied = _AccessDenied

    def _wait_procs(watched, timeout=None):
        gone = [p for p in watched if not p.alive]
        alive = [p for p in watched if p.alive]
        return gone, alive

    fake_psutil.wait_procs = _wait_procs
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    return fake_psutil


class _FakeProc:
    """Sahte süreç. `ignores_terminate=True` ise `terminate()` sinyalini
    yok sayar ve yalnızca `kill()` ile ölür — gerçek dünyada olan şey."""

    def __init__(self, name: str, pid: int = 1, ignores_terminate: bool = False) -> None:
        self.info = {"name": name, "pid": pid}
        self.pid = pid
        self.alive = True
        self.terminated = False
        self.killed = False
        self._ignores_terminate = ignores_terminate

    def terminate(self) -> None:
        self.terminated = True
        if not self._ignores_terminate:
            self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False


def test_stop_all_ollama_processes_terminates_matching_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ollama_manager import stop_all_ollama_processes

    procs = [_FakeProc("ollama.exe", 1), _FakeProc("chrome.exe", 2), _FakeProc("ollama", 3)]
    _make_fake_psutil(monkeypatch, procs)

    count = stop_all_ollama_processes()

    assert count == 2
    assert procs[0].terminated and procs[2].terminated
    assert not procs[1].terminated, "İlgisiz süreçlere DOKUNULMAMALI"


def test_stop_all_counts_only_processes_that_actually_died(monkeypatch: pytest.MonkeyPatch) -> None:
    """DÜRÜSTLÜK: `terminate()` çağrısı sayılmaz, GERÇEKTEN ölen sayılır.

    Bu fonksiyon bir dönem `terminate()` çağrılarını sayıyordu. Sinyali
    yok sayan bir süreç de "kapatıldı" olarak raporlanıyordu; kullanıcı
    RAM'in boşaldığını sanıyordu.
    """

    from core.ollama_manager import stop_all_ollama_processes

    inatci = _FakeProc("ollama.exe", 1, ignores_terminate=True)
    _make_fake_psutil(monkeypatch, [inatci])

    count = stop_all_ollama_processes()

    assert inatci.terminated, "önce nazikçe istenmeli"
    assert inatci.killed, "yanıt vermeyene ısrar edilmeli"
    assert count == 1, "kill sonrası gerçekten öldü, sayılmalı"


def test_stop_all_does_not_report_processes_it_could_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """`kill()` de işe yaramazsa o süreç SAYILMAMALI."""

    from core.ollama_manager import stop_all_ollama_processes

    olumsuz = _FakeProc("ollama.exe", 1, ignores_terminate=True)
    olumsuz.kill = lambda: None  # kill de etkisiz: süreç `alive` kalır

    _make_fake_psutil(monkeypatch, [olumsuz])

    assert stop_all_ollama_processes() == 0


def test_stop_all_ignores_unrelated_processes_with_ollama_in_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ollama-webui` / `ollama_bench.py` bir Ollama sunucusu DEĞİLDİR.

    Kontrol bir dönem `if "ollama" in name` idi; bir "RAM temizleme"
    komutunun kapsamı, adında bir alt dize geçen her şey olamaz.
    """

    from core.ollama_manager import stop_all_ollama_processes

    procs = [_FakeProc("ollama-webui", 1), _FakeProc("ollama_bench.py", 2), _FakeProc("ollama", 3)]
    _make_fake_psutil(monkeypatch, procs)

    count = stop_all_ollama_processes()

    assert count == 1
    assert not procs[0].terminated
    assert not procs[1].terminated
    assert procs[2].terminated
