"""Sistem tepsisi (system tray) simgesi.

Sesli asistan arka planda, penceresiz çalışır: overlay yalnızca Artemis
çağrıldığında görünür. Bu yüzden kullanıcının uygulamayı görebileceği ve
kapatabileceği tek yer tepsi simgesidir — onsuz asistanı durdurmanın
yolu Görev Yöneticisi'nden süreci sonlandırmak olurdu.

Simge, dosyadan yüklenmek yerine kod içinde çizilir: böylece depoya ikili
(binary) bir varlık dosyası eklemek ve yolunu yönetmek gerekmez.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)

_ICON_SIZE = 64


def build_icon() -> QIcon:
    """Artemis'in tepsi simgesini çizer: mavi-mor degradeli bir daire."""

    pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    gradient = QLinearGradient(QPointF(0, 0), QPointF(_ICON_SIZE, _ICON_SIZE))
    gradient.setColorAt(0.0, QColor(10, 132, 255))
    gradient.setColorAt(1.0, QColor(191, 90, 242))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawEllipse(QRectF(4, 4, _ICON_SIZE - 8, _ICON_SIZE - 8))

    # Ortada, dalga formunu çağrıştıran üç dikey çubuk.
    painter.setBrush(QColor(255, 255, 255, 235))
    for index, height in enumerate((16, 28, 20)):
        x = _ICON_SIZE / 2 - 11 + index * 9
        painter.drawRoundedRect(QRectF(x, (_ICON_SIZE - height) / 2, 5, height), 2.5, 2.5)

    painter.end()
    return QIcon(pixmap)


class ArtemisTray(QSystemTrayIcon):
    """Tepsi simgesi ve sağ tık menüsü.

    Args:
        on_listen: "Şimdi dinle" seçildiğinde çağrılır (elle uyandırma).
        on_quit: "Çıkış" seçildiğinde çağrılır.
        hotkey_text: Menüde bilgi olarak gösterilecek kısayol metni.
        on_settings: "Ayarlar" seçildiğinde çağrılır. `None` (varsayılan)
            ise menü öğesi HİÇ EKLENMEZ — yani bugünkü menü birebir aynı
            kalır. Bu, ayar penceresinin eklenmesini bir "herkes için
            zorunlu bağımlılık" olmaktan çıkarır: `main.py`'yi henüz
            değiştirmemiş bir çağrı yer de durur.
        on_panel: "Panel (geçmiş ve ayarlar)" seçildiğinde çağrılır
            (`ui/panel.py::show_panel`). Aynı seçime bağlı mantık:
            verilmezse menü öğesi eklenmez.
    """

    def __init__(
        self,
        on_listen: Callable[[], None],
        on_quit: Callable[[], None],
        hotkey_text: str = "",
        on_settings: Callable[[], None] | None = None,
        on_panel: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(build_icon())

        self._on_listen = on_listen
        self.setToolTip("Artemis — sesli asistan çalışıyor")

        menu = QMenu()

        listen_action = QAction(f"Şimdi dinle{f'  ({hotkey_text})' if hotkey_text else ''}", menu)
        listen_action.triggered.connect(lambda: on_listen())
        menu.addAction(listen_action)

        if on_panel is not None:
            panel_action = QAction("Panel (geçmiş ve ayarlar)", menu)
            panel_action.triggered.connect(lambda: on_panel())
            menu.addAction(panel_action)

        if on_settings is not None:
            settings_action = QAction("Ayarlar", menu)
            settings_action.triggered.connect(lambda: on_settings())
            menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("Çıkış", menu)
        quit_action.triggered.connect(lambda: on_quit())
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self._menu = menu  # menü çöpe atılmasın diye referans tutulur

        self.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Simgeye çift tıklanınca dinlemeyi başlatır."""

        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_listen()
