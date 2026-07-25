"""Tüm Artemis tool'larının uyması gereken ortak arayüz (interface).

Yeni bir yetenek eklemek isteyen her plugin, BaseTool sınıfından türeyip
`get_arguments_schema()` ve `execute()` metotlarını doldurmak zorundadır.
Bu, dispatcher'ın hangi tool olursa olsun aynı şekilde çağırabilmesini
sağlar ve yeni tool eklerken merkezi hiçbir dosyanın değiştirilmesine
gerek bırakmaz.

Composition Notu: Ortak davranışlar (loglama, config erişimi, hafıza)
inheritance ile değil, ToolContext nesnesinin execute() metoduna
"inject" edilmesiyle sağlanır. Böylece BaseTool tek seviyeli kalır;
her yeni cross-cutting ihtiyaç için yeni bir mixin/ara sınıf eklenmez
("Inheritance yerine Composition" kuralının somut karşılığı).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from config.settings import Settings
from core.enums import DangerLevel
from memory.context_memory import ContextMemory
from models.tool_models import ToolResult


@dataclass(frozen=True)
class ToolContext:
    """Her tool çağrısında BaseTool.execute()'a enjekte edilen ortak bağımlılıklar.

    Bir tool loglama, config veya hafızaya ihtiyaç duyduğunda bunları
    miras almaz; bu nesne üzerinden erişir (composition over inheritance).

    Attributes:
        settings: Uygulama geneli ayarlar (yollar, güvenlik listesi vb.).
        memory: Kısa/uzun süreli bağlam hafızası (örn. son açılan klasör).
        logger: Bu tool'a özel isimlendirilmiş logger.
    """

    settings: Settings
    memory: ContextMemory
    logger: logging.Logger


class BaseTool(ABC):
    """Her Artemis tool'unun implemente etmesi gereken soyut temel sınıf.

    Class Attributes:
        name: Nokta notasyonlu benzersiz tool kimliği (örn. "filesystem.open").
            `core.plugin_loader.register_tool` bu isim üzerinden kaydeder.
        description: LLM'e gösterilecek kısa açıklama.
        danger_level: SAFE ise doğrudan çalışır, CONFIRM_REQUIRED ise
            dispatcher kullanıcı onayı almadan execute() çağırmaz.
    """

    name: str
    description: str
    danger_level: DangerLevel = DangerLevel.SAFE

    @abstractmethod
    def get_arguments_schema(self) -> dict[str, Any]:
        """Bu tool'un beklediği argümanların JSON şemasını döndürür.

        Şema hem LLM'e tool tanıtımı yaparken (bkz. core.manifest) hem de
        gelen argümanları belgelendirmek için kullanılır.

        Returns:
            JSON-schema uyumlu bir sözlük.
        """

    @abstractmethod
    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Tool'un asıl işini yapar.

        Args:
            arguments: LLM'in ürettiği, bu tool'a özel argümanlar.
            context: Loglama/config/hafıza gibi ortak bağımlılıklar.

        Returns:
            İşlemin başarılı olup olmadığını ve sonucunu taşıyan ToolResult.
        """
