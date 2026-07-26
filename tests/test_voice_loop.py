"""Sesli asistan durum makinesinin uçtan uca (entegrasyon) testleri.

Buradaki testlerin amacı tek tek parçaları değil, PARÇALAR ARASINDAKİ
SÖZLEŞMEYİ doğrulamaktır: `voice_loop`'un `SpeechRecorder`, `SpeechToText`,
`TextToSpeech`, `TaskPlanner` ve overlay'i gerçekten doğru imzalarla
çağırıp çağırmadığı. Her modülün kendi testi geçse bile aralarındaki
bağlantı kopuk olabilir; bu dosya tam olarak o boşluğu kapatır.

Mikrofon, Whisper, Piper ve Ollama sahte nesnelerle değiştirilir —
gerçek donanım veya model dosyası gerekmez. Ama `ToolDispatcher`,
`TaskPlanner` ve `filesystem_plugin` GERÇEKTİR: komutun sonunda gerçekten
bir klasör oluşur, gerçekten bir onay kapısı işler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.plugin_loader import load_plugins
from core.voice_loop import VoiceAssistant, speakable
from memory.context_memory import ContextMemory
from voice.audio import BLOCK_FRAMES

load_plugins()


# --------------------------------------------------------------------------
# Sahte parçalar
# --------------------------------------------------------------------------


class FakeOverlay:
    """Arayüz yerine geçer; hangi durumların gösterildiğini kaydeder."""

    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []
        self.amplitudes: list[float] = []
        self.heard: list[str] = []
        self.dismissed = 0

    def set_heard(self, text: str) -> None:
        self.heard.append(text)

    def show_listening(self, text: str = "") -> None:
        self.states.append(("listening", text))

    def show_thinking(self, text: str = "") -> None:
        self.states.append(("thinking", text))

    def show_speaking(self, text: str = "") -> None:
        self.states.append(("speaking", text))

    def show_error(self, text: str) -> None:
        self.states.append(("error", text))

    def set_amplitude(self, amplitude: float) -> None:
        self.amplitudes.append(amplitude)

    def dismiss(self) -> None:
        self.dismissed += 1

    @property
    def state_names(self) -> list[str]:
        return [name for name, _ in self.states]


class FakeMicrophone:
    """Önceden hazırlanmış ses bloklarını sırayla veren sahte mikrofon."""

    def __init__(self, blocks: list[bytes]) -> None:
        self._blocks = list(blocks)
        self.reads = 0

    def read_block(self) -> bytes:
        self.reads += 1
        if self._blocks:
            return self._blocks.pop(0)
        return _silence()  # tükendiyse sessizlik ver (kayıt böylece biter)


class FakeLLM:
    """`OllamaLLMClient` yerine geçer; sabit bir tool-call listesi döndürür."""

    def __init__(self, tool_calls: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self._tool_calls = tool_calls or []
        self._error = error
        self.prompts: list[str] = []

    def get_tool_calls(self, system_prompt: str, user_input: str) -> list[dict[str, Any]]:
        self.prompts.append(user_input)
        if self._error is not None:
            raise self._error
        return self._tool_calls


class FakeSTT:
    """Whisper yerine geçer; sabit bir metin döndürür.

    `hotwords`'ü de kaydeder: özel isim ipucunun gerçekten modele kadar
    ulaştığı bu sayede doğrulanabilir (yabancı uygulama adlarının doğru
    tanınmasının tek dayanağı budur).
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0
        self.hotwords: list[str | None] = []

    def transcribe(self, audio: bytes, hotwords: str | None = None) -> str:
        self.calls += 1
        self.hotwords.append(hotwords)
        return self._text


class FakeTTS:
    """Piper yerine geçer; söylenenleri kaydeder ve genlik bildirir."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str, on_amplitude=None) -> None:
        self.spoken.append(text)
        if on_amplitude is not None:
            on_amplitude(0.5)  # konuşurken dalga formu canlanmalı

    def stop(self) -> None:
        pass


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------


def _silence() -> bytes:
    """Tamamen sessiz bir ses bloğu."""

    return b"\x00\x00" * BLOCK_FRAMES


def _speech() -> bytes:
    """Konuşma sayılacak kadar yüksek genlikli bir ses bloğu."""

    # ~8000 genliğinde kare dalga: sessizlik eşiğinin belirgin biçimde üstünde.
    return (8000).to_bytes(2, "little", signed=True) * BLOCK_FRAMES


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Her şeyi tmp_path altında tutan, kısa zaman aşımlı ayarlar."""

    return Settings(
        desktop_path=tmp_path / "Desktop",
        downloads_path=tmp_path / "Downloads",
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
        silence_timeout_seconds=0.3,
        max_command_seconds=3.0,
    )


@pytest.fixture
def dispatcher(settings: Settings) -> ToolDispatcher:
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


def _build_assistant(
    dispatcher: ToolDispatcher,
    settings: Settings,
    overlay: FakeOverlay,
    llm: FakeLLM,
    transcript: str,
) -> tuple[VoiceAssistant, FakeTTS]:
    """Ağır bileşenleri sahteleriyle değiştirilmiş bir VoiceAssistant kurar."""

    assistant = VoiceAssistant(dispatcher, llm, overlay, settings)
    tts = FakeTTS()
    assistant._stt = FakeSTT(transcript)
    assistant._tts = tts
    # Başlat Menüsü taraması yavaştır ve makineye göre değişir; testlerde
    # sözlük sabitlenir (içeriği değil, İLETİLDİĞİ doğrulanacak).
    assistant._vocabulary = "artemis, discord, steam"
    return assistant, tts


# --------------------------------------------------------------------------
# Testler
# --------------------------------------------------------------------------


def test_single_command_flows_from_speech_to_real_filesystem_change(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Konuşma → metin → LLM → tool → cevap zinciri gerçekten çalışmalı.

    Bu testin asıl değeri: sonunda GERÇEK bir klasör oluşur. Yani
    voice_loop'un planner/dispatcher'a bağlantısı sahte değil, gerçektir.
    """

    overlay = FakeOverlay()
    llm = FakeLLM([{"tool": "filesystem.create_folder", "arguments": {"name": "Orbit", "location": "desktop"}}])
    assistant, tts = _build_assistant(dispatcher, settings, overlay, llm, "masaüstünde orbit klasörü oluştur")

    mic = FakeMicrophone([_speech()] * 4 + [_silence()] * 6)
    assistant._handle_one_command(mic)

    # 1) Gerçekten dosya sistemi değişti mi?
    assert (settings.desktop_path / "Orbit").is_dir()

    # 2) LLM'e gerçek döküm gitti mi?
    assert llm.prompts == ["masaüstünde orbit klasörü oluştur"]

    # 3) Arayüz doğru sırada sürüldü mü?
    assert overlay.state_names == ["listening", "thinking", "speaking"]
    assert overlay.dismissed == 1

    # 3b) Kullanıcının ne dediği ekranda ayrıca gösterildi mi? Yanlış
    # anlaşılmanın fark edilebilmesinin tek yolu bu (bkz. README §19).
    assert overlay.heard == ["masaüstünde orbit klasörü oluştur"]

    # 4) Cevap sesli okundu mu?
    assert len(tts.spoken) == 1
    assert "Orbit" in tts.spoken[0]


def test_empty_transcript_shows_error_and_never_calls_llm(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Hiçbir şey duyulmadıysa LLM'e boşuna gidilmemeli."""

    overlay = FakeOverlay()
    llm = FakeLLM([{"tool": "filesystem.create_folder", "arguments": {"name": "Olmamali"}}])
    assistant, _ = _build_assistant(dispatcher, settings, overlay, llm, "")

    assistant._handle_one_command(FakeMicrophone([_silence()] * 6))

    assert llm.prompts == []  # LLM hiç çağrılmadı
    assert overlay.state_names == ["listening", "error"]
    assert not (settings.desktop_path / "Olmamali").exists()


def test_connection_error_is_reported_without_crashing(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Ollama kapalıysa asistan çökmemeli, kullanıcıya durumu söylemeli."""

    overlay = FakeOverlay()
    llm = FakeLLM(error=ConnectionError("sunucu yok"))
    assistant, tts = _build_assistant(dispatcher, settings, overlay, llm, "bir şey yap")

    assistant._handle_one_command(FakeMicrophone([_speech()] * 4 + [_silence()] * 6))

    assert overlay.state_names[-1] == "error"
    assert any("model" in message.lower() for message in tts.spoken)


def test_dangerous_tool_is_blocked_when_voice_confirmation_is_not_understood(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """GÜVENLİK: anlaşılmayan bir onay cevabı RED sayılmalı, dosya durmalı.

    `filesystem.delete` `CONFIRM_REQUIRED`'dır. Sesli onayda kullanıcının
    cevabı "kapıyı kapat" gibi alakasız bir şeye çözülürse, işlem
    ÇALIŞTIRILMAMALIDIR (bkz. README §17c: "şüphede reddet").
    """

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    victim = settings.desktop_path / "onemli.txt"
    victim.write_text("silinmemeli", encoding="utf-8")

    overlay = FakeOverlay()
    llm = FakeLLM([{"tool": "filesystem.delete", "arguments": {"target": "onemli.txt", "location": "desktop"}}])
    assistant, _ = _build_assistant(dispatcher, settings, overlay, llm, "onemli.txt dosyasını sil")

    # Onay dinlemesi de aynı mikrofon akışını kullanır (bkz. _active_mic).
    mic = FakeMicrophone([_speech()] * 4 + [_silence()] * 6)
    assistant._active_mic = mic
    assistant._stt = FakeSTT("kapıyı kapat")  # alakasız cevap = onay yok

    assistant._handle_one_command(mic)

    assert victim.exists(), "Onaylanmayan silme işlemi dosyayı YOK ETMEMELİ"
    assert victim.read_text(encoding="utf-8") == "silinmemeli"


def test_dangerous_tool_runs_after_clear_spoken_approval(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Net bir "evet" duyulduğunda işlem gerçekten çalışmalı."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    victim = settings.desktop_path / "gecici.txt"
    victim.write_text("silinebilir", encoding="utf-8")

    overlay = FakeOverlay()
    llm = FakeLLM([{"tool": "filesystem.delete", "arguments": {"target": "gecici.txt", "location": "desktop"}}])
    assistant, _ = _build_assistant(dispatcher, settings, overlay, llm, "gecici.txt dosyasını sil")

    mic = FakeMicrophone([_speech()] * 4 + [_silence()] * 6)
    assistant._active_mic = mic
    assistant._stt = FakeSTT("evet")

    assistant._handle_one_command(mic)

    assert not victim.exists(), "Onaylanan silme işlemi gerçekleşmeliydi"


def test_confirmation_without_active_microphone_is_refused(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Mikrofon yoksa onay ALINAMAZ; bu da RED demektir (sessizce evet değil)."""

    overlay = FakeOverlay()
    assistant = VoiceAssistant(dispatcher, FakeLLM(), overlay, settings)
    assistant._tts = FakeTTS()
    assistant._active_mic = None

    assert assistant._confirm_by_voice("filesystem.delete", {"target": "x"}) is False


def test_app_vocabulary_is_passed_to_speech_recognition(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Özel isim ipucu (hotwords) Whisper'a GERÇEKTEN ulaşmalı.

    Yabancı uygulama adlarının ("Discord", "Riot Client") doğru tanınması
    tamamen buna bağlıdır; ipucu yolda düşerse tanıma sessizce eski kötü
    haline döner ve bunu fark etmek zor olur (bkz. README §18).
    """

    overlay = FakeOverlay()
    assistant, _ = _build_assistant(dispatcher, settings, overlay, FakeLLM(), "discord aç")

    assistant._record_and_transcribe(FakeMicrophone([_speech()] * 4 + [_silence()] * 6))

    stt = assistant._stt
    assert stt.calls == 1
    assert stt.hotwords[0] is not None, "hotwords hiç geçirilmedi"
    assert "discord" in stt.hotwords[0]


def test_vocabulary_includes_turkish_command_words(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """İpucu sözlüğü yalnızca İngilizce uygulama adlarından oluşmamalı.

    Sözlük tamamen İngilizce olduğunda kod çözücü İngilizceye kayıyor ve
    Türkçe komut fiillerini yabancı sözcüklere benzetiyordu — gerçekte
    yaşanan hata: "League of Legends aç" -> "league of legends such",
    bu da LLM'in yanlış tool (web.open_url) seçmesine yol açtı.
    """

    assistant = VoiceAssistant(dispatcher, FakeLLM(), FakeOverlay(), settings)
    vocabulary = assistant._ensure_vocabulary()

    assert "aç" in vocabulary
    assert "oluştur" in vocabulary
    assert "klasör" in vocabulary


def test_confirmation_uses_affirmative_hotwords_not_app_names(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Onay dinlerken ipucu ONAY sözcükleri olmalı, uygulama adları değil.

    Beklenen cevap "evet"tir; modele uygulama sözlüğü vermek onu
    olmayacak bir kelimeye doğru çeker ve onayı kaçırmaya yol açar.
    """

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    (settings.desktop_path / "x.txt").write_text("veri", encoding="utf-8")

    overlay = FakeOverlay()
    llm = FakeLLM([{"tool": "filesystem.delete", "arguments": {"target": "x.txt", "location": "desktop"}}])
    assistant, _ = _build_assistant(dispatcher, settings, overlay, llm, "evet")

    mic = FakeMicrophone([_speech()] * 4 + [_silence()] * 6)
    assistant._active_mic = mic
    assistant._handle_one_command(mic)

    # Son transcribe çağrısı onay dinlemesidir.
    confirmation_hotwords = assistant._stt.hotwords[-1]
    assert confirmation_hotwords is not None
    assert "evet" in confirmation_hotwords
    assert "discord" not in confirmation_hotwords


class _ExplodingDetector:
    """Her `feed()` çağrısında patlayan sahte uyandırma algılayıcısı.

    Gerçek dünyada bu, eksik CUDA kütüphanesi ("Library cublas64_12.dll is
    not found") gibi bir donanım/kurulum sorunuydu.
    """

    def __init__(self) -> None:
        self.calls = 0

    def feed(self, block: bytes) -> bool:
        self.calls += 1
        raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")


def test_wake_word_failure_does_not_kill_the_assistant(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Uyandırma bozulsa bile asistan kısayolla çalışmaya DEVAM etmeli.

    Bu hata bir kez gerçekten yaşandı: `feed()` içindeki bir CUDA hatası
    ses işçisinin tamamını öldürüyordu, yani kısayol tuşu da dahil hiçbir
    şey çalışmıyordu. Doğru davranış: uyandırmayı kapat, kullanıcıyı
    bilgilendir, kısayolla uyandırılabilir kal.
    """

    overlay = FakeOverlay()
    assistant = VoiceAssistant(dispatcher, FakeLLM(), overlay, settings)

    detector = _ExplodingDetector()
    assistant._wake_detector = detector

    # Kısayol tetiklenince döngü normal şekilde dönmeli.
    assistant._manual_trigger.set()
    assistant._sleep_until_woken(FakeMicrophone([_silence()] * 3))

    # İkinci turda algılayıcı patlar ama döngü ölmemeli; kısayolla çıkılır.
    assistant._manual_trigger.clear()

    def _trigger_after_failure() -> None:
        assistant._manual_trigger.set()

    mic = FakeMicrophone([_speech()] * 2)
    original_read = mic.read_block

    def read_and_then_trigger() -> bytes:
        block = original_read()
        _trigger_after_failure()  # patlamadan sonra kısayolla çıkabilelim
        return block

    mic.read_block = read_and_then_trigger  # type: ignore[method-assign]
    assistant._sleep_until_woken(mic)

    assert assistant._wake_word_broken is True
    assert any(state == "error" for state, _ in overlay.states)
    assert any("kısayol" in text.lower() for _, text in overlay.states)


def test_broken_wake_word_is_not_retried_every_block(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """Bozuk algılayıcı her ses bloğunda yeniden denenmemeli.

    Denense saniyede birkaç kez aynı hata üretilir; log ve işlemci boğulur.
    """

    assistant = VoiceAssistant(dispatcher, FakeLLM(), FakeOverlay(), settings)
    detector = _ExplodingDetector()
    assistant._wake_detector = detector

    mic = FakeMicrophone([_speech()] * 12)
    original_read = mic.read_block
    reads = {"n": 0}

    def read_and_stop() -> bytes:
        reads["n"] += 1
        if reads["n"] > 8:
            assistant._stop_event.set()
        return original_read()

    mic.read_block = read_and_stop  # type: ignore[method-assign]
    assistant._sleep_until_woken(mic)

    assert detector.calls == 1, "Bozuk algılayıcı yalnızca bir kez denenmeliydi"


def test_voice_enabled_false_prevents_any_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`voice_enabled: false` sözünü tutmalı: ne mikrofon ne Ollama başlamalı.

    Bu ayar bir dönem `config.yaml` ve `Settings`'te BELGELİYDİ ama hiçbir
    kod onu okumuyordu — kullanıcı kapatsa bile mikrofon açılıyordu. Bu
    test o sessiz kopmanın tekrarını engeller.
    """

    import main

    settings = Settings(voice_enabled=False)

    class _Dispatcher:
        pass

    dispatcher = _Dispatcher()
    dispatcher.settings = settings

    started: list[str] = []

    class _ExplodingServerManager:
        def __init__(self) -> None:
            started.append("ollama")  # buraya hiç gelinmemeli

    monkeypatch.setattr(main, "bootstrap", lambda: dispatcher)
    monkeypatch.setattr(main, "OllamaServerManager", _ExplodingServerManager)

    main.main_voice()

    assert started == [], "voice_enabled=False iken Ollama sunucusu başlatılmamalıydı"


def test_provider_mode_from_settings_reaches_the_router(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """`stt_provider`/`tts_provider` ayarı gerçekten yönlendiriciye geçmeli."""

    settings.stt_provider = "local"
    settings.tts_provider = "cloud"
    assistant = VoiceAssistant(dispatcher, FakeLLM(), FakeOverlay(), settings)

    assert assistant._ensure_stt().mode == "local"
    assert assistant._ensure_tts().mode == "cloud"


def test_local_provider_mode_never_constructs_cloud_backend(
    dispatcher: ToolDispatcher, settings: Settings
) -> None:
    """GİZLİLİK: 'local' modda bulut sağlayıcı OLUŞTURULMAMALI bile.

    Kullanıcı sesinin dışarı çıkmamasını istediğinde, bulut istemcisinin
    kurulması (ve anahtarın okunması) bile gereksizdir. Bu test, bulut
    fabrikasının hiç çağrılmadığını kanıtlar.
    """

    settings.stt_provider = "local"
    assistant = VoiceAssistant(dispatcher, FakeLLM(), FakeOverlay(), settings)

    router = assistant._ensure_stt()
    cloud_built: list[str] = []
    router._cloud_factory = lambda: cloud_built.append("bulut")  # type: ignore[assignment]
    router._local_factory = lambda: FakeSTT("yerel")  # type: ignore[assignment]

    assert router.transcribe(b"ses", hotwords="x") == "yerel"
    assert cloud_built == [], "'local' modda bulut sağlayıcı kurulmamalıydı"


def test_amplitude_is_reported_while_listening(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """Dalga formunun canlanması için dinlerken genlik bildirilmeli."""

    overlay = FakeOverlay()
    assistant, _ = _build_assistant(dispatcher, settings, overlay, FakeLLM(), "merhaba")

    assistant._record_and_transcribe(FakeMicrophone([_speech()] * 4 + [_silence()] * 6))

    assert overlay.amplitudes, "Dinlerken hiç genlik bildirilmedi"
    assert max(overlay.amplitudes) > 0.1, "Konuşma bloklarında genlik yükselmeliydi"


# --------------------------------------------------------------------------
# speakable() — sesli okumaya uygun kısaltma
# --------------------------------------------------------------------------


def test_speakable_reduces_windows_paths_to_the_file_name() -> None:
    """Tam yol sesli okunursa "C ters bölü Users ters bölü..." olur."""

    result = speakable(r"'C:\Users\Ali\Desktop\Orbit' klasörü oluşturuldu.")

    assert result == "Orbit klasörü oluşturuldu."
    assert "\\" not in result


def test_speakable_reduces_urls_to_the_bare_domain() -> None:
    """Tam URL (şema, www, yol, sorgu) dinlenebilir bir şey değildir."""

    result = speakable("'https://www.bilmemne.com/arama?q=deneme&x=1' açıldı.")

    assert result == "bilmemne.com açıldı."


def test_speakable_keeps_plain_messages_untouched() -> None:
    """Yol/URL içermeyen mesaj olduğu gibi kalmalı — gereksiz bozma yok."""

    assert speakable("League of Legends başlatıldı.") == "League of Legends başlatıldı."
    assert speakable("3 sonuç bulundu.") == "3 sonuç bulundu."


def test_speakable_strips_quotes_that_are_meaningless_when_spoken() -> None:
    assert "'" not in speakable("'yapay zeka' için google araması açıldı.")


def test_speakable_truncates_very_long_messages_at_a_word_boundary() -> None:
    """Çok uzun cevap dinletmek yerine kırpılır; sözcük ortasından değil."""

    uzun = "Bu cevap gereğinden uzun bir metindir. " * 20
    result = speakable(uzun)

    assert len(result) <= 121  # 120 + kırpma işareti
    assert result.endswith("…")
    assert not result[:-1].endswith(" ")


def test_speakable_handles_multiple_paths_in_one_message() -> None:
    result = speakable(r"'C:\a\kaynak.txt' -> 'C:\b\hedef.txt' kopyalandı.")

    assert "kaynak.txt" in result
    assert "hedef.txt" in result
    assert ":\\" not in result
