"""LLM istemcilerinin ortak sözleşmesi.

Neden ayrı bir modül: `OllamaLLMClient`, `OpenRouterLLMClient` ve
`LLMRouter` üçü de aynı dört metodu sunar, ama hiçbiri diğerinden
türetilmez — üçü de `voice/router.py`'deki sağlayıcılar gibi bağımsız
uygulamalardır. Ortak arayüz bir sınıf değil bir PROTOKOL olduğu için
kopyalanmaz; burada tek yerde yazılır.

Bu, `scripts/_toolbench_vendor.py::LLMLike` için konulan öncülün
(Protocol ile tek yeteneği tanımlayıp bağımlılığı yapılandırmak) genel
hali: kıyas betiği tek bir metot için bir Protocol kullanıyordu, burada
dolaşım döngüleri dört metotluk bir sözleşme için aynı şeyi yapıyor.
"""

from __future__ import annotations

from typing import Any, Protocol


class LLMClient(Protocol):
    """OllamaLLMClient, OpenRouterLLMClient ve LLMRouter'ın ortak sözleşmesi.

    `core/conversation_loop.py` ve `core/voice_loop.py` bu üçünü örnek
    tiplemesiyle birbirinin yerine kullanır; tip belirtimi
    (`llm_client: LLMClient`) IDE/mypy için doğrulukla kalsın, gerçek
    dispatch tamamen duck-typing'dir.

    Burada `raise`/`return` gövdesi YOK: bir Protocol o sınıfların
    gerçekten uyguladığı metotlardan ibaret olmalı. Yeni bir sağlayıcı
    eklendiğinde bu sözleşmenin genişletilip genişletilmeyeceği sorusu
    buradan görülebilir — `LLMRouter`'ın `_run`'ı da yalnızca bu
    metotları çağırır. Dördüncü metot `get_structured_response`
    (ARCHITECTURE.md §42) tam olarak böyle eklendi: `get_raw_response` tool-call şemasına
    kilitli olduğu için başka bir JSON sözleşmesi konuşan proje görüşmesi
    onu kullanamıyordu.
    """

    def get_raw_response(self, system_prompt: str, user_input: str) -> str: ...

    def get_structured_response(
        self, system_prompt: str, user_input: str, schema: dict[str, Any], schema_name: str = "cevap"
    ) -> dict[str, Any]: ...

    def get_tool_calls(
        self, system_prompt: str, user_input: str, max_retries: int = 2
    ) -> list[dict[str, Any]]: ...

    def should_engage(self, user_input: str) -> bool: ...
