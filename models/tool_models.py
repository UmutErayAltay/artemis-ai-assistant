"""Pydantic ile tanımlanmış veri modelleri.

LLM'den gelen ham JSON çıktısı ile Python tarafındaki tool execution
katmanı arasındaki "sözleşmeyi" (contract) bu modeller temsil eder.
`ToolCall` yalnızca zarfı (`tool`/`arguments` alanlarının var olduğunu)
doğrular — argümanların İÇERİĞİNİN ilgili tool'un
`get_arguments_schema()`'sına uyup uymadığı burada DEĞİL,
`core/dispatcher.py::_validate_arguments`'ta, tek ve merkezi bir yerde
kontrol edilir (bkz. o modülün dokümantasyonu, README §24).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.enums import DangerLevel


class ToolCall(BaseModel):
    """LLM'in ürettiği ham tool çağrısı.

    Attributes:
        tool: Nokta notasyonlu tool adı (örn. "filesystem.open").
        arguments: Tool'un execute() metoduna geçirilecek argümanlar.
    """

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Bir tool çalıştırıldıktan sonra dönen standart sonuç.

    Attributes:
        success: İşlem başarılı mı?
        message: Kullanıcıya / üst katmana gösterilecek insan-okur mesaj.
        data: İşlemden dönen ek veri (opsiyonel).
        requires_confirmation: True ise işlem çalıştırılmadı, kullanıcı
            onayı bekleniyor demektir (bkz. GÜVENLİK kuralları).
    """

    success: bool
    message: str
    data: dict[str, Any] | None = None
    requires_confirmation: bool = False


class ToolDefinition(BaseModel):
    """Bir tool'un LLM'e tanıtılacak "manifest" girdisi.

    Bu model, registry'deki her tool için `core.manifest` tarafından
    otomatik olarak üretilir ve Ollama'nın tool-calling context'ine
    beslenir. Böylece 100 tool olsa bile sistem promptu elle güncellenmez.
    """

    name: str
    description: str
    arguments_schema: dict[str, Any]
    danger_level: DangerLevel = DangerLevel.SAFE
