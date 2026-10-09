"""`ui/projects_view.py` testleri — "Projeler" sekmesi.

Gerçek bir `ProjectStore` tmp_path içinde kurulur; işlemler (`stop`, `answer`) ve
klasör açıcı monkeypatch ile değiştirilir. Hiçbir test LLM, ağ ya da gerçek bir
süreç kullanmaz; klasör açma gerçek makinede bir pencere açmasın diye engellenir.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QMessageBox, QPushButton

from config.settings import ProjelerSettings, Settings
from models.project_models import Coder, JobStatus
from models.tool_models import ToolResult
from projects import jobs
from projects.store import ProjectStore
from projects.vault import display_dir
from ui import projects_view
from ui.projects_view import EMPTY_TEXT, REFRESH_MS, ProjectsView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    projeler = ProjelerSettings(root=tmp_path / "Projeler", state_dir=tmp_path / "durum")
    return Settings(log_dir=tmp_path, db_path=tmp_path / "m.db", projeler=projeler)


def _db(settings: Settings) -> Path:
    return settings.projeler.state_dir / "projeler.db"


def _make_jobs(settings: Settings) -> dict[str, int]:
    """Dört durumda iş: çalışıyor, soru soruyor, tamamlandı, başarısız. Yollar sahte bir kökte."""

    store = ProjectStore(_db(settings))
    root = settings.projeler.root
    ids: dict[str, int] = {}

    running = store.create_job(
        slug="fiyat-takip",
        project_dir=root / "fiyat-takip",
        coder=Coder.CLAUDE.value,
        command=["claude"],
        prompt="p",
        session_id="s1",
    )
    store.record_activity(running.id, "src/tarayici.py yazılıyor")
    ids["running"] = running.id

    asking = store.create_job(
        slug="kitap-listesi",
        project_dir=root / "kitap-listesi",
        coder=Coder.UCRETSIZ.value,
        command=["cor"],
        prompt="p",
        session_id="s2",
    )
    store.finish_job(asking.id, JobStatus.SORU_BEKLIYOR, question="Veriler SQLite'ta mı tutulsun?")
    ids["asking"] = asking.id

    done = store.create_job(
        slug="not-defteri",
        project_dir=root / "not-defteri",
        coder=Coder.CLAUDE.value,
        command=["claude"],
        prompt="p",
        session_id="s3",
    )
    store.finish_job(done.id, JobStatus.TAMAMLANDI, summary="Arama ve etiket eklendi.", commits=3, cost_usd=0.84)
    ids["done"] = done.id

    failed = store.create_job(
        slug="hava-durumu",
        project_dir=root / "hava-durumu",
        coder=Coder.UCRETSIZ.value,
        command=["cor"],
        prompt="p",
        session_id="s4",
    )
    store.finish_job(failed.id, JobStatus.BASARISIZ, error="API anahtarı tanımlı değil.")
    ids["failed"] = failed.id
    return ids


def _cards(view: ProjectsView) -> list[QFrame]:
    return [frame for frame in view.findChildren(QFrame) if frame.objectName() == "projectCard"]


def _button(card: QFrame, text: str) -> QPushButton:
    matches = [b for b in card.findChildren(QPushButton) if b.text() == text]
    assert len(matches) == 1, f"'{text}' düğmesi bu kartta bulunmalı"
    return matches[0]


def _card_for(view: ProjectsView, slug: str) -> QFrame:
    for card in _cards(view):
        if any(label.text() == slug for label in card.findChildren(QLabel)):
            return card
    raise AssertionError(f"'{slug}' kartı yok")


def _label_texts(widget: Any) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


def test_empty_state_says_so_and_creates_no_database(qapp: QApplication, settings: Settings) -> None:
    view = ProjectsView(settings, background=False)

    assert not _db(settings).exists(), "sekme açılışta depo dosyası OLUŞTURMAMALI"
    assert _cards(view) == []
    assert EMPTY_TEXT in _label_texts(view)


def test_one_card_per_job_with_status_badge_coder_and_folder(qapp: QApplication, settings: Settings) -> None:
    ids = _make_jobs(settings)
    view = ProjectsView(settings, background=False)

    assert len(_cards(view)) == 4
    texts = _label_texts(view)
    assert "⏳ kodlanıyor" in texts
    assert "❓ soru bekliyor" in texts
    assert "✅ tamamlandı" in texts
    assert "❌ başarısız" in texts

    done = _card_for(view, "not-defteri")
    done_texts = _label_texts(done)
    assert any(text.startswith("Claude") and "3 commit" in text and "0.84 $" in text for text in done_texts)
    assert any("Arama ve etiket eklendi." in text for text in done_texts)
    assert display_dir(settings.projeler.root / "not-defteri") in "\n".join(done_texts)

    free = _card_for(view, "hava-durumu")
    free_meta = [t for t in _label_texts(free) if t.startswith("Ücretsiz")]
    assert free_meta, "ücretsiz kodlayıcı adıyla gösterilmeli"
    assert not any("$" in t for t in _label_texts(free)), "ücretsiz işte tutar gösterilmez"
    assert "API anahtarı tanımlı değil." in "\n".join(_label_texts(free))

    assert ids["running"] != ids["asking"]


def test_stop_button_asks_first_and_calls_stop_with_the_slug(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)
    asked: list[str] = []

    def fake_question(_parent: Any, title: str, text: str, *_args: Any) -> QMessageBox.StandardButton:
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    calls: list[tuple[Any, ...]] = []

    def fake_stop(cfg: ProjelerSettings, name: str) -> ToolResult:
        calls.append((cfg, name))
        return ToolResult(success=True, message="'fiyat-takip' durduruldu.")

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    monkeypatch.setattr(jobs, "stop", fake_stop)
    view = ProjectsView(settings, background=False)

    _button(_card_for(view, "fiyat-takip"), "Durdur").click()

    assert len(asked) == 1 and "'fiyat-takip'" in asked[0], "onay, durdurulacak projeyi adıyla göstermeli"
    assert calls == [(settings.projeler, "fiyat-takip")]
    assert "durduruldu" in view._status.text()


def test_declining_the_stop_dialog_does_nothing(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.No)
    calls: list[Any] = []
    monkeypatch.setattr(jobs, "stop", lambda *a: calls.append(a) or ToolResult(success=True, message=""))
    view = ProjectsView(settings, background=False)

    _button(_card_for(view, "fiyat-takip"), "Durdur").click()

    assert calls == []


def test_answer_box_sends_the_reply_to_answer(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)
    calls: list[tuple[Any, ...]] = []

    def fake_answer(cfg: ProjelerSettings, name: str, reply: str) -> ToolResult:
        calls.append((cfg, name, reply))
        return ToolResult(success=True, message="Cevabını 'kitap-listesi' kodlayıcısına ilettim.")

    monkeypatch.setattr(jobs, "answer", fake_answer)
    view = ProjectsView(settings, background=False)
    card = _card_for(view, "kitap-listesi")
    box = card.findChild(QLineEdit)
    assert box is not None, "soru kartında cevap kutusu olmalı"
    box.setText("  SQLite olsun  ")

    _button(card, "Cevapla").click()

    assert calls == [(settings.projeler, "kitap-listesi", "SQLite olsun")]


def test_empty_answer_is_refused_without_calling_answer(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)
    calls: list[Any] = []
    monkeypatch.setattr(jobs, "answer", lambda *a: calls.append(a) or ToolResult(success=True, message=""))
    view = ProjectsView(settings, background=False)

    _button(_card_for(view, "kitap-listesi"), "Cevapla").click()

    assert calls == []
    assert "boş" in view._status.text()


def test_a_typed_answer_survives_a_refresh(qapp: QApplication, settings: Settings) -> None:
    ids = _make_jobs(settings)
    view = ProjectsView(settings, background=False)
    view._answer_boxes[ids["asking"]].setText("yarım kalan taslak")

    store = ProjectStore(_db(settings))
    store.create_job(
        slug="yeni-is",
        project_dir=settings.projeler.root / "yeni-is",
        coder=Coder.CLAUDE.value,
        command=["claude"],
        prompt="p",
        session_id="s9",
    )
    view.refresh()

    assert len(_cards(view)) == 5
    assert view._answer_boxes[ids["asking"]].text() == "yarım kalan taslak"


def test_folder_button_calls_the_opener_with_the_project_folder(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)
    opened: list[Path] = []
    monkeypatch.setattr(projects_view, "open_folder", lambda path: opened.append(path))
    view = ProjectsView(settings, background=False)

    _button(_card_for(view, "not-defteri"), "Klasörü aç").click()

    assert opened == [settings.projeler.root / "not-defteri"]


def test_a_failing_opener_is_reported_not_swallowed(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)

    def broken(_path: Path) -> None:
        raise FileNotFoundError("xdg-open yok")

    monkeypatch.setattr(projects_view, "open_folder", broken)
    view = ProjectsView(settings, background=False)

    _button(_card_for(view, "not-defteri"), "Klasörü aç").click()

    assert "xdg-open yok" in view._status.text()


def test_database_error_shows_a_line_and_does_not_crash(
    qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_jobs(settings)
    view = ProjectsView(settings, background=False)
    before = len(_cards(view))

    def locked(_cfg: ProjelerSettings) -> None:
        raise sqlite3.OperationalError("veritabanı kilitli")

    monkeypatch.setattr(jobs, "existing_store", locked)
    view.refresh()

    assert "veritabanı kilitli" in view._error.text()
    assert not view._error.isHidden(), "hata satırı görünür olmalı"
    assert len(_cards(view)) == before, "hata olunca önceki kartlar kalır"


def test_actions_run_off_the_ui_thread(qapp: QApplication, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    _make_jobs(settings)
    seen: list[int] = []

    def slow_stop(_cfg: ProjelerSettings, _name: str) -> ToolResult:
        seen.append(threading.get_ident())
        return ToolResult(success=True, message="tamam")

    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(jobs, "stop", slow_stop)
    view = ProjectsView(settings)  # gerçek kip: arka plan iş parçacığı

    _button(_card_for(view, "fiyat-takip"), "Durdur").click()
    deadline = time.monotonic() + 5
    while view._status.text() != "tamam" and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert seen and seen[0] != threading.get_ident(), "işlem GUI iş parçacığında çalışmamalı"
    assert view._status.text() == "tamam"


def test_refresh_timer_runs_only_while_the_tab_is_visible(qapp: QApplication, settings: Settings) -> None:
    view = ProjectsView(settings, background=False)
    assert view._timer.interval() == REFRESH_MS == 5000
    assert not view._timer.isActive()

    view.show()
    qapp.processEvents()
    assert view._timer.isActive()

    view.hide()
    assert not view._timer.isActive()
    view.close()


def test_running_job_shows_last_activity(qapp: QApplication, settings: Settings) -> None:
    _make_jobs(settings)
    view = ProjectsView(settings, background=False)

    assert any("src/tarayici.py yazılıyor" in t for t in _label_texts(_card_for(view, "fiyat-takip")))
    assert _button(_card_for(view, "fiyat-takip"), "Durdur").isEnabled()


def test_status_icons_come_from_the_telegram_table() -> None:
    for status in JobStatus:
        assert jobs.status_icon(status) == jobs._STATUS_ICONS[status]
