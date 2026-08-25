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
from pathlib import Path
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
    if candidate.anchor or ".." in candidate.parts or not candidate.parts:
        return None
    return base / candidate


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

        os.startfile(full_path)  # Windows'a özgü; proje Windows masaüstü hedefliyor.
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

        new_folder.mkdir(parents=True, exist_ok=True)
        context.memory.remember_last_path(str(new_folder))
        return ToolResult(
            success=True,
            message=f"'{new_folder}' klasörü oluşturuldu.",
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
            },
            "required": ["name"],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        name = arguments["name"]
        location = arguments.get("location", "desktop")
        content = arguments.get("content", "")
        base_path = _resolve_location(location, context)
        new_file = _safe_join(base_path, name)
        if new_file is None:
            return _unsafe_target_result(name)
        base_path.mkdir(parents=True, exist_ok=True)

        new_file.write_text(content, encoding="utf-8")
        context.memory.remember_last_path(str(new_file))
        return ToolResult(success=True, message=f"'{new_file}' oluşturuldu.", data={"path": str(new_file)})


@register_tool
class FilesystemSearchTool(BaseTool):
    """Bir klasör altında isme göre dosya/klasör arar."""

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

        matches = [str(p) for p in base_path.rglob("*") if query in p.name.lower()]
        return ToolResult(success=True, message=f"{len(matches)} sonuç bulundu.", data={"matches": matches})


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

        if destination.exists() and not overwrite:
            return ToolResult(
                success=False,
                message=(
                    f"'{destination}' hedefinde aynı isimde bir öğe zaten var; "
                    "üzerine yazmak için overwrite=true gönderin."
                ),
            )

        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=overwrite)
        else:
            shutil.copy2(source, destination)

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

        if destination.exists():
            if not overwrite:
                return ToolResult(
                    success=False,
                    message=(
                        f"'{destination}' hedefinde aynı isimde bir öğe zaten var; "
                        "üzerine yazmak için overwrite=true gönderin."
                    ),
                )
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()

        source.rename(destination)
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

        if destination.exists():
            if not overwrite:
                return ToolResult(
                    success=False,
                    message=(
                        f"'{destination}' hedefinde aynı isimde bir öğe zaten var; "
                        "üzerine yazmak için overwrite=true gönderin."
                    ),
                )
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()

        shutil.move(str(source), str(destination))
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

        if full_path.is_dir():
            shutil.rmtree(full_path)
        else:
            full_path.unlink()

        return ToolResult(success=True, message=f"'{full_path}' silindi.")
