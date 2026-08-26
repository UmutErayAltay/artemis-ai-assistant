"""Metni sesli okuma (Microsoft Edge TTS) — internet varken `voice.tts.
TextToSpeech`'in (Piper) yerini alan bulut tabanlı alternatif.

TASARIM: `edge_tts`, Microsoft Edge'in kullandığı bulut TTS servisini
konuşur; API anahtarı GEREKTİRMEZ ama internet ister. Kütüphane baştan
sona ASENKRON'dur (`Communicate.stream()` bir async generator) — ama bu
sınıfın çağrı sözleşmesi `voice.tts.TextToSpeech` ile BİREBİR aynı olmak
zorunda ki `voice/router.py` (ileride eklenecek yönlendirici) internet
durumuna göre ikisi arasında şeffafça geçiş yapabilsin: `speak()` senkron
ve bloklayıcıdır, asenkron sentez `asyncio.run()` ile senkron bir
sarmalayıcı içine alınır (bkz. `_synthesize`).

`tts.py`'deki Piper akışından FARKLI olarak burada cümle-cümle akış
(streaming synthesis) yapılmaz: `edge_tts` yalnızca mp3 üretebilir
(`Communicate.__init__` başka bir çıktı formatı parametresi sunmaz) ve
mp3'ü `sounddevice`'ın beklediği ham PCM'e çevirmek için tüm dosyanın
`av` (PyAV) ile çözülmesi gerekir — bu yüzden akış şöyle: ÖNCE tüm metin
sentezlenir (ağ), SONRA tamamı PCM'e çözülür, SONRA çalma parça parça
yapılır (bkz. `_play`). Ölçülen gecikme kabul edilebilir düzeyde (4.1
saniyelik bir cevap için sentez ~0.54 sn, çözme ~0.06 sn) olduğundan bu
basitlik tercih edildi.

Ağ hatası, zaman aşımı, boş/bozuk mp3 ya da çözme hatası gibi HER TÜRLÜ
arıza tek bir `CloudTextToSpeechUnavailableError` altında toplanır ve
gerçek nedenle (`from exc`) zincirlenir — sessizce "başardım" denmez.
Yönlendirici bunu yakalayıp yerel Piper'a düşer.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from collections.abc import Callable

from utils.asyncio_guard import ensure_no_running_event_loop
from voice.audio import rms_amplitude

logger = logging.getLogger(__name__)

_PLAYBACK_BLOCK_FRAMES = 2_000
"""Çalarken her defasında yazılacak örnek sayısı (~125 ms @ 16 kHz) —
`voice/tts.py::_PLAYBACK_BLOCK_FRAMES` ile aynı büyüklük mertebesi:
`on_amplitude` akıcı görünsün, `stop()` hızlı tepki versin diye küçük
tutulur."""


class CloudTextToSpeechUnavailableError(Exception):
    """Bulut TTS'e ulaşılamadığında fırlatılır: ağ hatası, zaman aşımı,
    boş/bozuk mp3 ya da mp3->PCM çözme hatası dahil her arıza burada
    toplanır (gerçek neden `__cause__` ile zincirlenir).

    `voice/router.py` (ileride eklenecek yönlendirici) bunu yakalayıp
    yerel `voice.tts.TextToSpeech` (Piper) ile devam eder — bu yüzden bu
    istisna asla yutulup sessizce yoksayılmamalı, her zaman fırlatılmalıdır.
    """


class EdgeTextToSpeech:
    """Verilen metni Microsoft Edge TTS bulut servisiyle sese çevirip hoparlörden çalar.

    `voice.tts.TextToSpeech` ile ARAYÜZ olarak birebir aynıdır (`speak`,
    `stop`) ki yönlendirici internet durumuna göre ikisi arasında şeffafça
    geçiş yapabilsin. API anahtarı gerekmediği ve doğrulanacak yerel bir
    dosya olmadığı için `__init__` hiçbir I/O yapmaz, hiçbir şey fırlatmaz
    (Piper'daki `TextToSpeechModelMissingError` kontrolünün burada bir
    karşılığı yoktur).

    Args:
        voice: Edge TTS ses kimliği. Varsayılan Türkçe kadın ses
            (`tr-TR-EmelNeural`); erkek ses için `tr-TR-AhmetNeural`.
        timeout: Bağlantı kurma ve yanıt bekleme zaman aşımı (saniye) —
            hem `connect_timeout` hem `receive_timeout` için kullanılır.
        rate: Konuşma hızı yüzde kayması ("+0%" = normal, "-10%" = %10
            daha yavaş). Nöral seslerin "robotik" duyulmasının en yaygın
            sebebi fazla hızlı ve düz okumasıdır; hafif bir yavaşlatma
            (örn. "-10%") kulakta belirgin biçimde daha doğal durur.
            `edge_tts` bu değeri STRING olarak bekler — sayıya çevirip
            geçirmeyin, olduğu gibi iletilir.
        pitch: Ses perdesi kayması ("+0Hz" = normal, "-10Hz" = daha pes).
            `rate` ile aynı mantıkla STRING olarak olduğu gibi iletilir.
    """

    def __init__(
        self,
        voice: str = "tr-TR-EmelNeural",
        timeout: float = 10.0,
        rate: str = "+0%",
        pitch: str = "+0Hz",
    ) -> None:
        self._voice = voice
        self._timeout = timeout
        self._rate = rate
        self._pitch = pitch
        self._stop_event = threading.Event()

    def speak(self, text: str, on_amplitude: Callable[[float], None] | None = None) -> None:
        """Metni bulutta sese çevirip hoparlörden çalar; konuşma bitene kadar bloklar.

        Sıra şöyledir: (1) `edge_tts` ile tüm metin mp3'e sentezlenir, (2)
        mp3 tamamı `av` ile ham PCM'e çözülür, (3) PCM `sounddevice` ile
        parça parça çalınır. İlk iki adım ağ/CPU ağırlıklıdır ve `stop()`
        tarafından KESİLEMEZ — yalnızca 3. adım (çalma) `stop()`'a duyarlıdır
        (bkz. `stop()` docstring'i).

        Args:
            text: Okunacak metin. Boş/yalnızca boşluktan oluşuyorsa hiçbir
                şey yapılmaz, ağa hiç çıkılmaz.
            on_amplitude: Verilirse, çalma sırasında her ses parçası için
                0.0-1.0 aralığında bir genlik bildirimi alır (bkz.
                `voice.audio.rms_amplitude`) — arayüzdeki dalga formunu
                canlandırmak için kullanılır.

        Raises:
            CloudTextToSpeechUnavailableError: Ağ hatası, zaman aşımı ya
                da boş/bozuk mp3/çözme hatası durumunda. Çağıran taraf
                (yönlendirici) bunu yakalayıp yerel Piper'a düşmelidir.
            RuntimeError: Zaten çalışan bir asyncio olay döngüsü içinden
                çağrılırsa (bkz. `_ensure_no_running_event_loop`) — bu bir
                kullanım hatasıdır, `CloudTextToSpeechUnavailableError`'a
                SARILMAZ.
        """

        text = text.strip()
        if not text:
            return

        ensure_no_running_event_loop(
            "EdgeTextToSpeech.speak()", "Sesi ayrı bir thread'de çalıştırın."
        )
        self._stop_event.clear()

        try:
            mp3_bytes = self._synthesize(text)
        except Exception as exc:
            raise CloudTextToSpeechUnavailableError(f"Edge TTS sentezi başarısız oldu: {exc}") from exc

        try:
            pcm_bytes, sample_rate = self._decode_mp3_to_pcm(mp3_bytes)
        except Exception as exc:
            raise CloudTextToSpeechUnavailableError(f"Edge TTS ses çözme başarısız oldu: {exc}") from exc

        if not pcm_bytes:
            raise CloudTextToSpeechUnavailableError("Edge TTS boş ses üretti.")

        self._play(pcm_bytes, sample_rate, on_amplitude)

    def _synthesize(self, text: str) -> bytes:
        """Asenkron sentezi (`_synthesize_async`) senkron bir sarmalayıcı içinde çalıştırır."""

        return asyncio.run(self._synthesize_async(text))

    async def _synthesize_async(self, text: str) -> bytes:
        """`edge_tts` ile metni mp3 baytlarına çevirir.

        `Communicate.stream()` bir async generator'dır ve ses parçalarının
        yanı sıra `WordBoundary`/`SentenceBoundary` gibi meta olaylar da
        üretir; yalnızca `chunk["type"] == "audio"` olanların `chunk["data"]`'sı
        mp3 baytlarıdır, diğerlerinin `data` alanı yoktur.

        `rate`/`pitch` STRING olarak olduğu gibi iletilir (`connect_timeout`/
        `receive_timeout`'un aksine — onlar KATI `int` ister, bkz. altta).
        """

        import edge_tts  # lazy import

        communicate = edge_tts.Communicate(
            text,
            self._voice,
            connect_timeout=int(self._timeout),
            receive_timeout=int(self._timeout),
            rate=self._rate,
            pitch=self._pitch,
        )

        mp3_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])
        return b"".join(mp3_chunks)

    @staticmethod
    def _decode_mp3_to_pcm(mp3_bytes: bytes) -> tuple[bytes, int]:
        """mp3 baytlarını tek kanal, 16-bit PCM'e çözer (`av`/PyAV ile).

        `edge_tts` yalnızca mp3 üretebildiği ve `sounddevice` ham PCM
        beklediği için bu adım şarttır.

        ÖRNEKLEME HIZI KORUNUR — düşürülmez. `voice.audio.SAMPLE_RATE`
        (16 kHz) Whisper'a GİRDİ verirken zorunludur; ama bu ses bir
        ÇIKTIDIR, kullanıcının hoparlörüne gider. Edge 24 kHz üretir;
        16 kHz'e indirmek bant genişliğinin üçte birini atar ve sesi
        "telefon gibi"/robotik yapar. Bu yüzden akışın kendi hızı
        korunup çağırana bildirilir.

        ÖRNEKLERİ `to_ndarray()` İLE AL — `bytes(frame.planes[0])` İLE DEĞİL.
        PyAV'ın düzlem (plane) tamponu hizalama nedeniyle gerçek ses
        verisinden BÜYÜK olabilir; ham tamponu olduğu gibi çalmak, sonuna
        anlamsız baytlar ekler ve bunlar hoparlörden CIZIRTI olarak duyulur.
        Ölçüm (3.5 saniyelik bir cevapta):

            bytes(planes[0]) -> 178752 bayt (3.72 sn)
            to_ndarray()     -> 169344 bayt (3.53 sn)
            fazlalık         ->   9408 bayt = %5.6 gürültü

        `to_ndarray()` yalnızca gerçekten çözülmüş örnekleri döndürür.

        Returns:
            (pcm_baytlari, ornekleme_hizi) çifti.
        """

        if not mp3_bytes:
            raise ValueError("boş mp3 verisi")

        import av  # lazy import
        import numpy as np  # lazy import

        container = av.open(io.BytesIO(mp3_bytes))
        source_rate = container.streams.audio[0].codec_context.sample_rate
        resampler = av.AudioResampler(format="s16", layout="mono", rate=source_rate)

        blocks: list[bytes] = []
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                # `.reshape(-1)`: mono kare (1, N) biçiminde gelir, düzleştirilir.
                blocks.append(out.to_ndarray().reshape(-1).astype(np.int16).tobytes())

        return b"".join(blocks), source_rate

    def _play(
        self,
        pcm_bytes: bytes,
        sample_rate: int,
        on_amplitude: Callable[[float], None] | None,
    ) -> None:
        """Ham PCM'i `sounddevice` ile parça parça çalar.

        TAKILMAYI ÖNLEYEN İKİ AYRINTI (ikisi de gerçek bir kekemelik
        şikayetinden sonra eklendi):

        1. `latency="high"`: varsayılan düşük gecikmeli tampon çok
           küçüktür; bu döngü her yazma arasında iş yaptığı için tampon
           boşalıp ses kesik kesik çıkıyordu. Bir asistanın cevabında
           birkaç on milisaniyelik gecikmenin hiçbir önemi yok, ama
           kesintinin var.
        2. Genlik, yazmadan SONRA değil ÖNCE hesaplanır: `stream.write()`
           tampon boşalana kadar bloklar; hesabı ondan sonra yapmak, bir
           sonraki yazmayı geciktirip yeniden boşalmaya yol açıyordu.
        """

        import sounddevice as sd  # lazy import

        block_size_bytes = _PLAYBACK_BLOCK_FRAMES * 2  # int16 = örnek başına 2 bayt

        stream = sd.RawOutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            latency="high",
        )
        stream.start()
        try:
            for offset in range(0, len(pcm_bytes), block_size_bytes):
                if self._stop_event.is_set():
                    break

                block = pcm_bytes[offset : offset + block_size_bytes]
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
        """Devam eden `speak()` çağrısının ÇALMA aşamasını yarıda keser.

        `speak()` bloklayan bir çağrı olduğu için bu metodun anlamlı
        olması için başka bir thread'den çağrılması gerekir (örn. kullanıcı
        Esc'e bastığında UI thread'i). `threading.Event` kullanıldığı için
        thread-safe'tir.

        Not: yalnızca `_play()` (3. adım) bu bayrağı kontrol eder — ağ
        sentezi ya da mp3 çözme aşamasında `stop()` çağrılırsa, çalma o
        aşamalar bitip sıraya geldiğinde hemen (ilk parçadan önce) durur;
        sentez/çözme'nin kendisi yarıda kesilmez.
        """

        self._stop_event.set()
