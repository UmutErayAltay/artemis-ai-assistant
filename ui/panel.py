"""Artemis paneli — geçmiş, ayarlar ve durum tek pencerede.

NEDEN AYRI BİR DOSYA: `ui/chat_window.py` bir SOHBET arayüzüdür (mesaj
kutusu + Gönder) ve `tests/test_ui_chat_window.py` ona 34 testle bağlıdır.
Panel yazma/yollama yüzü olmayan bir görüntüleme aracı olduğu için aynı
pencereye sıkıştırılmamalı, ayrı durmalıdır. `main.py`'nin `--chat-gui`
noktasının `ChatWindow` yerine bu modülü göstermesi gerekir (bkz. modül
sonundaki `_run_demo`).

GÖRÜNTÜ DİLİ: renkler, font ve QSS kuralları `ui/theme.py`'den gelir;
burada yeni bir palet icat edilmez. Sekme çubuğunun kuralları da ortak
`theme.stylesheet()` içine eklendi (üçüncü bir pencere daha aynı QSS'i
kullanıyor).

GEÇMİŞ NEREDEN GELİYOR — AÇIKÇA:
    Bu depoda KALICI BİR KONUŞMA KAYDI YOKTUR. `memory/artemis_memory.db`
    yalnızca `context_memory` anahtar-değer tablosunu tutar (`last_path`,
    `fact:*`); sohbet satırları orada hiçbir yerde saklanmaz.
    `core/voice_loop.py` her turu `logger.info` ile `logs/artemis.log`'a
    yazar; dosya 1 MB'a kadar dönerek büyür. Dolayısıyla panelin okuduğu
    TEK gerçek kaynak bu log dosyasıdır ve `parse_history` yalnızca
    oradaki üç satır biçimini tanır. Log YOKSA veya içinde konuşma
    kaydı YOKSA panel bunu uydurmaz; "henüz kayıt yok" durumunu gösterir.

    Bilinen körlükler (raporda da yazılı): `Cevap (tam)` satırı yalnızca
    sesli okuma metni kısaltıldığında yazılır, yani kısa cevaplar logda
    bulunmaz; ayrıca konuşmadan önce yazılan `Cevap (tam)` satırları da
    bir sonraki konuşmanın cevabı sanılabilir. Panel ne yazıyorsa logdan
    geliyordur — eksik olan kayıt, panelin işi değil kaynağın işidir.

GÖRÜNTÜ DÜZENİ (tasarım kararları ve ÖLÇÜLEN gerekçeler):
    * Turlar `BG_PANEL` zeminli kartlarla ayrılır; zaman damgası 12 px
      `TEXT_SECONDARY` (kart zemini üzerinde 8.26:1 — WCAG AA). Daha önce
      11 px `TEXT_MUTED` idi (4.09:1) ve zemine karışıyordu.
    * Durum şeridi iki satırlı bir ızgaradır: renkli nokta + başlık +
      kısa değer, altında ince ayrıntı satırı. `python main.py --voice`
      yolu EKRANDA değil, tooltip'tedir; ekranda olsaydı panelin minimum
      genişliği 823'e çıkardı.
    * Sarmalı her etiket `QSizePolicy.Ignored` taşır: Qt, sarmalı bir
      etiketin boyut ipucunu TEK SATIR genişliğine göre hesaplar ve bu,
      pencereyi içerikten geniş olmayan bir zemine zorlar. Metin yine
      verilen genişlikte sarılır.
    * Renk körlüğü ölçümü `_contrast_ratio` ile yapılır (gözle değil);
    * `test_every_body_text_color_clears_wcag_aa` bunu geriye dönük
      korur.
"""

from __future__ import annotations

import html
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config.settings import Settings
from ui import theme
from ui.settings_window import SettingsWindow

logger = logging.getLogger(__name__)

_WINDOW_WIDTH = 760
_WINDOW_HEIGHT = 700
_MIN_WIDTH = 560
"""Pencerenin taban genişliği.

Önceden panelin `minimumSizeHint`'i 823'e çıkıyordu ve pencere o değere
GERİ SÜRÜKLENİyordu: durum şeridindeki `python main.py --voice` cümlesi
tek satıra sığmayıp metin motorunun sarma hesabını şişiriyordu. O cümle
artık tooltip'te (bkz. `_VOICE_START_HINT`) ve tüm sarmalı etiketler
boyut ipucuna katılmıyor, dolayısıyla 560 gerçek bir alt sınırdır:
geçmiş kartları ve ayar formu bu genişlikte okunabilir kalır."""
_HISTORY_LIMIT = 50
"""Kaç tur gösterileceği. Log 1 MB'a kadar büyüyebiliyor; geçmişin
tamamını basmak yüzlerce satırlık bir liste ve anlamsız bir kaydırma
demektir. En YENİ `HISTORY_LIMIT` tur gösterilir."""

# --- Log satırı biçimleri -------------------------------------------------
#
# `utils/logger.py::setup_logging` her satırı şu biçimde yazar:
#   `2026-07-26 03:13:04,740 | INFO     | core.voice_loop | Duyulan komut: '...'`
# Altındaki üç desen o satırdan çıkarılan bilgi türleridir.

_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| \w+ +\| (?P<logger>[\w.]+) \| (?P<message>.*)$"
)
_HEARD = re.compile(r"^Duyulan komut: (?P<text>.*)$")
_TOOL = re.compile(r"^Tool çalıştırıldı: (?P<tool>\S+) -> success=(?P<success>\S+)$")
_REPLY = re.compile(r"^Cevap \(tam\): (?P<text>.*)$")

_PROVIDER_LABELS: dict[str, str] = {
    "OllamaLLMClient": "Yerel (Ollama)",
    "OpenRouterLLMClient": "Bulut (OpenRouter)",
    "LLMRouter": "Otomatik (bulut, gerekirse yerel)",
}
"""Çalışan istemci sınıfı -> ekranda görünecek ad.

NEDEN SAYIYA DEĞİL, `isinstance` KONTROLÜNE: burada `core.llm_client`
`ollama-python`'u, `core.openrouter_client` ise `httpx`'i import zincirine
katar. Panel o istemci olmadan da (yalnızca ayarlarla) açılabilmeli; bu
yüzden burada hiçbir LLM modülü import EDİLMEZ, sadece adı okunur. Üçüncü
bir sağlayıcı eklenirse tanınmayan ad olduğu gibi yazılır — uydurulmaz."""

_PROVIDER_MODE_LABELS = {
    "auto": "Otomatik — her çağrıda bulut, gerekirse yerel",
    "cloud": "Bulut (OpenRouter)",
    "local": "Yerel (Ollama)",
}


# --------------------------------------------------------------------------
# Geçmişi logdan okumak
# --------------------------------------------------------------------------


@dataclass
class HistoryTurn:
    """Log'dan çıkarılmış TEK konuşma turu.

    Attributes:
        timestamp: Turun başlangıcı (`Duyulan komut` satırının zamanı),
            ekranda gösterilecek biçimde.
        said: Kullanıcının söylediği metin. Boşsa ses anlaşılmamıştır.
        reply: Artemis'in verdiği yanıt (yalnızca logda varsa).
        tools: `(tool adı, başarılı mı)` çiftleri, logdaki çalıştırma
            sırasıyla.
    """

    timestamp: str
    said: str = ""
    reply: str = ""
    tools: list[tuple[str, bool]] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Ekranda gösterilecek hiçbir şeyi olmayan tur (ör. boş STT)."""

        return not (self.said or self.reply or self.tools)


def _unquote(text: str) -> str:
    """`%r` ile yazılmış tek tırnaklı log metnini düz metne çevirir.

    `voice_loop` komutu `logger.info("Duyulan komut: %r", transcript)` ile
    yazar, yani ham Python temsilini (`'merhaba'`, `''`) alırız. Tırnak
    YOKSA (beklenmedik bir biçim) metne dokunulmaz — kaybetmektense
    tırnaklı göstermek iyidir.
    """

    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_history(path: Path, limit: int = _HISTORY_LIMIT) -> list[HistoryTurn]:
    """Log dosyasındaki konuşma turlarını kronolojik sırayla döndürür.

    NEDEN İLERİ TARAMA: bir tur, `Duyulan komut` satırıyla BAŞLAR ve
    cevabı/araçları ONDAN SONRA gelen satırlardır. Yani satırı okuyup
    turu kapatmak ileri yönde doğal; ters yönde tarama, "bu tur ne
    söyledi" bilgisini en sonda bulup geriye doğru yamamak zorunda kalır
    — ve kaydı henüz tamamlanmamış son turda o bilgi yoktur (bugün
    logu ters tarayan ilk denemede, `Duyulan komut` satırı dosyanın en
    sonu olduğu için o tur sessizce kayboluyordu). Bir dosyanın tamamını
    okumak 1 MB'ta bile saniyenin altında; performans için doğruluk
    feda edilmez.

    Args:
        path: `settings.log_dir` içindeki `artemis.log`.
        limit: Döndürülecek en fazla tur sayısı (en yeniler).

    Returns:
        Kronolojik (eskiden yeniye) sıralı tur listesi.

    Raises:
        OSError: Dosya okunamazsa (çağıran yakalar ve hata durumu gösterir).
    """

    turns: list[HistoryTurn] = []
    current: HistoryTurn | None = None

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _LOG_LINE.match(line)
        if match is None:
            continue

        message = match.group("message")
        timestamp = _format_timestamp(match.group("ts"))

        if (heard := _HEARD.match(message)) is not None:
            # Yeni tur: önceki turu (varsa) kapatıp yenisini başlat.
            _close(turns, current)
            current = HistoryTurn(timestamp=timestamp, said=_unquote(heard.group("text")))
            continue

        if current is None:
            # Tur dışındaki araç/cevap satırı (log dosyası el ile
            # düzenlenmiş ya da formatı değişmiş olabilir) — yoksay.
            continue

        if (tool := _TOOL.match(message)) is not None:
            current.tools.append(
                (tool.group("tool"), tool.group("success") == "True")
            )
        elif (reply := _REPLY.match(message)) is not None:
            current.reply = reply.group("text")

    _close(turns, current)  # dosyanın sonunda yarım kalan tur
    return turns[-limit:] if limit > 0 else turns


def _close(turns: list[HistoryTurn], turn: HistoryTurn | None) -> None:
    """Tamamlanmış bir turu listeye ekler; boş turları ATAR.

    Boş mikrofon gürültüsü (`Duyulan komut: ''`, üstelik cevapsız) ekranda
    yer kaplamasın: kullanıcıya hiçbir bilgi vermeyen bir satırdır.
    """

    if turn is not None and not turn.is_empty():
        turns.append(turn)


def _format_timestamp(raw: str) -> str:
    """Logun `2026-07-26 03:13:04` damgasını Türkçe biçime çevirir.

    Biçim zaten `_LOG_LINE` ile katı olarak doğrulandığı için
    `strptime` burada güvenlidir; beklenmedik bir değerde olduğu gibi
    döndürülür (tarihi gizlemek yerine yanlış göstermek daha kötü).
    """

    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return raw


# --------------------------------------------------------------------------
# Görünüm
# --------------------------------------------------------------------------


def _on_background(color: QColor, base: QColor | None = None) -> str:
    """Yarı saydam bir palet rengini verilen zemin üzerinde KARIŞTIRIR.

    `theme.TEXT_SECONDARY`/`TEXT_MUTED` alfa kanallıdır ve `QColor.name()`
    o kanalı düşürürdü — açık bir renk paletten ödünç alınıp zeminle
    harmanlanmazsa yanlış (çok parlak) görünür. `QTextBrowser`'ın HTML'i
    `rgba()`'yı sürümden sürüme farklı yorumladığı için harmanlama
    burada, bir kez yapılır.

    `base` verilmezse panel zemini (`theme.BG_BASE`) kullanılır; kart
    zemini (`BG_PANEL`) üzerindeki metin için `base=theme.BG_PANEL`
    geçilir.
    """

    backdrop = theme.BG_BASE if base is None else base
    weight = color.alphaF()
    channels = [
        round(getattr(color, name)() * weight + getattr(backdrop, name)() * (1 - weight))
        for name in ("red", "green", "blue")
    ]
    return f"rgb({channels[0]}, {channels[1]}, {channels[2]})"


def _contrast_ratio(color: QColor, backdrop: QColor) -> float:
    """WCAG 2.x göreli parlaklık oranı — 4.5 metin / 3.0 büyük metin.

    Yalnızca TASARIM KONTROLÜ içindir: renk seçimi gözle değil, bu
    sayıyla yapılır. `theme`'in yarı saydam metin renkleri zeminle
    harmanlanmadan ölçülemez, önce harmanlanır.
    """

    def _luminance(rgb: tuple[int, int, int]) -> float:
        channels = []
        for raw in rgb:
            ratio = raw / 255
            channels.append(ratio / 12.92 if ratio <= 0.04045 else ((ratio + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def _blend(color: QColor) -> tuple[int, int, int]:
        weight = color.alphaF()
        return tuple(  # type: ignore[return-value]
            round(getattr(color, name)() * weight + getattr(backdrop, name)() * (1 - weight))
            for name in ("red", "green", "blue")
        )

    first, second = _luminance(_blend(color)), _luminance((0, 0, 0))
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _render_turn(turn: HistoryTurn, *, last: bool) -> str:
    """Tek bir turu, `QTextBrowser`'ın gösterebileceği HTML'e çevirir.

    TURLAR ARASI AYRIM bir KART ile yapılır: her tur `BG_PANEL` zeminli,
    1 px kenarlıklı bir kutu içinde durur. Önceden turlar yalnızca 14 px
    boşlukla ayrılıyordu — bu, üç turu okuyan birinin nerede yeni tur
    başladığını görmesini imkânsız kılıyordu.

    NEDEN `div`, NEDEN `table` DEĞİL: kart zemini ve kenar için
    `<table>` denendi ve doğru çizildi, ancak hücre içindeki iki
    `inline` parça (araç adı + renkli durum etiketi) FARKLI TABAN
    ÇİZGİLERİNE oturdu ve etiketin üstünden ince bir çizgi geçti —
    görsel olarak "üzeri çizilmiş" okunuyordu. Düz `div` yığını aynı
    taban çizgisini ve düzgün hizayı veriyor.

    ZAMAN DAMGASI artık `TEXT_SECONDARY` (kart zemini üzerinde 8.26:1).
    `TEXT_MUTED` burada 4.09:1 idi ve zemine karışıyordu; artık
    12 px'lik ikincil metinde de kullanılmıyor.
    """

    card = theme.BG_PANEL
    hairline = _on_background(theme.BORDER, card)
    stamp = _on_background(theme.TEXT_SECONDARY, card)  # 8.26:1
    body = _on_background(theme.TEXT_PRIMARY, card)  # 15.28:1
    accent = theme.ACCENT_TEAL.name()  # 12.20:1

    spacing = "" if last else "margin-bottom: 10px;"
    parts = [
        f'<div style="background-color: {card.name()}; border: 1px solid {hairline};'
        f' padding: 12px 16px; {spacing}">',
        f'<div style="color:{stamp}; font-size:12px; margin-bottom:8px;">'
        f"{html.escape(turn.timestamp)}</div>",
    ]

    said = turn.said.strip()
    spoken = html.escape(said) if said else f'<span style="color:{stamp};">ses anlaşılmadı</span>'
    parts.append(f'<div style="color:{body};"><span style="color:{accent};">Siz:</span> {spoken}</div>')

    for tool, ok in turn.tools:
        mark = "başarılı" if ok else "başarısız"
        color = (theme.ACCENT_GREEN if ok else theme.ACCENT_RED).name()
        parts.append(
            f'<div style="color:{stamp}; font-size:12px; margin-top:4px;">'
            f'&nbsp;&nbsp;{html.escape(tool)} '
            f'<span style="color:{color};">({mark})</span></div>'
        )

    if turn.reply:
        parts.append(f'<div style="color:{body}; margin-top:8px;">{html.escape(turn.reply)}</div>')

    parts.append("</div>")
    return "".join(parts)


def _render_empty_state(headline: str, detail: str) -> str:
    """Kayıt yokken gösterilen iki satır.

    Dikey/yatay ortalama `QTextBrowser.setAlignment` ile DEĞİL, dışarıda
    ortalanan bir `QLabel` ile yapılır (bkz. `_build_history_tab`): Qt'nin
    metin motoru belgeyi daima üstten yazar.
    """

    return (
        f'<div style="color:{_on_background(theme.TEXT_PRIMARY)}; font-size:15px; font-weight:600;">'
        f"{html.escape(headline)}</div>"
        f'<div style="color:{_on_background(theme.TEXT_SECONDARY)}; font-size:12px; margin-top:8px;">'
        f"{html.escape(detail)}</div>"
    )


def describe_llm(settings: Settings, llm_client: Any | None) -> str:
    """Durum şeridinin "Beyin" satırındaki DEĞER.

    Çalışan bir istemci varsa O konuşur (gerçek durum); yoksa ayardaki
    seçim yazılır. İkisi de elde olan gerçek bilgidir — panel, hangi
    sağlayıcının kullanılacağını TAHMİN etmez.

    ÖNEMLİ: "Beyin: " öneki burada YOK. Sütun başlığı zaten "Beyin"
    diyor; önek eklemek aynı kelimeyi iki kez yazıyordu.
    """

    if llm_client is not None:
        return _PROVIDER_LABELS.get(type(llm_client).__name__, type(llm_client).__name__)

    mode = _PROVIDER_MODE_LABELS.get(settings.llm_provider, settings.llm_provider)
    return f"{mode} — istemci başlatılmadı"


def describe_voice(settings: Settings) -> str:
    """Durum şeridi için sesli asistan satırının KISA hâli.

    Dürüst ol: panel sesli modu BAŞLATMAZ, yalnızca ayarın ne olduğunu
    söyler. Aksi halde "Sesli asistan: açık" yazısı, aslında mikrofonun
    hiç açılmadığı bir pencerede yanlış bilgi verirdi.

    NEDEN KISA: uzun sürümü (`uyandırma "Artemis" · kısayol … · başlatmak
    için: python main.py --voice`) tek satıra sığmayıp pencereyi 823
    piksele şişiriyordu. Uyanma sözcüğü ve kısayol artık AYRI bir
    ikincil satırda; `python main.py --voice` yolu ise bir TOOLTIP'te.
    """

    if not settings.voice_enabled:
        return "Kapalı — mikrofon hiç açılmaz"

    return "Açık" if settings.wake_word_enabled else "Açık — yalnızca kısayolla"


def describe_voice_details(settings: Settings) -> str:
    """Sesli asistan satırının altındaki ikincil açıklama.

    Boş dönüyorsa etiket hiç gösterilmez (kapalıyken ekranda boş bir
    satır kalmaz) — boş `QLabel` yükseklik yiyip hizayı bozar.
    """

    if not settings.voice_enabled:
        return "Açmak için: Ayarlar sekmesi → Sesli asistan"

    parts = []
    if settings.wake_word_enabled:
        parts.append('uyandırma "Artemis"')
    parts.append(f"kısayol {settings.voice_hotkey}")
    return "  ·  ".join(parts)


_VOICE_START_HINT = "Sesli modu başlatmak için: python main.py --voice"
"""Tooltip'te duran tek cümle. Ekranın kalabalığını artıran bu yol,
üzerine gelince görünür."""


class ArtemisPanel(QWidget):
    """Artemis'in görüntüleme paneli: geçmiş + ayarlar + durum.

    Mesaj YAZMA kutusu YOKTUR. Bu bir sohbet arayüzü değil, asistanın
    ne yaptığını gösteren bir paneldir; konuşmanın aracı `--voice`'dur.

    Args:
        settings: `config.yaml`'dan okunmuş ayarlar. Durum şeridinin tek
            kaynağıdır.
        llm_client: Varsa, çalışan istemci (durum şeridi bunun adını
            yazar). Yoksa panel yine açılır — yalnızca ayardaki seçim
            yazılır.
        parent: Qt üst widget'ı.
    """

    def __init__(
        self,
        settings: Settings,
        llm_client: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._settings = settings
        self._llm_client = llm_client
        self.setStyleSheet(theme.stylesheet())
        self.setWindowTitle("Artemis")
        self.setMinimumWidth(_MIN_WIDTH)
        self.resize(_WINDOW_WIDTH, _WINDOW_HEIGHT)

        # Boşluk ölçeği 4/8/12/16/24 — panelin her kenarı bu beş
        # değerden birini kullanır (arada yarım sayı yok).
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(self._build_title())
        layout.addWidget(self._build_status())
        layout.addWidget(self._build_tabs(), stretch=1)
        layout.addLayout(self._build_footer())

        self.reload_history()

    # ------------------------------------------------------------------
    # Kurulum
    # ------------------------------------------------------------------

    def _build_title(self) -> QLabel:
        title = QLabel("Artemis")
        title.setProperty("role", "heading")
        return title

    def _build_status(self) -> QWidget:
        """İki satırlık durum şeridi: nokta + etiket + değer.

        SOL HİZALAMA bir ızgara ile garanti edilir: etiket sütunu sabit
        genişlikte olduğundan "Beyin" ile "Sesli asistan" alt alta DÜZGÜN
        durur. Önceden iki ayrı `QLabel` serbest bırakılmıştı; uzun metin
        kendi genişliğini dayatıp ikinci satırı kaydırıyordu.

        Genişlik: hiçbir satır `python main.py --voice` yolunu İÇERMEZ.
        O cümle TOOLTIP'te durur; yol ekranda olmadığı için pencere
        minimumu 823 yerine ≈600'e düşer.
        """

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(1, 104)  # etiket sütunu: alt alta hizalı
        grid.setColumnStretch(2, 1)

        llm_value = describe_llm(self._settings, self._llm_client)
        voice_value = describe_voice(self._settings)
        voice_details = describe_voice_details(self._settings)

        rows = (
            ("Beyin", llm_value, theme.ACCENT_GREEN if self._llm_client is not None else theme.ACCENT_ORANGE,
             "Çalışan LLM istemcisi yok; panel yalnızca ayardaki seçimi gösterir."),
            ("Sesli asistan", voice_value, theme.ACCENT_GREEN if self._settings.voice_enabled else theme.TEXT_MUTED,
             _VOICE_START_HINT if self._settings.voice_enabled else "Açmak için: Ayarlar sekmesi"),
        )

        for row, (caption, value, dot_color, tip) in enumerate(rows):
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setProperty("role", "dot")
            dot.setStyleSheet(f"background-color: {dot_color.name()}; border-radius: 4px;")
            dot.setToolTip(tip)

            caption_label = QLabel(caption)
            caption_label.setProperty("role", "status")
            caption_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            value_label = QLabel(value)
            value_label.setProperty("role", "subtitle")
            value_label.setWordWrap(True)  # uzun model slug'ları taşmasın
            value_label.setToolTip(tip)
            # `Ignored`: sarmalı bir etiket, tek satır genişliğine göre
            # boyut ipucu verip şeridi (dolayısıyla tüm paneli) 676
            # piksele zorluyordu. Metin verilen genişlikte sarılır.
            value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

            grid.addWidget(dot, row, 0, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(caption_label, row, 1)
            grid.addWidget(value_label, row, 2)

        if voice_details:
            details = QLabel(voice_details)
            details.setProperty("role", "hint")
            details.setToolTip(_VOICE_START_HINT)
            details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            grid.addWidget(details, 2, 2)

        container = QWidget()
        container.setLayout(grid)
        return container

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()

        self._stack = QStackedWidget()
        self._history = QTextBrowser()
        self._history.setOpenExternalLinks(False)
        self._history.setOpenLinks(False)
        self._history.setFrameShape(QTextBrowser.Shape.NoFrame)
        # Sekme çubuğunun hemen altındaki boş kenar: içerik üstte
        # sekmeye yapışmasın. `QTextBrowser` kendi iç boşluğunu sıfır
        # bıraktığı için bu dolgu elle veriliyor.
        self._history.setContentsMargins(16, 12, 16, 12)

        self._empty = QLabel()
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setTextFormat(Qt.TextFormat.RichText)

        # Boş durum etiketi hem YATAY hem DİKEY ortalanır: dikey ortalama
        # için etiketin altına ve üstüne `addStretch` konur. Önceden
        # boşluk `margin-top` ile taklit ediliyordu ve metin pencere
        # ortasında değil, üstte yapışık duruyordu.
        empty_page = QWidget()
        empty_box = QVBoxLayout(empty_page)
        empty_box.setContentsMargins(24, 0, 24, 0)
        empty_box.addStretch(1)
        empty_box.addWidget(self._empty)
        empty_box.addStretch(1)

        self._stack.addWidget(empty_page)
        self._stack.addWidget(self._history)
        tabs.addTab(self._stack, "Geçmiş")

        # `SettingsWindow` normal bir `QWidget`; sekmeye YERLEŞTİRMEK onu
        # yeniden yazmaz, aynı pencereyi kullanır. Dolayısıyla `--settings`
        # yolu ve panelin ayarlar sekmesi TEK formu, TEK kaydetme yolunu
        # paylaşır.
        #
        # Kaydırma alanı iki işe yarar: pencereyi ayar formunun minimum
        # genişliğine sürüklemez ve pencere kısaldığında forma erişimi
        # korur. Uzun yol satırları kaydırmaya DEĞİL, sarma yapmaya
        # zorlanır (bkz. `ui/settings_window.py::SettingsWindow`).
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        settings_scroll.setWidget(SettingsWindow())
        tabs.addTab(settings_scroll, "Ayarlar")

        return tabs

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._source = QLabel("")
        self._source.setProperty("role", "hint")
        self._source.setWordWrap(True)
        # Log yolu mutlak ve uzundur (ör. `C:\Users\...\artemis.log`).
        # Etikete normalde 627 piksellik bir taban çiziyordu ve PANELİN
        # tamamı bu yüzden 823'e zorlanıyordu. `Ignored` yatay politika
        # yerleşime "boyut ipucuna bakma" der; sarma etiketi daraltır,
        # kaynak satırı pencere daraldıkça alt satıra iner.
        self._source.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self._source, stretch=1)

        refresh = QPushButton("Yenile")
        refresh.setToolTip("Log dosyasını yeniden oku")
        refresh.clicked.connect(self.reload_history)
        row.addWidget(refresh)

        return row

    # ------------------------------------------------------------------
    # Geçmiş
    # ------------------------------------------------------------------

    @property
    def log_path(self) -> Path:
        """Geçmişin okunduğu dosya (`settings.log_dir` + `artemis.log`)."""

        return Path(self._settings.log_dir) / "artemis.log"

    def reload_history(self) -> None:
        """Geçmiş sekmesini log dosyasından yeniden okur.

        Üç DURUM ayrıdır ve hiçbiri diğerinin yerine geçmez: dosya yok
        (henüz loglanmamış), dosya okunamadı (izin/bozuk dosya) ve
        dosya var ama içinde tanınabilir konuşma kaydı yok.

        Boş/hata durumları `QStackedWidget` üzerinde AYRI bir sayfada
        durur: metin motoru `QTextBrowser`'ı dikeyde ortalamadığı için
        ortalanma dışarıda, esnek bir düzende yapılır.
        """

        path = self.log_path

        if not path.exists():
            self._show_empty(
                "Henüz kayıt yok.",
                "Konuşmalar logs/artemis.log dosyasına yazılır. "
                "Sesli asistanı python main.py --voice ile başlatıp konuştuğunda burada görünür.",
            )
            self._source.setText(f"Kaynak: {path}")
            return

        try:
            turns = parse_history(path)
        except OSError as exc:
            logger.warning("Konuşma geçmişi okunamadı (%s): %s", path, exc)
            self._show_empty("Geçmiş okunamadı.", f"{path} açılamadı: {exc}")
            self._source.setText(f"Kaynak: {path}")
            return

        if not turns:
            self._show_empty(
                "Bu logda konuşma kaydı yok.",
                "Dosya var ama içinde okunabilir bir tur bulunamadı. "
                "Konuşma satırları logs/artemis.log dosyasına yazılır.",
            )
        else:
            body = "".join(_render_turn(turn, last=index == len(turns) - 1) for index, turn in enumerate(turns))
            self._history.setHtml(body)
            self._stack.setCurrentIndex(1)
            self._history.verticalScrollBar().setValue(0)

        self._source.setText(f"Kaynak: {path}  ·  {len(turns)} tur")

    def _show_empty(self, headline: str, detail: str) -> None:
        """Boş/hata durumunu ortalanmış sayfada gösterir."""

        self._empty.setText(_render_empty_state(headline, detail))
        self._stack.setCurrentIndex(0)


# --------------------------------------------------------------------------
# Tek pencere erişimi
# --------------------------------------------------------------------------

_open_panel: ArtemisPanel | None = None
"""Açık tek paneli tutan referans.

`ui/settings_window.py::show_settings`'in aynı gerekçesi: `ArtemisPanel()`
ifadesiyle açılan pencere, o ifade bitince referanssız kalır ve Python
toplayıcısı onu silmek zorunda kalır — görünür pencere bir anda yok olur.
"""


def show_panel(settings: Settings, llm_client: Any | None = None) -> ArtemisPanel:
    """Paneli açar (aynı anda yalnızca bir tane bulunur)."""

    global _open_panel
    if _open_panel is None:
        _open_panel = ArtemisPanel(settings, llm_client)

    _open_panel.show()
    _open_panel.raise_()
    _open_panel.activateWindow()
    return _open_panel


def _run_demo() -> None:
    """`python -m ui.panel`: arayüzü tek başına gösterir, hiçbir şey yapmaz."""

    from config.settings import get_settings

    app = QApplication(sys.argv)
    show_panel(get_settings())
    raise SystemExit(app.exec())


if __name__ == "__main__":
    _run_demo()
