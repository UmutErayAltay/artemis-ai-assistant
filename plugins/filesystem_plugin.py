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
from pathlib import Path
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult
from utils.paths import resolve_location as _resolve_location
from utils.paths import safe_join as _safe_join
from utils.paths import unsafe_target_result as _unsafe_target_result

# `_resolve_location`/`_safe_join`/`_unsafe_target_result` artık GERÇEKTEN
# `utils/paths.py`'de tanımlı — burada yalnızca YENİDEN DIŞA VERİLİYOR.
# `plugins/windows_plugin.py`'nin bu isim alanının PRIVATE bir sembolüne
# (`from plugins.filesystem_plugin import _resolve_location`) erişmesi
# gerekmesin diye taşındı; bu üç `_`-önekli ad burada yalnızca GERİYE
# DÖNÜK UYUMLULUK için tutuluyor (mevcut testler ve bu dosyanın kendi iç
# çağrıları bu adları kullanıyor).






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
                "destination_location": {
                    "type": "string",
                    "default": "desktop",
                    "description": "Kopyanın konacağı konum.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Hedefte aynı isim varsa üzerine yazılsın mı.",
                    "default": False,
                },
            },
            # NOT: `destination_location` `required`e EKLENMEZ — `default`
            # değeri (`"desktop"`) ile birlikte `required` olması, bir dönem
            # bu varsayımı hiçbir zaman uygulanamaz kılıyordu: `core/
            # dispatcher.py::_validate_arguments` `required`ı `default`
            # UYGULANMADAN ÖNCE kontrol ediyor (şema kendisi default
            # UYGULAMIYOR, yalnızca modele/dokümana bir ipucu veriyor).
            # Yani `destination_location` verilmeden yapılan HER çağrı,
            # dokümante edilen varsayılana rağmen reddediliyordu.
            "required": ["target"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        overwrite = arguments.get("overwrite", False)
        source = _safe_join(_resolve_location(arguments.get("source_location", "desktop"), context), target)
        if source is None:
            return _unsafe_target_result(target)
        destination_dir = _resolve_location(arguments.get("destination_location", "desktop"), context)

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
            # `destination_location` neden `required` DEĞİL: bkz.
            # `FilesystemCopyTool.get_arguments_schema` (aynı çakışma,
            # aynı düzeltme — burada bire bir tekrarlanmasın diye).
            "required": ["target"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        target = arguments["target"]
        overwrite = arguments.get("overwrite", False)
        source = _safe_join(_resolve_location(arguments.get("source_location", "desktop"), context), target)
        if source is None:
            return _unsafe_target_result(target)
        destination_dir = _resolve_location(arguments.get("destination_location", "desktop"), context)

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
