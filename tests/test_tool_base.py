"""`core/tool_base.py` testleri — `ToolContext` ve `BaseTool` sözleşmesi.

Bu modül daha önce HİÇ doğrudan test edilmemişti; her tool test dosyası
onu dolaylı olarak (gerçek bir `ToolContext` inşa ederek) kullanıyordu
ama sözleşmenin kendisi (frozen dataclass olması, ABC'nin eksik
metotları gerçekten reddetmesi) hiçbir yerde doğrudan sınanmıyordu.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

import pytest

from config.settings import Settings
from core.enums import DangerLevel
from core.tool_base import BaseTool, ToolContext
from memory.context_memory import ContextMemory
from models.tool_models import ToolResult


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    settings = Settings(desktop_path=tmp_path, db_path=tmp_path / "m.db", log_dir=tmp_path / "logs")
    return ToolContext(
        settings=settings,
        memory=ContextMemory(settings.db_path),
        logger=logging.getLogger("tests.tool_base"),
    )


def test_tool_context_is_frozen(context: ToolContext) -> None:
    """`ToolContext` composition amaçlı salt-okunur olmalı — bir tool
    kendi çalışırken bunu MUTASYONA uğratıp başka bir tool'u
    etkilememeli (bkz. modül dokümantasyonu, "composition over
    inheritance")."""

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.logger = logging.getLogger("baska")  # type: ignore[misc]


def test_base_tool_cannot_be_instantiated_without_required_methods() -> None:
    """`get_arguments_schema`/`execute` doldurulmadan bir `BaseTool` alt
    sınıfı örneklenemez — ABC bunu GERÇEKTEN zorluyor mu, sınanır
    (yalnızca docstring'de yazması yetmez)."""

    class EksikTool(BaseTool):
        name = "eksik.tool"
        description = "test"

    with pytest.raises(TypeError):
        EksikTool()  # type: ignore[abstract]


def test_base_tool_missing_only_execute_still_rejected() -> None:
    """Yalnızca `execute` eksik olsa bile (şema tanımlı) örnekleme reddedilmeli."""

    class YarimTool(BaseTool):
        name = "yarim.tool"
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

    with pytest.raises(TypeError):
        YarimTool()  # type: ignore[abstract]


def test_properly_formed_subclass_can_be_instantiated() -> None:
    """Pozitif kontrol: her iki soyut metot da dolduğunda örnekleme başarılı olmalı."""

    class TamTool(BaseTool):
        name = "tam.tool"
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            return ToolResult(success=True, message="ok")

    tool = TamTool()
    assert tool.name == "tam.tool"


def test_danger_level_defaults_to_safe_when_not_overridden() -> None:
    """Bir alt sınıf `danger_level`'ı hiç belirtmezse SAFE'e düşmeli —
    yanlışlıkla "belirtilmemiş = tehlikesiz" varsayımı DOĞRU tarafa
    düşer (belirtilmemiş bir CONFIRM_REQUIRED'ın sessizce SAFE
    davranması güvenlik açığı olurdu; burada tersi doğru: varsayılan
    zaten en kısıtlayıcı OLMAYAN taraf, dispatcher'ın onay mantığı
    zaten yalnızca AÇIKÇA CONFIRM_REQUIRED işaretlenmiş tool'ları durdurur)."""

    class VarsayilanliTool(BaseTool):
        name = "varsayilanli.tool"
        description = "test"

        def get_arguments_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            return ToolResult(success=True, message="ok")

    assert VarsayilanliTool().danger_level == DangerLevel.SAFE


def test_context_carries_real_dependencies_through(context: ToolContext, tmp_path: Path) -> None:
    """`ToolContext`, tool'ların gerçekten kullandığı bağımlılıkları
    (ayarlar, hafıza, logger) doğru taşımalı — DI'nin kendisi test
    edilir, belirli bir tool değil."""

    assert context.settings.desktop_path == tmp_path
    assert isinstance(context.memory, ContextMemory)
    assert context.logger.name == "tests.tool_base"

    context.memory.set("x", "y")
    assert context.memory.get("x") == "y"
