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
from pathlib import Path

import pytest

pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
)

from config.settings import Settings
from ui import theme
from ui.panel import _WINDOW_HEIGHT, ArtemisPanel, describe_llm, describe_voice, parse_history
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
    """Çalışan istemci VARKEN o konuşur; yokken ayar yazılır."""

    settings = Settings(log_dir=tmp_path, llm_provider="local")

    class OllamaLLMClient:  # sadece ADI önemli
        pass

    assert "Yerel (Ollama)" in describe_llm(settings, OllamaLLMClient())
    assert "istemci başlatılmadı" in describe_llm(settings, None)
    assert "Bulut (OpenRouter)" in describe_llm(
        Settings(log_dir=tmp_path, llm_provider="cloud"), None
    )


def test_voice_line_reflects_the_setting_including_the_off_case(tmp_path: Path) -> None:
    """Sesli mod kapalıyken "açık" yazılmaz — panel modu BAŞLATMAZ."""

    off = describe_voice(Settings(log_dir=tmp_path, voice_enabled=False))
    on = describe_voice(Settings(log_dir=tmp_path, voice_enabled=True))

    assert "kapalı" in off
    assert "--voice" in on, "sesli modu açma yolu gösterilmeli"


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


def test_panel_has_no_message_box_and_no_send_button(panel: ArtemisPanel) -> None:
    """Asıl istek: yazma yüzü KALDIRILDI.

    Panel bir sohbet arayüzü değil; mesaj kutusu ve "Gönder" düğmesi
    bulunmamalı. (Ayar formundaki `QLineEdit`'ler bu aramayı bozmaz —
    onlar ayar DEĞERİ alanlarıdır, mesaj yazmaya yaramaz.)
    """

    tabs = panel.findChild(QTabWidget)
    assert tabs is not None
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Geçmiş", "Ayarlar"]

    history = panel.findChild(QTextBrowser)
    assert history is not None, "geçmiş sekmesi bir metin görünümü olmalı"
    assert history.isReadOnly(), "geçmiş SALT OKUNUR"
    assert [button.text() for button in panel.findChildren(QPushButton)].count("Gönder") == 0
    # Ayar sekmesindeki `QLineEdit`'ler ayar DEĞERİ alanlarıdır (model adı,
    # kısayol) — mesaj yazmaya yaramaz; yalnızca geçmiş sekmesinde
    # yazılabilir bir yüz olmadığı önemlidir ve o yüz `QTextBrowser`'dır.


def test_settings_tab_reuses_the_existing_window_rather_than_a_copy(
    panel: ArtemisPanel,
) -> None:
    """İkinci sekme, `--settings` yolunun AYNI formudur.

    Yazma yerine yeniden kullanma: ayar kaydetme/yamlama mantığı iki
    kopyada yaşasaydı, biri güncellendiğinde diğeri eski kalırdı.
    """

    tabs = panel.findChild(QTabWidget)
    assert tabs is not None
    settings_tab = tabs.widget(1)
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


def test_missing_log_shows_an_empty_state_instead_of_inventing_history(
    panel: ArtemisPanel,
) -> None:
    """Log dosyası yokken panel sahte tur BASMAZ, durumu söyler."""

    history = panel.findChild(QTextBrowser)
    assert history is not None

    text = history.toPlainText()
    assert "Henüz kayıt yok" in text
    assert "Siz:" not in text, "uydurulmuş bir konuşma satırı olmamalı"


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
    assert "windows.launch_app" in text and "başarılı" in text
    assert "başarısız" in text
    assert "3 tur" in _status_text(panel)


def test_reload_reflects_a_log_that_appeared_after_the_panel_opened(
    panel: ArtemisPanel, tmp_path: Path
) -> None:
    """"Yenile" düğmesi gerçekten yeniden okur (sabit bir ilk durum değil)."""

    history = panel.findChild(QTextBrowser)
    assert history is not None
    assert "Henüz kayıt yok" in history.toPlainText()

    (tmp_path / "artemis.log").write_text(_LOG, encoding="utf-8")
    panel.reload_history()

    assert "league of legends, aç" in history.toPlainText()


def test_status_strip_reports_real_settings_only(panel: ArtemisPanel) -> None:
    """Durum şeridi ayarlardan gelir; hiçbir şey uydurmaz."""

    labels = [label.text() for label in panel.findChildren(QLabel) if label.property("role") == "subtitle"]
    joined = "  ".join(labels)

    assert any(text.startswith("Beyin:") for text in labels)
    assert any(text.startswith("Sesli asistan:") for text in labels)
    assert "Temel LLM" not in joined


def _status_text(panel: ArtemisPanel) -> str:
    """Panelin alt bilgi satırındaki kaynak/tur sayısı metni."""

    return " ".join(
        label.text() for label in panel.findChildren(QLabel) if label.property("role") == "hint"
    )


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
