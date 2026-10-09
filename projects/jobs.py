"""Proje işleri üzerindeki operasyonlar: başlat, cevapla, durdur, durum, liste.

`plugins/proje_plugin.py` bu fonksiyonları çağıran ince bir katmandır;
mantık burada, tek yerde. Her fonksiyon bir `ToolResult` döndürür ve
"başardım" demeden önce gerçekten olanı doğrular: başlatılan runner'ın
hemen ölüp ölmediğine bakılır, durdurulan sürecin gerçekten durduğu
kontrol edilir, "çalışıyor" görünen ama süreci olmayan iş `yarim_kaldi`
olarak düzeltilir (`reconcile`).
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

from config.settings import ProjelerSettings
from models.project_models import Coder, JobStatus, ProjectSpec, slugify
from models.tool_models import ToolResult
from projects import coder as coder_mod
from projects.interview import CONTEXT_ROLE
from projects.store import INTERVIEW_STARTED, Interview, Job, ProjectStore
from projects.vault import VaultBridge
from utils.paths import safe_join, unsafe_target_result

_STARTUP_GRACE_SECONDS = 1.0
"""Runner'ın hemen çöküp çökmediğini görmek için beklenen süre."""

_INITIAL_PROMPT = (
    "Bu klasördeki spec.md dosyasında tanımlanan projenin M1'ini geliştir. "
    "Önce spec.md'yi oku. Kuralların sistem promptunda; işin bitince kısa bir Türkçe özetle bitir."
)

STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.CALISIYOR: "kodlanıyor",
    JobStatus.TAMAMLANDI: "tamamlandı",
    JobStatus.BASARISIZ: "başarısız",
    JobStatus.SORU_BEKLIYOR: "soru bekliyor",
    JobStatus.DURDURULDU: "durduruldu",
    JobStatus.YARIM_KALDI: "yarım kaldı",
}
"""Vault proje notuna yazılan, insan okuyacağı durum etiketleri."""


def open_store(settings: ProjelerSettings) -> ProjectStore:
    return ProjectStore(coder_mod.db_path(settings))


def vault_bridge(settings: ProjelerSettings) -> VaultBridge:
    """Ayarlardan vault köprüsünü kurar; `vault_path` yoksa köprü pasiftir (hiçbir şey yapmaz)."""

    return VaultBridge(settings.vault_path, settings.vault_command, settings.vault_timeout_seconds)


def record_job_outcome(bridge: VaultBridge, store: ProjectStore, job: Job) -> None:
    """İşin sonucunu vault'taki proje notuna ve receipt'e yazar; not yolunu işe kaydeder.

    NEDEN HİÇBİR ŞEY FIRLATMAZ VE DURUMU DEĞİŞTİRMEZ: vault Artemis için
    isteğe bağlıdır. Köprü kendi hatalarını yutup `None` döner; burada da
    işin durumuna dokunulmaz — kodlayıcı başardıysa vault kapalı diye
    "başarısız" yazılmaz. Receipt reddedilse bile (`receipt_ok=False`) not
    yazılmıştır, o yüzden yol yine kaydedilir.
    """

    if not bridge.enabled:
        return
    try:
        spec = (job.project_dir / "spec.md").read_text(encoding="utf-8")
    except OSError:
        spec = None
    first_line = spec.lstrip().splitlines()[0] if spec and spec.strip() else ""
    title = first_line[2:].strip() if first_line.startswith("# ") else ""
    record = bridge.record_outcome(
        slug=job.slug,
        title=title or job.slug,
        status_label=STATUS_LABELS[job.status],
        summary=job.summary or job.question or job.error or "",
        project_dir=job.project_dir,
        spec_markdown=spec,
        # Aynı iş aynı durumda iki kez kaydedilirse receipt tekilleşsin; yeniden
        # başlatılan ya da cevaplanan iş farklı `finished_at` ile ayrı bir olay olur.
        event_key=f"{job.slug}-{job.id}-{job.status.value}-{int(job.finished_at or job.updated_at)}",
    )
    if record is not None:
        store.set_vault_note(job.id, record.note)


def existing_store(settings: ProjelerSettings) -> ProjectStore | None:
    """Depo dosyası VARSA açar. Döngüler her turda bunu çağırır; proje
    atölyesi hiç kullanılmadıysa diske tek bir dosya bile yazılmaz."""

    path = coder_mod.db_path(settings)
    return ProjectStore(path) if path.exists() else None


def reconcile(store: ProjectStore) -> None:
    """Kayıtta "çalışıyor" görünen ama süreci olmayan işleri `yarim_kaldi` yapar."""

    for job in store.list_jobs(limit=20):
        if job.status is JobStatus.CALISIYOR and job.pid and not coder_mod.pid_alive(job.pid):
            store.finish_job(
                job.id,
                JobStatus.YARIM_KALDI,
                error="Kodlama süreci artık çalışmıyor (makine kapandı ya da süreç çöktü); iş yarım kaldı.",
            )


def describe_job(job: Job) -> str:
    """Bir işin kullanıcıya okunacak tek paragraflık durumu."""

    text = _describe_status(job)
    return f"{text} Vault notu: {job.vault_note}" if job.vault_note else text


def _describe_status(job: Job) -> str:
    name = f"'{job.slug}'"
    if job.status is JobStatus.CALISIYOR:
        minutes = max(0, int((time.time() - job.created_at) // 60))
        last = f" Son: {job.last_activity}" if job.last_activity else ""
        return f"{name} kodlanıyor ({minutes} dk).{last}"
    if job.status is JobStatus.SORU_BEKLIYOR:
        return (
            f"{name} kodlayıcısı bir soru soruyor: {job.question} "
            f"Cevabını '{job.slug} için cevabım: ...' diye söyleyebilirsin."
        )
    if job.status is JobStatus.TAMAMLANDI:
        evidence = []
        if job.commits is not None:
            evidence.append(f"{job.commits} commit")
        if job.cost_usd is not None and job.coder == Coder.CLAUDE.value:
            evidence.append(f"~{job.cost_usd:.2f} $")
        extra = f" ({', '.join(evidence)})" if evidence else ""
        return f"{name} tamamlandı{extra}. Klasör: {job.project_dir}. Kodlayıcının özeti: {job.summary}"
    if job.status is JobStatus.DURDURULDU:
        return f"{name} durduruldu. Klasör: {job.project_dir}"
    reason = job.error or "bilinmeyen sebep"
    label = "yarım kaldı" if job.status is JobStatus.YARIM_KALDI else "başarısız oldu"
    return f"{name} {label}: {reason}"


def _find_spec(store: ProjectStore, name: str) -> tuple[Interview, ProjectSpec] | None:
    wanted = slugify(name)
    for interview in store.interviews_with_spec():
        spec = ProjectSpec(**interview.spec or {})
        if wanted in (spec.slug, slugify(spec.ad)):
            return interview, spec
    return None


def start(settings: ProjelerSettings, name: str, coder_name: str | None) -> ToolResult:
    store = open_store(settings)
    reconcile(store)
    found = _find_spec(store, name)
    if found is None:
        ready = [ProjectSpec(**i.spec or {}).slug for i in store.ready_specs()]
        hint = f" Hazır olanlar: {', '.join(ready)}." if ready else " Önce bir proje görüşmesi yapmalıyız."
        return ToolResult(success=False, message=f"'{name}' adında hazır bir proje spec'i yok.{hint}")
    interview, spec = found

    previous = store.latest_job(spec.slug)
    if previous is not None and previous.status in (JobStatus.CALISIYOR, JobStatus.SORU_BEKLIYOR):
        return ToolResult(success=False, message=f"'{spec.slug}' için zaten bir iş var. {describe_job(previous)}")

    project_dir = safe_join(settings.root, spec.slug)
    if project_dir is None:
        return unsafe_target_result(spec.slug)
    if project_dir.exists() and previous is None and any(p.name != "spec.md" for p in project_dir.iterdir()):
        # Artemis'in açmadığı, dolu bir klasöre kodlayıcı salınmaz.
        return ToolResult(
            success=False,
            message=f"'{project_dir}' zaten var ve boş değil; başka bir proje adı seçelim ya da klasörü taşı.",
        )

    coder = Coder(coder_name or spec.kodlayici)
    missing = _missing_cli(settings, coder)
    if missing:
        return ToolResult(success=False, message=missing)

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "spec.md").write_text(spec.to_markdown(), encoding="utf-8")

    session_id = str(uuid.uuid4())
    job = store.create_job(
        slug=spec.slug,
        project_dir=project_dir,
        coder=coder.value,
        command=coder_mod.build_command(settings, coder, session_id, resume=False),
        prompt=_INITIAL_PROMPT,
        session_id=session_id,
    )
    failure = _launch(settings, store, job)
    if failure:
        return failure
    store.mark_interview(interview.id, INTERVIEW_STARTED)
    label = "Claude" if coder is Coder.CLAUDE else f"ücretsiz model ({settings.free_model})"
    return ToolResult(
        success=True,
        message=(
            f"'{spec.slug}' kodlaması arka planda başladı ({label}). Klasör: {project_dir}. "
            "İstediğin an 'proje ne durumda' diye sorabilirsin; bitince haber veririm."
        ),
        data={"job_id": job.id, "project_dir": str(project_dir)},
    )


def answer(settings: ProjelerSettings, name: str | None, reply: str) -> ToolResult:
    store = open_store(settings)
    waiting = [
        job
        for job in store.list_jobs(limit=20)
        if job.status is JobStatus.SORU_BEKLIYOR and (not name or job.slug == slugify(name))
    ]
    if not waiting:
        return ToolResult(success=False, message="Cevap bekleyen bir proje işi yok.")
    if len(waiting) > 1:
        names = ", ".join(job.slug for job in waiting)
        return ToolResult(success=False, message=f"Birden fazla iş cevap bekliyor ({names}); hangisi için?")
    job = waiting[0]
    if not reply.strip():
        return ToolResult(success=False, message="Cevap boş; kodlayıcıya ne söyleyeyim?")

    coder = Coder(job.coder)
    missing = _missing_cli(settings, coder)
    if missing:
        return ToolResult(success=False, message=missing)
    command = coder_mod.build_command(settings, coder, job.session_id, resume=True)
    prompt = f"Kullanıcının cevabı: {reply.strip()}\n\nBu cevapla kaldığın yerden devam et; kurallar aynı."
    if not store.resume_job(job.id, command=command, prompt=prompt):
        return ToolResult(success=False, message=f"'{job.slug}' artık cevap beklemiyor. {describe_job(job)}")
    resumed = store.get_job(job.id)
    assert resumed is not None
    failure = _launch(settings, store, resumed)
    if failure:
        return failure
    return ToolResult(
        success=True,
        message=f"Cevabını '{job.slug}' kodlayıcısına ilettim; çalışmaya devam ediyor.",
        data={"job_id": job.id},
    )


def stop(settings: ProjelerSettings, name: str) -> ToolResult:
    store = open_store(settings)
    reconcile(store)
    job = store.latest_job(slugify(name))
    if job is None:
        return ToolResult(success=False, message=f"'{name}' adında bir proje işi yok.")
    if job.status not in (JobStatus.CALISIYOR, JobStatus.SORU_BEKLIYOR):
        return ToolResult(success=False, message=f"Durdurulacak bir şey yok. {describe_job(job)}")
    if job.status is JobStatus.CALISIYOR and job.pid and not coder_mod.stop_process_tree(job.pid):
        return ToolResult(success=False, message=f"'{job.slug}' sürecini durduramadım (pid {job.pid}).")
    if store.mark_stopped(job.id):
        # Yalnızca durdurma gerçekten yazıldıysa kaydedilir: bu arada biten bir işin
        # sonucu runner tarafından zaten vault'a işlenmiştir, ikinci kez yazılmaz.
        stopped = store.get_job(job.id)
        if stopped is not None:
            record_job_outcome(vault_bridge(settings), store, stopped)
    return ToolResult(success=True, message=f"'{job.slug}' durduruldu. Yapılan iş klasörde duruyor: {job.project_dir}")


def status(settings: ProjelerSettings, name: str | None) -> ToolResult:
    store = existing_store(settings)
    if store is None:
        return ToolResult(success=False, message="Henüz hiç proje işi başlatılmadı.")
    reconcile(store)
    job = store.latest_job(slugify(name)) if name else store.latest_job()
    if job is None:
        return ToolResult(
            success=False, message=f"'{name}' adında bir proje işi yok." if name else "Kayıtlı bir proje işi yok."
        )
    return ToolResult(success=True, message=describe_job(job), data={"job_id": job.id, "status": job.status.value})


def listing(settings: ProjelerSettings) -> ToolResult:
    store = existing_store(settings)
    if store is None:
        return ToolResult(success=False, message="Henüz hiç proje işi başlatılmadı.")
    reconcile(store)
    lines = [f"- {describe_job(job)}" for job in store.list_jobs(limit=5)]
    interview = store.open_interview()
    if interview is not None:
        lines.insert(0, f"- Açık bir proje görüşmesi var: {interview.idea[:80]}")
    if not lines:
        return ToolResult(success=False, message="Kayıtlı bir proje işi yok.")
    return ToolResult(success=True, message="\n".join(lines))


def begin_interview(settings: ProjelerSettings, idea: str, first_question: str) -> ToolResult:
    if not idea.strip():
        return ToolResult(success=False, message="Nasıl bir proje düşünüyorsun? Bir cümleyle anlatır mısın?")
    store = open_store(settings)
    current = store.open_interview()
    if current is not None:
        return ToolResult(
            success=False,
            message=f"Zaten açık bir proje görüşmemiz var ({current.idea[:60]}). Ona devam edelim ya da 'vazgeç' de.",
        )
    interview = store.create_interview(idea.strip(), first_question)

    # Vault bağlamı (tercihler + ilgili notlar) görüşmeye VERİ olarak eklenir; köprü pasifse
    # ya da bağlam boşsa hiçbir şey değişmez ve mesaj aynen `first_question` kalır.
    message = first_question
    sources: list[str] = []
    context = vault_bridge(settings).context_for(idea.strip())
    if context is not None and not context.is_empty():
        interview.transcript.insert(0, {"rol": CONTEXT_ROLE, "metin": context.to_prompt()})
        store.save_interview(interview)
        sources = context.sources()
        # Okunan notlar ADIYLA söylenir: alakasız olanı Umut fark edip "onlar ilgisiz" diyebilsin.
        # Yalnızca dosya adı (klasör ve `.md` yok) yeter; tam yol sesli okunacak mesajı şişirir.
        names = ", ".join(PurePosixPath(source.replace("\\", "/")).stem for source in sources)
        if context.notes and context.preferences:
            message += f" (Vault'tan tercihlerini ve {len(context.notes)} notu okudum: {names}.)"
        elif context.notes:
            message += f" (Vault'tan {len(context.notes)} notu okudum: {names}.)"
        else:
            message += " (Vault'tan tercihlerini okudum.)"
    return ToolResult(success=True, message=message, data={"interview_id": interview.id, "vault_sources": sources})


def _missing_cli(settings: ProjelerSettings, coder: Coder) -> str | None:
    if coder is Coder.CLAUDE and shutil.which(settings.claude_command) is None:
        return f"Claude Code CLI bulunamadı ('{settings.claude_command}'); kodlayıcı başlatılamaz."
    if coder is Coder.UCRETSIZ and shutil.which(settings.cor_command) is None:
        return f"cor bulunamadı ('{settings.cor_command}'); ücretsiz kodlayıcı cor üzerinden çalışır."
    return None


def _launch(settings: ProjelerSettings, store: ProjectStore, job: Job) -> ToolResult | None:
    """Runner'ı başlatır ve hemen ölmediğini doğrular. Sorun yoksa None."""

    try:
        process = coder_mod.spawn_runner(settings, job.id)
    except OSError as exc:
        store.finish_job(job.id, JobStatus.BASARISIZ, error=f"Runner başlatılamadı: {exc}")
        return ToolResult(success=False, message=f"Arka plan işini başlatamadım: {exc}")
    store.set_pid(job.id, process.pid)

    deadline = time.monotonic() + _STARTUP_GRACE_SECONDS
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.05)
    if process.poll() is None:
        return None

    # Runner bu kadar kısa sürede bittiyse ya kodlayıcı hemen hata verdi
    # ya da runner'ın kendisi çöktü; hangisi olduğunu kayıttan oku.
    finished = store.get_job(job.id)
    if finished is not None and finished.status is JobStatus.CALISIYOR:
        log = Path(coder_mod.job_dir(settings, job.id)) / "runner.log"
        store.finish_job(job.id, JobStatus.BASARISIZ, error=f"Runner hemen kapandı; ayrıntı: {log}")
        finished = store.get_job(job.id)
    if finished is not None and finished.status in (JobStatus.BASARISIZ, JobStatus.YARIM_KALDI):
        return ToolResult(success=False, message=describe_job(finished))
    return None
