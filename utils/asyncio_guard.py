"""`asyncio.run()` çağrılmadan önceki ortak güvenlik kontrolü.

Bu kontrol iki yerde AYNI gerekçeyle, iki ayrı kopya olarak yazılmıştı
(`plugins/mcp_plugin.py` ve `voice/tts_cloud.py`). İkisi de senkron bir
API'nin (`BaseTool.execute`, `TextToSpeech.speak`) içinden `asyncio.run()`
çağırıyor ve `asyncio.run()` iç içe çalışamaz.
"""

from __future__ import annotations

import asyncio


def ensure_no_running_event_loop(caller: str, hint: str = "") -> None:
    """Çağıran thread'de çalışan bir olay döngüsü OLMADIĞINI doğrular.

    `asyncio.run()` zaten çalışan bir döngü içinden çağrılırsa kendisi de
    `RuntimeError` fırlatır; bu fonksiyon aynı durumu daha erken ve
    kimin/nerede hata yaptığını söyleyen bir mesajla yakalar.

    BİLEREK PROJEYE ÖZGÜ BİR HATAYA SARILMAZ: bu bir KULLANIM/PROGRAMLAMA
    hatasıdır (yanlış thread'den çağrı), bir sunucunun erişilemez
    olmasıyla ilgisi yoktur. Örneğin `voice/router.py`'nin bunu yutup
    sessizce yerel sağlayıcıya düşmesi, gerçek hatayı gizlemekten başka
    işe yaramaz — kullanıcı "bulut neden hep kapalı" diye aylarca
    arardı.

    Args:
        caller: Hata mesajında görünecek çağıran adı (örn.
            `"EdgeTextToSpeech.speak()"`).
        hint: İsteğe bağlı ek yönlendirme (örn. "Sesi ayrı bir thread'de
            çalıştırın.").

    Raises:
        RuntimeError: Çağıran thread'de zaten çalışan bir olay döngüsü varsa.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # çalışan döngü yok -> beklenen/normal durum

    mesaj = f"{caller}, zaten çalışan bir asyncio olay döngüsü içinden çağrılamaz (asyncio.run() iç içe çalışamaz)."
    if hint:
        mesaj = f"{mesaj} {hint}"
    raise RuntimeError(mesaj)
