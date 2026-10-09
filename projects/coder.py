"""Arka plan kodlayıcısının komutunu kurar; runner sürecini başlatır, izler, durdurur.

KODLAYICI = Claude Code'un headless modu (`claude -p`). İki kip, TEK komut
biçimi — Vault'taki `.claude/scripts/ajan.py`'nin Umut'un makinesinde
kanıtlanmış yolu:

    claude   -> `claude -p ... --max-budget-usd <tavan>`
    ucretsiz -> `cor claude -p ... --model <ücretsiz slug>`
                (cor, `claude-*` olmayan modelleri OpenRouter'a çevirir)

Komut ARGÜMANLARI BİLEREK BASİT tutulur (boşluk/yeni satır yok): Windows'ta
`claude`/`cor` birer `.cmd` sarmalayıcısıdır ve çok satırlı bir argüman
cmd.exe'den geçerken bozulur. Bu yüzden kurallar `--append-system-prompt-file`
ile DOSYADAN, görev metni ise STDIN'den verilir (`ajan.py` ile aynı).

RUNNER AYRI SÜREÇTİR (`projects/runner.py`): Artemis kapansa da kodlama
sürer; durum `ProjectStore`'dan okunur. POSIX'te yeni bir oturum lideri
(`start_new_session`), Windows'ta yeni bir süreç grubu olarak başlatılır ki
durdurma tüm ağacı (runner + claude + onun alt süreçleri) kapsasın.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from config.settings import ProjelerSettings
from models.project_models import Coder

ARTEMIS_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ARTEMIS_ROOT / "prompts" / "kodlayici_kurallari.md"

_BASE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")
_DENIED_TOOLS = ("Bash(git push:*)", "Bash(git remote:*)")
"""Push ve uzak depo tanımı her durumda yasak: dış etki yalnızca Umut isteyince."""

_STILL_ACTIVE = 259  # Windows GetExitCodeProcess: süreç hâlâ çalışıyor


def job_dir(settings: ProjelerSettings, job_id: int) -> Path:
    """Bir işin günlüklerinin tutulduğu klasör (proje reposunun DIŞINDA)."""

    return settings.state_dir / "isler" / str(job_id)


def db_path(settings: ProjelerSettings) -> Path:
    return settings.state_dir / "projeler.db"


def build_command(settings: ProjelerSettings, coder: Coder, session_id: str, *, resume: bool) -> list[str]:
    """Kodlayıcı komutunu kurar.

    Args:
        coder: Claude mı, ücretsiz model mi.
        session_id: Claude Code oturum kimliği. İlk çalıştırmada
            `--session-id` ile ATANIR (sonradan çıktıdan avlanmaz);
            soru-cevap sonrası aynı kimlikle `--resume` edilir.
        resume: True ise mevcut oturuma devam edilir.
    """

    if coder is Coder.CLAUDE:
        base = [_resolve(settings.claude_command)]
        model = settings.claude_model
    else:
        base = [_resolve(settings.cor_command), "claude"]
        model = settings.free_model

    tools = [*_BASE_TOOLS, "Bash"] if settings.allow_bash else list(_BASE_TOOLS)
    command = [
        *base,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        ",".join(tools),
        "--disallowedTools",
        *_DENIED_TOOLS,
        # Kodlayıcı Vault hook'larını, MCP sunucularını ve skill yükünü
        # taşımasın (ajan.py'deki ölçüm: istek başı ~46K girdi token'ı).
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--append-system-prompt-file",
        str(RULES_PATH),
    ]
    if model:
        command += ["--model", model]
    if coder is Coder.CLAUDE:
        command += ["--max-budget-usd", f"{settings.claude_budget_usd:.2f}"]
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    return command


def _resolve(executable: str) -> str:
    # Windows'ta `claude` aslında `claude.cmd`; Popen çıplak adı PATHEXT ile
    # çözmez. Bulunamazsa ad olduğu gibi kalır ve runner temiz bir
    # "kodlayıcı başlatılamadı" hatası kaydeder.
    return shutil.which(executable) or executable


def runner_command(settings: ProjelerSettings, job_id: int) -> list[str]:
    """Runner sürecinin argv'si — saf bir fonksiyon, süreç başlatmadan test edilebilir.

    Vault ayarları runner'a komut satırıyla geçer çünkü runner ayrı bir süreçtir
    ve `Settings`'i yüklemez. Argümanlar `.cmd` sarmalayıcısına değil doğrudan
    python'a gider; bu yüzden `--vault-command`'ın JSON metni güvenle taşınır.
    """

    command = [
        sys.executable,
        "-m",
        "projects.runner",
        "--db",
        str(db_path(settings)),
        "--job",
        str(job_id),
        "--job-dir",
        str(job_dir(settings, job_id)),
    ]
    if settings.vault_path is not None:
        command += ["--vault-path", str(settings.vault_path), "--vault-timeout", str(settings.vault_timeout_seconds)]
        if settings.vault_command:
            command += ["--vault-command", json.dumps(settings.vault_command)]
    return command


def spawn_runner(settings: ProjelerSettings, job_id: int) -> subprocess.Popen[bytes]:
    """`projects/runner.py`'yi ayrık bir süreç olarak başlatır."""

    directory = job_dir(settings, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    command = runner_command(settings, job_id)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ARTEMIS_ROOT), os.environ.get("PYTHONPATH")]))}
    # Runner, Artemis kapansa bile yaşamalı ve bir ağaç olarak durdurulabilmeli;
    # bu yüzden Windows'ta yeni süreç grubu, POSIX'te yeni oturum açılır.
    # İki parametre de HER ZAMAN açıkça verilir (`**kwargs` mypy'nin Popen
    # aşırı yüklemesini çözmesini engelliyordu): POSIX'te `creationflags`
    # 0 olmak zorundadır (öyle), Windows'ta `start_new_session` göz ardı edilir.
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)

    with (directory / "runner.log").open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=ARTEMIS_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )


def pid_alive(pid: int | None) -> bool:
    """Bir sürecin hâlâ çalışıp çalışmadığını söyler.

    POSIX'te runner Artemis'in çocuğu olabilir; ölmüş ama toplanmamış bir
    çocuk (zombi) `os.kill(pid, 0)`'a hâlâ "var" der. Bu yüzden önce
    `waitpid(WNOHANG)` ile toplanmaya çalışılır.
    """

    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes  # lazy: Windows'a özgü

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass  # bizim çocuğumuz değil; aşağıdaki kontrol yeterli
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_tree(pid: int, timeout_seconds: float = 5.0) -> bool:
    """Runner'ı ve altındaki tüm süreçleri durdurur.

    Returns:
        Süreç gerçekten sonlandıysa True. "Gönderdim" değil, "durdu" —
        doğrulanmadan başarı bildirilmez.
    """

    if not pid_alive(pid):
        return True
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        if _wait_dead(pid, timeout_seconds):
            return True
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
    return _wait_dead(pid, timeout_seconds)


def _wait_dead(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)
