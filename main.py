"""Artemis giriş noktası.

Bu dosya tüm parçaları birbirine bağlar: config, logging, plugin loader,
dispatcher ve LLM istemcisi. Gerçek üründe bu dosyanın üstüne bir de ses
döngüsü (Whisper STT -> ... -> Piper/pyttsx3 TTS) eklenecektir;
`core/conversation_loop.py` o döngünün metin-tabanlı, LLM'e bağlı halidir.

Kullanım:
    python main.py                # tek seferlik demo dispatch (LLM'siz)
    python main.py --chat          # gerçek Ollama sohbet döngüsü
    python main.py --stop-ollama   # RAM temizliği: yetim ollama süreçlerini kapatır
"""

from __future__ import annotations

import json
import logging
import signal
import sys

from config.settings import get_settings
from core.conversation_loop import run as run_conversation_loop
from core.dispatcher import ToolDispatcher
from core.llm_client import OllamaLLMClient
from core.manifest import build_tool_manifest
from core.ollama_manager import (
    OllamaServerManager,
    OllamaUnavailableError,
    list_installed_models,
    prompt_user_to_select_model,
    stop_all_ollama_processes,
)
from core.plugin_loader import load_plugins
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


def bootstrap() -> ToolDispatcher:
    """Uygulamayı başlatmak için gereken tüm adımları sırayla çalıştırır.

    Returns:
        Kullanıma hazır bir ToolDispatcher örneği.
    """

    settings = get_settings()
    setup_logging(settings.log_dir)
    load_plugins()

    manifest = build_tool_manifest()
    logger.info("Artemis başlatıldı. Yüklü tool sayısı: %d", len(manifest))

    return ToolDispatcher(settings=settings)


def main() -> None:
    """LLM olmadan, tek bir örnek tool çağrısını simüle eden demo.

    Gerçek kullanımda `raw_call`, yerel Ollama modelinin ürettiği JSON
    çıktısından `json.loads(...)` ile elde edilir; burada elle simüle
    edilmiştir. Uçtan uca (LLM dahil) akış için `--chat` ile çalıştırın.
    """

    dispatcher = bootstrap()

    example_call = {
        "tool": "filesystem.create_folder",
        "arguments": {"name": "ArtemisDemo", "location": "desktop"},
    }

    result = dispatcher.dispatch(example_call)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


def main_chat() -> None:
    """Gerçek Ollama modeline bağlı, uçtan uca sohbet döngüsünü başlatır.

    Artık `ollama serve`'i siz elle başlatmak zorunda değilsiniz: sunucu
    çalışmıyorsa Artemis onu arka planda kendisi başlatır (ve yalnızca
    KENDİ başlattığı sunucuyu, çıkışta kapatır — sizin ayrı bir yerde
    başlattığınız bir sunucuya dokunmaz). Hangi modeli kullanacağınızı
    da `config.yaml`'a yazmanıza gerek yok; kurulu modeller listelenir,
    numarayla seçersiniz.

    Kapanış sağlamlığı: Ctrl+C (SIGINT) ve SIGTERM için de sunucu
    temizliği tetiklenir. NOT: bir IDE'nin (VS Code'un "Stop" düğmesi
    gibi) süreci SERT şekilde (taskkill/TerminateProcess) kapatması
    durumunda hiçbir Python kodu (signal handler dahil) çalışamaz — bu
    durumda arkada bir "yetim" ollama süreci kalabilir. Böyle bir şüphe
    varsa `python main.py --stop-ollama` ile elle temizleyin, ya da bu
    interaktif komutu VS Code'un Debug/Run (F5) yerine düz bir terminalde
    çalıştırın (hem daha az bellek yer, hem Ctrl+C daha güvenilir çalışır).

    Ön koşullar (kullanıcının kendi makinesinde):
        1) Ollama kurulu olmalı (sunucuyu elle başlatmanıza gerek yok).
        2) En az bir model çekilmiş olmalı (örn. `ollama pull llama3.1`).
        3) `pip install -r requirements.txt` (özellikle `ollama` paketi).
    """

    dispatcher = bootstrap()
    server_manager = OllamaServerManager()

    def _handle_termination_signal(signum: int, frame: object) -> None:
        logger.info("Sinyal alındı (%s), Ollama sunucusu (varsa) kapatılıyor...", signum)
        server_manager.stop_if_we_started_it()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_termination_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_termination_signal)
    except (ValueError, AttributeError, OSError):
        pass  # bazı platformlar/thread bağlamları SIGTERM'i desteklemeyebilir

    try:
        server_manager.ensure_running()
        models = list_installed_models()
        selected_model = prompt_user_to_select_model(models)
    except OllamaUnavailableError as exc:
        print(f"Artemis başlatılamadı: {exc}")
        return

    llm_client = OllamaLLMClient(
        model=selected_model,
        use_native_tool_calling=dispatcher.settings.use_native_tool_calling,
        keep_alive=dispatcher.settings.ollama_keep_alive,
    )

    try:
        run_conversation_loop(dispatcher, llm_client)
    finally:
        server_manager.stop_if_we_started_it()


def main_stop_ollama() -> None:
    """`python main.py --stop-ollama`: RAM temizliği için tüm ollama
    süreçlerini zorla sonlandırır (bkz. `core.ollama_manager.stop_all_ollama_processes`)."""

    count = stop_all_ollama_processes()
    if count:
        print(f"{count} ollama süreci kapatıldı.")
    else:
        print("Çalışan bir ollama süreci bulunamadı.")


if __name__ == "__main__":
    if "--stop-ollama" in sys.argv:
        main_stop_ollama()
    elif "--chat" in sys.argv:
        main_chat()
    else:
        main()
