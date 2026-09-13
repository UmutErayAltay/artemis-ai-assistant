"""Dosya sistemi ile ilgili tool'lar.

Bu dosya, plugin sisteminin somut bir referans örneğidir: yeni bir
dosya sistemi işlemi eklemek için burada yeni bir BaseTool alt sınıfı
yazıp `@register_tool` ile işaretlemek yeterlidir; `core/` altında
hiçbir şey değişmez. Gelecekteki `browser_plugin.py`, `windows_plugin.py`
gibi dosyalar da aynı şablonu izleyecektir.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult


def _resolve_location(location: str, context: ToolContext) -> Path:
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


def _safe_join(base: Path, target: str) -> Path | None:
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
    `ToolResult(success=False, ...)` döndürür (bkz. `_unsafe_target_result`).
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


def _walk_limited(root: Path, max_depth: int) -> Iterator[Path]:
    """`root` altındaki her dosya/klasörü, en fazla `max_depth` seviye
    inerek ve erişim reddedilen alt ağaçları ATLAYARAK gezer.

    `Path.rglob("*")`'in YERİNE geçer: `rglob` ne bir derinlik sınırı
    tanır ne de bir `PermissionError`'ı tolere eder — erişilemeyen TEK
    bir alt klasör (örn. bir sistem klasörü) aramanın TAMAMINI
    `PermissionError` ile düşürür. `os.walk(..., onerror=...)` bu ikisini
    de doğal olarak sağlar: `onerror` verilirse hatalı bir dizin
    sessizce atlanır, listeleme durmaz.
    """

    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda exc: None):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth
        if depth >= max_depth:
            dirnames[:] = []  # bu seviyede dur, daha derine inme
        for name in dirnames + filenames:
            yield current / name


def _unsafe_target_result(target: str) -> ToolResult:
    """`_safe_join` tarafından reddedilen bir `target/name` için tutarlı,
    açıklayıcı bir başarısızlık sonucu üretir (mesaj tüm tool'larda ortak).

    `_safe_join` üç ayrı durumda (mutlak yol, ".." veya `location`'ın
    kendisine sadeleşme) da aynı şekilde `None` döndürür (`Path | None`
    tasarımı, bkz. `_safe_join` docstring'i) — yani çağıran taraf reddin asıl
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


_OVERWRITE_HINT = "üzerine yazmak için overwrite=true gönderin."


def _fs_failure(op_description: str, path: Path, exc: OSError) -> ToolResult:
    """Bir dosya sistemi mutasyonu (kopyalama/taşıma/yeniden adlandırma/
    silme) başarısız olduğunda tutarlı bir `ToolResult` üretir.

    NEDEN GEREKLİ: bu çağrılar (`shutil.copytree`, `.rename()`,
    `shutil.move`, `shutil.rmtree`, `.unlink()`) hiçbiri `try/except`
    içinde değildi; `PermissionError`/`OSError` doğrudan dispatcher'ın
    genel "beklenmeyen hata" dalına düşüyordu (bkz. `core/dispatcher.
    py::dispatch`) — tam olarak CLAUDE.md'nin düzeltildiğini söylediği
    "kullanıcı 'target' diye anlaşılmaz bir hata görüyordu" sınıfı.
    """

    return ToolResult(success=False, message=f"'{path}' {op_description}: {exc}")


def _prepare_destination(destination: Path, overwrite: bool) -> ToolResult | None:
    """Hedefi yazılabilir hâle getirir; engel varsa açıklayan sonucu döndürür.

    Bu blok `copy`, `rename` ve `move`'da ÜÇ KEZ birebir tekrarlanıyordu.
    Tekrarın bedeli zaten ödendi: `.context` §6.14, aynı sınıf hatanın
    (kaynak == hedef olduğunda `overwrite` dalının önce hedefi SİLİP sonra
    kaynaktan taşımaya çalışması — yani kendini silmesi) bu üçlüde iki kez
    bulunduğunu kaydediyor. Üç kopya, bir düzeltmenin üçüne birden
    uygulanmasını unutmayı kolaylaştırır.

    Args:
        destination: Yazılacak hedef yol.
        overwrite: Hedefte aynı adda bir şey varsa silinsin mi.

    Returns:
        `None` ise yol açık, arayan devam edebilir. Aksi hâlde
        kullanıcıya döndürülecek başarısızlık sonucu.
    """

    if not destination.exists():
        return None

    if not overwrite:
        return ToolResult(
            success=False,
            message=f"'{destination}' hedefinde aynı isimde bir öğe zaten var; {_OVERWRITE_HINT}",
        )

    try:
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    except OSError as exc:
        # Koşulsuz devam yasak: silme başarısız olduysa (dosya açık, izin
        # yok) taşıma/kopyalama da başarısız olur — sebebini burada
        # söylemek, sonraki adımın anlaşılmaz hatasından iyidir.
        return ToolResult(success=False, message=f"'{destination}' üzerine yazılamadı: {exc}")

    return None


@register_tool
class FilesystemOpenTool(BaseTool):
    """Belirtilen konumdaki bir dosya/klasörü varsayılan uygulamayla açar."""

    name = "filesystem.open"
    description = "Bir dosyayı veya klasörü açar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Açılacak dosya/klasör adı."},
                "location": {
                    "type": "string",
                    "description": "'desktop', 'downloads', 'last' veya tam yol.",
                    "default": "desktop",
                },
            },
            "required": ["target"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        location = arguments.get("location", "desktop")
        full_path = _safe_join(_resolve_location(location, context), target)
        if full_path is None:
            return _unsafe_target_result(target)

        if not full_path.exists():
            return ToolResult(success=False, message=f"'{full_path}' bulunamadı.")

        # Korumasız `os.startfile` yasak: kayıtlı bir uygulaması olmayan
        # bir uzantı, bozuk bir kısayol ya da erişim reddi `OSError`
        # fırlatır ve kullanıcı "Beklenmeyen hata: ..." görürdü. Aynı
        # çağrı `windows_plugin.WindowsLaunchAppTool`'da zaten sarılı.
        try:
            os.startfile(full_path)  # Windows'a özgü; proje Windows masaüstü hedefliyor.
        except OSError as exc:
            return ToolResult(success=False, message=f"'{full_path}' açılamadı: {exc}")
        except AttributeError:
            return ToolResult(
                success=False,
                message="Dosya açma yalnızca Windows'ta desteklenir (os.startfile bu platformda yok).",
            )
        context.memory.remember_last_path(str(full_path))
        return ToolResult(success=True, message=f"'{full_path}' açıldı.", data={"path": str(full_path)})


@register_tool
class FilesystemCreateFolderTool(BaseTool):
    """Belirtilen konumda yeni bir klasör oluşturur."""

    name = "filesystem.create_folder"
    description = "Yeni bir klasör oluşturur."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Oluşturulacak klasörün adı."},
                "location": {"type": "string", "default": "desktop"},
            },
            "required": ["name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        name = arguments["name"]
        location = arguments.get("location", "desktop")
        new_folder = _safe_join(_resolve_location(location, context), name)
        if new_folder is None:
            return _unsafe_target_result(name)

        already_existed = new_folder.exists()
        try:
            new_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(success=False, message=f"'{new_folder}' oluşturulamadı: {exc}")

        context.memory.remember_last_path(str(new_folder))
        # `exist_ok=True` var olan bir klasörü de sessizce kabul eder;
        # bu durumda "oluşturuldu" demek yanıltıcıdır — kullanıcı klasörün
        # YENİ oluştuğunu sanır. Mesaj gerçek durumu yansıtır, ama bu bir
        # HATA değildir (idempotent davranış bilinçli — bkz. modül üstü
        # docstring).
        verb = "zaten vardı" if already_existed else "klasörü oluşturuldu"
        return ToolResult(
            success=True,
            message=f"'{new_folder}' {verb}.",
            data={"path": str(new_folder)},
        )


@register_tool
class FilesystemCreateFileTool(BaseTool):
    """Belirtilen konumda yeni (boş veya içerikli) bir dosya oluşturur."""

    name = "filesystem.create_file"
    description = "Yeni bir dosya oluşturur."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "location": {"type": "string", "default": "desktop"},
                "content": {"type": "string", "default": ""},
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Aynı isimde bir dosya zaten varsa üzerine yazılsın mı.",
                },
            },
            "required": ["name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        name = arguments["name"]
        location = arguments.get("location", "desktop")
        content = arguments.get("content", "")
        overwrite = bool(arguments.get("overwrite", False))
        base_path = _resolve_location(location, context)
        new_file = _safe_join(base_path, name)
        if new_file is None:
            return _unsafe_target_result(name)

        # `copy`/`rename`/`move` üçü de `_prepare_destination` ile aynı
        # şekilde davranıyor; `create_file` eskiden bunlardan HİÇBİRİNE
        # sahip değildi ve `write_text` var olan bir dosyanın üzerine
        # SESSİZCE yazıyordu — `danger_level=SAFE` altında veri kaybı
        # (CLAUDE.md: koşulsuz success=True/sessiz veri kaybı yasak).
        blocked = _prepare_destination(new_file, overwrite)
        if blocked is not None:
            return blocked

        try:
            base_path.mkdir(parents=True, exist_ok=True)
            new_file.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, message=f"'{new_file}' oluşturulamadı: {exc}")

        context.memory.remember_last_path(str(new_file))
        return ToolResult(success=True, message=f"'{new_file}' oluşturuldu.", data={"path": str(new_file)})


@register_tool
class FilesystemSearchTool(BaseTool):
    """Bir klasör altında isme göre dosya/klasör arar.

    SINIRLIDIR (derinlik + sonuç sayısı): eskiden `Path.rglob("*")`
    sınırsızdı — `location:"C:/"` gibi geniş bir kök dispatcher'ı (ve ses
    işçisini) dakikalarca bloke edip sınırsız bir listeyi LLM bağlamına
    dolduruyordu. Ayrıca `PermissionError` veren alt dizinler (erişim
    reddedilen sistem klasörleri gibi) artık aramanın TAMAMINI
    düşürmüyor, yalnızca o alt ağacı atlıyor.
    """

    name = "filesystem.search"
    description = "Dosya veya klasör arar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "location": {"type": "string", "default": "desktop"},
            },
            "required": ["query"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = arguments["query"].lower()
        base_path = _resolve_location(arguments.get("location", "desktop"), context)

        if not base_path.exists():
            return ToolResult(success=False, message=f"'{base_path}' bulunamadı.")

        max_results = context.settings.search_max_results
        max_depth = context.settings.search_max_depth

        matches: list[str] = []
        truncated = False
        for path in _walk_limited(base_path, max_depth):
            if query in path.name.lower():
                if len(matches) >= max_results:
                    truncated = True
                    break
                matches.append(str(path))

        message = f"{len(matches)} sonuç bulundu."
        if truncated:
            message += f" (İlk {max_results} sonuç gösteriliyor, daha fazlası olabilir.)"
        return ToolResult(success=True, message=message, data={"matches": matches})


@register_tool
class FilesystemCopyTool(BaseTool):
    """Bir dosyayı/klasörü başka bir konuma kopyalar."""

    name = "filesystem.copy"
    description = "Bir dosya veya klasörü kopyalar."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "source_location": {"type": "string", "default": "desktop"},
                "destination_location": {"type": "string", "default": "desktop"},
                "overwrite": {
                    "type": "boolean",
                    "description": "Hedefte aynı isim varsa üzerine yazılsın mı.",
                    "default": False,
                },
            },
            "required": ["target", "destination_location"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        overwrite = arguments.get("overwrite", False)
        source = _safe_join(_resolve_location(arguments.get("source_location", "desktop"), context), target)
        if source is None:
            return _unsafe_target_result(target)
        destination_dir = _resolve_location(arguments["destination_location"], context)

        if not source.exists():
            return ToolResult(success=False, message=f"'{source}' bulunamadı.")

        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name

        if source == destination:
            return ToolResult(
                success=False,
                message="Kaynak ve hedef aynı; kopyalanacak bir şey yok.",
            )

        engel = _prepare_destination(destination, overwrite)
        if engel is not None:
            return engel

        try:
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=overwrite)
            else:
                shutil.copy2(source, destination)
        except OSError as exc:
            return _fs_failure("kopyalanamadı", source, exc)

        # `rename`/`move` `remember_last_path` çağırır ama `copy` eskiden
        # çağırmıyordu — `location: "last"` bir kopyadan sonra tutarsız
        # davranıyordu (aynı dosyada üç mutasyon tool'unun ikisi hatırlıyor,
        # biri hatırlamıyordu).
        context.memory.remember_last_path(str(destination))
        return ToolResult(
            success=True,
            message=f"'{source.name}', {destination.parent.name} klasörüne kopyalandı.",
            data={"source": str(source), "destination": str(destination)},
        )


@register_tool
class FilesystemRenameTool(BaseTool):
    """Bir dosyayı/klasörü AYNI dizin içinde yeniden adlandırır.

    Konum değiştirmek için değil — bunun için `filesystem.move` var.
    Hem `target` (mevcut ad) hem `name` (yeni ad) `_safe_join` ile
    doğrulanır: yeni ad da mutlak yol/`..` içeremez, aksi halde
    `Path.rename()`'e verilen bir yol dizini dışına taşıyabilirdi.
    """

    name = "filesystem.rename"
    description = "Bir dosya/klasörü yeniden adlandırır (konumu değişmez)."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Mevcut dosya/klasör adı."},
                "name": {"type": "string", "description": "Yeni ad."},
                "location": {"type": "string", "default": "desktop"},
                "overwrite": {
                    "type": "boolean",
                    "description": "Hedefte aynı isim varsa üzerine yazılsın mı.",
                    "default": False,
                },
            },
            "required": ["target", "name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        new_name = arguments["name"]
        overwrite = arguments.get("overwrite", False)
        base_path = _resolve_location(arguments.get("location", "desktop"), context)

        source = _safe_join(base_path, target)
        if source is None:
            return _unsafe_target_result(target)
        destination = _safe_join(base_path, new_name)
        if destination is None:
            return _unsafe_target_result(new_name)

        if not source.exists():
            return ToolResult(success=False, message=f"'{source}' bulunamadı.")

        # Kaynak ve hedef aynı yola çözülüyorsa (örn. yeni ad eskiyle
        # birebir aynı) hiçbir şey yapmadan başarı dön — aksi halde
        # aşağıdaki "overwrite" dalı kaynağı SİLİP sonra yeniden
        # adlandırmaya çalışırdı (veri kaybı).
        if source == destination:
            return ToolResult(success=True, message=f"'{source.name}' zaten bu adda.", data={"path": str(source)})

        engel = _prepare_destination(destination, overwrite)
        if engel is not None:
            return engel

        try:
            source.rename(destination)
        except OSError as exc:
            return _fs_failure("yeniden adlandırılamadı", source, exc)

        context.memory.remember_last_path(str(destination))
        return ToolResult(
            success=True,
            message=f"'{source.name}', '{destination.name}' olarak yeniden adlandırıldı.",
            data={"path": str(destination)},
        )


@register_tool
class FilesystemMoveTool(BaseTool):
    """Bir dosyayı/klasörü BAŞKA bir konuma taşır (`filesystem.copy`'nin
    taşıma karşılığı — argüman şeması BİREBİR aynı, yalnızca kaynakta
    kopya bırakmaz)."""

    name = "filesystem.move"
    description = "Bir dosya/klasörü başka bir konuma taşır (kaynakta kopya kalmaz)."
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "source_location": {"type": "string", "default": "desktop"},
                "destination_location": {"type": "string", "default": "desktop"},
                "overwrite": {
                    "type": "boolean",
                    "description": "Hedefte aynı isim varsa üzerine yazılsın mı.",
                    "default": False,
                },
            },
            "required": ["target", "destination_location"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        overwrite = arguments.get("overwrite", False)
        source = _safe_join(_resolve_location(arguments.get("source_location", "desktop"), context), target)
        if source is None:
            return _unsafe_target_result(target)
        destination_dir = _resolve_location(arguments["destination_location"], context)

        if not source.exists():
            return ToolResult(success=False, message=f"'{source}' bulunamadı.")

        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name

        # Bkz. FilesystemRenameTool'daki aynı korumanın gerekçesi:
        # kaynak/hedef aynı yolsa "overwrite" dalı kaynağı silip sonra
        # taşımaya çalışırdı.
        if source == destination:
            return ToolResult(success=True, message="Kaynak zaten hedef konumda.", data={"path": str(source)})

        engel = _prepare_destination(destination, overwrite)
        if engel is not None:
            return engel

        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            return _fs_failure("taşınamadı", source, exc)

        context.memory.remember_last_path(str(destination))
        return ToolResult(
            success=True,
            message=f"'{source.name}', {destination.parent.name} klasörüne taşındı.",
            data={"source": str(source), "destination": str(destination)},
        )


@register_tool
class FilesystemDeleteTool(BaseTool):
    """Bir dosyayı veya klasörü siler.

    GERİ ALINAMAZ bir işlem olduğu için `danger_level=CONFIRM_REQUIRED`
    olarak işaretlenmiştir; dispatcher kullanıcı onayı olmadan bu
    execute() metodunu çağırmaz (bkz. GÜVENLİK kuralları).
    """

    name = "filesystem.delete"
    description = "Bir dosya veya klasörü siler."
    danger_level = DangerLevel.CONFIRM_REQUIRED

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "location": {"type": "string", "default": "desktop"},
            },
            "required": ["target"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        full_path = _safe_join(_resolve_location(arguments.get("location", "desktop"), context), target)
        if full_path is None:
            return _unsafe_target_result(target)

        if not full_path.exists():
            return ToolResult(success=False, message=f"'{full_path}' bulunamadı.")

        try:
            if full_path.is_dir():
                shutil.rmtree(full_path)
            else:
                full_path.unlink()
        except OSError as exc:
            return _fs_failure("silinemedi", full_path, exc)

        return ToolResult(success=True, message=f"'{full_path}' silindi.")
