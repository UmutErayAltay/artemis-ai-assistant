"""Artemis'in tüm görsel katmanlarında (overlay, sohbet penceresi, ayarlar
penceresi, tepsi simgesi) PAYLAŞILAN renk paleti ve tipografi.

NEDEN AYRI BİR MODÜL: `ui/overlay.py` zaten kendi paletini (`_PALETTES`)
tanımlıyordu; yeni pencereler eklenince her biri kendi renklerini
icat etseydi arayüz parça parça, "kim yaptıysa öyle" görünürdü — tam da
"AI yapmış gibi" hissin sebebi budur (her ekran ayrı bir varsayılan
mavi/mor gradyan seçer, tutarlı bir kimlik yok). Bu modül TEK bir kaynak:
her pencere aynı 6 rengi, aynı fontu kullanır.

PALET SEÇİMİ — bilinçli, rastgele değil: `ui/overlay.py`'nin zaten
kullandığı mavi→mor→pembe degrade Apple'ın Sistem Renkleri'ne (systemBlue
10,132,255 / systemPurple 191,90,242 / systemPink 255,55,95 / systemTeal
100,210,255 / systemOrange 255,159,10 / systemRed 255,69,58) birebir
karşılık gelir. Bu paleti YENİDEN İCAT ETMEK YERİNE genişletmek tercih
edildi: zaten kısıtlı (6 renk), zaten test edilmiş (overlay'de gözle
onaylanmış), ve genelleşmiş "mor-mavi AI gradyanı" klişesinden farklı
olarak GERÇEK bir tasarım sisteminden (Apple HIG) geliyor — her ekranda
farklı, rastgele seçilmiş bir gradyan yerine sabit bir kimlik.

Koyu tema tercih edildi (varsayılan Qt'nin açık/gri paneli değil):
overlay zaten koyu, sesli asistanın kimliği koyu — sohbet ve ayarlar
pencereleri farklı bir temada açılsaydı aynı uygulamanın parçası gibi
hissettirmezdi.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor

# --- Zemin ----------------------------------------------------------------
BG_BASE = QColor(18, 18, 24)  # en dış zemin (overlay panelinin alt durağı)
BG_PANEL = QColor(30, 30, 38)  # panel/pencere gövdesi (overlay'in üst durağı)
BG_ELEVATED = QColor(40, 40, 50)  # giriş kutusu, kart, asistan balonu
BG_ELEVATED_HOVER = QColor(50, 50, 61)

# --- Kenarlık / ayırıcı -----------------------------------------------------
BORDER = QColor(255, 255, 255, 26)
BORDER_STRONG = QColor(255, 255, 255, 46)

# --- Metin ------------------------------------------------------------------
TEXT_PRIMARY = QColor(235, 235, 245, 235)
TEXT_SECONDARY = QColor(255, 255, 255, 150)
TEXT_MUTED = QColor(255, 255, 255, 90)

# --- Vurgu (Apple Sistem Renkleri) ------------------------------------------
ACCENT_BLUE = QColor(10, 132, 255)  # systemBlue — dinleme/birincil vurgu
ACCENT_PURPLE = QColor(191, 90, 242)  # systemPurple — düşünme, orta durak
ACCENT_PINK = QColor(255, 55, 95)  # systemPink — dinleme degradesinin sonu
ACCENT_TEAL = QColor(100, 210, 255)  # systemTeal — konuşma
ACCENT_GREEN = QColor(48, 209, 88)  # systemGreen — başarı/bağlı durumu
ACCENT_ORANGE = QColor(255, 159, 10)  # systemOrange — uyarı
ACCENT_RED = QColor(255, 69, 58)  # systemRed — hata

FONT_FAMILY = "Segoe UI"
"""Windows'un kendi arayüz fontu — harici bir web fontu (örn. Inter) taşımak
yerine işletim sistemiyle zaten tutarlı, Windows'ta "yabancı" durmaz."""


def _rgba(color: QColor, alpha: int | None = None) -> str:
    """Bir `QColor`'ı QSS/CSS `rgba(...)` dizesine çevirir.

    Args:
        color: Kaynak renk.
        alpha: Verilirse rengin kendi alfa kanalını GEÇERSİZ kılar
            (0-255). QSS'te bazı özellikler (örn. `border-color`) alfa
            kanalını farklı yorumlayabildiği için bazen renk sabit
            tutulup yalnızca saydamlık değiştirilmek istenir.
    """

    a = color.alpha() if alpha is None else alpha
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {a / 255:.3f})"


def stylesheet() -> str:
    """Standart QWidget tabanlı pencereler (sohbet, ayarlar) için ortak QSS.

    `ui/overlay.py` bunu KULLANMAZ — o kendi `QPainter` ile çizilen özel bir
    yüzey (çerçevesiz/saydam pencere), Qt'nin standart widget'ları değil.
    Bu stil sayfası, standart `QWidget`/`QLineEdit`/`QPushButton`/
    `QListWidget`/`QScrollArea` kullanan pencereler için varsayılan gri
    Qt görünümünü bu modüldeki paletle değiştirir.
    """

    return f"""
    QWidget {{
        background-color: {_rgba(BG_BASE)};
        color: {_rgba(TEXT_PRIMARY)};
        font-family: "{FONT_FAMILY}";
        font-size: 13px;
    }}
    QLabel[role="title"] {{
        font-size: 15px;
        font-weight: 600;
        color: {_rgba(TEXT_PRIMARY)};
    }}
    QLabel[role="subtitle"] {{
        color: {_rgba(TEXT_SECONDARY)};
    }}
    QLabel[role="hint"] {{
        color: {_rgba(TEXT_MUTED)};
        font-size: 11px;
    }}
    QScrollArea, QAbstractScrollArea {{
        background: transparent;
        border: none;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {_rgba(BORDER_STRONG)};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background-color: {_rgba(BG_ELEVATED)};
        border: 1px solid {_rgba(BORDER)};
        border-radius: 8px;
        padding: 7px 10px;
        selection-background-color: {_rgba(ACCENT_BLUE, 140)};
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
        border: 1px solid {_rgba(ACCENT_BLUE, 200)};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QPushButton {{
        background-color: {_rgba(BG_ELEVATED)};
        border: 1px solid {_rgba(BORDER)};
        border-radius: 8px;
        padding: 7px 16px;
        color: {_rgba(TEXT_PRIMARY)};
    }}
    QPushButton:hover {{
        background-color: {_rgba(BG_ELEVATED_HOVER)};
    }}
    QPushButton:pressed {{
        background-color: {_rgba(BG_PANEL)};
    }}
    QPushButton[role="primary"] {{
        background-color: {_rgba(ACCENT_BLUE)};
        border: none;
        color: white;
        font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{
        background-color: {_rgba(ACCENT_BLUE.lighter(112))};
    }}
    QPushButton:disabled {{
        color: {_rgba(TEXT_MUTED)};
    }}
    QCheckBox {{
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {_rgba(BORDER_STRONG)};
        border-radius: 4px;
        background: {_rgba(BG_ELEVATED)};
    }}
    QCheckBox::indicator:checked {{
        background: {_rgba(ACCENT_BLUE)};
        border-color: {_rgba(ACCENT_BLUE)};
    }}
    QListWidget {{
        background: transparent;
        border: none;
    }}
    QTabWidget::pane {{
        border: 1px solid {_rgba(BORDER)};
        border-radius: 8px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {_rgba(TEXT_SECONDARY)};
        border: 1px solid transparent;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        padding: 7px 18px;
    }}
    QTabBar::tab:selected {{
        background: {_rgba(BG_PANEL)};
        color: {_rgba(TEXT_PRIMARY)};
        border-color: {_rgba(BORDER)};
    }}
    QTabBar::tab:hover:!selected {{
        color: {_rgba(TEXT_PRIMARY)};
    }}
    QToolTip {{
        background-color: {_rgba(BG_ELEVATED)};
        color: {_rgba(TEXT_PRIMARY)};
        border: 1px solid {_rgba(BORDER)};
        padding: 4px 8px;
    }}
    """
