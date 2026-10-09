"""Kullanıcıya yönelik, küratörlenmiş bir ayarlar penceresi.

NEDEN BU PENCERE VARSA: `config/config.yaml` bu projenin kullanıcıya
EN AÇIK, EN YOĞUN BELGELENMİŞ yüzeyi — her satırı Türkçe ve açıklamalı.
Ama bir YAML dosyasını elle düzenlemek, ayarını değiştirmek isteyen sıradan
bir kullanıcı için "kod okumak" demek. Bu pencere SIK KULLANILAN bir
kaç ayarı düz bir formda toplar; geri kalan her şey (yollar, `wake_words`,
`mcp_servers` gibi liste/nesne alanları) bilinçli olarak dosyada kalır —
tek bir `QListEdit` benzeri widget, `config.yaml`'ın bu alanlarının
gerçekten karmaşık biçimini (telaffuz varyantları, sunucu komutları)
karşılayamaz, bu yüzden v1'de YAPILMADI.

KAYDETME — DOSYA BOZULMAZ. `apply_config_overrides` bilinçli olarak bir
YAML dump/reparse YAPMAZ: `yaml.safe_load` + `yaml.safe_dump` bir
turda dosyanın bütün yorumlarını silerdi ve bu depo için asıl
belgelendirme yüzeyi olan `config.yaml`'ın kullanıcıya anlatabileceği
her şeyi yok ederdi. Bunun yerine yalnızca ilgili `key: ...` satırları
metin seviyesinde değiştirilir/eklenir (bkz. `apply_config_overrides`
docstring'i).

Tek başına önizleme (uygulama çalışmadan):

    python -m ui.settings_window
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import settings as config_settings
from ui import theme

logger = logging.getLogger(__name__)

_PROVIDER_ITEMS: tuple[tuple[str, str], ...] = (
    ("Otomatik (önce ücretsiz bulut model)", "auto"),
    ("Yalnızca bulut (OpenRouter)", "cloud"),
    ("Yalnızca yerel (Ollama)", "local"),
)
"""`llm_provider` seçenekleri: (görünen Türkçe metin, dosyaya yazılacak
ham jeton). Ekranda Türkçe konuşur, dosyada İngilizce yazar — `config.yaml`
makine-okunur bir sözleşmedir, `Literal["auto", "cloud", "local"]` değerleri
dışında hiçbir şey kabul etmez (bkz. `config/settings.py::Settings`)."""

_STT_ITEMS: tuple[tuple[str, str], ...] = (
    ("Otomatik (önce bulut, gerekirse yerel)", "auto"),
    ("Yalnızca bulut (Groq / Azure)", "cloud"),
    ("Yalnızca yerel (faster-whisper)", "local"),
)

_TTS_ITEMS: tuple[tuple[str, str], ...] = (
    ("Otomatik (önce bulut, gerekirse yerel)", "auto"),
    ("Yalnızca bulut (Edge TTS)", "cloud"),
    ("Yalnızca yerel (Piper)", "local"),
)

_EDGE_VOICE_ITEMS: tuple[tuple[str, str], ...] = (
    ("Emel (kadın)", "tr-TR-EmelNeural"),
    ("Ahmet (erkek)", "tr-TR-AhmetNeural"),
)

_SAVED_STATUS = "Kaydedildi. Bazı değişiklikler için Artemis'i yeniden başlatmanız gerekebilir."


# --------------------------------------------------------------------------
# config.yaml'ı metin seviyesinde yamalamak
# --------------------------------------------------------------------------


def _format_yaml_value(value: Any) -> str:
    """Bir ayar değerini, bu dosyanın kendi yazım kuralına göre YAML'a çevirir.

    Dosyanın kuralı: dizgeler ÇİFT TIRNAKLI (`ollama_model: "gemma4:e4b"`),
    mantıksal değerler ÇIPLAK ve KÜÇÜK HARFLİ (`voice_enabled: true`).
    Burada `str(True)` yazmak "True" üretir — YAML'da bu geçerlidir ama
    dosyanın geri kalanından farklı ve gereksiz gürültü olurdu; ayrıca
    pydantic `Literal`/bool alanlarına "True" yazıldığında davranış
    dosyadan dosyaya değişebilir. Bu yüzden bool AÇIKÇA çevrilir.

    Args:
        value: Yazılacak değer (bu pencere yalnızca `str` ve `bool` üretir).

    Returns:
        YAML metnine doğrudan yapıştırılabilecek değer parçası.
    """

    if isinstance(value, bool):
        # bool, int'in alt türüdür: str kontrolünden ÖNCE ele alınmalıdır.
        return "true" if value else "false"

    text = str(value).strip()
    # Burada genel bir YAML kaçışlayıcı KURULMAZ: değerler yalnızca model
    # adları/ses adları/kısayollar gibi basit dizgelerdir. Yine de bir
    # tırnak, dosyayı geçersiz YAML yapabileceği için savunmacı olarak
    # düşürülür — kullanıcının yazdığı bir şeyi sessizce bozmak yerine
    # onu düzeltmek daha iyidir.
    text = text.replace('"', "")
    return f'"{text}"'


def apply_config_overrides(yaml_text: str, values: dict[str, Any]) -> str:
    """`config.yaml`'ın metnini, verilen anahtar/değer çiftlerini uygulayarak
    döndürür. Dosyanın YORUM VE BİÇİMİNİ bozmaz: tam bir YAML dump/reparse
    YAPMAZ, yalnızca ilgili `key: ...` satırlarını metin seviyesinde
    değiştirir/ekler. Değer türüne göre YAML biçimi seçilir (string
    çift tırnaklı, bool/sayı çıplak) — bu dosyanın kendi kuralına uyar
    (bkz. `ollama_model: "gemma4:e4b"` vs `voice_enabled: true`).

    Neden metin seviyesinde: `yaml.safe_load` + `safe_dump` bir turda
    dosyanın BÜTÜN yorumlarını ve satır düzenini silerdi. `config.yaml`
    bu depoda kullanıcıya dönük bir KILAVUZ olduğu için (her ayarın
    neden var olduğu, neden varsayılan olduğu yazılıdır) bir ayar
    değiştirmek o metni silmemelidir.

    Args:
        yaml_text: Mevcut dosyanın tam metni.
        values: Yazılacak ayar adları ve değerleri.

    Returns:
        Değişikliklerin uygulanmış metin. Aynı değerlerle ikinci kez
        çağrıldığında ÇIKTI BİREBİR AYNI olur (idempotent): eklenen
        anahtarlar ikinci turda artık bulunur, "ekle" değil "değiştir"
        yoluna girer.
    """

    for key, value in values.items():
        formatted = _format_yaml_value(value)

        # Satırın kendisine varsa yalnızca O satır değişir. `(?P<comment>)`
        # grubu, o satırda satır sonunda bir yorum varsa (bugün config.yaml'da
        # YOKTUR, ama ileride eklenebilir) onu düşürmemek için yakalanır.
        key_line = re.compile(
            rf"^(?P<indent>\s*){re.escape(key)}\s*:[ \t]*(?P<rest>.*?)[ \t]*(?P<comment>#.*)?$",
            re.MULTILINE,
        )
        match = key_line.search(yaml_text)
        if match is not None:
            comment = match.group("comment")
            # Yorum dosyanın değerinden biraz uzun olsa da yapıştırılır;
            # iki boşluk, "value # comment" ayrımını gözle okunur kılar.
            suffix = f"  {comment}" if comment else ""
            replacement = f"{match.group('indent')}{key}: {formatted}{suffix}"
            yaml_text = yaml_text[: match.start()] + replacement + yaml_text[match.end() :]
            continue

        # Anahtar dosyada yok: EN SONA eklenir. Neden ortaya değil — dosyanın
        # her bölümü kendi açıklama yığınıyla başlıyor, araya sokmak o
        # yorum bloğunun altına girip neyin neyi anlattığını bulanıklaştırır.
        if yaml_text:
            # Önce ayırıcı boş satır; ama dosya HENÜZ boşsa (ilk yazım)
            # başta boş bir satır bırakmamak için eklenmez.
            if not yaml_text.endswith("\n"):
                yaml_text += "\n"
            yaml_text += "\n"
        yaml_text += f"# Ayarlar penceresinden eklendi:\n{key}: {formatted}\n"

    return yaml_text


def save_overrides(values: dict[str, Any], path: Path | None = None) -> None:
    """Verilen ayarları `config.yaml`'a yazar; dosya yoksa oluşturur.

    Yazma ATOMİKTİR: önce `<path>.tmp` yazılır, sonra `Path.replace`
    (aynı disk üzerinde `os.replace` = atomik) ile taşınır. Yarı kalan
    bir yazma asıl dosyayı bozamaz — `scripts/setup_voice.py` da aynı
    deseni kullanıyor.

    Yazdıktan sonra `get_settings`'in `lru_cache`'i TEMİZLENİR: o önbellek
    bayat kalırsa, aynı süreçte sonradan `get_settings()` çağıran her yer
    (dispatcher, MCP eklentisi, araç katmanı) kullanıcının YENİ değil ESKİ
    ayarı görür ve "kaydettim ama bir şey değişmedi" hatası üretilir.

    Args:
        values: Yazılacak ayar adları ve değerleri.
        path: Hedef dosya. Varsayılan, `config/settings.py` içindeki
            `DEFAULT_CONFIG_PATH` sabitidir — burada YENİDEN İCAT
            EDİLMEZ; modül özniteliği olarak okunur ki testler
            (ve kurulumun kendisi) yolu değiştirebilsin.
    """

    target = path if path is not None else config_settings.DEFAULT_CONFIG_PATH
    existing = target.read_text(encoding="utf-8") if target.exists() else ""

    tmp_path = target.with_name(target.name + ".tmp")
    tmp_path.write_text(apply_config_overrides(existing, values), encoding="utf-8")
    tmp_path.replace(target)

    config_settings.get_settings.cache_clear()


# --------------------------------------------------------------------------
# Pencere
# --------------------------------------------------------------------------


def _make_combo(items: tuple[tuple[str, str], ...], current: str) -> QComboBox:
    """`(görünen metin, ham değer)` çiftlerinden bir açılır liste kurar.

    Ekranda Türkçe görünen metin, seçimin ham değeri `userData` ile taşınır;
    geri okuma `currentData()` ile yapılır — GÖRÜNEN metin ASLA ayrıştırılmaz
    (türkçe karakter, çeviri ya da kısaltma değişikliği ayarı bozardı).

    NEDEN `AdjustToMinimumContentsLength`: varsayılan davranış açılır
    listenin EN UZUN öğesine göre genişlik hesaplar ("Otomatik (önce
    ücretsiz bulut model)" ≈ 250 piksel), dolayısıyla `minimumSizeHint`
    540'a çıkıp hem `--settings` penceresini hem de panelin ayarlar
    sekmesini gereksiz yere genişletiyordu. Burada genişlik sabit bir
    karakter sayısına bağlanır: seçenek metni AÇILIR LİSTEDE tam
    görünür, kapalı kutuda ise kısa bir satır kalır. Metin ASLA
    kesilmez — liste her zaman pencere kadar geniş olabilir.

    `24` karakter, seçili değerin ("Yerel (Ollama)", "Bulut (Groq /
    Azure)", "tr-TR-EmelNeural") üçte ikisini kesmeden alır.
    """

    combo = QComboBox()
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(24)
    for display_text, raw_value in items:
        combo.addItem(display_text, raw_value)

    index = combo.findData(current)
    if index >= 0:
        combo.setCurrentIndex(index)
    return combo


def _with_hint(control: QWidget, hint: str) -> QWidget:
    """Bir kontrolü, altında tek satırlık ipucu etiketiyle birlikte sarmalar."""

    if not hint:
        return control

    container = QWidget()
    box = QVBoxLayout(container)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(4)
    box.addWidget(control)

    hint_label = QLabel(hint)
    hint_label.setProperty("role", "hint")
    hint_label.setWordWrap(True)
    # Aynı sebepten: sarmalı ipucu etiketi, TEK SATIR genişliğine göre
    # boyut ipucu verdiği için formu gereksiz yere genişletiyordu.
    # `Ignored` yalnızca yerleşim ipucunu devre dışı bırakır; metin
    # verilen genişlikte sarılmaya devam eder.
    hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    box.addWidget(hint_label)

    return container


def _section_title(text: str) -> QLabel:
    """Form içindeki bölüm başlığı (`theme.stylesheet` bunu 15px/600 basar)."""

    label = QLabel(text)
    label.setProperty("role", "title")
    return label


class SettingsWindow(QWidget):
    """Sık kullanılan ayarların düz, koyu temalı düzenleme penceresi.

    Overlay'in aksine bu bir KALICI araç penceresidir: işletim sisteminin
    kendi pencere çerçevesi, boyutlandırması ve taşınabilirliği vardır.
    Dolayısıyla `QWidget` + `theme.stylesheet()` yeterlidir — overlay'in
    elle çizilmiş `QPainter` yüzeyine ihtiyacı yoktur.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Artemis — Ayarlar")
        self.setStyleSheet(theme.stylesheet())
        self.setMinimumWidth(520)
        self.resize(620, 640)

        settings = config_settings.get_settings()

        self._llm_provider = _make_combo(_PROVIDER_ITEMS, settings.llm_provider)
        self._openrouter_model = QLineEdit(settings.openrouter_model)
        self._ollama_model = QLineEdit(settings.ollama_model)
        self._voice_enabled = QCheckBox()
        self._voice_enabled.setChecked(settings.voice_enabled)
        self._wake_word_enabled = QCheckBox()
        self._wake_word_enabled.setChecked(settings.wake_word_enabled)
        self._stt_provider = _make_combo(_STT_ITEMS, settings.stt_provider)
        self._tts_provider = _make_combo(_TTS_ITEMS, settings.tts_provider)
        self._edge_tts_voice = _make_combo(_EDGE_VOICE_ITEMS, settings.edge_tts_voice)
        self._voice_hotkey = QLineEdit(settings.voice_hotkey)
        self._command_gate_enabled = QCheckBox()
        self._command_gate_enabled.setChecked(settings.command_gate_enabled)

        # `QLineEdit`, içindeki metnin TAM genişliğini `minimumSizeHint`
        # olarak bildirir; bir model slug'ı (`meta-llama/llama-3.3-70b…`)
        # tek başına 200+ piksel dayatır ve üç alan yan yana toplanınca
        # form 900'ü aşar. Metin zaten yatay kaydırılabilir; sabit bir taban
        # genişlik hem pencereyi makul tutar hem alanı birkaç satıra
        # sığdırma zorlamasından kurtarır.
        for field in (self._openrouter_model, self._ollama_model, self._voice_hotkey):
            field.setMinimumWidth(180)
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(6)
        layout.addWidget(_section_title("Ayarlar"))
        subtitle = QLabel("Sık kullanılan ayarlar. Diğer her şey config.yaml dosyasında.")
        subtitle.setProperty("role", "subtitle")
        # NEDEN `Ignored`: bu etiket `setWordWrap(True)` olduğu için
        # `minimumSizeHint`'i TEK SATIRINA göre (793 piksel) hesaplıyordu
        # ve tüm formu 919 piksele sürüklüyordu — panel o yüzden
        # 823'e zorlanıyor, `--settings` tek başına açıldığında da
        # 919'a geriliyordu. Yerleşime "boyut ipucuna bakma" demek,
        # yalnızca metnin sarması ve pencere boyutunu etkiler; ipucu
        # etiketi DEĞİŞMEZ (düzenleme yüzeyinin alt bilgisi kalır).
        subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(subtitle)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        form.addRow(_section_title("LLM ve model"))
        form.addRow(
            "Beyin (LLM)",
            _with_hint(
                self._llm_provider,
                "Otomatik: önce ücretsiz OpenRouter modeli, başarısız olursa Ollama'ya düşer.",
            ),
        )
        form.addRow(
            "OpenRouter modeli",
            _with_hint(
                self._openrouter_model,
                "Slug'ı https://openrouter.ai/models adresinden birebir kopyalayın.",
            ),
        )
        form.addRow(
            "Yerel model (Ollama)",
            _with_hint(
                self._ollama_model,
                "Etiket tam yazılmalı: `ollama list` çıktısındaki adı kopyalayın.",
            ),
        )

        form.addRow(_section_title("Sesli asistan"))
        form.addRow(
            "Sesli asistan",
            _with_hint(
                self._voice_enabled,
                "Kapalıyken mikrofon hiç açılmaz; Artemis yalnızca metin modunda çalışır.",
            ),
        )
        form.addRow(
            '"Artemis" uyandırma sözcüğü',
            _with_hint(
                self._wake_word_enabled,
                "Kapalıyken uyandırmak için kısayol tuşu gerekir.",
            ),
        )
        form.addRow(
            "Konuşma tanıma (STT)",
            _with_hint(self._stt_provider, "Yerel seçilirse ses hiçbir yere gönderilmez."),
        )
        form.addRow(
            "Sesli okuma (TTS)",
            _with_hint(self._tts_provider, "Otomatik, Edge TTS'i dener; olmazsa yerel Piper'a düşer."),
        )
        form.addRow("TTS sesi", self._edge_tts_voice)
        form.addRow(
            "Uyandırma kısayolu",
            _with_hint(self._voice_hotkey, "Biçim: değiştiriciler + tek tuş, artı ile ayrılır."),
        )
        form.addRow(
            '"Bu asistana mı?" süzgeci',
            _with_hint(
                self._command_gate_enabled,
                "Açıkken duyulan metin, tool seçimine gitmeden önce ayrıca süzülür.",
            ),
        )

        layout.addLayout(form)

        buttons = QHBoxLayout()
        save_button = QPushButton("Kaydet")
        save_button.setProperty("role", "primary")
        save_button.clicked.connect(self._on_save)
        buttons.addWidget(save_button)

        # Uzun metinli ikincil düğme, formu 748 piksele sürüklüyordu ve
        # dar pencerede düğme metni kesiliyordu. Kısa bir etiket + tam
        # metni tooltip'te: anlam aynı, yerleşim rahat.
        open_config_button = QPushButton("config.yaml")
        open_config_button.setToolTip("Gelişmiş ayarlar için config.yaml dosyasını aç")
        open_config_button.clicked.connect(self._on_open_config)
        buttons.addWidget(open_config_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._status = QLabel("")
        self._status.setProperty("role", "hint")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    # ------------------------------------------------------------------

    def _current_values(self) -> dict[str, Any]:
        """Formdaki HER küratörlenmiş ayarı, dosyaya yazılacak halleriyle döndürür."""

        return {
            "llm_provider": self._llm_provider.currentData(),
            "openrouter_model": self._openrouter_model.text().strip(),
            "ollama_model": self._ollama_model.text().strip(),
            "voice_enabled": self._voice_enabled.isChecked(),
            "wake_word_enabled": self._wake_word_enabled.isChecked(),
            "stt_provider": self._stt_provider.currentData(),
            "tts_provider": self._tts_provider.currentData(),
            "edge_tts_voice": self._edge_tts_voice.currentData(),
            "voice_hotkey": self._voice_hotkey.text().strip(),
            "command_gate_enabled": self._command_gate_enabled.isChecked(),
        }

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _on_save(self) -> None:
        """Ayarları `config.yaml`'a yazar.

        Hata DURUMUNDA ALDATMAYAN mesaj gösterilir: yazma başarısızsa
        kullanıcıya "kaydedildi" demek, projedeki en sık tekrarlanan
        yanlışlık sınıfıdır.
        """

        try:
            save_overrides(self._current_values())
        except OSError as exc:
            logger.warning("config.yaml yazılamadı: %s", exc)
            self._set_status(f"Kaydedilemedi: {exc}")
            return

        # Dürüst ol: `llm_provider`/`ollama_model` gibi alanlar açılışta
        # ÇÖZÜMLENİR ve bu pencere çalışan bir oturumu yeniden başlatamaz.
        self._set_status(_SAVED_STATUS)

    def _on_open_config(self) -> None:
        """`config.yaml` dosyasını işletim sisteminin varsayılan uygulamasında açar."""

        path = config_settings.DEFAULT_CONFIG_PATH
        try:
            # Korumasız `os.startfile` YASAK: aynı desen
            # `plugins/filesystem_plugin.py::FilesystemOpenTool`'ta zaten var
            # ve burada da uygulanır.
            os.startfile(path)  # Windows'a özgü; proje Windows masaüstü hedefliyor.
        except OSError as exc:
            logger.warning("config.yaml açılamadı: %s", exc)
            self._set_status(f"'{path}' açılamadı: {exc}")
        except AttributeError:
            self._set_status("Dosya açma yalnızca Windows'ta desteklenir (os.startfile bu platformda yok).")
        else:
            self._set_status(f"'{path}' varsayılan uygulamada açıldı.")


_open_window: SettingsWindow | None = None
"""Açık tek pencereyi tutan referans.

`SettingsWindow().show()` ifadesiyle açılan bir pencere, o ifade
bitince referanssız kalır ve Python toplayıcısı onu SİLMEK ZORUNDA
kalır — görünür pencere bir anda yok olur (Qt parent'ı olmadığı için
onu tutacak bir C++ referansı da yoktur). Tepsi menüsüne verilecek
`on_settings` geri çağırması bu yüzden `main.py`'de `SettingsWindow()`
yazmak yerine `ui.settings_window.show_settings()` olmalıdır.
"""


def show_settings() -> SettingsWindow:
    """Ayar penceresini açar (aynı anda yalnızca bir tane bulunur).

    Returns:
        Açılan pencere. Çağıran, sonrasında ne yapacağına karar vermek
        isterse kullanabilir; ama pencere `main.py`'nin `app.exec()`
        döngüsü boyunca yaşamaya devam eder.
    """

    global _open_window
    if _open_window is None:
        _open_window = SettingsWindow()

    _open_window.show()
    _open_window.raise_()
    _open_window.activateWindow()
    return _open_window


def _run_demo() -> None:
    """`python -m ui.settings_window`: ayar penceresini tek başına gösterir."""

    app = QApplication([])
    show_settings()
    sys_exit_code = app.exec()
    raise SystemExit(sys_exit_code)


if __name__ == "__main__":
    _run_demo()
