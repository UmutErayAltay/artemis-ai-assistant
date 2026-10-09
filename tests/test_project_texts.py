"""`brief_job` / `telegram_job_text` / `pending_notices(brief=True)` testleri.

Mock yok: işler gerçek bir `ProjectStore` (tmp_path altında SQLite) üzerinden kurulur, yani metinler
depodan okunmuş gerçek `Job` kayıtlarından üretilir. `Path.home()` yalnızca sabit bir yere yönlendirilir ki
"ev dizini sızmıyor" iddiası çalıştığı makinenin gerçek ev dizinine (örn. kök `/` olan bir konteyner) bağlı olmasın.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.settings import ProjelerSettings
from models.project_models import JobStatus
from projects import jobs
from projects.jobs import brief_job, describe_job, telegram_job_text
from projects.session import pending_notices
from projects.store import Job, ProjectStore


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`Path.home()`'u geçici bir klasöre yönlendirir (bkz. `tests/test_project_vault.py`)."""

    fake_home = tmp_path / "ev" / "kullanici-adi"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path / "state" / "projeler.db")


def _job(
    store: ProjectStore,
    status: JobStatus,
    *,
    slug: str = "x",
    project_dir: Path | None = None,
    coder: str = "claude",
    **fields: Any,
) -> Job:
    """Depoda bir iş açar ve `status` durumuna getirir; depodan okunmuş kaydı döndürür."""

    job = store.create_job(
        slug=slug,
        project_dir=project_dir or store.db_path.parent / slug,
        coder=coder,
        command=["a"],
        prompt="p",
        session_id="s",
    )
    if status is JobStatus.DURDURULDU:
        assert store.mark_stopped(job.id)
    elif status is not JobStatus.CALISIYOR:
        assert store.finish_job(job.id, status, **fields)
    stored = store.get_job(job.id)
    assert stored is not None
    return stored


# --- brief_job ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "fields", "expected"),
    [
        (JobStatus.CALISIYOR, {}, "'x' kodlanıyor."),
        (JobStatus.DURDURULDU, {}, "'x' durduruldu."),
        (JobStatus.TAMAMLANDI, {"summary": "M1 bitti.\nDetay: 3 test geçiyor."}, "'x' tamamlandı: M1 bitti."),
        (JobStatus.TAMAMLANDI, {}, "'x' tamamlandı."),
        (
            JobStatus.SORU_BEKLIYOR,
            {"question": "Postgres mi SQLite mı?\nİkinci satır."},
            "'x' kodlayıcısı soru soruyor: Postgres mi SQLite mı? Cevap için 'x için cevabım: ...' de.",
        ),
        (JobStatus.BASARISIZ, {"error": "Bütçe aşıldı\nstack trace"}, "'x' başarısız oldu: Bütçe aşıldı"),
        (JobStatus.BASARISIZ, {}, "'x' başarısız oldu: bilinmeyen sebep"),
        (JobStatus.YARIM_KALDI, {"error": "Süreç öldü"}, "'x' yarım kaldı: Süreç öldü"),
        (JobStatus.YARIM_KALDI, {}, "'x' yarım kaldı: bilinmeyen sebep"),
    ],
)
def test_brief_job_is_one_short_sentence_per_status(
    store: ProjectStore, status: JobStatus, fields: dict[str, Any], expected: str
) -> None:
    assert brief_job(_job(store, status, **fields)) == expected


def test_brief_job_uses_first_non_empty_line_with_collapsed_whitespace(store: ProjectStore) -> None:
    job = _job(store, JobStatus.TAMAMLANDI, summary="\n\n   Bir   iki\tüç  \nikinci satır")

    assert brief_job(job) == "'x' tamamlandı: Bir iki üç"


def test_brief_job_cuts_the_detail_to_200_characters(store: ProjectStore) -> None:
    job = _job(store, JobStatus.TAMAMLANDI, summary="a" * 500)

    detail = brief_job(job).removeprefix("'x' tamamlandı: ")
    assert detail == "a" * 199 + "…"
    assert len(detail) == 200
    assert len(brief_job(job)) <= 250


def test_brief_job_leaves_out_paths_costs_and_commits(store: ProjectStore, home: Path) -> None:
    """Sesli okunan/toast'a giden cümlede klasör, tutar ve commit sayısı bulunmaz."""

    project_dir = home / "Desktop" / "Projeler" / "x"
    job = _job(
        store,
        JobStatus.TAMAMLANDI,
        project_dir=project_dir,
        summary="M1 bitti.",
        cost_usd=0.84,
        commits=2,
    )

    text = brief_job(job)
    assert text == "'x' tamamlandı: M1 bitti."
    assert str(project_dir) not in text and str(home) not in text
    assert "$" not in text and "commit" not in text


# --- telegram_job_text ---------------------------------------------------------


def test_telegram_text_of_a_finished_job_has_evidence_detail_and_display_dir(store: ProjectStore, home: Path) -> None:
    job = _job(
        store,
        JobStatus.TAMAMLANDI,
        project_dir=home / "Desktop" / "Projeler" / "x",
        summary="M1 bitti.\nDetay",
        cost_usd=0.84,
        commits=2,
    )

    assert telegram_job_text(job) == (
        "Artemis · x\n✅ tamamlandı (2 commit, ~0.84 $)\nM1 bitti.\nDetay\n📁 ~/Desktop/Projeler/x"
    )


def test_telegram_text_of_a_question_tells_how_to_answer(store: ProjectStore, home: Path) -> None:
    job = _job(
        store,
        JobStatus.SORU_BEKLIYOR,
        project_dir=home / "Desktop" / "Projeler" / "x",
        question="Postgres mi SQLite mı?",
        cost_usd=0.42,
        commits=1,
    )

    text = telegram_job_text(job)
    assert text == (
        "Artemis · x\n❓ soru bekliyor\nPostgres mi SQLite mı?\n📁 ~/Desktop/Projeler/x\n"
        "Cevap: Artemis'e 'x için cevabım: ...' de."
    )


@pytest.mark.parametrize(
    ("status", "fields", "expected_lines"),
    [
        (JobStatus.BASARISIZ, {"error": "Bütçe aşıldı"}, ["Artemis · x", "❌ başarısız", "Bütçe aşıldı"]),
        (JobStatus.YARIM_KALDI, {"error": "Süreç öldü"}, ["Artemis · x", "⚠️ yarım kaldı", "Süreç öldü"]),
        (JobStatus.DURDURULDU, {}, ["Artemis · x", "⏹ durduruldu"]),
        (JobStatus.CALISIYOR, {}, ["Artemis · x", "⏳ kodlanıyor"]),
    ],
)
def test_telegram_text_per_status_has_no_evidence_and_omits_empty_detail(
    store: ProjectStore, home: Path, status: JobStatus, fields: dict[str, Any], expected_lines: list[str]
) -> None:
    """Kanıt (commit, tutar) yalnızca TAMAMLANDI'da yazılır; ayrıntısı olmayan durumda ayrıntı satırı yoktur."""

    job = _job(store, status, project_dir=home / "p" / "x", cost_usd=0.84, commits=2, **fields)

    text = telegram_job_text(job)
    assert text.splitlines() == [*expected_lines, "📁 ~/p/x"]
    assert "commit" not in text and "$" not in text


def test_telegram_text_omits_cost_for_the_free_coder_and_empty_detail_for_finished_job(
    store: ProjectStore, home: Path
) -> None:
    job = _job(store, JobStatus.TAMAMLANDI, project_dir=home / "p" / "x", coder="ucretsiz", cost_usd=0.1, commits=3)

    assert telegram_job_text(job).splitlines() == ["Artemis · x", "✅ tamamlandı (3 commit)", "📁 ~/p/x"]


def test_telegram_text_never_contains_the_home_directory(store: ProjectStore, home: Path) -> None:
    """Mesaj makineden çıkar: ne klasör satırında ne ayrıntıdaki hata metninde kullanıcı adı olmalı."""

    error = f"Runner hemen kapandı; ayrıntı: {home}/memory/projeler/jobs/7/runner.log"
    job = _job(store, JobStatus.BASARISIZ, project_dir=home / "Desktop" / "x", error=error)

    text = telegram_job_text(job)
    assert str(home) not in text and home.as_posix() not in text
    assert "~/memory/projeler/jobs/7/runner.log" in text
    assert text.splitlines()[-1] == "📁 ~/Desktop/x"


def test_telegram_text_does_not_mangle_paths_when_home_is_the_filesystem_root(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kök dizini ev sayan bir konteynerde her `/` `~`e çevrilirse hata metni okunmaz hale gelirdi."""

    monkeypatch.setattr(Path, "home", lambda: Path("/"))
    job = _job(store, JobStatus.BASARISIZ, error="Günlük: /var/log/runner.log")

    assert "Günlük: /var/log/runner.log" in telegram_job_text(job)


def test_telegram_text_shows_a_folder_outside_home_as_is(store: ProjectStore, tmp_path: Path) -> None:
    outside = tmp_path / "baska-yer" / "x"
    job = _job(store, JobStatus.DURDURULDU, project_dir=outside)

    assert telegram_job_text(job).splitlines()[-1] == f"📁 {outside}"


def test_telegram_detail_keeps_line_structure_and_cuts_at_a_line_boundary(store: ProjectStore, home: Path) -> None:
    short = _job(store, JobStatus.TAMAMLANDI, slug="kisa", project_dir=home / "kisa", summary="A\nB\n\n\nC  \n")
    assert telegram_job_text(short).splitlines() == ["Artemis · kisa", "✅ tamamlandı", "A", "B", "", "C", "📁 ~/kisa"]

    lines = [f"satır {index:03d} " + "-" * 30 for index in range(100)]
    long = _job(store, JobStatus.TAMAMLANDI, slug="uzun", project_dir=home / "uzun", summary="\n".join(lines))

    text = telegram_job_text(long)
    detail = text.split("\n", 2)[2].rsplit("\n📁", 1)[0]
    assert len(detail) <= 1500
    assert detail.endswith("\n…")
    kept = detail.removesuffix("\n…").splitlines()
    assert kept == lines[: len(kept)], "kesim satır sınırında olmalı: yarım satır kalmamalı"
    assert 0 < len(kept) < len(lines)


# --- describe_job değişmedi ----------------------------------------------------


def test_describe_job_output_is_unchanged(store: ProjectStore, tmp_path: Path) -> None:
    """Chat modu `describe_job`'a güvenir; kanıt kuralı ortak yardımcıya taşınırken metin değişmemeli."""

    job = _job(store, JobStatus.TAMAMLANDI, project_dir=tmp_path / "x", summary="M1 bitti.", cost_usd=0.84, commits=2)
    assert describe_job(job) == (
        f"'x' tamamlandı (2 commit, ~0.84 $). Klasör: {tmp_path / 'x'}. Kodlayıcının özeti: M1 bitti."
    )

    free = _job(
        store,
        JobStatus.TAMAMLANDI,
        slug="y",
        project_dir=tmp_path / "y",
        coder="ucretsiz",
        summary="Bitti.",
        cost_usd=0.1,
    )
    assert describe_job(free) == f"'y' tamamlandı. Klasör: {tmp_path / 'y'}. Kodlayıcının özeti: Bitti."


# --- pending_notices(brief=True) -------------------------------------------------


def test_pending_notices_brief_returns_short_texts_and_marks_them_announced(tmp_path: Path) -> None:
    projeler = ProjelerSettings(state_dir=tmp_path / "state")
    store = jobs.open_store(projeler)
    created = store.create_job(
        slug="x", project_dir=tmp_path / "x", coder="claude", command=["a"], prompt="p", session_id="s"
    )
    assert store.finish_job(created.id, JobStatus.TAMAMLANDI, summary="M1 bitti.\nDetay", cost_usd=0.84, commits=2)

    notices = pending_notices(projeler, brief=True)

    assert notices == ["'x' tamamlandı: M1 bitti."]
    assert "Klasör" not in notices[0]
    assert pending_notices(projeler, brief=True) == [], "her iş bir kez söylenir"
    assert pending_notices(projeler) == [], "tam metin kipi de aynı işareti paylaşır"


def test_pending_notices_default_mode_still_returns_the_full_text(tmp_path: Path) -> None:
    projeler = ProjelerSettings(state_dir=tmp_path / "state")
    store = jobs.open_store(projeler)
    created = store.create_job(
        slug="x", project_dir=tmp_path / "x", coder="claude", command=["a"], prompt="p", session_id="s"
    )
    assert store.finish_job(created.id, JobStatus.TAMAMLANDI, summary="M1 bitti.")

    [notice] = pending_notices(projeler)

    assert notice == f"'x' tamamlandı. Klasör: {tmp_path / 'x'}. Kodlayıcının özeti: M1 bitti."
