"""Sesli asistan döngüsü: uyandırma → dinleme → LLM → tool → konuşma.

Bu modül, ses katmanını (`voice/`), arayüzü (`ui/`) ve mevcut komut
zincirini (`llm_client` → `planner` → `dispatcher`) birbirine bağlar.
Kendisi ne ses tanır ne çizim yapar; yalnızca SIRAYI yönetir.

İŞ PARÇACIĞI MİMARİSİ (bu dosyanın en kritik yanı):

    Ana iş parçacığı          │  Ses işçisi (worker) iş parçacığı
    ──────────────────────────┼──────────────────────────────────
    Qt olay döngüsü           │  mikrofon okuma (bloklayıcı)
    ArtemisOverlay çizimi     │  uyandırma sözcüğü algılama
    kısayol tuşu              │  Whisper ile metne çevirme
                              │  Ollama'ya sorma + tool çalıştırma
                              │  Piper ile konuşma

    Qt'de arayüze YALNIZCA ana iş parçacığından dokunulabilir. Ses işçisi
    arayüzü doğrudan çağırmaz; `ArtemisOverlay`'in public metotları
    sinyal yayınlar ve Qt bunları otomatik olarak ana iş parçacığına
    kuyruklar (bkz. `ui/overlay.py` THREAD NOTU). Bu yüzden buradan
    overlay metotlarını çağırmak güvenlidir.

GERİ BESLEME (feedback) KORUMASI: Artemis konuşurken mikrofon hâlâ
açıktır ve kendi sesini duyar. Önlem alınmazsa kendi "artemis" deyişini
duyup kendini uyandırır. Bu yüzden konuşma boyunca ve kısa bir soğuma
süresi boyunca uyandırma algılama devre dışı bırakılır.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import urlparse

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError, OllamaLLMClient
from core.planner import TaskPlanner
from core.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

_FEEDBACK_COOLDOWN_SECONDS = 0.6
"""Artemis konuştuktan sonra, kendi yankısını duymamak için beklenen süre."""

_AFFIRMATIVE_WORDS = frozenset(
    {"evet", "onayla", "onaylıyorum", "onayliyorum", "tamam", "olur", "kabul"}
)
"""Sesli onayda kabul edilen sözcükler. Bunun DIŞINDAKİ her şey RED sayılır.

`"yap"` ve `"devam"` BİLEREK ÇIKARILDI: ikisi de tek başına net bir onay
değil ve olumsuz çekimlerinin (`"yapma"`, `"devam etme"`) kökü aynı olduğu
için ayırt edilmeleri kırılgan. Bir onay sözcüğü, YANLIŞ duyulduğunda
geri alınamaz bir işlemi çalıştıracak kadar net olmalıdır.
"""

_NEGATIVE_WORDS = frozenset(
    {
        "hayır",
        "hayir",
        "yapma",
        "etme",
        "iptal",
        "dur",
        "durdur",
        "vazgeç",
        "vazgectim",
        "vazgeçtim",
        "olmaz",
        "istemiyorum",
        "onaylamıyorum",
        "onaylamiyorum",
        "hayır!",
    }
)
"""Duyulduğunda onayı KOŞULSUZ reddeden sözcükler.

Türkçe eklemeli bir dildir ve olumsuzluk bir EKTİR: `"yapma"` sözcüğü
`"yap"`ı, `"onaylamıyorum"` `"onayla"`yı, `"tamamen"` `"tamam"`ı ALT DİZE
olarak içerir. Bu yüzden onay kontrolü alt dize araması OLAMAZ (bir dönem
öyleydi ve *"hayır yapma"* cevabı işlemi ÇALIŞTIRIYORDU — bkz. README §35).

Ayrıca `"hayir"`/`"vazgectim"` gibi noktasız yazımlar da listede: Whisper
Türkçe çıktısında noktalama/aksan tutarlı değildir, ve `str.lower()` Türkçe
yerel ayarı kullanmaz (`"HAYIR".lower()` -> `"hayir"`).
"""

_WORD_SPLIT_RE = re.compile(r"[^\wçğıöşüÇĞİÖŞÜ]+", re.UNICODE)
"""Onay cevabını sözcüklere ayırır; noktalama ayırıcı sayılır, Türkçe
harfler sözcüğün parçası kalır."""

_NEGATION_SUFFIX_RE = re.compile(
    r"(m[ıiuü]yor|m[ae]z|m[ae]yece|m[ae]m$|[a-zçğıöşü]{2,}m[ae]$)",
    re.UNICODE,
)
"""Türkçe olumsuzluk EKİNİ sözcük sonunda yakalar.

`_NEGATIVE_WORDS` sabit bir liste; ama Türkçe eklemeli bir dil olduğu için
olumsuzluk sonsuz sayıda çekim üretir ve hepsini listelemek mümkün DEĞİL::

    "kabul etmiyorum"   -> etmiyorum   (m + ıyor)
    "onaylamıyorum"     -> onaylamıyorum
    "yapmam"            -> yapmam      (m + am)
    "olmaz"             -> olmaz       (m + az)
    "yapmayacağım"      -> yapmayaca...(m + ayaca)
    "yapma"             -> yapma       (kök + ma)

Son dal (`[a-zçğıöşü]{2,}m[ae]$`) en az iki harflik bir kökten sonra gelen
`-ma`/`-me` emir olumsuzunu yakalar (`"yapma"`, `"etme"`, `"silme"`).

Bu SEZGİSEL bir kural, tam bir çekim çözümleyici değil; bu yüzden
`_AFFIRMATIVE_WORDS`'teki sözcükler bu testten MUAF tutulur (bkz.
`_is_affirmative`). Aksi halde `"tamam"` sözcüğü `m[ae]m$` dalına
takılır ("ta-**mam**") ve geçerli bir onay reddedilirdi.

YANLIŞ POZİTİF BİLİNÇLİ OLARAK TERCİH EDİLİR: bu kapı asimetriktir
(`.context` §6.5). Yanlışlıkla reddedilen bir onay kullanıcıya yalnızca
"tekrar söyle" dedirtir; yanlışlıkla kabul edilen bir onay geri alınamaz
bir işlemi çalıştırır.
"""

_CONFIRMATION_TIMEOUT_SECONDS = 8.0

def _is_affirmative(answer: str) -> bool:
    """Duyulan onay cevabının NET bir onay olup olmadığına karar verir.

    Kural asimetriktir ve bu bilinçlidir (bkz. `.context` §6.5): onay
    istenen tool'lar geri alınamaz işlemler yapar ve ses tanıma hata
    yapar. Bu yüzden **yalnızca net bir onay duyulursa** True döner;
    sessizlik, anlaşılmayan cevap, boş dize — hepsi False'tur.

    İKİ KATMAN, BU SIRAYLA:

    1. **Olumsuzluk vetosu**: cevapta `_NEGATIVE_WORDS`'ten bir sözcük
       varsa, başka ne varsa olsun RED. *"Hayır, tamam boş ver"* gibi bir
       cevapta hem olumsuz hem olumlu sözcük geçer; böyle bir belirsizlikte
       güvenli taraf durmaktır.
    2. **Sözcük seviyesinde** (ALT DİZE DEĞİL) olumlu eşleşme.

    Alt dize araması NEDEN OLMAZ: Türkçede olumsuzluk bir ektir, yani
    olumlu kök olumsuz sözcüğün İÇİNDE geçer::

        "yapma"        icerir "yap"
        "onaylamıyorum" icerir "onayla"
        "tamamen"      icerir "tamam"
        "devam etme"   icerir "devam"

    Bu fonksiyondan önce burada `any(word in answer for word in
    _AFFIRMATIVE_WORDS)` vardı; yani kullanıcı `filesystem.delete` onayına
    *"hayır yapma"* dediğinde işlem ÇALIŞIYORDU (README §35).

    Args:
        answer: `_listen_for_confirmation`'dan gelen, küçük harfe
            çevrilmiş ham metin (duyulamadıysa boş dize).

    Returns:
        Yalnızca net bir onay duyulduysa True.
    """

    words = {word for word in _WORD_SPLIT_RE.split(answer.strip().lower()) if word}
    if not words:
        return False
    if words & _NEGATIVE_WORDS:
        return False
    # Sezgisel ek testi yalnızca TANIMADIĞIMIZ sözcüklere uygulanır:
    # `"tamam"` sözcüğü `m[ae]m$` dalına takılır ("ta-mam"), oysa geçerli
    # bir onaydır. Muafiyet güvenliği zayıflatmaz — muaf tutulan küme,
    # zaten olumsuzluk içermeyen 7 sabit sözcüktür.
    unknown_words = words - _AFFIRMATIVE_WORDS
    if any(_NEGATION_SUFFIX_RE.search(word) for word in unknown_words):
        return False
    return bool(words & _AFFIRMATIVE_WORDS)



_WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[^\s'\"]+")
_URL_PATTERN = re.compile(r"https?://[^\s'\"]+")
_MAX_SPOKEN_LENGTH = 120

_SPEAKABLE_NOISE = re.compile(r"[''\"]")


def speakable(message: str) -> str:
    """Bir tool mesajını SESLİ OKUNMAYA uygun, kısa bir biçime indirger.

    Tool mesajları terminal için yazılmıştı ve tam yol/tam URL içeriyor:

        "'C:\\Users\\Ali\\Desktop\\Orbit' klasörü oluşturuldu."
        "'https://www.bilmemne.com/a/b?q=1' açıldı."

    Ekranda bunlar bilgi vericidir ama sesli dinlerken işkencedir —
    kullanıcı "C ters bölü Users ters bölü..." diye dinlemek zorunda
    kalır. Bu fonksiyon gürültüyü atar, anlamı bırakır:

        "Orbit klasörü oluşturuldu."
        "bilmemne.com açıldı."

    Yapılan üç şey:
      1. Windows yolları son bileşene indirilir (`...\\Orbit` -> `Orbit`).
      2. URL'ler yalnızca alan adına indirilir; `www.` ve yol/sorgu atılır.
      3. Tırnaklar temizlenir (sesli okumada anlamsız) ve sonuç çok
         uzunsa sözcük sınırından kırpılır.

    Tam mesaj KAYBOLMAZ: log'a olduğu gibi yazılır (bkz. `_respond`).
    """

    def _shorten_path(match: re.Match[str]) -> str:
        return PureWindowsPath(match.group(0)).name or match.group(0)

    def _shorten_url(match: re.Match[str]) -> str:
        host = urlparse(match.group(0)).netloc
        return host[4:] if host.startswith("www.") else (host or match.group(0))

    text = _URL_PATTERN.sub(_shorten_url, message)
    text = _WINDOWS_PATH_PATTERN.sub(_shorten_path, text)
    text = _SPEAKABLE_NOISE.sub("", text).strip()

    if len(text) <= _MAX_SPOKEN_LENGTH:
        return text

    clipped = text[:_MAX_SPOKEN_LENGTH].rsplit(" ", 1)[0]
    return f"{clipped}…"


_TURKISH_COMMAND_WORDS = (
    "aç",
    "kapat",
    "başlat",
    "oluştur",
    "sil",
    "ara",
    "klasör",
    "dosya",
    "masaüstü",
)
"""İpucu sözlüğüne eklenen Türkçe komut sözcükleri.

Sözlük yalnızca İngilizce uygulama adlarından oluştuğunda kod çözücü
İngilizceye kayıyor ve Türkçe komut fiillerini yabancı sözcüklere
benzetiyordu. Ölçülen fark:

    ipucu = yalnızca uygulamalar : "League of Legends aç" -> "lagoi of legansage"
    ipucu = uygulamalar + bunlar : "League of Legends aç" -> "lagoi of legends, aç"

Yani hem fiil hem uygulama adı düzeliyor.
"""


class VoiceAssistant:
    """Sesli asistanın durum makinesi.

    Args:
        dispatcher: Tool'ları çalıştıracak, hazır ToolDispatcher.
        llm_client: Yerel Ollama istemcisi.
        overlay: Görsel geri bildirim penceresi (`ui.overlay.ArtemisOverlay`).
        settings: Uygulama ayarları.
    """

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        llm_client: OllamaLLMClient,
        overlay: Any,
        settings: Settings,
    ) -> None:
        self._dispatcher = dispatcher
        self._llm = llm_client
        self._overlay = overlay
        self._settings = settings

        self._stop_event = threading.Event()
        self._manual_trigger = threading.Event()
        self._thread: threading.Thread | None = None
        self._muted_until = 0.0  # geri besleme koruması (monotonic saat)
        # Ses işçisinin o an sahip olduğu mikrofon akışı. Onay dinlemesi
        # gibi iç adımlar İKİNCİ bir akış açmak yerine bunu kullanır:
        # aynı cihazı iki kez açmak bazı Windows ses sürücülerinde
        # başarısız olur ve gereksizdir.
        self._active_mic: Any = None

        self._system_prompt = build_system_prompt()
        self._planner = TaskPlanner(dispatcher, confirm_callback=self._confirm_by_voice)

        # Ağır bileşenler ilk kullanımda kurulur (bkz. _ensure_*).
        self._wake_detector = None
        self._recorder = None
        self._stt = None
        self._tts = None
        self._vocabulary: str | None = None
        # Uyandırma algılaması onarılamaz biçimde bozulduysa True olur;
        # asistan bu durumda kısayol/tepsi ile çalışmaya devam eder
        # (bkz. `_disable_wake_word`).
        self._wake_word_broken = False
        # Açılışta ortam gürültüsü ölçülerek doldurulur (bkz.
        # `_calibrate_silence_threshold`). None ise varsayılan kullanılır.
        self._silence_threshold: float | None = None

    # ------------------------------------------------------------------
    # Yaşam döngüsü
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Ses işçisini arka planda başlatır (ana iş parçacığını bloklamaz)."""

        if self._thread is not None:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="artemis-voice", daemon=True)
        self._thread.start()
        logger.info("Sesli asistan başlatıldı.")

    def stop(self) -> None:
        """Ses işçisini durdurur ve kapanmasını bekler."""

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._tts is not None:
            self._tts.stop()
        logger.info("Sesli asistan durduruldu.")

    def trigger(self) -> None:
        """Uyandırma sözcüğü beklemeden elle uyandırır (kısayol tuşu için)."""

        self._manual_trigger.set()

    # ------------------------------------------------------------------
    # Ana döngü
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Ses işçisinin gövdesi: mikrofonu açar ve durum makinesini sürer."""

        from voice.audio import MicrophoneStream, MicrophoneUnavailableError

        try:
            with MicrophoneStream() as mic:
                self._active_mic = mic
                self._calibrate_silence_threshold(mic)
                try:
                    while not self._stop_event.is_set():
                        self._sleep_until_woken(mic)
                        if self._stop_event.is_set():
                            break
                        self._handle_one_command(mic)
                finally:
                    self._active_mic = None
        except MicrophoneUnavailableError as exc:
            logger.error("Mikrofon açılamadı: %s", exc)
            self._overlay.show_error(f"Mikrofona erişilemiyor: {exc}")
        except Exception:  # noqa: BLE001 - işçi iş parçacığı asla sessizce ölmemeli
            logger.exception("Sesli asistan döngüsünde beklenmeyen hata")
            self._overlay.show_error("Sesli asistanda beklenmeyen bir hata oluştu.")

    def _sleep_until_woken(self, mic: Any) -> None:
        """Uyandırma sözcüğü (veya kısayol) gelene kadar hafif dinleme yapar.

        DAYANIKLILIK: Uyandırma algılaması bozulursa (eksik CUDA
        kütüphanesi, model indirilememesi gibi) asistanın TAMAMI ölmemeli.
        Böyle bir durumda uyandırma sözcüğü kalıcı olarak devre dışı
        bırakılır ve asistan kısayol tuşu / tepsi menüsüyle çalışmaya
        devam eder — kullanıcı en azından Artemis'i çağırabilir.

        Hata döngü başına bir kez raporlanır: her ses bloğunda tekrar
        denemek, saniyede birkaç kez aynı hatayı üretip log'u ve işlemciyi
        boğardı.
        """

        detector = None
        if self._settings.wake_word_enabled and not self._wake_word_broken:
            try:
                detector = self._ensure_wake_detector()
            except Exception as exc:  # noqa: BLE001 - model kurulamayabilir
                self._disable_wake_word(exc)

        while not self._stop_event.is_set():
            if self._manual_trigger.is_set():
                self._manual_trigger.clear()
                return

            block = mic.read_block()

            # Kendi sesimizi duyup uyanmayalım.
            if time.monotonic() < self._muted_until:
                continue

            if detector is None:
                continue  # yalnızca kısayol/tepsi ile uyandırılabilir

            try:
                if detector.feed(block):
                    return
            except Exception as exc:  # noqa: BLE001 - tanıma çalışma anında da patlayabilir
                self._disable_wake_word(exc)
                detector = None

    def _disable_wake_word(self, exc: Exception) -> None:
        """Uyandırma sözcüğünü kalıcı olarak kapatır ve kullanıcıyı bilgilendirir."""

        self._wake_word_broken = True
        self._wake_detector = None
        logger.error("Uyandırma sözcüğü devre dışı bırakıldı: %s", exc, exc_info=True)
        self._overlay.show_error(
            f"Uyandırma sözcüğü çalışmıyor; '{self._settings.voice_hotkey}' kısayolunu kullanın."
        )

    def _handle_one_command(self, mic: Any) -> None:
        """Tek bir komutu baştan sona yürütür: kaydet → çöz → çalıştır → söyle."""

        self._overlay.show_listening("Dinliyorum…")

        transcript = self._record_and_transcribe(mic)

        # Dökümü MUTLAKA logla. Bu satır olmadan, "asistan yanlış şeyi yaptı"
        # şikayetleri araştırılamıyor: log'da yalnızca hangi tool'un
        # çalıştığı görünüyor, kullanıcının ne dediği ve modelin ne
        # duyduğu görünmüyordu — yani hatanın tanımada mı yoksa LLM'in
        # tool seçiminde mi olduğu ayırt edilemiyordu.
        logger.info("Duyulan komut: %r", transcript)

        if not transcript:
            self._overlay.show_error("Sizi duyamadım.")
            time.sleep(1.4)
            self._overlay.dismiss()
            return

        # Duyulan metin başlığın altında, cevaptan AYRI bir satırda kalır:
        # kullanıcı yanlış anlaşıldığını ancak böyle fark edebilir.
        self._overlay.set_heard(transcript)
        self._overlay.show_thinking("Düşünüyorum…")

        # KOMUT KAPISI: her duyulan şey asistana yönelik değildir. Uyandırma
        # sözcüğü gürültüyle de tetiklenebiliyor ve sonrasında yakalanan
        # arka plan konuşması, tool seçiminde rastgele bir eyleme
        # dönüşüyordu (bkz. `llm_client.should_engage`). Sınır "komut mu"
        # değil "yönelik mi" olduğu için sorular ve sohbet de buradan
        # geçer — onları `assistant.reply` karşılar (v3.3, README §20f).
        if self._settings.command_gate_enabled and not self._llm.should_engage(transcript):
            self._respond("Bir komut duymadım.")
            return

        try:
            tool_calls = self._llm.get_tool_calls(self._system_prompt, transcript)
        except LLMResponseParseError:
            self._respond("Bunu anlayamadım, başka türlü söyler misiniz?", error=True)
            return
        except ConnectionError as exc:
            logger.error("Ollama bağlantı hatası: %s", exc)
            self._respond("Yerel modele ulaşamıyorum.", error=True)
            return

        # Hangi tool'un neden seçildiğini sonradan anlayabilmek için, LLM'in
        # döküme karşılık ürettiği planı da logla (bkz. yukarıdaki not).
        logger.info(
            "LLM planı: %s",
            [(call.get("tool"), call.get("arguments")) for call in tool_calls],
        )

        step_results = self._planner.execute_plan(tool_calls)
        self._respond(self._summarize(step_results, len(tool_calls)))

    def _record_and_transcribe(self, mic: Any) -> str:
        """Konuşma bitene kadar kaydeder ve metne çevirir."""

        from voice.audio import rms_amplitude

        recorder = self._ensure_recorder()
        recorder.reset()

        while not self._stop_event.is_set():
            block = mic.read_block()
            self._overlay.set_amplitude(rms_amplitude(block))
            if recorder.feed(block):
                break

        audio = recorder.audio_bytes
        recorder.reset()

        if not audio:
            return ""

        try:
            return self._ensure_stt().transcribe(audio, hotwords=self._ensure_vocabulary())
        except Exception:  # noqa: BLE001 - model yüklenemeyebilir
            logger.exception("Konuşma metne çevrilemedi")
            return ""

    # ------------------------------------------------------------------
    # Onay — sesli, ama bilinçli olarak "şüphede reddet"
    # ------------------------------------------------------------------

    def _confirm_by_voice(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Tehlikeli bir işlem için sesli onay ister.

        GÜVENLİK GEREKÇESİ: Ses tanıma hata yapabilir ve bu tool'lar geri
        alınamaz işlemler yapar (`filesystem.delete`, `windows.shutdown`).
        Bu yüzden kural, "hayır dediğini anlarsam durdur" DEĞİL, tam
        tersidir: **yalnızca net bir onay duyarsam devam et.** Sessizlik,
        anlaşılmayan bir cevap, zaman aşımı veya mikrofon hatası —
        hepsi RED sayılır.

        Kullanıcı neyi onayladığını görebilsin diye işlem ve argümanları
        ekranda gösterilir (bkz. README §16b: kör onay güvenlik açığıdır).
        """

        argument_text = ", ".join(f"{k}: {v}" for k, v in arguments.items()) or "argüman yok"
        self._overlay.show_error(f"Onay gerekiyor — {tool_name} ({argument_text}). 'Evet' deyin.")
        self._speak(f"{tool_name} işlemi onay gerektiriyor. Onaylıyor musunuz?")

        answer = self._listen_for_confirmation()
        approved = _is_affirmative(answer)

        logger.info(
            "Sesli onay: tool=%s duyulan=%r sonuç=%s",
            tool_name,
            answer,
            "ONAYLANDI" if approved else "REDDEDİLDİ",
        )
        return approved

    def _listen_for_confirmation(self) -> str:
        """Kısa bir süre onay cevabı dinler; duyamazsa boş string döner.

        Ses işçisinin hâlihazırda açık olan mikrofon akışını kullanır
        (bkz. `_active_mic`); yeni bir akış açmaz.
        """

        from voice.audio import rms_amplitude

        mic = self._active_mic
        if mic is None:
            logger.error("Onay dinlenemedi: etkin mikrofon akışı yok.")
            return ""

        recorder = self._ensure_recorder()
        recorder.reset()
        deadline = time.monotonic() + _CONFIRMATION_TIMEOUT_SECONDS

        try:
            while time.monotonic() < deadline and not self._stop_event.is_set():
                block = mic.read_block()
                self._overlay.set_amplitude(rms_amplitude(block))
                if recorder.feed(block):
                    break
        except Exception:  # noqa: BLE001 - mikrofon hatası = onay yok = red
            logger.exception("Onay dinlenirken mikrofon hatası")
            return ""

        audio = recorder.audio_bytes
        recorder.reset()
        if not audio:
            return ""

        try:
            # Burada uygulama sözlüğü DEĞİL, onay sözcükleri ipucu verilir:
            # beklenen cevap "evet"/"hayır"tır, "Discord" değil. Yanlış
            # sözlük vermek, modeli olmayacak bir kelimeye zorlardı.
            answer = self._ensure_stt().transcribe(audio, hotwords=", ".join(sorted(_AFFIRMATIVE_WORDS)))
            return answer.lower()
        except Exception:  # noqa: BLE001 - anlaşılmayan cevap = red
            logger.exception("Onay cevabı çözümlenemedi")
            return ""

    # ------------------------------------------------------------------
    # Cevap üretme ve konuşma
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(step_results: list[Any], total: int) -> str:
        """Plan sonuçlarını sesli okunacak tek bir cümleye indirger.

        Terminalde adımlar tek tek yazdırılabilir ama sesli asistanda uzun
        listeler dinlemesi yorucudur; bu yüzden tek adımda doğrudan
        sonucun mesajı, çok adımda kısa bir özet okunur.
        """

        if not step_results:
            return "Yapılacak bir işlem bulamadım."

        if total == 1:
            return step_results[0].result.message

        successful = sum(1 for step in step_results if step.result.success)
        if successful == total:
            return f"{total} işlemin hepsini tamamladım."

        return f"{total} işlemden {successful} tanesini tamamladım; kalanında sorun oldu."

    def _respond(self, message: str, error: bool = False) -> None:
        """Cevabı ekranda gösterir, sesli okur ve pencereyi kapatır.

        Kullanıcıya giden metin `speakable()` ile kısaltılır: tool
        mesajları terminal için yazıldığından tam yol/tam URL içeriyor ve
        bunları dinlemek işkence ("C ters bölü Users ters bölü..."). Tam
        mesaj kaybolmaz — log'a olduğu gibi yazılır.
        """

        spoken = speakable(message)
        if spoken != message:
            logger.info("Cevap (tam): %s", message)

        if error:
            self._overlay.show_error(spoken)
        else:
            self._overlay.show_speaking(spoken)

        self._speak(spoken)
        self._overlay.dismiss()

    def _speak(self, message: str) -> None:
        """Metni sesli okur; TTS yoksa sessizce geçer (akış bozulmaz).

        Konuşma boyunca dalga formu, Artemis'in kendi sesine göre canlanır.
        Bittiğinde geri besleme koruması için kısa bir süre uyandırma
        algılaması susturulur.
        """

        try:
            self._ensure_tts().speak(message, on_amplitude=self._overlay.set_amplitude)
        except Exception:  # noqa: BLE001 - ses çıkışı yoksa asistan yine de çalışmalı
            # Hem bulut hem yerel başarısız oldu (internet yok + Piper modeli
            # kurulu değil gibi). Sessiz kalmak yerine, cevabın okunabilmesi
            # için pencereyi metin uzunluğuyla orantılı bir süre açık tut.
            logger.exception("Sesli okuma başarısız; yalnızca ekranda gösteriliyor")
            time.sleep(min(3.0, 0.05 * len(message)))
        finally:
            self._muted_until = time.monotonic() + _FEEDBACK_COOLDOWN_SECONDS

    # ------------------------------------------------------------------
    # Ağır bileşenlerin gecikmeli (lazy) kurulumu
    # ------------------------------------------------------------------

    def _ensure_vocabulary(self) -> str:
        """Whisper'a ipucu olarak verilecek özel isim sözlüğünü kurar.

        Ses tanımanın bu projedeki en zayıf noktası yabancı özel
        isimlerdir: "Discord", "Riot Client", "Valorant" Türkçe bir model
        için sözlük dışıdır ve ipucu verilmezse "Dischord", "Biyot
        Cilent" diye çevrilir (ölçüldü, bkz. `voice/stt.py::transcribe`).

        Sözlük, kullanıcının GERÇEKTEN kurulu uygulamalarından üretilir —
        sabit bir liste her makinede yanlış olurdu. Başlat Menüsü taraması
        pahalı olduğu için `windows_plugin`'in zaten oluşturduğu tekil
        `AppResolver` örneği paylaşılır ve sonuç bir kez hesaplanır.
        """

        if self._vocabulary is not None:
            return self._vocabulary

        names: list[str] = list(self._settings.wake_words[:1])  # "artemis"
        try:
            from plugins.windows_plugin import _app_resolver

            names.extend(_app_resolver.known_app_names())
        except Exception:  # noqa: BLE001 - sözlük olmadan da tanıma çalışmalı
            logger.warning("Uygulama sözlüğü oluşturulamadı; ipucusuz devam ediliyor.", exc_info=True)

        # Türkçe komut fiilleri olmadan sözlük tamamen İngilizce kalır ve
        # kod çözücü "aç" gibi sözcükleri "such"a benzetir (bkz. sabitin
        # dokümantasyonundaki ölçüm).
        names.extend(_TURKISH_COMMAND_WORDS)

        self._vocabulary = ", ".join(names)
        logger.info("Ses tanıma ipucu sözlüğü hazır (%d ad).", len(names))
        return self._vocabulary

    def _ensure_wake_detector(self):
        if self._wake_detector is None:
            from voice.wake_word import WakeWordDetector

            # Uyandırma için KÜÇÜK model (varsayılan "tiny") kullanılır:
            # sürekli tetiklenebildiği için ucuz olmalı. Komut tanımada ise
            # doğruluk kritik olduğundan `whisper_model_size` (varsayılan
            # "small") geçerlidir — bkz. `_ensure_stt`.
            self._wake_detector = WakeWordDetector(
                wake_words=tuple(self._settings.wake_words),
                model_size=self._settings.wake_word_model_size,
                compute_type=self._settings.whisper_compute_type,
                device=self._settings.whisper_device,
                # Konuşma/gürültü ayrımını Vosk yapar; sabit enerji eşiği
                # değişken ortam gürültüsünde çalışmıyordu (bkz.
                # `voice/wake_word.py` modül dokümantasyonu).
                vosk_model_path=self._settings.vosk_speech_gate_model_path,
            )
            # Açılışta ortam ölçüldüyse kalibre edilmiş eşiği kullan;
            # sabit bir sayı, değişken ortam gürültüsünde çalışmıyordu.
            if self._silence_threshold is not None:
                self._wake_detector.set_energy_threshold(self._silence_threshold)
        return self._wake_detector

    def _calibrate_silence_threshold(self, mic: Any) -> None:
        """Açılışta ortam gürültüsünü ölçüp sessizlik eşiğini ona göre ayarlar.

        Sabit bir eşik, ortam gürültüsü değişken olduğu için çalışmıyordu:
        eşiğin üstünde kalan bir odada kayıt "kullanıcı sustu" diyemiyor ve
        komutun etrafındaki konuşmaları da yutuyordu (bkz.
        `voice.audio.measure_noise_floor`).

        Ölçüm başarısız olursa varsayılan eşik korunur — kalibrasyonun
        kendisi asistanı çalışmaz hâle getirmemeli.
        """

        from voice.audio import measure_noise_floor

        try:
            self._silence_threshold = measure_noise_floor(mic)
        except Exception:  # noqa: BLE001 - kalibrasyon başarısızlığı akışı durdurmasın
            logger.warning("Ortam gürültüsü ölçülemedi; varsayılan eşik kullanılacak.", exc_info=True)
            return

        # Uyandırma algılayıcısı zaten kurulduysa yeni eşiği ona da ver:
        # gürültü sorunu hem kayıtta hem uyandırmada aynıydı.
        if self._wake_detector is not None:
            self._wake_detector.set_energy_threshold(self._silence_threshold)

    def _ensure_recorder(self):
        if self._recorder is None:
            from voice.stt import SpeechRecorder

            from voice.stt import DEFAULT_SILENCE_THRESHOLD

            self._recorder = SpeechRecorder(
                silence_timeout=self._settings.silence_timeout_seconds,
                max_duration=self._settings.max_command_seconds,
                # Açılışta ölçülen ortam gürültüsüne göre ayarlanmış eşik;
                # kalibrasyon yapılamadıysa varsayılana düşülür.
                silence_threshold=self._silence_threshold or DEFAULT_SILENCE_THRESHOLD,
            )
        return self._recorder

    def _ensure_stt(self):
        """Konuşma tanımayı kurar: internet varsa bulut, yoksa yerel.

        Sağlayıcılar FABRİKA olarak verilir, hazır nesne olarak değil —
        böylece bulut çalıştığı sürece yerel Whisper hiç yüklenmez ve
        RAM maliyeti hiç ödenmez (bkz. `voice/router.py`).
        """

        if self._stt is not None:
            return self._stt

        from config.settings import get_groq_api_key
        from voice.router import SpeechToTextRouter

        def build_cloud():
            """Ayarda seçilen bulut tanıma servisini kurar.

            Anahtarı olmayan bir servis seçilirse burada hata FIRLATILMAZ;
            sağlayıcılar anahtarı ancak `transcribe()` sırasında kontrol
            eder ve yönlendirici o hatayı yakalayıp yerele düşer. Yani
            yanlış/eksik yapılandırma asistanı bozmaz, yalnızca doğruluğu
            etkiler.
            """

            if self._settings.stt_cloud_provider == "azure":
                from config.settings import get_azure_speech_credentials
                from voice.stt_azure import AzureSpeechToText

                api_key, region = get_azure_speech_credentials()
                return AzureSpeechToText(
                    api_key=api_key,
                    region=region,
                    timeout=self._settings.cloud_timeout_seconds,
                )

            from voice.stt_cloud import GroqSpeechToText

            return GroqSpeechToText(
                api_key=get_groq_api_key(),
                model=self._settings.groq_model,
                timeout=self._settings.cloud_timeout_seconds,
            )

        def build_local():
            from voice.stt import SpeechToText

            return SpeechToText(
                model_size=self._settings.whisper_model_size,
                compute_type=self._settings.whisper_compute_type,
                device=self._settings.whisper_device,
            )

        self._stt = SpeechToTextRouter(
            cloud_factory=build_cloud,
            local_factory=build_local,
            mode=self._settings.stt_provider,
            failure_cooldown=self._settings.cloud_failure_cooldown_seconds,
        )
        return self._stt

    def _ensure_tts(self):
        """Sesli okumayı kurar: internet varsa Edge, yoksa yerel Piper.

        Yönlendirici oluşturmak hiçbir ağ/model işi yapmaz, bu yüzden
        eskisinden farklı olarak burada None dönme durumu yoktur; bir
        sağlayıcı gerçekten kullanılamazsa bu `speak()` sırasında
        anlaşılır. Piper modeli yoksa ve internet de yoksa `_speak()`
        hatayı yakalar ve asistan sessiz çalışmaya devam eder.
        """

        if self._tts is not None:
            return self._tts

        from voice.router import TextToSpeechRouter

        def build_cloud():
            from voice.tts_cloud import EdgeTextToSpeech

            return EdgeTextToSpeech(
                voice=self._settings.edge_tts_voice,
                timeout=self._settings.cloud_timeout_seconds,
                rate=self._settings.edge_tts_rate,
                pitch=self._settings.edge_tts_pitch,
            )

        def build_local():
            from voice.tts import TextToSpeech

            return TextToSpeech(model_path=self._settings.piper_model_path)

        self._tts = TextToSpeechRouter(
            cloud_factory=build_cloud,
            local_factory=build_local,
            mode=self._settings.tts_provider,
            failure_cooldown=self._settings.cloud_failure_cooldown_seconds,
        )
        return self._tts
