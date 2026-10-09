"""Panelin "Projeler" sekmesi: proje işlerinin kartları; durdur, cevapla, klasör aç.

NEDEN BİR SEKME: proje işleri arka planda dakikalarca sürer. Umut "ne durumda?"
diye sohbete yazmadan bakabilmeli; soru soran bir iş için cevabını yerinde
verebilmeli ve gerektiğinde durdurabilmeli. Veriyi ve işlemleri `projects/jobs.py`
verir (`reconcile`, `stop`, `answer`); bu modülde ikinci bir iş mantığı YOK.

Yenileme: sekme görünürken her 5 saniyede bir okunur, gizlenince durur. Kartlar
yalnızca içerik değişince yeniden kurulur; yazılmakta olan bir cevap kutusu bu
yüzden her yenilemede silinmez.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config.settings import Settings
from models.project_models import Coder, JobStatus
from models.tool_models import ToolResult
from projects import jobs
from projects.store import Job
from projects.vault import display_dir
from ui import theme

logger = logging.getLogger(__name__)

REFRESH_MS = 5000
"""Sekme görünürken kayıtların yeniden okunma aralığı (milisaniye)."""

LIST_LIMIT = 20
"""Gösterilen en fazla iş; `ProjectStore.list_jobs` ile aynı sınır (en yenisi önce)."""

EMPTY_TEXT = "Henüz proje yok. 'Yeni bir proje yapalım' diyerek başlayabilirsin."
"""Hiç iş yokken gösterilen metin. Bu sekme bir dosya OLUŞTURMAZ: depo yoksa boş durum yeter."""

_DETAIL_LIMIT = 300
"""Kartta gösterilen özet/soru/hata metninin azami uzunluğu; tam metin kayıtta kalır."""

_STATUS_COLORS: dict[JobStatus, QColor] = {
    JobStatus.CALISIYOR: theme.ACCENT_BLUE,
    JobStatus.TAMAMLANDI: theme.ACCENT_GREEN,
    JobStatus.BASARISIZ: theme.ACCENT_RED,
    JobStatus.SORU_BEKLIYOR: theme.ACCENT_ORANGE,
    JobStatus.DURDURULDU: theme.TEXT_SECONDARY,
    JobStatus.YARIM_KALDI: theme.ACCENT_ORANGE,
}
"""Durum rozetinin rengi: çalışan mavi, biten yeşil, hata kırmızı, kullanıcı bekleyen turuncu."""

_ONGOING = (JobStatus.CALISIYOR, JobStatus.SORU_BEKLIYOR)
"""Hâlâ açık olan işler: süreleri bitiş zamanına değil, geçen süreye göre gösterilir."""


def _css(color: QColor, alpha: int | None = None) -> str:
    """Rengi QSS `rgba(...)` metnine çevirir. `ui/theme.py`'nin özel `_rgba`'sı yerine
    aynı paleti kullanmak için küçük bir yardımcı (theme'in iç API'sine bağlanılmaz)."""

    a = color.alpha() if alpha is None else alpha
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {a / 255:.3f})"


def open_folder(path: Path) -> None:
    """Klasörü işletim sisteminin dosya yöneticisinde açar; açılamazsa `OSError` fırlatır.

    Windows'ta `os.startfile` (proje Windows masaüstü hedefliyor), diğerlerinde `xdg-open`.
    `xdg-open` yoksa `Popen` `FileNotFoundError` verir: sessizce "açıldı" denmez. Dosya
    yöneticisinin penceresi asenkron açılır; bunun sonucu ölçülemez, bu yüzden çağıran
    "gönderildi" der, "açıldı" değil.
    """

    if sys.platform == "win32":
        import os

        os.startfile(path)  # Windows'a özgü; diğer platformlarda bu dal hiç çalışmaz.
    else:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _one_line(text: str | None) -> str:
    """Metni tek satıra indirir ve kart için kısaltır; metin yoksa `""`."""

    flat = " ".join((text or "").split())
    return flat if len(flat) <= _DETAIL_LIMIT else flat[: _DETAIL_LIMIT - 1].rstrip() + "…"


def _minutes(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} dk"


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True)
class _CardModel:
    """Bir kartın ekranda gösterilecek her parçası; eşitlik karşılaştırması içerik değişimini söyler."""

    job_id: int
    slug: str
    status: JobStatus
    coder: str
    timing: str
    evidence: str
    detail: str
    detail_label: str
    folder: Path
    vault_note: str | None


def _card_model(job: Job, now: float) -> _CardModel:
    """İş kaydını karta çevirir. Tutar yalnızca Claude kodlayıcısında yazılır (ücretsiz modelde anlamsız)."""

    if job.status in _ONGOING:
        timing = f"{_minutes(now - job.created_at)} geçti"
    else:
        end = job.finished_at or job.updated_at
        timing = f"{_minutes(end - job.created_at)} sürdü · {_when(end)}"

    evidence_parts: list[str] = []
    if job.commits is not None:
        evidence_parts.append(f"{job.commits} commit")
    if job.cost_usd is not None and job.coder == Coder.CLAUDE.value:
        evidence_parts.append(f"~{job.cost_usd:.2f} $")

    if job.status is JobStatus.CALISIYOR:
        detail_label, detail = "Son etkinlik", job.last_activity or "Henüz etkinlik bildirilmedi."
    elif job.status is JobStatus.SORU_BEKLIYOR:
        detail_label, detail = "Kodlayıcının sorusu", job.question or ""
    elif job.status is JobStatus.TAMAMLANDI:
        detail_label, detail = "Özet", job.summary or ""
    elif job.status in (JobStatus.BASARISIZ, JobStatus.YARIM_KALDI):
        detail_label, detail = "Hata", job.error or "Bilinmeyen sebep."
    else:
        detail_label, detail = "", ""

    return _CardModel(
        job_id=job.id,
        slug=job.slug,
        status=job.status,
        coder="Claude" if job.coder == Coder.CLAUDE.value else "Ücretsiz",
        timing=timing,
        evidence=" · ".join(evidence_parts),
        detail=_one_line(detail),
        detail_label=detail_label,
        folder=job.project_dir,
        vault_note=job.vault_note,
    )


class _ActionSignals(QObject):
    """İş parçacığındaki işlemin sonucunu GUI iş parçacığına taşır (bkz. `ui/panel.py::_CommandSignals`)."""

    done = pyqtSignal(object)


class ProjectsView(QWidget):
    """Proje işlerinin kart listesi.

    Args:
        settings: Ayarlar; proje ayarları `settings.projeler` içinden okunur.
        parent: Qt üst widget'ı.
        background: `True` (varsayılan) durdur/cevapla işlemini bir iş parçacığında
            yapar. `False` yalnızca testler içindir: işlem GUI iş parçacığında senkron çalışır.
    """

    def __init__(self, settings: Settings, parent: QWidget | None = None, *, background: bool = True) -> None:
        super().__init__(parent)
        self._settings = settings
        self._background = background
        self._busy = False
        self._models: list[_CardModel] | None = None
        self._buttons: list[QPushButton] = []
        self._answer_boxes: dict[int, QLineEdit] = {}
        self._signals = _ActionSignals(self)
        self._signals.done.connect(self._on_action_done)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._cards_host = QWidget()
        self._cards = QVBoxLayout(self._cards_host)
        self._cards.setContentsMargins(0, 0, 12, 0)
        self._cards.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._cards_host)
        layout.addWidget(scroll, stretch=1)

        self._error = self._hint_label()
        self._error.setVisible(False)
        layout.addWidget(self._error)

        self._status = self._hint_label()
        self._status.setVisible(False)
        layout.addWidget(self._status)

        self.refresh()

    # ------------------------------------------------------------------
    # Yenileme
    # ------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt'nin metot adı
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt'nin metot adı
        self._timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        """Kayıtları okur ve içerik değiştiyse kartları yeniden kurar.

        Depo dosyası yoksa `existing_store` None döner: hiçbir dosya oluşturulmaz ve
        boş durum gösterilir. Veritabanı hatası kırmızı bir satırda söylenir; önceki
        kartlar olduğu gibi kalır.
        """

        now = time.time()
        try:
            store = jobs.existing_store(self._settings.projeler)
            models: list[_CardModel] = []
            if store is not None:
                jobs.reconcile(store)
                models = [_card_model(job, now) for job in store.list_jobs(limit=LIST_LIMIT)]
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Proje kayıtları okunamadı: %s", exc)
            self._show_text(self._error, f"Proje kayıtları okunamadı: {exc}", ok=False)
            return

        self._error.setVisible(False)
        if models != self._models:
            self._models = models
            self._render(models)

    def _render(self, models: list[_CardModel]) -> None:
        # Yazılmakta olan cevaplar yeniden kurmada kaybolmasın: kutu metinleri önce saklanır.
        drafts = {job_id: box.text() for job_id, box in self._answer_boxes.items() if box.text()}
        self._answer_boxes = {}
        self._buttons = []

        while self._cards.count():
            item = self._cards.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Önce ebeveynden ayrılır: `deleteLater` silmeyi olay döngüsüne bırakır ve
                # eski kart, yeniden kurulmuş listede hâlâ bulunabilir kalır.
                widget.setParent(None)
                widget.deleteLater()

        if not models:
            self._cards.addStretch(1)
            empty = QLabel(EMPTY_TEXT)
            empty.setProperty("role", "hint")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._cards.addWidget(empty)
            self._cards.addStretch(1)
            return

        for model in models:
            card = self._build_card(model)
            if model.job_id in drafts and model.status is JobStatus.SORU_BEKLIYOR:
                self._answer_boxes[model.job_id].setText(drafts[model.job_id])
            self._cards.addWidget(card)
        self._cards.addStretch(1)
        self._set_busy(self._busy)

    # ------------------------------------------------------------------
    # Kart
    # ------------------------------------------------------------------

    def _build_card(self, model: _CardModel) -> QFrame:
        card = QFrame()
        card.setObjectName("projectCard")
        card.setStyleSheet(
            f"QFrame#projectCard {{ background-color: {_css(theme.BG_ELEVATED)};"
            f" border: 1px solid {_css(theme.BORDER)}; border-radius: 12px; }}"
            # Tema `QWidget`'a koyu zemin verir; etiketler kartın zeminini değil kendi zeminini
            # boyamalı, yoksa her satır kartın içinde koyu bir şerit olur.
            "QFrame#projectCard QLabel { background: transparent; }"
        )
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        name = QLabel(model.slug)
        name.setProperty("role", "title")
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setWordWrap(True)
        header.addWidget(name, stretch=1)
        badge = QLabel(f"{jobs.status_icon(model.status)} {jobs.STATUS_LABELS[model.status]}")
        badge.setStyleSheet(f"color: {_css(_STATUS_COLORS[model.status])}; font-weight: 600;")
        header.addWidget(badge)
        if model.status is JobStatus.CALISIYOR:
            stop = QPushButton("Durdur")
            stop.setToolTip("Kodlayıcı sürecini durdurur; yapılan iş klasörde kalır")
            stop.clicked.connect(lambda _=False, m=model: self._on_stop(m))
            header.addWidget(stop)
            self._buttons.append(stop)
        box.addLayout(header)

        meta = " · ".join(part for part in (model.coder, model.timing, model.evidence) if part)
        box.addWidget(self._text_label(meta, role="hint"))

        if model.detail_label:
            box.addWidget(self._text_label(f"{model.detail_label}:", role="hint"))
            box.addWidget(self._text_label(model.detail or "-"))

        if model.status is JobStatus.SORU_BEKLIYOR:
            row = QHBoxLayout()
            row.setSpacing(8)
            answer_box = QLineEdit()
            answer_box.setPlaceholderText("Cevabını yaz…")
            answer_box.returnPressed.connect(lambda m=model: self._on_answer(m))
            self._answer_boxes[model.job_id] = answer_box
            row.addWidget(answer_box, stretch=1)
            answer = QPushButton("Cevapla")
            answer.setProperty("role", "primary")
            answer.clicked.connect(lambda _=False, m=model: self._on_answer(m))
            row.addWidget(answer)
            self._buttons.append(answer)
            box.addLayout(row)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        folder_row.addWidget(self._text_label(f"📁 {display_dir(model.folder)}", role="hint"), stretch=1)
        open_button = QPushButton("Klasörü aç")
        open_button.clicked.connect(lambda _=False, p=model.folder: self._on_open_folder(p))
        folder_row.addWidget(open_button)
        self._buttons.append(open_button)
        box.addLayout(folder_row)

        if model.vault_note:
            box.addWidget(self._text_label(f"Vault notu: {model.vault_note}", role="hint"))
        return card

    @staticmethod
    def _text_label(text: str, role: str | None = None) -> QLabel:
        label = QLabel(text)
        if role:
            label.setProperty("role", role)
        label.setWordWrap(True)
        # Uzun yol ya da kesintisiz metin kartı genişletmesin: `Ignored` yatay politika
        # sarmayı zorlar ve yatay kaydırma çubuğu çıkmaz (bkz. `ui/panel.py` kaynak satırı).
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _hint_label() -> QLabel:
        label = QLabel()
        label.setProperty("role", "hint")
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    # ------------------------------------------------------------------
    # Eylemler
    # ------------------------------------------------------------------

    def _on_stop(self, model: _CardModel) -> None:
        choice = QMessageBox.question(
            self,
            "Projeyi durdur",
            f"'{model.slug}' kodlaması durdurulsun mu?\nYapılan iş klasörde kalır: {display_dir(model.folder)}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        self._run_action(jobs.stop, self._settings.projeler, model.slug)

    def _on_answer(self, model: _CardModel) -> None:
        box = self._answer_boxes.get(model.job_id)
        reply = box.text().strip() if box is not None else ""
        if not reply:
            self._show_text(self._status, "Cevap boş; kodlayıcıya ne yazayım?", ok=False)
            return
        self._run_action(jobs.answer, self._settings.projeler, model.slug, reply)

    def _on_open_folder(self, folder: Path) -> None:
        try:
            open_folder(folder)
        except OSError as exc:
            logger.warning("Proje klasörü açılamadı (%s): %s", folder, exc)
            self._show_text(self._status, f"Klasör açılamadı: {exc}", ok=False)
            return
        self._show_text(self._status, f"Klasör açma komutu gönderildi: {display_dir(folder)}", ok=True)

    def _run_action(self, action: Callable[..., ToolResult], *args: Any) -> None:
        """İşlemi (durdur/cevapla) arka planda çalıştırır; düğmeler sonuca kadar kilitlenir."""

        self._set_busy(True)
        self._show_text(self._status, "İşleniyor…", ok=True)
        if self._background:
            threading.Thread(
                target=self._perform,
                args=(action, args),
                name="artemis-proje-islem",
                daemon=True,
            ).start()
        else:
            self._perform(action, args)

    def _perform(self, action: Callable[..., ToolResult], args: tuple[Any, ...]) -> None:
        """İşlemi çalıştırır; HİÇBİR widget'a dokunmaz, sonucu sinyalle GUI'ye verir."""

        try:
            result = action(*args)
        # Geniş yakalama BİLEREK: bu iş parçacığında patlayan hata düğmeleri kilitli bırakırdı;
        # hata kırmızı bir satır olarak gösterilir.
        except Exception as exc:
            logger.exception("Proje işlemi beklenmeyen hata verdi")
            result = ToolResult(success=False, message=f"İşlem tamamlanamadı: {exc}")
        self._signals.done.emit(result)

    def _on_action_done(self, result: ToolResult) -> None:
        self._set_busy(False)
        self._show_text(self._status, result.message, ok=result.success)
        self.refresh()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in self._buttons:
            button.setEnabled(not busy)

    def _show_text(self, label: QLabel, text: str, *, ok: bool) -> None:
        label.setText(text)
        color = theme.TEXT_SECONDARY if ok else theme.ACCENT_RED
        label.setStyleSheet(f"color: {_css(color)};")
        label.setVisible(True)
