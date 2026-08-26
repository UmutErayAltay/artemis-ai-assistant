"""`plugins.filesystem_plugin` içindeki 8 tool ve `_resolve_location` testleri.

Gerçek dosya sistemi + gerçek `ToolDispatcher` kullanılır (tmp_path ile
izole edilmiş sahte bir masaüstü/indirilenler klasörü); yalnızca
`os.startfile` (gerçekten dosya/uygulama açan Windows'a özgü çağrı)
monkeypatch ile yakalanır. Bu, dosya sistemini mock'lamak DEĞİL, harici
(OS düzeyinde) bir yan etkiyi engellemektir — hangi yolla çağrıldığı yine
de `opened_paths` listesinden doğrulanır (bkz. CLAUDE.md test kuralları).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from core.tool_base import ToolContext
from memory.context_memory import ContextMemory
from plugins.filesystem_plugin import _resolve_location, _safe_join


@pytest.fixture
def desktop(tmp_path: Path) -> Path:
    """İzole sahte masaüstü klasörü (gerçek kullanıcı masaüstüne asla dokunulmaz)."""

    path = tmp_path / "Desktop"
    path.mkdir()
    return path


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    """İzole sahte indirilenler klasörü."""

    path = tmp_path / "Downloads"
    path.mkdir()
    return path


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """`os.startfile` çağrılarını gerçek yan etki olmadan yakalar.

    `filesystem.open` Windows'a özgü `os.startfile()` çağırır; bu gerçekten
    bir dosya/uygulama açar. Testte bu yan etkiyi tetiklemeden hangi yolla
    çağrıldığını doğrulamak için monkeypatch ile yakalanıp bu listeye
    kaydedilir.
    """

    calls: list[Path] = []
    monkeypatch.setattr(os, "startfile", lambda path: calls.append(Path(path)), raising=False)
    return calls


@pytest.fixture
def dispatcher(desktop: Path, downloads: Path, tmp_path: Path, opened_paths: list[Path]) -> ToolDispatcher:
    settings = Settings(
        desktop_path=desktop,
        downloads_path=downloads,
        db_path=tmp_path / "memory.db",
        log_dir=tmp_path / "logs",
    )
    memory = ContextMemory(settings.db_path)
    return ToolDispatcher(settings=settings, memory=memory)


@pytest.fixture
def context(dispatcher: ToolDispatcher) -> ToolContext:
    """`_resolve_location` gibi tool-içi yardımcıları dispatcher olmadan
    doğrudan çağırabilmek için dispatcher ile aynı settings/memory'yi
    paylaşan bir ToolContext üretir.
    """

    return ToolContext(
        settings=dispatcher.settings,
        memory=dispatcher.memory,
        logger=logging.getLogger("tests.filesystem_plugin"),
    )


# --- filesystem.open ---


def test_open_existing_file_calls_startfile_and_remembers_path(
    dispatcher: ToolDispatcher, desktop: Path, opened_paths: list[Path]
) -> None:
    target = desktop / "notlar.txt"
    target.write_text("merhaba", encoding="utf-8")

    result = dispatcher.dispatch({"tool": "filesystem.open", "arguments": {"target": "notlar.txt"}})

    assert result.success is True
    assert opened_paths == [target]
    assert dispatcher.memory.get_last_path() == str(target)


def test_open_missing_target_fails_and_never_touches_startfile(
    dispatcher: ToolDispatcher, opened_paths: list[Path]
) -> None:
    result = dispatcher.dispatch({"tool": "filesystem.open", "arguments": {"target": "olmayan.txt"}})

    assert result.success is False
    assert opened_paths == []


def test_open_without_required_target_argument_fails_gracefully(dispatcher: ToolDispatcher) -> None:
    """Eksik zorunlu anahtar artık tool'a hiç ulaşmadan, merkezi doğrulamada
    yakalanır (bkz. `core/dispatcher.py::_validate_arguments`, README §24).
    Eskiden bu tool içinde ham bir `KeyError`e düşüp dispatcher'ın genel
    `except Exception` bloğuna yakalanıyordu — süreç çökmüyordu ama kullanıcı
    "Beklenmeyen hata: 'target'" gibi anlaşılmaz bir mesaj görüyordu.
    """

    result = dispatcher.dispatch({"tool": "filesystem.open", "arguments": {}})

    assert result.success is False
    assert "target" in result.message
    assert "Beklenmeyen hata" not in result.message


def test_open_rejects_absolute_target_outside_location(
    dispatcher: ToolDispatcher, tmp_path: Path, opened_paths: list[Path]
) -> None:
    """`target` mutlak bir yol olursa `location` tamamen görmezden
    alınabilirdi (pathlib `/` davranışı); Düzeltme 1 bunu reddetmeli.
    """

    outside = tmp_path / "disaridaki.txt"
    outside.write_text("dokunulmamali", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.open", "arguments": {"target": str(outside)}}
    )

    assert result.success is False
    assert opened_paths == []


# --- filesystem.create_folder ---


def test_create_folder_creates_directory_on_desktop(dispatcher: ToolDispatcher, desktop: Path) -> None:
    result = dispatcher.dispatch({"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}})

    assert result.success is True
    assert (desktop / "Orbit").is_dir()
    assert result.data == {"path": str(desktop / "Orbit")}


def test_create_folder_twice_is_idempotent(dispatcher: ToolDispatcher, desktop: Path) -> None:
    first = dispatcher.dispatch({"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}})
    second = dispatcher.dispatch({"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}})

    assert first.success is True
    assert second.success is True
    assert (desktop / "Orbit").is_dir()


def test_create_folder_at_explicit_absolute_location_creates_missing_parents(
    dispatcher: ToolDispatcher, tmp_path: Path
) -> None:
    custom_root = tmp_path / "custom_root"  # henüz mevcut değil

    result = dispatcher.dispatch(
        {"tool": "filesystem.create_folder", "arguments": {"name": "Alt", "location": str(custom_root)}}
    )

    assert result.success is True
    assert (custom_root / "Alt").is_dir()


def test_create_folder_rejects_absolute_name(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    """`name` mutlak bir yol verirse (örn. sistem klasörü), `location`
    dışında bir yerde klasör oluşturulmamalı."""

    outside = tmp_path / "DisaridakiKlasor"

    result = dispatcher.dispatch(
        {"tool": "filesystem.create_folder", "arguments": {"name": str(outside)}}
    )

    assert result.success is False
    assert not outside.exists()


# --- filesystem.create_file ---


def test_create_file_writes_given_content(dispatcher: ToolDispatcher, desktop: Path) -> None:
    result = dispatcher.dispatch(
        {"tool": "filesystem.create_file", "arguments": {"name": "notes.txt", "content": "merhaba dünya"}}
    )

    assert result.success is True
    assert (desktop / "notes.txt").read_text(encoding="utf-8") == "merhaba dünya"


def test_create_file_default_content_is_empty_string(dispatcher: ToolDispatcher, desktop: Path) -> None:
    result = dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "bos.txt"}})

    assert result.success is True
    assert (desktop / "bos.txt").read_text(encoding="utf-8") == ""


def test_create_file_creates_missing_parent_location(dispatcher: ToolDispatcher, desktop: Path) -> None:
    nested_location = desktop / "AltKlasor"  # henüz mevcut değil

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.create_file",
            "arguments": {"name": "app.py", "location": str(nested_location)},
        }
    )

    assert result.success is True
    assert (nested_location / "app.py").exists()


def test_create_file_rejects_absolute_name(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    outside = tmp_path / "disaridaki.txt"

    result = dispatcher.dispatch(
        {"tool": "filesystem.create_file", "arguments": {"name": str(outside), "content": "x"}}
    )

    assert result.success is False
    assert not outside.exists()


def test_create_file_relative_subpath_name_still_works(dispatcher: ToolDispatcher, desktop: Path) -> None:
    """Aşırı kısıtlama yapmadığının kanıtı: `name` içindeki meşru göreli
    alt yol (var olan bir alt klasör altında) hâlâ çalışmalı."""

    (desktop / "AltKlasor").mkdir()

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.create_file",
            "arguments": {"name": "AltKlasor/dosya.txt", "content": "merhaba"},
        }
    )

    assert result.success is True
    assert (desktop / "AltKlasor" / "dosya.txt").read_text(encoding="utf-8") == "merhaba"


# --- filesystem.search ---


def test_search_matches_are_case_insensitive(dispatcher: ToolDispatcher, desktop: Path) -> None:
    (desktop / "Rapor2024.txt").write_text("x", encoding="utf-8")
    (desktop / "diger.txt").write_text("x", encoding="utf-8")

    result = dispatcher.dispatch({"tool": "filesystem.search", "arguments": {"query": "rapor"}})

    assert result.success is True
    assert result.data is not None
    assert len(result.data["matches"]) == 1
    assert "Rapor2024.txt" in result.data["matches"][0]


def test_search_returns_empty_matches_when_nothing_found(dispatcher: ToolDispatcher, desktop: Path) -> None:
    (desktop / "diger.txt").write_text("x", encoding="utf-8")

    result = dispatcher.dispatch({"tool": "filesystem.search", "arguments": {"query": "kesinlikle-yok"}})

    assert result.success is True
    assert result.data == {"matches": []}


def test_search_missing_base_location_fails(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    missing = tmp_path / "yok-boyle-bir-klasor"

    result = dispatcher.dispatch(
        {"tool": "filesystem.search", "arguments": {"query": "x", "location": str(missing)}}
    )

    assert result.success is False


# --- filesystem.copy ---


def test_copy_file_to_downloads_keeps_original(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.copy",
            "arguments": {"target": "kaynak.txt", "destination_location": "downloads"},
        }
    )

    assert result.success is True
    assert source.exists()  # kopyalama, taşıma değil
    copied = downloads / "kaynak.txt"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "içerik"

    # Mesaj sesli okunacağı için hedef KLASÖR adını söylemeli, tam yol
    # içermemeli (bkz. `core/voice_loop.py::speakable`).
    assert "Downloads" in result.message
    assert ":\\" not in result.message

    # Ayrıntı kaybolmaz: tam yollar `data` alanında kalır.
    assert result.data["source"] == str(source)
    assert result.data["destination"] == str(copied)


def test_copy_folder_recursively_includes_nested_file(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    source_dir = desktop / "Proje"
    source_dir.mkdir()
    (source_dir / "dosya.txt").write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.copy",
            "arguments": {"target": "Proje", "destination_location": "downloads"},
        }
    )

    assert result.success is True
    assert (downloads / "Proje" / "dosya.txt").exists()


def test_copy_missing_source_fails_without_creating_destination(
    dispatcher: ToolDispatcher, downloads: Path
) -> None:
    result = dispatcher.dispatch(
        {
            "tool": "filesystem.copy",
            "arguments": {"target": "olmayan.txt", "destination_location": "downloads"},
        }
    )

    assert result.success is False
    assert not (downloads / "olmayan.txt").exists()


def test_copy_rejects_absolute_target(
    dispatcher: ToolDispatcher, tmp_path: Path, downloads: Path
) -> None:
    outside = tmp_path / "gizli-kaynak.txt"
    outside.write_text("hassas", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.copy",
            "arguments": {"target": str(outside), "destination_location": "downloads"},
        }
    )

    assert result.success is False
    assert not (downloads / "gizli-kaynak.txt").exists()


def test_copy_onto_existing_target_fails_by_default_and_keeps_existing_content(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    """Düzeltme 2: hedefte aynı isimde bir dosya varsa, `overwrite`
    gönderilmediği sürece sessizce üzerine yazılmamalı."""

    source = desktop / "kaynak.txt"
    source.write_text("yeni içerik", encoding="utf-8")
    existing = downloads / "kaynak.txt"
    existing.write_text("eski (korunmali) içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.copy",
            "arguments": {"target": "kaynak.txt", "destination_location": "downloads"},
        }
    )

    assert result.success is False
    assert existing.read_text(encoding="utf-8") == "eski (korunmali) içerik"


def test_copy_with_overwrite_true_replaces_existing_target(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("yeni içerik", encoding="utf-8")
    existing = downloads / "kaynak.txt"
    existing.write_text("eski içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.copy",
            "arguments": {
                "target": "kaynak.txt",
                "destination_location": "downloads",
                "overwrite": True,
            },
        }
    )

    assert result.success is True
    assert existing.read_text(encoding="utf-8") == "yeni içerik"


# --- filesystem.rename ---


def test_rename_file_changes_name_keeps_content(dispatcher: ToolDispatcher, desktop: Path) -> None:
    source = desktop / "eski.txt"
    source.write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.rename", "arguments": {"target": "eski.txt", "name": "yeni.txt"}}
    )

    assert result.success is True
    assert not source.exists()
    renamed = desktop / "yeni.txt"
    assert renamed.exists()
    assert renamed.read_text(encoding="utf-8") == "içerik"
    assert result.data["path"] == str(renamed)


def test_rename_folder(dispatcher: ToolDispatcher, desktop: Path) -> None:
    source = desktop / "EskiKlasor"
    source.mkdir()
    (source / "dosya.txt").write_text("x", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.rename", "arguments": {"target": "EskiKlasor", "name": "YeniKlasor"}}
    )

    assert result.success is True
    assert not source.exists()
    assert (desktop / "YeniKlasor" / "dosya.txt").exists()


def test_rename_missing_source_fails(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch(
        {"tool": "filesystem.rename", "arguments": {"target": "olmayan.txt", "name": "yeni.txt"}}
    )

    assert result.success is False


def test_rename_rejects_absolute_target(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    outside = tmp_path / "gizli.txt"
    outside.write_text("hassas", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.rename", "arguments": {"target": str(outside), "name": "yeni.txt"}}
    )

    assert result.success is False
    assert outside.exists()  # dokunulmamalı


def test_rename_rejects_traversal_in_new_name(dispatcher: ToolDispatcher, desktop: Path) -> None:
    """Yeni ad da `_safe_join`'den geçmeli — aksi halde `Path.rename()`'e
    verilen bir `../` dizin dışına taşırdı (bkz. tool docstring'i)."""

    source = desktop / "dosya.txt"
    source.write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.rename", "arguments": {"target": "dosya.txt", "name": "../disari.txt"}}
    )

    assert result.success is False
    assert source.exists()  # eski ad hâlâ duruyor, taşınmadı


def test_rename_onto_existing_name_fails_by_default(dispatcher: ToolDispatcher, desktop: Path) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("yeni", encoding="utf-8")
    existing = desktop / "hedef.txt"
    existing.write_text("korunmalı", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.rename", "arguments": {"target": "kaynak.txt", "name": "hedef.txt"}}
    )

    assert result.success is False
    assert existing.read_text(encoding="utf-8") == "korunmalı"
    assert source.exists()


def test_rename_onto_existing_name_with_overwrite_replaces(dispatcher: ToolDispatcher, desktop: Path) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("yeni", encoding="utf-8")
    existing = desktop / "hedef.txt"
    existing.write_text("eski", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.rename",
            "arguments": {"target": "kaynak.txt", "name": "hedef.txt", "overwrite": True},
        }
    )

    assert result.success is True
    assert existing.read_text(encoding="utf-8") == "yeni"
    assert not source.exists()


def test_rename_to_same_name_is_a_safe_noop(dispatcher: ToolDispatcher, desktop: Path) -> None:
    """REGRESYON: kaynak/hedef aynı yola çözülüyorsa (yeni ad = eski ad),
    "overwrite" dalı kaynağı SİLİP sonra yeniden adlandırmaya çalışmamalı
    — bu veri kaybına yol açardı (bkz. tool'daki koruma yorumu)."""

    source = desktop / "dosya.txt"
    source.write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.rename",
            "arguments": {"target": "dosya.txt", "name": "dosya.txt", "overwrite": True},
        }
    )

    assert result.success is True
    assert source.exists()
    assert source.read_text(encoding="utf-8") == "içerik"


# --- filesystem.move ---


def test_move_file_removes_source_and_creates_destination(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.move",
            "arguments": {"target": "kaynak.txt", "destination_location": "downloads"},
        }
    )

    assert result.success is True
    assert not source.exists()  # taşıma: kopyalamadan farklı olarak kaynakta iz kalmaz
    moved = downloads / "kaynak.txt"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8") == "içerik"
    assert result.data["destination"] == str(moved)


def test_move_folder_recursively(dispatcher: ToolDispatcher, desktop: Path, downloads: Path) -> None:
    source_dir = desktop / "Proje"
    source_dir.mkdir()
    (source_dir / "dosya.txt").write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.move", "arguments": {"target": "Proje", "destination_location": "downloads"}}
    )

    assert result.success is True
    assert not source_dir.exists()
    assert (downloads / "Proje" / "dosya.txt").exists()


def test_move_missing_source_fails(dispatcher: ToolDispatcher, downloads: Path) -> None:
    result = dispatcher.dispatch(
        {
            "tool": "filesystem.move",
            "arguments": {"target": "olmayan.txt", "destination_location": "downloads"},
        }
    )

    assert result.success is False


def test_move_rejects_absolute_target(dispatcher: ToolDispatcher, tmp_path: Path, downloads: Path) -> None:
    outside = tmp_path / "gizli.txt"
    outside.write_text("hassas", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.move",
            "arguments": {"target": str(outside), "destination_location": "downloads"},
        }
    )

    assert result.success is False
    assert outside.exists()


def test_move_onto_existing_target_fails_by_default(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("yeni", encoding="utf-8")
    existing = downloads / "kaynak.txt"
    existing.write_text("korunmalı", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.move",
            "arguments": {"target": "kaynak.txt", "destination_location": "downloads"},
        }
    )

    assert result.success is False
    assert existing.read_text(encoding="utf-8") == "korunmalı"
    assert source.exists()  # başarısız taşıma kaynağı silmemeli


def test_move_with_overwrite_true_replaces_existing_target(
    dispatcher: ToolDispatcher, desktop: Path, downloads: Path
) -> None:
    source = desktop / "kaynak.txt"
    source.write_text("yeni", encoding="utf-8")
    existing = downloads / "kaynak.txt"
    existing.write_text("eski", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.move",
            "arguments": {"target": "kaynak.txt", "destination_location": "downloads", "overwrite": True},
        }
    )

    assert result.success is True
    assert existing.read_text(encoding="utf-8") == "yeni"
    assert not source.exists()


def test_move_to_same_location_is_a_safe_noop(dispatcher: ToolDispatcher, desktop: Path) -> None:
    """REGRESYON: kaynak/hedef aynı yola çözülüyorsa (aynı konuma "taşı"),
    kaynağı silip sonra taşımaya çalışmamalı — veri kaybı olurdu."""

    source = desktop / "dosya.txt"
    source.write_text("içerik", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.move",
            "arguments": {"target": "dosya.txt", "destination_location": "desktop", "overwrite": True},
        }
    )

    assert result.success is True
    assert source.exists()
    assert source.read_text(encoding="utf-8") == "içerik"


# --- filesystem.delete (CONFIRM_REQUIRED) ---


def test_delete_without_confirmation_is_blocked_and_file_survives(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    target = desktop / "gizli.txt"
    target.write_text("x", encoding="utf-8")

    result = dispatcher.dispatch({"tool": "filesystem.delete", "arguments": {"target": "gizli.txt"}})

    assert result.requires_confirmation is True
    assert result.success is False
    assert target.exists()  # asıl güvenlik garantisi: onaysız silme dosyaya dokunmamalı


def test_delete_with_confirmation_removes_file(dispatcher: ToolDispatcher, desktop: Path) -> None:
    target = desktop / "gizli.txt"
    target.write_text("x", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.delete", "arguments": {"target": "gizli.txt"}}, confirmed=True
    )

    assert result.success is True
    assert not target.exists()


def test_delete_with_confirmation_removes_folder_recursively(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    target_dir = desktop / "SilinecekKlasor"
    target_dir.mkdir()
    (target_dir / "icindeki.txt").write_text("x", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.delete", "arguments": {"target": "SilinecekKlasor"}}, confirmed=True
    )

    assert result.success is True
    assert not target_dir.exists()


def test_delete_missing_target_fails_even_when_confirmed(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch(
        {"tool": "filesystem.delete", "arguments": {"target": "olmayan.txt"}}, confirmed=True
    )

    assert result.success is False
    assert result.requires_confirmation is False


def test_delete_rejects_absolute_target_and_file_survives(
    dispatcher: ToolDispatcher, tmp_path: Path
) -> None:
    """Düzeltme 1'in asıl güvenlik garantisi: `target` mutlak bir sistem
    yolu olsa bile (`location`'ın dışında), `confirmed=True` geçilse dahi
    o dosyaya dokunulmamalı. `confirmed=True` bilerek kullanılıyor ki
    testin geçmesi onay kapısına değil, `_safe_join` reddine bağlı olsun.
    """

    outside_target = tmp_path / "sistem_dosyasi.txt"
    outside_target.write_text("dokunulmamali", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.delete",
            "arguments": {"target": str(outside_target), "location": "desktop"},
        },
        confirmed=True,
    )

    assert result.success is False
    assert result.requires_confirmation is False
    assert outside_target.exists()
    assert outside_target.read_text(encoding="utf-8") == "dokunulmamali"


def test_delete_rejects_parent_traversal_target_and_file_survives(
    dispatcher: ToolDispatcher, desktop: Path, tmp_path: Path
) -> None:
    """`target` içinde ".." kullanılarak `location`'ın üstüne çıkılmaya
    çalışılması da (mutlak yol olmasa dahi) reddedilmeli."""

    outside_target = tmp_path / "disaridaki.txt"
    outside_target.write_text("dokunulmamali", encoding="utf-8")

    result = dispatcher.dispatch(
        {
            "tool": "filesystem.delete",
            "arguments": {"target": "../disaridaki.txt", "location": str(desktop)},
        },
        confirmed=True,
    )

    assert result.success is False
    assert result.requires_confirmation is False
    assert outside_target.exists()


def test_delete_rejects_target_dot_and_desktop_survives_intact(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    """EN KRİTİK REGRESYON TESTİ.

    Düzeltme öncesi doğrulanan gerçek açık: `target="."` (yani `location`'ın
    KENDİSİ) + `confirmed=True` gönderildiğinde, pathlib'de `base / "."`
    doğrudan `base`'e sadeleştiği için `filesystem.delete` `location`
    klasörünün TAMAMINI — gerçek kullanımda tüm masaüstünü — içindeki her
    şeyle birlikte siliyordu (`success=True` dönüyordu). Bu test hem
    çağrının artık reddedildiğini hem de `desktop` klasörünün, içindeki iki
    dosyayla birlikte, silme denemesinden önceki hâliyle bozulmadan diskte
    durduğunu doğrular.
    """

    (desktop / "onemli1.txt").write_text("kaybolmamali-1", encoding="utf-8")
    (desktop / "onemli2.txt").write_text("kaybolmamali-2", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.delete", "arguments": {"target": ".", "location": "desktop"}},
        confirmed=True,
    )

    assert result.success is False
    assert result.requires_confirmation is False
    assert desktop.exists()  # masaüstünün kendisi silinmemiş olmalı
    assert sorted(p.name for p in desktop.iterdir()) == ["onemli1.txt", "onemli2.txt"]


def test_delete_rejects_empty_string_and_dot_slash_targets(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    """`""` ve `"./"` de `"."` ile aynı şekilde `base`'e sadeleşir
    (`Path("")` ve `Path("./")` pathlib'de `Path(".")` ile eşdeğerdir);
    LLM'in ürettiği daha az sık ama eşit derecede tehlikeli bu varyantlar
    da aynı kuralla yakalanmalı."""

    survivor = desktop / "onemli.txt"
    survivor.write_text("kaybolmamali", encoding="utf-8")

    for target in ("", "./"):
        result = dispatcher.dispatch(
            {"tool": "filesystem.delete", "arguments": {"target": target, "location": "desktop"}},
            confirmed=True,
        )
        assert result.success is False, f"target={target!r} reddedilmeliydi"
        assert result.requires_confirmation is False
        assert survivor.exists()


def test_delete_legitimate_relative_subpath_still_works(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    """Aşırı kısıtlama yapmadığının kanıtı: `AltKlasor/dosya.txt` gibi
    meşru göreli bir alt yol hâlâ silinebilmeli."""

    nested = desktop / "AltKlasor"
    nested.mkdir()
    nested_file = nested / "dosya.txt"
    nested_file.write_text("x", encoding="utf-8")

    result = dispatcher.dispatch(
        {"tool": "filesystem.delete", "arguments": {"target": "AltKlasor/dosya.txt"}},
        confirmed=True,
    )

    assert result.success is True
    assert not nested_file.exists()


# --- _resolve_location ---


def test_resolve_location_desktop_alias(context: ToolContext) -> None:
    assert _resolve_location("desktop", context) == context.settings.desktop_path


def test_resolve_location_downloads_alias(context: ToolContext) -> None:
    assert _resolve_location("downloads", context) == context.settings.downloads_path


def test_resolve_location_last_falls_back_to_home_when_memory_empty(context: ToolContext) -> None:
    assert context.memory.get_last_path() is None  # bu testte hiç tool çalıştırılmadı
    assert _resolve_location("last", context) == Path.home()


def test_resolve_location_arbitrary_absolute_path_passthrough(context: ToolContext, tmp_path: Path) -> None:
    custom = tmp_path / "baska_bir_yer"

    assert _resolve_location(str(custom), context) == custom


def test_resolve_location_expands_user_home_shortcut(context: ToolContext) -> None:
    assert _resolve_location("~", context) == Path.home()


# --- _safe_join ---
#
# `_resolve_location` testlerine paralel olarak, dispatcher/ToolContext
# kurmadan `_safe_join`'i doğrudan çağırıp Düzeltme 1'in çekirdek kuralını
# (mutlak yol / ".." reddi, meşru göreli alt yola izin) birim düzeyinde
# doğrular. Uçtan uca (dispatcher üzerinden) kanıt için yukarıdaki
# `filesystem.delete`/`filesystem.copy` testlerine bakınız.


def test_safe_join_rejects_absolute_windows_path(tmp_path: Path) -> None:
    assert _safe_join(tmp_path, "C:/Windows/System32") is None


def test_safe_join_rejects_drive_relative_path(tmp_path: Path) -> None:
    """Sürücü harfi içeren ama "kök" içermeyen sınır durum (`Path.is_absolute()`
    False döner ama yine de `base`'i atlayabilir) da reddedilmeli."""

    assert _safe_join(tmp_path, "C:tmp") is None


def test_safe_join_rejects_rooted_path_without_drive(tmp_path: Path) -> None:
    """Sürücü olmadan sadece kökle başlayan yol (`\\Windows\\...`) da
    `base`'in dışına (o an aktif sürücünün köküne) çıkabildiği için reddedilmeli."""

    assert _safe_join(tmp_path, "\\Windows\\System32") is None


def test_safe_join_rejects_parent_traversal(tmp_path: Path) -> None:
    assert _safe_join(tmp_path, "../disaridaki.txt") is None


def test_safe_join_rejects_parent_traversal_in_the_middle(tmp_path: Path) -> None:
    assert _safe_join(tmp_path, "AltKlasor/../../disaridaki.txt") is None


def test_safe_join_allows_simple_name(tmp_path: Path) -> None:
    assert _safe_join(tmp_path, "dosya.txt") == tmp_path / "dosya.txt"


def test_safe_join_allows_legitimate_relative_subpath(tmp_path: Path) -> None:
    assert _safe_join(tmp_path, "Orbit/app.py") == tmp_path / "Orbit" / "app.py"


def test_safe_join_rejects_targets_that_collapse_to_base(tmp_path: Path) -> None:
    """Düzeltme 3'ün çekirdek kuralı: boş dize, "." ve "./" pathlib'de
    parçasız (`candidate.parts == ()`) bir yola karşılık gelir ve
    `base / candidate` doğrudan `base`'in KENDİSİNE eşitlenir — yani
    `location`'ın kendisini hedefler. Uçtan uca kanıt (silinen tüm masaüstü
    senaryosu) için yukarıdaki `test_delete_rejects_target_dot_and_desktop_survives_intact`
    ve `test_delete_rejects_empty_string_and_dot_slash_targets`'a bakınız."""

    assert _safe_join(tmp_path, "") is None
    assert _safe_join(tmp_path, ".") is None
    assert _safe_join(tmp_path, "./") is None


def test_safe_join_still_allows_legitimate_targets(tmp_path: Path) -> None:
    """Aşırı kısıtlama yapmadığının kanıtı: Düzeltme 3, `base`'in KENDİSİNE
    sadeleşmeyen (yani en az bir parçası olan) hiçbir meşru göreli hedefi
    etkilememeli."""

    assert _safe_join(tmp_path, "Orbit") == tmp_path / "Orbit"
    assert _safe_join(tmp_path, "Orbit/app.py") == tmp_path / "Orbit" / "app.py"
    assert _safe_join(tmp_path, "AltKlasor/dosya.txt") == tmp_path / "AltKlasor" / "dosya.txt"


# --- location: "last" bağlam hafızası akışı (uçtan uca) ---


def test_last_location_resolves_to_previously_created_folder(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    create_result = dispatcher.dispatch(
        {"tool": "filesystem.create_folder", "arguments": {"name": "Orbit"}}
    )
    assert create_result.success is True

    follow_up = dispatcher.dispatch(
        {
            "tool": "filesystem.create_file",
            "arguments": {"name": "app.py", "location": "last"},
        }
    )

    assert follow_up.success is True
    assert (desktop / "Orbit" / "app.py").exists()


def test_last_location_updates_after_each_remembering_tool_call(
    dispatcher: ToolDispatcher, desktop: Path
) -> None:
    dispatcher.dispatch({"tool": "filesystem.create_folder", "arguments": {"name": "Once"}})
    dispatcher.dispatch({"tool": "filesystem.create_folder", "arguments": {"name": "Sonra"}})

    follow_up = dispatcher.dispatch(
        {"tool": "filesystem.create_file", "arguments": {"name": "x.txt", "location": "last"}}
    )

    assert follow_up.success is True
    assert (desktop / "Sonra" / "x.txt").exists()
    assert not (desktop / "Once" / "x.txt").exists()


def test_safe_join_rejects_backslash_traversal_on_every_platform() -> None:
    """`..` ters bölü ile yazıldığında da yakalanmalı.

    POSIX'te `Path("AltKlasor\\..\\..\\x")` TEK bir parçadır — `..` hiç
    görünmez. Bu koruma bir dönem yalnızca `Path` kullanıyordu, yani
    doğruluğu çalıştığı işletim sistemine bağlıydı. Bir güvenlik
    kontrolünün platforma bağlı olmaması gerekir.
    """

    assert _safe_join(Path("/tmp/base"), "AltKlasor\\..\\..\\disaridaki.txt") is None


def test_safe_join_rejects_windows_absolute_path_on_every_platform() -> None:
    """POSIX'te `Path("C:/x")` mutlak DEĞİLDİR; yine de reddedilmeli."""

    assert _safe_join(Path("/tmp/base"), "C:/Windows/System32") is None
    assert _safe_join(Path("/tmp/base"), "C:tmp") is None
