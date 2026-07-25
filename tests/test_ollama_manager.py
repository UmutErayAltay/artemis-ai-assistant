"""`core.ollama_manager` testleri.

Gerçek bir Ollama sunucusu veya kurulu model gerektirmez; `ollama` modülü
ve `subprocess.Popen`/`time.sleep` sahte (mock) sürümlerle değiştirilir.
"""

from __future__ import annotations

import types

import pytest

from core.ollama_manager import (
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


def test_stop_all_ollama_processes_terminates_matching_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ollama_manager import stop_all_ollama_processes

    class _FakeProc:
        def __init__(self, name):
            self.info = {"name": name}
            self.terminated = False

        def terminate(self):
            self.terminated = True

    procs = [_FakeProc("ollama.exe"), _FakeProc("chrome.exe"), _FakeProc("ollama")]

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.process_iter = lambda attrs=None: procs

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    fake_psutil.NoSuchProcess = _NoSuchProcess
    fake_psutil.AccessDenied = _AccessDenied
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    count = stop_all_ollama_processes()
    assert count == 2  # yalnızca adında "ollama" gecen 2 surec
    assert procs[0].terminated is True
    assert procs[1].terminated is False
    assert procs[2].terminated is True
