"""Playwright destekli, GERÇEK DOM-seviyesi tarayıcı otomasyonu MCP sunucusu.

`plugins/browser_plugin.py` neden yetmiyor: o plugin işletim sistemi
seviyesinde klavye kısayolları (`pyautogui.hotkey`) gönderir, yani sayfanın
DOM'unu GÖRMEZ. Bir butonu "sağ üstteki mavi olan" gibi tarif ederek
tıklatmak kırılgan bir konum bulmacasıdır. O dosyanın modül dokümantasyonu bu
boşluğu açıkça bırakmıştı: "Sayfa içeriğini okumak, DOM'a erişmek ya da
belirli bir sayfa öğesine tıklamak gibi gerçek bir protokol istemcisi gerektiren
ihtiyaçlar `plugins/mcp_plugin.py` kapsamındadır" — ve MCP köprüsü o
kapsamda bir taşıma katmanından ibaretti. İÇERİK boştu; bu dosya onu doldurur.

NEDEN ÜÇÜNCÜ TARAF BİR NPM SUNUCUSU DEĞİL: `@playwright/mcp` gibi
resmî paketler ağdan indirilir, sürümleri kayar ve "ne yapıyor"u bu deponun
kaynak kodunda görünmez. Buradaki sunucu ise (a) çalışma zamanında ağa
çıkmaz, (b) tamamen bu depoda okunabilir, (c) testte deterministiktir
(`file://` üzerinden statik bir sayfa).

TEK TOOL, TEK OTURUM — MİMARİ KISIT (asıl nedeni bu):
`plugins/mcp_plugin.py` modül dokümantasyonunun son paragrafı şunu söyler:
"HER TOOL ÇAĞRISI TAZE BİR BAĞLANTI AÇAR ... iki ayrı tool çağrısı arasında
KALICI bir MCP oturumu/süreç TUTULAMAZ." Bunun tasarımdaki karşılığı şudur:
tek bir tool, tek bir çağrıda TÜM adımları (`git` + `tıkla` + `yaz` + `oku`)
AYNI Playwright oturumu içinde sırayla yürütür. "Önce sayfaya git" ve
"sonra bir düğmeye tıkla" diye İKİ AYRI Artemis tool'u yazmak bir tuzaktır:
ikinci çağrı bambaşka bir tarayıcı süreci başlatır, DOM durumu (doldurulmuş
form, gizli öğeler, oturum çerezleri) kaybolur ve ikinci adım çoğu gerçek
sitede hiçbir şey bulamaz. Bu yüzden `steps` TEK bir liste argümanıdır.

SENKRON API NEDEN GÜVENLİ: `playwright.sync_api`, içinde çalıştığı thread'de
bir asyncio event loop YOKTMASINI ister; MCP sunucu süreci ise baştan sona
asyncio döngüsünde yaşar. İkisinin çarpışmamasının ölçülmüş cevabı: MCP SDK'sı
senkron tool fonksiyonlarını `anyio.to_thread.run_sync` ile AYRI bir worker
thread'ine taşır (`mcp/server/mcpserver/resolve.py`). Yani bu fonksiyon
iş parçacığı dışında, döngüsüz, tertemiz bir thread'de çalışır ve
`sync_playwright()` burada güvenle kullanılabilir. Bu, sunucunun senkron
API'yi seçmesinin nedenidir; async API seçilseydi aynı döngüde
`asyncio.run()`'a girmek imkânsız olurdu.

İSTİSNA FIRLATMA DİYE KURULDU — `CLAUDE.md`'deki "koşulsuz `success=True`
yasak" ilkesinin somut karşılığı: bir adım bulunamaz/timeout olursa MCP
tool çağrısını bir istisnayla patlatmak, kullanıcıya elinde `partial_results`
olmayan jenerik bir hata mesajı bırakır (bkz. `tests/test_mcp_plugin.py::
test_mcp_tool_exception_becomes_a_failed_tool_result_not_a_crash` — mcp 2.0
sunucu tarafı istisna metinlerini bile iletmiyor). Bu yüzden HATA bir
DICT olarak döner: `{"success": false, "failed_step": i, "error": ...,
"partial_results": {...}}`. `success: true` yalnızca tüm adımlar bittiğinde
yazılır.

KURULUM: `pip install -r requirements.txt` yalnızca Python paketini getirir;
Chromium'un kendisi ayrıca indirilmelidir:

    python -m playwright install chromium

Bu adım çalıştırılmamışsa `run_browser_task` istisna fırlatmaz, yine de
anlamlı bir mesajla `{"success": false, "error": ...}` döner — aynı
ilke.
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from pydantic import BaseModel, Field

# `mcp` 2.0'da `FastMCP` -> `MCPServer` olarak yeniden adlandırıldı. İkisini
# de deniyoruz: bu depo `mcp>=1.20` diyor ve üst sınırı yok, kullanıcının
# ortamındaki sürümü kontrol edemiyoruz. `tests/fixtures/mcp_echo_server.py`
# ile BİREBİR aynı desen — sürüm farkı yüzünden kırılan bir sunucu, Artemis'i
# değil, sadece kendi ortamını ilgilendirir.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server

_DEFAULT_TIMEOUT_MS = 10_000
"""Bir adımın selector'ı için tanınan azami bekleme (ms)."""

_NAVIGATION_TIMEOUT_MS = 30_000
"""`page.goto` için üst sınır (ms). Yavaş/sonsuza kadar yüklenen sayfaların
MCP çağrısını sonsuza kadar asılı bırakmaması için; `MCPServerConfig.
timeout_seconds` yine de ikinci bir güvenlik ağıdır."""


class Step(BaseModel):
    """Tek bir tarayıcı adımı.

    NEDEN PYDANTIC MODELİ, çıplak `dict` DEĞİL: `list[dict]` parametresinden
    MCP SDK'sı yalnızca `{"type": "array", "items": {"type": "object"}}`
    üretir — `action` alanının hangi değerleri alabildiği modele HİÇ
    gösterilmez. LLM'in `steps` içeriğini doğru üretmesi bu alanların şemada
    görünmesine bağlıdır. Model olunca SDK (a) `action` seçeneklerini
    şemaya yazar, (b) `selector`'ı zorunlu işaretler ve (c) gelen JSON'u
    SUNUCU TARAFINDA doğrular. Elle yazılmış şema ise yalnızca (a)'yı
    yapabiliyordu.

    Alan adları bilinçli olarak `action`/`selector`/`value`/`timeout_ms`:
    MCP sözleşmesi zaten `snake_case` konuşuyor, ayrıca bir eşleme katmanı
    kasten yok.
    """

    action: Literal["click", "fill", "read_text", "wait_for"] = Field(
        description=(
            "click: selector'a tıklar. fill: selector'a value yazar. "
            "read_text: selector'ın metnini sonuca ekler. "
            "wait_for: selector görünene kadar bekler."
        )
    )
    selector: str = Field(
        description=(
            "Playwright seçicisi — CSS, 'text=...', 'role=...' gibi Playwright'ın "
            "kendi seçici dili (yeniden icat edilmez)."
        )
    )
    value: str | None = Field(default=None, description="Yalnızca 'fill' için yazılacak metin.")
    timeout_ms: int | None = Field(default=None, description="Bu adımın bekleme üst sınırı (ms).")

    def timeout(self) -> int:
        """Adımın bekleme üst sınırı: verilmişse kendisi, yoksa varsayılan."""

        return max(1, self.timeout_ms) if self.timeout_ms else _DEFAULT_TIMEOUT_MS


server = _Server("browser-automation")


@server.tool()
def run_browser_task(url: str, steps: list[Step]) -> dict[str, Any]:
    """Tek bir tarayıcı oturumunda çok adımlı DOM otomasyonu yapar.

    TÜM adımlar TEK çağrıda, TEK oturumda çalışır. "Önce git" ve "sonra
    tıkla" diye iki ayrı tool çağrısı YAPILAMAZ: her tool çağrısı taze bir
    tarayıcı süreci başlatır ve aradaki DOM durumu kaybolur (bkz.
    `plugins/mcp_plugin.py` modül dokümantasyonu).

    Args:
        url: Açılacak adres (`https://...` ya da `file:///...`).
        steps: Sırayla uygulanacak adımlar. Her adım `{"action": ...,
            "selector": ..., "value": ..., "timeout_ms": ...}` olabilir;
            `action` şunlardan biridir:
              - `click`: `selector`'a tıklar.
              - `fill`: `selector`'a `value` yazar (yoksa boş string).
              - `read_text`: `selector`'ın metnini `read_texts`'a ekler.
              - `wait_for`: `selector` görünene kadar bekler.

    Returns:
        Başarıda `{"success": True, "title", "url", "read_texts", "steps_executed"}`.
        Adım hatasında `{"success": False, "failed_step": i, "error": str,
        "partial_results": {...}}` — istisna fırlatmaz.

        Dönüş imzası BİLEREK `dict[str, Any]` ve çıplak `dict` DEĞİL:
        MCP SDK'sı yapılandırılmış çıktıyı ancak dönüş tipinden bir çıktı
        modeli türetebiliyorsa üretir (`output_schema`); çıplak `dict` bu
        türetmeyi atlar ve `structured_content` `None` kalır. Artemis'te
        bunun somut sonucu vardır: `ToolResult.data` boş gelir ve
        planner bu tool'un çıktısına `{{step_N.alan}}` ile zincirleyemez
        (bkz. `plugins/mcp_plugin.py` modül dokümantasyonu, planner
        zincirleme). Yani bu imza, sunucunun LLM'e sadece metin değil
        düz bir veri sözlüğü de sunmasını sağlar.
    """

    return _run_browser_task(url, steps)


def _run_browser_task(url: str, steps: list[Step]) -> dict[str, Any]:
    """`run_browser_task`'ın gerçek gövdesi.

    Doğrulama burada YOK: `Step` bir pydantic modeli olduğu için SDK, tool'u
    çağırmadan önce gelen JSON'u sunucu tarafında doğruluyor (bilinmeyen
    `action`, eksik `selector` vb. gövdeye hiç ulaşmıyor).
    """

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    read_texts: dict[str, str] = {}

    try:
        _p = sync_playwright().start()
    except PlaywrightError as exc:
        # `playwright` paketi kurulu değil ya da sürüm uyuşmuyor.
        return _browser_unavailable(exc)

    try:
        browser = _p.chromium.launch(headless=True)
    except PlaywrightError as exc:
        # Chromium ikilisi kurulu değil (`playwright install chromium`
        # çalıştırılmamış) en sık bu yola düşer. Ham traceback yerine
        # ne yapılması gerektiğini söyleyen bir mesaj.
        _p.stop()
        return _browser_unavailable(exc)

    try:
        page = browser.new_page()
        page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        try:
            page.goto(url, timeout=_NAVIGATION_TIMEOUT_MS, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            # Sayfa hiç açılamadı: `steps` çalıştırılmadan biter. Yine de
            # "başarısız" demenin yanında NE OLDUĞUNU söylüyoruz; `goto`
            # hatasını da `_browser_unavailable`'a yollamak yanlış olurdu
            # (o mesaj "kurulum eksik" diyor, burada ise kurulum tam).
            return {
                "success": False,
                "failed_step": None,
                "error": f"Sayfa açılamadı ({url}): {exc}",
                "partial_results": {},
            }

        for index, step in enumerate(steps):
            try:
                _apply_step(page, step, read_texts)
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                # Playwright'ın iki hata sınıfı da YAKALANIR: `click`'in
                # zaman aşımı `TimeoutError`, geçersiz seçici sözdizimi
                # ise `Error`'dür. İkisi de bu sunucunun "adım başarısız"
                # durumudur; `read_texts` o ana kadarki olgunluğuyla
                # döner, böylece uzun bir akışın ilk adımları kaybolmaz.
                return {
                    "success": False,
                    "failed_step": index,
                    "error": f"{step.action} adımı ({step.selector}) başarısız: {exc}",
                    "partial_results": dict(read_texts),
                    "title": page.title(),
                    "url": page.url,
                }

        return {
            "success": True,
            "title": page.title(),
            "url": page.url,
            "read_texts": dict(read_texts),
            "steps_executed": len(steps),
        }
    finally:
        # Tarayıcı HATA DURUMUNDA da kapatılır. Kapatılmazsa her
        # başarısız çağrı RAM'de bıraktığı bir chromium süreci biriktirir —
        # "başarısız oldu" demek, "iz bırakma" demek değildir.
        browser.close()
        _p.stop()


def _apply_step(page: Any, step: Step, read_texts: dict[str, str]) -> None:
    """Tek bir adımı sayfaya uygular.

    `else` dalı YOK: `Step.action` bir `Literal`, yani dört değer dışında
    hiçbir şey bu fonksiyona ulaşamaz. Buradaki her eşleşme doğrudan
    Playwright'ın karşılığıdır; seçici dili Playwright'ın kendisidir
    (CSS, `text=...`, `role=...`), burada yeniden yorumlanmaz.
    """

    timeout = step.timeout()

    if step.action == "click":
        page.click(step.selector, timeout=timeout)
    elif step.action == "fill":
        page.fill(step.selector, step.value or "", timeout=timeout)
    elif step.action == "read_text":
        read_texts[step.selector] = page.inner_text(step.selector, timeout=timeout)
    else:  # wait_for
        page.wait_for_selector(step.selector, timeout=timeout)


def _browser_unavailable(exc: Exception) -> dict[str, Any]:
    """Chromium kurulu değilken/başlatılamadığında dönecek dürüst hata.

    Playwright'ın "Executable doesn't exist ... Looks like Playwright was just
    installed or updated" hatası, kurulum adını zaten söyler; burada
    yalnızca bu sunucunun hangi komutu beklediğini netleştiriyoruz ve
    ham traceback'i (yüzlerce satır) kullanıcıya/üst katmana boşaltmıyoruz.
    """

    return {
        "success": False,
        "error": (
            "Tarayıcı başlatılamadı. Büyük ihtimalle Chromium ikilisi kurulu "
            "değil; bu sunucuyu ilk kez kuruyorsanız "
            "`python -m playwright install chromium` komutunu bir kez "
            f"çalıştırın. Orijinal hata: {exc}"
        ),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # ELLE DENEME YOLU: sunucuyu stdio üzerinden değil, doğrudan çağırma.
        # Resmi girdi noktası stdio'dur (`server.run()`); bu bayrak yalnızca
        # `python -m mcp_servers.browser_automation_server --selftest <url>`
        # ile hızlı bir kontrol için vardır.
        print(_run_browser_task(sys.argv[2], [Step(action="read_text", selector="#status")]))
    else:
        server.run()
