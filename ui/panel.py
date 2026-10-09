"""Artemis paneli — geçmiş, ayarlar, durum ve yazılı komut tek pencerede.

NEDEN AYRI BİR DOSYA: `ui/chat_window.py` eski bir SOHBET penceresidir ve
`tests/test_ui_chat_window.py` ona bağlıdır; panel onun yerine geçmiş +
ayarlar + durum gösterir, altındaki yazı kutusu ise AYNI komut hattını
(`core/command_runner.py`) sesli asistanla paylaşan ince bir giriştir.

YAZILI KOMUT: alt kutuya yazılan komut sesli komutla aynı yoldan geçer
(proje görüşmesi → LLM → tool planı → onay). İşlem bir İŞ PARÇACIĞINDA
çalışır, arayüz donmaz; onay gereken adım için Qt diyaloğu açılır ve
diyalog gösterdiği tool adını ve ARGÜMANLARI yazar. Sonuç hemen bir balon
çifti olarak geçmişe eklenir ve log'a da yazılır (`Yazılı komut:` satırı),
böylece "Yenile" ile okunduğunda aynı görünür.

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
    * Turlar `BG_PANEL` zeminli, kenarlıklı kartlarla ayrılır; zaman
      damgası 12 px `TEXT_SECONDARY` (kart zemini üzerinde 8.26:1 — WCAG
      AA). Daha önce 11 px `TEXT_MUTED` idi (4.09:1) ve zemine karışıyordu.
      Kart `<table>`+`bgcolor` ile çizilir; Qt `<div>` border/padding'i
      düşürdüğü için turlar yapışık okunuyordu (bkz. `_render_turn`).
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
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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
from core.command_runner import CommandOutcome, CommandRunner
from core.dispatcher import ToolDispatcher
from ui import theme
from ui.settings_window import SettingsWindow
from utils.confirmation import format_confirmation_arguments
from utils.tool_labels import tool_label

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

_DEFAULT_SOURCE_LABEL = "logs/artemis.log"
"""Ekran görüntüsü üretirken alt satırda GÖRÜNEN kaynak adı.

`ArtemisPanel(source_label=…)` verilmezse gerçek mutlak yol yazılır
(davranış değişmez). Yalnızca `scripts/screenshot_panel.py`, geçici
klasördeki örnek log için bunu geçer: README'de `C:\\Users\\…\\Temp\\…`
gibi bir geçici yol görünmesin diye. Panel kodu sahte veri TUTMAZ —
yalnızca ETİKET değişir, okunan dosya aynıdır."""

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

# Yazılı komut satırları (bkz. `ArtemisPanel._execute_command`).
# Biçim: `Yazılı komut: <metin>` ve `Yazılı cevap[ (hata|proje)]: <metin>`.
# Tür, cevabın balon rengini geri kazanmak için parantez içinde taşınır.
_TYPED = re.compile(r"^Yazılı komut: (?P<text>.*)$")
_TYPED_REPLY = re.compile(r"^Yazılı cevap(?: \((?P<kind>hata|proje)\))?: (?P<text>.*)$")
_TYPED_REPLY_KINDS = {"hata": "error", "proje": "project"}

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
        said: Kullanıcının söylediği ya da yazdığı metin. Boşsa ses anlaşılmamıştır.
        reply: Artemis'in verdiği yanıt (yalnızca logda varsa).
        tools: `(tool adı, başarılı mı)` çiftleri, logdaki çalıştırma
            sırasıyla.
        typed: Tur panelin yazı kutusundan gelmişse True. Bu turlar balon
            çifti olarak çizilir; sesli turlar eskisi gibi kart olarak kalır.
        kind: Cevabın türü (`core/command_runner.py::CommandKind`). Yalnızca
            yazılı turlar için anlamlıdır; sesli turlarda "done" kalır.
    """

    timestamp: str
    said: str = ""
    reply: str = ""
    tools: list[tuple[str, bool]] = field(default_factory=list)
    typed: bool = False
    kind: str = "done"

    def is_failed(self) -> bool:
        """Turda bir hata var mı: hatalı cevap türü ya da başarısız bir tool adımı."""

        return self.kind == "error" or any(not ok for _, ok in self.tools)

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

        if (typed := _TYPED.match(message)) is not None:
            # Yazılı komut da bir turdur; sesli turla aynı kuralla kapanır.
            _close(turns, current)
            current = HistoryTurn(timestamp=timestamp, said=typed.group("text"), typed=True)
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
        elif (typed_reply := _TYPED_REPLY.match(message)) is not None:
            current.reply = typed_reply.group("text")
            current.kind = _TYPED_REPLY_KINDS.get(typed_reply.group("kind") or "", "done")

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


def _tool_line(tool: str, ok: bool, stamp: str, *, margin: str) -> str:
    """Bir tool adımının tek satırlık HTML'i: etiket + başarı durumu.

    Ham ad `title` özniteliğiyle taşınır: Qt bunu karakter biçimine
    `toolTip` olarak alır, yani üzerine gelince gerçek tool adı görünür.
    """

    mark = "başarılı" if ok else "başarısız"
    color = (theme.ACCENT_GREEN if ok else theme.ACCENT_RED).name()
    return (
        f'<p style="color:{stamp}; font-size:12px; margin:{margin};">'
        f'&nbsp;&nbsp;<span title="{html.escape(tool)}">{html.escape(tool_label(tool))}</span> '
        f'<span style="color:{color}; font-size:12px;">({mark})</span></p>'
    )


def _mix_hex(color: QColor, base: QColor) -> str:
    """Yarı saydam bir rengi zemine karıştırıp `#rrggbb` döndürür.

    Tablo `bgcolor` özniteliği `rgb(...)` kabul etmez (Qt uyarır ve rengi atlar);
    bu yüzden balon zemini hex olarak üretilir.
    """

    weight = color.alphaF()
    channels = [
        round(getattr(color, name)() * weight + getattr(base, name)() * (1 - weight))
        for name in ("red", "green", "blue")
    ]
    return QColor(channels[0], channels[1], channels[2]).name()


_BUBBLE_GAP = '<p style="margin:6px 0 0 0;"></p>'
"""Kullanıcı ve Artemis balonları arasındaki boşluk (tablolar ayrı olduğu için)."""


def _render_exchange(turn: HistoryTurn, *, last: bool) -> str:
    """Yazılı bir turu balon çifti olarak çizer: kullanıcı sağda, Artemis solda.

    Sağa/sola yaslama iki sütunlu bir tabloyla yapılır (boş sütun + balon).
    Balonlar `<table>` + `bgcolor` ile çizilir, çünkü Qt'nin zengin metin
    alt kümesi `<div>` kenarlığını düşürür (bkz. `_render_turn`).

    Renkler `theme` tokenlarından KARIŞTIRILIR (yeni palet yok):
        * kullanıcı balonu: mavi vurgunun zemine karıştırılmış hâli, metin
          ana metin rengi — beyaz-mavi çiftinin kontrastı WCAG AA'nın altında
          kaldığı için beyaz metin kullanılmaz;
        * Artemis balonu: yükseltilmiş yüzey (`BG_ELEVATED`);
        * hatalı/başarısız: kırmızı kenarlık; proje cevabı: turkuaz kenarlık.
    """

    card = theme.BG_PANEL
    stamp = _on_background(theme.TEXT_SECONDARY, card)
    body_on_card = _on_background(theme.TEXT_PRIMARY, card)

    # Kullanıcı balonu: mavi vurgunun %27'si kart zeminine karışık. Metin
    # açık ana metin rengi — doygun maviye beyaz metin koymak kontrastı
    # düşürür; bu zemin koyu olduğu için açık metin yeterince okunur.
    user_fill = _mix_hex(QColor(theme.ACCENT_BLUE.red(), theme.ACCENT_BLUE.green(), theme.ACCENT_BLUE.blue(), 70), card)
    user_body = theme.TEXT_PRIMARY.name()

    if turn.is_failed():
        reply_border = theme.ACCENT_RED.name()
    elif turn.kind == "project":
        reply_border = theme.ACCENT_TEAL.name()
    else:
        reply_border = _on_background(theme.BORDER, card)
    reply_fill = theme.BG_ELEVATED.name()

    def _bubble(inner: str, fill: str, border: str) -> str:
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td bgcolor="{fill}" style="background-color:{fill}; border:1px solid {border}; '
            f'padding: 10px 14px;">{inner}</td></tr></table>'
        )

    said = html.escape(turn.said.strip() or "(boş)")
    user_html = _bubble(
        f'<p style="color:{user_body}; font-size:13px; margin:0;">{said}</p>',
        user_fill,
        user_fill,
    )

    reply_parts = [
        f'<p style="color:{body_on_card}; font-size:13px; margin:0;">{html.escape(turn.reply)}</p>'
        if turn.reply
        else f'<p style="color:{stamp}; font-size:12px; margin:0;">Cevap yok.</p>'
    ]
    for index, (tool, ok) in enumerate(turn.tools):
        reply_parts.append(_tool_line(tool, ok, stamp, margin="8px 0 0 0" if index == 0 else "4px 0 0 0"))
    reply_html = _bubble("".join(reply_parts), reply_fill, reply_border)

    # Iki sıra AYRI tablolardır: tek tabloda sütun genişlikleri iki sıra için
    # ortaktır ve Artemis balonunu %25'e sıkıştırırdı.
    stamp_html = (
        f'<p style="color:{stamp}; font-size:12px; margin:0 0 4px 0; text-align:right;">'
        f"{html.escape(turn.timestamp)}</p>"
    )
    user_row = (
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="25%">&nbsp;</td><td width="75%">{stamp_html}{user_html}</td></tr></table>'
    )
    reply_row = (
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="75%">{reply_html}</td><td width="25%">&nbsp;</td></tr></table>'
    )
    spacer = "" if last else '<p style="margin:0; font-size:8px;">&nbsp;</p>'
    return f"{user_row}{_BUBBLE_GAP}{reply_row}{spacer}"


def _render_turn(turn: HistoryTurn, *, last: bool) -> str:
    """Tek bir turu, `QTextBrowser`'ın gösterebileceği HTML'e çevirir.

    Yazılı turlar balon çiftine (`_render_exchange`) gider; sesli turlar aşağıdaki
    kart biçimiyle çizilir.

    TURLAR ARASI AYRIM bir KART ile yapılır: her tur `BG_PANEL` zeminli,
    kenarlıklı bir kutu içinde durur. Önceden turlar yalnızca 14 px
    boşlukla ayrılıyordu — bu, üç turu okuyan birinin nerede yeni tur
    başladığını görmesini imkânsız kılıyordu.

    NEDEN `table`, NEDEN `div` DEĞİL: Qt'nin metin motoru `<div>`'in
    `border` ve `padding`'ini DÜŞÜRÜR (`toHtml()` çıktısında kart çerçevesi
    kaybolduğu için doğrulanmıştı) — kart zemini yayılıyor, kenar
    çizilmiyor, turlar birbirine yapışıyordu. `<table>` + `bgcolor` ise
    QTextBrowser'ın zengin metin alt kümesinde hem zemin hem `cellpadding`
    olarak GERÇEKTEN çizilir.

    NEDEN İKİ PARÇA DEĞİL: aynı satırdaki iki `inline` parça
    (`Siz:` etiketi ve metin, araç adı ve durum etiketi) taban çizgisini
    paylaşmadığında üstlerinden ince bir çizgi geçiyordu — "üzeri
    çizilmiş" görünüyordu. Bu, `QTextBrowser`'ın satır YÜKSELTME
    HESABI değil, Qt'nin zengin metin alt kümesinin `<table>` hücresi
    içinde satır taban çizgisini düzeltmemesidir. ÖLÇÜLDÜ: aynı
    boyuttaki iki span sorunsuz; 11 px + 13 px karışımı çizgi bırakıyor.
    Bu yüzden her satır TEK bir `<p>` ve içinde TEK bir `<span>`
    (`Siz:` etiketi kendi satırında) — farklı boyut aynı satırda
    görünmez.

    ZAMAN DAMGASI `TEXT_SECONDARY` (kart zemini üzerinde 8.26:1);
    `TEXT_MUTED` 4.09:1 idi ve zemine karışıyordu.
    """

    if turn.typed:
        return _render_exchange(turn, last=last)

    card = theme.BG_PANEL
    hairline = _on_background(theme.BORDER, card)
    stamp = _on_background(theme.TEXT_SECONDARY, card)  # 8.26:1
    body = _on_background(theme.TEXT_PRIMARY, card)  # 15.28:1
    accent = theme.ACCENT_TEAL.name()  # 12.20:1

    gap = "" if last else "margin-bottom: 12px;"
    parts = [
        f'<table width="100%" cellpadding="0" cellspacing="0" bgcolor="{card.name()}"'
        f' style="background-color: {card.name()}; {gap}">',
        f'<tr><td bgcolor="{card.name()}" style="background-color: {card.name()};'
        f' border: 1px solid {hairline}; border-radius: 8px; padding: 12px 16px;">',
    ]

    # Zaman damgası: turun kimlik etiketi, kartın en üstünde.
    parts.append(
        f'<p style="color:{stamp}; font-size:12px; margin:0 0 8px 0;">{html.escape(turn.timestamp)}</p>'
    )

    said = turn.said.strip()
    if said:
        # Etiket ve metin AYRI satırlar: aynı satırda iki renkli
        # parça, Qt'de taban çizgisi hizasını bozuyor (yukarıdaki ölçüm).
        parts.append(
            f'<p style="color:{accent}; font-size:12px; margin:0 0 2px 0;">Siz:</p>'
            f'<p style="color:{body}; font-size:13px; margin:0;">{html.escape(said)}</p>'
        )
    else:
        parts.append(
            f'<p style="color:{stamp}; font-size:12px; margin:0;">ses anlaşılmadı</p>'
        )

    for tool, ok in turn.tools:
        parts.append(_tool_line(tool, ok, stamp, margin="6px 0 0 0"))

    if turn.reply:
        parts.append(
            f'<p style="color:{body}; font-size:13px; margin:10px 0 0 0;">'
            f"{html.escape(turn.reply)}</p>"
        )

    parts.append("</td></tr></table>")
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


_TYPED_REPLY_MARKERS = {"error": " (hata)", "project": " (proje)"}
"""Cevap türü -> log'daki parantezli işaret. `parse_history` bunu geri okur."""


def _one_line(text: str) -> str:
    """Log satırı için metni tek satıra indirir (yeni satır log'u bölerdi)."""

    return " ".join(text.split())


def _log_outcome(outcome: CommandOutcome) -> None:
    """Yazılı cevabı log'a yazar; geçmiş sekmesi bunu Yenile'de geri okur.

    `%s` kullanılır, `%r` değil: `%r` yol ters eğik çizgilerini çiftler
    (`C:\\\\Users`), oysa geçmişte insanın yazdığı gibi görünmesi gerekir.
    """

    marker = _TYPED_REPLY_MARKERS.get(outcome.kind, "")
    logger.info("Yazılı cevap%s: %s", marker, _one_line(outcome.reply))


@dataclass
class _ConfirmationRequest:
    """Çalışan iş parçacığı ile onay diyaloğu arasındaki tek yönlü elçi.

    `ui/chat_window.py::_ConfirmationRequest` ile aynı desen. Bilinçli olarak
    kopyalandı: o modül artık kullanılmayan eski bir pencereye ait ve panelin
    onu içe aktarması, panelin yaşam döngüsünü eski pencereye bağlardı.

    Attributes:
        event: Komut iş parçacığı bunun üzerinde bekler; diyalog cevabı
            yazdıktan sonra `set()` çağrılır.
        result: Tek elemanlı liste; cevap buraya yazılır (`bool`'u doğrudan
            döndüremeyiz, sinyal argümanı olarak taşınır).
    """

    event: threading.Event = field(default_factory=threading.Event)
    result: list[bool] = field(default_factory=lambda: [False])


class _CommandSignals(QObject):
    """Komut iş parçacığından GUI iş parçacığına köprü sinyalleri.

    Bu nesne GUI iş parçacığında yaşar. Komut düz bir `threading.Thread`'de
    çalışıp bu sinyalleri yayınladığında Qt, alıcı (panel) farklı bir iş
    parçacığında olduğu için çağrıyı olay kuyruğuna koyar: yuvalar GUI
    iş parçacığında çalışır, widget'a dokunmak güvenlidir.

    Düz bir `threading.Thread` tercih edildi, `QThread` değil: `QThread`
    nesnesi çalışırken yok edilirse Qt süreci çökertir; düz bir daemon iş
    parçacığı uygulama kapanırken sorunsuz biter.
    """

    turn_finished = pyqtSignal(str, object)
    """(yazılan metin, `CommandOutcome`): komut bitti."""

    confirmation_requested = pyqtSignal(str, dict, object)
    """(tool adı, argümanlar, `_ConfirmationRequest`): onay gerekiyor."""


class ArtemisPanel(QWidget):
    """Artemis'in paneli: geçmiş + ayarlar + durum + yazılı komut.

    Geçmiş salt okunurdur; altındaki yazı kutusu aynı komut hattına (sesli
    asistanla ortak) komut gönderir. Komut çalışırken kutu kilitlenir ve
    "Düşünüyorum…" görünür.

    Args:
        settings: `config.yaml`'dan okunmuş ayarlar. Durum şeridinin tek
            kaynağıdır.
        llm_client: Varsa, çalışan istemci (durum şeridi bunun adını
            yazar). Yoksa panel yine açılır — yalnızca ayardaki seçim
            yazılır.
        parent: Qt üst widget'ı.
        source_label: Alt satırda gösterilecek kaynak adı. `None` (varsayılan)
            ise gerçek mutlak yol yazılır; ekran görüntüsü üretirken
            geçici klasör yolunu gizlemek için `logs/artemis.log` verilir.
        dispatcher: Komutların çalıştığı tool dağıtıcısı. `llm_client` ile
            birlikte verildiğinde yazı kutusu etkinleşir.
        command_runner: Hazır bir komut hattı (test ve özel kullanım için);
            verilirse `dispatcher`/`llm_client` yok sayılır.
        background: `True` (varsayılan) komutu bir iş parçacığında çalıştırır.
            `False` yalnızca testler içindir: komut GUI iş parçacığında, senkron
            çalışır (gerçek uygulamada asla kapatılmaz).
        hide_on_close: Pencere kapatılınca yok edilmek yerine gizlensin mi.
            Tepsi uygulamasında panel tek örnek kalır ve tekrar açılır.
    """

    def __init__(
        self,
        settings: Settings,
        llm_client: Any | None = None,
        parent: QWidget | None = None,
        source_label: str | None = None,
        *,
        dispatcher: ToolDispatcher | None = None,
        command_runner: CommandRunner | None = None,
        background: bool = True,
        hide_on_close: bool = True,
    ) -> None:
        super().__init__(parent)

        self._settings = settings
        self._llm_client = llm_client
        self._source_label = source_label
        self._background = background
        self._hide_on_close = hide_on_close
        self._is_busy = False
        self._turns: list[HistoryTurn] = []
        self._signals = _CommandSignals(self)
        self._signals.turn_finished.connect(self._on_turn_finished)
        self._signals.confirmation_requested.connect(self._on_confirmation_requested)

        # Komut hattı: hazır verilmediyse ve elde bir beyin + dağıtıcı varsa
        # kurulur. İkisinden biri yoksa yazı kutusu kapalı kalır — uydurma bir
        # cevap vermek yerine neden kapalı olduğu söylenir.
        if command_runner is None and dispatcher is not None and llm_client is not None:
            command_runner = CommandRunner(dispatcher, llm_client, settings)
        self._command_runner = command_runner

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
        layout.addWidget(self._build_busy_line())
        layout.addLayout(self._build_input_row())
        layout.addLayout(self._build_footer())

        self._set_busy(False)
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

    def _build_busy_line(self) -> QLabel:
        """Komut çalışırken görünen tek satırlık durum; boştayken gizli.

        Gizli bir satır yer kaplamaz (QVBoxLayout), bu yüzden boştayken
        pencere düzeni değişmez.
        """

        self._busy_line = QLabel("Düşünüyorum…")
        self._busy_line.setProperty("role", "hint")
        self._busy_line.setVisible(False)
        return self._busy_line

    def _build_input_row(self) -> QHBoxLayout:
        """Yazı kutusu + Gönder. Komut hattı yoksa ikisi de kapalıdır.

        Kapalıyken kutu neden kapalı olduğunu placeholder'da söyler; sessizce
        boş bir kutu, kullanıcıya "çalışmıyor" mesajından daha kötüdür.
        """

        row = QHBoxLayout()
        row.setSpacing(8)

        self._input = QLineEdit()
        self._input.returnPressed.connect(self._submit)
        if self._command_runner is None:
            self._input.setPlaceholderText("Komut için bir dil modeli bağlantısı gerekli")
        else:
            self._input.setPlaceholderText("Artemis'e yazın, ör. 'hesap makinesini aç'")
        row.addWidget(self._input, stretch=1)

        self._send = QPushButton("Gönder")
        self._send.setProperty("role", "primary")
        self._apply_role(self._send)
        self._send.clicked.connect(self._submit)
        row.addWidget(self._send)

        return row

    @staticmethod
    def _apply_role(widget: QWidget) -> None:
        """Dinamik `role` özelliğini QSS'e tanıtır (bkz. `ui/chat_window.py`).

        Qt, `setProperty` sonrası stili kendiliğinden yeniden hesaplamaz;
        `unpolish`/`polish` olmadan birincil düğme koyu kalır.
        """

        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ------------------------------------------------------------------
    # Yazılı komut
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        """Komut sürerken yazı kutusunu ve düğmeyi kilitler, "Düşünüyorum…" gösterir.

        Kilit, aynı cümlenin iki kez gönderilmesini engeller: iki tur aynı
        anda çalışsa, ikincinin sonucu birincinin balonları arasına karışırdı.
        """

        self._is_busy = busy
        enabled = (not busy) and self._command_runner is not None
        self._input.setEnabled(enabled)
        self._send.setEnabled(enabled)
        self._busy_line.setVisible(busy)

    def _submit(self) -> None:
        """Enter ya da "Gönder": komutu alır ve çalıştırır.

        Boş girdi ve meşgulken gelen istek sessizce yok sayılır.
        """

        if self._command_runner is None or self._is_busy:
            return
        text = self._input.text().strip()
        if not text:
            return

        self._input.clear()
        self._set_busy(True)

        if self._background:
            threading.Thread(
                target=self._execute_command,
                args=(text,),
                name="artemis-panel-komut",
                daemon=True,
            ).start()
        else:
            self._execute_command(text)

    def _execute_command(self, text: str) -> None:
        """Komutu çalıştırır ve sonucu sinyalle GUI iş parçacığına bildirir.

        Bu yöntem komut iş parçacığında (ya da testte GUI'de) çalışır ve HİÇBİR
        widget'a doğrudan dokunmaz. Beklenmeyen bir hata bile panelin
        çökmesine yol açmaz: kırmızı bir balon olarak gösterilir.
        """

        runner = self._command_runner
        if runner is None:  # _submit korur; yine de iş parçacığında sessiz kalma
            return

        logger.info("Yazılı komut: %s", _one_line(text))
        try:
            outcome = runner.run(text, confirm=self._confirm_from_worker, gate=False)
        except Exception as exc:
            # Geniş yakalama BİLEREK: bu iş parçacığında patlayan her hata
            # panelin "Düşünüyorum…" kilidini sonsuza kadar açık bırakırdı.
            # Hata kırmızı bir balon olarak gösterilir ve log'a tam döküm gider.
            logger.exception("Yazılı komut işlenirken beklenmeyen hata")
            outcome = CommandOutcome(reply=f"Komut işlenemedi: {exc}", kind="error")

        _log_outcome(outcome)
        self._signals.turn_finished.emit(text, outcome)

    def _confirm_from_worker(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Planlayıcının onay sorusunu GUI iş parçacığına taşır ve cevabı bekler.

        Komut iş parçacığı burada bekler; GUI iş parçacığı boş kaldığı için
        diyalog açılır ve cevap gelir. Testte (senkron kip) sinyal doğrudan
        yuvayı çağırır ve bekleme hemen döner.
        """

        request = _ConfirmationRequest()
        self._signals.confirmation_requested.emit(tool_name, dict(arguments), request)
        request.event.wait()
        return request.result[0]

    @pyqtSlot(str, dict, object)
    def _on_confirmation_requested(self, tool_name: str, arguments: dict[str, Any], request: _ConfirmationRequest) -> None:
        """GUI iş parçacığında onay diyaloğunu açar ve cevabı geri bildirir.

        `finally`: diyalog beklenmedik biçimde patlasa bile komut iş parçacığı
        sonsuza kadar beklemez; cevap yoksa güvenli taraf REDDETMEKTİR.
        """

        answer = False
        try:
            answer = self._ask_confirmation(tool_name, arguments)
        finally:
            request.result[0] = answer
            request.event.set()

    def _ask_confirmation(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Onay kutusu: tool adı VE argümanlar. Yalnızca "Evet" True döndürür.

        Argümanlar gösterilmek zorunda (CLAUDE.md, README §16b): tool adı tek
        başına `filesystem.delete`'in hangi dosyayı sileceğini söylemez.
        Varsayılan düğme "Hayır": Enter'a basılmış bir geri alınamaz işlemi
        yanlışlıkla onaylamasın.
        """

        box = QMessageBox(self)
        box.setStyleSheet(theme.stylesheet())
        box.setWindowTitle("Onay gerekiyor")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"'{tool_name}' işlemi onay gerektiriyor.")
        box.setInformativeText(f"Argümanlar: {format_confirmation_arguments(arguments)}")

        evet = box.addButton("Evet", QMessageBox.ButtonRole.AcceptRole)
        hayir = box.addButton("Hayır", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(hayir)

        box.exec()
        return box.clickedButton() is evet

    @pyqtSlot(str, object)
    def _on_turn_finished(self, text: str, outcome: CommandOutcome) -> None:
        """Komut bitti: meşgullüğü kaldırır ve balon çiftini geçmişe ekler."""

        self._set_busy(False)
        self.append_exchange(text, outcome)

    def append_exchange(self, text: str, outcome: CommandOutcome) -> None:
        """Yazılan komutun balon çiftini, log yeniden okunmadan hemen gösterir.

        Log da aynı turu içerir (bkz. `_execute_command`); "Yenile" ile okunduğunda
        aynı balonlar gelir. Burada yalnızca görüntü hemen güncellenir.
        """

        turn = HistoryTurn(
            timestamp=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            said=text,
            reply=outcome.reply,
            tools=[(step.tool_name, step.result.success) for step in outcome.step_results],
            typed=True,
            kind=outcome.kind,
        )
        self._turns.append(turn)
        self._show_turns(self._turns)
        bar = self._history.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._source.setText(f"Kaynak: {self._source_text()}  ·  {len(self._turns)} tur")

    # ------------------------------------------------------------------
    # Geçmiş
    # ------------------------------------------------------------------

    @property
    def log_path(self) -> Path:
        """Geçmişin okunduğu dosya (`settings.log_dir` + `artemis.log`)."""

        return Path(self._settings.log_dir) / "artemis.log"

    def _source_text(self) -> str:
        """Alt satırda yazılacak kaynak metni.

        `source_label` verilmişse o AD yazılır (okunan dosya değişmez);
        verilmemişse gerçek mutlak yol — panel her zaman nerede baktığını
        söyler.
        """

        return self._source_label if self._source_label is not None else str(self.log_path)

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
            self._turns = []
            self._show_empty(
                "Henüz kayıt yok.",
                "Konuşmalar logs/artemis.log dosyasına yazılır. "
                "Sesli asistanı python main.py --voice ile başlatıp konuştuğunda burada görünür.",
            )
            self._source.setText(f"Kaynak: {self._source_text()}")
            return

        try:
            turns = parse_history(path)
        except OSError as exc:
            logger.warning("Konuşma geçmişi okunamadı (%s): %s", path, exc)
            self._turns = []
            self._show_empty("Geçmiş okunamadı.", f"{path} açılamadı: {exc}")
            self._source.setText(f"Kaynak: {self._source_text()}")
            return

        # Log tek kaynaktır: Yenile, oturumda eklenen balonları da log'dan
        # yeniden kurar. Yazılı komutlar log'a yazıldığı için kayıp olmaz.
        self._turns = turns
        if not turns:
            self._show_empty(
                "Bu logda konuşma kaydı yok.",
                "Dosya var ama içinde okunabilir bir tur bulunamadı. "
                "Konuşma satırları logs/artemis.log dosyasına yazılır.",
            )
        else:
            self._show_turns(turns)
            self._history.verticalScrollBar().setValue(0)

        self._source.setText(f"Kaynak: {self._source_text()}  ·  {len(turns)} tur")

    def _show_turns(self, turns: list[HistoryTurn]) -> None:
        """Turları geçmiş sekmesine çizer (kart ya da balon, tura göre)."""

        body = "".join(_render_turn(turn, last=index == len(turns) - 1) for index, turn in enumerate(turns))
        self._history.setHtml(body)
        self._stack.setCurrentIndex(1)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt'nin metot adı
        """Kapatma paneli YOK ETMEZ, gizler (tepsi uygulamasında tek örnek kalır)."""

        if self._hide_on_close:
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)

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


def show_panel(
    settings: Settings,
    llm_client: Any | None = None,
    dispatcher: ToolDispatcher | None = None,
) -> ArtemisPanel:
    """Paneli açar; panel bir kez kurulur, sonraki çağrılarda yalnızca öne getirilir.

    Yazı kutusu için `dispatcher` ve `llm_client` birlikte gerekir (bkz.
    `ArtemisPanel.__init__`). İlk kurulumdan sonraki çağrılar bu iki argümanı
    yok sayar: panel tek örnektir ve komut hattı kuruluşta bağlanır.
    """

    global _open_panel
    if _open_panel is None:
        _open_panel = ArtemisPanel(settings, llm_client, dispatcher=dispatcher)

    _open_panel.show()
    _open_panel.raise_()
    _open_panel.activateWindow()
    return _open_panel


def _run_demo() -> None:
    """`python -m ui.panel`: arayüzü tek başına gösterir (komut hattı yok, kutu kapalı)."""

    from config.settings import get_settings

    app = QApplication(sys.argv)
    # Demo'da kapatma gerçekten çıkmalı: tepsi yoktur, gizlenen pencere
    # süreci açık bırakırdı.
    panel = ArtemisPanel(get_settings(), hide_on_close=False)
    panel.show()  # yerel değişken, app.exec() boyunca yaşar
    raise SystemExit(app.exec())


if __name__ == "__main__":
    _run_demo()
