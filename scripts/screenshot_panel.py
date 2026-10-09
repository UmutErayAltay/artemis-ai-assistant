"""`ui/panel.py` ekran görüntüsü üretir — GERÇEK PENCERE AÇMAZ.

    python scripts/screenshot_panel.py

`QT_QPA_PLATFORM=offscreen` ile üç kare kaydeder:
`docs/screenshots/panel.png` (geçmişli), `panel-empty.png` (boş durum)
ve `panel-ayarlar.png` (ayarlar sekmesi).

NEDEN AYRI BİR SCRIPT: README'deki görsel elle üretilmiş bir ekran
görüntüsü olmamalı. Uydurma veri YALNIZCA burada üretilir (gerçek
`artemis.log`'dan kopyalanan satırlar); `ui/panel.py` içinde sahte bir
kayıt yoktur — panel yalnızca gerçekten var olan logu okur.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "2")  # README'de net görünsün
# offscreen eklentisi Windows fontlarını kendiliğinden bulmaz; yazı yerine kutucuk (□) çıkar.
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QTabWidget

from config.settings import Settings
from ui.panel import _DEFAULT_SOURCE_LABEL, ArtemisPanel
from ui.settings_window import SettingsWindow

_SOURCE_LABEL = _DEFAULT_SOURCE_LABEL
"""Alt satırda görünecek kaynak adı.

Örnek log geçici bir klasörde durduğu için GERÇEK yol
`C:\\Users\\…\\AppData\\…\\Temp\\…` olurdu; README görüntüsünde o yol
kötü duruyor. Panelin `source_label` parametresi yalnızca GÖRÜNEN
addı değiştirir — okunan dosya aynıdır, panel kodu sahte veri tutmaz."""


# Bu satırlar `logs/artemis.log`'dan BİREBİR kopyalandı (gösterilecek turlar
# seçilip kısaltıldı). `parse_history` yalnızca bu üç biçimi tanıdığı için
# örnek de gerçek kayıtla AYNI biçimde olmak zorunda.
_SAMPLE_LOG = """\
2026-07-26 03:13:04,740 | INFO     | core.voice_loop | Duyulan komut: 'league of legends, aç'
2026-07-26 03:13:11,599 | INFO     | core.dispatcher | Tool çalıştırıldı: windows.launch_app -> success=True
2026-07-26 03:22:33,323 | INFO     | core.voice_loop | Duyulan komut: ''
2026-07-26 03:23:15,290 | INFO     | core.voice_loop | Duyulan komut: 'Vurayım gibi kapat.'
2026-07-26 03:23:26,835 | INFO     | core.dispatcher | Onay gerektiren işlem beklemede: windows.shutdown
2026-07-26 03:23:38,748 | INFO     | core.voice_loop | Sesli onay: tool=windows.shutdown duyulan='' sonuç=REDDEDİLDİ
2026-07-26 03:23:52,100 | INFO     | core.dispatcher | Tool çalıştırıldı: windows.close_app -> success=False
2026-07-26 03:23:52,200 | INFO     | core.voice_loop | Cevap (tam): 'windows.close_app' başarısız oldu; kalan 0 adım durduruldu.
2026-07-26 13:10:00,000 | INFO     | core.voice_loop | Duyulan komut: 'youtube aç ve guvenlik nasil yapilir diye sor'
2026-07-26 13:10:12,000 | INFO     | core.dispatcher | Tool çalıştırıldı: web.search -> success=True
2026-07-26 13:10:14,000 | INFO     | core.dispatcher | Tool çalıştırıldı: web.open -> success=True
2026-07-26 13:10:18,208 | INFO     | core.voice_loop | Cevap (tam): 'https://www.youtube.com' açıldı. Güvenlik ayarları için https://www.youtube.com/account_privacy adresine bakabilirsin.
"""


def _grab(panel: ArtemisPanel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    panel.show()
    panel.raise_()
    panel.grab().save(str(path))
    print(f"{path} ({path.stat().st_size} bayt)")


def _show_both_checkbox_states(form: SettingsWindow) -> None:
    """Görüntüde onay kutularının İKİ hâli birden görünsün.

    NEDEN: `config.yaml`'da üç onay kutusu da `true` olduğu için düz bir
    ekran görüntüsü yalnızca "dolu mavi kare" gösterirdi ve işaretli ile
    işaretsiz ayrımı GÖRÜLEMEZDI — okuyucu kutuya bakıp "bu açık mı
    kapalı mı" diye kendi başına karar veremezdi.

    Burada `setChecked()` yalnızca EKRAN GÖRÜNTÜSÜ için değiştirilir;
    hiçbir şey KAYDEDİLMEZ, `config.yaml`'a dokunulmaz ve pencere
    kapanırken ayar dosyasına yazılmaz. Rastgele iki kutu seçilmez:
    işaretli bırakılan bir kutu "şu özellik açık" bilgisini korur.
    """

    form._command_gate_enabled.setChecked(False)


def main() -> None:
    app = QApplication(sys.argv)
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)

        # Boş durum: log dosyasi HIC YOK. Panel uydurma tur basmaz.
        empty = ArtemisPanel(
            Settings(log_dir=log_dir, db_path=log_dir / "m.db"),
            source_label=_SOURCE_LABEL,
        )
        _grab(empty, out_dir / "panel-empty.png")
        empty.close()

        # Dolu durum: gecmise sahip bir log.
        (log_dir / "artemis.log").write_text(_SAMPLE_LOG, encoding="utf-8")
        filled = ArtemisPanel(
            Settings(log_dir=log_dir, db_path=log_dir / "m.db"),
            source_label=_SOURCE_LABEL,
        )
        _grab(filled, out_dir / "panel.png")

        # Ayarlar sekmesi ayni pencerenin ikinci yuzudur; ayri bir ekran
        # goruntusu olarak kaydedilir, README ikisini de gosterir.
        tabs = filled.findChild(QTabWidget)
        tabs.setCurrentIndex(1)
        form = tabs.widget(1).findChild(SettingsWindow)
        if form is not None:
            _show_both_checkbox_states(form)
        app.processEvents()
        _grab(filled, out_dir / "panel-ayarlar.png")
        filled.close()

    del app


if __name__ == "__main__":
    main()
