"""Tüm test dosyalarının paylaştığı fixture'lar.

Bu dosya yokken iki şey oluyordu:

1. **Tekrar.** `_load_all_plugins` autouse fixture'ı DOKUZ dosyada,
   `dispatcher` fixture'ı SEKİZ dosyada neredeyse birebir kopyalanmıştı.

2. **Testler arası sızıntı.** `TOOL_REGISTRY` süreç genelinde tek bir
   sözlüktür ve `register_tool` aynı isimde ikinci kayıtta `ValueError`
   fırlatır. Bir testin oraya bıraktığı tool, İLGİSİZ bir dosyadaki
   testi kırabiliyordu — bu kuramsal değil, gerçekten yaşandı:
   `test_mcp_plugin.py`'nin bıraktığı tool'lar `test_prompt_builder.py`
   içindeki prompt-boyutu bekçisini kırmıştı (`.context` §6.13). O dosya
   bu yüzden elle bir `_deregister` yardımcısı yazmak zorunda kalmış.
   Aşağıdaki `_isolate_tool_registry`, bu hata sınıfını her test için
   merkezî olarak kapatır.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.plugin_loader import TOOL_REGISTRY, load_plugins
from memory.context_memory import ContextMemory


@pytest.fixture(autouse=True)
def _isolate_tool_registry() -> Iterator[None]:
    """Plugin'leri yükler ve her testten sonra `TOOL_REGISTRY`'yi geri alır.

    Testin kendisi ne kaydederse kaydetsin (MCP tool'ları, geçici sahte
    plugin'ler), sonraki teste sızmaz. Kopya sığdır: değerler tool
    SINIFLARIDIR, örnek değil — paylaşılmaları zararsızdır.

    ANLIK GÖRÜNTÜ NEDEN `load_plugins()`'DEN SONRA ALINIR: `load_plugins`
    modülleri `importlib` ile import eder ve kayıt, modül gövdesindeki
    `@register_tool` dekoratörüyle IMPORT ANINDA yapılır. Python modülleri
    önbelleğe aldığı için ikinci `load_plugins()` çağrısı hiçbir şey
    KAYDETMEZ. Yani önce anlık görüntü alınsaydı, ilk testin sonunda
    registry boş hâline döndürülür ve sonraki testlerin hiçbiri tool
    bulamazdı.
    """

    load_plugins()
    snapshot = dict(TOOL_REGISTRY)
    try:
        yield
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(snapshot)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Her şeyi `tmp_path` altında tutan izole ayarlar.

    GERÇEK masaüstüne/indirilenlere asla dokunulmaz (CLAUDE.md: testin
    çalıştığı gerçek makineyi etkileyen hiçbir şey varsayılan olarak
    çalışmamalı).
    """

    return Settings(
        desktop_path=tmp_path / "Desktop",
        downloads_path=tmp_path / "Downloads",
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def dispatcher(settings: Settings) -> ToolDispatcher:
    """İzole ayarlarla kurulmuş GERÇEK dispatcher.

    Plugin'ler `_isolate_tool_registry` (autouse) tarafından yüklenir.
    """

    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


def install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attributes: Any) -> types.ModuleType:
    """`sys.modules`'a sahte bir modül koyar ve onu döndürür.

    NEDEN GERÇEK MODÜLÜ MONKEYPATCH'LEMEK YERİNE BU: `pyautogui` ve
    `PyQt6` gibi paketler IMPORT ANINDA bir ekran/oturum ister ve
    başlıksız (headless) bir CI makinesinde `import` satırının kendisi
    patlar — üstelik "skip" olarak değil, TOPLAMA (collection) hatası
    olarak, yani tüm dosyayı düşürerek.

    Test edilen plugin'lerin hepsi bu modülleri `execute()` içinde tembel
    import ediyor (CLAUDE.md kuralı), dolayısıyla `sys.modules`'a konan
    bir sahte modül gerçek kod yolunu eksiksiz sınar. Testin gerçek
    paketi import etmesi hiçbir zaman gerekmiyordu.
    """

    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module
