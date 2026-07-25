"""Ollama sunucu sürecini ve model seçimini yönetir.

Kullanıcının her seferinde ayrı bir terminalde `ollama serve` çalıştırması
gerekmesin diye, Artemis başlarken sunucunun ayakta olup olmadığını
kontrol eder; değilse arka planda kendisi başlatır (ve yalnızca KENDİ
başlattığı sunucuyu, uygulama kapanırken kapatır — kullanıcının önceden
başka bir yerde başlattığı bir sunucuya asla dokunmaz).

Aynı şekilde, hangi modelin kullanılacağını `config.yaml`'a hardcode
etmek yerine, kurulu modelleri listeleyip kullanıcıya numaralandırılmış
bir seçim sunar (RAM/hız tercihine göre oturumdan oturuma değişebilir).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

_STARTUP_TIMEOUT_SECONDS = 15
_POLL_INTERVAL_SECONDS = 0.5


class OllamaUnavailableError(Exception):
    """`ollama` komutu bulunamadığında, sunucu başlatılamadığında veya
    hiç model kurulu olmadığında fırlatılır."""


class OllamaServerManager:
    """Ollama sunucusunun yaşam döngüsünü (varsa kullan, yoksa başlat) yönetir."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None  # yalnızca BİZ başlattıysak dolu olur

    def ensure_running(self) -> None:
        """Sunucu zaten çalışıyorsa dokunmaz; çalışmıyorsa arka planda başlatır.

        Raises:
            OllamaUnavailableError: `ollama` komutu PATH'te yoksa veya
                sunucu zaman aşımı içinde yanıt vermezse.
        """

        if self._is_server_responding():
            logger.info("Ollama sunucusu zaten çalışıyor.")
            return

        if shutil.which("ollama") is None:
            raise OllamaUnavailableError(
                "'ollama' komutu bulunamadı. https://ollama.com adresinden kurup PATH'e eklemeniz gerekiyor."
            )

        logger.info("Ollama sunucusu çalışmıyor, arka planda başlatılıyor ('ollama serve')...")
        self._process = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._is_server_responding():
                logger.info("Ollama sunucusu hazır.")
                return
            time.sleep(_POLL_INTERVAL_SECONDS)

        raise OllamaUnavailableError(f"Ollama sunucusu {_STARTUP_TIMEOUT_SECONDS} saniye içinde yanıt vermedi.")

    def stop_if_we_started_it(self) -> None:
        """Sunucuyu yalnızca BİZ başlattıysak kapatır.

        Kullanıcının kendi başka bir yerde (örn. ayrı bir terminalde)
        başlattığı bir sunucuya asla dokunulmaz — yalnızca `_process`
        doluysa (yani `ensure_running` sunucuyu biz başlattıysa) kapatılır.
        """

        if self._process is not None and self._process.poll() is None:
            logger.info("Artemis tarafından başlatılan Ollama sunucusu kapatılıyor.")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    @staticmethod
    def _is_server_responding() -> bool:
        try:
            import ollama

            ollama.list()
            return True
        except Exception:  # noqa: BLE001 - bağlantı hatası/vs. hepsi "çalışmıyor" sayılır
            return False


def list_installed_models() -> list[str]:
    """Yerel olarak çekilmiş (pulled) Ollama modellerinin adlarını döndürür.

    `ollama-python` sürümüne göre `ollama.list()` bir dict veya bir
    `ListResponse` nesnesi döndürebilir; ikisini de destekleyecek şekilde
    esnek okunur.

    Returns:
        Model adlarının listesi (örn. `["llama3.1:latest", "qwen2.5:7b"]`).
        Hiç model kuruluysa boş liste döner (hata fırlatmaz; çağıran
        taraf, `prompt_user_to_select_model` içinde bu durumu ele alır).
    """

    import ollama

    response = ollama.list()
    raw_models: list[Any] = response.get("models", []) if isinstance(response, dict) else getattr(response, "models", [])

    names: list[str] = []
    for raw_model in raw_models:
        if isinstance(raw_model, dict):
            name = raw_model.get("model") or raw_model.get("name")
        else:
            name = getattr(raw_model, "model", None) or getattr(raw_model, "name", None)
        if name:
            names.append(name)
    return names


def prompt_user_to_select_model(models: list[str]) -> str:
    """Kurulu modelleri numaralandırıp terminalden kullanıcı seçimi ister.

    Args:
        models: `list_installed_models()`'dan gelen model adları.

    Returns:
        Kullanıcının seçtiği model adı.

    Raises:
        OllamaUnavailableError: `models` boşsa (hiç model kurulu değilse).
    """

    if not models:
        raise OllamaUnavailableError(
            "Hiç Ollama modeli kurulu değil. Önce bir model çekin, örn: 'ollama pull llama3.1'"
        )

    print("\nKurulu Ollama modelleri:")
    for i, name in enumerate(models, start=1):
        print(f"  {i}) {name}")

    while True:
        choice = input(f"Hangi modeli kullanmak istiyorsunuz? (1-{len(models)}): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            selected = models[int(choice) - 1]
            print(f"Seçildi: {selected}\n")
            return selected
        print(f"Geçersiz seçim; 1 ile {len(models)} arasında bir sayı girin.")


def stop_all_ollama_processes() -> int:
    """Sistemde çalışan TÜM `ollama` süreçlerini zorla sonlandırır.

    RAM için bir "reset düğmesi": normalde `OllamaServerManager` yalnızca
    kendi başlattığı sunucuyu kapatır (bkz. modül dokümantasyonu). Ancak
    bir IDE'nin (örn. VS Code) "Stop" düğmesi Python sürecini sert şekilde
    kapatırsa (`finally` bloğu çalışmadan), arkada "yetim" bir `ollama
    serve` süreci ve yüklü model kalabilir. Bu fonksiyon, o durumda elle
    çağrılacak bir temizlik komutudur (`python main.py --stop-ollama`).

    Returns:
        Sonlandırılan süreç sayısı.
    """

    import psutil

    killed = 0
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if "ollama" in name:
            try:
                proc.terminate()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return killed
