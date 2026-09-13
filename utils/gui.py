"""`pyautogui` çağrılarını tembel import ederek çalıştıran paylaşılan yardımcı.

NEDEN AYRI BİR MODÜL: "önce `pyautogui`'yi tembel import et, sonra çağrıyı
`try/except Exception` içinde yap" kalıbı `mouse_keyboard_plugin.py`'de
BEŞ, `windows_plugin.py`'de İKİ, `browser_plugin.py`'de BİR yerde —
toplam SEKİZ kez — birebir aynı şekilde tekrarlanıyordu. Bu yardımcı o
tekrarı tek bir yerde toplar. Import KENDİSİ yine bu fonksiyonun
GÖVDESİNDE (çağrı anında) yapılır — yani CLAUDE.md'nin "Windows'a özgü
bağımlılıklar lazy import edilir" kuralı korunur; `utils/gui.py` modül
seviyesinde ASLA `import pyautogui` yapmaz.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def run_pyautogui(action: Callable[[Any], T], failure_prefix: str) -> tuple[T | None, str | None]:
    """`pyautogui`'yi tembel import eder ve `action(pyautogui)`'yi çalıştırır.

    Args:
        action: `pyautogui` modülünü parametre olarak alıp gerçek çağrı(lar)ı
            yapan bir fonksiyon (genelde küçük bir `lambda` ya da iç içe
            `def`). Dönüş değeri olduğu gibi çağırana iletilir.
        failure_prefix: Bir istisna oluşursa (kurulu değil, ekran/girdi
            erişimi kısıtlı, ya da çağrının kendisi başarısız) kullanıcıya
            gösterilecek mesajın başı (örn. "İmleç taşınamadı").

    Returns:
        `(sonuç, None)` başarılıysa; `(None, hata_mesajı)` aksi halde.
    """

    try:
        import pyautogui

        return action(pyautogui), None
    except Exception as exc:  # noqa: BLE001 - ekran/girdi erişimi bu oturumda kısıtlı olabilir
        return None, f"{failure_prefix}: {exc}"
