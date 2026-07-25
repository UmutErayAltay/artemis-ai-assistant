"""LLM çıktısını (Tool JSON) gerçek Python fonksiyon çağrısına dönüştüren katman.

Dispatcher, sistemin tek "giriş kapısı"dır: LLM ne üretirse üretsin bütün
akış buradan geçer. Böylece loglama, hata yönetimi, güvenlik kontrolü ve
hafıza güncellemesi TEK bir yerde, her tool için tekrar yazılmadan
yönetilir (DRY prensibi).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from config.settings import Settings, get_settings
from core.enums import DangerLevel
from core.exceptions import InvalidToolArgumentsError, ToolNotFoundError
from core.plugin_loader import TOOL_REGISTRY
from core.tool_base import ToolContext
from memory.context_memory import ContextMemory
from models.tool_models import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class ToolDispatcher:
    """LLM'den gelen ToolCall'ları çalıştıran merkezi orkestratör.

    Attributes:
        settings: Uygulama ayarları.
        memory: Bağlam hafızası.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        memory: ContextMemory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.memory = memory or ContextMemory(self.settings.db_path)

    def dispatch(self, raw_call: dict[str, Any], confirmed: bool = False) -> ToolResult:
        """Ham bir tool çağrısı sözlüğünü doğrular, güvenlik kontrolünden
        geçirir ve ilgili tool'u çalıştırır.

        Args:
            raw_call: LLM'in ürettiği ham JSON'dan parse edilmiş dict,
                örn. {"tool": "filesystem.open", "arguments": {...}}.
            confirmed: Kullanıcı, daha önce "onay gerektirir" olarak
                işaretlenen bu işlemi onayladıysa True.

        Returns:
            İşlemin sonucunu taşıyan ToolResult. Hiçbir zaman exception
            fırlatmaz; her hata durumu success=False bir ToolResult'a
            çevrilir (üst katmanların try/except yazmasına gerek kalmaz).
        """

        try:
            call = ToolCall(**raw_call)
        except ValidationError as exc:
            logger.warning("Geçersiz tool çağrısı formatı: %s", exc)
            return ToolResult(success=False, message=f"Geçersiz tool çağrısı formatı: {exc}")

        try:
            return self._execute(call, confirmed=confirmed)
        except ToolNotFoundError as exc:
            logger.error("Tool bulunamadı: %s", call.tool)
            return ToolResult(success=False, message=str(exc))
        except InvalidToolArgumentsError as exc:
            logger.error("Argüman hatası (%s): %s", call.tool, exc)
            return ToolResult(success=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - beklenmeyen hatayı da güvenli döndür
            logger.exception("'%s' çalıştırılırken beklenmeyen hata", call.tool)
            return ToolResult(success=False, message=f"Beklenmeyen hata: {exc}")

    def _execute(self, call: ToolCall, confirmed: bool) -> ToolResult:
        tool_cls = TOOL_REGISTRY.get(call.tool)
        if tool_cls is None:
            raise ToolNotFoundError(call.tool)

        tool = tool_cls()

        is_dangerous = (
            tool.danger_level == DangerLevel.CONFIRM_REQUIRED
            or call.tool in self.settings.dangerous_tools
        )
        if is_dangerous and not confirmed:
            logger.info("Onay gerektiren işlem beklemede: %s", call.tool)
            if call.arguments:
                args_text = ", ".join(f"{key}={value}" for key, value in call.arguments.items())
            else:
                args_text = "argüman yok"
            return ToolResult(
                success=False,
                message=f"'{call.tool}' işlemi onay gerektiriyor. Argümanlar: {args_text}",
                data={"tool": call.tool, "arguments": call.arguments},
                requires_confirmation=True,
            )

        context = ToolContext(
            settings=self.settings,
            memory=self.memory,
            logger=logging.getLogger(f"artemis.tools.{call.tool}"),
        )

        result = tool.execute(call.arguments, context)
        logger.info("Tool çalıştırıldı: %s -> success=%s", call.tool, result.success)
        return result
