"""`core.planner.TaskPlanner` testleri.

Gerçek `ToolDispatcher` + `filesystem_plugin` tool'ları kullanılır (tmp_path
ile izole edilmiş sahte bir masaüstü); yalnızca `confirm_callback`
(terminal I/O) sahte (mock) bir fonksiyonla değiştirilir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.planner import TaskPlanner
from core.plugin_loader import load_plugins
from memory.context_memory import ContextMemory


@pytest.fixture(autouse=True)
def _load_all_plugins() -> None:
    load_plugins()


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(desktop_path=tmp_path, db_path=tmp_path / "memory.db", log_dir=tmp_path / "logs")
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


def _always_confirm(tool_name: str, arguments: dict) -> bool:
    return True


def _always_deny(tool_name: str, arguments: dict) -> bool:
    return False


def test_multi_step_plan_all_succeed(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_confirm)

    plan = [
        {"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}},
        {"tool": "filesystem.create_file", "arguments": {"name": "app.py", "location": str(tmp_path / "Orbit")}},
    ]
    results = planner.execute_plan(plan)

    assert len(results) == 2
    assert all(step.result.success for step in results)
    assert (tmp_path / "Orbit" / "app.py").exists()


def test_plan_stops_after_real_failure(dispatcher: ToolDispatcher) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_confirm)

    plan = [
        {"tool": "filesystem.open", "arguments": {"target": "olmayan-klasor"}},  # başarısız olacak
        {"tool": "filesystem.create_folder", "arguments": {"name": "BuAslaOlusturulmamali"}},
    ]
    results = planner.execute_plan(plan)

    assert len(results) == 1  # ikinci adıma hiç sıra gelmedi
    assert results[0].result.success is False


def test_plan_continues_past_failure_when_stop_on_failure_false(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_confirm, stop_on_failure=False)

    plan = [
        {"tool": "filesystem.open", "arguments": {"target": "olmayan-klasor"}},  # başarısız
        {"tool": "filesystem.create_folder", "arguments": {"name": "YineDeOlustu"}},  # yine de çalışmalı
    ]
    results = planner.execute_plan(plan)

    assert len(results) == 2
    assert results[0].result.success is False
    assert results[1].result.success is True
    assert (tmp_path / "YineDeOlustu").is_dir()


def test_plan_stops_entirely_when_user_denies_confirmation(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_deny)

    dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "x.txt"}})

    plan = [
        {"tool": "filesystem.delete", "arguments": {"target": "x.txt"}},  # onay istenecek, reddedilecek
        {"tool": "filesystem.create_folder", "arguments": {"name": "BuDaOlusturulmamali"}},
    ]
    results = planner.execute_plan(plan)

    assert len(results) == 1  # ikinci adım hiç çalıştırılmadı
    assert results[0].result.success is False
    assert results[0].result.requires_confirmation is True
    assert (tmp_path / "x.txt").exists()  # silme reddedildiği için dosya hâlâ duruyor
    assert not (tmp_path / "BuDaOlusturulmamali").exists()


def test_plan_continues_when_user_confirms(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_confirm)

    dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "x.txt"}})

    plan = [
        {"tool": "filesystem.delete", "arguments": {"target": "x.txt"}},
        {"tool": "filesystem.create_folder", "arguments": {"name": "SonrakiAdimCalisti"}},
    ]
    results = planner.execute_plan(plan)

    assert len(results) == 2
    assert results[0].result.success is True  # onaylandığı için silme başarılı
    assert results[1].result.success is True
    assert not (tmp_path / "x.txt").exists()
    assert (tmp_path / "SonrakiAdimCalisti").is_dir()


def test_confirm_callback_receives_correct_arguments(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    """`confirm_callback`'e DOĞRU argümanların geçirildiğini doğrular (regresyon testi).

    Güvenlik düzeltmesi: kullanıcı artık yalnızca tool adını değil, o tool'un
    hangi argümanlarla (örn. silinecek `target`) çalışacağını da görebilmeli.
    Bu test, `confirm_callback`'in imzasının `(tool_name, arguments)` olarak
    kalmasını VE gerçek adım argümanlarının (mock ya da boş sözlük değil)
    iletilmesini garanti eder — bu koruma olmadan biri callback'i tekrar tek
    parametreli hale getirip argümanları sessizce düşürebilir.
    """

    received: list[tuple[str, dict]] = []

    def _recording_confirm(tool_name: str, arguments: dict) -> bool:
        received.append((tool_name, arguments))
        return True

    planner = TaskPlanner(dispatcher, confirm_callback=_recording_confirm)

    dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "hedef.txt"}})

    plan = [
        {"tool": "filesystem.delete", "arguments": {"target": "hedef.txt"}},
    ]
    planner.execute_plan(plan)

    assert len(received) == 1
    confirmed_tool_name, confirmed_arguments = received[0]
    assert confirmed_tool_name == "filesystem.delete"
    assert confirmed_arguments == {"target": "hedef.txt"}


def test_empty_plan_returns_empty_results(dispatcher: ToolDispatcher) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_confirm)
    assert planner.execute_plan([]) == []


def test_step_result_indices_are_sequential(dispatcher: ToolDispatcher) -> None:
    planner = TaskPlanner(dispatcher, confirm_callback=_always_confirm)

    plan = [
        {"tool": "filesystem.create_folder", "arguments": {"name": "A"}},
        {"tool": "filesystem.create_folder", "arguments": {"name": "B"}},
        {"tool": "filesystem.create_folder", "arguments": {"name": "C"}},
    ]
    results = planner.execute_plan(plan)

    assert [step.index for step in results] == [1, 2, 3]
    assert [step.tool_name for step in results] == ["filesystem.create_folder"] * 3
