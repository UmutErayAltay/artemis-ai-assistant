"""`ui/overlay.py` testleri: adım listesi, onay düğmeleri ve uzun cevap metni.

Pencere GERÇEKTİR (offscreen Qt) ama ekranda hiçbir şey açılmaz. Tıklamalar
`QTest` ile sentetik olarak verilir; gerçek fare kullanılmaz. Onay cevabı
doğrudan overlay'in geri çağrısına ulaşır — bu, sesli döngünün onu nasıl
dinlediğinin aynısıdır.
"""

from __future__ import annotations

import os
import threading

import pytest

pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from ui import theme
from ui.overlay import ArtemisOverlay, OverlayState, _visible_step_start, _wrap_text


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Modül boyunca TEK `QApplication` (diğer ui testleriyle aynı desen)."""

    return QApplication.instance() or QApplication([])


@pytest.fixture
def overlay(qapp: QApplication) -> ArtemisOverlay:
    window = ArtemisOverlay()
    yield window
    window.hide()


def _metrics() -> QFontMetrics:
    return QFontMetrics(QFont(theme.FONT_FAMILY, 12))


# --- Adım listesi --------------------------------------------------------


def test_steps_start_pending_and_follow_reported_states(overlay: ArtemisOverlay) -> None:
    overlay.show_steps(["Tarayıcıda açma", "Web'de arama", "Silme"])
    assert overlay._step_states == ["pending", "pending", "pending"]

    overlay.set_step_state(0, "running")
    assert overlay._step_states == ["running", "pending", "pending"]

    overlay.set_step_state(0, "done")
    overlay.set_step_state(1, "failed")
    assert overlay._step_states == ["done", "failed", "pending"]


def test_unknown_step_state_is_shown_as_pending_and_bad_index_is_ignored(overlay: ArtemisOverlay) -> None:
    overlay.show_steps(["Yazma", "Tıklama"])

    overlay.set_step_state(0, "bilinmeyen")
    overlay.set_step_state(7, "done")  # listede yok
    overlay.set_step_state(-1, "done")

    assert overlay._step_states == ["pending", "pending"]


def test_steps_survive_an_error_but_are_cleared_by_reply_and_new_turn(overlay: ArtemisOverlay) -> None:
    """Plan ortasındaki onay bir ERROR durumudur; adım listesi onda kalmalı.

    Cevap (SPEAKING) ve yeni tur (LISTENING) ise planın bittiğini ya da
    yenisinin başladığını gösterir; eski liste silinir.
    """

    overlay.show_steps(["Yazma", "Tıklama"])
    overlay.show_error("Onay gerekiyor")
    assert overlay._steps == ["Yazma", "Tıklama"]

    overlay.show_speaking("Tamamlandı.")
    assert overlay._steps == []

    overlay.show_steps(["Yazma", "Tıklama"])
    overlay.show_listening("Dinliyorum…")
    assert overlay._steps == []


def test_visible_step_window_keeps_the_running_step_on_screen() -> None:
    states = ["done", "done", "done", "done", "running", "pending"]

    start = _visible_step_start(states, 4)

    assert start <= 4 < start + 4
    assert _visible_step_start(["done", "pending"], 4) == 0


# --- Onay düğmeleri ------------------------------------------------------


def test_click_evet_resolves_confirmation_and_hides_buttons(overlay: ArtemisOverlay) -> None:
    decisions: list[bool] = []
    overlay.show_confirmation("Onay: filesystem.delete (target: x)", decisions.append)
    assert overlay._yes_button.isVisible()
    assert overlay._no_button.isVisible()

    QTest.mouseClick(overlay._yes_button, Qt.MouseButton.LeftButton)

    assert decisions == [True]
    assert not overlay._yes_button.isVisible()
    assert not overlay._no_button.isVisible()


def test_click_hayir_resolves_as_refusal(overlay: ArtemisOverlay) -> None:
    decisions: list[bool] = []
    overlay.show_confirmation("Onay: filesystem.delete (target: x)", decisions.append)

    QTest.mouseClick(overlay._no_button, Qt.MouseButton.LeftButton)

    assert decisions == [False]


def test_double_click_answers_only_once(overlay: ArtemisOverlay) -> None:
    decisions: list[bool] = []
    overlay.show_confirmation("Onay", decisions.append)

    QTest.mouseClick(overlay._yes_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(overlay._yes_button, Qt.MouseButton.LeftButton)

    assert decisions == [True], "Çift tıklama ikinci kez karar saymamalı"


def test_enter_is_evet_and_escape_is_hayir_only_while_confirming(overlay: ArtemisOverlay) -> None:
    decisions: list[bool] = []
    dismissed: list[int] = []
    overlay._dismiss_requested.connect(lambda: dismissed.append(1))

    overlay.show_confirmation("Onay", decisions.append)
    QTest.keyClick(overlay, Qt.Key.Key_Return)
    assert decisions == [True]

    overlay.show_confirmation("Onay", decisions.append)
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    assert decisions == [True, False]
    assert dismissed == [], "Onay açıkken Esc pencereyi kapatmamalı, reddetmeli"

    # Onay yokken Esc eski davranışı sürdürür: pencereyi kapatır.
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    assert dismissed == [1]


def test_click_elsewhere_does_not_dismiss_during_confirmation(overlay: ArtemisOverlay) -> None:
    decisions: list[bool] = []
    dismissed: list[int] = []
    overlay._dismiss_requested.connect(lambda: dismissed.append(1))
    overlay.show_confirmation("Onay", decisions.append)

    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))

    assert dismissed == []
    assert decisions == []
    assert overlay._yes_button.isVisible()


def test_hide_confirmation_removes_buttons_without_answering(overlay: ArtemisOverlay) -> None:
    decisions: list[bool] = []
    overlay.show_listening("Dinliyorum…")
    overlay.show_confirmation("Onay", decisions.append)
    assert overlay._state is OverlayState.ERROR, "onay turuncu/kırmızı palete geçmeli"

    overlay.hide_confirmation()
    QApplication.processEvents()

    assert decisions == [], "gizlemek karar vermemeli"
    assert not overlay._yes_button.isVisible()
    assert overlay._state is OverlayState.LISTENING, "palet onaydan önceki haline dönmeli"


def test_confirmation_and_steps_are_marshalled_from_a_worker_thread(overlay: ArtemisOverlay) -> None:
    """Ses işçisi (ayrı iş parçacığı) bu çağrıları yapar; GUI'ye sinyalle ulaşmalı."""

    decisions: list[bool] = []

    def worker() -> None:
        overlay.show_steps(["Yazma", "Tıklama"])
        overlay.set_step_state(0, "done")
        overlay.show_confirmation("Onay", decisions.append)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    QApplication.processEvents()

    assert overlay._steps == ["Yazma", "Tıklama"]
    assert overlay._step_states == ["done", "pending"]
    assert overlay._confirming
    assert overlay._yes_button.isVisible()


# --- Uzun cevap metni ----------------------------------------------------


def test_short_reply_stays_on_one_line() -> None:
    lines = _wrap_text("Dinliyorum…", _metrics(), 496, 4)

    assert lines == ["Dinliyorum…"]


def test_long_reply_wraps_into_at_most_four_lines_with_ellipsis() -> None:
    metrics = _metrics()
    text = " ".join(["Masaüstünde Orbit klasörünü oluşturdum, içine rapor koydum."] * 12)

    lines = _wrap_text(text, metrics, 496, 4)

    assert len(lines) == 4
    assert lines[-1].endswith("…")
    assert all(metrics.horizontalAdvance(line) <= 496 for line in lines)


def test_one_overlong_word_is_cut_inside_its_own_line() -> None:
    metrics = _metrics()

    lines = _wrap_text("x" * 400, metrics, 200, 4)

    assert len(lines) == 1
    assert metrics.horizontalAdvance(lines[0]) <= 200
    assert lines[0].endswith("…")


def test_window_size_never_changes_with_content(overlay: ArtemisOverlay) -> None:
    """Adım listesi, onay ya da uzun cevap pencereyi büyütmez: boyut baştan sabit."""

    size = (overlay.width(), overlay.height())

    overlay.show_steps(["Yazma", "Tıklama", "Silme", "Bekleme", "Kapatma"])
    assert (overlay.width(), overlay.height()) == size

    overlay.show_confirmation("Onay: " + "çok uzun bir argüman " * 10, lambda _ok: None)
    assert (overlay.width(), overlay.height()) == size

    overlay.show_speaking("uzun cevap " * 60)
    assert (overlay.width(), overlay.height()) == size


@pytest.mark.parametrize("mode", ["listening", "steps", "confirm", "long_reply"])
def test_every_mode_paints_at_window_size(overlay: ArtemisOverlay, mode: str) -> None:
    """Her görünüm (ve uzun cevap) gerçekten çizilebilmeli; boyut tutarlı kalmalı."""

    if mode == "steps":
        overlay.show_steps(["Yazma", "Tıklama", "Silme"])
        overlay.set_step_state(0, "done")
        overlay.set_step_state(1, "running")
    elif mode == "confirm":
        overlay.show_confirmation("Onay: filesystem.delete (target: x, location: desktop)", lambda _ok: None)
    elif mode == "long_reply":
        overlay.show_speaking("Tamamlandı. " * 40)
    else:
        overlay.show_listening("Dinliyorum…")

    pixmap = overlay.grab()

    assert (pixmap.width(), pixmap.height()) == (overlay.width(), overlay.height())
