"""`utils/confirmation.py` testleri.

Bu modül `core/dispatcher.py`, `core/conversation_loop.py` ve
`core/voice_loop.py` içinde ÜÇ AYRI YERDE tekrarlanan onay-argümanı
biçimlendirmesinin ortak çözümü (bkz. modül dokümantasyonu).
"""

from __future__ import annotations

from utils.confirmation import format_confirmation_arguments


def test_no_arguments_is_reported_honestly() -> None:
    assert format_confirmation_arguments({}) == "argüman yok"


def test_single_argument_is_shown_as_key_value() -> None:
    assert format_confirmation_arguments({"target": "onemli.txt"}) == "target: onemli.txt"


def test_multiple_arguments_are_comma_joined_in_insertion_order() -> None:
    result = format_confirmation_arguments({"target": "onemli.txt", "location": "desktop"})
    assert result == "target: onemli.txt, location: desktop"
