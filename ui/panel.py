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

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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
_WINDOW_HEIGHT = 640
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


def _on_background(color: QColor) -> str:
    """Yarı saydam bir palet rengini panel zemini üzerinde KARIŞTIRIR.

    `theme.TEXT_SECONDARY`/`TEXT_MUTED` alfa kanallıdır ve `QColor.name()`
    o kanalı düşürürdü — açık bir renk paletten ödünç alınıp zeminle
    harmanlanmazsa yanlış (çok parlak) görünür. `QTextBrowser`'ın HTML'i
    `rgba()`'yı sürümden sürüme farklı yorumladığı için harmanlama
    burada, bir kez yapılır.
    """

    weight = color.alphaF()
    channels = [
        round(getattr(color, name)() * weight + getattr(theme.BG_BASE, name)() * (1 - weight))
        for name in ("red", "green", "blue")
    ]
    return f"rgb({channels[0]}, {channels[1]}, {channels[2]})"


def _render_turn(turn: HistoryTurn) -> str:
    """Tek bir turu, `QTextBrowser`'ın gösterebileceği HTML'e çevirir."""

    accent = theme.ACCENT_TEAL.name()
    muted = _on_background(theme.TEXT_MUTED)
    secondary = _on_background(theme.TEXT_SECONDARY)

    lines = [
        f'<div style="color:{muted}; font-size:11px; margin-bottom:2px;">'
        f"{html.escape(turn.timestamp)}</div>"
    ]

    said = turn.said.strip()
    lines.append(
        f'<div><span style="color:{accent};">Siz:</span> '
        f"{html.escape(said) if said else '— ses anlaşılmadı —'}</div>"
    )

    for tool, ok in turn.tools:
        mark = "başarılı" if ok else "başarısız"
        color = theme.ACCENT_GREEN.name() if ok else theme.ACCENT_RED.name()
        lines.append(
            f'<div style="color:{muted}; margin-left:12px;">'
            f"{html.escape(tool)} <span style=\"color:{color};\">({mark})</span></div>"
        )

    if turn.reply:
        lines.append(
            f'<div style="color:{secondary}; margin-top:2px;">'
            f"{html.escape(turn.reply)}</div>"
        )

    return '<div style="margin-bottom:14px;">%s</div>' % "".join(lines)


def _render_empty_state(headline: str, detail: str) -> str:
    """Kayıt yokken gösterilen tek paragraf — kutu, ikon, süslü boşluk yok."""

    return (
        f'<div style="color:{_on_background(theme.TEXT_SECONDARY)};'
        f' margin-top:24px; text-align:center;">'
        f'<div style="font-size:14px;">{html.escape(headline)}</div>'
        f'<div style="font-size:12px; margin-top:6px;">{html.escape(detail)}</div></div>'
    )


def describe_llm(settings: Settings, llm_client: Any | None) -> str:
    """Durum şeridi için LLM satırı.

    Çalışan bir istemci varsa O konuşur (gerçek durum); yoksa ayardaki
    seçim yazılır. İkisi de elde olan gerçek bilgidir — panel, hangi
    sağlayıcının kullanılacağını TAHMİN etmez.
    """

    if llm_client is not None:
        label = _PROVIDER_LABELS.get(type(llm_client).__name__, type(llm_client).__name__)
        return f"Beyin: {label}"

    mode = _PROVIDER_MODE_LABELS.get(settings.llm_provider, settings.llm_provider)
    return f"Beyin: {mode} (istemci başlatılmadı)"


def describe_voice(settings: Settings) -> str:
    """Durum şeridi için sesli asistan satırı.

    Dürüst ol: panel sesli modu BAŞLATMAZ, yalnızca ayarın ne olduğunu
    söyler. Aksi halde "Sesli asistan: açık" yazısı, aslında mikrofonun
    hiç açılmadığı bir pencerede yanlış bilgi verirdi.
    """

    if not settings.voice_enabled:
        return "Sesli asistan: kapalı (voice_enabled: false)"

    return (
        f'Sesli asistan: açık  ·  uyandırma "Artemis"  ·  '
        f"kısayol {settings.voice_hotkey}  ·  başlatmak için: python main.py --voice"
    )


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
        self.resize(_WINDOW_WIDTH, _WINDOW_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

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
        title.setProperty("role", "title")
        return title

    def _build_status(self) -> QWidget:
        container = QWidget()
        box = QVBoxLayout(container)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        for text in (describe_llm(self._settings, self._llm_client), describe_voice(self._settings)):
            label = QLabel(text)
            label.setProperty("role", "subtitle")
            label.setWordWrap(True)  # model slug'ları uzun, taşmasın
            box.addWidget(label)

        return container

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()

        self._history = QTextBrowser()
        self._history.setOpenExternalLinks(False)
        self._history.setOpenLinks(False)
        tabs.addTab(self._history, "Geçmiş")

        # `SettingsWindow` normal bir `QWidget`; sekmeye YERLEŞTİRMEK onu
        # yeniden yazmaz, aynı pencereyi kullanır. Dolayısıyla `--settings`
        # yolu ve panelin ayarlar sekmesi TEK formu, TEK kaydetme yolunu
        # paylaşır.
        #
        # NEDEN KAYDIRMA ALANI: formdaki ipucu etiketleri
        # `setWordWrap(True)` olduğu için `minimumSizeHint` 919 piksele
        # çıkıyor. Doğrudan sekmeye konursa panel de o boyuta GERİ
        # SÜRÜKLENİR ve geçmiş sekmesi ekranın çoğunu boşa yerleştirir.
        # Kaydırma, pencereyi 760 pikselde tutarken dar ekranda da formun
        # tamamına erişimi korur.
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(SettingsWindow())
        tabs.addTab(settings_scroll, "Ayarlar")

        return tabs

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._source = QLabel("")
        self._source.setProperty("role", "hint")
        self._source.setWordWrap(True)
        row.addWidget(self._source, stretch=1)

        refresh = QPushButton("Yenile")
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
        """

        path = self.log_path

        if not path.exists():
            self._history.setHtml(
                _render_empty_state(
                    "Henüz kayıt yok.",
                    "Konuşmalar logs/artemis.log dosyasına yazılır; ilk konuşmadan sonra burada görünür.",
                )
            )
            self._source.setText(f"Kaynak: {path}")
            return

        try:
            turns = parse_history(path)
        except OSError as exc:
            logger.warning("Konuşma geçmişi okunamadı (%s): %s", path, exc)
            self._history.setHtml(
                _render_empty_state("Geçmiş okunamadı.", f"{path} açılamadı: {exc}")
            )
            self._source.setText(f"Kaynak: {path}")
            return

        if not turns:
            self._history.setHtml(
                _render_empty_state(
                    "Bu logda konuşma kaydı yok.",
                    "Dosya var ama içinde okunabilir bir tur bulunamadı.",
                )
            )
        else:
            body = "".join(_render_turn(turn) for turn in turns)
            self._history.setHtml(f'<div style="padding: 2px 6px;">{body}</div>')

        self._source.setText(f"Kaynak: {path}  ·  {len(turns)} tur")


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
