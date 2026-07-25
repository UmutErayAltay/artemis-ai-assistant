"""Proje genelinde kullanılan ortak enum tanımları.

Magic string kullanımını (örn. tool.danger_level == "confirm_required")
engellemek için tüm sabit değer kümeleri burada toplanır.
"""

from __future__ import annotations

from enum import Enum


class DangerLevel(str, Enum):
    """Bir tool'un ne kadar riskli olduğunu belirtir.

    Attributes:
        SAFE: Doğrudan çalıştırılabilir (örn. dosya arama, pencere listeleme).
        CONFIRM_REQUIRED: Kullanıcı onayı olmadan çalıştırılamaz
            (örn. silme, kapatma, format, registry, servis işlemleri).
            `GÜVENLİK` bölümündeki kural bu seviye üzerinden uygulanır.
    """

    SAFE = "safe"
    CONFIRM_REQUIRED = "confirm_required"
