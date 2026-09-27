"""Artemis'in kalıcı sohbet penceresi — `--chat` yolunun arayüzlü hali.

NEDEN AYRI BİR PENCERE: terminal çıktısı (`core/conversation_loop.py`)
bir ARAYÜZ değil, bir hata ayıklama yüzeyidir — "Artemis: [1/2] ..." gibi
satırlar ekranda kayar ve geçer. Bu pencere aynı döngünün GÖRÜNÜR hali:
kullanıcı yazdığını gerçek bir balon olarak görür, Artemis'in cevabı
balonlar hâlinde birikir, pencere kapatılana kadar yukarıda kalır.

MOTOR AYRI, PENCERE SUNUM: burada hiçbir karar mantığı yok. Aynı
`ToolDispatcher` + `TaskPlanner` + `LLMClient` sözleşmesi çalışır, tek
fark terminal I/O'su yerine Qt widget'ları vardır. Bu yüzden yeni bir
tool eklendiğinde (örn. `memory.*`) burada HİÇBİR şey değişmez — akış
zaten `core/conversation_loop.py` ile birebir aynıdır ve bir tool'un tek
satırlık raporu iki arayüzde de aynı biçimlendirilir.

GÖRÜNTÜ DİLİ: her renk, font ve QSS kuralı `ui/theme.py`'den gelir.
Burası kendi paletini `icat etmez` — üç ayrı pencerenin üç ayrı
paletiyle açılması, uygulamanın "AI yapmış gibi" görünmesinin en
kısa yoludur (bkz. `ui/theme.py` modül dokümantasyonu).

İŞ PARÇACIĞI (THREAD) NOTU:
    Qt'de widget'lara YALNIZCA ana (GUI) iş parçacığından dokunulabilir;
    LLM'e giden `get_tool_calls` çağrısı ise saniyeler sürebilen bir
    AĞ işlemidir. Bu yüzden tüm döngü, `ui/overlay.py`'deki desenin
    SENKRON dönüş değeri gerektiren hali olarak `_ChatWorker` adlı
    ayrı bir `QThread`'de yaşar: iş parçacığı sinyallerle talep alır,
    sonucu yine sinyalle geri bildirir.

    Onay köprüsü (`_ChatWorker._confirm`) bu dosyanın en kolay
    ÇÖZÜLEMEZ görünen parçasıdır; neden kilitlenmediği `_confirm`
    docstring'inde AÇIKÇA yazılıdır.

Tek başına önizleme (Ollama/tool çalıştırmadan, yalnızca görünüm için):

    python -m ui.chat_window
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.conversation_loop import _EXIT_COMMANDS
from core.dispatcher import ToolDispatcher
from core.llm_client import LLMResponseParseError
from core.planner import TaskPlanner
from core.prompt_builder import build_system_prompt
from ui import theme
from utils.confirmation import format_confirmation_arguments
from utils.text import lower_variants

logger = logging.getLogger(__name__)

_WINDOW_WIDTH = 480
_WINDOW_HEIGHT = 640
_BUBBLE_WIDTH_RATIO = 0.75
"""Balonun, kaydırma alanının genişliğine oranı. Tam genişlikte bir balon
ekranın iki kenarına dayanır ve kimse onun bir KONUŞMA olduğunu
anlamaz; %75 hem mesajın okunmasını hem de tarafların (kullanıcı sağda,
asistan solda) görünmesini korur."""

_BUBBLE_MAX_WIDTH = int(_WINDOW_WIDTH * _BUBBLE_WIDTH_RATIO)
"""TEK SEFERLİK hesap (v1). Pencere yeniden boyutlandırıldığında
GÜNCELLENMEZ: uzun bir sohbette kullanıcı pencereyi büyüttüğünde
balonlar biraz dar kalır. Yeniden hesaplamak bir `resizeEvent` ve
"bütün balonları gez" maliyeti ister; gerçek kullanımda mesajlar zaten
bu genişlikte sarıldığı için v1'de bilinçli olarak atlanıyor."""

_EXIT_FAREWELL_MS = 600
"""Vedala balonunun ekranda kalacağı süre. `close()` anında çağrılırsa
kullanıcı "Görüşürüz." yazısını hiç göremez — pencere bir anda kaybolur
ve komutu yanlış yaptığını sanar."""

_CLOSE_GRACE_MS = 200
"""`close()` ile tur bitişinin ARASI yarışa bırakılan süre.

Yarış şu: kullanıcı cevabı görüp pencereyi kapatır, ama iş parçacığı
`event.set()`'ten sonra `_idle.set()`'e kadar birkaç satır daha
çalışmaktadır. `close()` tam o aralığa denk gelirse pencere turu hâlâ
AÇIK görür, beklemez ve pencerenin Python tarafı yok edilirken iş
parçacığı hâlâ ayakta kalır — Qt bunu "QThread: Destroyed while thread is
still running" diye bağırır ve süreç "Fatal Python error: Aborted" ile
çöker (bu sırada ölçüldü).

Kısa bir bekleme yarışı kapatır. Tur GERÇEKTEN sürüyorsa bekleme 200
ms'de KESİLİR: kapanan bir pencerede 200 ms, fark edilmez; buna karşılık
donduran `wait()` ölçüldüğü gibi tam çağrı süresi kadar (3 saniyelik bir
çağrıda arayüz tam 3 saniye donuyordu) sürebiliyor."""

# --- Balon QSS'leri --------------------------------------------------------
#
# NEDEN `theme.stylesheet()`'in KENDİ KURALLARI DEĞİL: ortak QSS'teki
# `QWidget` kuralı `BG_BASE` zeminini her yere serer; bir `QFrame` bunu
# miras alırsa balon koyu zeminde kalır ve "mavi balon" hiç oluşmaz.
# Bu yüzden balonlar KENDİ QSS'lerini alır — palet yine `theme`'den
# gelir, sadece seçim yer içinde yapılır.
#
# `QLabel { background: transparent }` kuralı atlanamaz: içteki etiket de
# üstteki `QWidget` kuralını miras alır ve mavi balonun ortasına koyu bir
# dikdörtgen çizer. Daha yakın bir atas (bu `QFrame`) QSS'i kazanır.

_USER_BUBBLE_QSS = f"""
QFrame {{
    background-color: rgba({theme.ACCENT_BLUE.red()}, {theme.ACCENT_BLUE.green()}, {theme.ACCENT_BLUE.blue()}, 0.85);
    border-radius: 12px;
}}
QLabel {{
    background: transparent;
    color: white;
    padding: 8px 12px;
}}
"""

_ASSISTANT_BUBBLE_QSS = f"""
QFrame {{
    background-color: {theme.BG_ELEVATED.name()};
    border-radius: 12px;
}}
QLabel {{
    background: transparent;
    color: {theme.TEXT_PRIMARY.name()};
    padding: 8px 12px;
}}
"""


def is_exit_command(text: str) -> bool:
    """Kullanıcının pencereyi kapatmak istediğini söyleyip söylemediğini belirler.

    Neden `core/conversation_loop.py::_EXIT_COMMANDS`'ı IMPORT ediyoruz,
    kendimiz yazmıyoruz: bu liste iki yerde de birebir aynı olmalı. Bir
    dizede `{"quit"}` eklenip diğerinde eklenmezse terminalden çıkılır,
    pencereden çıkılamaz — sessiz, can sıkıcı bir tutarsızlık. Kural da
    aynı: `lower_variants` (`str.lower()` Türkçe kurallarını uygulamaz,
    `"ÇIKIŞ".lower()` -> `"çikiş"`, listede yok).

    Args:
        text: Kullanıcının yazdığı ham metin.

    Returns:
    """
    return bool(lower_variants(text) & _EXIT_COMMANDS)


@dataclass
class _ConfirmationRequest:
    """Çalışan iş parçacığı ile onay diyaloğu arasındaki tek yönlü elçi.

    Neden iki ayrı sinyal argümanı değil de tek bir nesne: `event` (bekle)
    ile `result` (cevap) aynı isteğe ait ve daima birlikte taşınıyorlar,
    ikisi ayrı argüman olsaydı alıcı slot iki argümanı eşleştirmeyi
    unutabilirdi.

    Attributes:
        event: İş parçacığı bunun üzerinde bekler; diyalog bittiğinde
            `set()` çağrılır.
        result: Tek elemanlı liste. Diyalog cevabı buraya yazılır —
            `bool` doğrudan döndürülemez, çünkü dönen değer Qt sinyalinin
            Python nesnesi olarak taşınmalı, referansı korunmalıdır.
    """

    event: threading.Event = field(default_factory=threading.Event)
    result: list[bool] = field(default_factory=lambda: [False])


class _ChatWorker(QObject):
    """LLM'e giden ve tool'ları çalıştıran döngü — GUI iş parçacığı DIŞINDA.

    Neden `QObject` ve düz bir `threading.Thread` değil: `pyqtSignal`
    sadece `QObject` üzerinde tanımlıdır ve `QThread`'in yasak döngüsü
    (`exec`) bize ait bir iş döngüsü kurmadan boş bir mesaj döngüsü
    sağlar; `quit()` onu durdurur, `wait()` çıkmasını bekler.

    Bu sınıf HİÇBİR widget'a dokunmaz. Tek istisna `pyqtSignal` ile
    yayınladığı verilerdir; onlar da Qt tarafından GUI iş parçacığına
    kuyruklanır.
    """

    busy_changed = pyqtSignal(bool)
    """Bir tur başladı/bitti.

    Neden ayrı bir sinyal: `_ChatWorker`'ın kullanıcı DÜĞMELERİ için
    (`_set_busy`) bir "meşgul" bilgisi zaten üretiyor — aynı bilgi
    kapanışta da kullanılır. `_idle` varlık belirteci bu sinyalin
    KENDİSİ değil, iş parçacığından okunabilen ve bloklamayan karşılığıdır:
    sinyal GUI iş parçacığına KUYRUKLANIR (yani `closeEvent` anında henüz
    teslim edilmemiş olabilir), `threading.Event` ise anında okunur.
    """

    turn_finished = pyqtSignal(list)
    """Tur başarıyla bitti: (adım başına bir satır) listesi."""

    turn_failed = pyqtSignal(str)
    """Tur hata ile bitti: kullanıcıya gösterilecek TEK satırlık mesaj."""

    confirmation_requested = pyqtSignal(str, dict, object)
    """Onay gereken bir adım geldi: (tool adı, argümanlar, `_ConfirmationRequest`)."""

    def __init__(self, dispatcher: ToolDispatcher, llm_client: Any) -> None:
        super().__init__()

        # Neden `Any`: bu pencere HANGİ istemciyi alacağını bilmez —
        # yerel `OllamaLLMClient`, bulut `OpenRouterLLMClient` ya da her
        # çağrıda seçim yapan `core/llm_router.py::LLMRouter` olabilir.
        # Somut bir sınıfı import edip ona bağlamak, üçüncü bir sağlayıcı
        # eklendiğinde burayı kırar. Tek şart, `get_tool_calls` metodu.
        self._llm_client = llm_client

        # Varlık belirteci: `_handle_submit` başlarken temizlenir, her
        # dönüş yolunda (`try/finally`) kurulur. `threading.Event` bilinçli
        # seçildi: burada yalnızca "iş var / iş yok" lazım, sayaç değil.
        self._idle = threading.Event()
        self._idle.set()  # başlangıçta boştayız

        # Sistem promptu bir kez kurulur: `prompts/system_prompt.md`'yi
        # okumak + tool manifestini gömmek her turda gereksiz I/O'dır ve
        # (tool listeselliği) kullanıcının beklediği şey de değildir.
        self._system_prompt = build_system_prompt()

        self._planner = TaskPlanner(dispatcher, confirm_callback=self._confirm)

    # ------------------------------------------------------------------
    # İş parçacığı yuvaları (slot) — GUI iş parçacığından QUEUE ile gelir
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def _handle_submit(self, user_input: str) -> None:
        """Tek bir kullanıcı cümlesini işleyip sonucu sinyalle bildirir.

        `core/conversation_loop.py::run`'ın döngü gövdesinin BİREBİR
        karşılığıdır: aynı iki `except`, aynı mesajlar, aynı sıralama.
        Farkı yalnızca hedef: `print(...)` yerine sinyal.
        """

        self.busy_changed.emit(True)
        self._idle.clear()
        try:
            self._run_turn(user_input)
        finally:
            # Kilit ACILMALI: erken dönen her `return`/`except` yolunda
            # unutulursa bir sonraki tur "düğmeler kapalı" halde başlar ve
            # kullanıcı bir daha hiçbir şey gönderemez.
            self._idle.set()
            self.busy_changed.emit(False)

    def _run_turn(self, user_input: str) -> None:
        """`_handle_submit`'ın asıl işi — varlık belirteci olmadan.

        Ayrı bir metot, `_handle_submit`'ı "işaretle + çalıştır + işaretle"
        gibi okunabilir bırakır; `try/finally` bloğu her dönüş yolunu
        kapsar ama turun gövdesini boğmaz.
        """

        try:
            tool_calls = self._llm_client.get_tool_calls(self._system_prompt, user_input)
        except LLMResponseParseError as exc:
            logger.warning("LLM çıktısı ayrıştırılamadı: %s", exc)
            self.turn_failed.emit("Anlayamadım, farklı bir şekilde ifade eder misiniz?")
            return
        except ConnectionError as exc:
            logger.error("Ollama bağlantı hatası: %s", exc)
            self.turn_failed.emit(f"Yerel modele ulaşılamıyor ('ollama serve' çalışıyor mu?). Detay: {exc}")
            return

        step_results = self._planner.execute_plan(tool_calls)
        total = len(tool_calls)

        lines: list[str] = []
        for step in step_results:
            prefix = f"[{step.index}/{total}] " if total > 1 else ""
            # Terminalde satırın başında "Artemis: " vardı; burada balonun
            # KENDİSİ konuşmacıyı belli ettiği için o önek gereksiz.
            lines.append(f"{prefix}{step.result.message}")

        completed = len(step_results)
        if completed < total:
            skipped = total - completed
            lines.append(f"({skipped} adım, önceki bir adımın durdurulması nedeniyle çalıştırılmadı.)")

        self.turn_finished.emit(lines)

    # ------------------------------------------------------------------
    # Onay köprüsü
    # ------------------------------------------------------------------

    def _confirm(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """`TaskPlanner`'ın onay sorusunu GUI iş parçacığına taşır.

        NEDEN KİLİTLENMEZ — üç iş parçacığının tek döngüde nasıl durduğu:
            1) `TaskPlanner.execute_plan` `_ChatWorker`'ın kendi
               iş parçacığında çalışır; `event.wait()` O iş parçacığını
               durdurur, GUI iş parçacığını DEĞİL.
            2) GUI iş parçacığı boş kaldığı için Qt'nin olay döngüsü
               çalışmaya devam eder ve kuyruklanmış `confirmation_
               requested` sinyalini GUI yuvalarına TESLİM EDER.
            3) Diyalog bittiğinde yuva `event.set()` çağırır ve 1. adımdaki
               bekleme biter.
        Hiçbir adımda bir iş parçacığı KENDİNİ bekletmez; bekleyen
        tek şey iş parçacığı, bekleten tek şey GUI iş parçacığıdır.
        Tersi (GUI iş parçacığında beklemek) tam olarak donan pencere ve
        tıklanamayan onay kutusu olurdu.

        Bu yüzden `QMessageBox.exec()` de `_confirm` DEĞİL, yalnızca
        GUI iş parçacığındaki yuvada çağrılır.

        Args:
            tool_name: Onay gerektiren tool'ın adı.
            arguments: O tool'ın GERÇEK (referansları çözülmüş) argümanları.

        Returns:
            Kullanıcı onayladıysa True, reddettiyse ya da yanıt
            verilemezse False.
        """

        request = _ConfirmationRequest()
        self.confirmation_requested.emit(tool_name, arguments, request)
        request.event.wait()
        return request.result[0]


class ChatWindow(QWidget):
    """Kalıcı, normal pencereli sohbet arayüzü.

    Args:
        dispatcher: Tool'ları çalıştıracak hazır `ToolDispatcher`.
        llm_client: `get_tool_calls` metodu olan herhangi bir istemci
            (bkz. `_ChatWorker.__init__` docstring'i).

    Kullanım (`main.py` tarafından, bu dosyadan BAĞIMSIZ olarak):

        window = ChatWindow(dispatcher, llm_client)
        window.show()
    """

    submit_requested = pyqtSignal(str)
    """GUI iş parçacığından iş parçacığına "şunu işle" isteği.

    SINIF SEVİYESİNDE olmak zorunda: `pyqtSignal` bir sınıf niteliği
    olarak tanımlanır; `__init__` içinde `self.x = pyqtSignal(str)`
    yazılırsa PyQt6 onu bir sinyal DEĞİL, sıradan bir nesne sanar ve
    `.connect()` çağrısı `AttributeError` ile patlar.
    """

    def __init__(self, dispatcher: ToolDispatcher, llm_client: Any) -> None:
        super().__init__()

        # ÜST DÜZEYDE, TEK SEFER: QSS kalıtımı yukarı doğru çalışır, yani
        # çocuk widget'lar buradan gelen kuralları miras alır ve her birine
        # `setStyleSheet` çağırmak gerekmez. Balonlar bu kuralın İSTİSNASI
        # (bkz. modül içindeki `_USER_BUBBLE_QSS` yorumu).
        self.setStyleSheet(theme.stylesheet())

        self.setWindowTitle("Artemis")
        self.resize(_WINDOW_WIDTH, _WINDOW_HEIGHT)

        self._pending_confirmation: _ConfirmationRequest | None = None
        self._thread_stopped = False

        # `QTimer.singleShot(0, self._scroll_to_bottom_now)` STATİK bir
        # çağrıdır: görevi, bağlandığı nesneye DEĞİL yalnızca Python
        # referansına tutunur. Bu yüzden pencere kapatıldıktan sonra da
        # tetiklenir ve `self._scroll.verticalScrollBar()` artık YOK EDİLMİŞ
        # bir C++ nesnesine erişir — pencerenin kapanışından saniyeler sonra
        # Qt'nin "internal C++ object already deleted" uyarısı ve çökme.
        # Tek bir ÜYE `QTimer` bu sorunu yapısal olarak bitirir: pencere
        # onun da sahibidir, pencereyle birlikte yok edilir ve görev
        # pencere daha varken çalışır (bkz. `_scroll_to_bottom`).
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom_now)

        self._worker = _ChatWorker(dispatcher, llm_client)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        # GUI → iş parçacığı isteği ve iş parçacığı → GUI sonucu.
        # İkisi de farklı iş parçacıkları arasında olduğu için Qt
        # otomatik olarak QUEUED bağlantı kurar; ek bir altyapı gerekmez.
        self.submit_requested.connect(self._worker._handle_submit)
        self._worker.busy_changed.connect(self._set_busy)
        self._worker.turn_finished.connect(self._on_turn_finished)
        self._worker.turn_failed.connect(self._on_turn_failed)
        self._worker.confirmation_requested.connect(self._on_confirmation_requested)

        self._build_ui()

        self._thread.start()

    # ------------------------------------------------------------------
    # Arayüz kurulumu
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        root.addWidget(self._build_title())
        root.addWidget(self._build_message_area(), stretch=1)
        root.addLayout(self._build_input_row())

    def _build_title(self) -> QWidget:
        title = QLabel("Artemis")
        title.setProperty("role", "title")
        self._apply_role(title)
        return title

    def _build_message_area(self) -> QScrollArea:
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._messages = QVBoxLayout(container)
        self._messages.setContentsMargins(0, 0, 0, 0)
        self._messages.setSpacing(8)
        # Sondaki germe (stretch): balonlar yukarıdan aşağı YIGILIR, aşağıya
        # yapışmaz. Kısa bir sohbette mesajlar tepede durur — sohbet
        # penceresinin bir mesaj listesi değil, bir konuşma olması gerektiği gibi.
        self._messages.addStretch(1)

        self._scroll.setWidget(container)
        return self._scroll

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Bir şey yazın… (çıkmak için: çıkış)")
        self._input.returnPressed.connect(self._on_submit)
        row.addWidget(self._input, stretch=1)

        self._send_button = QPushButton("Gönder")
        self._send_button.setProperty("role", "primary")
        self._apply_role(self._send_button)
        self._send_button.clicked.connect(self._on_submit)
        row.addWidget(self._send_button)

        return row

    @staticmethod
    def _apply_role(widget: QWidget) -> None:
        """Dinamik `role` özelliğini QSS'e tanıtır.

        Qt, `setProperty` sonrası widget'ın stilini KENDİSİNE yeniden
        sormaz; `theme.stylesheet()`'teki `QPushButton[role="primary"]`
        kuralı ancak `unpolish`/`polish` çağrısından sonra eşleşir.
        Atlanırsa düğme koyu kalır ve "vurgulu" olduğu hiç belli olmaz.
        """

        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ------------------------------------------------------------------
    # Balonlar
    # ------------------------------------------------------------------

    def _append_bubble(self, text: str, *, from_user: bool) -> None:
        """Mesaj listesine yeni bir balon ekler ve en alta kaydırır."""

        # Germe, HER eklemeden sonra yeniden ekleniyor: yeni balon en son
        # sırada dursun, germe yalnızca EN SONDA kalsın.
        self._messages.insertWidget(self._messages.count() - 1, self._build_bubble(text, from_user=from_user))
        self._scroll_to_bottom()

    def _build_bubble(self, text: str, *, from_user: bool) -> QWidget:
        """Tek bir mesaj balonu: yuvarlak `QFrame` + satır kaydıran `QLabel`."""

        bubble = QFrame()
        bubble.setStyleSheet(_USER_BUBBLE_QSS if from_user else _ASSISTANT_BUBBLE_QSS)
        bubble.setMaximumWidth(_BUBBLE_MAX_WIDTH)

        label = QLabel(text)
        label.setWordWrap(True)  # uzun mesaj kenar kenarına değil, kendi kutusuna sarılır
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(0, 0, 0, 0)
        bubble_layout.addWidget(label)

        # Hizalama, germe ile yapılır: kullanıcı sağda (sol boşluk),
        # asistan solda (sağ boşluk).
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(0 if from_user else 1)
        row.addWidget(bubble)
        row.addStretch(1 if from_user else 0)

        container = QWidget()
        container.setLayout(row)
        return container

    def _scroll_to_bottom(self) -> None:
        """En yeni balonu görünür kılar.

        Neden ZAMANLAYICI: yeni balonun YERLEŞİMİ henüz hesaplanmamıştır;
        kaydırma çubuğunun `maximum()` değeri eskidir ve balonun son satırı
        ekranın altında kalır. Bir olay döngüsü turundan sonra çağırmak,
        yerleşimin oturmasını bekler.

        Neden tek bir ÜYE `QTimer` ve neden `singleShot(0, ...)` DEĞİL:
        bkz. `__init__`'teki açıklama — statik `singleShot`, görevi
        bağlandığı pencereye değil yalnızca Python referansına tutundurur
        ve pencere kapanınca YOK EDİLMİŞ widget'a erişerek çöker.
        """

        self._scroll_timer.start(0)

    def _scroll_to_bottom_now(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------------
    # Kullanıcı etkileşimi
    # ------------------------------------------------------------------

    def _on_submit(self) -> None:
        """Enter'a basılmış ya da "Gönder" tıklanmış — tek giriş kapısı.

        İki yolun da aynı yuvaya bağlanması bilinçlidir: buton ile Enter
        arasındaki fark, tuşun `returnPressed` sinyalidir; mantık ikisinde de
        aynıdır.
        """

        user_input = self._input.text().strip()
        if not user_input:
            return  # boş satır bir komut değildir; `run()`'ın `continue` davranışı

        self._input.clear()

        if is_exit_command(user_input):
            self._append_bubble("Görüşürüz.", from_user=False)
            # Vedala yazdıktan SONRA kapat: aksi halde kullanıcı yazdığı
            # şeyi göremez.
            QTimer.singleShot(_EXIT_FAREWELL_MS, self.close)
            return

        self._append_bubble(user_input, from_user=True)
        self.submit_requested.emit(user_input)

    @pyqtSlot(bool)
    def _set_busy(self, busy: bool) -> None:
        """Bir tur sürerken giriş alanını ve düğmeyi kapatır.

        Neden ikisi birden: giriş alanı kapalıyken bile "Gönder" düğmesi
        tıklanabilir olsaydı, aynı cümle iki kez gönderilirdi. Sıralı iki
        turun tek bir iş parçacığında çalışması görünür bir kayma olurdu ve
        ikinci turun sonucu ilkinin balonları arasına karışırdı.

        Bu yuva, iş parçacığının `busy_changed` sinyaline bağlıdır ve
        `_on_submit` içinde DEĞİL çağrılır: `_on_submit` anında
        `_set_busy(True)` yapsaydı, tur BİTİP `busy_changed(False)`
        kuyruğu işlenmeden önce ikinci bir gönderim kabul edilirdi.
        Sıralamayı TEK bir kaynak belirler: iş parçacığının kendisi.
        """

        self._input.setEnabled(not busy)
        self._send_button.setEnabled(not busy)

    # ------------------------------------------------------------------
    # İş parçacığı → GUI yuvaları
    # ------------------------------------------------------------------

    @pyqtSlot(list)
    def _on_turn_finished(self, lines: list[str]) -> None:
        for line in lines:
            self._append_bubble(line, from_user=False)

    @pyqtSlot(str)
    def _on_turn_failed(self, message: str) -> None:
        # Hata da bir balon: sessizce yutulursa kullanıcı neden cevap
        # gelmediğini anlamaz ve pencere takılı sanır.
        self._append_bubble(message, from_user=False)

    @pyqtSlot(str, dict, object)
    def _on_confirmation_requested(
        self, tool_name: str, arguments: dict[str, Any], request: _ConfirmationRequest
    ) -> None:
        """Onay diyaloğunu gösterir ve cevabı iş parçacığına geri taşır.

        GUI iş parçacığında çalışır (sinyal kuyrukla buraya gelir), dolayısıyla
        `QMessageBox` çağırmak GÜVENLİDİR — asıl kilitlenme riski
        tam tersiydi: bu yuvayı iş parçacığında çalıştırmak.
        """

        self._pending_confirmation = request
        try:
            answer = self._ask_confirmation(tool_name, arguments)
        finally:
            # `exec()` beklenmedik bir şekilde patlarsa bile iş parçacığı
            # sonsuza kadar BEKLEMESİN; cevap yoksa güvenli olan reddetmektir.
            request.result[0] = answer
            request.event.set()
            self._pending_confirmation = None

    def _ask_confirmation(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Tema uyumlu modal onay kutusu; `True` yalnızca "Evet"e basılırsa.

        Neden `QMessageBox.question(...)` değil: o kısayol kutuya hiçbir QSS
        UYGULAMAZ ve ortadaki koyu pencerenin yanında Qt'nin varsayılan gri
        diyaloğu belirir. `setStyleSheet` burada çağrılmak zorunda.

        Argümanlar `format_confirmation_arguments` ile gösterilir: kullanıcı
        yalnızca tool adını görürse `filesystem.delete`'in hangi dosyayı
        sileceğini BİLEMEZ — kör onay güvenlik açığıdır (bkz. CLAUDE.md).
        """

        box = QMessageBox(self)
        box.setStyleSheet(theme.stylesheet())
        box.setWindowTitle("Onay gerekiyor")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"'{tool_name}' işlemi onay gerektiriyor.")
        box.setInformativeText(f"Argümanlar: {format_confirmation_arguments(arguments)}")

        evet = box.addButton("Evet", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Hayır", QMessageBox.ButtonRole.RejectRole)
        # Varsayılan "Evet" ama odak "Hayır" üzerinde değil: `Enter`'a
        # basıldığında yanlışlıkla geri alınamaz işlem çalışmasın.
        box.setDefaultButton(evet)

        box.exec()
        clicked = box.clickedButton()
        return clicked is not None and clicked is evet

    # ------------------------------------------------------------------
    # Kapanış
    # ------------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt'nin metot adı
        """Pencere kapanırken iş parçacığını da kapatır.

        Bu olmadan `_ChatWorker`'ın `QThread`'i pencereden sonra da yaşamaya
        devam eder; uygulama kapanırken "QThread: Destroyed while thread is
        still running" uyarısı ve düzensiz bir süreç sonu elde edilir.

        SIRA ÖNEMLİDİR: önce bekleyen onay isteği çözülür, SONRA iş
        parçacığı durdurulur. Ters sırada, `wait()` kendi çıkmasını
        beklediği iş parçacığını (`event.wait()`'te bekleyen) beklemezdi —
        pencere kapanmadan onay diyaloğu açıksa hep böyle bir kilitlenme
        mümkün olurdu.
        """

        # Pencere HEMEN kapanır; iş parçacığının bitmesi beklenmez (aşağıya
        # bkz. `_shutdown`). `closeEvent` GUI iş parçacığında çalışır ve
        # burada `wait()` çağırmak, o iş parçacığında süren bir tur varsa
        # arayüzü DONDURUR.
        event.accept()
        self._shutdown()

    def _shutdown(self) -> None:
        """İş parçacığını ve bekleyen onay isteğini kapatır (idempotent).

        `wait()` KOŞULLUDUR ve koşul `_ChatWorker._idle` varlık
        belirtecidir (bir tur çalışmıyorsa):

        * **Boştayken bekle.** `quit()` çağrısı boştaki mesaj döngüsünden
          çıkar ama OS iş parçacığı O AN bitmeyebilir; `wait()` bu
          belirsizliği ortadan kaldırır. Aks halde `QThread` nesnesi
          pencereyle birlikte yok edilirken hâlâ ayakta olur ve Qt
          "QThread: Destroyed while thread is still running" uyarısı
          basar — bu uyarı, çalışma sırasında gözlenen
          "Fatal Python error: Aborted" çökmesinin KÖK NEDENİYDİ.
        * **Tur sürerken BEKLEME.** İş parçacığı `get_tool_calls`
          çağrısındadır ve o çağrı `config.yaml::ollama_timeout_seconds`
          (varsayılan 120 s) boyunca sürebilir. `wait()` burada
          çağırsaydı pencere kapanmadan o kadar beklenirdi — ÖLÇÜLDÜ:
          3 saniyelik bir çağrıda arayüz tam 3 saniye dondu. O yol
          bilinçli olarak korunur: yalnızca `quit()` gönderilir, iş
          parçacığı boştaki döngüsüne döner ve kendiliğinden sonlanır.

        Sonuç olarak "iş parçacığı pencereden sonra da yaşar mı?" sorusunun
        cevabı bazen EVET'tir ve bu BİLEREK kabul edilmiştir: pencereyi
        kapatmak çalışan bir tool'u YARIM BIRAKMAK anlamına gelmez. `quit()`
        mevcut bir turu da kesmez — LLM'e giden istek iptal EDİLEMEZ,
        yalnızca cevabı teslim edilecek bir alıcı kalmaz (bağlantının
        kapatılması `llm_client`'ın işidir, bu pencerenin sorumluluğu
        değildir).
        """

        if self._thread_stopped:
            return

        # Pencerenin onay diyaloğu açıkken kapatılması bir köşe durumudur
        # (diyalog modal, ama başlık çubuğu hâlâ erişilebilir olabilir).
        # İstek varsa onu REDDEDEREK çözüyoruz: kullanıcı pencereyi
        # kapatmakla geri alınamaz işlemi de sessizce onaylamış olmaz.
        if self._pending_confirmation is not None:
            self._pending_confirmation.result[0] = False
            self._pending_confirmation.event.set()
            self._pending_confirmation = None

        # Kapatma sinyali her koşulda gider: döngüde hiçbir iş yoksa
        # `quit()` anında etkisini gösterir, iş varsa boşta olduğunda.
        self._thread.quit()

        # Yalnızca KISA ve BOŞTA olduğuna inandığımız iş parçacığını
        # bekleriz; iki koşul birden gerekir:
        #   1) döngü bitti/bir zamanlar hiç başlamadı -> `wait()` anında
        #      döner ve `QThread`'in "hâlâ ayaktayken yok edilmesi" uyarısı
        #      YOK edilir;
        #   2) döngü AZ ÖNCE bitti ama `_idle` bayrağı birkaç satır geç
        #      kurulacak -> kısa süre, bitim bayrağı BEKLENİR, sonra
        #      yine boştayız diye yeniden bakılır ve bitirilir.
        # Gerçekten süren bir tur varsa ikinci koşul da sağlanmaz ve
        # bekleme yapılmaz: `wait()` çağrısındaki süre sınırı, kapanan
        # pencerede fark edilmez.
        if self._worker._idle.is_set():
            self._thread.wait()
        elif self._worker._idle.wait(timeout=_CLOSE_GRACE_MS / 1000):
            self._thread.wait()

        self._thread_stopped = True


# --------------------------------------------------------------------------
# `python -m ui.chat_window` — yalnızca görünüm önizlemesi
# --------------------------------------------------------------------------


class _DemoLLMClient:
    """`main.py`'ye BAĞLANMADAN pencereyi göstermek için sahte beyin.

    Gerçek `TaskPlanner` ve gerçek `ToolDispatcher` çalışır; yalnızca
    `get_tool_calls` sahtedir. Böylece bu pencereyi `main.py`'ye bağlayacak
    kişi, Ollama kurmadan balonların, çok adımlı numaralandırmanın ve hata
    balonunun nasıl göründüğünü gerçek kod yolunda görebilir.

    Yan etki bilinçli olarak YOK: planlar yalnızca `assistant.reply`
    içerir. `filesystem.create_folder` gibi bir adım, bu önizlemenin
    geliştiricinin GERÇEK Masaüstüne klasör açmasına yol açardı — bir
    arayüz kontrolü için kabul edilemez.
    """

    def __init__(self) -> None:
        self._index = 0

    def get_tool_calls(self, system_prompt: str, user_input: str) -> list[dict[str, Any]]:
        self._index += 1
        if self._index == 1:
            return [{"tool": "assistant.reply", "arguments": {"message": f'"{user_input}" aldım, ne yapayım?'}}]
        if self._index == 2:
            return [
                {"tool": "filesystem.search", "arguments": {"pattern": "*.txt"}},
                {"tool": "assistant.reply", "arguments": {"message": "Masaüstündeki metin dosyaları listelendi."}},
            ]
        raise LLMResponseParseError("demo: model ayrıştırılamadı")


def _run_demo() -> None:
    """`python -m ui.chat_window`: arayüzü canlı gösterir, hiçbir tool çalıştırmaz."""

    from config.settings import get_settings
    from core.plugin_loader import load_plugins

    app = QApplication(sys.argv)
    load_plugins()
    dispatcher = ToolDispatcher(settings=get_settings())

    window = ChatWindow(dispatcher, _DemoLLMClient())
    window.show()

    print("Artemis sohbet penceresi önizlemesi. Bir şey yazıp Enter'a basın; kapatmak için 'çıkış'.")
    sys.exit(app.exec())


if __name__ == "__main__":
    _run_demo()
