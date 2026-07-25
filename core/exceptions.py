"""Artemis çekirdek istisna (exception) hiyerarşisi.

Bütün proje-özel hatalar bu modüldeki sınıflardan türetilir. Böylece
dispatcher ve plugin katmanları, ham Python istisnalarını (KeyError,
ValueError vb.) yakalamak yerine anlamlı, projeye özgü hata tiplerini
yakalayıp işleyebilir; bu da hata yönetimini tek bir yerde standardize eder.
"""

from __future__ import annotations


class ArtemisError(Exception):
    """Tüm Artemis istisnalarının temel sınıfı."""


class ToolNotFoundError(ArtemisError):
    """İstenen tool, registry içinde bulunamadığında fırlatılır."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool bulunamadı: '{tool_name}'")


class InvalidToolArgumentsError(ArtemisError):
    """LLM'in ürettiği argümanlar, tool'un beklediği şemaya uymadığında fırlatılır."""

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"'{tool_name}' için geçersiz argümanlar: {reason}")


class PluginLoadError(ArtemisError):
    """Bir plugin dosyası yüklenirken/import edilirken hata oluştuğunda fırlatılır."""

    def __init__(self, module_name: str, reason: str) -> None:
        self.module_name = module_name
        self.reason = reason
        super().__init__(f"Plugin yüklenemedi '{module_name}': {reason}")
