"""Plugin auto-discovery ve tool registry.

Yeni bir yetenek eklemek için yapılması gereken TEK şey `plugins/`
klasörüne yeni bir .py dosyası eklemek ve içindeki tool sınıfını
`@register_tool` ile işaretlemektir. Bu dosya (plugin_loader.py) veya
başka hiçbir merkezi dosya değiştirilmez; import edilen her plugin
modülü kendi tool'unu, modül seviyesinde (decorator ile) registry'e
kaydeder. 1 tool ile 100 tool arasında bu mekanizmada hiçbir fark yoktur.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from core.exceptions import PluginLoadError
from core.tool_base import BaseTool

logger = logging.getLogger(__name__)

# Tüm tool sınıflarının (instance değil, class) tutulduğu merkezi kayıt defteri.
TOOL_REGISTRY: dict[str, type[BaseTool]] = {}


def register_tool(cls: type[BaseTool]) -> type[BaseTool]:
    """Bir BaseTool alt sınıfını TOOL_REGISTRY'e kaydeden decorator.

    Kullanım::

        @register_tool
        class FilesystemOpenTool(BaseTool):
            name = "filesystem.open"
            ...

    Args:
        cls: Kaydedilecek BaseTool alt sınıfı.

    Returns:
        Değiştirilmeden aynı sınıf (decorator zincirlemeye izin verir).

    Raises:
        ValueError: Sınıfın `name` özniteliği yoksa veya aynı isimle
            başka bir tool zaten kayıtlıysa.
    """

    tool_name = getattr(cls, "name", None)
    if not tool_name:
        raise ValueError(f"{cls.__name__} sınıfı bir 'name' özniteliği tanımlamalı.")

    if tool_name in TOOL_REGISTRY:
        raise ValueError(
            f"Tool adı çakışması: '{tool_name}' zaten "
            f"{TOOL_REGISTRY[tool_name].__name__} tarafından kullanılıyor."
        )

    TOOL_REGISTRY[tool_name] = cls
    logger.debug("Tool kaydedildi: %s -> %s", tool_name, cls.__name__)
    return cls


def load_plugins(plugins_package: str = "plugins") -> dict[str, type[BaseTool]]:
    """`plugins/` klasöründeki tüm modülleri dinamik olarak import eder.

    Her modül import edildiğinde, içindeki `@register_tool` ile
    işaretlenmiş sınıflar otomatik olarak TOOL_REGISTRY'e eklenir.
    Böylece 1 tool ile 100 tool arasında bu fonksiyonda hiçbir
    değişiklik gerekmez.

    Not: Plugin sayısı arttıkça (örn. `plugins/browser/`,
    `plugins/windows/` gibi alt paketlere bölündüğünde) bu fonksiyondaki
    `pkgutil.iter_modules` çağrısı `pkgutil.walk_packages` ile
    değiştirilerek iç içe paketler de otomatik taranabilir.

    Args:
        plugins_package: Taranacak paketin import yolu (varsayılan: "plugins").

    Returns:
        Yüklenen tüm tool sınıflarının (name -> class) sözlüğü.

    Raises:
        PluginLoadError: Bir plugin modülü import edilirken hata olursa.
    """

    package = importlib.import_module(plugins_package)
    package_path = Path(package.__file__).resolve().parent

    for module_info in pkgutil.iter_modules([str(package_path)]):
        if module_info.name.startswith("_"):
            continue

        full_module_name = f"{plugins_package}.{module_info.name}"
        try:
            importlib.import_module(full_module_name)
            logger.info("Plugin yüklendi: %s", full_module_name)
        except Exception as exc:  # noqa: BLE001 - kasıtlı olarak geniş yakalama
            raise PluginLoadError(full_module_name, str(exc)) from exc

    return TOOL_REGISTRY
