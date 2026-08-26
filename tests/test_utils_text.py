"""`utils/text.py` testleri — Türkçe küçük harf çevrimi.

Bu modül iki gerçek arızanın ortak çözümü: sohbetten çıkış komutu
(`"ÇIKIŞ"`) ve sesli onay reddi (`"HAYIR"`) BÜYÜK HARFLE geldiğinde
sabit listelerle eşleşmiyordu.
"""

from __future__ import annotations

import pytest

from utils.text import lower_variants, turkish_lower


@pytest.mark.parametrize(
    ("buyuk", "beklenen"),
    [
        ("ÇIKIŞ", "çıkış"),
        ("HAYIR", "hayır"),
        ("YAPMA", "yapma"),
        ("İSTEMİYORUM", "istemiyorum"),
        ("TAMAM", "tamam"),
        ("IŞIK", "ışık"),
    ],
)
def test_turkish_lower_handles_the_dotted_and_dotless_i(buyuk: str, beklenen: str) -> None:
    """Türkçede `I`'nın küçüğü `ı`, `İ`'nin küçüğü `i`'dir.

    Python'ın `str.lower()`'ı ikisini de İngilizce kurallarına göre
    çevirir; sonuç sessiz bir eşleşmeme olur.
    """

    assert turkish_lower(buyuk) == beklenen


def test_python_lower_really_does_get_these_wrong() -> None:
    """Bu modülün var olma sebebini sabitler.

    Python bir gün Türkçe yerel ayarını varsayılan yaparsa bu test
    kırılır ve `utils/text.py`'nin hâlâ gerekli olup olmadığı gözden
    geçirilir — sessizce gereksiz kod taşımayalım.
    """

    assert "ÇIKIŞ".lower() != "çıkış"
    assert "HAYIR".lower() != "hayır"


def test_lower_variants_covers_both_languages() -> None:
    """Karşılaştırılan listeler iki dilden sözcük içeriyor; tek kural yetmez."""

    assert "çıkış" in lower_variants("ÇIKIŞ")
    assert "exit" in lower_variants("EXIT")


def test_lower_variants_is_safe_for_words_without_i() -> None:
    """`I`/`İ` içermeyen sözcüklerde iki çeviri aynıdır."""

    assert lower_variants("EVET") == {"evet"}
