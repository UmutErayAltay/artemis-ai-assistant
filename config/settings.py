"""Uygulama geneli ayarlar.

Hardcode path ve magic string kullanımını engellemek için tüm yollar ve
güvenlik-kritik sabitler burada, pathlib ve Pydantic ile tek noktadan
yönetilir. Ayarlar bir YAML dosyasından okunur; dosya yoksa güvenli
varsayılanlarla çalışılır.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


class Settings(BaseModel):
    """Artemis'in çalışması için gereken tüm ayarlar.

    Attributes:
        desktop_path: Kullanıcının masaüstü klasörü.
        downloads_path: Kullanıcının indirilenler klasörü.
        log_dir: Log dosyalarının yazılacağı klasör.
        db_path: Hafıza (context memory) SQLite veritabanı dosyası.
        ollama_model: Kullanılacak yerel Ollama model adı.
        use_native_tool_calling: bkz. core.llm_client modül dokümantasyonu.
        ollama_keep_alive: Modelin son kullanımdan sonra RAM'de ne kadar
            süre tutulacağı (örn. "5m", "30s", "0" = hemen boşalt,
            "-1" = süresiz tut). Kısa değer = daha az idle RAM ama her
            "uyanışta" birkaç saniye yeniden yükleme gecikmesi.
        dangerous_tools: Ek olarak kısıtlanmak istenen tool adları.
            Not: Her tool zaten kendi `danger_level`'ını bildirir; bu
            liste, kod değiştirmeden config üzerinden ek/geçici bir
            kısıtlama uygulamak istendiğinde kullanılan ikincil bir
            güvenlik katmanıdır.
    """

    desktop_path: Path = Field(default_factory=lambda: Path.home() / "Desktop")
    downloads_path: Path = Field(default_factory=lambda: Path.home() / "Downloads")
    log_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "logs"
    )
    db_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent
        / "memory"
        / "artemis_memory.db"
    )
    ollama_model: str = "llama3.1"
    use_native_tool_calling: bool = False
    ollama_keep_alive: str = "1m"
    dangerous_tools: list[str] = Field(default_factory=list)


@lru_cache
def get_settings(config_path: Path | None = None) -> Settings:
    """Ayarları config.yaml dosyasından yükler (yoksa varsayılanları kullanır).

    `lru_cache` sayesinde uygulama boyunca tek bir Settings örneği
    paylaşılır. Testlerde farklı bir config gerekiyorsa doğrudan
    `Settings(...)` örneği oluşturup dispatcher'a enjekte etmek yeterlidir;
    bu fonksiyona dokunmaya gerek yoktur.

    Args:
        config_path: Alternatif bir config dosyası yolu (testler için).

    Returns:
        Doldurulmuş Settings nesnesi.
    """

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Settings()

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return Settings(**raw)
