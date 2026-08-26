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


def _type_matches(value: Any, expected_type: str) -> bool:
    """Bir değerin, şemadaki JSON Schema tipiyle uyumlu olup olmadığını kontrol eder.

    Standart bir JSON Schema doğrulayıcısı (örn. `jsonschema` paketi)
    BİLEREK kullanılmadı: o paket `"50"` (string) değerini asla bir
    `integer` saymaz, ama bu projedeki tool'ların `execute()` metotları
    zaten TOLERANSLI dönüşüm yapıyor (bkz. `WindowsSetBrightnessTool`:
    `int(arguments["level"])`). Model küçük/yerel olduğu için sayısal bir
    alanı string üretmesi olası bir hata sınıfı; doğrulama tool'ların
    kendisinden daha KATI olursa, önceden sorunsuz çalışan çağrılar
    burada gereksiz yere reddedilir. Asıl yakalanması gereken hata EKSİK
    ya da KÖKTEN YANLIŞ TÜR (örn. bir listenin geldiği yerde string),
    sayısal-string ayrımı değil.
    """

    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, str):
            try:
                int(value)
            except ValueError:
                return False
            return True
        return False
    if expected_type == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            try:
                float(value)
            except ValueError:
                return False
            return True
        return False
    if expected_type == "boolean":
        if isinstance(value, bool):
            return True
        return isinstance(value, str) and value.strip().lower() in ("true", "false")
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True  # bilinmeyen bir tip bildirimi: kontrol dışı bırak, gelecekte kırılmasın


def _validate_arguments(tool_name: str, arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """`arguments`ı tool'un `get_arguments_schema()`'sına göre doğrular.

    NEDEN BURADA, HER TOOL'UN `execute()`'UNDA DEĞİL: onay mantığı gibi
    bu da merkezi bir sorumluluktur (bkz. CLAUDE.md — "Onay mantığı
    yalnızca dispatcher'da yaşar" ilkesinin doğrulama karşılığı).
    Öncesinde bu kontrol HİÇ yoktu: `InvalidToolArgumentsError` tanımlıydı
    ama hiçbir yerde fırlatılmıyordu (README §24). Eksik zorunlu argüman,
    tool'un içinde ham bir `KeyError` olup dispatcher'ın genel
    yakalayıcısına düşüyor ve kullanıcı "Beklenmeyen hata: 'target'" gibi
    anlaşılmaz bir mesaj görüyordu.

    Raises:
        InvalidToolArgumentsError: Zorunlu bir alan eksikse, bir alanın
            türü şemayla uyuşmuyorsa ya da bir `enum` alanına listede
            olmayan bir değer verilmişse.
    """

    required = schema.get("required", [])
    missing = [key for key in required if key not in arguments]
    if missing:
        raise InvalidToolArgumentsError(tool_name, f"eksik zorunlu argüman(lar): {', '.join(missing)}")

    properties = schema.get("properties", {})
    for key, value in arguments.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            continue  # şemada olmayan fazladan alan: tool zaten kullanmayacak, zararsız

        expected_type = prop_schema.get("type")
        if expected_type and not _type_matches(value, expected_type):
            raise InvalidToolArgumentsError(
                tool_name,
                f"'{key}' alanı {expected_type} türünde olmalı, {type(value).__name__} geldi",
            )

        enum = prop_schema.get("enum")
        if enum is not None and value not in enum:
            options = ", ".join(str(o) for o in enum)
            raise InvalidToolArgumentsError(
                tool_name, f"'{key}' alanı şunlardan biri olmalı: {options} (gelen: {value!r})"
            )


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
        self._warn_about_unknown_dangerous_tools()

    def _warn_about_unknown_dangerous_tools(self) -> None:
        """`dangerous_tools`'taki tanınmayan adlar için uyarır.

        Bu liste bir GÜVENLİK kontrolüdür: kod değiştirmeden, config
        üzerinden ek kısıtlama uygulamaya yarar. Ama hiçbir yerde
        doğrulanmıyordu — `windows.shutdow` gibi bir yazım hatası,
        sessizce ETKİSİZ bir güvenlik ayarı demekti. Kullanıcı
        kısıtlamayı koyduğunu sanırken tool onaysız çalışıyordu.

        Hata değil UYARI: registry, plugin'ler yüklenmeden önce boş
        olabilir ve dispatcher'ı açılış sırasına duyarlı yapmak
        istemiyoruz. Ayrıca ileride kaldırılmış bir tool'un adının
        config'de kalması, asistanın hiç açılmamasını gerektirmez.
        """

        if not TOOL_REGISTRY:
            return

        unknown = [name for name in self.settings.dangerous_tools if name not in TOOL_REGISTRY]
        if unknown:
            logger.warning(
                "config.yaml::dangerous_tools içinde tanınmayan tool adı var (%s); "
                "bu girdiler HİÇBİR ŞEYİ kısıtlamaz. Kayıtlı adlarla karşılaştırın.",
                ", ".join(unknown),
            )

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
        except Exception as exc:
            # Ham istisna METNİ kullanıcıya VERİLMEZ. Bu mesaj terminale
            # yazılır ve ses modunda YÜKSEK SESLE OKUNUR (bkz.
            # `voice_loop._summarize`); istisna metinleri ise rutin olarak
            # mutlak yol, kullanıcı adı ve `WinError` kodu içerir. Ayrıntı
            # log'a gider (`logger.exception` yığın izini de yazar),
            # kullanıcı ne olduğunu ve nereye bakacağını öğrenir.
            logger.exception("'%s' çalıştırılırken beklenmeyen hata", call.tool)
            return ToolResult(
                success=False,
                message=f"'{call.tool}' çalıştırılırken beklenmeyen bir hata oluştu ({type(exc).__name__}). "
                "Ayrıntı için log dosyasına bakın.",
            )

    def _execute(self, call: ToolCall, confirmed: bool) -> ToolResult:
        tool_cls = TOOL_REGISTRY.get(call.tool)
        if tool_cls is None:
            raise ToolNotFoundError(call.tool)

        tool = tool_cls()
        _validate_arguments(call.tool, call.arguments, tool.get_arguments_schema())

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
