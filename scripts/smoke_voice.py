"""Sesli asistanı uçtan uca, GERÇEK bileşenlerle deneyen duman testi.

    python scripts/smoke_voice.py

NE YAPAR: Piper ile Türkçe konuşma üretir ve bunu mikrofondan geliyormuş
gibi `VoiceAssistant`'a besler. Zincirin TAMAMI gerçektir — uyandırma
algılama, Whisper (GPU/bulut), komut kapısı, Ollama, planner, dispatcher
ve tool'lar. Yalnızca üç şey taklit edilir:

    mikrofon  -> üretilmiş ses blokları (sizin sesiniz gerekmez)
    hoparlör  -> susturulur (test sırasında konuşmasın)
    masaüstü  -> geçici bir klasör (GERÇEK masaüstünüze dokunulmaz)

NEDEN GEREKLİ: Birim testleri her parçayı ayrı ayrı doğruluyor ama
"mikrofondan tool'a" yolunun tamamının çalıştığını yalnızca bu gösterir.
Gerçek arızaların çoğu (eksik CUDA kütüphanesi, yanlış tool seçimi,
uyandırma sözcüğünün duyulmaması) ancak burada görünür.

SINIR: Piper'ın sentetik sesi sizin sesiniz değildir; İngilizce özel
isimleri Türkçe fonetikle okur ("Steam" -> "si-tem"). Bu yüzden tanıma
başarısı burada GERÇEKTEN OLDUĞUNDAN KÖTÜ görünebilir. Buradaki amaç
doğruluk ölçmek değil, ZİNCİRİN KOPUK OLMADIĞINI kanıtlamaktır.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.settings import Settings, get_settings  # noqa: E402
from core.dispatcher import ToolDispatcher  # noqa: E402
from core.llm_client import OllamaLLMClient  # noqa: E402
from core.ollama_manager import (  # noqa: E402
    OllamaUnavailableError,
    OllamaServerManager,
    list_installed_models,
)
from core.plugin_loader import load_plugins  # noqa: E402
from core.voice_loop import VoiceAssistant  # noqa: E402
from memory.context_memory import ContextMemory  # noqa: E402
from voice.audio import BLOCK_FRAMES, SAMPLE_RATE  # noqa: E402

SENARYOLAR = [
    "Masaüstünde Orbit klasörü oluştur",
    "Discord aç",
    "Sen kimsin?",
]
"""Denenecek komutlar. Sonuncusu KOMUT DEĞİLDİR: komut kapısının onu
durdurup dosya/uygulama açmadığını göstermek için bilerek eklendi."""


class _SessizOverlay:
    """Arayüz yerine geçer; ekrana çizmez, durumları yazdırır."""

    def __init__(self) -> None:
        self.durumlar: list[str] = []
        self.duyulan = ""
        self.cevap = ""

    def show_listening(self, text: str = "") -> None:
        self.durumlar.append("dinliyor")

    def show_thinking(self, text: str = "") -> None:
        self.durumlar.append("düşünüyor")

    def show_speaking(self, text: str = "") -> None:
        self.durumlar.append("konuşuyor")
        self.cevap = text

    def show_error(self, text: str) -> None:
        self.durumlar.append("hata")
        self.cevap = text

    def set_heard(self, text: str) -> None:
        self.duyulan = text

    def set_amplitude(self, amplitude: float) -> None:
        pass

    def dismiss(self) -> None:
        pass


class _UretilmisMikrofon:
    """Verilen PCM'i blok blok veren, sonra sessizlik üreten sahte mikrofon."""

    def __init__(self, pcm: bytes) -> None:
        blok = BLOCK_FRAMES * 2
        self._bloklar = [pcm[i : i + blok] for i in range(0, len(pcm), blok)]
        self._sessizlik = b"\x00\x00" * BLOCK_FRAMES

    def read_block(self) -> bytes:
        return self._bloklar.pop(0) if self._bloklar else self._sessizlik


class _SessizTTS:
    """Konuşmayı bastırır; testte hoparlörden ses çıkmasın."""

    def speak(self, text: str, on_amplitude=None) -> None:
        pass

    def stop(self) -> None:
        pass


def _konusma_uret(metin: str) -> bytes:
    """Piper ile Türkçe konuşma üretip 16 kHz PCM'e çevirir."""

    import numpy as np
    from piper import PiperVoice

    ayarlar = Settings()
    ses = PiperVoice.load(str(ayarlar.piper_model_path))
    parcalar = list(ses.synthesize(metin))
    kaynak_hiz = parcalar[0].sample_rate

    dalga = np.concatenate([p.audio_float_array for p in parcalar])
    hedef_uzunluk = int(len(dalga) * SAMPLE_RATE / kaynak_hiz)
    yeniden = np.interp(
        np.linspace(0, len(dalga) - 1, hedef_uzunluk), np.arange(len(dalga)), dalga
    )
    return (yeniden * 32767).astype(np.int16).tobytes()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="    %(levelname)s | %(name)s | %(message)s")

    print("Artemis sesli asistan duman testi")
    print("=" * 62)

    load_plugins()

    try:
        OllamaServerManager().ensure_running()
        modeller = list_installed_models()
    except OllamaUnavailableError as exc:
        print(f"HATA: Ollama kullanılamıyor: {exc}")
        return 1

    if not modeller:
        print("HATA: Hiç Ollama modeli kurulu değil.")
        return 1

    # Yapılandırmadaki model varsa ONU kullan. Burada körlemesine
    # `modeller[0]` alınıyordu; `ollama list` en son indirileni başa
    # koyduğu için duman testi, asistanın gerçekte çalıştıracağı modeli
    # değil rastgele bir modeli sınıyordu — yani yeşil sonuç hiçbir şey
    # kanıtlamıyordu.
    # `get_settings()` — `Settings()` DEĞİL: ikincisi config.yaml'ı hiç
    # okumaz, yalnızca sınıftaki varsayılanları döndürür.
    yapilandirilan = get_settings().ollama_model
    model = yapilandirilan if yapilandirilan in modeller else modeller[0]
    if model != yapilandirilan:
        print(f"UYARI: yapılandırmadaki {yapilandirilan!r} kurulu değil, {model!r} kullanılıyor.")
    print(f"Model: {model}")

    gecici = Path(tempfile.mkdtemp(prefix="artemis_smoke_"))
    ayarlar = Settings(
        desktop_path=gecici / "Desktop",
        downloads_path=gecici / "Downloads",
        db_path=gecici / "memory.db",
        log_dir=gecici / "logs",
    )
    ayarlar.desktop_path.mkdir(parents=True, exist_ok=True)
    print(f"Geçici masaüstü: {ayarlar.desktop_path}")
    print(f"Cihaz: {ayarlar.whisper_device} | STT modeli: {ayarlar.whisper_model_size}")
    print()

    dispatcher = ToolDispatcher(settings=ayarlar, memory=ContextMemory(ayarlar.db_path))
    llm = OllamaLLMClient(model=model, keep_alive=ayarlar.ollama_keep_alive)

    basarili = 0
    for senaryo in SENARYOLAR:
        overlay = _SessizOverlay()
        asistan = VoiceAssistant(dispatcher, llm, overlay, ayarlar)
        asistan._tts = _SessizTTS()  # noqa: SLF001 - duman testi bilinçli olarak içeri uzanır

        print(f"[söylenen] {senaryo}")
        try:
            asistan._handle_one_command(_UretilmisMikrofon(_konusma_uret(senaryo)))  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - duman testi çökmemeli, raporlamalı
            print(f"  ÇÖKTÜ: {type(exc).__name__}: {exc}\n")
            continue

        print(f"  duyulan : {overlay.duyulan!r}")
        print(f"  akış    : {' -> '.join(overlay.durumlar)}")
        print(f"  cevap   : {overlay.cevap}")
        print()
        basarili += 1

    print("=" * 62)
    print(f"{basarili}/{len(SENARYOLAR)} senaryo çökmeden tamamlandı.")
    print(f"Oluşan dosyalar: {sorted(p.name for p in ayarlar.desktop_path.iterdir())}")
    print("(Gerçek masaüstünüze dokunulmadı.)")
    return 0 if basarili == len(SENARYOLAR) else 1


if __name__ == "__main__":
    raise SystemExit(main())
