"""Türkçe metin normalleştirme.

NEDEN AYRI BİR MODÜL: Python'ın `str.lower()`'ı Türkçe yerel ayarını
KULLANMAZ ve bu, bu projede iki ayrı yerde gerçek arızaya yol açtı.
Sorun tek bir harfte: Türkçede büyük `I`'nın küçüğü noktasız `ı`,
büyük `İ`'nin küçüğü noktalı `i`'dir. Python ikisini de İngilizce
kurallarına göre çevirir::

    "ÇIKIŞ".lower()  ->  "çikiş"   (beklenen: "çıkış")
    "HAYIR".lower()  ->  "hayir"   (beklenen: "hayır")

Sonuç, kullanıcının BÜYÜK HARFLE yazdığı ya da konuşma tanıyıcının
büyük harfle döndürdüğü sözcüklerin sabit listelerle eşleşmemesidir —
ve iki kullanım yeri de bir KARAR noktası: sohbetten çıkış ve geri
alınamaz bir işlemin onayı.
"""

from __future__ import annotations

_TURKISH_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})


def turkish_lower(text: str) -> str:
    """Metni Türkçe kurallarına göre küçük harfe çevirir.

    Önce `I`/`İ` elle eşlenir, sonra kalan her şey için standart
    `str.lower()` uygulanır (diğer harflerde Python zaten doğru:
    `Ç->ç`, `Ş->ş`, `Ğ->ğ`, `Ö->ö`, `Ü->ü`).

    Args:
        text: Ham metin.

    Returns:
        Türkçe kurallarına göre küçültülmüş metin.
    """

    return text.translate(_TURKISH_LOWER_MAP).lower()


def lower_variants(text: str) -> set[str]:
    """Metnin hem Türkçe hem İngilizce kurallarına göre küçük hâlini verir.

    NEDEN İKİSİ BİRDEN: bu projede karşılaştırılan sabit listeler her iki
    dilden sözcük içeriyor (`{"çıkış", "cikis", "exit", "quit"}`). Tek bir
    kural seçmek diğerini bozar::

        turkish_lower("EXIT")  -> "exıt"   (İngilizce sözcük bozuldu)
        str.lower("ÇIKIŞ")     -> "çikiş"  (Türkçe sözcük bozuldu)

    Girdinin hangi dilde yazıldığını önceden bilemeyiz; bu yüzden ikisi de
    denenir. Yanlış eşleşme riski yok: iki çeviri yalnızca `I`/`İ`
    harflerinde ayrışıyor.

    Args:
        text: Ham metin.

    Returns:
        En fazla iki elemanlı bir küme; üyelik testi için kullanılır.
    """

    return {text.lower(), turkish_lower(text)}
