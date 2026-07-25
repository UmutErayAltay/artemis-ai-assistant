"""Siri benzeri, çerçevesiz ve yarı saydam Artemis penceresi.

Bu modül YALNIZCA görüntüden sorumludur. Ses tanıma, LLM veya tool
çalıştırma mantığı burada yaşamaz; pencere dışarıdan basit metotlarla
(`show_listening()`, `set_amplitude()`, `show_thinking()`, ...) sürülür.
Böylece arayüz tamamen değiştirilse bile `core/` ve `voice/`
katmanlarında hiçbir değişiklik gerekmez.

İŞ PARÇACIĞI (THREAD) NOTU — önemli:
    Qt'de arayüz nesnelerine YALNIZCA ana (GUI) iş parçacığından
    dokunulabilir. Wake-word ve ses tanıma katmanları ise ayrı bir iş
    parçacığında çalışır. Bu yüzden buradaki tüm public metotlar,
    doğrudan çizim yapmak yerine bir Qt SİNYALİ yayınlar; sinyal Qt
    tarafından otomatik olarak GUI iş parçacığına kuyruklanır
    (`QueuedConnection`). Yani bu sınıfın public metotları HERHANGİ bir
    iş parçacığından güvenle çağrılabilir.

Tek başına önizleme (ses katmanı olmadan):

    python -m ui.overlay
"""

from __future__ import annotations

import math
import random
import sys
from enum import Enum, auto

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QWidget

# --- Pencere ölçüleri ---------------------------------------------------
_WINDOW_WIDTH = 620
_WINDOW_HEIGHT = 260
_GLOW_MARGIN = 34  # panelin dışında, parıltı (glow) için ayrılan boşluk
_CORNER_RADIUS = 30
_BOTTOM_OFFSET = 90  # ekranın altından yukarı boşluk (görev çubuğu payı)

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


class OverlayState(Enum):
    """Pencerenin görsel durumu.

    Her durum farklı bir renk paleti ve farklı bir dalga formu davranışı
    ifade eder (bkz. `_PALETTES` ve `_advance_animation`).
    """

    LISTENING = auto()  # mikrofon dinliyor; dalga formu gerçek sese tepki verir
    THINKING = auto()  # LLM düşünüyor; dalga formu kendi kendine nabız atar
    SPEAKING = auto()  # TTS konuşuyor; dalga formu konuşmaya tepki verir
    ERROR = auto()  # bir hata oluştu; kırmızı/turuncu palet


# Her durum için (sol, orta, sağ) degrade renkleri.
_PALETTES: dict[OverlayState, tuple[QColor, QColor, QColor]] = {
    OverlayState.LISTENING: (QColor(10, 132, 255), QColor(191, 90, 242), QColor(255, 55, 95)),
    OverlayState.THINKING: (QColor(120, 92, 255), QColor(191, 90, 242), QColor(120, 92, 255)),
    OverlayState.SPEAKING: (QColor(100, 210, 255), QColor(10, 132, 255), QColor(100, 210, 255)),
    OverlayState.ERROR: (QColor(255, 159, 10), QColor(255, 69, 58), QColor(255, 159, 10)),
}


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

        self._configure_window()

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

    # ------------------------------------------------------------------
    # Pencere kurulumu ve konumlandırma
    # ------------------------------------------------------------------

    def _configure_window(self) -> None:
        """Çerçevesiz, saydam, her zaman üstte bir araç penceresi kurar."""

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # görev çubuğunda ayrı bir pencere olarak görünmesin
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(_WINDOW_WIDTH, _WINDOW_HEIGHT)
        self.setWindowOpacity(0.0)

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

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt'nin metot adı
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        panel = self.rect().adjusted(_GLOW_MARGIN, _GLOW_MARGIN, -_GLOW_MARGIN, -_GLOW_MARGIN)

        self._paint_glow(painter, panel)
        self._paint_panel(painter, panel)
        self._paint_title(painter, panel)
        self._paint_heard(painter, panel)
        self._paint_waveform(painter, panel)
        self._paint_text(painter, panel)

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
        background.setColorAt(0.0, QColor(30, 30, 38, 238))
        background.setColorAt(1.0, QColor(18, 18, 24, 238))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(path, background)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
        painter.drawPath(path)

    def _paint_title(self, painter: QPainter, panel) -> None:
        """Üstteki "ARTEMIS" başlığını, harf aralıklı ve soluk çizer."""

        font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4.0)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 120))

        rect = panel.adjusted(0, 20, 0, 0)
        rect.setHeight(20)
        painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._title)

    def _paint_waveform(self, painter: QPainter, panel) -> None:
        """Duruma göre renklenen, sese tepki veren dalga formunu çizer."""

        left, mid, right = _PALETTES[self._state]

        total_width = _BAR_COUNT * _BAR_WIDTH + (_BAR_COUNT - 1) * _BAR_GAP
        start_x = panel.x() + (panel.width() - total_width) / 2.0
        center_y = panel.y() + panel.height() / 2.0 + 2

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

        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor(255, 255, 255, 150))

        rect = panel.adjusted(24, 44, -24, 0)
        rect.setHeight(22)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(f"“{self._heard}”", Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)

    def _paint_text(self, painter: QPainter, panel) -> None:
        """Alt satırdaki durum/döküm metnini çizer (uzunsa kısaltılır)."""

        if not self._text:
            return

        painter.setFont(QFont("Segoe UI", 12))
        painter.setPen(QColor(235, 235, 245, 205))

        rect = panel.adjusted(28, 0, -28, -22)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(self._text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, elided)

    # ------------------------------------------------------------------
    # Kullanıcı etkileşimi
    # ------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt'nin metot adı
        """Esc ile pencereyi kapatır."""

        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt'nin metot adı
        """Pencereye tıklanınca kapatır (Siri'de olduğu gibi)."""

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
