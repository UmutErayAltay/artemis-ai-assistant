"""Artemis giriş noktası.

Bu dosya tüm parçaları birbirine bağlar: config, logging, plugin loader,
dispatcher ve LLM istemcisi. Gerçek üründe bu dosyanın üstüne bir de ses
döngüsü (Whisper STT -> ... -> Piper/pyttsx3 TTS) eklenecektir;
`core/conversation_loop.py` o döngünün metin-tabanlı, LLM'e bağlı halidir.

Kullanım:
    python main.py                # tek seferlik demo dispatch (LLM'siz)
    python main.py --chat          # gerçek Ollama sohbet döngüsü (terminal)
    python main.py --voice         # sesli asistan: "Artemis" deyince ekrana gelir
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
        timeout_seconds=dispatcher.settings.ollama_timeout_seconds,
    )

    try:
        run_conversation_loop(dispatcher, llm_client)
    finally:
        server_manager.stop_if_we_started_it()


def main_voice() -> None:
    """`python main.py --voice`: sesli asistanı arka planda başlatır.

    Artemis penceresiz çalışır; yalnızca adı söylendiğinde (veya kısayol
    tuşuna basıldığında) ekranın altında Siri benzeri bir pencere belirir.
    Uygulamayı kapatmak için sistem tepsisindeki simgeyi kullanın.

    Ön koşullar:
        1) `pip install -r requirements.txt`
        2) `python scripts/setup_voice.py` (ses modellerini indirir)
        3) Ollama kurulu ve en az bir model çekilmiş olmalı.
    """

    dispatcher = bootstrap()
    settings = dispatcher.settings

    # `voice_enabled: false` "mikrofon hiç açılmaz" anlamına gelir; bu
    # sözü tutmanın tek yolu ses işçisini hiç başlatmamaktır. Ollama'yı
    # başlatmadan ÖNCE kontrol edilir ki gereksiz yere sunucu ayağa
    # kalkmasın ve model seçimi sorulmasın.
    #
    # Bu kontrol, GUI/ses yığınının import'undan da ÖNCE yapılır: ses
    # kapalıyken PyQt6'yı (ve dolayısıyla bir pencere sistemi bağlantısını)
    # yüklemenin bir sebebi yok. Yan faydası, `--voice`'un kapalıyken
    # başlıksız bir makinede de dürüstçe "kapalı" diyebilmesi.
    if not settings.voice_enabled:
        print(
            "Sesli asistan config.yaml'da kapalı (voice_enabled: false).\n"
            "Açmak için bu ayarı true yapın, ya da metin modunda çalıştırın:\n"
            "    python main.py --chat"
        )
        return

    from PyQt6.QtWidgets import QApplication

    from core.voice_loop import VoiceAssistant
    from ui.hotkey import GlobalHotkey, HotkeyParseError
    from ui.overlay import ArtemisOverlay
    from ui.tray import ArtemisTray

    server_manager = OllamaServerManager()

    try:
        server_manager.ensure_running()
        selected_model = prompt_user_to_select_model(list_installed_models())
    except OllamaUnavailableError as exc:
        print(f"Artemis başlatılamadı: {exc}")
        return

    llm_client = OllamaLLMClient(
        model=selected_model,
        use_native_tool_calling=settings.use_native_tool_calling,
        keep_alive=settings.ollama_keep_alive,
        timeout_seconds=settings.ollama_timeout_seconds,
    )

    app = QApplication(sys.argv)
    # KRİTİK: overlay gizlendiğinde son pencere kapanmış sayılır. Bu bayrak
    # olmadan Qt, Artemis ilk kez sustuğu anda tüm uygulamayı kapatır.
    app.setQuitOnLastWindowClosed(False)

    overlay = ArtemisOverlay()
    assistant = VoiceAssistant(dispatcher, llm_client, overlay, settings)

    hotkey_text = settings.voice_hotkey
    hotkey: GlobalHotkey | None = None
    try:
        hotkey = GlobalHotkey(hotkey_text, assistant.trigger)
        app.installNativeEventFilter(hotkey)
        if not hotkey.register():
            hotkey = None
    except HotkeyParseError as exc:
        logger.warning("Kısayol ayarı geçersiz (%s); kısayol olmadan devam ediliyor.", exc)
        hotkey = None

    tray = ArtemisTray(
        on_listen=assistant.trigger,
        on_quit=app.quit,
        hotkey_text=hotkey_text if hotkey else "",
    )
    tray.show()

    assistant.start()

    print("Artemis dinlemede. Adını söyleyin veya tepsi simgesinden 'Şimdi dinle' deyin.")
    if hotkey:
        print(f"Kısayol: {hotkey_text}")
    print("Çıkmak için tepsi simgesine sağ tıklayıp 'Çıkış' deyin.")

    try:
        app.exec()
    finally:
        assistant.stop()
        if hotkey is not None:
            hotkey.unregister()
        tray.hide()
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
    elif "--voice" in sys.argv:
        main_voice()
    elif "--chat" in sys.argv:
        main_chat()
    else:
        main()
