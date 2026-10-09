"""Sohbet döngülerinin (`--chat`, `--voice`) proje atölyesine bağlandığı tek nokta.

Döngüler her turda iki şey sorar:

    route_to_interview(...)  -> açık bir proje görüşmesi varsa girdi ONA gider,
                                tool seçimine değil (bkz. projects/interview.py)
    pending_notices(...)     -> arka planda biten / soru soran işler için
                                kullanıcıya söylenecek bildirimler

İkisi de proje atölyesi hiç kullanılmadıysa diske DOKUNMAZ (depo dosyası
yoksa hemen boş döner) — her komutta bir SQLite dosyası yaratmak, testlerde
de gerçek repo dizinine yazmak demek olurdu.
"""

from __future__ import annotations

from config.settings import ProjelerSettings
from core.llm_types import LLMClient
from projects.interview import InterviewTurn, ProjectInterview
from projects.jobs import brief_job, describe_job, existing_store, reconcile


def route_to_interview(settings: ProjelerSettings, llm: LLMClient, user_input: str) -> InterviewTurn | None:
    """Açık bir görüşme varsa girdiyi işler; yoksa None (normal akış devam eder)."""

    store = existing_store(settings)
    if store is None:
        return None
    interview = store.open_interview()
    if interview is None:
        return None
    return ProjectInterview(store, llm, settings).handle(interview, user_input)


def pending_notices(settings: ProjelerSettings, *, brief: bool = False) -> list[str]:
    """Henüz söylenmemiş iş bitişleri/soruları — her biri bir kez döner.

    `brief=True` her iş için tek kısa cümle (`brief_job`) verir: kendiliğinden SESLİ söylenen
    bildirimde tam yol ve özet okunmasın diye. Varsayılan (`describe_job`) bir cevabın sonuna
    eklenen ayrıntılı metindir. İki kipte de iş "söylendi" diye işaretlenir.
    """

    store = existing_store(settings)
    if store is None:
        return []
    reconcile(store)
    describe = brief_job if brief else describe_job
    return [describe(job) for job in store.unannounced_jobs()]
