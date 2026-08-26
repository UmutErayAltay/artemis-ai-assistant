"""`core/plugin_loader.py` testleri — otomatik keşif ve kayıt sözleşmesi.

Bu modül daha önce HİÇ doğrudan test edilmemişti; her test dosyası onu
dolaylı olarak (`load_plugins()` çağırarak) kullanıyordu ama mekanizmanın
kendisi (isim çakışması `ValueError` fırlatır mı, `_` ile başlayan
dosyalar GERÇEKTEN atlanır mı) hiçbir yerde doğrudan sınanmıyordu.

`_` ile başlama kuralı GERÇEK `plugins/` paketiyle sınanamaz (`_app_
resolver.py` hiç tool tanımlamıyor — "atlandı" ile "zaten tool'u yok"
ayırt edilemez). Bu yüzden izole, geçici bir sahte paket kurulup
`load_plugins()` ona karşı çalıştırılır; gerçek `TOOL_REGISTRY`'ye
KALICI hiçbir şey eklenmez (test sonunda temizlenir).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from core.exceptions import PluginLoadError
from core.plugin_loader import TOOL_REGISTRY, load_plugins, register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult


def test_register_tool_raises_on_name_collision() -> None:
    """Aynı isimle ikinci bir kayıt `ValueError` fırlatmalı — bu, yeni
    bir tool eklerken sessizce birbirinin üzerine yazılmasını önleyen
    tek koruma (bkz. CLAUDE.md: "Tool adları çakışmamalı")."""

    class IlkTool(BaseTool):
        name = "test.collision.probe"
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            return ToolResult(success=True, message="ok")

    class IkinciTool(BaseTool):
        name = "test.collision.probe"  # KASITLI aynı isim
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            return ToolResult(success=True, message="ok")

    register_tool(IlkTool)
    try:
        with pytest.raises(ValueError, match="çakışması"):
            register_tool(IkinciTool)
    finally:
        del TOOL_REGISTRY["test.collision.probe"]  # gerçek registry'yi kirletme


def test_register_tool_requires_a_name_attribute() -> None:
    class IsimsizTool(BaseTool):
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            return ToolResult(success=True, message="ok")

    with pytest.raises(ValueError, match="name"):
        register_tool(IsimsizTool)


def test_register_tool_returns_the_same_class_unchanged() -> None:
    """Decorator zincirlemeye izin vermeli — sınıfı SARMALAMAMALI, aynen döndürmeli."""

    class TekilTool(BaseTool):
        name = "test.identity.probe"
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            return ToolResult(success=True, message="ok")

    try:
        returned = register_tool(TekilTool)
        assert returned is TekilTool
    finally:
        del TOOL_REGISTRY["test.identity.probe"]


@pytest.fixture
def fake_plugins_package(tmp_path: Path) -> str:
    """Geçici, izole bir sahte `plugins` paketi kurar:

        fake_plugins_<id>/
            __init__.py
            normal_module.py     -> bir tool KAYDEDER (adı benzersiz)
            _private_module.py   -> `_` ile başladığı için ATLANMALI

    `load_plugins()` bu pakete karşı çalıştırılır; gerçek `plugins/`
    paketine ya da `TOOL_REGISTRY`'ye kalıcı hiçbir etkisi olmaz (test
    sonunda `sys.path`/`sys.modules`/`TOOL_REGISTRY` temizlenir).

    Returns:
        Sahte paketin dotted import adı (`load_plugins()`'e verilecek).
    """

    package_name = f"fake_plugins_{id(tmp_path)}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")

    (package_dir / "normal_module.py").write_text(
        "from core.plugin_loader import register_tool\n"
        "from core.tool_base import BaseTool\n"
        "from models.tool_models import ToolResult\n\n"
        "@register_tool\n"
        "class NormalTool(BaseTool):\n"
        f"    name = 'test.fake_pkg.{package_name}.normal'\n"
        "    description = 'test'\n"
        "    def get_arguments_schema(self):\n"
        "        return {'type': 'object', 'properties': {}}\n"
        "    def execute(self, arguments, context):\n"
        "        return ToolResult(success=True, message='ok')\n",
        encoding="utf-8",
    )

    # Bu dosya import EDİLİRSE bir tool kaydederdi; `_` öneki YÜZÜNDEN
    # load_plugins() bunu hiç import ETMEMELİ.
    (package_dir / "_private_module.py").write_text(
        "from core.plugin_loader import register_tool\n"
        "from core.tool_base import BaseTool\n"
        "from models.tool_models import ToolResult\n\n"
        "@register_tool\n"
        "class GizliTool(BaseTool):\n"
        f"    name = 'test.fake_pkg.{package_name}.private'\n"
        "    description = 'test'\n"
        "    def get_arguments_schema(self):\n"
        "        return {'type': 'object', 'properties': {}}\n"
        "    def execute(self, arguments, context):\n"
        "        return ToolResult(success=True, message='ok')\n",
        encoding="utf-8",
    )

    sys.path.insert(0, str(tmp_path))
    try:
        yield package_name
    finally:
        sys.path.remove(str(tmp_path))
        for key in list(sys.modules):
            if key == package_name or key.startswith(f"{package_name}."):
                del sys.modules[key]
        for tool_name in list(TOOL_REGISTRY):
            if tool_name.startswith(f"test.fake_pkg.{package_name}."):
                del TOOL_REGISTRY[tool_name]


def test_load_plugins_discovers_a_normal_module(fake_plugins_package: str) -> None:
    load_plugins(fake_plugins_package)

    assert f"test.fake_pkg.{fake_plugins_package}.normal" in TOOL_REGISTRY


def test_load_plugins_skips_underscore_prefixed_modules(fake_plugins_package: str) -> None:
    """REGRESYON KORUMASI: `_app_resolver.py` gibi `_` ile başlayan
    dosyalar (yardımcı modüller, tool DEĞİL) otomatik taramadan
    ATLANMALI — aksi halde her yardımcı dosya da bir plugin gibi
    içe aktarılmaya çalışılırdı."""

    load_plugins(fake_plugins_package)

    assert f"test.fake_pkg.{fake_plugins_package}.private" not in TOOL_REGISTRY


def test_load_plugins_raises_plugin_load_error_on_broken_module(tmp_path: Path) -> None:
    """Bir plugin dosyası import edilirken hata verirse (sözdizimi/
    içe aktarma hatası), bu sessizce yutulmamalı — `PluginLoadError`
    olarak fırlatılmalı ki bozuk bir plugin fark edilmeden kalmasın."""

    package_name = f"fake_broken_{id(tmp_path)}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "broken_module.py").write_text("bu gecerli python degil ///", encoding="utf-8")

    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(PluginLoadError):
            load_plugins(package_name)
    finally:
        sys.path.remove(str(tmp_path))
        for key in list(sys.modules):
            if key == package_name or key.startswith(f"{package_name}."):
                del sys.modules[key]


def test_load_plugins_against_real_package_registers_known_tools() -> None:
    """Pozitif kontrol: gerçek `plugins/` paketine karşı çalıştırıldığında
    bilinen tool'lar registry'de olmalı (sahte paket testleri
    mekanizmayı izole sınıyordu, bu gerçek entegrasyonu doğrular)."""

    registry = load_plugins()

    assert "filesystem.create_folder" in registry
    assert "assistant.reply" in registry
    assert "memory.remember" in registry
