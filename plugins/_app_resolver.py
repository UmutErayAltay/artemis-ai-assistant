"""Kullanıcı dostu uygulama adını gerçek çalıştırılabilir yola çevirir.

`windows.launch_app`'ın eskiden yaşadığı sorun: "lol", "riot client",
"league of legends" gibi kullanıcı ifadeleri doğrudan Windows'un `start`
komutuna gönderiliyordu. `start`, tam adı bilmediği bir isim için
KOMUT SATIRINDA sessizce başarısız oluyor (hata kendi stdout'una yazılıyor,
Python tarafında yakalanmıyor) — bu da "başlatıldı" diye yanlış-pozitif
bir sonuca yol açıyordu.

Bu modül üç kaynağı sırayla dener ve GERÇEK bir dosya yoluna ulaşırsa
`os.startfile()` ile başlatır (bu, dosya yoksa GERÇEKTEN exception
fırlatır — artık sessiz başarısızlık yok):

    1) Bilinen sistem komutları (notepad, calc, explorer, chrome...) —
       bunlar PATH üzerinden güvenle çözülür, ekstra arama gerekmez.
    2) Windows Registry "App Paths" anahtarı — çoğu kurulan uygulama
       (Opera, Chrome, VS Code, Discord...) kendini buraya kaydeder.
    3) Başlat Menüsü kısayıları (.lnk) arasında bulanık (fuzzy) arama —
       Riot Client, oyunlar gibi App Paths'e kaydolmayan ama Başlat
       Menüsü'nde kısayolu olan her uygulamayı kapsar.

Bir `_ALIASES` sözlüğü, "lol" -> "league of legends" gibi günlük dil
kısaltmalarını kısayol adlarıyla eşleşecek forma çevirir.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

_ALIASES: dict[str, str] = {
    "lol": "league of legends",
    "league": "league of legends",
    "leauge of legends": "league of legends",  # yaygın yazım hatası
    "riot": "riot client",
    "riot games": "riot client",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "not defteri": "notepad",
    "hesap makinesi": "calculator",
    "dosya gezgini": "explorer",
    "google chrome": "chrome",
}

# Bunlar PATH/App-Paths üzerinden güvenle çözülür; ekstra arama gerekmez.
_SYSTEM_COMMANDS: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "chrome": "chrome",
}


class AppResolver:
    """Uygulama adını (sistem komutu) veya (gerçek dosya yolu) olarak çözer.

    Başlat Menüsü kısayol taraması (registry'ye göre yavaş) yalnızca ilk
    ihtiyaç duyulduğunda yapılır ve nesne ömrü boyunca önbelleğe alınır
    (`plugins/windows_plugin.py` bu sınıfı modül seviyesinde, tek bir
    örnek olarak paylaşır).
    """

    def __init__(self) -> None:
        self._shortcut_index: dict[str, Path] | None = None

    def resolve(self, raw_name: str) -> tuple[str | None, Path | None]:
        """Girdi adını çözer.

        Returns:
            `(sistem_komutu, kısayol_yolu)` çifti. Tam olarak biri
            dolu olur; ikisi de None ise çözülemedi demektir.
        """

        normalized = raw_name.strip().lower()
        normalized = _ALIASES.get(normalized, normalized)

        if normalized in _SYSTEM_COMMANDS:
            return _SYSTEM_COMMANDS[normalized], None

        registry_path = self._search_registry(normalized)
        if registry_path is not None:
            return None, registry_path

        shortcut_path = self._search_shortcuts(normalized)
        if shortcut_path is not None:
            return None, shortcut_path

        return None, None

    def suggestions(self, raw_name: str, limit: int = 3) -> list[str]:
        """Çözülemediğinde kullanıcıya önermek için en yakın kısayol adlarını döndürür."""

        self._ensure_shortcut_index()
        names = list(self._shortcut_index.keys()) if self._shortcut_index else []
        return difflib.get_close_matches(raw_name.strip().lower(), names, n=limit, cutoff=0.3)

    @staticmethod
    def _search_registry(name: str) -> Path | None:
        """Windows Registry "App Paths" anahtarında ada göre arar."""

        try:
            import winreg
        except ImportError:
            return None  # Windows dışı bir ortam (örn. test sandbox'ı)

        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, base) as apps_key:
                    count = winreg.QueryInfoKey(apps_key)[0]
                    for i in range(count):
                        subkey_name = winreg.EnumKey(apps_key, i)
                        if name in subkey_name.lower():
                            with winreg.OpenKey(apps_key, subkey_name) as subkey:
                                value, _ = winreg.QueryValueEx(subkey, None)
                                if value:
                                    return Path(value)
            except OSError:
                continue
        return None

    def _ensure_shortcut_index(self) -> None:
        """Başlat Menüsü'ndeki tüm .lnk dosyalarını (ad -> hedef yol) olarak önbelleğe alır."""

        if self._shortcut_index is not None:
            return

        self._shortcut_index = {}
        search_dirs = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ]

        try:
            import win32com.client

            shell = win32com.client.Dispatch("WScript.Shell")
        except ImportError:
            return  # pywin32 yoksa (örn. test ortamı) sessizce boş bırak

        for base_dir in search_dirs:
            if not base_dir.exists():
                continue
            for lnk_path in base_dir.rglob("*.lnk"):
                try:
                    shortcut = shell.CreateShortcut(str(lnk_path))
                    target = shortcut.Targetpath
                except Exception:  # noqa: BLE001 - bozuk/erişilemeyen kısayollar atlanır
                    continue
                if target:
                    self._shortcut_index[lnk_path.stem.lower()] = Path(target)

    def _search_shortcuts(self, name: str) -> Path | None:
        self._ensure_shortcut_index()
        if not self._shortcut_index:
            return None

        if name in self._shortcut_index:
            return self._shortcut_index[name]

        close = difflib.get_close_matches(name, self._shortcut_index.keys(), n=1, cutoff=0.5)
        if close:
            return self._shortcut_index[close[0]]

        # Kaba alt-dizge eşleşmesi: "league of legends" arıyorsak ve kısayol
        # adı "League of Legends (TR)" ise difflib oranı düşük çıkabilir.
        for key, path in self._shortcut_index.items():
            if name in key or key in name:
                return path

        return None
