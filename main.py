"""Artemis giriş noktası.

Bu dosya tüm parçaları birbirine bağlar: config, logging, plugin loader,
dispatcher ve LLM istemcisi. Gerçek üründe bu dosyanın üstüne bir de ses
döngüsü (Whisper STT -> ... -> Piper/pyttsx3 TTS) eklenecektir;
`core/conversation_loop.py` o döngünün metin-tabanlı, LLM'e bağlı halidir.

Kullanım:
    python main.py                # tek seferlik demo dispatch (LLM'siz)
    python main.py --chat          # gerçek sohbet döngüsü (terminal)
    python main.py --chat-gui      # Artemis paneli: geçmiş + ayarlar (ui/panel.py)
    python main.py --voice         # sesli asistan: "Artemis" deyince ekrana gelir
    python main.py --settings      # ayarlar penceresini tek başına açar
    python main.py --stop-ollama   # RAM temizliği: yetim ollama süreçlerini kapatır

`--chat`/`--chat-gui`/`--voice` beyni nerden alacağını `config.yaml::
llm_provider` belirler: "auto" (varsayılan — bulut/OpenRouter, olmazsa
yerel/Ollama), "cloud" (yalnızca OpenRouter) ya da "local" (yalnızca
Ollama). `--voice` tepsi menüsünden "Ayarlar" ile de bu pencere açılabilir.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys

from config.settings import Settings, get_openrouter_api_key, get_settings
from core.conversation_loop import run as run_conversation_loop
from core.dispatcher import ToolDispatcher
from core.llm_client import OllamaLLMClient
from core.llm_router import LLMRouter
from core.llm_types import LLMClient
from core.manifest import build_tool_manifest
from core.ollama_manager import (
    OllamaServerManager,
    OllamaUnavailableError,
    list_installed_models,
    prompt_user_to_select_model,
    stop_all_ollama_processes,
)
from core.openrouter_client import OpenRouterLLMClient
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


def _start_llm_session(settings: Settings) -> tuple[OllamaServerManager | None, LLMClient] | None:
    """Asistanın beynini hazırlar: `config.yaml::llm_provider` ne diyor ona göre
    ya OpenRouter (bulut) istemcisi, ya yerel Ollama, ya da ikisini seçen bir
    `core/llm_router.py::LLMRouter` kurar.

    Bu hazırlık `--chat` ve `--voice` yollarında birebir tekrarlanıyordu;
    bir ayar eklendiğinde (örn. `ollama_timeout_seconds`) iki yeri birden
    güncellemeyi unutmak kolaydı.

    ÜÇ DALLI YAPI NEDEN VAR (bkz. config/config.yaml'daki ayar bloğu):
        * "local": sunucu AÇILIŞTA ayağa kalkar, model seçtirilir.
          Dönen tip `LLMClient` olur.
        * "cloud": Ollama'ya hiç dokunulmaz, sunucu bile başlatılmaz.
          Kullanıcı "yalnızca bulut" dediyse sessizce yerele düşmek
          tercihini çiğnemek olurdu; yönlendirici de aynı sözü veriyor.
        * "auto": AÇILIŞTA OLLAMA'YA HİÇ DOKUNULMAZ — sunucu başlatılmaz,
          model seçtirilmez, `OllamaLLMClient` kurulmaz. Her şey
          yönlendiricinin tembel `local_factory`'sine sığınır: ilk gerçek
          istekte önce bulut (OpenRouter) denenir ve çalışıyorsa yerel
          sağlayıcıya hiç girilmez. Bulut başarısız olduğunda (anahtar yok,
          ağ yok, 429/5xx, soğuma) O ZAMAN — ve yalnızca o zaman — sunucu
          `ensure_running()` ile başlatılır; 15 saniyelik bekleme de sadece
          bu yedek yolda yaşar. (Eskiden bu, bulut HİÇ denenmeden
          açılışta oluyordu: kullanıcı her açılışta yerel sunucuyu
          bekletti, sonra buluta düştü.)

    "auto"da MODEL SEÇİMİ NEDEN SORULMUYOR: menü `input()` çağırır, yedek
    yol ise ilk konuşmada — `--voice` gibi KONSOLSUZ bir yolda bile —
    tetiklenebilir; kullanıcı görmediği bir konsol istemine cevap vermek
    zorunda kalır ve asistan kilitlenir. Bu yüzden yedek yol
    `config.yaml::ollama_model` değerini kullanır; modeli değiştirmek
    isteyen kullanıcı config'i düzenler ya da `llm_provider: "local"`
    seçip başlangıçtaki menüyü kullanır.

    Yedek yolda sunucu hiç ayağa kalkamazsa hata çağırana yükselir:
    bulut zaten denendi ve işe yaramadı, sessizce "başarılı" bir cevap
    üretmek dürüst olmazdı. Hata `ConnectionError` olarak yükseltilir ki
    dolaşım döngüleri (bkz. `core/conversation_loop.py`,
    `core/voice_loop.py`) onu yakalayıp Türkçe bir mesaj göstersin.

    Returns:
        (sunucu_yöneticisi, istemci) çifti. Sunucu yöneticisi yalnızca
        "cloud" modunda `None` olabilir — bu yüzden çağıran taraf
        `stop_if_we_started_it()` çağrılarını `server_manager is not None`
        ile korumak ZORUNDADIR (aksi halde "bulut modundayken"
        AttributeError ile çökeriz). "auto" modunda yönetici açılışta BOŞ
        bir nesne olarak döner (`ensure_running` hiç çağrılmaz); tembel
        fabrika aynı ÖRNEĞE yazar, böylece çıkışta kapatılan sunucu yine
        yalnızca bizim başlattığımız sunucudur.
    """

    def _cloud_client() -> LLMClient:
        """Bulut istemcisi. Anahtar constructor'da DOĞRULANMAZ; eksik anahtar
        yalnızca gerçek bir çağrıda hataya dönüşür (bkz.
        OpenRouterLLMClient.__init__). Bu, "auto" modda buluttan sessizce
        yerele düşmeyi mümkün kılan şeydir."""
        return OpenRouterLLMClient(
            model=settings.openrouter_model,
            api_key=get_openrouter_api_key(),
            timeout_seconds=settings.openrouter_timeout_seconds,
        )

    if settings.llm_provider == "cloud":
        # Ollama yoluna hiç girmiyoruz: sunucu yöneticisi kurulmaz, model
        # seçtirilmez.
        return None, _cloud_client()

    server_manager = OllamaServerManager()

    if settings.llm_provider == "local":
        # DEĞİŞMEYEN DAVRANIŞ: sunucu burada, açılışta kurulur.
        try:
            server_manager.ensure_running()
            selected_model = prompt_user_to_select_model(
                list_installed_models(), fallback_model=settings.ollama_model
            )
        except OllamaUnavailableError as exc:
            # Kullanıcı "yalnızca yerel" dediyse bile burada buluta
            # düşülür: `llm_client` modülü zaten aynı sözü veriyordu ve
            # eski davranış buydu.
            logger.warning("Yerel Ollama kullanılamıyor (%s); bulut LLM denenecek.", exc)
            return None, _cloud_client()

        return server_manager, OllamaLLMClient(
            model=selected_model,
            use_native_tool_calling=settings.use_native_tool_calling,
            keep_alive=settings.ollama_keep_alive,
            timeout_seconds=settings.ollama_timeout_seconds,
        )

    def _lazy_local_client() -> LLMClient:
        """Yedek yol: yalnızca bulut bu istekte başarısız olduğunda çağrılır.

        `ensure_running()` sunucu zaten ayaktaysa dokunmaz, kapalıysa arka
        planda başlatıp hazır olana kadar bekler (bkz.
        `core/ollama_manager.py`) — bu bekleme artık yalnızca yedeğe
        düşülen isteği geciktirir, uygulamanın AÇILIŞINI değil.

        Sunucu hiç ayağa kalkamazsa hata `ConnectionError` OLARAK
        yükseltilir: dolaşım döngüleri (`core/conversation_loop.py`,
        `core/voice_loop.py`) bu türü zaten yakalayıp kullanıcıya Türkçe
        bir mesaj gösteriyor; `OllamaUnavailableError` sızdırılsa ham bir
        traceback basılırdı. Sebep (`from exc`) korunur, hata yutulmaz.
        """
        try:
            server_manager.ensure_running()
        except OllamaUnavailableError as exc:
            raise ConnectionError(f"Yerel Ollama kullanılamıyor: {exc}") from exc

        return OllamaLLMClient(
            model=settings.ollama_model,
            use_native_tool_calling=settings.use_native_tool_calling,
            keep_alive=settings.ollama_keep_alive,
            timeout_seconds=settings.ollama_timeout_seconds,
        )

    router = LLMRouter(
        cloud_factory=_cloud_client,
        local_factory=_lazy_local_client,
        mode=settings.llm_provider,
    )
    return server_manager, router


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
    """Gerçek LLM'e bağlı, uçtan uca sohbet döngüsünü başlatır.

    Modelin nereden geldiği `config.yaml::llm_provider`'a bağlıdır: "local"
    ise yerel Ollama, "cloud" ise OpenRouter, "auto" ise
    `core/llm_router.py`'ın her çağrıda seçtiği sağlayıcı.

    Artık `ollama serve`'i siz elle başlatmak zorunda değilsiniz: sunucu
    çalışmıyorsa Artemis onu arka planda kendisi başlatır (ve yalnızca
    KENDİ başlattığı sunucuyu, çıkışta kapatır — sizin ayrı bir yerde
    başlattığınız bir sunucuya dokunmaz). Hangi modeli kullanacağınızı
    da `config.yaml`'a yazmanıza gerek yok; kurulu modeller listelenir,
    numarayla seçersiniz.

    "auto" modunda bu ikisi de YEDEK YOLDA olur: açılışta sunucu
    başlatılmaz, ilk istekte bulut (OpenRouter) denenir, o da olmazsa
    yerel sunucu başlatılır ve model `config.yaml::ollama_model`'den
    gelir (menü yalnızca `llm_provider: "local"` modunda sorulur).

    Kapanış sağlamlığı: Ctrl+C (SIGINT) ve SIGTERM için de sunucu
    temizliği tetiklenir. NOT: bir IDE'nin (VS Code'un "Stop" düğmesi
    gibi) süreci SERT şekilde (taskkill/TerminateProcess) kapatması
    durumunda hiçbir Python kodu (signal handler dahil) çalışamaz — bu
    durumda arkada bir "yetim" ollama süreci kalabilir. Böyle bir şüphe
    varsa `python main.py --stop-ollama` ile elle temizleyin, ya da bu
    interaktif komutu VS Code'un Debug/Run (F5) yerine düz bir terminalde
    çalıştırın (hem daha az bellek yer, hem Ctrl+C daha güvenilir çalışır).

    Ön koşullar (kullanıcının kendi makinesinde):
        1) En az bir LLM erişimi:
           - `llm_provider: "local"` (veya anahtarsız "auto") ise Ollama
             kurulu olmalı (sunucuyu elle başlatmanıza gerek yok) ve en az
             bir model çekilmiş olmalı (örn. `ollama pull llama3.1`).
           - `llm_provider: "cloud"`/`"auto"` ve anahtar tanımlıysa Ollama'ya
             hiç gerek yoktur; yalnızca `OPENROUTER_API_KEY` gerekir.
        2) `pip install -r requirements.txt` (özellikle `ollama` paketi).
    """

    dispatcher = bootstrap()

    session = _start_llm_session(dispatcher.settings)
    if session is None:
        return
    server_manager, llm_client = session

    def _handle_termination_signal(signum: int, frame: object) -> None:
        logger.info("Sinyal alındı (%s), Ollama sunucusu (varsa) kapatılıyor...", signum)
        if server_manager is not None:
            server_manager.stop_if_we_started_it()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_termination_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_termination_signal)
    except (ValueError, AttributeError, OSError):
        pass  # bazı platformlar/thread bağlamları SIGTERM'i desteklemeyebilir

    try:
        run_conversation_loop(dispatcher, llm_client)
    finally:
        if server_manager is not None:
            server_manager.stop_if_we_started_it()


def main_chat_gui() -> None:
    """`python main.py --chat-gui`: Artemis panelini açar (geçmiş + ayarlar).

    Artık mesaj YAZMA penceresi değildir: panel sohbet geçmişini okur,
    ayarları gösterir ve asistanın durumunu bildirir. Konuşmanın aracı
    `--chat` (terminal) ve `--voice`'dur; panel yalnızca bakmak içindir.

    `ui/chat_window.py::ChatWindow` SİLİNMEDİ — modül, `QThread`'li
    döngüsü ve onay köprüsüyle birlikte duruyor ve `tests/
    test_ui_chat_window.py` ona bağlı; yalnızca bu giriş noktası artık
    onu açmıyor. Geri bağlamak isteyen tek satırlık iştir.

    LLM oturumu (`_start_llm_session`) yine kurulur: durum şeridi
    çalışan sağlayıcının adını gerçekten yazabilsin diye. Panel bu
    istemciyi KULLANMAZ; sadece adını okur.
    """

    from PyQt6.QtWidgets import QApplication

    from ui.panel import show_panel

    dispatcher = bootstrap()

    session = _start_llm_session(dispatcher.settings)
    if session is None:
        return
    server_manager, llm_client = session

    app = QApplication(sys.argv)
    show_panel(dispatcher.settings, llm_client)

    try:
        sys.exit(app.exec())
    finally:
        if server_manager is not None:
            server_manager.stop_if_we_started_it()


def main_settings() -> None:
    """`python main.py --settings`: ayarlar penceresini TEK BAŞINA açar.

    Bilerek `bootstrap()`/`_start_llm_session()` ÇAĞIRMAZ: burada ne
    plugin yüklemesi ne bir LLM oturumu gerekir, yalnızca `config.yaml`'ı
    okuyup/yazan bir form. `--voice` çalışırken aynı pencere tepsi
    menüsünden ("Ayarlar") de açılabilir — orada zaten çalışan bir
    `QApplication` vardır, burada ayrıca bir tane kurulur.
    """

    from PyQt6.QtWidgets import QApplication

    from ui.settings_window import show_settings

    setup_logging(get_settings().log_dir)

    app = QApplication(sys.argv)
    show_settings()
    sys.exit(app.exec())


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
    from ui.panel import show_panel
    from ui.settings_window import show_settings
    from ui.tray import ArtemisTray

    session = _start_llm_session(settings)
    if session is None:
        return
    server_manager, llm_client = session

    app = QApplication(sys.argv)
    # KRİTİK: overlay gizlendiğinde son pencere kapanmış sayılır. Bu bayrak
    # olmadan Qt, Artemis ilk kez sustuğu anda tüm uygulamayı kapatır.
    app.setQuitOnLastWindowClosed(False)

    overlay = ArtemisOverlay()
    assistant = VoiceAssistant(dispatcher, llm_client, overlay, settings)

    hotkey_text = settings.voice_hotkey
    hotkey: GlobalHotkey | None = None
    try:
        candidate = GlobalHotkey(hotkey_text, assistant.trigger)
        # ÖNCE kaydet, SONRA filtreyi kur. `app.installNativeEventFilter`
        # Qt tarafında C++ seviyesinde bir referans tutar; eskiden filtre
        # `register()`'dan ÖNCE kuruluyordu ve kayıt başarısız olduğunda
        # Python tarafı `hotkey = None` ile son referansını düşürüyordu —
        # Qt ise `removeNativeEventFilter` hiç çağrılmadığı için ham
        # işaretçiyi tutmaya devam ediyordu (bellek sızıntısı / potansiyel
        # kullanım-sonrası-serbest bırakma riski). Şimdi filtre yalnızca
        # kayıt GERÇEKTEN başarılıysa kurulur.
        if candidate.register():
            app.installNativeEventFilter(candidate)
            hotkey = candidate
    except HotkeyParseError as exc:
        logger.warning("Kısayol ayarı geçersiz (%s); kısayol olmadan devam ediliyor.", exc)
        hotkey = None

    tray = ArtemisTray(
        on_listen=assistant.trigger,
        on_quit=app.quit,
        hotkey_text=hotkey_text if hotkey else "",
        on_settings=show_settings,
        # Panel, `--chat-gui`'nin açtığı AYNI pencere: sesli modda da
        # geçmiş + ayarlar tepsi menüsünden bir tıkla erişilir. `functools
        # .partial` gerekir çünkü `show_panel` ayar + istemci ister, menü
        # geri çağırması ise ek parametresizdir.
        on_panel=lambda: show_panel(settings, llm_client),
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
            # Kayıt sırasıyla ters: önce Qt'nin native event filter
            # listesinden çıkar, sonra işletim sistemi kaydını kaldır.
            app.removeNativeEventFilter(hotkey)
            hotkey.unregister()
        tray.hide()
        if server_manager is not None:
            server_manager.stop_if_we_started_it()


def main_stop_ollama() -> None:
    """`python main.py --stop-ollama`: RAM temizliği için tüm ollama
    süreçlerini zorla sonlandırır (bkz. `core.ollama_manager.stop_all_ollama_processes`)."""

    count = stop_all_ollama_processes()
    if count:
        print(f"{count} ollama süreci kapatıldı.")
    else:
        print("Çalışan bir ollama süreci bulunamadı.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Komut satırı seçeneklerini ayrıştırır.

    Elle `sys.argv` taraması yerine `argparse`: yazım hatası yapan bir
    kullanıcı (`--voise`) sessizce LLM'siz demo moduna düşüyordu, çünkü
    tarama yalnızca bilinen üç dizeyi arıyor, bilinmeyeni yok sayıyordu.
    Artık bilinmeyen seçenek hata verir ve `--help` çalışır.
    """

    parser = argparse.ArgumentParser(
        prog="artemis",
        description="Yerel çalışan, Türkçe konuşan Ollama tabanlı masaüstü asistanı.",
    )
    mod = parser.add_mutually_exclusive_group()
    mod.add_argument("--chat", action="store_true", help="Terminal tabanlı sohbet döngüsü.")
    mod.add_argument(
        "--chat-gui",
        action="store_true",
        dest="chat_gui",
        help="Artemis paneli: sohbet geçmişi + ayarlar (ui/panel.py).",
    )
    mod.add_argument("--voice", action="store_true", help="Sesli asistan (tepsi + overlay).")
    mod.add_argument(
        "--settings",
        action="store_true",
        help="Ayarlar penceresini tek başına açar (ui/settings_window.py).",
    )
    mod.add_argument(
        "--stop-ollama",
        action="store_true",
        dest="stop_ollama",
        help="RAM temizliği: yetim ollama süreçlerini kapatır.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.stop_ollama:
        main_stop_ollama()
    elif args.settings:
        main_settings()
    elif args.voice:
        main_voice()
    elif args.chat_gui:
        main_chat_gui()
    elif args.chat:
        main_chat()
    else:
        main()
