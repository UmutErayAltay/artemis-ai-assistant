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

import threading
from typing import Any


class ConfirmationLatch:
    """Bir onay sorusunun tek cevabını tutar: İLK gelen karar geçerlidir.

    NEDEN: sesli onay iki yoldan cevaplanabilir — konuşma (ses işçisi iş parçacığı)
    ve ekrandaki Evet/Hayır düğmesi (GUI iş parçacığı). İkisi aynı anda ya da
    birbirine çok yakın gelebilir. Düz bir değişken, ikinci kararın birincinin
    üstüne sessizce yazmasına izin verirdi; burada karar bir kez yazılır ve
    sonrakiler yok sayılır. Kilit, iki iş parçacığının aynı anda yazmasını da engeller.

    Attributes:
        source: Kararı veren kanal ("ses" ya da "düğme"); günlük için. Karar yoksa boş.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decided = False
        self._approved = False
        self.source = ""

    def decide(self, approved: bool, source: str) -> None:
        """Kararı yazar; zaten bir karar varsa sessizce yok sayar."""

        with self._lock:
            if self._decided:
                return
            self._decided = True
            self._approved = approved
            self.source = source

    def is_decided(self) -> bool:
        """Bir karar verilmiş mi? Dinleme döngüsü bunu her blokta yoklar."""

        with self._lock:
            return self._decided

    @property
    def approved(self) -> bool:
        """Verilen kararın kendisi. Henüz karar yoksa `False` (güvenli taraf: red)."""

        with self._lock:
            return self._approved


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
