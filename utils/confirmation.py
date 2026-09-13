"""Onay istemlerinde kullanıcıya gösterilecek argüman metnini biçimlendirir.

NEDEN AYRI BİR MODÜL: aynı "argümanları okunaklı bir metne çevir" mantığı
`core/dispatcher.py`, `core/conversation_loop.py` ve `core/voice_loop.py`
içinde ÜÇ AYRI YERDE, üç farklı biçimde (birinde "key=value", diğer
ikisinde "key: value") tekrarlanıyordu. Bu GÜVENLİK açısından kritik bir
metin: CLAUDE.md'nin "onay NEYİ onayladığını göstermek zorunda" ilkesi
(bkz. README §16b) her üç yerde de geçerli, yani biçimlendirmenin kendisi
de TEK bir yerde yaşamalı — üç kopyadan biri güncellenip diğer ikisi
unutulursa, o arayüzdeki onay sessizce eksik bilgiyle kör bir onaya
dönüşür.
"""

from __future__ import annotations

from typing import Any


def format_confirmation_arguments(arguments: dict[str, Any]) -> str:
    """Bir tool çağrısının argümanlarını, onay istemi için TEK SATIRLIK
    okunaklı bir metne çevirir.

    Args:
        arguments: Onay gerektiren tool çağrısının argümanları.

    Returns:
        `"key: value, key2: value2"` biçiminde bir metin; argüman yoksa
        `"argüman yok"`.
    """

    if not arguments:
        return "argüman yok"
    return ", ".join(f"{key}: {value}" for key, value in arguments.items())
