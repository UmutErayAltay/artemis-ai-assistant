"""Metni sesli okuma (Piper) — Artemis'in cevabını konuşturur.

TASARIM: `synthesize()` metin başına değil CÜMLE başına bir `AudioChunk`
üretir (Piper'ın kendi API'si böyle çalışır) ve bu bilinçli olarak
akış (streaming) halinde işlenir: uzun bir cevabın ilk cümlesi çalmaya
başlarken sonraki cümle henüz sentezlenmemiş olabilir. Bu hem ilk sese
kadar geçen gecikmeyi düşürür hem tüm cevabı belleğe almayı gerektirmez.

Her `AudioChunk`'ın kendi genliği `voice.audio.rms_amplitude()` ile
ölçülüp küçük parçalar (`_PLAYBACK_BLOCK_FRAMES`) halinde çalınır; bu
sayede `on_amplitude` geri çağrısı konuşma boyunca akıcı biçimde
tetiklenir ve arayüzdeki dalga formu gerçek sese göre canlanır.

ÖNEMLİ: Piper'ın örnekleme hızı MODELE göre değişir (bkz. `PiperVoice.
config.sample_rate`) — `voice.audio.SAMPLE_RATE` (16 kHz, mikrofon/Whisper
için sabit) burada VARSAYILMAZ; ses cihazı her zaman modelin kendi hızıyla
açılır.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from voice.audio import rms_amplitude

logger = logging.getLogger(__name__)

_PLAYBACK_BLOCK_FRAMES = 2_000
"""Çalarken her defasında yazılacak örnek sayısı (~125 ms @ 16 kHz, daha
yüksek örnekleme hızlarında biraz daha kısa). `voice/audio.py::BLOCK_FRAMES`
ile aynı büyüklük mertebesi: `on_amplitude` akıcı görünsün, `stop()` hızlı
tepki versin diye küçük tutulur."""


class TextToSpeechModelMissingError(Exception):
    """Piper ses modeli (`.onnx` ya da yanındaki `.onnx.json`) diskte bulunamadığında fırlatılır."""


class TextToSpeech:
    """Verilen metni Piper ile sese çevirip hoparlörden çalar.

    Ses LAZY yüklenir: nesne oluşturulurken yalnızca dosyaların varlığı
    doğrulanır, gerçek model ilk `speak()` çağrısında (`_ensure_voice`)
    belleğe alınır.

    Args:
        model_path: Piper ses modelinin (`.onnx`) yolu. Piper, yanında
            aynı adı taşıyan bir `<model_path>.onnx.json` yapılandırma
            dosyasının da bulunmasını şart koşar.

    Raises:
        TextToSpeechModelMissingError: `.onnx` ya da `.onnx.json` yoksa.
    """

    def __init__(self, model_path: Path) -> None:
        self._model_path = Path(model_path)
        self._config_path = Path(f"{self._model_path}.json")
        self._voice = None
        self._stop_event = threading.Event()

        if not self._model_path.exists():
            raise TextToSpeechModelMissingError(
                f"Piper ses modeli bulunamadı: '{self._model_path}'. "
                "Kurmak için: python scripts/setup_voice.py"
            )
        if not self._config_path.exists():
            raise TextToSpeechModelMissingError(
                f"Piper model yapılandırması bulunamadı: '{self._config_path}'. "
                "Kurmak için: python scripts/setup_voice.py"
            )

    def _ensure_voice(self):
        """Piper sesini ilk kullanımda yükler (lazy).

        Returns:
            Yüklenmiş `piper.PiperVoice` örneği.
        """

        if self._voice is not None:
            return self._voice

        from piper import PiperVoice  # lazy import

        self._voice = PiperVoice.load(str(self._model_path), config_path=str(self._config_path))
        logger.info("Piper ses modeli yüklendi: %s (%d Hz).", self._model_path.name, self._voice.config.sample_rate)
        return self._voice

    def speak(self, text: str, on_amplitude: Callable[[float], None] | None = None) -> None:
        """Metni sese çevirip hoparlörden çalar; konuşma bitene kadar bloklar.

        Çağıran taraf sırayı bilebilsin diye (örn. "önce cevabı söyle,
        sonra bir sonraki adıma geç") bu metot senkrondur — arka planda
        çalıştırmak isteyen taraf kendi thread'ini açmalı (bkz. `stop()`).

        Args:
            text: Okunacak metin. Boş/yalnızca boşluktan oluşuyorsa hiçbir
                şey yapılmaz (ses modeli bile yüklenmez).
            on_amplitude: Verilirse, çalma sırasında her ses parçası için
                0.0-1.0 aralığında bir genlik bildirimi alır (bkz.
                `voice.audio.rms_amplitude`) — arayüzdeki dalga formunu
                canlandırmak için kullanılır.
        """

        text = text.strip()
        if not text:
            return

        voice = self._ensure_voice()

        import sounddevice as sd  # lazy import

        self._stop_event.clear()
        block_size_bytes = _PLAYBACK_BLOCK_FRAMES * 2  # int16 = örnek başına 2 bayt

        # `latency="high"`: varsayılan düşük gecikmeli tampon çok küçüktür ve
        # bu döngü her yazma arasında iş yaptığı için tampon boşalıp ses
        # kesik kesik çıkıyordu. Asistan cevabında birkaç on milisaniyelik
        # gecikmenin önemi yok, kesintinin var (bkz. `voice/tts_cloud.py`).
        stream = sd.RawOutputStream(
            samplerate=voice.config.sample_rate,
            channels=1,
            dtype="int16",
            latency="high",
        )
        stream.start()
        try:
            for chunk in voice.synthesize(text):
                if self._stop_event.is_set():
                    break

                audio_bytes = chunk.audio_int16_bytes
                for offset in range(0, len(audio_bytes), block_size_bytes):
                    if self._stop_event.is_set():
                        break

                    block = audio_bytes[offset : offset + block_size_bytes]
                    # Genlik ÖNCE hesaplanır: `write()` tampon boşalana kadar
                    # bloklar, hesabı sonraya bırakmak bir sonraki yazmayı
                    # geciktirip yeniden boşalmaya yol açıyordu.
                    if on_amplitude is not None:
                        on_amplitude(rms_amplitude(block))
                    stream.write(block)
        finally:
            if self._stop_event.is_set():
                stream.abort()  # bekleyen tamponu da atarak HEMEN sustur (Esc tepkisi)
            else:
                stream.stop()  # zaten yazılmış son tamponun bitmesini bekleyerek durdur
            stream.close()

    def stop(self) -> None:
        """Devam eden `speak()` çağrısını yarıda keser.

        `speak()` bloklayan bir çağrı olduğu için bu metodun anlamlı
        olması için başka bir thread'den çağrılması gerekir (örn. kullanıcı
        Esc'e bastığında UI thread'i). `threading.Event` kullanıldığı için
        thread-safe'tir.
        """

        self._stop_event.set()
