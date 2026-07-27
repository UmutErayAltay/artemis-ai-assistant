"""Uygulama geneli ayarlar.

Hardcode path ve magic string kullanımını engellemek için tüm yollar ve
güvenlik-kritik sabitler burada, pathlib ve Pydantic ile tek noktadan
yönetilir. Ayarlar bir YAML dosyasından okunur; dosya yoksa güvenli
varsayılanlarla çalışılır.
"""

from __future__ import annotations

import os
import sys
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
        voice_enabled: Sesli asistan katmanının ana açma/kapama anahtarı.
            False ise mikrofon hiç açılmaz; Artemis yalnızca metin
            tabanlı (`--chat`) çalışır.
        wake_word_enabled: Sürekli "Artemis" uyandırma sözcüğü
            dinlemesinin açma/kapama anahtarı. `voice_enabled` genel
            sesli asistanı (STT/TTS dahil) kapatırken bu yalnızca
            sürekli dinlemeyi kapatır; kapalıyken uyanmak için kısayol
            tuşu kullanılır (bkz. `ui/hotkey.py`).
        wake_words: Uyandırma sözcüğü için kabul edilen telaffuz
            varyantları (küçük harfle karşılaştırılır). "Artemis" özel
            bir isim olduğundan küçük Türkçe Vosk modeli onu "artemiz",
            "arte mis" gibi farklı dizilere çevirebilir; kullanıcı
            log'da kendi telaffuzu için modelin ne ürettiğini görüp
            buraya yeni bir varyant ekleyebilir (bkz. `voice/wake_word.py`).
        vosk_speech_gate_model_path: Uyandırma algılamasında "birisi
            gerçekten KONUŞUYOR mu?" kapısı için kullanılan Vosk model
            klasörü. Vosk burada uyandırma sözcüğünü TANIMAZ (bunu
            Whisper yapar); tek görevi gürültüyle konuşmayı ayırmaktır —
            sabit enerji eşiği, ortam gürültüsü değişken olduğu için bu
            işi yapamıyordu (bkz. `voice/wake_word.py`). Klasör yoksa
            enerji eşiğine düşülür, asistan çalışmaya devam eder.
        whisper_device: Whisper'ın çalışacağı cihaz: "cpu" (varsayılan)
            veya "cuda". **"auto" KULLANMAYIN.** faster-whisper'ın kendi
            varsayılanı "auto"dur ve makinede bir NVIDIA GPU görürse
            CUDA'yı seçer; CUDA çalışma kütüphaneleri (cuBLAS/cuDNN)
            kurulu değilse bu, model yüklenirken değil İLK TANIMA
            sırasında `Library cublas64_12.dll is not found` ile çöker.
            Bu yüzden proje cihazı her zaman AÇIKÇA geçer. GPU'yu
            kullanmak istiyorsanız önce CUDA Toolkit + cuDNN kurun,
            sonra bunu "cuda" yapın.
        whisper_model_size: Komut tanıma (STT) için `faster-whisper`
            model boyutu ("tiny"/"base"/"small"/"medium"/...). Boyut
            arttıkça doğruluk artar ama RAM/CPU yükü de artar: "small"
            Türkçe için makul bir denge (~500MB RAM); "medium" daha
            doğru ama ~1.5GB RAM ister ve zayıf donanımda gecikmeye
            yol açabilir.
        whisper_compute_type: `faster-whisper` çıkarımının sayısal
            hassasiyeti ("int8"/"float16"/"float32"...). CPU'da "int8"
            en az RAM'i kullanır ve en hızlı çalışır (ufak bir doğruluk
            kaybı karşılığında); CUDA/GPU varsa "float16" tercih
            edilebilir.
        piper_model_path: Piper Türkçe ses (TTS) modelinin `.onnx`
            dosya yolu. Piper, yanında aynı adı taşıyan bir
            `.onnx.json` dosyasının da bulunmasını şart koşar; ikisi de
            `python scripts/setup_voice.py` ile birlikte indirilir.
        silence_timeout_seconds: Komut dinlerken kullanıcı sustuktan
            sonra kaydın bitirilmesi için beklenecek süre (saniye).
            Kısa değer tepkiyi hızlandırır ama cümle arasında duraklayan
            kullanıcıları yarıda kesebilir.
        max_command_seconds: Tek bir komut kaydı için üst sınır
            (saniye); kullanıcı hiç susmasa bile kayıt bu süre sonunda
            zorla kesilir (sonsuz dinlemeyi önleyen güvenlik sınırı).
        wake_word_model_size: Uyandırma sözcüğü için kullanılan Whisper
            model boyutu. Komut tanımadan AYRI ve kasıtlı olarak küçük
            tutulur ("tiny"): uyandırma algılaması çok daha sık çalışır,
            bu yüzden ucuz olmalı. Komut tanımada doğruluk kritik
            olduğundan orada `whisper_model_size` (varsayılan "small")
            geçerlidir. Ölçüm: "tiny" 0.81 sn'lik klibi 0.24 sn'de,
            "Artemis"i doğru tanıyarak çözüyor.
        stt_provider: Konuşma tanımanın nereden yapılacağı: "auto"
            (varsayılan — önce bulut, hata/internet yoksa otomatik yerele
            düşer), "cloud" (yalnızca bulut; başarısız olursa dürüstçe
            hata verir), "local" (yalnızca yerel — ses HİÇBİR yere
            gönderilmez). Bkz. `voice/router.py`.
        tts_provider: Sesli okumanın nereden yapılacağı; `stt_provider`
            ile aynı üç değeri alır. "local" seçilirse cevap metni de
            dışarı çıkmaz.
        command_gate_enabled: Duyulan metin tool seçimine gönderilmeden
            önce "bu asistana yönelik mi?" diye ayrı bir ikili soruyla
            süzülsün mü (varsayılan True). Kapatmak, uyandırma sözcüğü
            gürültüyle tetiklendiğinde arka plan konuşmasının rastgele
            eylemlere dönüşmesine izin verir (bkz.
            `core/llm_client.py::should_engage`). Karşılığı komut başına
            ~1.5 saniyelik ek gecikmedir.
        stt_cloud_provider: Bulut konuşma tanımada hangi servisin
            kullanılacağı: "azure" (varsayılan) veya "groq". Seçilen
            servisin anahtarı yoksa yönlendirici zaten yerele düşer, bu
            yüzden yanlış seçim asistanı bozmaz — yalnızca doğruluğu
            etkiler. Anahtarlar BU DOSYADAN GELMEZ (bkz.
            `get_groq_api_key`, `get_azure_speech_credentials`).
        groq_model: `stt_cloud_provider: "groq"` iken kullanılacak model.
        edge_tts_voice: Microsoft Edge TTS ses adı. Türkçe seçenekler:
            "tr-TR-EmelNeural" (kadın), "tr-TR-AhmetNeural" (erkek).
        edge_tts_rate: Konuşma hızı ("+0%" = normal, "-10%" = daha yavaş).
            Nöral seslerin "robotik" duyulmasının yaygın sebebi fazla hızlı
            ve düz okumasıdır; hafif yavaşlatmak belirgin biçimde daha
            doğal duyulur.
        edge_tts_pitch: Ses perdesi ("+0Hz" = normal, "-10Hz" = daha pes).
        cloud_timeout_seconds: Bulut çağrılarının zaman aşımı.
        cloud_failure_cooldown_seconds: Bir bulut hatasından sonra
            bulutun atlanacağı süre. Bu olmadan, internet kapalıyken her
            komut önce zaman aşımını beklerdi (bkz. `voice/router.py`).
        voice_hotkey: Artemis'i elle uyandıran, sistem genelinde çalışan
            kısayol ("ctrl+alt+a" biçiminde). `wake_word_enabled: false`
            iken tek çağırma yoludur. Kısayol başka bir uygulama
            tarafından kullanılıyorsa Artemis kısayolsuz çalışmaya devam
            eder (bkz. `ui/hotkey.py`).
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
    # Etiket AÇIKÇA yazılır: Ollama, etiketsiz bir adı `:latest` diye
    # çözer ve o etiket kurulu değilse asistan sebebi anlaşılmayan bir
    # bağlantı hatasıyla düşer (buradaki değer bir dönem "llama3.1" idi,
    # kurulu olan ise "llama3.1:8b").
    ollama_model: str = "gemma4:e4b"
    use_native_tool_calling: bool = False
    ollama_keep_alive: str = "1m"
    dangerous_tools: list[str] = Field(default_factory=list)
    voice_enabled: bool = True
    wake_word_enabled: bool = True
    wake_words: list[str] = Field(
        default_factory=lambda: [
            "artemis",
            "artemiz",
            "arte mis",
            "art emis",
            "hartemis",
        ]
    )
    vosk_speech_gate_model_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent
        / "voice_models"
        / "vosk-model-small-tr-0.3"
    )
    whisper_model_size: str = "large-v3-turbo"
    whisper_compute_type: str = "float16"
    whisper_device: str = "cuda"
    piper_model_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent
        / "voice_models"
        / "tr_TR-dfki-medium.onnx"
    )
    silence_timeout_seconds: float = 1.2
    max_command_seconds: float = 12.0
    voice_hotkey: str = "ctrl+alt+a"
    wake_word_model_size: str = "tiny"

    # --- Hibrit (bulut/yerel) ses ayarları ---
    stt_provider: str = "auto"
    tts_provider: str = "auto"
    command_gate_enabled: bool = True
    stt_cloud_provider: str = "azure"
    groq_model: str = "whisper-large-v3-turbo"
    edge_tts_voice: str = "tr-TR-EmelNeural"
    edge_tts_rate: str = "+0%"
    edge_tts_pitch: str = "+0Hz"
    cloud_timeout_seconds: float = 15.0
    cloud_failure_cooldown_seconds: float = 60.0


SECRETS_PATH = Path(__file__).resolve().parent / "secrets.yaml"
"""API anahtarlarının tutulduğu, **versiyon kontrolüne GİRMEYEN** dosya."""


def get_groq_api_key() -> str | None:
    """Groq API anahtarını, gizli kalacağı garanti edilen kaynaklardan okur.

    Anahtar BİLİNÇLİ olarak `Settings`/`config.yaml` üzerinden gelmez:
    `config/config.yaml` versiyon kontrolünde izlenen bir dosyadır ve bu
    depo herkese açıktır — oraya yazılan bir anahtar ilk commit'te
    yayınlanmış olurdu. Bunun yerine üç kaynak sırayla denenir:

        1. `GROQ_API_KEY` ortam değişkeni (önerilen).
        2. `config/secrets.yaml` dosyasındaki `groq_api_key` alanı.
           Bu dosya `.gitignore`'dadır; commit edilmez.
        3. Windows kullanıcı ortam değişkenleri (kayıt defteri).
           Sebep: `setx GROQ_API_KEY ...` değeri kalıcı olarak kaydeder
           ama ZATEN AÇIK olan terminalleri etkilemez. Kullanıcı `setx`
           çalıştırıp aynı pencereden Artemis'i başlattığında anahtar
           "kayıtlı ama görünmez" olur; bu adım o kafa karıştırıcı
           durumu ortadan kaldırır.

    Returns:
        Anahtar, ya da hiçbir kaynakta yoksa None (bu bir hata değildir —
        yalnızca bulut sağlayıcı devre dışı kalır, yerel çalışmaya devam
        edilir; bkz. `voice/router.py`).
    """

    from_env = os.environ.get("GROQ_API_KEY", "").strip()
    if from_env:
        return from_env

    from_secrets = _read_from_secrets_file("groq_api_key")
    if from_secrets:
        return from_secrets

    return _read_from_windows_environment("GROQ_API_KEY")


def get_azure_speech_credentials() -> tuple[str | None, str | None]:
    """Azure Speech anahtarını ve bölgesini gizli kaynaklardan okur.

    Groq anahtarıyla AYNI üç kaynağı aynı sırayla dener (bkz.
    `get_groq_api_key`): ortam değişkeni → `config/secrets.yaml` →
    Windows kayıt defteri. Son adım, `setx` ile kaydedilip zaten açık
    olan terminale yansımayan değerleri de bulabilmek içindir.

    Bölge gizli bir değer değildir ama anahtarla birlikte gitmesi
    gerektiği için aynı yerden okunur; ikisinden biri eksikse bulut
    tanıma kullanılamaz.

    Returns:
        (anahtar, bölge) çifti; bulunamayanlar None olur.
    """

    key = (
        os.environ.get("AZURE_SPEECH_KEY", "").strip()
        or _read_from_secrets_file("azure_speech_key")
        or _read_from_windows_environment("AZURE_SPEECH_KEY")
    )
    region = (
        os.environ.get("AZURE_SPEECH_REGION", "").strip()
        or _read_from_secrets_file("azure_speech_region")
        or _read_from_windows_environment("AZURE_SPEECH_REGION")
    )
    return key or None, region or None


def _read_from_secrets_file(field: str) -> str | None:
    """`config/secrets.yaml` içindeki bir alanı okur (dosya yoksa None)."""

    if not SECRETS_PATH.exists():
        return None

    try:
        with SECRETS_PATH.open("r", encoding="utf-8") as f:
            secrets = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        # Bozuk/okunamayan gizli dosya, uygulamayı çökertmemeli.
        return None

    value = str(secrets.get(field, "")).strip()
    return value or None


def _read_from_windows_environment(variable: str) -> str | None:
    """Windows kullanıcı ortam değişkenlerinden (kayıt defteri) bir değer okur.

    `setx` ile kaydedilmiş ama bu sürecin ortamına yansımamış bir değeri
    bulmak için son çare. Windows dışında sessizce None döner.
    """

    if sys.platform != "win32":
        return None

    try:
        import winreg  # lazy import: yalnızca Windows'ta vardır

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env_key:
            value, _ = winreg.QueryValueEx(env_key, variable)
    except (ImportError, OSError):
        # Değişken yok, erişilemiyor ya da Windows API kullanılamıyor.
        return None

    return str(value).strip() or None


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
