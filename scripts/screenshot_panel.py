"""`ui/panel.py` ekran görüntüsü üretir — GERÇEK PENCERE AÇMAZ.

    python scripts/screenshot_panel.py

`QT_QPA_PLATFORM=offscreen` ile iki kare kaydeder:
`docs/screenshots/panel.png` (geçmişli) ve `panel-empty.png` (boş durum).

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from config.settings import Settings  # noqa: E402
from ui.panel import ArtemisPanel  # noqa: E402

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


def main() -> None:
    app = QApplication(sys.argv)
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)

        # Boş durum: log dosyasi HIC YOK. Panel uydurma tur basmaz.
        empty = ArtemisPanel(Settings(log_dir=log_dir, db_path=log_dir / "m.db"))
        _grab(empty, out_dir / "panel-empty.png")
        empty.close()

        # Dolu durum: gecmise sahip bir log. Ayarlar sekmesi de gorunur.
        (log_dir / "artemis.log").write_text(_SAMPLE_LOG, encoding="utf-8")
        filled = ArtemisPanel(Settings(log_dir=log_dir, db_path=log_dir / "m.db"))
        _grab(filled, out_dir / "panel.png")
        filled.close()

    del app


if __name__ == "__main__":
    main()
