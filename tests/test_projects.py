"""Proje atölyesi testleri: model, depo, komut kurucu, runner ve iş operasyonları.

Kodlayıcı SAHTEDİR ama süreçler GERÇEKTİR: `claude` yerine `tmp_path`'e
yazılan bir betik konur; Artemis gerçek runner'ı ayrı süreç olarak
başlatır, runner da betiği gerçek `stream-json` biçiminde konuşturur.
Biçim, gerçek `claude -p --output-format stream-json` çıktısından alındı
(ARCHITECTURE.md §42): `system/init`, `assistant` (tool_use/text),
`result` (`is_error`, `result`, `total_cost_usd`, `session_id`).

Betik shebang ile çalıştığı için bu testler POSIX'te koşar.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from config.settings import ProjelerSettings, Settings
from core.conversation_loop import run as run_chat
from core.dispatcher import ToolDispatcher
from models.project_models import Coder, JobStatus, ProjectSpec, slugify
from projects import coder as coder_mod
from projects import jobs
from projects.interview import first_question
from projects.runner import StreamTracker
from projects.session import pending_notices, route_to_interview
from projects.store import INTERVIEW_READY, INTERVIEW_STARTED, ProjectStore

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="sahte kodlayıcı shebang ile çalışır")

_FAKE_CODER = r"""#!{python}
import json, os, pathlib, subprocess, sys, time

args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["SAHTE_KAYIT"], "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": args, "prompt": prompt, "cwd": os.getcwd()}}, ensure_ascii=False) + "\n")

mode = os.environ.get("SAHTE_MOD", "basari")
resume = "--resume" in args
sid = args[args.index("--resume" if resume else "--session-id") + 1]

def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)

emit({{"type": "system", "subtype": "hook_started", "hook_id": "x"}})
emit({{"type": "system", "subtype": "init", "session_id": sid, "model": "sahte-model"}})
if mode == "cokme":
    sys.exit(3)
if mode == "uyku" and not resume:
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "uzun bir iş yapıyorum"}}]}}}})
    time.sleep(120)

target = os.path.join(os.getcwd(), "app.py")
emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Write", "input": {{"file_path": target}}}}]}}}})
pathlib.Path(target).write_text("print('merhaba')\n", encoding="utf-8")
emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Bash", "input": {{"command": "pytest -q"}}}}]}}}})

if mode == "hata":
    emit({{"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "Bütçe aşıldı", "total_cost_usd": 5.0, "session_id": sid}})
    sys.exit(1)

if mode == "soru" and not resume:
    text, cost = "Veri katmanı hazır.\nSORU: Postgres mi SQLite mı?", 0.42
else:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], check=False)
    subprocess.run(["git", "add", "."], check=False)
    subprocess.run(git + ["commit", "-q", "-m", "M1"], check=False)
    text, cost = ("Cevabı aldım; M1 bitti, 3 test geçiyor." if resume else "M1 bitti, 3 test geçiyor."), (0.55 if resume else 0.42)
emit({{"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": cost, "session_id": sid}})
"""

_SPEC = {
    "ad": "Not Defteri",
    "slug": "not-defteri",
    "amac": "Hızlı not almak.",
    "kullanici": "Umut",
    "ozellikler": ["not ekle", "notları listele"],
    "teknoloji": "Python + SQLite",
    "m1_bitti_olcutu": "pytest yeşil",
    "kodlayici": "claude",
    "notlar": "",
}


@pytest.fixture
def projeler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjelerSettings:
    script = tmp_path / "bin" / "sahte-claude"
    script.parent.mkdir()
    script.write_text(_FAKE_CODER.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("SAHTE_KAYIT", str(tmp_path / "kayit.jsonl"))
    monkeypatch.setenv("SAHTE_MOD", "basari")
    return ProjelerSettings(
        root=tmp_path / "Projeler",
        state_dir=tmp_path / "durum",
        claude_command=str(script),
        cor_command=str(script),
    )


@pytest.fixture
def store(projeler: ProjelerSettings) -> ProjectStore:
    return jobs.open_store(projeler)


def _ready_spec(store: ProjectStore, **overrides: Any) -> ProjectSpec:
    interview = store.create_interview("not uygulaması", first_question())
    interview.status, interview.spec = INTERVIEW_READY, _SPEC | overrides
    store.save_interview(interview)
    return ProjectSpec(**interview.spec)


def _wait(store: ProjectStore, job_id: int, *, until: set[JobStatus] | None = None, timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if until is None and job.status is not JobStatus.CALISIYOR:
            return job
        if until is not None and job.status in until:
            return job
        time.sleep(0.1)
    raise AssertionError(f"iş {timeout} sn içinde beklenen duruma gelmedi: {store.get_job(job_id)}")


def _calls(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "kayit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# --- model -----------------------------------------------------------------


def test_slugify_handles_turkish_and_symbols() -> None:
    assert slugify("Fiyat Takip Eklentisi!") == "fiyat-takip-eklentisi"
    assert slugify("Çiçek Şöleni Ğ İ ı") == "cicek-soleni-g-i-i"


def test_spec_derives_slug_and_rejects_unnameable_projects() -> None:
    assert ProjectSpec(**(_SPEC | {"slug": "Not Defteri v2"})).slug == "not-defteri-v2"
    with pytest.raises(ValueError):
        ProjectSpec(**(_SPEC | {"ad": "!!!", "slug": ""}))
    markdown = ProjectSpec(**_SPEC).to_markdown()
    assert "## M1 bitti ölçütü" in markdown and "- not ekle" in markdown


# --- depo ------------------------------------------------------------------


def test_finish_does_not_overwrite_a_stop(store: ProjectStore, tmp_path: Path) -> None:
    """Kullanıcı durdurduktan sonra ölmekte olan runner'ın "bitti" yazısı kararı ezemez."""

    job = store.create_job(slug="x", project_dir=tmp_path, coder="claude", command=["a"], prompt="p", session_id="s")
    assert store.mark_stopped(job.id)
    assert not store.finish_job(job.id, JobStatus.TAMAMLANDI, summary="bitti")
    assert store.get_job(job.id).status is JobStatus.DURDURULDU


def test_resume_only_from_question_and_notices_are_said_once(store: ProjectStore, tmp_path: Path) -> None:
    job = store.create_job(slug="x", project_dir=tmp_path, coder="claude", command=["a"], prompt="p", session_id="s")
    assert not store.resume_job(job.id, command=["b"], prompt="q"), "çalışan iş 'devam' ettirilemez"

    store.finish_job(job.id, JobStatus.SORU_BEKLIYOR, question="hangisi?")
    assert [j.id for j in store.unannounced_jobs()] == [job.id]
    assert store.unannounced_jobs() == []
    assert store.resume_job(job.id, command=["b"], prompt="q")
    assert store.get_job(job.id).question is None


# --- komut kurucu ------------------------------------------------------------


def test_claude_command_has_budget_session_and_denies_push(projeler: ProjelerSettings) -> None:
    command = coder_mod.build_command(projeler, Coder.CLAUDE, "sid-1", resume=False)

    assert command[0] == projeler.claude_command
    assert command[command.index("--session-id") + 1] == "sid-1"
    assert command[command.index("--max-budget-usd") + 1] == "5.00"
    assert "Bash(git push:*)" in command
    assert "Bash" in command[command.index("--allowedTools") + 1].split(",")
    rules = Path(command[command.index("--append-system-prompt-file") + 1])
    assert rules.is_file() and "SORU:" in rules.read_text(encoding="utf-8")
    # Windows'ta .cmd sarmalayıcısından geçeceği için hiçbir argüman satır içermez.
    assert not any("\n" in part for part in command)


def test_free_command_goes_through_cor_without_budget(projeler: ProjelerSettings) -> None:
    no_bash = projeler.model_copy(update={"allow_bash": False})
    command = coder_mod.build_command(no_bash, Coder.UCRETSIZ, "sid-2", resume=True)

    assert command[:3] == [projeler.cor_command, "claude", "-p"]
    assert command[command.index("--model") + 1] == projeler.free_model
    assert command[command.index("--resume") + 1] == "sid-2"
    assert "--max-budget-usd" not in command
    assert "Bash" not in command[command.index("--allowedTools") + 1].split(",")


# --- stream-json okuyucu -----------------------------------------------------


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def test_tracker_reports_activity_and_reads_question(tmp_path: Path) -> None:
    tracker = StreamTracker(tmp_path)
    write = {"type": "tool_use", "name": "Edit", "input": {"file_path": str(tmp_path / "src" / "app.py")}}
    assert tracker.feed(_line({"type": "assistant", "message": {"content": [write]}})) == "Düzenliyor: src/app.py"
    assert tracker.feed("bozuk satır {") is None
    assert tracker.feed(_line({"type": "stream_event", "event": {}})) is None

    tracker.feed(
        _line({"type": "result", "is_error": False, "result": "Hazır.\nSORU: Hangi port?", "total_cost_usd": 0.3})
    )
    outcome = tracker.outcome(0, "")
    assert outcome.status is JobStatus.SORU_BEKLIYOR
    assert outcome.question == "Hangi port?"
    assert outcome.summary == "Hazır."


def test_tracker_never_claims_success_without_a_result(tmp_path: Path) -> None:
    tracker = StreamTracker(tmp_path)
    tracker.feed(_line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Her şey bitti!"}]}}))

    outcome = tracker.outcome(0, "")
    assert outcome.status is JobStatus.BASARISIZ
    assert "sonuç bildirmeden" in (outcome.error or "")

    tracker.feed(_line({"type": "result", "is_error": False, "result": "tamam"}))
    assert tracker.outcome(1, "").status is JobStatus.BASARISIZ, "sıfır olmayan çıkış kodu başarı değildir"


# --- iş operasyonları (gerçek süreçler) -------------------------------------


@posix_only
def test_start_runs_coder_in_background_and_finishes(
    store: ProjectStore, projeler: ProjelerSettings, tmp_path: Path
) -> None:
    spec = _ready_spec(store)

    result = jobs.start(projeler, "Not Defteri", None)
    assert result.success, result.message
    job = _wait(store, result.data["job_id"])

    assert job.status is JobStatus.TAMAMLANDI, job.error
    assert job.summary == "M1 bitti, 3 test geçiyor."
    assert job.cost_usd == pytest.approx(0.42)
    project_dir = projeler.root / spec.slug
    assert (project_dir / "spec.md").read_text(encoding="utf-8").startswith("# Not Defteri")
    assert (project_dir / "app.py").exists()
    if shutil.which("git"):
        assert job.commits == 1
    call = _calls(tmp_path)[0]
    assert Path(call["cwd"]) == project_dir
    assert "spec.md" in call["prompt"]
    assert "--max-budget-usd" in call["argv"]
    assert store.open_interview() is None
    assert store.interviews_with_spec()[0].status == INTERVIEW_STARTED

    events = (coder_mod.job_dir(projeler, job.id) / "events.jsonl").read_text(encoding="utf-8")
    assert '"type": "result"' in events

    notices = pending_notices(projeler)
    assert len(notices) == 1
    assert "tamamlandı" in notices[0]
    assert pending_notices(projeler) == [], "bildirim bir kez söylenir"


@posix_only
def test_question_is_answered_on_the_same_session(
    store: ProjectStore, projeler: ProjelerSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAHTE_MOD", "soru")
    _ready_spec(store)
    job = _wait(store, jobs.start(projeler, "not-defteri", None).data["job_id"])
    assert job.status is JobStatus.SORU_BEKLIYOR
    assert job.question == "Postgres mi SQLite mı?"

    result = jobs.answer(projeler, None, "SQLite kullan")
    assert result.success, result.message
    job = _wait(store, job.id)

    assert job.status is JobStatus.TAMAMLANDI
    first, second = _calls(tmp_path)
    session = first["argv"][first["argv"].index("--session-id") + 1]
    assert second["argv"][second["argv"].index("--resume") + 1] == session
    assert "SQLite kullan" in second["prompt"]
    # Oturum maliyeti BİRİKİMLİ raporlanır: toplanmaz, son değer yazılır (0.42 + 0.55 değil).
    assert job.cost_usd == pytest.approx(0.55)


@posix_only
@pytest.mark.parametrize(("mode", "phrase"), [("cokme", "sonuç bildirmeden"), ("hata", "Bütçe aşıldı")])
def test_coder_failures_are_reported_as_failures(
    store: ProjectStore, projeler: ProjelerSettings, monkeypatch: pytest.MonkeyPatch, mode: str, phrase: str
) -> None:
    monkeypatch.setenv("SAHTE_MOD", mode)
    _ready_spec(store)
    result = jobs.start(projeler, "not-defteri", None)
    job = _wait(store, store.latest_job("not-defteri").id) if result.success else store.latest_job("not-defteri")

    assert job.status is JobStatus.BASARISIZ
    assert phrase in job.error


@posix_only
def test_stop_really_stops_the_process_tree(
    store: ProjectStore, projeler: ProjelerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAHTE_MOD", "uyku")
    _ready_spec(store)
    job_id = jobs.start(projeler, "not-defteri", None).data["job_id"]
    deadline = time.monotonic() + 20
    while store.get_job(job_id).last_activity != "uzun bir iş yapıyorum" and time.monotonic() < deadline:
        time.sleep(0.1)
    pid = store.get_job(job_id).pid
    assert coder_mod.pid_alive(pid)

    result = jobs.stop(projeler, "not-defteri")

    assert result.success, result.message
    assert not coder_mod.pid_alive(pid)
    assert store.get_job(job_id).status is JobStatus.DURDURULDU
    assert not jobs.stop(projeler, "not-defteri").success, "durmuş iş tekrar durdurulamaz"


def test_reconcile_marks_vanished_runs_as_interrupted(store: ProjectStore, tmp_path: Path) -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    job = store.create_job(slug="x", project_dir=tmp_path, coder="claude", command=["a"], prompt="p", session_id="s")
    store.set_pid(job.id, finished.pid)

    jobs.reconcile(store)

    job = store.get_job(job.id)
    assert job.status is JobStatus.YARIM_KALDI
    assert "yarım kaldı" in jobs.describe_job(job)


def test_start_refuses_unknown_names_foreign_folders_and_missing_cli(
    store: ProjectStore, projeler: ProjelerSettings
) -> None:
    assert "hazır bir proje spec'i yok" in jobs.start(projeler, "yok-boyle", None).message

    _ready_spec(store)
    foreign = projeler.root / "not-defteri"
    foreign.mkdir(parents=True)
    (foreign / "onemli.txt").write_text("dokunma", encoding="utf-8")
    result = jobs.start(projeler, "not-defteri", None)
    assert not result.success and "boş değil" in result.message
    shutil.rmtree(foreign)

    missing = projeler.model_copy(update={"cor_command": "kesinlikle-olmayan-cor"})
    result = jobs.start(missing, "not-defteri", "ucretsiz")
    assert not result.success and "cor bulunamadı" in result.message
    assert store.latest_job("not-defteri") is None, "başlatılamayan iş kayda girmemeli"


@posix_only
def test_spec_slug_cannot_escape_the_project_root(store: ProjectStore, projeler: ProjelerSettings) -> None:
    """Depodaki spec'e `../../disari` yazılsa bile proje kökün altında açılır."""

    _ready_spec(store, slug="../../disari")
    result = jobs.start(projeler, "Not Defteri", None)

    assert result.success, result.message
    assert Path(result.data["project_dir"]) == projeler.root / "disari"
    _wait(store, result.data["job_id"])
    assert not (projeler.root.parent.parent / "disari").exists()


# --- tool'lar ve döngü --------------------------------------------------------


def _settings_with(settings: Settings, projeler: ProjelerSettings) -> Settings:
    return settings.model_copy(update={"projeler": projeler})


def test_start_stop_and_answer_require_confirmation(settings: Settings, projeler: ProjelerSettings) -> None:
    dispatcher = ToolDispatcher(settings=_settings_with(settings, projeler))

    for islem in ("baslat", "cevapla", "durdur"):
        result = dispatcher.dispatch({"tool": "proje.islem", "arguments": {"islem": islem, "ad": "not-defteri"}})
        assert result.requires_confirmation
        assert islem in result.message and "not-defteri" in result.message


def test_new_project_opens_one_interview_and_status_needs_no_store(
    settings: Settings, projeler: ProjelerSettings
) -> None:
    dispatcher = ToolDispatcher(settings=_settings_with(settings, projeler))

    status = dispatcher.dispatch({"tool": "proje.sor", "arguments": {"islem": "durum"}})
    assert not status.success
    assert not coder_mod.db_path(projeler).exists(), "durum sormak depo yaratmamalı"

    first = dispatcher.dispatch({"tool": "proje.sor", "arguments": {"islem": "yeni", "metin": "not uygulaması"}})
    assert first.success and first.message == first_question()
    second = dispatcher.dispatch({"tool": "proje.sor", "arguments": {"islem": "yeni", "metin": "başka fikir"}})
    assert not second.success and "açık bir proje görüşmemiz" in second.message


def test_routing_is_inert_without_a_store(projeler: ProjelerSettings) -> None:
    assert route_to_interview(projeler, object(), "merhaba") is None  # type: ignore[arg-type]
    assert pending_notices(projeler) == []
    assert not coder_mod.db_path(projeler).exists()


class _ChatLLM:
    """Tool seçimi ve görüşme için sıralı cevaplar veren sahte istemci."""

    def __init__(self) -> None:
        self.tool_inputs: list[str] = []

    def get_tool_calls(self, system_prompt: str, user_input: str) -> list[dict[str, Any]]:
        self.tool_inputs.append(user_input)
        return [{"tool": "proje.sor", "arguments": {"islem": "yeni", "metin": "not uygulaması"}}]

    def get_structured_response(
        self, system_prompt: str, user_input: str, schema: dict[str, Any], schema_name: str = "cevap"
    ) -> dict[str, Any]:
        return {"durum": "hazir", "mesaj": "Anladım.", "spec": _SPEC}


@posix_only
def test_chat_loop_end_to_end_from_idea_to_finished_job(
    settings: Settings, projeler: ProjelerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--chat`: fikir → görüşme → 'başlat' → onay → arka planda kodlama → bildirim."""

    dispatcher = ToolDispatcher(settings=_settings_with(settings, projeler))
    llm = _ChatLLM()
    store = jobs.open_store(projeler)
    inputs = ["yeni bir proje yapalım, not uygulaması", "kendim için, CLI yeter", "başlat", "e"]
    printed: list[str] = []

    def fake_input(prompt: str = "") -> str:
        if inputs:
            return inputs.pop(0)
        # Bitişi bekle; bir sonraki turun başında bildirim söylenmeli.
        job = store.latest_job("not-defteri")
        _wait(store, job.id)
        # Boş girdi bir tur daha döndürür; turun başında bildirim basılır.
        return "çıkış" if any("tamamlandı" in line for line in printed) else ""

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    run_chat(dispatcher, llm)  # type: ignore[arg-type]

    assert llm.tool_inputs == ["yeni bir proje yapalım, not uygulaması"], "görüşme cevapları tool seçimine gitmemeli"
    text = "\n".join(printed)
    assert first_question() in text
    assert "Spec hazır" in text
    assert "kodlaması arka planda başladı" in text
    assert "[proje] 'not-defteri' tamamlandı" in text
    assert (projeler.root / "not-defteri" / "app.py").exists()
