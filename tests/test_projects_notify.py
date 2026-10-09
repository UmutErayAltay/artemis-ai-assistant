"""Runner'ın iş bitince bildirim göndermesi: komut kurucu + süreç içi uçtan uca testler.

Kodlayıcı SAHTEDİR (`tests/test_projects.py::_FAKE_CODER`), Telegram da `127.0.0.1` üzerinde açılan gerçek
bir yerel HTTP sunucusudur (`tests/test_project_notify.py`). `runner.main` bu süreçte doğrudan çağrılır:
ayrık süreç başlatmaya gerek yok, `capsys` runner'ın `runner.log`'a gidecek satırlarını görür.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path

import pytest

from config.settings import ProjelerSettings
from models.project_models import Coder, JobStatus
from projects import coder as coder_mod
from projects import jobs, runner
from projects.store import Job, ProjectStore
from tests.test_project_notify import FakeTelegram, _bypass_proxy, telegram_server
from tests.test_projects import _FAKE_CODER, _ready_spec, posix_only

TOKEN = "987:GIZLI-TOKEN-abc"
TOKEN_ENV = "ARTEMIS_TEST_TELEGRAM_TOKEN"

# `telegram_server` ve `_bypass_proxy` fixture olarak yeniden kullanılır (kopyalanmaz).
__all__ = ["_bypass_proxy", "telegram_server"]


@pytest.fixture
def projeler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjelerSettings:
    """Sahte kodlayıcı kullanan ayarlar. Kodlayıcıyı sarmalayan betik, aldığı ortamın değişken ADLARINI yazar."""

    real = tmp_path / "bin" / "sahte-claude"
    real.parent.mkdir()
    real.write_text(_FAKE_CODER.format(python=sys.executable), encoding="utf-8")
    real.chmod(0o755)
    wrapper = tmp_path / "bin" / "sarmal-claude"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "open(os.environ['ORTAM_DOKUMU'], 'w', encoding='utf-8').write(json.dumps(sorted(os.environ)))\n"
        f"os.execv({str(real)!r}, [{str(real)!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("SAHTE_KAYIT", str(tmp_path / "kayit.jsonl"))
    monkeypatch.setenv("SAHTE_MOD", "basari")
    monkeypatch.setenv("ORTAM_DOKUMU", str(tmp_path / "ortam.json"))
    return ProjelerSettings(
        root=tmp_path / "Projeler",
        state_dir=tmp_path / "durum",
        claude_command=str(wrapper),
        cor_command=str(wrapper),
    )


def _create_job(projeler: ProjelerSettings, command: list[str] | None = None) -> tuple[ProjectStore, Job]:
    """`jobs.start`'ın yaptığını, ayrık runner BAŞLATMADAN yapar: spec, klasör ve CALISIYOR bir iş."""

    store = jobs.open_store(projeler)
    spec = _ready_spec(store)
    project_dir = projeler.root / spec.slug
    project_dir.mkdir(parents=True)
    (project_dir / "spec.md").write_text(spec.to_markdown(), encoding="utf-8")
    session_id = str(uuid.uuid4())
    job = store.create_job(
        slug=spec.slug,
        project_dir=project_dir,
        coder=Coder.CLAUDE.value,
        command=command or coder_mod.build_command(projeler, Coder.CLAUDE, session_id, resume=False),
        prompt="spec.md'yi oku.",
        session_id=session_id,
    )
    return store, job


def _run_runner(projeler: ProjelerSettings, job: Job, *extra: str) -> int:
    return runner.main(
        [
            "--db",
            str(coder_mod.db_path(projeler)),
            "--job",
            str(job.id),
            "--job-dir",
            str(coder_mod.job_dir(projeler, job.id)),
            *extra,
        ]
    )


def _telegram_args(server: FakeTelegram) -> list[str]:
    return [
        "--notify-telegram-chat-id",
        "42",
        "--notify-telegram-token-env",
        TOKEN_ENV,
        "--telegram-api-base",
        server.api_base,
    ]


# --- runner_command -------------------------------------------------------------


def test_runner_command_defaults_to_desktop_notification_and_always_passes_the_token_env_name() -> None:
    command = coder_mod.runner_command(ProjelerSettings(), 7)

    assert "--notify-desktop" in command
    assert command[command.index("--notify-telegram-token-env") + 1] == "ARTEMIS_TELEGRAM_BOT_TOKEN"
    assert "--notify-telegram-chat-id" not in command
    assert command[command.index("--notify-timeout") + 1] == "10.0"


def test_runner_command_without_any_channel_only_carries_the_token_env_name() -> None:
    command = coder_mod.runner_command(ProjelerSettings(desktop_notify=False, telegram_chat_id="  "), 7)

    assert [part for part in command if part.startswith("--notify")] == ["--notify-telegram-token-env"]


def test_runner_command_with_chat_id_never_contains_the_token_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    settings = ProjelerSettings(telegram_chat_id=" 42 ", telegram_token_env=TOKEN_ENV, desktop_notify=False)

    command = coder_mod.runner_command(settings, 7)

    assert command[command.index("--notify-telegram-chat-id") + 1] == "42"
    assert command[command.index("--notify-telegram-token-env") + 1] == TOKEN_ENV
    assert "--notify-desktop" not in command and "--notify-timeout" in command
    assert not any(TOKEN in part for part in command)


# --- uçtan uca --------------------------------------------------------------------


@posix_only
def test_finished_job_sends_one_telegram_message_and_the_coder_never_sees_the_token(
    projeler: ProjelerSettings,
    telegram_server: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    store, job = _create_job(projeler)

    assert _run_runner(projeler, job, *_telegram_args(telegram_server)) == 0

    finished = store.get_job(job.id)
    assert finished is not None and finished.status is JobStatus.TAMAMLANDI
    [request] = telegram_server.requests
    assert request.path == f"/bot{TOKEN}/sendMessage"
    assert request.body["chat_id"] == "42"
    assert request.body["text"].startswith(f"Artemis · {job.slug}")
    assert "✅" in request.body["text"] and "M1 bitti, 3 test geçiyor." in request.body["text"]
    out = capsys.readouterr().out
    assert "Bildirim gönderildi: telegram" in out and TOKEN not in out
    # Kodlayıcı çalıştı (ortamını yazdı) ve token'ı GÖRMEDİ
    coder_env = json.loads((tmp_path / "ortam.json").read_text(encoding="utf-8"))
    assert "SAHTE_MOD" in coder_env and TOKEN_ENV not in coder_env


@posix_only
def test_token_is_removed_from_the_environment_even_without_a_chat_id(
    projeler: ProjelerSettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kullanıcı değişkeni `setx` ile genel tanımlamış olabilir; Telegram kapalıysa da kodlayıcıya geçmemeli."""

    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    _, job = _create_job(projeler)

    assert _run_runner(projeler, job, "--notify-telegram-token-env", TOKEN_ENV) == 0

    assert TOKEN_ENV not in json.loads((tmp_path / "ortam.json").read_text(encoding="utf-8"))


@posix_only
def test_failed_notification_never_changes_the_job_nor_leaks_the_token(
    projeler: ProjelerSettings,
    telegram_server: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    telegram_server.respond(401, b'{"ok": false}')
    store, job = _create_job(projeler)

    with caplog.at_level(logging.DEBUG):
        assert _run_runner(projeler, job, *_telegram_args(telegram_server)) == 0

    finished = store.get_job(job.id)
    assert finished is not None and finished.status is JobStatus.TAMAMLANDI
    assert len(telegram_server.requests) == 1
    out = capsys.readouterr().out
    assert "Bildirim gönderilemedi (kanallar: telegram)" in out
    assert TOKEN not in out and TOKEN not in caplog.text


@posix_only
def test_question_notification_carries_the_answer_hint(
    projeler: ProjelerSettings, telegram_server: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv("SAHTE_MOD", "soru")
    store, job = _create_job(projeler)

    assert _run_runner(projeler, job, *_telegram_args(telegram_server)) == 0

    assert store.get_job(job.id).status is JobStatus.SORU_BEKLIYOR  # type: ignore[union-attr]
    [request] = telegram_server.requests
    assert "❓" in request.body["text"] and "Postgres mi SQLite mı?" in request.body["text"]
    assert f"Cevap: Artemis'e '{job.slug} için cevabım: ...' de." in request.body["text"]


@posix_only
def test_failing_coder_notification_is_marked_as_failure(
    projeler: ProjelerSettings, telegram_server: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv("SAHTE_MOD", "hata")
    store, job = _create_job(projeler)

    assert _run_runner(projeler, job, *_telegram_args(telegram_server)) == 0

    assert store.get_job(job.id).status is JobStatus.BASARISIZ  # type: ignore[union-attr]
    [request] = telegram_server.requests
    assert "❌" in request.body["text"] and "Bütçe aşıldı" in request.body["text"]


def test_coder_that_cannot_start_sends_one_failure_notification(
    projeler: ProjelerSettings,
    telegram_server: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    store, job = _create_job(projeler, command=[str(projeler.root / "yok" / "kodlayici")])

    assert _run_runner(projeler, job, *_telegram_args(telegram_server)) == 1

    failed = store.get_job(job.id)
    assert failed is not None and failed.status is JobStatus.BASARISIZ
    [request] = telegram_server.requests
    assert "❌" in request.body["text"] and "Kodlayıcı başlatılamadı" in request.body["text"]
    assert "Bildirim gönderildi: telegram" in capsys.readouterr().out


@posix_only
def test_without_notify_options_nothing_is_sent(
    projeler: ProjelerSettings,
    telegram_server: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    store, job = _create_job(projeler)

    assert _run_runner(projeler, job) == 0

    assert store.get_job(job.id).status is JobStatus.TAMAMLANDI  # type: ignore[union-attr]
    assert telegram_server.requests == []
    assert "Bildirim" not in capsys.readouterr().out
