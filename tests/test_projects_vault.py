"""Vault köprüsünün proje atölyesine bağlanması: görüşme bağlamı + iş sonucu kaydı.

`tests/test_project_vault.py` köprünün KENDİSİNİ sınar (`projects/vault.py`);
buradakiler köprünün `projects/jobs.py`, `projects/runner.py`, `projects/store.py`
ve `projects/interview.py` ile birleşimini sınar. Hiçbir şey mock'lanmaz: vault
`tmp_path` altında gerçek dosyalardan kurulur, `beyin.py` yerine
`tests/sahte_beyin.py`'nin gerçek bir alt süreç olarak çalışan betiği konur ve
iş testlerinde runner da kodlayıcı da gerçek ayrı süreçlerdir
(bkz. `tests/test_projects.py`; sahte kodlayıcı shebang ile çalıştığı için POSIX).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from config.settings import ProjelerSettings
from models.project_models import JobStatus
from projects import coder as coder_mod
from projects import jobs, runner
from projects.interview import CONTEXT_ROLE, ProjectInterview, first_question
from projects.store import Job, ProjectStore
from tests.sahte_beyin import SahteVault, sahte_vault
from tests.test_project_interview import _EMPTY_SPEC, ScriptedLLM
from tests.test_projects import _FAKE_CODER, _ready_spec, _wait, posix_only

_NOTE_SOURCE = "knowledge/concepts/sqlite-tercihi.md"
_NOTE_EXCERPT = "Küçük araçlarda sunucusuz SQLite kullanıyorum"
_RECORDS = [
    {"source": _NOTE_SOURCE, "text": f"# SQLite tercihi\n\n{_NOTE_EXCERPT}; Postgres gereksiz yük. [truncated]"},
    {"source": "daily/2026-09-01.md", "text": "# Günlük\n\nHam oturum kaydı, bağlama girmemeli."},
]
_PROJECT_NOTE = "🏰 300-Projects/not-defteri.md"

_OLD_JOBS_TABLE = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    coder TEXT NOT NULL,
    status TEXT NOT NULL,
    command TEXT NOT NULL,
    prompt TEXT NOT NULL,
    session_id TEXT NOT NULL,
    pid INTEGER,
    last_activity TEXT,
    question TEXT,
    summary TEXT,
    error TEXT,
    cost_usd REAL,
    commits INTEGER,
    announced INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
"""
"""`projects/store.py::_SCHEMA`'daki `jobs` tablosu, `vault_note` sütunu EKLENMEDEN önceki hâliyle."""


def _projeler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vault: SahteVault | None,
    *,
    bridge_on: bool = True,
    mode: str = "basari",
) -> ProjelerSettings:
    """`tests/test_projects.py::projeler` gibi sahte kodlayıcılı ayarlar + vault köprüsü.

    Args:
        vault: Verilirse CLI komutu ayarlara girer.
        bridge_on: False ise `vault_path` None kalır (köprü kapalı) ama komut yine de
            verilir; böylece "kapalıyken CLI hiç çağrılmaz" iddiası sınanabilir.
        mode: Sahte kodlayıcının davranışı (`SAHTE_MOD`).
    """

    script = tmp_path / "bin" / "sahte-claude"
    script.parent.mkdir(exist_ok=True)
    script.write_text(_FAKE_CODER.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("SAHTE_KAYIT", str(tmp_path / "kayit.jsonl"))
    monkeypatch.setenv("SAHTE_MOD", mode)
    return ProjelerSettings(
        root=tmp_path / "Projeler",
        state_dir=tmp_path / "durum",
        claude_command=str(script),
        cor_command=str(script),
        vault_path=vault.path if vault is not None and bridge_on else None,
        vault_command=vault.command if vault is not None else None,
    )


def _wait_for_note(store: ProjectStore, job_id: int, *, timeout: float = 20.0) -> Job:
    """Runner işi bitirdikten SONRA vault'a yazar; not yolunun kayda düşmesini bekler."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job is not None and job.vault_note is not None:
            return job
        time.sleep(0.1)
    raise AssertionError(f"iş {timeout} sn içinde vault notuna bağlanmadı: {store.get_job(job_id)}")


def _wait_for_activity(store: ProjectStore, job_id: int, expected: str, *, timeout: float = 20.0) -> None:
    """Kodlayıcının son etkinliğinin `expected` olmasını bekler (sessizleşmiş bir kodlayıcı dahil).

    Runner her yeni etkinliği anında yazar; böylece "kodlayıcı gerçekten uyku evresinde"
    kanıtı `last_activity`'dedir. Süre dolarsa sessizce geçilmez, test düşer.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job is not None and job.last_activity == expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"son etkinlik {timeout} sn içinde {expected!r} olmadı: {store.get_job(job_id)}")


def _wait_for_runner_exit(
    settings: ProjelerSettings, store: ProjectStore, job_id: int, *, timeout: float = 20.0
) -> str:
    """Runner'ın çıkmasını bekler ve `runner.log`'u döndürür.

    "Vault notu hiç yazılmadı" iddiası için sabit bir bekleme süresi yerine runner'ın
    BİTMESİ beklenir: runner vault'a yazmayı bırakıp çıktıysa artık yazacak bir şey kalmamıştır.
    """

    job = store.get_job(job_id)
    assert job is not None and job.pid
    deadline = time.monotonic() + timeout
    while coder_mod.pid_alive(job.pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not coder_mod.pid_alive(job.pid), "runner beklenen sürede çıkmadı"
    return (coder_mod.job_dir(settings, job_id) / "runner.log").read_text(encoding="utf-8", errors="replace")


def _receipts(vault: SahteVault) -> list[dict[str, Any]]:
    return [call for call in vault.calls() if call["argv"] and call["argv"][0] == "receipt"]


# --- 1. depo göçü ----------------------------------------------------------------


def test_store_migrates_a_database_without_the_vault_note_column(tmp_path: Path) -> None:
    """Önceki sürümün veritabanı `vault_note` sütunsuzdur; açılış sütunu ekler, kayıtlar okunur kalır."""

    db_path = tmp_path / "eski" / "projeler.db"
    db_path.parent.mkdir()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(_OLD_JOBS_TABLE)
        conn.execute(
            "INSERT INTO jobs (slug, project_dir, coder, status, command, prompt, session_id, announced, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1.0, 2.0)",
            ("eski-is", str(tmp_path / "eski-is"), "claude", JobStatus.TAMAMLANDI.value, '["claude"]', "p", "sid"),
        )

    store = ProjectStore(db_path)

    job = store.get_job(1)
    assert job is not None and job.slug == "eski-is"
    assert job.vault_note is None
    store.set_vault_note(1, _PROJECT_NOTE)
    job = store.get_job(1)
    assert job is not None and job.vault_note == _PROJECT_NOTE
    assert job.status is JobStatus.TAMAMLANDI, "not yazmak işin durumuna dokunmaz"
    assert [j.id for j in store.unannounced_jobs()] == [1], "not yazmak bildirim durumunu değiştirmez"

    ProjectStore(db_path)  # ikinci açılış sütun zaten varken de hatasız olmalı
    with closing(sqlite3.connect(db_path)) as conn:
        names = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
    assert names.count("vault_note") == 1


# --- 2. görüşme bağlamı ------------------------------------------------------------


def test_interview_gets_preferences_and_related_notes_as_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = sahte_vault(tmp_path, _RECORDS)
    projeler = _projeler(tmp_path, monkeypatch, vault)

    result = jobs.begin_interview(projeler, "not defteri uygulaması", first_question())

    assert result.success, result.message
    assert result.message.startswith(first_question())
    assert "Vault'tan" in result.message
    # Okunan not ADIYLA söylenir (klasör ve `.md` olmadan) ki alakasızsa Umut itiraz edebilsin.
    assert result.message == first_question() + " (Vault'tan tercihlerini ve 1 notu okudum: sqlite-tercihi.)"
    assert result.data["vault_sources"] == [_NOTE_SOURCE], "ham günlük kaydı bağlama girmemeli"
    store = jobs.open_store(projeler)
    interview = store.open_interview()
    assert interview is not None
    assert interview.transcript[0]["rol"] == CONTEXT_ROLE == "baglam"
    assert [turn["rol"] for turn in interview.transcript[1:]] == ["kullanici", "artemis"]

    llm = ScriptedLLM([{"durum": "soru", "mesaj": "Hangi dil?", "spec": _EMPTY_SPEC}])
    turn = ProjectInterview(store, llm, projeler.model_copy(update={"interview_max_questions": 2})).handle(
        interview, "kendim için"
    )

    assert turn.message == "Hangi dil?"
    sent = llm.inputs[0]
    assert sent.count("VAULT NOTLARI") == 1
    assert _NOTE_EXCERPT in sent
    assert "Ham oturum kaydı" not in sent
    assert "[truncated]" not in sent, "CLI'nın kırpma işareti modele gitmemeli"
    assert "Her madde" not in sent, "Core.md'nin üst bilgi cümlesi tercih değildir"
    conversation = sent.split("GÖRÜŞME:", 1)[1]
    assert "Umut'un kalıcı tercihleri" not in conversation, "vault bağlamı konuşma gibi basılmamalı"
    assert "Umut: not defteri uygulaması" in conversation and "Umut: kendim için" in conversation
    assert "SORU HAKKIN BİTTİ" not in sent, "bağlam turu soru hakkından düşmez"
    saved = store.open_interview()
    assert saved is not None and saved.transcript[0]["rol"] == CONTEXT_ROLE


def test_message_names_every_note_that_was_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"source": "knowledge/concepts/webrtc-p2p-sesli-goruntulu-arama.md", "text": "# WebRTC\n\nP2P arama."},
        {"source": "knowledge\\concepts\\supabase-mfa-totp.md", "text": "# MFA\n\nTOTP tuzakları."},
        {"source": "daily/2026-09-01.md", "text": "# Günlük\n\nGürültü."},
    ]
    vault = sahte_vault(tmp_path, records)
    projeler = _projeler(tmp_path, monkeypatch, vault)

    result = jobs.begin_interview(projeler, "sesli arama uygulaması", first_question())

    names = "webrtc-p2p-sesli-goruntulu-arama, supabase-mfa-totp"
    assert result.message == first_question() + f" (Vault'tan tercihlerini ve 2 notu okudum: {names}.)"
    assert len(result.data["vault_sources"]) == 2

    # Core.md yoksa yalnızca notlar vardır; mesaj aynı biçimi korur.
    (vault.path / "🔮 850-Companion" / "Core.md").unlink()
    jobs.open_store(projeler).mark_interview(result.data["interview_id"], "cancelled")
    notes_only = jobs.begin_interview(projeler, "sesli arama uygulaması", first_question())
    assert notes_only.message == first_question() + f" (Vault'tan 2 notu okudum: {names}.)"


# --- 3. kapalı köprü -------------------------------------------------------------


def test_disabled_bridge_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = sahte_vault(tmp_path, _RECORDS)
    projeler = _projeler(tmp_path, monkeypatch, vault, bridge_on=False)
    assert projeler.vault_path is None

    result = jobs.begin_interview(projeler, "not defteri uygulaması", first_question())

    assert result.success and result.message == first_question()
    assert result.data["vault_sources"] == []
    interview = jobs.open_store(projeler).open_interview()
    assert interview is not None
    assert [turn["rol"] for turn in interview.transcript] == ["kullanici", "artemis"]
    assert vault.calls() == [], "köprü kapalıyken CLI hiç çağrılmaz"


# --- 4. CLI çökerken ---------------------------------------------------------------


def test_failing_vault_cli_still_gives_preferences_from_core_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = sahte_vault(tmp_path, _RECORDS, context_exit=1)
    projeler = _projeler(tmp_path, monkeypatch, vault)

    result = jobs.begin_interview(projeler, "not defteri uygulaması", first_question())

    assert result.success, result.message
    assert result.message.startswith(first_question())
    assert result.message == first_question() + " (Vault'tan tercihlerini okudum.)"
    assert "notu" not in result.message, "CLI çöktü: not sayısı iddia edilmemeli"
    assert result.data["vault_sources"] == []
    interview = jobs.open_store(projeler).open_interview()
    assert interview is not None and interview.transcript[0]["rol"] == CONTEXT_ROLE
    assert "Tercih 1: Kısa ve net cevaplar." in interview.transcript[0]["metin"]


# --- 5. uçtan uca iş kaydı -----------------------------------------------------------


@posix_only
def test_finished_job_is_recorded_in_the_vault_note_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = sahte_vault(tmp_path)
    projeler = _projeler(tmp_path, monkeypatch, vault)
    store = jobs.open_store(projeler)
    _ready_spec(store)

    result = jobs.start(projeler, "not-defteri", None)
    assert result.success, result.message
    job = _wait(store, result.data["job_id"])
    assert job.status is JobStatus.TAMAMLANDI, job.error
    job = _wait_for_note(store, job.id)

    assert job.vault_note == _PROJECT_NOTE
    note = (vault.path / _PROJECT_NOTE).read_text(encoding="utf-8")
    assert "title: Not Defteri" in note
    assert "tamamlandı" in note
    assert "M1 bitti, 3 test geçiyor." in note
    assert "## M1 bitti ölçütü" in note, "spec.md notun içine alınır"
    assert "kodlayıcının sözleşmesidir" not in note, "spec'in kendi uyarısı notun uyarısıyla çift görünmemeli"
    assert note.count("\n> ") == 1, "notta yalnızca Artemis'in kendi uyarısı olmalı"
    receipts = _receipts(vault)
    assert len(receipts) == 1
    assert receipts[0]["receipt"]["refs"] == [job.vault_note]
    assert receipts[0]["receipt"]["event_id"].startswith("artemis-proje-not-defteri-")
    assert "Vault notu:" in jobs.describe_job(job) and _PROJECT_NOTE in jobs.describe_job(job)
    assert "Traceback" not in _wait_for_runner_exit(projeler, store, job.id)


# --- 6. vault sorunları işi bozmaz --------------------------------------------------------


@posix_only
def test_receipt_failure_keeps_the_job_and_the_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = sahte_vault(tmp_path, receipt_status="failed")
    projeler = _projeler(tmp_path, monkeypatch, vault)
    store = jobs.open_store(projeler)
    _ready_spec(store)

    job = _wait(store, jobs.start(projeler, "not-defteri", None).data["job_id"])
    job = _wait_for_note(store, job.id)

    assert job.status is JobStatus.TAMAMLANDI, "receipt reddedildi diye iş başarısız olmaz"
    assert job.vault_note == _PROJECT_NOTE, "receipt başarısız olsa da not yazılmıştır"
    assert len(_receipts(vault)) == 1


@posix_only
def test_vault_without_projects_folder_leaves_the_job_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = sahte_vault(tmp_path)
    shutil.rmtree(vault.path / "🏰 300-Projects")
    projeler = _projeler(tmp_path, monkeypatch, vault)
    store = jobs.open_store(projeler)
    _ready_spec(store)

    job = _wait(store, jobs.start(projeler, "not-defteri", None).data["job_id"])
    log = _wait_for_runner_exit(projeler, store, job.id)

    job = store.get_job(job.id)
    assert job is not None
    assert job.status is JobStatus.TAMAMLANDI, job.error
    assert job.vault_note is None
    assert _receipts(vault) == [], "not yazılamadıysa receipt de gönderilmez"
    assert "300-Projects" in log, "sorun runner.log'a uyarı olarak düşer"
    assert "Traceback" not in log
    assert "Vault notu" not in jobs.describe_job(job)


# --- 7. durdurma ---------------------------------------------------------------------------


@posix_only
def test_stopped_job_is_recorded_as_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = sahte_vault(tmp_path)
    projeler = _projeler(tmp_path, monkeypatch, vault, mode="uyku")
    store = jobs.open_store(projeler)
    _ready_spec(store)
    job_id = jobs.start(projeler, "not-defteri", None).data["job_id"]
    _wait_for_activity(store, job_id, "uzun bir iş yapıyorum")

    result = jobs.stop(projeler, "not-defteri")

    assert result.success, result.message
    job = store.get_job(job_id)
    assert job is not None
    assert job.status is JobStatus.DURDURULDU
    assert job.vault_note == _PROJECT_NOTE
    assert "durduruldu" in (vault.path / _PROJECT_NOTE).read_text(encoding="utf-8")
    assert len(_receipts(vault)) == 1, "öldürülen runner kendi adına ikinci bir kayıt yazmaz"


# --- 8. runner komutu ------------------------------------------------------------------------


def test_runner_command_carries_vault_options_only_when_configured(tmp_path: Path) -> None:
    base = ProjelerSettings(root=tmp_path / "Projeler", state_dir=tmp_path / "durum")

    plain = coder_mod.runner_command(base, 7)
    assert plain[:3] == [sys.executable, "-m", "projects.runner"]
    assert plain[plain.index("--job") + 1] == "7"
    assert plain[plain.index("--db") + 1] == str(coder_mod.db_path(base))
    assert plain[plain.index("--job-dir") + 1] == str(coder_mod.job_dir(base, 7))
    assert not any(option.startswith("--vault") for option in plain)

    vault_dir = tmp_path / "vault"
    no_command = base.model_copy(update={"vault_path": vault_dir, "vault_timeout_seconds": 5.0})
    command = coder_mod.runner_command(no_command, 7)
    assert command[: len(plain)] == plain
    assert command[command.index("--vault-path") + 1] == str(vault_dir)
    assert command[command.index("--vault-timeout") + 1] == "5.0"
    assert "--vault-command" not in command

    vault_command = [sys.executable, str(vault_dir / "beyin.py"), "--ek", "boşluklu değer"]
    with_command = base.model_copy(update={"vault_path": vault_dir, "vault_command": vault_command})
    command = coder_mod.runner_command(with_command, 7)
    assert "--vault-path" in command and str(vault_dir) in command
    assert json.loads(command[command.index("--vault-command") + 1]) == vault_command


def test_runner_ignores_a_malformed_vault_command_instead_of_dying(tmp_path: Path) -> None:
    """Vault isteğe bağlıdır: bozuk `--vault-command` kodlamayı düşürmez, yalnızca köprüyü kapatır."""

    def parse(**overrides: object) -> object:
        values: dict[str, object] = {"vault_path": tmp_path, "vault_command": None, "vault_timeout": 20.0}
        return runner._vault_bridge_from(argparse.Namespace(**(values | overrides)))

    assert parse(vault_path=None) is None
    assert parse(vault_command="bu json değil") is None
    assert parse(vault_command='{"liste": "değil"}') is None
    assert parse(vault_command="[1, 2]") is None
    bridge = parse(vault_command=json.dumps([sys.executable, "beyin.py"]))
    assert bridge is not None and bridge.enabled
