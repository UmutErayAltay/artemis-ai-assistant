"""`utils/text.py` testleri — Türkçe küçük harf çevrimi.

Bu modül iki gerçek arızanın ortak çözümü: sohbetten çıkış komutu
(`"ÇIKIŞ"`) ve sesli onay reddi (`"HAYIR"`) BÜYÜK HARFLE geldiğinde
sabit listelerle eşleşmiyordu.
"""

from __future__ import annotations

import pytest

from utils.text import is_clear_affirmative_answer, lower_variants, turkish_lower


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


# --- is_clear_affirmative_answer ---------------------------------------
#
# Ayrıntılı olumsuzluk-eki sezgiseli senaryoları zaten
# `tests/test_voice_loop.py`'de (`REDDEDILMESI_GEREKEN_CEVAPLAR` /
# `ONAYLANMASI_GEREKEN_CEVAPLAR`) `_confirm_by_voice` üzerinden uçtan uca
# sınanıyor. Bu testler yalnızca fonksiyonun DOĞRUDAN çağrıldığında da
# aynı sözleşmeyi taşıdığını doğrular — artık `core/conversation_loop.py`
# da aynı fonksiyona bağlı olduğu için bu paylaşılan sözleşmenin garantisi
# burada.


def test_is_clear_affirmative_answer_accepts_a_clear_yes() -> None:
    assert is_clear_affirmative_answer("evet") is True


def test_is_clear_affirmative_answer_rejects_a_clear_no() -> None:
    assert is_clear_affirmative_answer("hayır") is False


def test_is_clear_affirmative_answer_negation_veto_wins_over_an_affirmative_word() -> None:
    """"İptal, evet" gibi çelişkili bir cevap RED sayılmalı — güvenli taraf."""

    assert is_clear_affirmative_answer("iptal evet") is False


def test_is_clear_affirmative_answer_rejects_empty_and_unclear_answers() -> None:
    assert is_clear_affirmative_answer("") is False
    assert is_clear_affirmative_answer("belki") is False
