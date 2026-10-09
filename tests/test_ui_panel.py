"""`ui/panel.py` testleri — geçmiş ayrıştırma + panel görünümü.

İki katman:
  1. `parse_history` saf metin okur — `QApplication` gerektirmez.
  2. `ArtemisPanel` canlı pencere kurar; `QT_QPA_PLATFORM=offscreen` ile
     başlıksız CI'da da çalışır, PyQt6 yoksa `importorskip` atlar
     (bkz. `tests/test_ui_hotkey.py`, `tests/test_ui_chat_window.py`).

ÖRNEK LOG: aşağıdaki `_LOG` bu deponun GERÇEK `logs/artemis.log`
satırlarından üç tur seçilerek alınmıştır (`logs/README.md`'deki biçimle
birebir). Test verisi KODA gömülüdür, çalışma anında hiçbir dosya
OKUNMAZ/YAZILMAZ — böylece logu olan ve olmayan iki makinede aynı
sonucu verir.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config.settings import Settings
from ui import theme
from ui.panel import (
    _WINDOW_HEIGHT,
    ArtemisPanel,
    describe_llm,
    describe_voice,
    describe_voice_details,
    parse_history,
)
from ui.settings_window import SettingsWindow

_LOG = """\
2026-09-28 00:00:01,000 | INFO     | __main__ | Artemis başlatıldı. Yüklü tool sayısı: 39
2026-07-26 03:13:04,740 | INFO     | core.voice_loop | Duyulan komut: 'league of legends, aç'
2026-07-26 03:13:11,553 | INFO     | core.voice_loop | LLM planı: [('windows.launch_app', {'name': 'League of Legends'})]
2026-07-26 03:13:11,599 | INFO     | core.dispatcher | Tool çalıştırıldı: windows.launch_app -> success=True
2026-07-26 03:13:22,650 | INFO     | voice.router | Ses sağlayıcı (TTS): bulut
2026-07-26 03:22:33,323 | INFO     | core.voice_loop | Duyulan komut: ''
2026-07-26 03:23:15,290 | INFO     | core.voice_loop | Duyulan komut: 'Vurayım gibi kapat.'
2026-07-26 03:23:26,835 | INFO     | core.dispatcher | Onay gerektiren işlem beklemede: windows.shutdown
2026-07-26 03:23:38,748 | INFO     | core.voice_loop | Sesli onay: tool=windows.shutdown duyulan='' sonuç=REDDEDİLDİ
2026-07-26 03:23:52,100 | INFO     | core.dispatcher | Tool çalıştırıldı: windows.close_app -> success=False
2026-07-26 03:23:52,200 | INFO     | core.voice_loop | Cevap (tam): 'windows.close_app' başarısız oldu; kalan 0 adım durduruldu.
2026-07-26 13:10:00,000 | INFO     | core.voice_loop | Duyulan komut: 'youtube aç'
2026-07-26 13:10:17,900 | INFO     | core.dispatcher | Tool çalıştırıldı: web.search -> success=True
2026-07-26 13:10:18,208 | INFO     | core.voice_loop | Cevap (tam): 'https://www.youtube.com' açıldı.
"""


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "artemis.log"
    path.write_text(_LOG, encoding="utf-8")
    return path


# --- parse_history ---------------------------------------------------------


def test_a_turn_carries_what_the_user_said_the_tools_and_the_reply(log_file: Path) -> None:
    """Logdaki üç tur, üçü de kendi satırından yeniden kurulur.

    Kaydın ters yönde okunması, `tools` listesinin de TERS gelmesi
    demektir: tarama `windows.close_app` (başarısız) gördükten sonra
    `windows.launch_app`'e (başarılı) ulaşır. Kronolojik sıra bu testin
    konusudur.
    """

    turns = parse_history(log_file)

    assert len(turns) == 3, "boş STT içeren tur sayılır ama gömülmez, üç tur kalmalı"
    assert [turn.said for turn in turns] == [
        "league of legends, aç",
        "Vurayım gibi kapat.",
        "youtube aç",
    ]


def test_tools_keep_their_running_order_and_success_flag(log_file: Path) -> None:
    """Başarı/başarısızlık KARIŞTIRILMAZ: `success=False` görünmeli."""

    first, middle, last = parse_history(log_file)

    assert first.tools == [("windows.launch_app", True)]
    # `windows.shutdown` onayda REDDEDİLDİği için logda "Tool çalıştırıldı"
    # satırı YOKTUR — onay köprüsü logu panelin okuduğu üç desenden
    # biri değil. Yani orta turda tek tool görünür (close_app, başarısız).
    assert middle.tools == [("windows.close_app", False)]
    assert last.tools == [("web.search", True)]


def test_reply_and_timestamp_are_carried_verbatim(log_file: Path) -> None:
    """Cevap metni ve zaman damgası KESİLMEZ; sadece biçimlenir."""

    turns = parse_history(log_file)

    assert turns[-1].reply == "'https://www.youtube.com' açıldı."
    assert turns[0].timestamp == "26.07.2026 03:13:04"
    assert turns[0].reply == "", "kısa cevap loga yazılmıyorsa panel de uydurmamalı"


def test_limit_keeps_only_the_most_recent_turns(log_file: Path) -> None:
    """Log 1 MB'a büyüyebilir; eski turlar kesilir, kronoloji bozulmaz."""

    turns = parse_history(log_file, limit=2)

    assert [turn.said for turn in turns] == ["Vurayım gibi kapat.", "youtube aç"]


def test_an_empty_heard_turn_is_dropped_rather_than_shown_as_noise(log_file: Path) -> None:
    """Mikrofondan gelen boş metin (`Duyulan komut: ''`) gösterilmez.

    Bu satır logda GERÇEKTEN var (ses anlaşılmadığında yazılıyor) ve
    kullanıcıya hiçbir bilgi vermez — panelde "ses anlaşılmadı" diye
    tek başına durmasındansa hiç görünmemesi iyidir.
    """

    assert "" not in [turn.said for turn in parse_history(log_file)]


def test_a_log_with_no_conversation_yields_no_turns(tmp_path: Path) -> None:
    """Plugin yükleme satırları olan bir log boş döner; panel bunu
    "henüz kayıt yok" olarak göstermelidir, sahte tur UYDURMAMALIDIR."""

    path = tmp_path / "artemis.log"
    path.write_text(
        "2026-09-28 00:00:01,000 | INFO     | __main__ | Artemis başlatıldı. Yüklü tool sayısı: 39\n",
        encoding="utf-8",
    )

    assert parse_history(path) == []


def test_lines_that_do_not_match_the_format_are_ignored(tmp_path: Path) -> None:
    """Bilinmeyen satırlar turu bozmaz (sürümlenmemiş log, yarım yazım)."""

    path = tmp_path / "artemis.log"
    path.write_text(
        "bu bir log satırı değil\n"
        "\n"
        "2026-07-26 03:13:04,740 | INFO     | core.voice_loop | Duyulan komut: 'merhaba'\n",
        encoding="utf-8",
    )

    assert [turn.said for turn in parse_history(path)] == ["merhaba"]


# --- Durum şeridi metinleri ------------------------------------------------


def test_llm_line_names_the_running_client_not_a_guess(tmp_path: Path) -> None:
    """Çalışan istemci VARKEN o konuşur; yokken ayar yazılır.

    Değer metninde "Beyin: " ÖNEKİ YOK: sütun başlığı zaten "Beyin"
    diyor, önek aynı kelimeyi iki kez yazıyordu.
    """

    settings = Settings(log_dir=tmp_path, llm_provider="local")

    class OllamaLLMClient:  # sadece ADI önemli
        pass

    assert describe_llm(settings, OllamaLLMClient()) == "Yerel (Ollama)"
    assert "istemci başlatılmadı" in describe_llm(settings, None)
    assert "Bulut (OpenRouter)" in describe_llm(
        Settings(log_dir=tmp_path, llm_provider="cloud"), None
    )
    assert not describe_llm(settings, None).startswith("Beyin"), (
        "sütun başlığı zaten 'Beyin'; metin öneki tekrar etmemeli"
    )


def test_voice_line_reflects_the_setting_including_the_off_case(tmp_path: Path) -> None:
    """Sesli mod kapalıyken "açık" yazılmaz — panel modu BAŞLATMAZ.

    Değer metni KISA tutulur: sütun başlığı zaten "Sesli asistan"
    diyor, ayrıca uyandırma sözcüğü ve kısayol ikincil satırda,
    `python main.py --voice` yolu ise tooltip'te.
    """

    off = describe_voice(Settings(log_dir=tmp_path, voice_enabled=False))
    on = describe_voice(Settings(log_dir=tmp_path, voice_enabled=True))
    details = describe_voice_details(Settings(log_dir=tmp_path, voice_enabled=True))

    assert "kapalı" in off.lower()
    assert "Açık" in on
    assert "--voice" not in on, "başlatma yolu değer satırında olmamalı"
    assert "ctrl+alt+a" in details, "kısayol ikincil satırda görünmeli"
    assert "Artemis" in details, "uyandırma sözcüğü ikincil satırda görünmeli"


# --- Panel görünümü --------------------------------------------------------


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Modül boyunca TEK `QApplication`."""

    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp: QApplication, tmp_path: Path) -> ArtemisPanel:
    window = ArtemisPanel(Settings(log_dir=tmp_path, db_path=tmp_path / "m.db"))
    yield window
    window.close()


def test_panel_uses_the_shared_design_language(panel: ArtemisPanel) -> None:
    """Palet ve QSS `ui/theme.py`'den gelir; panel kendi rengini icat etmez."""

    assert panel.styleSheet() == theme.stylesheet()
    assert panel.isWindow(), "çerçevesiz bir katman değil, NORMAL bir pencere olmalı"


def test_panel_has_a_command_box_and_send_button_and_a_read_only_history(panel: ArtemisPanel) -> None:
    """Yazma yüzü YALNIZCA alt kutudur; geçmiş salt okunur kalır.

    Eski tasarımda panelde mesaj kutusu yoktu. Şimdi panelin altında tek bir
    komut kutusu ve "Gönder" düğmesi vardır; komutlar sesli komutla aynı hattan
    geçer (bkz. `tests/test_ui_command_input.py`). Geçmiş yine yazılamaz.
    """

    tabs = panel.findChild(QTabWidget)
    assert tabs is not None
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Geçmiş", "Projeler", "Ayarlar"]

    history = panel.findChild(QTextBrowser)
    assert history is not None, "geçmiş sekmesi bir metin görünümü olmalı"
    assert history.isReadOnly(), "geçmiş SALT OKUNUR"
    assert [button.text() for button in panel.findChildren(QPushButton)].count("Gönder") == 1
    assert panel._input is not None, "alt yazı kutusu olmalı"


def test_settings_tab_reuses_the_existing_window_rather_than_a_copy(
    panel: ArtemisPanel,
) -> None:
    """İkinci sekme, `--settings` yolunun AYNI formudur.

    Yazma yerine yeniden kullanma: ayar kaydetme/yamlama mantığı iki
    kopyada yaşasaydı, biri güncellendiğinde diğeri eski kalırdı.
    """

    tabs = panel.findChild(QTabWidget)
    assert tabs is not None
    settings_tab = tabs.widget(2)  # Geçmiş, Projeler, Ayarlar
    assert settings_tab.findChild(SettingsWindow) is not None


def test_panel_keeps_its_own_size_instead_of_growing_to_the_settings_form(
    panel: ArtemisPanel,
) -> None:
    """Ayar formu panele BÜYÜTMEZ.

    Formdaki satır kaydırmalı ipuçları `minimumSizeHint`'i 919 piksele
    çıkarıyor; sekmeye doğrudan konsaydı pencere o boyuta sürüklenir ve
    geçmiş sekmesi ekranın çoğunu boşa yerleştirirdi. Kaydırma alanı
    hem boyutu sabitler hem de dar ekranda formun tamamına erişimi korur.
    """

    # Tam eşitlik değil: durum şeridindeki `python main.py --voice` ipucu
    # satırı kendi minimumunu getiriyor. Önemli olan, panelin AYAR
    # FORMUNUN minimumu kadar büyümemesi — yani 919'a sürüklenmemesi.
    assert panel.minimumSizeHint().width() < 900
    assert panel.size().height() == _WINDOW_HEIGHT


def test_the_status_strip_cannot_blow_the_window_past_its_own_floor(
    panel: ArtemisPanel,
) -> None:
    """Uzun durum metni pencereyi 823 piksele şişirmemeli.

    GERİ ÇÖZÜLEN KUSUR: durum şeridindeki tek satırlık
    "Sesli asistan: açık · uyandırma … · başlatmak için: python main.py
    --voice" cümlesi sarmalı etiketin boyut ipucunu 676'ya, paneli
    823'e çıkıyordu. Artık hiçbir durum satırı o yolu İÇERMEZ.
    """

    status = " ".join(
        label.text()
        for label in panel.findChildren(QLabel)
        if label.property("role") in {"status", "subtitle", "hint"}
    )

    assert "--voice" not in status, (
        "sesli modu başlatma yolu ekranda olmamalı; tooltip'te durur"
    )
    assert panel.minimumSizeHint().width() <= 640, (
        f"durum şeridi pencereyi {panel.minimumSizeHint().width()} piksele zorluyor"
    )
    assert panel.minimumWidth() == 560


def test_the_settings_form_no_longer_demands_nine_hundred_pixels(
    qapp: QApplication,
) -> None:
    """`--settings` ve panelin ayarlar sekmesi aynı makul genişliği ister.

    GERİ ÇÖZÜLEN KUSUR: en uzun sarmalı ipucu etiketi (793) tek satır
    genişliğine göre boyut ipucu veriyor, açılır listeler en uzun
    seçeneğe (≈250) göre genişliyor ve uzun bir düğme etiketi ("Gelişmiş
    ayarlar için config.yaml dosyasını aç") satırı 748'e çıkarıyordu;
    toplamda form 919 piksele zorlanıyordu. Artık: sarmalı etiketler
    boyut ipucuna katılmıyor, listeler sabit bir karakter tabanına
    bağlı, düğme etiketi kısa. Kalan genişlik GERÇEK içerikten gelir
    (en uzun etiket + en uzun alan), bir kusur değil.
    """

    window = SettingsWindow()
    try:
        assert window.minimumSizeHint().width() < 820, (
            f"ayar formu {window.minimumSizeHint().width()} piksele zorluyor"
        )
        assert window.minimumWidth() == 520
    finally:
        window.close()


def test_history_cards_separate_turns_and_keep_the_timestamp_readable(
    panel: ArtemisPanel, tmp_path: Path
) -> None:
    """Turlar kartla ayrılır, zaman damgası gövde metninden sönük AMA okunur.

    Önceden turlar yalnızca 14 px boşlukla ayrılıyor ve damga 11 px
    `TEXT_MUTED` (kart zemini üzerinde 4.09:1) idi — WCAG AA'nın
    (4.5:1) altında.

    KART `<table>` İLE ÇİZİLİR: Qt `<div>`'in `border`/`padding`'ini
    düşürdüğü için kart çerçevesi kayboluyor ve turlar yapışık okunuyordu.
    `bgcolor` ise `toHtml()` serileştirmesinde KORUNUR; bu yüzden ayrım
    artık burada SAYILABİLİR (eskiden yalnızca yorumla inanılıyordu).
    """

    (tmp_path / "artemis.log").write_text(_LOG, encoding="utf-8")
    panel.reload_history()

    history = panel.findChild(QTextBrowser)
    assert history is not None
    html = history.toHtml()

    timestamps = re.findall(r"\d\d\.\d\d\.\d{4} \d\d:\d\d:\d\d", html)
    assert len(timestamps) == 3, f"üç tur, üç zaman damgası: {timestamps}"
    assert "background-color:#1e1e26" in html, "her tur kart zeminli olmalı"
    assert "font-size:12px" in html, "zaman damgası 12 px (11 px değil)"

    # Kart KENARI artık serileştirilmiş HTML'de korunuyor: Qt `<table>` +
    # `bgcolor` + hücre `border`'ı düşürmüyor. Üç tur => üç kutu.
    # (Qt kenarları dört ayrı kola açar: `border-top:1px` vb.)
    assert html.count("border-top:1px") >= 3, (
        "her turun kart kenarlığı çizilmeli; kenar kaybolursa turlar "
        "birbirine yapışır ve tur sınırı okunmaz"
    )
    # Ayraç: turlar arasında GERÇEK boşluk olmalı, son kart hariç.
    assert html.count("margin-bottom:12px") >= 2, (
        "ardışık turlar arasında dikey boşluk bırakılmalı"
    )


def test_every_body_text_color_clears_wcag_aa(qapp: QApplication) -> None:
    """Gövde metni renkleri WCAG AA (4.5:1) eşiğini geçmeli.

    Renk seçimi gözle değil SAYIYLA yapılır; bu test geriye dönük
    bir koruma: palette yarı saydam bir renk eklendiğinde (ya da bir
    metin rengi `TEXT_MUTED`'a döndürüldüğünde) sessizce AA altına
    düşmesin.
    """

    from ui.panel import _contrast_ratio

    for backdrop in (theme.BG_BASE, theme.BG_PANEL):
        for name in ("TEXT_PRIMARY", "TEXT_SECONDARY"):
            ratio = _contrast_ratio(getattr(theme, name), backdrop)
            assert ratio >= 4.5, f"{name} {backdrop.name()} üzerinde yalnızca {ratio:.2f}:1"

    # 12 px'lik zaman damgası da gövde sayılır.
    assert _contrast_ratio(theme.TEXT_SECONDARY, theme.BG_PANEL) >= 4.5


def test_the_empty_state_is_centered_and_explains_where_records_come_from(
    panel: ArtemisPanel,
) -> None:
    """Boş durum ortalanır ve kaydın NEREDEN geldiğini söyler.

    Önceden metin `margin-top:24px` ile üste yapışıktı; kullanıcı
    "burada bir şey mi eksik" diye bakıp geçiyordu.
    """

    stack = panel._stack
    assert stack.currentIndex() == 0, "log yokken boş sayfa gösterilmeli"

    label = panel._empty
    assert label.text() and "logs/artemis.log" in label.text()

    # Dikey ortalama: etiket, sayfanın dikey ortasına yakın durur.
    page_height = stack.widget(0).height()
    centre = label.y() + label.height() / 2
    assert abs(centre - page_height / 2) < page_height * 0.15, (
        f"boş durum ortalanmamış: merkez {centre:.0f}, sayfa ortası {page_height / 2:.0f}"
    )


def test_a_written_log_switches_the_empty_page_back_to_the_list(
    panel: ArtemisPanel, tmp_path: Path
) -> None:
    """Yenile gerçekten geçiş yapıyor (sabit bir ilk durum değil)."""

    stack = panel._stack
    assert stack.currentIndex() == 0

    (tmp_path / "artemis.log").write_text(_LOG, encoding="utf-8")
    panel.reload_history()

    assert stack.currentIndex() == 1, "kayıt varsa liste sayfası gösterilmeli"


def test_missing_log_shows_an_empty_state_instead_of_inventing_history(
    panel: ArtemisPanel,
) -> None:
    """Log dosyası yokken panel sahte tur BASMAZ, durumu söyler.

    Boş durum artık `QTextBrowser` DEĞİL, ortalanmış ayrı bir
    `QLabel` sayfasıdır; metin oradan okunur.
    """

    assert "Henüz kayıt yok" in panel._empty.text()
    assert "Siz:" not in panel._empty.text(), "uydurulmuş bir konuşma satırı olmamalı"


def _history_tooltips(browser: QTextBrowser) -> set[str]:
    """Geçmiş belgesindeki karakter tooltip'leri (ham tool adları burada tutulur)."""

    found: set[str] = set()
    block = browser.document().begin()
    while block.isValid():
        fragments = block.begin()
        while not fragments.atEnd():
            fragment = fragments.fragment()
            if fragment.isValid() and fragment.charFormat().toolTip():
                found.add(fragment.charFormat().toolTip())
            fragments += 1
        block = block.next()
    return found


def test_a_written_log_is_rendered_into_the_history_tab(
    panel: ArtemisPanel, tmp_path: Path
) -> None:
    """Kayıt varsa ekranda: kullanıcı sözü, araçlar ve cevap görünür."""

    (tmp_path / "artemis.log").write_text(_LOG, encoding="utf-8")
    panel.reload_history()

    history = panel.findChild(QTextBrowser)
    assert history is not None
    text = history.toPlainText()

    assert "league of legends, aç" in text
    assert "Uygulama başlatma" in text and "başarılı" in text
    assert "windows.launch_app" in {tip for tip in _history_tooltips(history)}
    assert "başarısız" in text
    assert "3 tur" in _status_text(panel)


def test_reload_reflects_a_log_that_appeared_after_the_panel_opened(
    panel: ArtemisPanel, tmp_path: Path
) -> None:
    """"Yenile" düğmesi gerçekten yeniden okur (sabit bir ilk durum değil)."""

    assert "Henüz kayıt yok" in panel._empty.text()

    (tmp_path / "artemis.log").write_text(_LOG, encoding="utf-8")
    panel.reload_history()

    history = panel.findChild(QTextBrowser)
    assert history is not None
    assert "league of legends, aç" in history.toPlainText()


def test_status_strip_reports_real_settings_only(panel: ArtemisPanel) -> None:
    """Durum şeridi ayarlardan gelir; hiçbir şey uydurmaz.

    Başlıklar (`Beyin`, `Sesli asistan`) "status" rolünde, DEĞERLER
    "subtitle" rolündedir — iki sütunlu düzen böyle okunur.
    """

    captions = [
        label.text() for label in panel.findChildren(QLabel) if label.property("role") == "status"
    ]
    values = [label.text() for label in panel.findChildren(QLabel) if label.property("role") == "subtitle"]

    assert captions == ["Beyin", "Sesli asistan"]
    # Varsayılan `llm_provider: "auto"` — değer ayardaki seçimi yazar.
    assert any("Otomatik" in text for text in values), values
    assert "Temel LLM" not in "  ".join(captions + values)


def _status_text(panel: ArtemisPanel) -> str:
    """Panelin alt bilgi satırındaki kaynak/tur sayısı metni."""

    return " ".join(
        label.text() for label in panel.findChildren(QLabel) if label.property("role") == "hint"
    )


# --- Onay kutusu görünümü ---------------------------------------------------


def test_checked_and_unchecked_checkboxes_are_visibly_different(
    qapp: QApplication,
) -> None:
    """İşaretli kutu TİK, işaretsiz kutu BOŞ çerçeve olmalı.

    GERİ ÇÖZÜLEN KUSUR: `QCheckBox::indicator:checked` yalnızca dolu
    mavi bir kare veriyordu — ekran görüntüsünde "Sesli asistan" ve
    "uyandırma sözcüğü" kutuları işaretli mi yoksa kapalı mı
    ANLAŞILAMIYORDU. İşaretli hâl bir tik gerektirir; QSS
    `QCheckBox::indicator` için metin basamadığı için tik kodda
    çizilip `image: url(data:…)` olarak verilir.
    """

    from PyQt6.QtWidgets import QCheckBox

    assert "QCheckBox::indicator:checked" in theme.stylesheet()
    assert "image: url(data:image/png;base64," in theme.stylesheet(), (
        "işaretli durumun görünür bir işareti (tik) olmalı"
    )
    # İşaretsiz durum da kendi kuralıyla boş çerçeve çiziyor.
    sheet = theme.stylesheet()
    assert "QCheckBox::indicator {" in sheet
    assert "QCheckBox::indicator:disabled" in sheet, (
        "devre dışı onay kutusu da tanımlı olmalı (panel koyu temada)"
    )

    # TİK GERÇEKTEN ÇİZİLİYOR MU: aynı QSS'u alan iki kutunun
    # işaretli/ işaretsiz hâli farklı piksel veriyor olmalı.
    host = QWidget()
    host.setStyleSheet(theme.stylesheet())
    on = QCheckBox("a")
    on.setChecked(True)
    off = QCheckBox("b")
    off.setChecked(False)
    lay = QVBoxLayout(host)
    lay.addWidget(on)
    lay.addWidget(off)
    host.show()
    try:
        qapp.processEvents()
        assert on.grab().toImage() != off.grab().toImage(), (
            "işaretli ve işaretsiz hâl aynı görünüyor; ayrım kaybolmuş"
        )
    finally:
        host.close()


def test_source_label_hides_a_temp_path_without_faking_the_data(
    qapp: QApplication, tmp_path: Path
) -> None:
    """`source_label` YALNIZCA görünen adı değiştirir, veriyi değil.

    Ekran görüntüsü üretirken örnek log geçici bir klasörde duruyor;
    panelin altında `C:\\Users\\…\\Temp\\tmpXXXX\\artemis.log` yazmasın
    diye `logs/artemis.log` görünür. Panel kodu sahte bir tur TUTMAZ:
    geçmiş yine gerçek dosyadan okunur.
    """

    (tmp_path / "artemis.log").write_text(_LOG, encoding="utf-8")

    real = ArtemisPanel(Settings(log_dir=tmp_path, db_path=tmp_path / "m.db"))
    named = ArtemisPanel(
        Settings(log_dir=tmp_path, db_path=tmp_path / "m.db"),
        source_label="logs/artemis.log",
    )
    try:
        assert str(tmp_path) in _status_text(real), "verilmezse gerçek yol yazılmalı"
        status = _status_text(named)
        assert "logs/artemis.log" in status
        assert str(tmp_path) not in status, "geçici klasör yolu sızmamalı"

        # Veri AYNI: etiket değişti, geçmiş değişmedi.
        real_history = real.findChild(QTextBrowser)
        named_history = named.findChild(QTextBrowser)
        assert real_history is not None and named_history is not None
        assert real_history.toPlainText() == named_history.toPlainText()
    finally:
        real.close()
        named.close()


# --- Tepsi menüsündeki karşılık -------------------------------------------


def test_tray_gets_a_panel_entry_only_when_a_callback_is_given(qapp: QApplication) -> None:
    """Tepsi menüsü panel öğesini `on_panel` verilirse ekler, vermezse eklemez.

    `ui/tray.py` bu davranışı `on_settings` için zaten böyle kurgulamıştı
    (geriye uyum için `None` varsayılan). Aynı kalıba uyulur: eski bir
    çağrı (`on_panel` vermeyen) menüyü bugünkü hâliyle görmeye devam eder.
    """

    from ui.tray import ArtemisTray

    without = ArtemisTray(lambda: None, lambda: None)
    texts = [action.text() for action in without._menu.actions()]

    assert not any("Panel" in text for text in texts), "verilmeyen geri çağırma menü öğesi eklemez"

    opened: list[bool] = []
    with_panel = ArtemisTray(lambda: None, lambda: None, "", lambda: None, lambda: opened.append(True))
    panel_entries = [a.text() for a in with_panel._menu.actions() if "Panel" in a.text()]

    assert len(panel_entries) == 1
    assert "geçmiş" in panel_entries[0].lower()
