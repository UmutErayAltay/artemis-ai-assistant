"""`core/planner.py` ilerleme geri çağrısı: isteğe bağlı ve adım durumlarını doğru bildirir.

Sahte bir dispatcher kullanılır; asıl davranış (sıra, durma, onay) planner'ın kendisinde.
Mevcut planner testleri bu geri çağrıyı hiç kullanmaz (varsayılan None) ve değişmeden geçer.
"""

from __future__ import annotations

from typing import Any

from core.planner import PlanProgress, TaskPlanner
from models.tool_models import ToolResult


class FakeDispatcher:
    """Tool'ları çalıştırmaz; her ad için önceden verilen sonucu döndürür."""

    def __init__(self, outcomes: dict[str, ToolResult] | None = None, confirm: set[str] | None = None) -> None:
        self._outcomes = outcomes or {}
        self._confirm = confirm or set()
        self.executed: list[str] = []

    def dispatch(self, call: dict[str, Any], confirmed: bool = False) -> ToolResult:
        name = call["tool"]
        if name in self._confirm and not confirmed:
            return ToolResult(success=False, message="onay gerekli", requires_confirmation=True)
        self.executed.append(name)
        return self._outcomes.get(name, ToolResult(success=True, message="tamam"))


def _run(plan: list[dict[str, Any]], dispatcher: FakeDispatcher, confirm: bool = True, **kwargs: Any):
    events: list[PlanProgress] = []
    planner = TaskPlanner(
        dispatcher,
        confirm_callback=lambda _name, _args: confirm,
        progress_callback=events.append,
        **kwargs,
    )
    planner.execute_plan(plan)
    return events


def _states(events: list[PlanProgress]) -> list[tuple[int, str]]:
    return [(event.index, event.state) for event in events]


def test_two_successful_steps_report_running_then_done_for_each() -> None:
    plan = [{"tool": "a.one", "arguments": {}}, {"tool": "b.two", "arguments": {}}]

    events = _run(plan, FakeDispatcher())

    assert _states(events) == [(1, "running"), (1, "done"), (2, "running"), (2, "done")]
    assert all(event.tool_names == ("a.one", "b.two") for event in events)


def test_failed_step_is_reported_and_later_steps_are_never_reported() -> None:
    plan = [{"tool": "a.one", "arguments": {}}, {"tool": "b.two", "arguments": {}}]
    dispatcher = FakeDispatcher({"a.one": ToolResult(success=False, message="hata")})

    events = _run(plan, dispatcher)

    assert _states(events) == [(1, "running"), (1, "failed")]
    assert dispatcher.executed == ["a.one"]


def test_rejected_confirmation_is_reported_as_failed_and_stops_the_plan() -> None:
    plan = [{"tool": "a.delete", "arguments": {}}, {"tool": "b.two", "arguments": {}}]
    dispatcher = FakeDispatcher(confirm={"a.delete"})

    events = _run(plan, dispatcher, confirm=False)

    assert _states(events) == [(1, "running"), (1, "failed")]
    assert dispatcher.executed == []


def test_accepted_confirmation_runs_the_step_and_reports_done() -> None:
    plan = [{"tool": "a.delete", "arguments": {}}]
    dispatcher = FakeDispatcher(confirm={"a.delete"})

    events = _run(plan, dispatcher, confirm=True)

    assert _states(events) == [(1, "running"), (1, "done")]
    assert dispatcher.executed == ["a.delete"]


def test_unresolvable_reference_still_reports_running_before_failed() -> None:
    """Referans hatası da bir adımdır: her adımın olay dizisi "running" ile başlar.

    Arayüz adım listesini ilk "running" olayında kurduğu için bu sıra şarttır.
    """

    plan = [
        {"tool": "a.one", "arguments": {}},
        {"tool": "b.two", "arguments": {"target": "{{step_1.path}}"}},
    ]
    dispatcher = FakeDispatcher({"a.one": ToolResult(success=False, message="hata")})

    events = _run(plan, dispatcher, stop_on_failure=False)

    assert _states(events) == [(1, "running"), (1, "failed"), (2, "running"), (2, "failed")]
    assert dispatcher.executed == ["a.one"], "referansı çözülemeyen adım hiç çalıştırılmamalı"


def test_planner_without_callback_behaves_as_before() -> None:
    planner = TaskPlanner(FakeDispatcher(), confirm_callback=lambda _n, _a: True)

    results = planner.execute_plan([{"tool": "a.one", "arguments": {}}])

    assert len(results) == 1 and results[0].result.success
    assert planner.progress_callback is None
