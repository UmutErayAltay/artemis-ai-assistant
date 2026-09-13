"""Dosya sistemi yollarını çözen ve doğrulayan paylaşılan yardımcılar.

NEDEN AYRI BİR MODÜL: `_resolve_location`/`_safe_join`/`_unsafe_target_result`
`plugins/filesystem_plugin.py`'de tanımlanmıştı, ama `plugins/
windows_plugin.py` da (`windows.screenshot` için) `_resolve_location`'a
ihtiyaç duyuyor ve bunu sibling bir plugin'in PRIVATE (alt çizgiyle
başlayan) isim alanından `from plugins.filesystem_plugin import
_resolve_location` diye içe aktarıyordu. Bu iki sorun taşıyordu: (1)
"private" bir sembolün modül dışına, hatta plugin dışına sızması —
`filesystem_plugin.py` içindeki bir refactor bunu habersizce kırabilirdi;
(2) paylaşılan bir yol/güvenlik yardımcısının "sahibi" hangi plugin
olduğu belirsizdi. Bu üçü artık gerçek sahipleri olan bir yerde:
paylaşılan altyapı (`utils/`). `plugins/filesystem_plugin.py` geriye
dönük uyumluluk için (ve mevcut testlerin `from plugins.filesystem_plugin
import _resolve_location, _safe_join` biçimindeki import'ları kırılmasın
diye) bu isimleri kendi isim alanından YENİDEN DIŞA VERİR.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from core.tool_base import ToolContext
from models.tool_models import ToolResult


def resolve_location(location: str, context: ToolContext) -> Path:
    """"desktop", "downloads", "last" gibi sembolik konumları gerçek Path'e çevirir.

    Tüm filesystem tool'ları aynı çözümlemeyi kullandığı için bu mantık
    tek bir yerde tutulur (kod tekrarını önleyen ortak yardımcı fonksiyon).
    """

    aliases = {
        "desktop": context.settings.desktop_path,
        "downloads": context.settings.downloads_path,
    }
    if location in aliases:
        return aliases[location]
    if location == "last":
        last = context.memory.get_last_path()
        return Path(last) if last else Path.home()
    return Path(location).expanduser()


def safe_join(base: Path, target: str) -> Path | None:
    """`target`'ı `base` altında kalan güvenli bir alt yola çevirir.

    `target` LLM tarafından üretilir ve HALÜSİNASYON içerebilir. Şemaların
    açıklaması `target`/`name` için bir dosya/klasör *adı* (gerekirse göreli
    bir alt yol, örn. "Orbit/app.py") bekler — mutlak bir yol beklemez.
    Ama `target` mutlak bir yol olursa `Path(base) / Path(target)` pathlib
    davranışı gereği `base`'i tamamen görmezden gelir; `target` içinde ".."
    olursa da üst dizinlere çıkılabilir. İkisi de `location` ile ifade
    edilen (ve kullanıcının onayladığı varsayılan) konumun dışına çıkışa,
    yani dizin dışına sızmaya yol açar.

    Üçüncü bir tehlike de `target`'ın `base`'in KENDİSİNE sadeleşmesidir:
    boş dize, "." veya "./" (ve "././." gibi tekrarları) pathlib'de hiç
    parçası olmayan (`candidate.parts == ()`) bir yola karşılık gelir ve
    `base / candidate` doğrudan `base`'in kendisine eşitlenir. Kullanıcı
    "masaüstündeki Orbit'i sil" derken masaüstünün kendisini değil, içindeki
    bir şeyi kastediyor; `location`'ın kendisini işaret eden bir `target` de
    (özellikle `filesystem.delete` için) dizin dışına sızma kadar tehlikeli
    olduğundan aynı şekilde reddedilmelidir.

    Bu yüzden `target` şu durumlarda REDDEDİLİR (None döner):
        - mutlak bir yol veya bir sürücü/kök içeriyorsa (`candidate.anchor`
          hem `Path.is_absolute()` hem de yalnızca sürücü/kök içeren
          "C:tmp" gibi sınır durumları kapsar),
        - parçalarından biri ".." ise (üst dizine çıkış),
        - hiç parçası yoksa (`candidate.parts == ()`) — yani boş dize, "."
          veya "./" gibi `base`'in kendisine sadeleşen bir değerse
          (`location`'ın kendisini hedefleme).
    Bunların dışındaki göreli alt yollar (örn. "AltKlasor/dosya.txt")
    kısıtlanmadan `base / target` olarak döndürülür.

    Reddetme durumunda exception fırlatmak yerine None döndürülür; çağıran
    tool bunu kontrol edip kullanıcıya açıklayıcı bir Türkçe mesajla
    `ToolResult(success=False, ...)` döndürür (bkz. `unsafe_target_result`).
    """

    candidate = Path(target)

    # Yol, HEM çalıştığımız platformun kurallarına HEM de Windows
    # kurallarına göre denetlenir. Sebep ölçüldü: POSIX'te
    # `Path("C:/Windows/System32")` MUTLAK DEĞİLDİR (`anchor == ""`),
    # parçaları `("C:", "Windows", "System32")` olur — yani bu koruma
    # Linux/macOS'ta çalışırken tam olarak engellemesi gereken girdiyi
    # KABUL EDİYORDU. Aynı şekilde `"AltKlasor\..\..\x"` POSIX'te tek
    # bir parçadır, `..` hiç görünmez.
    #
    # Artemis bir Windows uygulaması, yani üretimde bu fark görünmezdi;
    # ama bu bir güvenlik kontrolü ve bir güvenlik kontrolünün doğruluğu
    # çalıştığı makinenin işletim sistemine BAĞLI OLMAMALIDIR. (Pratik
    # sonucu da var: bu üç senaryonun testleri Linux CI'da kırılıyordu.)
    windows_candidate = PureWindowsPath(target)

    for parsed in (candidate, windows_candidate):
        if parsed.anchor or ".." in parsed.parts or not parsed.parts:
            return None

    return base / candidate


def unsafe_target_result(target: str) -> ToolResult:
    """`safe_join` tarafından reddedilen bir `target/name` için tutarlı,
    açıklayıcı bir başarısızlık sonucu üretir (mesaj tüm tool'larda ortak).

    `safe_join` üç ayrı durumda (mutlak yol, ".." veya `location`'ın
    kendisine sadeleşme) da aynı şekilde `None` döndürür (`Path | None`
    tasarımı, bkz. `safe_join` docstring'i) — yani çağıran taraf reddin asıl
    sebebini bilmez. Bu yüzden burada tek bir mesaj her üç durumu da
    kapsayacak şekilde genelleştirilmiştir.
    """

    return ToolResult(
        success=False,
        message=(
            f"'{target}' geçersiz: 'target'/'name', 'location' içindeki bir "
            "dosya/klasör adı ya da göreli bir alt yol olmalı; mutlak yol, "
            "'..' içeremez ve 'location'ın kendisini (boş, '.' gibi bir "
            "değerle) işaret edemez. Farklı bir konum hedeflemek için "
            "'location' argümanını kullanın."
        ),
    )
