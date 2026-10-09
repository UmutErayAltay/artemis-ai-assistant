"""Siri benzeri, çerçevesiz ve yarı saydam Artemis penceresi.

Bu modül YALNIZCA görüntüden sorumludur. Ses tanıma, LLM veya tool
çalıştırma mantığı burada yaşamaz; pencere dışarıdan basit metotlarla
(`show_listening()`, `set_amplitude()`, `show_thinking()`, ...) sürülür.
Böylece arayüz tamamen değiştirilse bile `core/` ve `voice/`
katmanlarında hiçbir değişiklik gerekmez.

Tek istisna ETKİLEŞİMLİ ONAYDIR: sesli onay beklenirken pencere Evet/Hayır
düğmeleri gösterir ve tıklamanın sonucunu bir geri çağrıyla (`on_decision`)
geri verir. Yani pencere, sesli cevapla AYNI soruyu sorar; hangisi önce
gelirse o geçerlidir (karar mantığı `core/voice_loop.py`'de yaşar).

İŞ PARÇACIĞI (THREAD) NOTU — önemli:
    Qt'de arayüz nesnelerine YALNIZCA ana (GUI) iş parçacığından
    dokunulabilir. Wake-word ve ses tanıma katmanları ise ayrı bir iş
    parçacığında çalışır. Bu yüzden buradaki tüm public metotlar,
    doğrudan çizim yapmak yerine bir Qt SİNYALİ yayınlar; sinyal Qt
    tarafından otomatik olarak GUI iş parçacığına kuyruklanır
    (`QueuedConnection`). Yani bu sınıfın public metotları HERHANGİ bir
    iş parçacığından güvenle çağrılabilir. Onay geri çağrısı (`on_decision`)
    ise GUI iş parçacığında, tıklamada çağrılır; onu dinleyen taraf bunu
    bir kilitle korumalıdır.

Tek başına önizleme (ses katmanı olmadan):

    python -m ui.overlay
"""

from __future__ import annotations

import math
import random
import sys
from collections.abc import Callable
from enum import Enum, auto

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from ui import theme

# --- Pencere ölçüleri ---------------------------------------------------
_WINDOW_WIDTH = 620
_GLOW_MARGIN = 34  # panelin dışında, parıltı (glow) için ayrılan boşluk
_CORNER_RADIUS = 30
_BOTTOM_OFFSET = 90  # ekranın altından yukarı boşluk (görev çubuğu payı)

# --- Yerleşim (panel içi, üst kenardan ölçülen piksel) ------------------
# Pencere boyu İÇERİĞE GÖRE DEĞİŞMEZ. Alttaki "bölge" (zone) en çok
# _TEXT_MAX_LINES satır tutar; adım listesi, onay satırı ya da cevap metni
# aynı bölgeyi sırayla kullanır. Bölge yüksekliği baştan ayrıldığı için uzun
# bir cevap pencereyi büyütüp ekranda yer değiştirmez ve dalga formunun
# üstüne binmez.
_WAVE_CENTER_Y = 98
_ZONE_TOP = 146
_ZONE_BOTTOM_PAD = 14
_ZONE_SIDE_PAD = 28
_TEXT_MAX_LINES = 4
_TEXT_FONT_SIZE = 12
_LIST_FONT_SIZE = 11

# --- Onay düğmeleri ----------------------------------------------------
_BUTTON_WIDTH = 112
_BUTTON_HEIGHT = 30
_BUTTON_GAP = 12
_BUTTON_RADIUS = 15

# --- Dalga formu --------------------------------------------------------
_BAR_COUNT = 38
_BAR_WIDTH = 7
_BAR_GAP = 5
_BAR_MIN_HEIGHT = 7
_BAR_MAX_HEIGHT = 82

# --- Animasyon ----------------------------------------------------------
_FRAME_INTERVAL_MS = 16  # ~60 fps
_FADE_DURATION_MS = 220
_AMPLITUDE_ATTACK = 0.45  # sese ne kadar hızlı tepki verilir (0-1)
_AMPLITUDE_RELEASE = 0.12  # sessizlikte ne kadar hızlı sönümlenir (0-1)

# --- Adım listesi -------------------------------------------------------
# Durum adları planner'ın (`core/planner.py::PlanProgress.state`) ürettiği
# değerlerle aynıdır; "pending" planner'da olay olarak gelmez, arayüz kendisi
# "henüz başlamadı" için kullanır.
_STEP_MARKS = {"pending": "·", "running": "⏳", "done": "✓", "failed": "✗"}
_STEP_MARK_COLORS = {
    "pending": theme.TEXT_MUTED,
    "running": theme.ACCENT_BLUE,
    "done": theme.ACCENT_GREEN,
    "failed": theme.ACCENT_RED,
}
_STEP_LABEL_COLORS = {
    "pending": theme.TEXT_MUTED,
    "running": theme.TEXT_PRIMARY,
    "done": theme.TEXT_SECONDARY,
    "failed": theme.TEXT_SECONDARY,
}
_STEP_MARK_GAP = 8

# Onay açıkken Enter = Evet, Esc = Hayır (bkz. `keyPressEvent`).
_ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

# "Düşünme" durumunun iki ucu: mavi ile mor arasında, ikisinden de koyu
# bir ton. `ui/theme.py`'de karşılığı OLMAYAN tek renk budur (palet oradan
# çıkarılırken atlanmış — Apple'in Sistem Renkleri'nde de karşılığı yok);
# bu yüzden paylaşılan palete zorla yakın bir renk sokmak yerine burada,
# SADECE bu durumda kullanılan yerel bir sabit olarak duruyor.
_THINKING_EDGE = QColor(120, 92, 255)


class OverlayState(Enum):
    """Pencerenin görsel durumu.

    Her durum farklı bir renk paleti ve farklı bir dalga formu davranışı
    ifade eder (bkz. `_PALETTES` ve `_advance_animation`).
    """

    LISTENING = auto()  # mikrofon dinliyor; dalga formu gerçek sese tepki verir
    THINKING = auto()  # LLM düşünüyor; dalga formu kendi kendine nabız atar
    SPEAKING = auto()  # TTS konuşuyor; dalga formu konuşmaya tepki verir
    ERROR = auto()  # bir hata oluştu; kırmızı/turuncu palet


# Her durum için (sol, orta, sağ) degrade renkleri. Renkler artık
# `ui/theme.py`'den gelir: aynı palet sohbet/ayarlar pencerelerinde de
# kullanıldığı için tek kaynak orasıdır (bkz. `ui/theme.py` modül
# dokümantasyonu).
_PALETTES: dict[OverlayState, tuple[QColor, QColor, QColor]] = {
    OverlayState.LISTENING: (theme.ACCENT_BLUE, theme.ACCENT_PURPLE, theme.ACCENT_PINK),
    OverlayState.THINKING: (_THINKING_EDGE, theme.ACCENT_PURPLE, _THINKING_EDGE),
    OverlayState.SPEAKING: (theme.ACCENT_TEAL, theme.ACCENT_BLUE, theme.ACCENT_TEAL),
    OverlayState.ERROR: (theme.ACCENT_ORANGE, theme.ACCENT_RED, theme.ACCENT_ORANGE),
}


def _qss_rgba(color: QColor, alpha: int | None = None) -> str:
    """`QColor`'ı QSS `rgba(...)` metnine çevirir.

    `ui/theme.py::_rgba` ile aynı işi yapar; o fonksiyon modül-içi (özel) olduğu
    için burada küçük bir kopya tutuluyor. Çıktı değişirse ikisi birlikte değişmeli.
    """

    a = color.alpha() if alpha is None else alpha
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {a / 255:.3f})"


def _button_style(primary: bool) -> str:
    """Onay düğmesinin QSS'i. Evet "birincil" (dolu mavi), Hayır ikincil (koyu) görünür.

    Düğmeler saydam bir pencerenin çocuğu olduğu için arka planları opak
    verilir; yoksa arkadaki masaüstü düğmenin içinden görünürdü.
    """

    if primary:
        body = (
            f"background-color: {_qss_rgba(theme.ACCENT_BLUE)}; color: white; border: none;"
        )
        hover = f"background-color: {_qss_rgba(theme.ACCENT_BLUE.lighter(112))};"
    else:
        body = (
            f"background-color: {_qss_rgba(theme.BG_ELEVATED)}; "
            f"color: {_qss_rgba(theme.TEXT_PRIMARY)}; "
            f"border: 1px solid {_qss_rgba(theme.BORDER)};"
        )
        hover = f"background-color: {_qss_rgba(theme.BG_ELEVATED_HOVER)};"
    return (
        f"QPushButton {{ {body} border-radius: {_BUTTON_RADIUS}px; "
        f'font-family: "{theme.FONT_FAMILY}"; font-size: 12px; font-weight: 600; padding: 0px; }}'
        f"QPushButton:hover {{ {hover} }}"
    )


def _wrap_text(text: str, metrics: QFontMetrics, width: int, max_lines: int) -> list[str]:
    """Metni `width` genişliğe sözcük sınırından sarar; en çok `max_lines` satır döner.

    NEDEN: eskiden uzun cevap tek satıra sığdırılıp sonu kesiliyordu ("…ve içine rapor
    ..."); kullanıcı cevabın ne olduğunu göremiyordu. Şimdi cevap okunaklı satırlara
    bölünür. Yine de bir sınır var: `max_lines` satırı aşan kısım son satırın sonunda
    "…" ile kesilir — pencere bu yüzden hiçbir cevapta büyümez.

    Tek başına sığmayan bir sözcük (uzun URL gibi) kendi satırında kesilir; bu da
    satırın taşmasını önler.
    """

    def fit(line: str) -> str:
        if metrics.horizontalAdvance(line) <= width:
            return line
        return metrics.elidedText(line, Qt.TextElideMode.ElideRight, width)

    words = text.split()
    lines: list[str] = []
    current = ""
    for position, word in enumerate(words):
        candidate = f"{current} {word}" if current else word
        if metrics.horizontalAdvance(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(fit(current))
            if len(lines) == max_lines:
                # Kalan sözcükler sığmadı: son satır, kalanla birlikte kesilir.
                rest = " ".join(words[position:])
                lines[-1] = metrics.elidedText(f"{lines[-1]} {rest}", Qt.TextElideMode.ElideRight, width)
                return lines
        current = word
    if current:
        lines.append(fit(current))
    return lines


def _visible_step_start(states: list[str], capacity: int) -> int:
    """Adım listesi bölgeye sığmazsa hangi adımdan başlayarak gösterileceğini seçer.

    Uzun planda çalışan adım gizlenmesin diye pencere, çalışan adımı (yoksa ilk bekleyeni)
    kapsayacak şekilde kayar; çalışan adımın bir üstündeki satır bağlam olarak görünür.
    """

    count = len(states)
    if count <= capacity:
        return 0
    active = next((i for i, state in enumerate(states) if state == "running"), None)
    if active is None:
        active = next((i for i, state in enumerate(states) if state == "pending"), count - 1)
    return max(0, min(active - 1, count - capacity))


class ArtemisOverlay(QWidget):
    """Ekranın altında beliren, Siri benzeri yarı saydam asistan penceresi.

    Dışarıdan sürülen bir "aptal" görüntü katmanıdır: kendi başına ne
    mikrofon dinler ne de karar verir. Tüm public metotları iş parçacığı
    güvenlidir (bkz. modül dokümantasyonundaki THREAD NOTU).
    """

    # Ayrı iş parçacıklarından gelen istekleri GUI iş parçacığına taşıyan sinyaller.
    _state_requested = pyqtSignal(object, str)
    _text_requested = pyqtSignal(str)
    _heard_requested = pyqtSignal(str)
    _amplitude_requested = pyqtSignal(float)
    _dismiss_requested = pyqtSignal()
    _steps_requested = pyqtSignal(object)
    _step_state_requested = pyqtSignal(int, str)
    _confirm_requested = pyqtSignal(str, object)
    _confirm_clear_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()

        self._state = OverlayState.LISTENING
        self._title = "ARTEMIS"
        self._text = ""
        self._heard = ""  # kullanıcının söylediği anlaşılan metin (üst satır)
        self._phase = 0.0
        self._amplitude = 0.0  # o an çizilen (yumuşatılmış) genlik
        self._target_amplitude = 0.0  # dışarıdan bildirilen ham genlik
        self._bar_noise = [random.uniform(0.0, math.tau) for _ in range(_BAR_COUNT)]

        # Adım listesi: etiketler ve her adımın durumu (bkz. `show_steps`).
        self._steps: list[str] = []
        self._step_states: list[str] = []

        # Sesli onay sırasında gösterilen düğmeler (bkz. `show_confirmation`).
        self._confirming = False
        self._confirm_summary = ""
        self._on_decision: Callable[[bool], None] | None = None
        self._state_before_confirm = self._state

        # Yerleşim, yazı tipinin satır yüksekliğine bağlıdır; pencere boyu da buna göre
        # bir kez hesaplanır ve sonra DEĞİŞMEZ (bkz. `_ZONE_TOP` yorumu).
        self._line_height = QFontMetrics(QFont(theme.FONT_FAMILY, _TEXT_FONT_SIZE)).lineSpacing()
        self._zone_height = _TEXT_MAX_LINES * self._line_height

        self._configure_window()
        self._build_confirm_buttons()

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(_FADE_DURATION_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._advance_animation)

        # Sinyalleri, GUI iş parçacığında çalışacak yuvalara (slot) bağla.
        self._state_requested.connect(self._apply_state)
        self._text_requested.connect(self._apply_text)
        self._heard_requested.connect(self._apply_heard)
        self._amplitude_requested.connect(self._apply_amplitude)
        self._dismiss_requested.connect(self._apply_dismiss)
        self._steps_requested.connect(self._apply_steps)
        self._step_state_requested.connect(self._apply_step_state)
        self._confirm_requested.connect(self._apply_confirmation)
        self._confirm_clear_requested.connect(self._clear_confirmation)

    # ------------------------------------------------------------------
    # Public API — herhangi bir iş parçacığından güvenle çağrılabilir
    # ------------------------------------------------------------------

    def show_listening(self, text: str = "Dinliyorum…") -> None:
        """Pencereyi gösterir ve dinleme moduna alır."""

        self._state_requested.emit(OverlayState.LISTENING, text)

    def show_thinking(self, text: str = "Düşünüyorum…") -> None:
        """LLM cevabı beklenirken nabız animasyonuna geçer."""

        self._state_requested.emit(OverlayState.THINKING, text)

    def show_speaking(self, text: str = "") -> None:
        """TTS konuşurken kullanılan moda geçer."""

        self._state_requested.emit(OverlayState.SPEAKING, text)

    def show_error(self, text: str) -> None:
        """Hata palletiyle bir mesaj gösterir."""

        self._state_requested.emit(OverlayState.ERROR, text)

    def set_heard(self, text: str) -> None:
        """Kullanıcının söylediği anlaşılan metni gösterir (başlığın altında).

        Alt satırdan (`set_text`) ayrıdır ve pencere kapanana kadar
        görünür kalır: asistanın yanlış anladığı ancak böyle fark edilir.
        """

        self._heard_requested.emit(text)

    def set_text(self, text: str) -> None:
        """Alt satırdaki metni günceller (örn. anlık konuşma dökümü)."""

        self._text_requested.emit(text)

    def set_amplitude(self, amplitude: float) -> None:
        """Mikrofon/hoparlör ses seviyesini bildirir (0.0 - 1.0).

        Dalga formunun yüksekliği bu değere göre canlanır. Değer
        yumuşatılarak uygulanır; ani sıçramalar tırtıklı görünmez.
        """

        self._amplitude_requested.emit(float(amplitude))

    def show_steps(self, labels: list[str]) -> None:
        """Plan adımlarını etiketleriyle gösterir; hepsi "bekliyor" durumunda başlar.

        Çağıran tarafın (sesli döngü) yalnızca çok adımlı planlarda çağırması beklenir;
        tek adımlı bir planın ayrı bir listeye ihtiyacı yoktur.
        """

        self._steps_requested.emit(list(labels))

    def set_step_state(self, index: int, state: str) -> None:
        """Bir adımın durumunu değiştirir.

        Args:
            index: Adımın 0-tabanlı sırası (`show_steps` listesindeki konum).
            state: "pending", "running", "done" ya da "failed". Bilinmeyen bir değer
                "pending" sayılır; çizim asla bozulmaz.
        """

        self._step_state_requested.emit(int(index), state)

    def show_confirmation(self, summary: str, on_decision: Callable[[bool], None]) -> None:
        """Sesli onay beklenirken Evet/Hayır düğmelerini gösterir.

        Args:
            summary: Onaylanacak işlemin kısa metni (tool adı ve argümanlar); NEYİ
                onayladığını kullanıcının görmesi için gösterilir.
            on_decision: Düğmeye tıklanınca ya da Enter (Evet) / Esc (Hayır) basılınca
                `True`/`False` ile çağrılır. Bu çağrı GUI iş parçacığında gerçekleşir.
        """

        self._confirm_requested.emit(summary, on_decision)

    def hide_confirmation(self) -> None:
        """Onay düğmelerini gizler; cevap sesle geldiğinde ya da zaman aşımında çağrılır."""

        self._confirm_clear_requested.emit()

    def dismiss(self) -> None:
        """Pencereyi yumuşakça kapatır (fade-out)."""

        self._dismiss_requested.emit()

    # ------------------------------------------------------------------
    # GUI iş parçacığında çalışan yuvalar (slots)
    # ------------------------------------------------------------------

    def _apply_state(self, state: OverlayState, text: str) -> None:
        # Yeni bir dinleme turu başlıyorsa önceki turun dökümü silinmeli;
        # aksi halde kullanıcı bir önceki komutunu görüp yeni komutunun
        # yanlış anlaşıldığını sanır.
        if state is OverlayState.LISTENING:
            self._heard = ""
        # Adım listesi yalnızca yeni tur (dinleme) ya da cevap (konuşma) gelince silinir.
        # Hata/onay durumu plan ortasında da gelebilir; adım listesi onda kalmalı.
        if state in (OverlayState.LISTENING, OverlayState.SPEAKING):
            self._steps = []
            self._step_states = []

        self._state = state
        if text:
            self._text = text

        if not self.isVisible():
            self._reveal()
        self.update()

    def _apply_text(self, text: str) -> None:
        self._text = text
        self.update()

    def _apply_heard(self, text: str) -> None:
        self._heard = text
        self.update()

    def _apply_amplitude(self, amplitude: float) -> None:
        self._target_amplitude = max(0.0, min(1.0, amplitude))

    def _apply_dismiss(self) -> None:
        # Açık bir onay varsa düğmeleri de kaldır; karar vermeden. Soruyu hâlâ sesle
        # dinleyen taraf zaman aşımında "hayır" sayar (güvenli taraf).
        self._clear_confirmation()

        if not self.isVisible():
            return

        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        try:
            self._fade.finished.disconnect()
        except TypeError:
            pass  # bağlı bir alıcı yoktu; sorun değil
        self._fade.finished.connect(self._on_fade_out_finished)
        self._fade.start()

    def _on_fade_out_finished(self) -> None:
        self._timer.stop()
        self.hide()
        self._amplitude = 0.0
        self._target_amplitude = 0.0

    def _apply_steps(self, labels: list[str]) -> None:
        self._steps = list(labels)
        self._step_states = ["pending"] * len(self._steps)
        self.update()

    def _apply_step_state(self, index: int, state: str) -> None:
        if not 0 <= index < len(self._step_states):
            return
        self._step_states[index] = state if state in _STEP_MARKS else "pending"
        self.update()

    def _apply_confirmation(self, summary: str, on_decision: Callable[[bool], None]) -> None:
        if not self._confirming:
            self._state_before_confirm = self._state  # onay bitince palet eski haline döner
        self._confirming = True
        self._confirm_summary = summary
        self._on_decision = on_decision
        self._state = OverlayState.ERROR  # onay bir uyarıdır: turuncu/kırmızı palet

        self._yes_button.show()
        self._no_button.show()
        if not self.isVisible():
            self._reveal()
        # Enter/Esc'in bu pencereye ulaşması için odak istenir. Windows, arka plandaki bir
        # süreçten gelen odağı bazen reddeder; o durumda tuşlar çalışmaz ama düğmeler çalışır.
        self.activateWindow()
        self.update()

    def _clear_confirmation(self) -> None:
        """Onay düğmelerini gizler ve palet eski haline döner. Karar VERMEZ."""

        if not self._confirming:
            return
        self._confirming = False
        self._on_decision = None
        self._confirm_summary = ""
        self._yes_button.hide()
        self._no_button.hide()
        if self._state is OverlayState.ERROR:
            self._state = self._state_before_confirm
        self.update()

    def _answer_confirmation(self, approved: bool) -> None:
        """Kullanıcının cevabını (tıklama ya da tuş) geri çağrıya iletir.

        Önce arayüz kapatılır, sonra cevap verilir: çift tıklama ikinci kez saymaz.
        """

        callback = self._on_decision
        if callback is None:
            return
        self._clear_confirmation()
        callback(approved)

    # ------------------------------------------------------------------
    # Pencere kurulumu ve konumlandırma
    # ------------------------------------------------------------------

    def _window_height(self) -> int:
        """Pencere boyu: panel (başlık + dalga + bölge + alt boşluk) ve parıltı payı."""

        return 2 * _GLOW_MARGIN + _ZONE_TOP + self._zone_height + _ZONE_BOTTOM_PAD

    def _configure_window(self) -> None:
        """Çerçevesiz, saydam, her zaman üstte bir araç penceresi kurar."""

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # görev çubuğunda ayrı bir pencere olarak görünmesin
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(_WINDOW_WIDTH, self._window_height())
        self.setWindowOpacity(0.0)

    def _build_confirm_buttons(self) -> None:
        """Evet/Hayır düğmelerini kurar (başta gizli).

        Düğmeler odak almaz (`NoFocus`): tuş olayları pencereye gider ve Enter/Esc
        `keyPressEvent`'te yakalanır. Tıklamalar yine düğmeye ulaşır.
        """

        self._no_button = QPushButton("Hayır", self)
        self._yes_button = QPushButton("Evet", self)
        for button, primary in ((self._no_button, False), (self._yes_button, True)):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(_button_style(primary))
            button.hide()

        self._yes_button.clicked.connect(lambda: self._answer_confirmation(True))
        self._no_button.clicked.connect(lambda: self._answer_confirmation(False))

        zone = self._zone_rect(self._panel_rect())
        row_width = 2 * _BUTTON_WIDTH + _BUTTON_GAP
        x = zone.x() + (zone.width() - row_width) // 2
        y = zone.bottom() + 1 - _BUTTON_HEIGHT
        self._no_button.setGeometry(x, y, _BUTTON_WIDTH, _BUTTON_HEIGHT)
        self._yes_button.setGeometry(x + _BUTTON_WIDTH + _BUTTON_GAP, y, _BUTTON_WIDTH, _BUTTON_HEIGHT)

    def _move_to_bottom_center(self) -> None:
        """Pencereyi, imlecin bulunduğu ekranın alt-ortasına yerleştirir.

        Birden fazla monitörde, kullanıcının o an baktığı ekranda açılması
        için imlecin bulunduğu ekran temel alınır.
        """

        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen is None:
            return

        area = screen.availableGeometry()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + area.height() - self.height() - _BOTTOM_OFFSET
        self.move(QPoint(x, y))

    def _reveal(self) -> None:
        """Pencereyi doğru ekrana taşıyıp fade-in ile gösterir."""

        self._move_to_bottom_center()
        self.show()
        self.raise_()

        self._fade.stop()
        try:
            self._fade.finished.disconnect()
        except TypeError:
            pass
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

        if not self._timer.isActive():
            self._timer.start()

    # ------------------------------------------------------------------
    # Animasyon
    # ------------------------------------------------------------------

    def _advance_animation(self) -> None:
        """Her karede fazı ilerletir ve genliği hedefe doğru yumuşatır."""

        self._phase += 0.16

        if self._state is OverlayState.THINKING:
            # "Düşünürken" mikrofon dinlenmiyor; kendi kendine nabız atsın.
            target = 0.35 + 0.25 * math.sin(self._phase * 0.9)
        else:
            target = self._target_amplitude

        # Yükselirken hızlı, düşerken yavaş: konuşma doğal görünür.
        rate = _AMPLITUDE_ATTACK if target > self._amplitude else _AMPLITUDE_RELEASE
        self._amplitude += (target - self._amplitude) * rate

        self.update()

    def _bar_heights(self) -> list[float]:
        """Her çubuğun o karedeki yüksekliğini (piksel) hesaplar."""

        heights: list[float] = []
        for i in range(_BAR_COUNT):
            # Kenarlara doğru hafifçe sönümlenen bir pencere. Üs küçük
            # tutulur (0.35): aksi halde kenar çubukları noktaya dönüşüp
            # dalga formu "ince kesik çizgi" gibi görünüyor.
            position = i / (_BAR_COUNT - 1)
            envelope = 0.45 + 0.55 * math.sin(position * math.pi) ** 0.35

            # Organik hareket için iki farklı frekansta sinüs + sabit gürültü.
            # Taban 0.70: çubuklar hiçbir zaman tamamen çökmez, dalga
            # sürekli "canlı" görünür.
            wobble = 0.70 + 0.30 * math.sin(self._phase + i * 0.34 + self._bar_noise[i])
            shimmer = 0.88 + 0.12 * math.sin(self._phase * 2.3 + i * 0.11)

            scale = self._amplitude * envelope * wobble * shimmer
            heights.append(_BAR_MIN_HEIGHT + scale * (_BAR_MAX_HEIGHT - _BAR_MIN_HEIGHT))
        return heights

    # ------------------------------------------------------------------
    # Çizim
    # ------------------------------------------------------------------

    def _panel_rect(self) -> QRect:
        """Ana panelin dikdörtgeni: pencere eksi parıltı payı."""

        return self.rect().adjusted(_GLOW_MARGIN, _GLOW_MARGIN, -_GLOW_MARGIN, -_GLOW_MARGIN)

    def _zone_rect(self, panel: QRect) -> QRect:
        """Adım listesi / onay / cevap metninin çizileceği alt bölge."""

        return QRect(
            panel.x() + _ZONE_SIDE_PAD,
            panel.y() + _ZONE_TOP,
            panel.width() - 2 * _ZONE_SIDE_PAD,
            self._zone_height,
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt'nin metot adı
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        panel = self._panel_rect()

        self._paint_glow(painter, panel)
        self._paint_panel(painter, panel)
        self._paint_title(painter, panel)
        self._paint_heard(painter, panel)
        self._paint_waveform(painter, panel)
        self._paint_zone(painter, self._zone_rect(panel))

        painter.end()

    def _paint_glow(self, painter: QPainter, panel) -> None:
        """Panelin dışına, duruma göre renklenen yumuşak bir parıltı çizer.

        Qt'nin `QGraphicsDropShadowEffect`'i saydam üst-seviye pencerelerde
        güvenilir çalışmadığı için parıltı elle, giderek saydamlaşan iç içe
        yuvarlatılmış dikdörtgenlerle üretilir.
        """

        _, mid, _ = _PALETTES[self._state]
        intensity = 0.55 + 0.45 * self._amplitude

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step in range(_GLOW_MARGIN, 0, -2):
            # Merkeze yaklaştıkça hızla yoğunlaşan bir düşüş (kare alınarak),
            # panelin hemen dibinde belirgin, dışa doğru yumuşak bir hale verir.
            falloff = (1.0 - step / _GLOW_MARGIN) ** 2
            alpha = int(intensity * 120 * falloff)
            if alpha <= 0:
                continue
            color = QColor(mid.red(), mid.green(), mid.blue(), alpha)
            painter.setPen(QPen(color, 2.5))
            painter.drawRoundedRect(
                panel.adjusted(-step, -step, step, step),
                _CORNER_RADIUS + step * 0.6,
                _CORNER_RADIUS + step * 0.6,
            )

    def _paint_panel(self, painter: QPainter, panel) -> None:
        """Koyu, hafif degradeli ve ince kenarlıklı ana paneli çizer."""

        path = QPainterPath()
        path.addRoundedRect(float(panel.x()), float(panel.y()), float(panel.width()), float(panel.height()), _CORNER_RADIUS, _CORNER_RADIUS)

        background = QLinearGradient(panel.topLeft().toPointF(), panel.bottomRight().toPointF())
        # Panel 238 alfa ile çizilir: arkasında bazen masaüstü görünür
        # (pencere yarı saydam) ve tam opak bir zemin kötü görünürdü.
        # `theme`'deki renkler opak olduğu için KOPYALANIP alfası ayarlanır;
        # modül sabitinin kendisi ASLA değiştirilmez.
        panel_top = QColor(theme.BG_PANEL)
        panel_top.setAlpha(238)
        panel_bottom = QColor(theme.BG_BASE)
        panel_bottom.setAlpha(238)
        background.setColorAt(0.0, panel_top)
        background.setColorAt(1.0, panel_bottom)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(path, background)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(theme.BORDER, 1.0))
        painter.drawPath(path)

    def _paint_title(self, painter: QPainter, panel) -> None:
        """Üstteki "ARTEMIS" başlığını, harf aralıklı ve soluk çizer."""

        font = QFont(theme.FONT_FAMILY, 10, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4.0)
        painter.setFont(font)
        # "ARTEMIS" bilinçli olarak soluk: bir dekoratif başlık, dikkat
        # çekmemeli. `TEXT_SECONDARY` (150) biraz daha belirgin olduğu için
        # alfa burada 120'ye çekiliyor — `theme`'ye yalnızca tek bir çağrı
        # yüzünden yeni bir alfa varyantı eklenmez.
        title_color = QColor(theme.TEXT_SECONDARY)
        title_color.setAlpha(120)
        painter.setPen(title_color)

        rect = panel.adjusted(0, 20, 0, 0)
        rect.setHeight(20)
        painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._title)

    def _paint_waveform(self, painter: QPainter, panel) -> None:
        """Duruma göre renklenen, sese tepki veren dalga formunu çizer."""

        left, mid, right = _PALETTES[self._state]

        total_width = _BAR_COUNT * _BAR_WIDTH + (_BAR_COUNT - 1) * _BAR_GAP
        start_x = panel.x() + (panel.width() - total_width) / 2.0
        center_y = panel.y() + _WAVE_CENTER_Y

        gradient = QLinearGradient(start_x, 0.0, start_x + total_width, 0.0)
        gradient.setColorAt(0.0, left)
        gradient.setColorAt(0.5, mid)
        gradient.setColorAt(1.0, right)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)

        for i, height in enumerate(self._bar_heights()):
            x = start_x + i * (_BAR_WIDTH + _BAR_GAP)
            painter.drawRoundedRect(
                int(x),
                int(center_y - height / 2.0),
                _BAR_WIDTH,
                int(height),
                _BAR_WIDTH / 2.0,
                _BAR_WIDTH / 2.0,
            )

    def _paint_heard(self, painter: QPainter, panel) -> None:
        """Kullanıcının söylediği anlaşılan metni başlığın hemen altına yazar.

        Neden ayrı bir satır: alt satır Artemis'in DURUMUNU/cevabını
        gösterir ve cevap gelince değişir. Kullanıcının ne dediği ise
        pencere kapanana kadar görünür kalmalı — asistanın yanlış
        anladığı ancak böyle fark edilir ("league of legends aç" ->
        "league of legends such" gibi).
        """

        if not self._heard:
            return

        painter.setFont(QFont(theme.FONT_FAMILY, 11))
        painter.setPen(theme.TEXT_SECONDARY)

        rect = panel.adjusted(24, 44, -24, 0)
        rect.setHeight(22)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(f"“{self._heard}”", Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)

    def _paint_zone(self, painter: QPainter, zone: QRect) -> None:
        """Alt bölgede o an geçerli içeriği seçer: onay > adım listesi > cevap metni."""

        if self._confirming:
            self._paint_confirmation(painter, zone)
        elif self._steps:
            self._paint_steps(painter, zone)
        else:
            self._paint_text(painter, zone)

    def _paint_text(self, painter: QPainter, zone: QRect) -> None:
        """Cevap / durum metnini bölgeye sarıp çizer (en çok `_TEXT_MAX_LINES` satır).

        Metin alt hizalıdır: kısa bir durum ("Dinliyorum…") bölgenin altına oturur,
        uzun bir cevap yukarı doğru dolar.
        """

        if not self._text:
            return

        font = QFont(theme.FONT_FAMILY, _TEXT_FONT_SIZE)
        painter.setFont(font)
        # Cevap metni panelin en okunması gereken satırı; `TEXT_PRIMARY`
        # (235) burada fazla sert düşüyordu. 205, arada bir yerde durur.
        text_color = QColor(theme.TEXT_PRIMARY)
        text_color.setAlpha(205)
        painter.setPen(text_color)

        lines = _wrap_text(self._text, QFontMetrics(font), zone.width(), _TEXT_MAX_LINES)
        top = zone.bottom() + 1 - len(lines) * self._line_height
        for row, line in enumerate(lines):
            rect = QRect(zone.x(), top + row * self._line_height, zone.width(), self._line_height)
            painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, line)

    def _paint_steps(self, painter: QPainter, zone: QRect) -> None:
        """Plan adımlarını durum işaretleriyle, ortalanmış bir blok olarak çizer."""

        font = QFont(theme.FONT_FAMILY, _LIST_FONT_SIZE)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        start = _visible_step_start(self._step_states, _TEXT_MAX_LINES)
        rows = range(start, min(len(self._steps), start + _TEXT_MAX_LINES))
        mark_width = max(metrics.horizontalAdvance(mark) for mark in _STEP_MARKS.values())
        label_limit = zone.width() - mark_width - _STEP_MARK_GAP
        labels = {
            index: metrics.elidedText(self._steps[index], Qt.TextElideMode.ElideRight, label_limit)
            for index in rows
        }
        block_width = mark_width + _STEP_MARK_GAP + max(
            (metrics.horizontalAdvance(label) for label in labels.values()), default=0
        )
        x = zone.x() + (zone.width() - block_width) // 2

        for row, index in enumerate(rows):
            state = self._step_states[index]
            y = zone.y() + row * self._line_height
            painter.setPen(_STEP_MARK_COLORS[state])
            painter.drawText(
                QRect(x, y, mark_width, self._line_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _STEP_MARKS[state],
            )
            painter.setPen(_STEP_LABEL_COLORS[state])
            painter.drawText(
                QRect(x + mark_width + _STEP_MARK_GAP, y, block_width - mark_width - _STEP_MARK_GAP, self._line_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                labels[index],
            )

    def _paint_confirmation(self, painter: QPainter, zone: QRect) -> None:
        """Onaylanacak işlemin kısa metnini bölgenin üstüne yazar; düğmeler alta gelir.

        Düğmeler ayrı çocuk pencerelerdir (`_build_confirm_buttons`); burada yalnızca
        neyin onaylandığı çizilir. Metin tek satırdır ve sığmazsa kesilir: tam
        argümanlar kayıtta (log) ve sesli soruda zaten vardır.
        """

        font = QFont(theme.FONT_FAMILY, _LIST_FONT_SIZE)
        painter.setFont(font)
        color = QColor(theme.TEXT_PRIMARY)
        color.setAlpha(205)
        painter.setPen(color)
        summary = QFontMetrics(font).elidedText(self._confirm_summary, Qt.TextElideMode.ElideRight, zone.width())
        painter.drawText(
            QRect(zone.x(), zone.y(), zone.width(), self._line_height),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            summary,
        )

    # ------------------------------------------------------------------
    # Kullanıcı etkileşimi
    # ------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt'nin metot adı
        """Onay açıkken Enter = Evet, Esc = Hayır; değilken Esc pencereyi kapatır."""

        key = event.key()
        if self._confirming:
            if key in _ENTER_KEYS:
                self._answer_confirmation(True)
                return
            if key == Qt.Key.Key_Escape:
                self._answer_confirmation(False)
                return
        elif key == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt'nin metot adı
        """Pencereye tıklanınca kapatır (Siri'de olduğu gibi).

        Onay sırasında kapatma YAPILMAZ: boşluğa tıklamak soruyu yarım bırakıp
        kullanıcıyı bir sonraki tıklamaya kadar şaşırtmasın. Cevap yalnızca
        düğmelerle (ya da Enter/Esc ile) verilir.
        """

        if self._confirming:
            return
        self.dismiss()


def _run_demo() -> None:
    """`python -m ui.overlay`: ses katmanı olmadan pencereyi canlı gösterir.

    Sahte bir ses seviyesi üreterek dinleme → düşünme → konuşma
    durumlarını sırayla dolaşır; böylece arayüz, mikrofon/LLM/TTS
    kurulmadan da gözle kontrol edilebilir.
    """

    app = QApplication(sys.argv)
    overlay = ArtemisOverlay()
    overlay.show_listening("Dinliyorum…")

    # Sahte mikrofon seviyesi: konuşuyormuş gibi dalgalanan bir değer.
    noise = QTimer()
    noise.setInterval(60)
    noise.timeout.connect(lambda: overlay.set_amplitude(random.uniform(0.15, 0.95)))
    noise.start()

    def to_thinking() -> None:
        noise.stop()
        overlay.show_thinking("Düşünüyorum…")

    def to_speaking() -> None:
        overlay.show_speaking("Masaüstünde 'Orbit' klasörünü oluşturdum.")
        noise.start()

    def to_error() -> None:
        noise.stop()
        overlay.set_amplitude(0.25)
        overlay.show_error("Yerel modele ulaşılamıyor.")

    QTimer.singleShot(3500, to_thinking)
    QTimer.singleShot(6500, to_speaking)
    QTimer.singleShot(10500, to_error)
    QTimer.singleShot(13500, overlay.dismiss)
    QTimer.singleShot(14500, app.quit)

    print("Artemis arayüz önizlemesi: dinleme → düşünme → konuşma → hata → kapanış")
    print("(Esc veya tıklama ile de kapatabilirsiniz.)")
    sys.exit(app.exec())


if __name__ == "__main__":
    _run_demo()
