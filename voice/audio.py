"""Ham mikrofon akışı ve ses seviyesi ölçümü.

Hem uyandırma sözcüğü (`wake_word.py`) hem de konuşma tanıma (`stt.py`)
aynı ses formatına ihtiyaç duyar: 16 kHz, tek kanal, 16-bit PCM. Bu hem
Vosk'un hem Whisper'ın beklediği formattır, bu yüzden dönüştürme/yeniden
örnekleme yapmaya gerek kalmaz.

Ses seviyesi (`rms_amplitude`) arayüzdeki dalga formunu beslemek için
kullanılır — ses verisi hiçbir zaman diske yazılmaz, ağa gönderilmez;
yalnızca bellekte işlenip atılır.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import TracebackType

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
"""Örnekleme hızı (Hz). Vosk ve Whisper'ın ikisi de bunu bekler."""

CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM

BLOCK_FRAMES = 2_000
"""Her okumada alınacak örnek sayısı (~125 ms). Küçük tutulur ki dalga
formu akıcı görünsün ve konuşma sonu hızlı algılansın."""

_SPEECH_RMS_CEILING = 5_000.0
"""Normal konuşmanın kabaca üst RMS sınırı (int16 ölçeğinde, tepe 32767).
Genlik bu değere göre 0-1 aralığına ölçeklenir."""


class MicrophoneUnavailableError(Exception):
    """Mikrofon açılamadığında fırlatılır (cihaz yok, başka uygulama kullanıyor vb.)."""


def rms_amplitude(block: bytes) -> float:
    """Bir ses bloğunun ortalama gücünü 0.0-1.0 aralığına ölçekler.

    Arayüzdeki dalga formunu beslemek için kullanılır. Karekök eğrisi
    uygulanır: insan kulağı sesi logaritmik algıladığı için ham RMS
    doğrudan kullanılırsa dalga formu sessizlerde ölü, yüksek seslerde
    doygun görünür.

    Args:
        block: 16-bit little-endian PCM veri.

    Returns:
        0.0 (sessizlik) ile 1.0 (yüksek ses) arasında bir değer.
    """

    import numpy as np  # lazy import: ses kullanılmadan proje import edilebilsin

    # Tek baytlık artık varsa kırp: `np.frombuffer` int16 hizasına uymayan
    # bir tampon için ValueError fırlatır ve bu, ses döngüsünü çökertirdi.
    usable = len(block) - (len(block) % SAMPLE_WIDTH_BYTES)
    if usable <= 0:
        return 0.0

    samples = np.frombuffer(block[:usable], dtype=np.int16)
    if samples.size == 0:
        return 0.0

    # float64'e yükselt: int16 karesi alınırken taşmasın.
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    normalized = min(1.0, rms / _SPEECH_RMS_CEILING)
    return normalized**0.5


class MicrophoneStream:
    """Mikrofonu açan ve ses bloklarını sırayla veren bağlam yöneticisi.

    Kullanım::

        with MicrophoneStream() as mic:
            for block in mic.blocks():
                ...

    `with` bloğundan çıkıldığında cihaz her durumda serbest bırakılır —
    bir istisna fırlatılsa bile mikrofon açık kalmaz.

    Args:
        device: Kullanılacak giriş cihazı (None ise sistem varsayılanı).
        block_frames: Her okumada alınacak örnek sayısı.
    """

    def __init__(self, device: int | str | None = None, block_frames: int = BLOCK_FRAMES) -> None:
        self._device = device
        self._block_frames = block_frames
        self._stream = None

    def __enter__(self) -> MicrophoneStream:
        import sounddevice as sd  # lazy import

        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=self._block_frames,
                device=self._device,
                dtype="int16",
                channels=CHANNELS,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - cihaz/sürücü kaynaklı her hata
            # `__enter__` hata fırlatırsa `__exit__` ÇAĞRILMAZ; yarı kurulmuş
            # akışı burada kapatmazsak ses cihazı tutulu kalır (sızıntı).
            self.close()
            raise MicrophoneUnavailableError(f"Mikrofon açılamadı: {exc}") from exc

        logger.debug("Mikrofon açıldı (%d Hz, %d kanal).", SAMPLE_RATE, CHANNELS)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Mikrofonu kapatır. Birden fazla kez çağrılması güvenlidir."""

        if self._stream is None:
            return

        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001 - kapatma hatası akışı bozmamalı
            logger.debug("Mikrofon kapatılırken hata yoksayıldı.", exc_info=True)
        finally:
            self._stream = None
            logger.debug("Mikrofon kapatıldı.")

    def read_block(self) -> bytes:
        """Tek bir ses bloğu okur (yaklaşık `block_frames / SAMPLE_RATE` saniye).

        Returns:
            16-bit PCM ham veri.

        Raises:
            MicrophoneUnavailableError: Akış açık değilse.
        """

        if self._stream is None:
            raise MicrophoneUnavailableError("Mikrofon akışı açık değil.")

        data, overflowed = self._stream.read(self._block_frames)
        if overflowed:
            # İşleme yetişemedik; bu bir uyarıdır, akışı durdurmaz.
            logger.debug("Mikrofon tamponu taştı (işleme yetişemedi).")
        return bytes(data)

    def blocks(self) -> Iterator[bytes]:
        """Akış kapatılana kadar sürekli ses bloğu üretir."""

        while self._stream is not None:
            yield self.read_block()
