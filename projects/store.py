"""Proje görüşmelerinin ve arka plan kodlama işlerinin kalıcı kaydı.

NEDEN SQLite, NEDEN AYRI DOSYA: işin durumunu iki AYRI süreç yazar —
Artemis (başlatır, durdurur) ve `projects/runner.py` (kodlayıcının
çıktısını okuyup ilerlemeyi ve sonucu yazar). Artemis kapansa bile runner
çalışmaya devam eder; Artemis yeniden açıldığında durum buradan okunur.
`ContextMemory`'nin anahtar-değer tablosu bu iki sürecin eşzamanlı ve
koşullu güncellemelerine (örn. "yalnızca hâlâ çalışıyorsa bitti yaz")
uygun değil; bu yüzden aynı bağlantı desenini kullanan ayrı bir depo.

DURUM GEÇİŞLERİ KOŞULLUDUR: `finish_job` yalnızca iş hâlâ `calisiyor`
iken yazar. Böylece kullanıcı işi durdurduktan (`durduruldu`) hemen sonra
ölmekte olan runner'ın geç gelen bir "bitti" yazısı bu kararı ezemez.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.project_models import JobStatus

_LOCK_TIMEOUT_SECONDS = 10.0

INTERVIEW_ACTIVE = "active"
"""Artemis soru soruyor; kullanıcının her girdisi görüşmeye gider."""
INTERVIEW_READY = "ready"
"""Spec hazır, kullanıcının "başlat" demesi bekleniyor; girdiler hâlâ görüşmeye gider."""
INTERVIEW_STARTED = "started"
INTERVIEW_CANCELLED = "cancelled"
OPEN_INTERVIEW_STATUSES = (INTERVIEW_ACTIVE, INTERVIEW_READY)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    spec_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
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
    finished_at REAL,
    vault_note TEXT
);
"""


@dataclass
class Interview:
    """Bir proje görüşmesinin kaydı.

    `transcript`: `{"rol": "artemis"|"kullanici", "metin": str}` sözlükleri.
    """

    id: int
    idea: str
    transcript: list[dict[str, str]]
    status: str
    spec: dict[str, Any] | None


@dataclass
class Job:
    """Arka plandaki bir kodlama işinin kaydı."""

    id: int
    slug: str
    project_dir: Path
    coder: str
    status: JobStatus
    command: list[str]
    prompt: str
    session_id: str
    pid: int | None
    last_activity: str | None
    question: str | None
    summary: str | None
    error: str | None
    cost_usd: float | None
    commits: int | None
    created_at: float
    updated_at: float
    finished_at: float | None
    vault_note: str | None = None


class ProjectStore:
    """Görüşme ve iş kayıtlarını tutan SQLite deposu.

    Args:
        db_path: Veritabanı dosyası. Klasörü yoksa oluşturulur.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # `CREATE TABLE IF NOT EXISTS` mevcut bir tabloya sütun EKLEMEZ: önceki
            # sürümün oluşturduğu veritabanında `vault_note` yoktur ve `SELECT *`
            # sonrası `row["vault_note"]` patlardı. Eksikse burada eklenir.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "vault_note" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN vault_note TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # `memory/context_memory.py::ContextMemory._connect` ile aynı desen:
        # sqlite3'ün kendi `with`'i bağlantıyı KAPATMAZ.
        conn = sqlite3.connect(self.db_path, timeout=_LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # --- görüşmeler -------------------------------------------------------

    def create_interview(self, idea: str, first_question: str) -> Interview:
        now = time.time()
        transcript = [{"rol": "kullanici", "metin": idea}, {"rol": "artemis", "metin": first_question}]
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO interviews (idea, transcript, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (idea, json.dumps(transcript, ensure_ascii=False), INTERVIEW_ACTIVE, now, now),
            )
            interview_id = int(cursor.lastrowid or 0)
        return Interview(interview_id, idea, transcript, INTERVIEW_ACTIVE, None)

    def open_interview(self) -> Interview | None:
        """Kullanıcı girdisini üstlenen (aktif ya da başlatılmayı bekleyen) görüşme."""

        placeholders = ",".join("?" for _ in OPEN_INTERVIEW_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM interviews WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                OPEN_INTERVIEW_STATUSES,
            ).fetchone()
        return _row_to_interview(row) if row else None

    def save_interview(self, interview: Interview) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE interviews SET transcript = ?, status = ?, spec_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(interview.transcript, ensure_ascii=False),
                    interview.status,
                    json.dumps(interview.spec, ensure_ascii=False) if interview.spec is not None else None,
                    time.time(),
                    interview.id,
                ),
            )

    def ready_specs(self) -> list[Interview]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM interviews WHERE status = ? ORDER BY id DESC", (INTERVIEW_READY,)
            ).fetchall()
        return [_row_to_interview(row) for row in rows]

    def interviews_with_spec(self) -> list[Interview]:
        """Spec'i olan (hazır ya da daha önce başlatılmış) görüşmeler, yeniden eskiye.

        Başlatılmış olanlar da dahil: başarısız bir işi aynı spec'le yeniden
        başlatmak için görüşmeyi baştan yapmak gerekmesin.
        """

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM interviews WHERE spec_json IS NOT NULL AND status IN (?, ?) ORDER BY id DESC",
                (INTERVIEW_READY, INTERVIEW_STARTED),
            ).fetchall()
        return [_row_to_interview(row) for row in rows]

    def mark_interview(self, interview_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE interviews SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), interview_id)
            )

    # --- işler -------------------------------------------------------------

    def create_job(
        self,
        *,
        slug: str,
        project_dir: Path,
        coder: str,
        command: list[str],
        prompt: str,
        session_id: str,
    ) -> Job:
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs (slug, project_dir, coder, status, command, prompt, session_id, "
                "announced, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    slug,
                    str(project_dir),
                    coder,
                    JobStatus.CALISIYOR.value,
                    json.dumps(command, ensure_ascii=False),
                    prompt,
                    session_id,
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid or 0)
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: int) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def latest_job(self, slug: str | None = None) -> Job | None:
        with self._connect() as conn:
            if slug:
                row = conn.execute("SELECT * FROM jobs WHERE slug = ? ORDER BY id DESC LIMIT 1", (slug,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(self, limit: int = 10) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_job(row) for row in rows]

    def set_pid(self, job_id: int, pid: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET pid = ?, updated_at = ? WHERE id = ?", (pid, time.time(), job_id))

    def set_vault_note(self, job_id: int, note: str) -> None:
        """İşin vault'ta yazılan proje notunun yolunu kaydeder.

        Durum koşulu YOK ve `announced`'e dokunulmaz: not, işin sonucundan
        sonra (ve durdurulmuş işler için de) yazılabilir; kullanıcıya
        bildirim durumunu etkilemez.
        """

        with self._connect() as conn:
            conn.execute("UPDATE jobs SET vault_note = ?, updated_at = ? WHERE id = ?", (note, time.time(), job_id))

    def record_activity(self, job_id: int, activity: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET last_activity = ?, updated_at = ? WHERE id = ? AND status = ?",
                (activity[:300], time.time(), job_id, JobStatus.CALISIYOR.value),
            )

    def finish_job(
        self,
        job_id: int,
        status: JobStatus,
        *,
        summary: str | None = None,
        question: str | None = None,
        error: str | None = None,
        cost_usd: float | None = None,
        commits: int | None = None,
    ) -> bool:
        """İşi sonlandırır — YALNIZCA hâlâ `calisiyor` ise.

        Returns:
            Yazma gerçekleştiyse True; iş bu arada başka bir duruma
            (örn. kullanıcı durdurdu) geçmişse False.
        """

        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, summary = ?, question = ?, error = ?, "
                "cost_usd = COALESCE(?, cost_usd), commits = COALESCE(?, commits), "
                "announced = 0, updated_at = ?, finished_at = ? WHERE id = ? AND status = ?",
                (
                    status.value,
                    summary,
                    question,
                    error,
                    cost_usd,
                    commits,
                    now,
                    now,
                    job_id,
                    JobStatus.CALISIYOR.value,
                ),
            )
            return cursor.rowcount == 1

    def resume_job(self, job_id: int, *, command: list[str], prompt: str) -> bool:
        """Soru bekleyen işi kullanıcının cevabıyla yeniden `calisiyor` yapar."""

        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, command = ?, prompt = ?, question = NULL, pid = NULL, "
                "announced = 1, finished_at = NULL, updated_at = ? WHERE id = ? AND status = ?",
                (
                    JobStatus.CALISIYOR.value,
                    json.dumps(command, ensure_ascii=False),
                    prompt,
                    time.time(),
                    job_id,
                    JobStatus.SORU_BEKLIYOR.value,
                ),
            )
            return cursor.rowcount == 1

    def mark_stopped(self, job_id: int) -> bool:
        """Kullanıcı durdurdu. Zaten bitmiş bir işin sonucunu ezmez."""

        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, finished_at = ? WHERE id = ? AND status IN (?, ?)",
                (
                    JobStatus.DURDURULDU.value,
                    now,
                    now,
                    job_id,
                    JobStatus.CALISIYOR.value,
                    JobStatus.SORU_BEKLIYOR.value,
                ),
            )
            return cursor.rowcount == 1

    def unannounced_jobs(self) -> list[Job]:
        """Kullanıcıya henüz bildirilmemiş bitiş/soru durumları — ve bildirildi işaretler."""

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE announced = 0 ORDER BY id").fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                conn.execute(f"UPDATE jobs SET announced = 1 WHERE id IN ({','.join('?' for _ in ids)})", ids)
        return [_row_to_job(row) for row in rows]


def _row_to_interview(row: sqlite3.Row) -> Interview:
    return Interview(
        id=row["id"],
        idea=row["idea"],
        transcript=json.loads(row["transcript"]),
        status=row["status"],
        spec=json.loads(row["spec_json"]) if row["spec_json"] else None,
    )


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        slug=row["slug"],
        project_dir=Path(row["project_dir"]),
        coder=row["coder"],
        status=JobStatus(row["status"]),
        command=json.loads(row["command"]),
        prompt=row["prompt"],
        session_id=row["session_id"],
        pid=row["pid"],
        last_activity=row["last_activity"],
        question=row["question"],
        summary=row["summary"],
        error=row["error"],
        cost_usd=row["cost_usd"],
        commits=row["commits"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        vault_note=row["vault_note"],
    )
