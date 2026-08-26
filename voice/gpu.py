"""CUDA çalışma kütüphanelerini Windows'ta bulunabilir hâle getirir.

SORUN: `faster-whisper` (CTranslate2) GPU'da çalışmak için cuBLAS ve
cuDNN DLL'lerine ihtiyaç duyar. Bunlar tam CUDA Toolkit kurulumuyla
gelir, ama `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` ile de
kurulabilir — ki bu çok daha kolaydır. Ne var ki pip bu DLL'leri
`site-packages/nvidia/*/bin` altına koyar ve Windows oraya BAKMAZ:

    RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

Bu hata model YÜKLENİRKEN değil, İLK TANIMA sırasında çıkar; yani her
şey yolunda başlar, kullanıcı konuşunca çöker.

ÇÖZÜM — iki adım birden gerekir (ölçüldü):
    1. `os.add_dll_directory()` ile dizinleri arama yoluna ekle.
    2. DLL'leri `ctypes.CDLL()` ile AÇIKÇA sürece yükle.

Yalnızca (1) YETMEZ: CTranslate2 kütüphaneyi kendi yükleme yolundan
aradığı için eklenen dizinleri kullanmayabiliyor. (2) DLL'i sürece bir
kez sokar; sonrasında CTranslate2 onu hazır bulur.
"""

from __future__ import annotations

import logging
import os
import site
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_REQUIRED_LIBRARIES = ("cublas64_12.dll", "cudnn64_9.dll")
"""CTranslate2'nin GPU'da ihtiyaç duyduğu, pip paketleriyle gelen DLL'ler."""

_prepared = False


def _nvidia_dll_directories() -> list[Path]:
    """pip ile kurulmuş `nvidia-*` paketlerinin DLL klasörlerini bulur."""

    directories: list[Path] = []
    for site_dir in site.getsitepackages():
        nvidia_root = Path(site_dir) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for package_dir in sorted(nvidia_root.iterdir()):
            bin_dir = package_dir / "bin"
            if bin_dir.is_dir():
                directories.append(bin_dir)
    return directories


def _cuda_runtime_is_loadable() -> bool:
    """Windows dışında CUDA çalışma kütüphanelerinin GERÇEKTEN var olup
    olmadığına bakar.

    `ctypes.util.find_library` sistemin linker yolunu (ldconfig önbelleği,
    `LD_LIBRARY_PATH`) sorgular; pip ile kurulmuş `nvidia-*` paketleri de
    bu yola giren `.so` dosyaları sağlar. Bulunamazsa GPU denenmemelidir.

    Neden `import torch` ya da `nvidia-smi` DEĞİL: torch bu projenin
    bağımlılığı değil (faster-whisper CTranslate2 kullanır), `nvidia-smi`
    ise bir alt süreç başlatmak demek — bu fonksiyon açılış yolunda.
    """

    from ctypes.util import find_library

    return any(find_library(name) for name in ("cublas", "cudnn"))


def prepare_cuda_libraries() -> bool:
    """CUDA DLL'lerini yüklenebilir hâle getirir. Birden fazla çağrı güvenlidir.

    Returns:
        True ise gerekli kütüphaneler sürece yüklendi ve GPU denenebilir.
        False ise GPU kullanılamaz (Windows değil, paketler kurulu değil,
        ya da DLL yüklenemedi) — çağıran taraf CPU'ya düşmelidir.
    """

    global _prepared
    if _prepared:
        return True

    if sys.platform != "win32":
        # Linux/macOS'ta pip paketleri zaten linker yolunda bulunur — ama
        # "yol sorunu yok" ile "CUDA VAR" AYNI ŞEY DEĞİL. Burada bir dönem
        # koşulsuz `True` dönülüyordu, yani `resolve_device("cuda")` GPU'su
        # olmayan bir Linux makinesinde bile "cuda" diyordu. Sonuç:
        # `whisper_device` varsayılanı "cuda" olduğu için ses katmanı, bu
        # modülün vaat ettiği CPU'ya düşüş yerine ilk tanımada
        # `SpeechRecognitionUnavailableError` ile ölüyordu. Yani geri
        # düşüş yalnızca Windows'ta çalışıyordu.
        available = _cuda_runtime_is_loadable()
        if not available:
            logger.info("CUDA çalışma kütüphaneleri bulunamadı; GPU kullanılamaz.")
        _prepared = available
        return available

    import ctypes

    for directory in _nvidia_dll_directories():
        try:
            os.add_dll_directory(str(directory))
        except OSError:  # dizin okunamıyorsa diğerlerini denemeye devam et
            logger.debug("DLL dizini eklenemedi: %s", directory, exc_info=True)

    for library in _REQUIRED_LIBRARIES:
        try:
            ctypes.CDLL(library)
        except OSError as exc:
            logger.warning(
                "CUDA kütüphanesi yüklenemedi (%s): %s. GPU kullanılamayacak; "
                "kurmak için: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12",
                library,
                exc,
            )
            return False

    _prepared = True
    logger.info("CUDA kütüphaneleri hazır; GPU kullanılabilir.")
    return True


CPU_COMPUTE_TYPE = "int8"
"""CPU'da kullanılacak hassasiyet.

`float16` bir GPU biçimidir; CTranslate2 CPU'da onu ya reddeder ya da
çok yavaş çalıştırır. GPU'dan CPU'ya düşüldüğünde hassasiyetin de
düşürülmesi ŞARTTIR, yoksa "CPU'ya düştük ama yine çalışmıyor" durumu
oluşur."""


def resolve_compute_type(device: str, requested: str) -> str:
    """Cihaza uygun hassasiyeti seçer.

    GPU istenip CPU'ya düşüldüğünde `float16` ile devam etmek yeni bir
    arızaya yol açar; bu yüzden cihaz CPU ise hassasiyet de CPU'ya uygun
    olana çevrilir.

    Args:
        device: `resolve_device()` sonucu (gerçekten kullanılacak cihaz).
        requested: `config.yaml::whisper_compute_type` değeri.

    Returns:
        Kullanılacak hassasiyet.
    """

    if device != "cpu" or requested != "float16":
        return requested

    logger.info("CPU'da 'float16' desteklenmiyor; '%s' kullanılacak.", CPU_COMPUTE_TYPE)
    return CPU_COMPUTE_TYPE


def resolve_device(requested: str) -> str:
    """İstenen cihazı, gerçekten kullanılabilir olana çevirir.

    "cuda" istendiği hâlde CUDA kütüphaneleri yoksa sessizce çökmek yerine
    CPU'ya düşer ve durumu loglar — asistanın hiç çalışmaması, yavaş
    çalışmasından kötüdür.

    Args:
        requested: `config.yaml::whisper_device` değeri ("cpu" / "cuda").

    Returns:
        Gerçekten kullanılacak cihaz adı.
    """

    if requested != "cuda":
        return requested

    if prepare_cuda_libraries():
        return "cuda"

    logger.warning("whisper_device='cuda' istendi ama CUDA hazır değil; CPU'ya düşülüyor.")
    return "cpu"
