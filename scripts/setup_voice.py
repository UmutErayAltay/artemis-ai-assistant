"""Sesli asistanın yerel TTS modeli için gereken dosyaları indirir.

Kullanım::

    python scripts/setup_voice.py

Bu betik yalnızca Piper Türkçe ses modelini indirip `config.settings.
Settings`'in gösterdiği `voice_models/` klasörüne yerleştirir: metni
sese çevirme (TTS) için kullanılan bir `.onnx` dosyası ve yanında
ZORUNLU bir `.onnx.json` dosyası (Piper ikisinin yan yana olmasını
şart koşar).

Whisper modelleri (uyandırma sözcüğü için `tiny`, komut tanıma için
`small` — bkz. `voice/wake_word.py`, `voice/stt.py`) BU BETİKLE
İNDİRİLMEZ: `faster-whisper` onları ilk kullanımda Hugging Face
Hub'dan kendisi indirip yerel önbelleğe alır. Bu yüzden ilk uyandırma
sözcüğü/komut denemesinde birkaç saniyelik tek seferlik bir gecikme
olması normaldir; sonraki çalıştırmalarda model önbellekten yüklenir.

Yalnızca standart kütüphane kullanılır, yeni bir bağımlılık gerekmez.
İndirilen toplam boyut ~60MB'tır (yalnızca Piper). İndirme sırasında
yüzde ilerleme gösterilir. Yarım kalan bir indirme asıl dosyayı
bozmasın diye önce `.tmp` uzantılı bir dosyaya yazılır; yalnızca
tamamen bittiğinde asıl ada taşınır. Model zaten kuruluysa bu adım
atlanır.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

# `python scripts/setup_voice.py` doğrudan çalıştırıldığında sys.path[0]
# bu dosyanın bulunduğu `scripts/` klasörü olur, repo kökü değil; bu
# yüzden `config` paketini bulabilmek için repo kökünü elle ekliyoruz.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.settings import Settings, get_settings  # noqa: E402

PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/tr/tr_TR/dfki/medium/"
PIPER_ONNX_FILENAME = "tr_TR-dfki-medium.onnx"
PIPER_JSON_FILENAME = f"{PIPER_ONNX_FILENAME}.json"

_CHUNK_SIZE = 256 * 1024  # 256 KB - okuma/yazma parça boyutu
_REQUEST_TIMEOUT_SECONDS = 30


class DownloadError(Exception):
    """İndirme veya arşiv açma sırasında oluşan, kullanıcıya gösterilecek hata."""


def _format_mb(num_bytes: int) -> str:
    """Bayt sayısını okunabilir bir MB metnine çevirir."""

    return f"{num_bytes / (1024 * 1024):.1f}"


def _download_with_progress(url: str, destination: Path, description: str) -> None:
    """URL'deki dosyayı `destination`'a indirir; ilerlemeyi yüzde olarak yazdırır.

    Yarım kalmış bir indirme asıl dosyayı bozmasın diye önce `.tmp`
    uzantılı bir dosyaya yazılır; ancak indirme tamamen bittiğinde asıl
    ada taşınır (`Path.replace`, aynı disk üzerinde atomiktir).

    Args:
        url: İndirilecek dosyanın adresi.
        destination: Diskte yazılacağı nihai yol.
        description: İlerleme satırında gösterilecek insan-okunur ad.

    Raises:
        DownloadError: Ağ/HTTP hatası oluşursa (yarım kalan `.tmp` dosyası silinir).
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")

    print(f"  İndiriliyor: {description}")
    print(f"  Kaynak: {url}")

    request = urllib.request.Request(url, headers={"User-Agent": "artemis-setup-voice/1.0"})

    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0

            with tmp_path.open("wb") as f:
                while True:
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break

                    f.write(chunk)
                    downloaded += len(chunk)

                    if total_size > 0:
                        percent = min(100, downloaded * 100 // total_size)
                        print(
                            f"\r    %{percent:>3}  ({_format_mb(downloaded)}/{_format_mb(total_size)} MB)",
                            end="",
                            flush=True,
                        )
                    else:
                        print(f"\r    {_format_mb(downloaded)} MB indirildi", end="", flush=True)
        print()  # ilerleme satırını kapat
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise DownloadError(f"'{description}' indirilemedi ({url}): {exc}") from exc

    tmp_path.replace(destination)
    print(f"  Tamamlandı: {destination.name}")


def _setup_piper_voice(settings: Settings) -> bool:
    """Piper Türkçe ses modelini (.onnx + .onnx.json) kurar; zaten kuruluysa atlar.

    Returns:
        Yeni bir kurulum yapıldıysa True, model zaten kuruluysa False.
    """

    onnx_path = settings.piper_model_path
    json_path = onnx_path.with_name(onnx_path.name + ".json")

    if onnx_path.exists() and json_path.exists():
        print(f"Piper Türkçe ses modeli zaten kurulu, atlanıyor: {onnx_path}")
        return False

    print(f"Piper Türkçe ses modeli kuruluyor -> {onnx_path.parent}")
    _download_with_progress(
        PIPER_BASE_URL + PIPER_ONNX_FILENAME,
        onnx_path,
        f"Piper ses modeli ({PIPER_ONNX_FILENAME}, ~60 MB)",
    )
    _download_with_progress(
        PIPER_BASE_URL + PIPER_JSON_FILENAME,
        json_path,
        f"Piper model tanımı ({PIPER_JSON_FILENAME})",
    )

    print(f"  Piper sesi hazır: {onnx_path}")
    return True


def main() -> int:
    """Betiğin giriş noktası.

    Returns:
        Başarıda 0; ağ/HTTP hatası veya bozuk arşivde 0'dan farklı bir kod.
    """

    settings = get_settings()

    print("Artemis sesli asistan modeli kuruluyor.")
    print(f"Model klasörü: {settings.piper_model_path.parent}\n")

    try:
        piper_installed = _setup_piper_voice(settings)
    except DownloadError as exc:
        print(f"\nHATA: {exc}")
        print("İnternet bağlantınızı kontrol edip betiği tekrar çalıştırın.")
        return 1
    except KeyboardInterrupt:
        print("\nKullanıcı tarafından durduruldu.")
        return 130

    print("\n=== Özet ===")
    print(
        f"Piper Türkçe sesi : {'kuruldu' if piper_installed else 'zaten kuruluydu'} "
        f"-> {settings.piper_model_path}"
    )
    print(
        "\nNot: Whisper modelleri (uyandırma sözcüğü için 'tiny', komut tanıma "
        "için 'small') bu betikle indirilmez; faster-whisper onları ilk "
        "kullanımda Hugging Face Hub'dan kendisi indirip önbelleğe alır. İlk "
        "uyandırma/komut denemesinde bu yüzden birkaç saniyelik tek seferlik "
        "bir gecikme olması normaldir."
    )
    print("\nPiper sesi hazır. 'python main.py --voice' ile deneyebilirsiniz.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
