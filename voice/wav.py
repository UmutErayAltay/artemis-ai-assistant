"""Ham PCM verisini bellekte WAV konteynerine sarma.

Bu dönüşüm `voice/stt_cloud.py` (Groq) ve `voice/stt_azure.py` (Azure)
içinde İKİ AYRI KOPYA olarak duruyordu — gövdeleri birebir aynıydı ve
`stt_azure.py`'nin docstring'i zaten diğer kopyaya işaret ediyordu
("aynı desen"). İki bulut sağlayıcısı da, tüm OpenAI-uyumlu Whisper
uçları gibi, ham PCM değil konteynerlenmiş bir ses dosyası bekliyor.
"""

from __future__ import annotations

import io
import wave

from voice.audio import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH_BYTES


def pcm_to_wav(pcm_bytes: bytes) -> bytes:
    """Ham 16-bit PCM veriyi, DİSKE YAZMADAN bir WAV konteynerine sarar.

    Dönüşüm tamamen `io.BytesIO` ile RAM'de yapılır: `voice.audio`
    modülünün "ses verisi hiçbir zaman diske yazılmaz" ilkesi, buluta
    gönderilen ses için de geçerlidir.

    Args:
        pcm_bytes: 16 kHz, tek kanal, 16-bit little-endian PCM veri.

    Returns:
        Geçerli, tek kanallı/16 kHz/16-bit bir `.wav` dosyasının ham baytları.
    """

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()
