"""Plan adımlarının tool adlarını ekranda okunaklı Türkçe etiketlere çevirir.

NEDEN AYRI BİR MODÜL: etiket tablosu sesli döngüde (`core/voice_loop.py`) adım
listesini hazırlarken lazım; overlay ise yalnızca hazır etiketi çizer. Tabloyu
`ui/overlay.py` içine koymak, sesli döngünün PyQt'ye bağımlı olmasını gerektirirdi.
Bu modül saf Python'dır ve Qt yüklemeden test edilir.

Tabloda olmayan bir tool (ör. ileride eklenen ya da MCP üzerinden gelen) adıyla
gösterilir: yanlış ya da uydurma bir etiketten, ham adın kendisi daha dürüsttür.
"""

from __future__ import annotations

_TOOL_LABELS: dict[str, str] = {
    "web.open_url": "Tarayıcıda açma",
    "web.search": "Web'de arama",
    "browser.new_tab": "Tarayıcıda yeni sekme",
    "browser.close_tab": "Sekmeyi kapatma",
    "browser.go_back": "Tarayıcıda geri gitme",
    "browser.go_forward": "Tarayıcıda ileri gitme",
    "browser.refresh": "Sayfayı yenileme",
    "browser.switch_tab": "Sekme değiştirme",
    "filesystem.open": "Dosyayı açma",
    "filesystem.create_folder": "Klasör oluşturma",
    "filesystem.create_file": "Dosya oluşturma",
    "filesystem.search": "Dosya arama",
    "filesystem.copy": "Kopyalama",
    "filesystem.rename": "Yeniden adlandırma",
    "filesystem.move": "Taşıma",
    "filesystem.delete": "Silme",
    "windows.launch_app": "Uygulama başlatma",
    "windows.close_app": "Uygulamayı kapatma",
    "windows.lock": "Ekranı kilitleme",
    "windows.sleep": "Uyku moduna alma",
    "windows.shutdown": "Bilgisayarı kapatma",
    "windows.restart": "Yeniden başlatma",
    "windows.set_volume": "Ses ayarı",
    "windows.set_brightness": "Parlaklık ayarı",
    "windows.screenshot": "Ekran görüntüsü alma",
    "windows.clipboard_copy": "Panoya kopyalama",
    "windows.list_windows": "Pencereleri listeleme",
    "windows.focus_window": "Pencereye geçme",
    "windows.arrange_window": "Pencere düzenleme",
    "mouse_keyboard.move_mouse": "Fareyi taşıma",
    "mouse_keyboard.click": "Tıklama",
    "mouse_keyboard.type_text": "Yazma",
    "mouse_keyboard.press_key": "Tuşa basma",
    "mouse_keyboard.scroll": "Kaydırma",
    "memory.remember": "Not kaydetme",
    "memory.recall": "Notlara bakma",
    "memory.forget": "Notu silme",
    "assistant.reply": "Yanıt verme",
    "kule.status": "Kule durumu",
    "proje.sor": "Proje sorusu",
    "proje.islem": "Proje işlemi",
}


def tool_label(tool_name: str) -> str:
    """Bir tool adının ekranda gösterilecek Türkçe etiketini döndürür.

    Args:
        tool_name: Nokta notasyonlu tool adı, örn. `"web.open_url"`.

    Returns:
        Tabloda varsa Türkçe etiket; yoksa tool adının kendisi.
    """

    return _TOOL_LABELS.get(tool_name, tool_name)
