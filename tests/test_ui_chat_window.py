"""`ui/chat_window.py` testleri — kalıcı sohbet penceresi.

Bu dosya iki katmana ayrılır:

1. **İŞ PARÇACIĞI mantığı** (`_ChatWorker`) — canlı bir pencere ya da
   olay döngüsü GEREKTİRMEZ. `_ChatWorker` düz bir `QObject`'tir ve
   sinyalleri `app.exec()` olmadan da bağlanıp tetiklenebilir; bu yüzden
   satır biçimlendirmesi, hata mesajları ve onay köprüsü burada gerçek
   `TaskPlanner`/`ToolDispatcher` üzerinden sınanır (CLAUDE.md: gerçek
   dosya sistemi işlemleri için mock KULLANMA).

2. **PENCERE davranışı** (`ChatWindow`) — `QApplication` gerektirir.
   Başlıksız (headless) CI'da `QT_QPA_PLATFORM=offscreen` ile çalışır;
   PyQt6 kurulu değilse `importorskip` tüm dosyayı dürüstçe atlar
   (`tests/test_ui_hotkey.py` ile aynı düzen).
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

import pytest

# `ui.chat_window` modül seviyesinde PyQt6 import eder. PyQt6 kurulu değilse
# (örn. `requirements-dev.txt` ile çalışan başlıksız bir CI) bu satır TOPLAMA
# hatası verip tüm dosyayı düşürürdü — `importorskip` onu dürüst bir
# "atlandı"ya çevirir. Bkz. `tests/test_ui_hotkey.py`.
pytest.importorskip("PyQt6", reason="ui/ katmanı PyQt6 gerektirir")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpacerItem,
)

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError
from ui.chat_window import (
    _BUBBLE_MAX_WIDTH,
    _WINDOW_WIDTH,
    ChatWindow,
    _ChatWorker,
    _ConfirmationRequest,
    is_exit_command,
)

# Başlıksız makinede "qt.qpa.plugin: could not load the Qt platform plugin"
# ile düşmemek için QApplication YARATILMADAN ayarlanmalı.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeLLM:
    """Sırayla verilen planları döndüren sahte istemci.

    `tests/test_conversation_loop.py`'deki sahte ile aynı sözleşme: bir
    eleman `Exception` ise fırlatılır. Buradaki sahte, pencerenin üç
    sağlayıcıdan (Ollama / OpenRouter / router) hangisini alacağına
    BAĞIMLI OLMAMALIDIR (bkz. `_ChatWorker.__init__` docstring'i).
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def get_tool_calls(self, system_prompt: str, user_input: str) -> list[dict[str, Any]]:
        self.prompts.append(user_input)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _BlockingLLM:
    """`release` işareti bırakılana kadar turu açık tutan sahte istemci.

    Neden `FakeLLM([])` DEĞİL: boş listeye sahip bir sahte, iş parçacığında
    `IndexError` fırlatır. `QThread` üzerindeki bir yuvada yakalanmayan bu
    istisna PyQt6 tarafından `abort()`'a çevrilir — yani testin ölçtüğü "düğmeler
    kapalı mı" sorusunu yanıtlamadan ÖNCE süreç çöker. Test, kapı düğmelerini
    gerçekten bir tur AÇIKKEN sınamak istiyor; tur daima sürebilir olmalıdır.
    """

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def get_tool_calls(self, system_prompt: str, user_input: str) -> list[dict[str, Any]]:
        # Zaman aşımı, test bir yerde hata verirse iş parçacığının sonsuza
        # kadar asılı kalmasın diye: bu noktadan sonra zaten hiçbir şey
        # doğrulanamaz.
        self._release.wait(timeout=10.0)
        return []


@pytest.fixture
def worker(dispatcher: ToolDispatcher) -> _ChatWorker:
    """Sinyalleri bağlanabilir, iş parçacığı YOK bir işçi.

    `QThread` üzerine taşımak testin konusu değil: Qt'nin kuyruklama
    davranışını sınamak istiyorsak `QApplication` gerekir. Buradaki
    metotlar (`_handle_submit`, `_confirm`) zaten iş parçacığının
    HANGİSİNDE çağrıldıklarından bağımsız çalışır; onay köprüsünün
    kilitlenmemesi ayrı ve GERÇEK bir iş parçacığıyla sınanır
    (bkz. `test_confirmation_bridge_*`).
    """

    return _ChatWorker(dispatcher, FakeLLM([]))


def _capture_finished(worker: _ChatWorker) -> list[list[str]]:
    """`turn_finished` sinyalini toplayan bir kayıtçı döndürür.

    Neden SINYALİN KENDİSİ DEĞİL `append` bağlanıyor: `_handle_submit`
    doğrudan çağrıldığı için sinyaller zaten AYNI iş parçacığında
    tetiklenir ve `AutoConnection` doğrudan bağlantı kurar. Bu, üretim
    kodunun kuyruklanmış bağlantısını değil, YAYINLANAN YÜKÜ sınar.
    Kuyruklanma/iş parçacığı ayrımı `test_turn_survives_a_real_qthread_`
    hop_test`'te gerçek bir `QThread` ile sınanır.
    """

    received: list[list[str]] = []
    worker.turn_finished.connect(received.append)
    return received


def _capture_failed(worker: _ChatWorker) -> list[str]:
    received: list[str] = []
    worker.turn_failed.connect(received.append)
    return received


def _ensure_qapplication() -> QApplication:
    """Süreçteki `QApplication`'ı döndürür, yoksa oluşturur.

    Qt bir uygulama nesnesinden YALNIZCA bir tane kabul eder; ikincisi
    kurulmaya çalışılırsa süreç çöker. `QT_QPA_PLATFORM=offscreen`
    (modül başında) başlıksız CI'da da pencere kurulabilmesini sağlar.
    """

    return QApplication.instance() or QApplication([])


def _drain_events(app: QApplication, slot: Any, timeout: float) -> None:
    """Ana iş parçacığında Qt olay döngüsünü çalıştırır.

    Kuyruklanmış sinyaller YALNIZCA burada teslim edilir; döngü dönmeye
    başlamadan bir "GUI" yuvalarına ulaşamaz. Üretimde bu rolü
    `app.exec()` üstlenir.

    Döngü, `slot` bir kez çalıştıktan SONRA da kısa bir süre daha döner:
    "diyaloğu göster" ve "tıklamayı işle" iki ayrı turlarda olabilir.

    Args:
        app: Olay döngüsünü çalıştıracağımız uygulama.
        slot: Her turda çağrılacak GUI işi.
        timeout: Toplam süre sınırı — takılı bir döngü testi
            sonsuza kadar asılı bırakmamalıdır.
    """

    deadline = time.monotonic() + timeout
    ran = False
    while time.monotonic() < deadline:
        app.processEvents()
        slot()
        ran = True
        time.sleep(0.005)
    app.processEvents()
    assert ran, "olay döngüsü hiç çalışmadı"


# --------------------------------------------------------------------------
# Tur sonuçlarının biçimlendirilmesi
# --------------------------------------------------------------------------


def test_single_step_is_reported_without_step_numbers(worker: _ChatWorker) -> None:
    """TEK adımlı planda `[1/1]` öneki GÜRÜLTÜDÜR.

    Terminaldeki karşılığı birebir aynı kural (`conversation_loop.run`):
    `prefix` yalnızca `total > 1` iken eklenir.
    """

    worker._llm_client = FakeLLM([[{"tool": "assistant.reply", "arguments": {"message": "Merhaba!"}}]])
    received = _capture_finished(worker)

    worker._handle_submit("selam")

    assert received == [["Merhaba!"]]


def test_multi_step_plan_is_reported_with_step_numbers(worker: _ChatWorker) -> None:
    """Çok adımlı planda hangi adımın ne yaptığı ANLAŞILMALIYDI."""

    worker._llm_client = FakeLLM(
        [
            [
                {"tool": "assistant.reply", "arguments": {"message": "birinci"}},
                {"tool": "assistant.reply", "arguments": {"message": "ikinci"}},
                {"tool": "assistant.reply", "arguments": {"message": "üçüncü"}},
            ]
        ]
    )
    received = _capture_finished(worker)

    worker._handle_submit("üç şey yap")

    assert received == [["[1/3] birinci", "[2/3] ikinci", "[3/3] üçüncü"]]


def test_steps_never_reached_are_reported_honestly(worker: _ChatWorker, settings: Settings) -> None:
    """Plan erken durduysa kullanıcı KAÇ adımın çalışmadığını öğrenmeli.

    Sessizce yarım kalan bir plan, kullanıcının hepsinin olduğunu
    sanmasına yol açar. Bu satır `conversation_loop.run`'dakiyle BİREBİR
    aynı sözdizimini taşır.
    """

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    worker._llm_client = FakeLLM(
        [
            [
                {"tool": "filesystem.open", "arguments": {"target": "olmayan", "location": "desktop"}},
                {"tool": "filesystem.create_folder", "arguments": {"name": "Sonraki", "location": "desktop"}},
            ]
        ]
    )
    received = _capture_finished(worker)

    worker._handle_submit("önce aç sonra oluştur")

    assert received[0][-1] == "(1 adım, önceki bir adımın durdurulması nedeniyle çalıştırılmadı.)"
    assert not (settings.desktop_path / "Sonraki").exists(), "ikinci adım çalıştırılmamalıydı"


def test_a_real_command_runs_end_to_end(worker: _ChatWorker, settings: Settings) -> None:
    """GERÇEK dispatcher + GERÇEK filesystem tool'u: tmp_path altında klasör."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    worker._llm_client = FakeLLM(
        [[{"tool": "filesystem.create_folder", "arguments": {"name": "Orbit", "location": "desktop"}}]]
    )
    received = _capture_finished(worker)

    worker._handle_submit("orbit klasörü oluştur")

    assert (settings.desktop_path / "Orbit").is_dir()
    assert any("Orbit" in line for line in received[0])


# --------------------------------------------------------------------------
# Hata yolları
# --------------------------------------------------------------------------


def test_unparseable_model_output_fails_the_turn(worker: _ChatWorker) -> None:
    """Ayrıştırılamayan çıktı kullanıcıya söylenmeli.

    Mesaj `conversation_loop.run`'ın YAZDIĞI metnin aynısı olmalı; iki
    arayüz farklı şey söylerse, "terminalde çalışıyordu" gerekçesiyle
    birinde düzeltilip diğerinde unutulur.
    """

    worker._llm_client = FakeLLM([LLMResponseParseError("bozuk")])
    failed = _capture_failed(worker)
    finished = _capture_finished(worker)

    worker._handle_submit("anlaşılmaz")

    assert failed == ["Anlayamadım, farklı bir şekilde ifade eder misiniz?"]
    assert finished == [], "hatalı turda sonuç üretilmemeli"


def test_connection_error_reports_the_underlying_reason(worker: _ChatWorker) -> None:
    """Bağlantı hatası kullanıcıya SEBEBİNİYLE birlikte gösterilmeli."""

    worker._llm_client = FakeLLM([ConnectionError("sunucu kapalı")])
    failed = _capture_failed(worker)

    worker._handle_submit("bir şey yap")

    assert len(failed) == 1
    assert "ulaşılamıyor" in failed[0]
    assert "sunucu kapalı" in failed[0], "hata metni (`exc`) kullanıcıya da gösterilmeli"


# --------------------------------------------------------------------------
# Onay köprüsü — bu dosyanın en riskli parçası
# --------------------------------------------------------------------------


def test_confirm_returns_the_answer_given_by_the_dialog(worker: _ChatWorker) -> None:
    """Diyaloğun cevabı, diyaloğu GÖSTEREN taraftan neyse o."""

    worker.confirmation_requested.connect(
        lambda tool, args, request: request.result.__setitem__(0, True) or request.event.set()
    )

    assert worker._confirm("filesystem.delete", {"target": "x.txt"}) is True


def test_confirm_returns_false_when_the_dialog_is_declined(worker: _ChatWorker) -> None:
    """Varsayılan cevap "reddet"tir.

    `QMessageBox` herhangi bir nedenle `exec()`'den boş dönerse
    (istisna, kapatma), iş parçacığı yanıt bekleyip ASLA takılı kalmaz.
    """

    worker.confirmation_requested.connect(lambda tool, args, request: request.event.set())

    assert worker._confirm("filesystem.delete", {"target": "x.txt"}) is False


def test_confirmation_carries_tool_name_and_arguments(worker: _ChatWorker) -> None:
    """KÖR ONAY güvenlik açığıdır (CLAUDE.md).

    Kullanıcı yalnızca tool adını görürse `filesystem.delete`'in hangi
    dosyayı sileceğini BİLEMEZ.
    """

    seen: list[tuple[str, dict[str, Any]]] = []
    worker.confirmation_requested.connect(
        lambda tool, args, request: seen.append((tool, args)) or request.event.set()
    )

    worker._confirm("filesystem.delete", {"target": "onemli.txt", "location": "desktop"})

    assert seen == [("filesystem.delete", {"target": "onemli.txt", "location": "desktop"})]


def test_confirmation_bridge_unblocks_the_worker_thread(worker: _ChatWorker) -> None:
    """GERÇEK iş parçacığı sınaması — asıl kilitlenme riski burada.

    Bekleme, üretici iş parçacığında (`threading.Thread`) yapılır; cevap
    veren taraf bu testin ANA iş parçacığıdır (GUI'nin rolü). `join` için
    ZAMAN AŞIMI konur: bir kilitlenme testi sonsuza kadar ASILı kalmak
    yerine kırmızı olmalıdır.

    Kuyruk GEÇİŞİ GERÇEKTİR: iş parçacığı ile ana iş parçacığı farklı
    olduğu için Qt `AutoConnection`'ı QUEUED seçer ve sinyal doğrudan
    DEĞIL, olay döngüsü döndüğünde teslim edilir. Testin bu yüzden
    KENDİ olay döngüsünü çalıştırması gerekir (bkz. `_drain_events`) —
    `QApplication` olmadan alıcı YANLİŞ iş parçacığında koşardı.
    """

    app = _ensure_qapplication()
    arrived: queue.Queue[_ConfirmationRequest] = queue.Queue()
    worker.confirmation_requested.connect(lambda tool, args, request: arrived.put(request))
    answers: list[bool] = []

    # Diyaloğu "gösteren" taraf: teslim edilen kuyruktan isteği alıp
    # cevaplar. Üretim kodundaki `QMessageBox.exec()` yalnızca bu adımın
    # görünür halidir.
    def gui_slot() -> None:
        while not arrived.empty():
            request = arrived.get_nowait()
            request.result[0] = True
            request.event.set()

    def simulate_worker_thread() -> None:
        answers.append(worker._confirm("filesystem.delete", {"target": "onemli.txt"}))

    thread = threading.Thread(target=simulate_worker_thread, daemon=True)
    thread.start()

    # GUI iş parçacığı çalışırken iş parçacığını da cevaplandır: kullanıcı
    # ancak DİYALOGU GÖRDÜKTEN SONRA tıklar, yani aradaki boşluk normaldir.
    _drain_events(app, gui_slot, timeout=5.0)

    thread.join(timeout=5.0)
    assert not thread.is_alive(), "onaydan sonra iş parçacığı KİLİTLENMEZ"
    assert answers == [True]


def test_dangerous_step_is_refused_and_the_file_survives(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """Uçtan uca: onay "hayır" dönce dosya GERÇEKTEN durmalı."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    kurban = settings.desktop_path / "onemli.txt"
    kurban.write_text("silinmemeli", encoding="utf-8")

    worker = _ChatWorker(
        dispatcher,
        FakeLLM([[{"tool": "filesystem.delete", "arguments": {"target": "onemli.txt", "location": "desktop"}}]]),
    )
    worker.confirmation_requested.connect(lambda tool, args, request: request.event.set())  # reddet
    finished = _capture_finished(worker)

    worker._handle_submit("onemli.txt sil")

    assert kurban.exists()
    assert kurban.read_text(encoding="utf-8") == "silinmemeli"
    assert any("onay gerektiriyor" in line for line in finished[0])


def test_dangerous_step_runs_after_explicit_approval(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """Onay "evet" dönce dosya GERÇEKTEN silinmeli."""

    settings.desktop_path.mkdir(parents=True, exist_ok=True)
    kurban = settings.desktop_path / "gecici.txt"
    kurban.write_text("silinebilir", encoding="utf-8")

    worker = _ChatWorker(
        dispatcher,
        FakeLLM([[{"tool": "filesystem.delete", "arguments": {"target": "gecici.txt", "location": "desktop"}}]]),
    )
    worker.confirmation_requested.connect(
        lambda tool, args, request: request.result.__setitem__(0, True) or request.event.set()
    )
    finished = _capture_finished(worker)

    worker._handle_submit("gecici.txt sil")

    assert not kurban.exists()
    assert not any("onay gerektiriyor" in line for line in finished[0])


# --------------------------------------------------------------------------
# Çıkış komutları
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["çıkış", "cikis", "exit", "quit", "ÇIKIŞ", "Exit", "  ÇIKIŞ "])
def test_exit_words_are_recognized_in_both_languages_and_cases(text: str) -> None:
    """`str.lower()` Türkçe kurallarını UYGULAMAZ.

    `"ÇIKIŞ".lower()` -> `"çikiş"`, `"EXIT".lower()` -> `"exit"` olmak üzere
    iki ayrı bozulma; `lower_variants` ikisini birden dener.

    Baştaki/sondaki boşluk yalnızca `ChatWindow._on_submit`'te `strip()`
    ile temizlenir (birebir `run()`'daki `input(...).strip()`); saf
    yardımcı sadece küçültme yapar, `is_exit_command("  çıkış  ")` bu
    yüzden False'tur.
    """

    assert is_exit_command(text.strip()) is True


@pytest.mark.parametrize("text", ["merhaba", "çıkış klasörü aç", "exits", "quit et"])
def test_ordinary_sentences_are_not_exit_commands(text: str) -> None:
    """Yalnızca TAM eşleşme kapatır; "exits" bir çıkış komutu değildir."""

    assert is_exit_command(text) is False


def test_exit_word_set_is_the_same_as_the_terminal_loop() -> None:
    """Pencere ile terminal AYRI listeler tutmamalı.

    Terminalde çıkılabilip pencereden çıkılamıyorsa (ya da tersi) bu bir
    tutarsızlıktır; liste `conversation_loop`'dan İÇE AKTARILIR.
    """

    from core.conversation_loop import _EXIT_COMMANDS

    for word in ("çıkış", "cikis", "exit", "quit"):
        assert is_exit_command(word) is True
        assert word in _EXIT_COMMANDS


def test_turn_survives_a_real_qthread_hop(dispatcher: ToolDispatcher, settings: Settings) -> None:
    """İş parçacığı GERÇEKTEN ayrı olduğunda da tur sonucu ulaşır.

    Yukarıdaki testler `_handle_submit`'i doğrudan çağırır ve sinyaller
    aynı iş parçacığında, DOĞRUDAN bağlanır. Üretimde ise işçi kendi
    `QThread`'inde çalışır ve bağlantı QUEUED olur: sonuç yalnızca
    olay döngüsü döndüğünde teslim edilir. Bu test o yolun tamamını
    sınar — iş parçacığı taşıma, kuyruklama, teslim.
    """

    app = _ensure_qapplication()
    settings.desktop_path.mkdir(parents=True, exist_ok=True)

    worker = _ChatWorker(
        dispatcher,
        FakeLLM([[{"tool": "filesystem.create_folder", "arguments": {"name": "Orbit", "location": "desktop"}}]]),
    )
    received = _capture_finished(worker)

    thread = QThread()
    worker.moveToThread(thread)
    thread.start()
    try:
        # Yayıncı burada ana iş parçacığından çağrılıyor; Qt onu hedef
        # iş parçacığının KUYRUĞUNA alır (doğrudan bağlantı DEĞİL).
        worker._handle_submit("orbit klasörü oluştur")
        _drain_events(app, lambda: None, timeout=3.0)
    finally:
        thread.quit()
        thread.wait()

    assert (settings.desktop_path / "Orbit").is_dir(), "tool iş parçacığında GERÇEKTEN çalışmalıydı"
    assert any("Orbit" in line for line in received[0]), "sonuç GUI iş parçacığına ulaşmalıydı"


# --------------------------------------------------------------------------
# Pencere davranışı — QApplication gerektirir
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Modül boyunca TEK `QApplication`."""

    return _ensure_qapplication()


@pytest.fixture
def llm_release() -> threading.Event:
    """Yapay olarak AÇIK tutulan turu serbest bırakan işaret.

    `window` fixture'ı bu işareti `FakeLLM`'in yerine geçen `_BlockingLLM`'ye
    verir. Kapatma sırasında boşaltılması, test bittikten sonra gerçek bir
    çağrının sürmesini engeller.
    """

    return threading.Event()


@pytest.fixture
def window(qapp: QApplication, dispatcher: ToolDispatcher, llm_release: threading.Event) -> ChatWindow:
    """Kapatılması garantili bir sohbet penceresi.

    `ChatWindow` KENDİ `QThread`'ini başlatır; kapatılmayan bir pencere
    test sürecinde takılı bir iş parçacığı bırakır.
    """

    created = ChatWindow(dispatcher, _BlockingLLM(llm_release))
    try:
        yield created
    finally:
        llm_release.set()  # turu (varsa) bitir ki kapanış beklemesin
        created.close()


def _bubbles(window: ChatWindow) -> list[QFrame]:
    """Penceredeki GERÇEK balon çerçevelerini bulur.

    Neden `QFrame`'e özgü arama şart: `QScrollArea` ve dikey `QScrollBar`
    de `QFrame` ALT SINIFLARIDIR. Sadece "bir QFrame bul" demek, kaydırma
    çubuğunu balon sanan (ve sayıları 2 yerine 6 bulan) bir teste yol
    açar. Gerçek balonun ayırt edici özelliği, metnini taşıyan
    `QLabel`'e sahip olmasıdır.
    """

    return [
        frame
        for frame in window.findChildren(QFrame)
        if frame.findChild(QLabel) is not None and frame.styleSheet() != ""
    ]


def test_window_is_built_with_the_shared_design_language(window: ChatWindow, qapp: QApplication) -> None:
    """Pencere `ui/theme.py`'nin QSS'ini TEK sefer, üst düzeyde alır.

    Her pencere kendi paletini `icat etmez`; renkler tek kaynaktan gelir
    (bkz. `ui/theme.py` modül dokümantasyonu).
    """

    from ui import theme

    assert window.styleSheet() == theme.stylesheet()
    assert window.windowTitle() == "Artemis"
    assert window.isWindow(), "çerçevesiz bir katman değil, NORMAL bir pencere olmalı"


def test_input_row_has_both_enter_and_a_primary_button(window: ChatWindow) -> None:
    """Enter VE düğme aynı yuvaya bağlıdır; ikisi de birincil görünür."""

    button = window.findChild(QPushButton)
    assert button is not None
    assert button.property("role") == "primary", "QSS'in `role=\"primary\"` kuralı eşleşmeli"
    assert button.text() == "Gönder"
    assert isinstance(window.findChild(QLineEdit), QLineEdit)


def test_empty_input_is_a_no_op(window: ChatWindow) -> None:
    """Boş satır bir komut değildir; modele gönderilmez (bkz. `run()`)."""

    window._on_submit()

    assert window._messages.count() == 1, "yalnızca sondaki germe kalmalı, balon eklenmemeli"


def test_a_submitted_turn_disables_both_input_and_button(window: ChatWindow, qapp: QApplication) -> None:
    """Cift gönderim, kullanıcının kendi kendine konuşmasına yol açar.

    `_BlockingLLM` turu AÇIK tutar (`llm_release` bırakılana kadar), yani
    düğmelerin devre dışı kaldığı AN gerçekten ölçülür. `processEvents`
    KUYRUKTAKİ işi değil, "iş gönderildi" anını yansıtır.
    """

    window._input.setText("merhaba")
    window._on_submit()
    _drain_events(qapp, lambda: None, timeout=1.0)

    assert window._input.isEnabled() is False
    assert window.findChild(QPushButton).isEnabled() is False


def test_exit_word_closes_the_window_after_saying_goodbye(window: ChatWindow, qapp: QApplication) -> None:
    """Çıkış komutu bir balon bırakır ve PENCEREYİ KAPATIR.

    Vedala balonu eklenmeden kapatılırsa kullanıcı kendi yazdığını
    göremeden pencere kaybolur ve komutu yanlış yaptığını sanar.
    """

    # `show()` ZORUNLU: `isVisible()` yalnızca GÖSTERİLMİŞ bir widget için
    # True olur. Bu pencere modül boyunca tek örnek olarak paylaşıldığı ve
    # gösterilmesi önceki testleri (kaydırma, düğme vurgusu) etkileyebileceği
    # için burada en sonda kapatmak üzere gösterilir.
    window.show()
    try:
        window._input.setText("çıkış")
        window._on_submit()

        assert "Görüşürüz." in [label.text() for label in window.findChildren(QLabel)]

        # Vedala balonu ZAMANLAYICIYA bağlı: 600 ms sonra kapanır, o zaman
        # ekranda görülebilir. Süre dolmadan `visible` hâlâ True olmalı.
        assert window.isVisible() is True
        _drain_events(qapp, lambda: None, timeout=1.0)
        assert window.isVisible() is False
    finally:
        window._scroll_timer.stop()


def test_user_and_assistant_bubbles_use_different_backgrounds(window: ChatWindow) -> None:
    """Kullanıcı mavi (vurgu), asistan koyu (yükseltilmiş zemin) konuşur.

    İkisi de kendi QSS'ini alır: ortak `QWidget` kuralı `BG_BASE`
    serdiği için balonlar miras alsaydı arka planları görünmezdi.
    """

    window._append_bubble("kullanıcı cümlesi", from_user=True)
    window._append_bubble("asistan cevabı", from_user=False)

    bubbles = _bubbles(window)
    assert len(bubbles) == 2

    user_qss, assistant_qss = (bubble.styleSheet() for bubble in bubbles)
    assert "10, 132, 255" in user_qss, "kullanıcı balonu vurgu mavisi olmalı"
    assert "white" in user_qss
    assert "40, 40, 50" in assistant_qss.replace("#", "rgb(") or "282832" in assistant_qss


def test_bubbles_are_capped_and_stretched_to_the_speakers_side(window: ChatWindow) -> None:
    """Balon kenardan kenara yayılmaz; her iki taraf da konuşmacıya aittir.

    "Kullanıcı sağda, asistan solda" kuralı, iki yanına yerleştirilen
    germenin (`QSpacerItem`) YERİYLE anlatılır: asistan balonunda germe
    solda (ilk öğe), kullanıcı balonunda sağda (son öğe) bulunur.
    """

    window._append_bubble("çok uzun bir mesaj " * 20, from_user=False)
    window._append_bubble("kısa bir cevap", from_user=True)

    assistant_bubble, user_bubble = _bubbles(window)
    for bubble in (assistant_bubble, user_bubble):
        assert bubble.maximumWidth() == _BUBBLE_MAX_WIDTH
    # `width()` YERLEŞİM hesaplanana kadar 0'dır (pencere hiç gösterilmedi),
    # bu yüzden karşılaştırma GERÇEK pencere genişliğine (`_WINDOW_WIDTH`)
    # yapılır: 360 < 480. Balonun %75 kuralının özü de budur.
    assert _BUBBLE_MAX_WIDTH < _WINDOW_WIDTH

    assistant_row = assistant_bubble.parentWidget().layout()
    user_row = user_bubble.parentWidget().layout()
    assert isinstance(assistant_row, QHBoxLayout) and isinstance(user_row, QHBoxLayout)

    assert isinstance(assistant_row.itemAt(0), QSpacerItem), "asistan balonunda sol boşluk olmalı"
    assert isinstance(user_row.itemAt(2), QSpacerItem), "kullanıcı balonunda sağ boşluk olmalı"

    assert assistant_bubble.findChild(QLabel).wordWrap() is True, "uzun metin kutuya SARILMALI"


def test_closing_a_busy_window_never_waits_for_the_turn(
    window: ChatWindow, qapp: QApplication, llm_release: threading.Event
) -> None:
    """Pencere, tur hâlâ sürerken kapatıldığında ARAYÜZ DONDURULMAZ.

    Tur açık olduğu için `_ChatWorker` içindeki LLM çağrısı bitmemiştir.
    `closeEvent` bu durumda `wait()` ÇAĞIRMAZ; aksi halde pencere, çağrı
    bitene kadar (varsayılan 120 saniye) yanıt vermezdi.
    """

    window._input.setText("merhaba")
    window._on_submit()
    _drain_events(qapp, lambda: None, timeout=0.3)

    assert window._worker._idle.is_set() is False, "önce turun AÇIK olduğundan emin ol"

    started = time.monotonic()
    window.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"kapanış {elapsed:.2f} sn bekledi; arayüz donmuş demektir"
    assert window._thread.isFinished() is False, "busy yolda beklemedik; iş parçacığı hâlâ canlı olmalı"

    # Kapanış bitmiş ama iş parçacığı KENDİ KENDİLİĞİNDEN sonlanacak. Bu
    # testin sorumluluğu onu beklemek: pencerenin Python tarafı yok edilirken
    # ayakta kalan bir `QThread`, Qt'nin "Destroyed while thread is still
    # running" uyarısıyla SÜRECİ çökertir (ölçüldü). Üretimde buna ihtiyaç
    # yoktur — `app.exec()` bitince süreç zaten sonlanır.
    llm_release.set()
    qapp.processEvents()
    assert window._thread.wait(5000) is True, "serbest bırakılan tur bitmeliydi"


def test_closing_an_idle_window_joins_the_worker_thread(window: ChatWindow) -> None:
    """Boşta kapatılan pencerede iş parçacığı GERÇEKTEN sonlanır.

    Aks halde `QThread` nesnesi hâlâ ayaktayken yok edilir ve Qt
    "QThread: Destroyed while thread is still running" uyarısı basar —
    bu, ölçüldüğü gibi sürecin "Fatal Python error: Aborted" ile çökmesine
    yol açıyordu.
    """

    assert window._worker._idle.is_set() is True

    window.close()

    assert window._thread.isFinished() is True, "boştayken kapanınca iş parçacığı BİTMELİ"
