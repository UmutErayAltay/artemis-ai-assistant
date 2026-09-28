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
kaynak kodunda görünmez. Buradaki sunucu ise (a) tamamen bu depoda
okunabilir, (b) üçüncü taraf bir süreç/internet bağımlılığı getirmez,
(c) testte deterministiktir.

DİKKAT — "ÇALIŞMA ZAMANINDA AĞA ÇIKMAZ" YANLIŞ BİR İDİA DIYYDI (v3.7
güvenlik düzeltmesi): `run_browser_task` bir `url` alır ve `page.goto`
O ADRESE GİDER. Verilen adres `http(s)://` ise gerçekten internete çıkar.
Bu sunucunun TESTLERİ ağ kullanmaz (statik sayfa) — ama bu bir kod
özelliği değil, test seçimidir. Gerçek koruma `_validate_url`'dur:
varsayılan olarak yalnızca `http`/`https` şemaları ve loopback/private/
link-local OLMAYAN (yani genel) host'lara izin verilir. `file://` ile
`~/.ssh/id_rsa` okumak ölçülmüş bir açıktı ve bu sürümde reddedilir.

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

import ipaddress
import os
import socket
import sys
from typing import Any, Literal
from urllib.parse import urlsplit

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
timeout_seconds` yine de ikinci bir güvenlik ağıdır.

ÖNEMLİ: `timeout_seconds` BUNDAN KÜÇÜKse üst katman (`plugins/mcp_plugin.py`:
`asyncio.wait_for`) önce bitecek ve kullanıcı, gerçek hata yerine "sunucu N
saniye içinde cevap vermedi" görecek. Yani `timeout_seconds` her zaman
`_NAVIGATION_TIMEOUT_MS`'ten (30 sn) BÜYÜK olmalıdır; örnek config 60 sn."""

_ALLOWED_SCHEMES = frozenset({"http", "https"})
"""`page.goto`'ya verilebilecek TEK iki şema. `file://` bilerek burada
DEĞİLDİR: headless chromium'a `file:///home/kullanici/.ssh/id_rsa` gibi bir
yol vermek, o dosyanın TAMAMINI okuyup `read_text` ile geri döndürmek
demektir (ölçüldü). `data:` ve `javascript:` da aynı sınıf — tarayıcıyı
sayfa yüklemeden içerik/zarar üretmeye zorlar."""

_ALLOW_LOCAL_ENV = "ARTEMIS_BROWSER_ALLOW_LOCAL"
"""Loopback/private/link-local hedeflere izin veren ortam değişkeni.

VARSAYILAN KAPALI. Açıkken `1`/`true`/`yes`/`on` değerleri bu sunucunun
kendi ağ kısıtlamasını düşürür — KENDİ KENDİ DAHA AZ GÜVENLİ olur, o
yüzden kullanıcı bilerek açmalıdır. `MCPServerConfig.env` üzerinden verilir
(`config.yaml::mcp_servers.<ad>.env`), çünkü `mcp` SDK'sı alt sürece yalnızca
HOME/PATH/... gibi birkaç değişkeni miras aldırır.

KULLANIM SENARYOSU: kullanıcı `http://localhost:3000` üzerindeki kendi
geliştirme sunucusunu otomatikleştirmek isteyebilir. Bunun için bu
değişkeni `1` yapmak yeterlidir; geri kalan her şey (şema kısıtı dahil)
aynen korunur.

YALNIZCA HOST KATMANINI KAPATIR. `file://`/`data:`/`javascript:` şema
kısıtı bu değişkenle AÇILMAZ — "localhost'u aç" demek, "dosya sistemini
okumaya da izin ver" demek değildir (bkz. `_validate_url`)."""


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
        url: Açılacak adres. YALNIZCA `http://` ya da `https://`. `file://`,
            `data:`, `javascript:` reddedilir; loopback (`127.0.0.1`, `::1`,
            `localhost`), özel (`10.x`, `172.16-31.x`, `192.168.x`) ve
            link-local (`169.254.x`, `fe80::`) adresler de varsayılan olarak
            reddedilir.
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
        "partial_results": {...}}` — istisna fırlatmaz. Reddedilen `url`
        (`file://`, `data:`, loopback, ...) tarayıcı hiç başlatılmadan
        `{"success": False, "failed_step": None, "error": ...,
        "partial_results": {}}` döner.

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

    TEK İSTİSNA `url`: pydantic şeması `str` dediği için içeriği hiç
    denetlenmez, ama `url` doğrudan `page.goto`'ya gider. Kısıt bu yüzden
    burada, `goto`'dan ÖNCE uygulanır.
    """

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    read_texts: dict[str, str] = {}

    # Tarayıcı BAŞLATILMADAN önce. `file://` ile bir SSH private key'i okumak
    # (ya da `http://127.0.0.1:PORT`'taki yerel servise gitmek) tarayıcı
    # açmadan da engellenebilir; açtıktan sonra engellemek, saldırı yüzeyini
    # kullanmadan önce kapatmak demek değildir.
    if (rejection := _validate_url(url)) is not None:
        return {"success": False, "failed_step": None, "error": rejection, "partial_results": {}}

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


def _validate_url(url: str) -> str | None:
    """`url`'yi `page.goto`'ya vermeden önce denetler.

    Returns:
        `None` -> URL güvenli, `page.goto` çağrılabilir.
        `str`  -> insan-okunur REDDEDME sebebi. Çağıran, bunu
                  `{"success": False, "error": ...}` olarak döner; istisna
                  FIRLATILMAZ (bkz. modül dokümantasyonu, "İSTİSNA
                  FIRLATMA DİYE KURULDU").

    İki katman var ve BİRLİKTE çalışır:

    1. ŞEMA. Yalnızca `http`/`https`. `file://` okutma açığıdır (dosya
       sistemi), `data:`/`javascript:` ise sayfa yüklemeden içerik
       üretir. Bu katman HİÇBİR ORTAM DEĞİŞKENİYLE devre dışı
       bırakılamaz — kullanıcı localhost'u açabilir ama `file://`'yi
       açmanın bir nedeni yoktur; ihtiyaç duymadığı bir riski
       açmanın bedeli de yoktur.
    2. HOST. Loopback/private/link-local adresler. Bunlar genel internetten
       "ulaşılamayan" ama `page.goto` için TAM ERİŞİLEBİLİR olan adreslerdir:
       kullanıcının kendi makinelerindeki servisler (SSH agent, Docker
       portları, geliştirici sunucuları, yönlendirici panelleri). Bir
       istemci (ya da istemciye sızan bir LLM) `url` alanını kontrol
       ederek ağın içine SSRF yapabilirdi. YALNIZCA bu katman
       `_ALLOW_LOCAL_ENV` ile kapatılabilir (bkz. yukarıdaki tanım).
    """

    if not url or not url.strip():
        return "url boş olamaz."

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    if scheme not in _ALLOWED_SCHEMES:
        # Kullanıcıya hangi şemaların serbest olduğunu söylüyoruz; bu
        # sessiz bir "geçersiz" değil, kullanılabilir bir yönlendirme.
        return (
            f"'{scheme or url}' şemasına izin verilmiyor. "
            f"Bu tool yalnızca http:// ve https:// adreslerini açar "
            f"(file://, data:, javascript: ve diğer şemalar reddedilir) — "
            f"dosya sistemi okumak ya da sayfa yüklemeden içerik üretmek "
            f"bu tool'ın işi değildir."
        )

    # ŞEMA geçti. Bundan SONRA — ve yalnızca bundan sonra — yerel
    # adreslere izin verilebilir; aksi halde `file://` de yeniden açılır.
    if _allow_local_targets():
        return None

    host = parts.hostname
    if not host:
        return f"'{url}' içinde bir host yok (örn. 'https://example.com')."

    # `hostname` zaten küçük harfe indirir ve portu/credential'ı ayıklar.
    # Bir IP literal ise çözüm YAPMADAN doğrudan sınıflandırılır —
    # "decimal IP" gibi DNS'e girmeyen atlatmalar böylece mümkün olmaz.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        return _reject_ip(literal, url)

    if host == "localhost" or host.endswith(".localhost"):
        return (
            f"'{host}' yerel bir adrese işaret ediyor. Varsayılan olarak "
            f"loopback'e gidilmez; {host} kullanmak istiyorsanız "
            f"{_ALLOW_LOCAL_ENV}=1 ayarlayın."
        )

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # DNS çözülemezse REDDEDİLİR: "çözülemeyen host" ile "loopback'e
        # giden host" arasında ayrım yapamıyorsak güvenli taraf seçilir.
        return f"'{host}' çözülemedi ({exc}). Erişilebilir olduğu doğrulanamayan bir adrese gidilmez."

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover - getaddrinfo hep geçerli IP döner
            return f"'{host}' beklenmeyen bir adres çözümü verdi ({addr}); reddedildi."
        rejection = _reject_ip(ip, url)
        if rejection is not None:
            return rejection

    return None


def _reject_ip(ip: Any, url: str) -> str | None:
    """Tek bir IP'nin engellenip engellenmediğini söyler (`None` = geçerli).

    `is_private`/`is_loopback`/`is_link_local` bayrakları ayrı ayrı kontrol
    edilir çünkü bazı adres sınıfları (örn. `169.254.0.0/16` link-local ya da
    benzersiz-yerel IPv6) birbirinin alt kümesi değildir. SIRA ÖNEMLİ:
    Python'un `ipaddress`'ında `is_private`, link-local adresler için de
    `True` döner; mesajın "link-local" demesi için önce o kontrol edilir.
    """

    if ip.is_loopback:
        reason = "loopback"
    elif ip.is_link_local:
        reason = "link-local"
    elif ip.is_private:
        reason = "özel (private)"
    elif ip.is_reserved or ip.is_multicast:
        reason = "ayrılmış/çok noktaya yayın"
    elif ip.is_unspecified:
        reason = "belirsiz (0.0.0.0/::)"
    else:
        return None

    return (
        f"'{url}' {reason} bir adrese ({ip}) işaret ediyor; varsayılan olarak "
        f"reddedildi. Bu, tarayıcıyı kendi makinenizin/iç ağınızın servislerine "
        f"yönlendirmeyi (SSRF) engeller. Kendi yerel servisinizi "
        f"otomatikleştirmek istiyorsanız ortam değişkeni "
        f"{_ALLOW_LOCAL_ENV}=1 yapın."
    )


def _allow_local_targets() -> bool:
    """`_ALLOW_LOCAL_ENV` etkin mi? (yalnızca http/https kontrolü kapatılır)"""

    return os.environ.get(_ALLOW_LOCAL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


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
