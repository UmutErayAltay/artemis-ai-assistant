"""Bir kodlama işini AYRI bir süreçte yürütür: `python -m projects.runner --db ... --job N`.

Artemis bu modülü import etmez, yalnızca başlatır (`projects/coder.py::
spawn_runner`). Runner işin komutunu ve görev metnini depodan okur,
kodlayıcıyı (Claude Code headless) çalıştırır, `stream-json` çıktısını
satır satır `events.jsonl`'e yazar ve son durumu depoya kaydeder.

SONUÇ NASIL OKUNUR (gerçek çıktıyla doğrulandı, bkz. ARCHITECTURE.md §42):
    * `assistant` olayları  -> içerikteki `tool_use`/`text` blokları
      "son etkinlik" satırını besler ("Düzenliyor: app.py").
    * `result` olayı        -> `is_error`, `result` (son mesaj),
      `total_cost_usd`, `session_id`. `total_cost_usd` OTURUM BOYUNCA
      BİRİKİMLİDİR: `--resume` ile devam eden ikinci çalıştırmanın
      raporu ilkini de içerir (modelUsage token'ları toplanmış gelir).
      Bu yüzden toplanmaz, son değer yazılır.

"BAŞARDIM" İDDİASI KANITA BAĞLIDIR: `tamamlandi` yalnızca süreç 0 ile
çıktıysa VE bir `result` olayı `is_error=false` dediyse yazılır. Sonuç
olayı hiç gelmediyse (çöktü, öldürüldü, CLI bulunamadı) iş `basarisiz`
olur — kodlayıcının son metni ne kadar iyimser olursa olsun.

SORU: kodlayıcı ilerlemek için kullanıcıya danışması gerekiyorsa son
mesajında `SORU:` ile başlayan bir satır bırakır (bkz.
`prompts/kodlayici_kurallari.md`); iş `soru_bekliyor` olur ve
`proje.cevapla` aynı oturumu `--resume` ile sürdürür.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.project_models import JobStatus
from projects.jobs import record_job_outcome
from projects.store import ProjectStore
from projects.vault import VaultBridge

_QUESTION_RE = re.compile(r"^\s*SORU\s*:\s*(.+)", re.MULTILINE | re.DOTALL)
_STDERR_TAIL_CHARS = 600
_VAULT_RECORDED_STATUSES = (JobStatus.TAMAMLANDI, JobStatus.BASARISIZ, JobStatus.SORU_BEKLIYOR)
"""Runner'ın vault'a yazdığı sonuçlar. `durduruldu` / `yarim_kaldi` runner'ın değil
Artemis'in kararıdır (`jobs.stop`); runner o durumlarda yazmaz."""


@dataclass
class Outcome:
    status: JobStatus
    summary: str | None = None
    question: str | None = None
    error: str | None = None
    cost_usd: float | None = None


class StreamTracker:
    """`stream-json` satırlarını okuyup son etkinliği ve sonucu çıkarır.

    Bilinmeyen olay türleri (hook, stream_event, rate_limit...) sessizce
    atlanır: CLI yeni olay türleri ekledikçe runner kırılmasın.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self.result: dict[str, Any] | None = None
        self.last_text: str | None = None

    def feed(self, line: str) -> str | None:
        """Bir satırı işler; kullanıcıya gösterilecek yeni bir etkinlik varsa döndürür."""

        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(event, dict):
            return None

        kind = event.get("type")
        if kind == "result":
            self.result = event
            return None
        if kind == "system" and event.get("subtype") == "init":
            model = event.get("model") or "?"
            return f"Kodlayıcı başladı (model: {model})."
        if kind != "assistant":
            return None

        activity: str | None = None
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                activity = self._describe_tool(block.get("name", "?"), block.get("input") or {})
            elif block.get("type") == "text" and str(block.get("text", "")).strip():
                self.last_text = str(block["text"]).strip()
                activity = _one_line(self.last_text, 160)
        return activity

    def _describe_tool(self, name: str, tool_input: dict[str, Any]) -> str:
        if name in ("Write", "Edit", "Read", "NotebookEdit") and tool_input.get("file_path"):
            verb = {"Write": "Yazıyor", "Edit": "Düzenliyor", "Read": "Okuyor"}.get(name, name)
            return f"{verb}: {self._relative(str(tool_input['file_path']))}"
        if name == "Bash" and tool_input.get("command"):
            return f"Çalıştırıyor: {_one_line(str(tool_input['command']), 120)}"
        if name in ("Glob", "Grep") and tool_input.get("pattern"):
            return f"Arıyor: {_one_line(str(tool_input['pattern']), 80)}"
        return f"Araç: {name}"

    def _relative(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self._project_dir.resolve()))
        except (ValueError, OSError):
            return path

    def outcome(self, returncode: int, stderr_tail: str) -> Outcome:
        result = self.result
        if result is None:
            detail = f" Hata çıktısı: {stderr_tail}" if stderr_tail else ""
            return Outcome(
                JobStatus.BASARISIZ,
                error=f"Kodlayıcı bir sonuç bildirmeden kapandı (çıkış kodu {returncode}).{detail}",
            )

        cost = result.get("total_cost_usd")
        cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        text = str(result.get("result") or "").strip()

        if result.get("is_error") or returncode != 0:
            reason = text or str(result.get("subtype") or "bilinmeyen hata")
            return Outcome(
                JobStatus.BASARISIZ,
                error=f"Kodlayıcı hata ile bitti: {_one_line(reason, 400)} (çıkış kodu {returncode})",
                cost_usd=cost_usd,
            )

        question = _QUESTION_RE.search(text)
        if question:
            return Outcome(
                JobStatus.SORU_BEKLIYOR,
                question=question.group(1).strip()[:1000],
                summary=text[: question.start()].strip() or None,
                cost_usd=cost_usd,
            )
        return Outcome(JobStatus.TAMAMLANDI, summary=text or "(kodlayıcı özet bırakmadı)", cost_usd=cost_usd)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def count_commits(project_dir: Path) -> int | None:
    """Projedeki commit sayısı — "bitti" iddiasının yanına konan somut kanıt."""

    try:
        completed = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return 0 if (project_dir / ".git").exists() else None
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


def run_job(store: ProjectStore, job_id: int, job_directory: Path, bridge: VaultBridge | None = None) -> int:
    """İşi yürütür ve sonucu kaydeder. Çıkış kodu runner'ın kendi durumudur.

    Args:
        bridge: Verilirse ve iş sonuçlanırsa sonuç vault'a da yazılır. Vault
            sorunu işin durumunu ASLA değiştirmez: durum depoya vault'tan
            ÖNCE yazılır ve köprü hata fırlatmaz.
    """

    job = store.get_job(job_id)
    if job is None or job.status is not JobStatus.CALISIYOR:
        return 2

    job_directory.mkdir(parents=True, exist_ok=True)
    stderr_path = job_directory / "kodlayici-stderr.log"
    tracker = StreamTracker(job.project_dir)

    with (
        (job_directory / "events.jsonl").open("a", encoding="utf-8") as events,
        stderr_path.open("a", encoding="utf-8") as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                job.command,
                cwd=job.project_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            store.finish_job(job_id, JobStatus.BASARISIZ, error=f"Kodlayıcı başlatılamadı: {exc}")
            return 1

        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(job.prompt)
            process.stdin.close()
        except OSError:
            pass  # süreç hemen öldüyse sonuç olayı gelmez; aşağıda başarısız yazılır

        for line in process.stdout:
            events.write(line)
            events.flush()
            activity = tracker.feed(line)
            if activity:
                # NEDEN KISITLAMA (throttle) YOK: etkinlik yalnızca `system/init` ve
                # `assistant` olaylarından gelir (`StreamTracker.feed`), akış sağanağı
                # olan `stream_event` satırlarından değil; yani seyrektir. Eskiden
                # 2 sn'de bir yazılıp aradaki son etkinlik BİR SONRAKİ satıra kadar
                # bekletiliyordu: kodlayıcı uzun süre sessiz kalırsa (düşünüyor, test
                # koşuyor) `last_activity` bayat görünürdü.
                store.record_activity(job_id, activity)
        returncode = process.wait()

    stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-_STDERR_TAIL_CHARS:].strip()
    outcome = tracker.outcome(returncode, stderr_tail)
    written = store.finish_job(
        job_id,
        outcome.status,
        summary=outcome.summary,
        question=outcome.question,
        error=outcome.error,
        cost_usd=outcome.cost_usd,
        commits=count_commits(job.project_dir),
    )
    # `written` False ise iş bu arada durdurulmuştur; o kararı `jobs.stop` kaydeder,
    # burada ikinci (yanlış durumlu) bir vault girdisi açılmaz.
    if written and bridge is not None and outcome.status in _VAULT_RECORDED_STATUSES:
        finished = store.get_job(job_id)
        if finished is not None:
            record_job_outcome(bridge, store, finished)
    return 0


def _vault_bridge_from(args: argparse.Namespace) -> VaultBridge | None:
    """`--vault-*` seçeneklerinden köprüyü kurar; `--vault-path` yoksa None.

    Bozuk bir `--vault-command` işi düşürmez: vault isteğe bağlıdır, bu yüzden
    uyarı `runner.log`'a yazılır ve köprü kapalı kalır.
    """

    if args.vault_path is None:
        return None
    command: list[str] | None = None
    if args.vault_command is not None:
        try:
            parsed = json.loads(args.vault_command)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, list) or not all(isinstance(part, str) for part in parsed):
            print(f"Uyarı: --vault-command JSON metin listesi değil, vault kaydı yok: {args.vault_command!r}")
            return None
        command = parsed
    return VaultBridge(args.vault_path, command, args.vault_timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Artemis proje kodlama işi yürütücüsü")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--job", required=True, type=int)
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--vault-path", type=Path, default=None, help="Vault klasörü; verilmezse kayıt yapılmaz")
    parser.add_argument("--vault-command", type=str, default=None, help="Vault CLI komutu (JSON metin listesi)")
    parser.add_argument("--vault-timeout", type=float, default=20.0, help="Tek vault CLI çağrısı için saniye")
    args = parser.parse_args(argv)
    store = ProjectStore(args.db)
    try:
        return run_job(store, args.job, args.job_dir, _vault_bridge_from(args))
    except Exception as exc:  # noqa: BLE001 - runner ölmeden önce işi kapatmalı
        # İş "çalışıyor"da asılı kalmasın. Yığın izi runner.log'a gider
        # (stdout/stderr oraya yönlendirildi); kullanıcıya tür ve yer yeter.
        traceback.print_exc()
        store.finish_job(
            args.job,
            JobStatus.BASARISIZ,
            error=f"Runner beklenmeyen bir hatayla durdu ({type(exc).__name__}); ayrıntı: {args.job_dir / 'runner.log'}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
