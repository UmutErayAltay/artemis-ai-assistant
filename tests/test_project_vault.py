"""`projects/vault.py` testleri: gerçek dosya sistemi, gerçek alt süreç.

Hiçbir şey mock'lanmaz: vault `tmp_path` altında gerçek dosyalardan kurulur ve
köprü, `tests/sahte_beyin.py`'nin yazdığı gerçek bir betiği alt süreç olarak
çalıştırır. İki yerde `Path` yöntemleri YALNIZCA bir müdahale noktası olarak
sarılır (gerçek çağrı yine yapılır), bkz. `test_existing_note_is_never_rewritten`.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pytest

from projects.vault import (
    NEW_NOTE_BANNER,
    PROJECT_LOG_HEADING,
    VaultBridge,
    VaultContext,
    VaultNote,
    VaultRecord,
)
from tests.sahte_beyin import SahteVault, sahte_vault


@pytest.fixture(autouse=True)
def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`Path.home()`'u geçici bir klasöre yönlendirir.

    Köprü proje dizinini ev dizininin altındaysa `~/...` yazar; testlerin sonucu
    çalıştığı makinenin gerçek ev dizinine (ör. kök `/` olan bir konteyner) bağlı olmasın.
    """
    home = tmp_path / "ev" / "kullanici-adi"
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _outcome(bridge: VaultBridge, **overrides: object) -> VaultRecord | None:
    """`record_outcome`'ı makul varsayılanlarla çağırır; testler yalnızca ilgilendiğini verir."""
    arguments: dict[str, object] = {
        "slug": "not-defteri",
        "title": "Not Defteri",
        "status_label": "TAMAM",
        "summary": "Özellik eklendi",
        "project_dir": Path("/projeler/not-defteri"),
        "spec_markdown": None,
        "event_key": "k1",
    }
    arguments.update(overrides)
    return bridge.record_outcome(**arguments)  # type: ignore[arg-type]


def _bridge(sv: SahteVault, **kwargs: object) -> VaultBridge:
    return VaultBridge(vault_path=sv.path, command=sv.command, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #
# 1. Pasif köprü
# ---------------------------------------------------------------------- #


def test_disabled_bridge_returns_none_and_never_calls_cli(tmp_path: Path) -> None:
    """`vault_path` None / var olmayan dizin / dosya → enabled False, her şey None, CLI log'u boş."""
    sv = sahte_vault(tmp_path)
    a_file = tmp_path / "dizin-degil.txt"
    a_file.write_text("x", encoding="utf-8")

    for vault_path in (None, tmp_path / "yok", a_file):
        bridge = VaultBridge(vault_path=vault_path, command=sv.command)
        assert bridge.enabled is False
        assert bridge.context_for("test") is None
        assert _outcome(bridge) is None

    assert sv.calls() == []


# ---------------------------------------------------------------------- #
# 2. context_for: tercihler, filtre, özet, argv
# ---------------------------------------------------------------------- #


def test_context_for_preferences_filtering_and_excerpts(tmp_path: Path) -> None:
    long_text = "# Uzun Not\n\n" + " ".join(["kelime"] * 200)
    records = [
        {"source": "knowledge/concepts/rls.md", "text": "# RLS\n\nRow Level Security gerekli."},
        {"source": "daily/import-2026-07-part-023.md", "text": "# Günlük\n\nBugün iş yaptım."},
        {"source": "receipts/abc.md", "text": "# Receipt\n\nKanıt."},
        {"source": "knowledge/index.md", "text": "# Index\n\nListe."},
        {"source": "knowledge/log.md", "text": "# Log\n\nKayıt."},
        {"source": "🔮 850-Companion/Threads.md", "text": "# Threads\n\nKonuşma."},
        {"source": "", "text": "# Kaynaksız\n\nAtlanmalı."},
        {
            "source": "knowledge/concepts/clean-arch.md",
            "text": "---\ntitle: Clean\ntags: [x]\n---\n# Clean Architecture\n\nBağımlılık kuralı.\n   Tek   satır.",
        },
        {"source": "knowledge/concepts/bos.md", "text": "# Sadece başlık\n\n   \n## Alt başlık\n"},
        {"source": "🏰 300-Projects/sosyal-medya.md", "text": long_text},
    ]
    sv = sahte_vault(tmp_path, records=records)

    ctx = _bridge(sv).context_for("RLS nedir?", max_notes=10)

    assert ctx is not None
    # Tercihler: iki madde (ikincinin girintili devam satırıyla); üst bilgi cümlesi, HTML yorumu
    # ve sonraki bölüm yok.
    assert ctx.preferences == (
        "- Tercih 1: Kısa ve net cevaplar.\n- Tercih 2: Türkçe yanıtlamak.\n  Teknik terimler İngilizce kalabilir."
    )
    assert "Her madde" not in ctx.preferences
    assert "kurallar-gerekce" not in ctx.preferences
    assert "<!--" not in ctx.preferences
    assert "HTML yorumu" not in ctx.preferences
    assert "Başka bölüm" not in ctx.preferences

    # Yalnızca bilgi taşıyan kaynaklar; gürültü ve boş özetler elendi.
    assert ctx.sources() == [
        "knowledge/concepts/rls.md",
        "knowledge/concepts/clean-arch.md",
        "🏰 300-Projects/sosyal-medya.md",
    ]
    excerpts = {note.source: note.excerpt for note in ctx.notes}
    assert excerpts["knowledge/concepts/rls.md"] == "Row Level Security gerekli."
    assert excerpts["knowledge/concepts/clean-arch.md"] == "Bağımlılık kuralı. Tek satır."
    for note in ctx.notes:
        assert len(note.excerpt) <= 300
        assert "\n" not in note.excerpt
        assert "---" not in note.excerpt
    long_excerpt = excerpts["🏰 300-Projects/sosyal-medya.md"]
    assert len(long_excerpt) == 300
    assert long_excerpt.endswith("…")
    assert "Uzun Not" not in long_excerpt

    # CLI doğru argümanlarla ve vault kökünde çağrıldı.
    (call,) = sv.calls()
    assert set(call) == {"argv", "cwd", "receipt", "receipt_path"}
    assert call["argv"] == ["context", "--no-sync", "--limit", "40", "--budget-chars", "200000", "RLS nedir?"]
    assert Path(call["cwd"]).resolve() == sv.path.resolve()
    assert call["receipt"] is None
    assert call["receipt_path"] is None


def test_context_for_strips_the_cli_truncation_marker(tmp_path: Path) -> None:
    """CLI bütçesi biten kayıtların metnini " [truncated]" ile bitirir; bu işaret özete girmemeli."""
    long_cut = "# Uzun\n\n" + " ".join(["kelime"] * 100) + " [truncated]"
    records = [
        {"source": "knowledge/concepts/kisa.md", "text": "# Kısa\n\nİlk cümle yarım kal [truncated]"},
        {"source": "knowledge/concepts/satirda.md", "text": "# Satırda\n\nSatır sonundan sonra\n[truncated]"},
        {"source": "knowledge/concepts/uzun.md", "text": long_cut},
        {"source": "knowledge/concepts/tam.md", "text": "# Tam\n\nİşaretsiz not."},
    ]
    sv = sahte_vault(tmp_path, records=records)

    ctx = _bridge(sv).context_for("q", max_notes=10)

    assert ctx is not None
    excerpts = {note.source: note.excerpt for note in ctx.notes}
    assert excerpts["knowledge/concepts/kisa.md"] == "İlk cümle yarım kal"
    assert excerpts["knowledge/concepts/satirda.md"] == "Satır sonundan sonra"
    assert excerpts["knowledge/concepts/tam.md"] == "İşaretsiz not."
    assert len(excerpts["knowledge/concepts/uzun.md"]) == 300
    assert excerpts["knowledge/concepts/uzun.md"].endswith("…")
    assert all("[truncated]" not in note.excerpt for note in ctx.notes)
    assert "[truncated]" not in ctx.to_prompt()
    # Geniş bütçe: ilk kayıt bütçeyi yiyip sonrakileri kırpmasın.
    (call,) = sv.calls()
    assert call["argv"][2:6] == ["--limit", "40", "--budget-chars", "200000"]


def test_preferences_keep_only_list_items_and_their_indented_continuations() -> None:
    content = (
        "# Core\n\n## What I should never forget\n"
        "Her madde Umut'un söylediği bir kuraldır (kaynak: [[Thread-Detay/kurallar-gerekce]]).\n\n"
        "- Madde bir\n  devam satırı\n"
        "* Madde iki\n"
        "Ara paragraf.\n  girintili ama bir maddeye ait değil\n"
        "- Madde üç\n    - alt madde\n\n"
        "## Sonraki\n- dışarıda\n"
    )

    assert VaultBridge._extract_preferences(content) == (
        "- Madde bir\n  devam satırı\n* Madde iki\n- Madde üç\n    - alt madde"
    )
    assert VaultBridge._extract_preferences("## What I should never forget\nYalnızca üst bilgi.\n") == ""


def test_context_for_respects_max_notes(tmp_path: Path) -> None:
    records = [{"source": f"knowledge/concepts/n{i}.md", "text": f"Not {i} içeriği"} for i in range(6)]
    sv = sahte_vault(tmp_path, records=records)
    bridge = _bridge(sv)

    two = bridge.context_for("q", max_notes=2)
    default = bridge.context_for("q")

    assert two is not None and default is not None
    assert two.sources() == ["knowledge/concepts/n0.md", "knowledge/concepts/n1.md"]
    assert len(default.notes) == 4


def test_context_for_without_core_md_still_returns_context(tmp_path: Path) -> None:
    """Core.md yoksa tercihler boş, notlar yine gelir."""
    sv = sahte_vault(tmp_path, records=[{"source": "knowledge/concepts/a.md", "text": "içerik"}])
    (sv.path / "🔮 850-Companion" / "Core.md").unlink()

    ctx = _bridge(sv).context_for("q")

    assert ctx is not None
    assert ctx.preferences == ""
    assert ctx.sources() == ["knowledge/concepts/a.md"]


# ---------------------------------------------------------------------- #
# 3. CLI hataları
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("options", "timeout"),
    [
        pytest.param({"context_exit": 1}, 5.0, id="sifir-olmayan-cikis"),
        pytest.param({"context_stdout": "bu json değil"}, 5.0, id="cop-stdout"),
        pytest.param({"context_stdout": "[1, 2]"}, 5.0, id="nesne-olmayan-json"),
        pytest.param({"context_stdout": ""}, 5.0, id="bos-stdout"),
        pytest.param({"sleep_seconds": 2.0}, 0.5, id="zaman-asimi"),
    ],
)
def test_context_for_cli_failure_keeps_preferences(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, options: dict[str, object], timeout: float
) -> None:
    sv = sahte_vault(tmp_path, **options)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="projects.vault"):
        ctx = _bridge(sv, timeout_seconds=timeout).context_for("test")

    assert ctx is not None
    assert ctx.notes == ()
    assert "Tercih 1" in ctx.preferences
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    # Sahte CLI çağrıyı bekleme/yanıttan önce loglar: zaman aşımında bile çağrı görünür.
    assert [call["argv"][0] for call in sv.calls()] == ["context"]


def test_cli_cannot_start_returns_preferences_only(tmp_path: Path) -> None:
    """Komut hiç başlatılamıyorsa (OSError) da exception yok."""
    sv = sahte_vault(tmp_path)
    bridge = VaultBridge(vault_path=sv.path, command=[str(tmp_path / "yok" / "beyin")])

    ctx = bridge.context_for("test")

    assert ctx is not None
    assert ctx.notes == ()
    assert "Tercih 1" in ctx.preferences


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param('{"records": ["metin", 3, null]}', id="nesne-olmayan-kayitlar"),
        pytest.param('{"records": "liste değil"}', id="records-liste-degil"),
        pytest.param('{"records": {"source": "knowledge/concepts/a.md"}}', id="records-nesne"),
        pytest.param(
            '{"records": [{"source": 5, "text": "x"}, {"source": "knowledge/concepts/a.md", "text": 7}]}',
            id="tip-hatali-alanlar",
        ),
        pytest.param("{}", id="records-yok"),
    ],
)
def test_context_for_malformed_records_are_ignored(tmp_path: Path, stdout: str) -> None:
    sv = sahte_vault(tmp_path, context_stdout=stdout)

    ctx = _bridge(sv).context_for("test")

    assert ctx is not None
    assert ctx.notes == ()
    assert "Tercih 1" in ctx.preferences


# ---------------------------------------------------------------------- #
# 4. to_prompt
# ---------------------------------------------------------------------- #


def test_to_prompt_exact_format() -> None:
    ctx = VaultContext(
        preferences="- Kısa cevap ver.",
        notes=(VaultNote("a.md", "bir"), VaultNote("b.md", "iki")),
    )

    # Başlık hemen içeriğiyle başlar, notlar ardışıktır, yalnız iki bölüm arasında bir boş satır vardır.
    assert ctx.to_prompt() == (
        "Umut'un kalıcı tercihleri (Core.md):\n- Kısa cevap ver.\n\nİlgili vault notları:\n- a.md: bir\n- b.md: iki"
    )
    assert ctx.sources() == ["a.md", "b.md"]
    assert not ctx.is_empty()


def test_to_prompt_omits_empty_parts_and_empty_context() -> None:
    assert VaultContext().is_empty()
    assert VaultContext().to_prompt() == ""
    assert VaultContext(preferences="p").to_prompt() == "Umut'un kalıcı tercihleri (Core.md):\np"
    only_notes = VaultContext(notes=(VaultNote("a.md", "bir"),))
    assert only_notes.to_prompt() == "İlgili vault notları:\n- a.md: bir"


# ---------------------------------------------------------------------- #
# 5. record_outcome: yeni not
# ---------------------------------------------------------------------- #


def test_record_outcome_creates_new_note_and_sends_receipt(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)
    project_dir = Path("/projeler/not-defteri")
    summary = "Not ekleme özelliği eklendi ve testler geçti"

    record = _outcome(
        _bridge(sv),
        summary=summary,
        project_dir=project_dir,
        spec_markdown="# Spec\n\nBu bir spec.\nİkinci satır.",
        event_key="abc123",
    )

    assert record == VaultRecord(note="🏰 300-Projects/not-defteri.md", receipt_ok=True)

    # Dosyanın tamamı: frontmatter, tek başlık, uyarı, spec gövdesi (kendi başlığı olmadan), kayıt başlığı, giriş.
    content = (sv.path / record.note).read_text(encoding="utf-8")
    date = r"\d{4}-\d{2}-\d{2}"
    expected = (
        rf"---\ntitle: Not Defteri\ncreated: {date}\nmodified: {date}\ntype: project\nstatus: active\n"
        rf"tags: \[artemis, proje\]\n---\n"
        rf"# Not Defteri\n\n{re.escape(NEW_NOTE_BANNER)}\n\n"
        rf"Bu bir spec\.\nİkinci satır\.\n\n"
        rf"{re.escape(PROJECT_LOG_HEADING)}\n\n"
        rf"- {date} \d{{2}}:\d{{2}} · TAMAM · {re.escape(f'`{project_dir}`')} — {re.escape(summary)}\n"
    )
    assert re.fullmatch(expected, content), content
    assert content.count("# Not Defteri") == 1
    assert "# Spec" not in content

    # Receipt: CLI yükü kendi okudu.
    receipt_call = next(call for call in sv.calls() if call["argv"][0] == "receipt")
    assert set(receipt_call) == {"argv", "cwd", "receipt", "receipt_path"}
    assert Path(receipt_call["cwd"]).resolve() == sv.path.resolve()
    assert receipt_call["argv"][1] == "--file"
    assert receipt_call["argv"][3:] == ["--harness", "claude"]
    payload = receipt_call["receipt"]
    assert payload["event_id"] == "artemis-proje-abc123"
    assert payload["refs"] == [record.note]
    assert payload["summary"].startswith("Artemis proje atölyesi: 'not-defteri' TAMAM. " + summary)
    assert payload["summary"].endswith("\nÖğrenilen: yok")

    # Geçici dosya silindi ve vault içinde hiç açılmadı.
    receipt_path = Path(receipt_call["receipt_path"])
    assert not receipt_path.exists()
    assert sv.path.resolve() not in receipt_path.resolve().parents
    assert list(sv.path.rglob("*.json")) == []


def test_record_outcome_new_note_without_spec_and_with_risky_title(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)

    record = _outcome(_bridge(sv), title="Not: Defteri\nİkinci satır", spec_markdown="   \n")

    assert record is not None
    lines = (sv.path / record.note).read_text(encoding="utf-8").splitlines()
    # Satır sonu başka bir frontmatter anahtarı enjekte edemez; `: ` içeren başlık tırnaklanır.
    assert lines[1] == 'title: "Not: Defteri İkinci satır"'
    assert lines[8] == "# Not: Defteri İkinci satır"
    assert lines[9:12] == ["", NEW_NOTE_BANNER, ""]
    assert lines[12] == PROJECT_LOG_HEADING


_SPEC_WITH_BLOCKQUOTE = """# Not Defteri

> Bu dosya Artemis'in proje görüşmesinden üretildi; kodlayıcının sözleşmesidir.
> İkinci uyarı satırı.

## Amaç

Hızlı not almak.

> Spec'in içindeki gerçek bir alıntı kalmalı.
"""


def test_new_note_drops_the_specs_own_leading_blockquote(tmp_path: Path) -> None:
    """Spec'in başlık altındaki uyarısı atılır (notun kendi uyarısı var); gövdedeki alıntılar kalır."""
    sv = sahte_vault(tmp_path)

    record = _outcome(_bridge(sv), spec_markdown=_SPEC_WITH_BLOCKQUOTE)

    assert record is not None
    content = (sv.path / record.note).read_text(encoding="utf-8")
    assert "sözleşmesidir" not in content and "İkinci uyarı satırı" not in content
    assert content.count(NEW_NOTE_BANNER) == 1
    assert f"{NEW_NOTE_BANNER}\n\n## Amaç\n\nHızlı not almak." in content
    assert "> Spec'in içindeki gerçek bir alıntı kalmalı." in content


def test_entry_renders_project_dir_under_home_as_tilde(tmp_path: Path, _fake_home: Path) -> None:
    """Ev dizininin altındaki yol `~/...` yazılır: vault bir git deposu, kullanıcı adı sızmamalı."""
    sv = sahte_vault(tmp_path)
    bridge = _bridge(sv)
    outside = tmp_path / "baska" / "disarida"

    _outcome(bridge, slug="icerde", project_dir=_fake_home / "Desktop" / "Projeler" / "icerde")
    _outcome(bridge, slug="ev-kendisi", project_dir=_fake_home)
    _outcome(bridge, slug="disarida", project_dir=outside)

    inside_note = (sv.path / "🏰 300-Projects" / "icerde.md").read_text(encoding="utf-8")
    assert "`~/Desktop/Projeler/icerde`" in inside_note
    assert "kullanici-adi" not in inside_note
    assert "`~`" in (sv.path / "🏰 300-Projects" / "ev-kendisi.md").read_text(encoding="utf-8")
    # Ev dizininin dışındaki yol olduğu gibi kalır.
    assert f"`{outside}`" in (sv.path / "🏰 300-Projects" / "disarida.md").read_text(encoding="utf-8")


def test_entry_keeps_the_absolute_path_when_home_cannot_be_determined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    sv = sahte_vault(tmp_path)
    project_dir = tmp_path / "projeler" / "x"

    record = _outcome(_bridge(sv), project_dir=project_dir)

    assert record is not None
    assert f"`{project_dir}`" in (sv.path / record.note).read_text(encoding="utf-8")


# ---------------------------------------------------------------------- #
# 6. record_outcome: var olan kullanıcı notu
# ---------------------------------------------------------------------- #

_USER_NOTE = """---
title: Var Olan
created: 2026-01-01
modified: 2026-01-01
type: note
status: draft
tags: [kullanici]
---
# Var Olan

Kullanıcının kendi metni.
"""


def test_record_outcome_appends_to_existing_note(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)
    bridge = _bridge(sv)
    note_path = sv.path / "🏰 300-Projects" / "var-olan.md"
    note_path.write_text(_USER_NOTE, encoding="utf-8")
    original_bytes = note_path.read_bytes()
    original_inode = os.stat(note_path).st_ino

    first = _outcome(bridge, slug="var-olan", status_label="BASLADI", summary="İlk kayıt", event_key="e1")
    second = _outcome(bridge, slug="var-olan", status_label="BITTI", summary="İkinci kayıt", event_key="e2")

    assert first == VaultRecord("🏰 300-Projects/var-olan.md", True)
    assert second == VaultRecord("🏰 300-Projects/var-olan.md", True)
    # Ekleme modu: aynı dosya nesnesi, orijinal baytlar birebir önek.
    assert os.stat(note_path).st_ino == original_inode
    new_bytes = note_path.read_bytes()
    assert new_bytes.startswith(original_bytes)
    text = new_bytes.decode("utf-8")
    assert text.count(PROJECT_LOG_HEADING) == 1
    assert text.index("İlk kayıt") < text.index("İkinci kayıt")
    assert text.index("BASLADI") < text.index("BITTI")
    # Yeni içerik: boş satır, başlık, boş satır, iki giriş.
    tail = text[len(original_bytes.decode("utf-8")) :]
    assert tail.startswith(f"\n{PROJECT_LOG_HEADING}\n\n- ")
    assert tail.count("\n- ") == 2


def test_record_outcome_existing_note_without_trailing_newline_or_with_heading(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)
    bridge = _bridge(sv)
    projects = sv.path / "🏰 300-Projects"

    # Sonu satır sonuyla bitmeyen not: önce yalnızca bir "\n" eklenir.
    no_newline = projects / "satirsiz.md"
    no_newline.write_bytes(b"# Satirsiz\n\nson satir")
    _outcome(bridge, slug="satirsiz", summary="kayit")
    assert no_newline.read_bytes().startswith(b"# Satirsiz\n\nson satir\n\n" + PROJECT_LOG_HEADING.encode())

    # Kayıt başlığı zaten varsa tekrar eklenmez, giriş doğrudan altına gider.
    with_heading = projects / "baslikli.md"
    original = f"# Baslikli\n\n{PROJECT_LOG_HEADING}\n\n- eski giriş\n"
    with_heading.write_text(original, encoding="utf-8")
    _outcome(bridge, slug="baslikli", summary="yeni giriş")
    text = with_heading.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.count(PROJECT_LOG_HEADING) == 1
    assert text[len(original) :].startswith("- ")
    assert text.endswith("yeni giriş\n")


def test_existing_note_is_never_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Okuma ile yazma arasında kullanıcı notu düzenlerse düzenleme kaybolmamalı ve `write_text` çağrılmamalı."""
    sv = sahte_vault(tmp_path)
    note_path = sv.path / "🏰 300-Projects" / "var-olan.md"
    note_path.write_text(_USER_NOTE, encoding="utf-8")
    late_edit = "Obsidian'de sonradan yazılan satır.\n"

    real_read_text = Path.read_text
    real_write_text = Path.write_text
    state = {"edited": False}

    def read_then_user_edits(self: Path, *args: object, **kwargs: object) -> str:
        # Gerçek okuma yapılır; hemen ardından "kullanıcı" aynı dosyaya ekleme yapar.
        text = real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]
        if self == note_path and not state["edited"]:
            state["edited"] = True
            with self.open("a", encoding="utf-8") as handle:
                handle.write(late_edit)
        return text

    def forbid_note_write_text(self: Path, *args: object, **kwargs: object) -> int:
        assert self != note_path, "var olan kullanıcı notu write_text ile yeniden yazılmamalı"
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_then_user_edits)
    monkeypatch.setattr(Path, "write_text", forbid_note_write_text)

    record = _outcome(_bridge(sv), slug="var-olan")

    assert record is not None and record.receipt_ok
    text = note_path.read_bytes().decode("utf-8")
    assert text.startswith(_USER_NOTE + late_edit)
    assert text.count(PROJECT_LOG_HEADING) == 1


def test_create_new_note_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    """Yeni not dışlayıcı modda açılır: araya başka biri notu oluşturduysa üstüne yazılmaz.

    `record_outcome` bu `FileExistsError`'ı yakalayıp ekleme yoluna geçer.
    """
    note_path = tmp_path / "yarisan.md"
    note_path.write_text(_USER_NOTE, encoding="utf-8")

    with pytest.raises(FileExistsError):
        VaultBridge._create_new_note(note_path, title="X", spec_markdown=None, entry_line="- x")

    assert note_path.read_text(encoding="utf-8") == _USER_NOTE


# ---------------------------------------------------------------------- #
# 7. Receipt
# ---------------------------------------------------------------------- #


def test_receipt_failed_still_counts_note_as_written(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path, receipt_status="failed")

    record = _outcome(_bridge(sv), slug="fail-test", status_label="HATA", summary="Bir hata oluştu")

    assert record == VaultRecord(note="🏰 300-Projects/fail-test.md", receipt_ok=False)
    assert "HATA" in (sv.path / record.note).read_text(encoding="utf-8")
    receipt_call = next(call for call in sv.calls() if call["argv"][0] == "receipt")
    assert not Path(receipt_call["receipt_path"]).exists()


_RECEIPT_PREFIX = "Artemis proje atölyesi: '{slug}' TAMAM. "
_RECEIPT_SUFFIX = "\nÖğrenilen: yok"


def _receipts_by_slug(sv: SahteVault) -> dict[str, str]:
    return {
        Path(call["receipt"]["refs"][0]).stem: call["receipt"]["summary"]
        for call in sv.calls()
        if call["argv"][0] == "receipt"
    }


def _receipt_body(sv: SahteVault, slug: str) -> str:
    summary = _receipts_by_slug(sv)[slug]
    return summary.removeprefix(_RECEIPT_PREFIX.format(slug=slug)).removesuffix(_RECEIPT_SUFFIX)


def _entry_line(sv: SahteVault, slug: str) -> str:
    return (sv.path / "🏰 300-Projects" / f"{slug}.md").read_text(encoding="utf-8").splitlines()[-1]


def test_entry_uses_only_the_first_summary_line_but_receipt_keeps_the_lines(tmp_path: Path) -> None:
    """Kodlayıcı özetinin İLK satırı tek başına durur: not girdisine o girer, receipt tüm satırları taşır."""
    sv = sahte_vault(tmp_path)
    summary = "\n  M1 bitti,   3 test geçiyor.\n\n\nYapılanlar:\n- not ekle\n- notları listele   \n"

    _outcome(_bridge(sv), slug="cok-satirli", summary=summary)

    entry = _entry_line(sv, "cok-satirli")
    assert entry.endswith(" — M1 bitti, 3 test geçiyor.")
    note = (sv.path / "🏰 300-Projects" / "cok-satirli.md").read_text(encoding="utf-8")
    assert "Yapılanlar" not in note and "notları listele" not in note
    # Receipt: satır yapısı korunur (boş satır tek, satır sonu boşluğu yok), sonda sabit "Öğrenilen".
    assert _receipts_by_slug(sv)["cok-satirli"] == (
        "Artemis proje atölyesi: 'cok-satirli' TAMAM. M1 bitti,   3 test geçiyor.\n\n"
        "Yapılanlar:\n- not ekle\n- notları listele\nÖğrenilen: yok"
    )


def test_entry_is_cut_at_300_chars_and_receipt_at_a_line_boundary_within_1500(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)
    bridge = _bridge(sv)
    lines = [f"Satır {index:03d}: " + "x" * 40 for index in range(100)]  # satır başına 50 karakter
    one_long_line = "alfa " * 1000

    _outcome(bridge, slug="satirli", summary="\n".join(lines))
    _outcome(bridge, slug="tek-satir", summary=one_long_line)

    # Giriş: yalnızca ilk satır; kısa olduğu için kesilmez.
    assert _entry_line(sv, "satirli").endswith(f" — {lines[0]}")

    # Receipt: 1500'ü aşmaz, "…" ile biter ve kesim TAM bir satırdan sonradır (yarım satır yok).
    body = _receipt_body(sv, "satirli")
    assert len(body) <= 1500 and body.endswith("\n…")
    kept = body.split("\n")[:-1]
    assert kept == lines[: len(kept)]
    assert len(body) > 1400, "kesim sınıra yakın olmalı, çok erken değil"

    # Tek uzun satırda satır sınırı yoktur: karakterden kesilir (giriş 300, receipt 1500).
    entry = _entry_line(sv, "tek-satir").split(" — ", 1)[1]
    assert len(entry) == 300 and entry.endswith("…")
    long_body = _receipt_body(sv, "tek-satir")
    assert len(long_body) == 1500 and long_body.endswith("…") and "\n" not in long_body


# ---------------------------------------------------------------------- #
# 8. Yazılamayan / güvensiz durumlar
# ---------------------------------------------------------------------- #


def test_record_outcome_without_projects_dir_returns_none(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)
    (sv.path / "🏰 300-Projects").rmdir()

    assert _outcome(_bridge(sv)) is None
    assert sv.calls() == []  # not yazılmadıysa receipt de gönderilmez


@pytest.mark.parametrize("slug", ["../kacak", "alt/klasor", "/mutlak", "..", ""])
def test_record_outcome_rejects_unsafe_slug(tmp_path: Path, slug: str) -> None:
    sv = sahte_vault(tmp_path)

    assert _outcome(_bridge(sv), slug=slug) is None
    assert sv.calls() == []
    assert not (tmp_path / "kacak.md").exists()
    assert not (sv.path / "kacak.md").exists()


def test_record_outcome_write_failure_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Not yolu bir dizinse yazma OSError verir → uyarı, None, receipt yok."""
    sv = sahte_vault(tmp_path)
    (sv.path / "🏰 300-Projects" / "dizin.md").mkdir()

    with caplog.at_level(logging.WARNING, logger="projects.vault"):
        assert _outcome(_bridge(sv), slug="dizin") is None

    assert any("yazılamadı" in record.getMessage() for record in caplog.records)
    assert sv.calls() == []


# ---------------------------------------------------------------------- #
# 9. _command
# ---------------------------------------------------------------------- #


def test_command_resolves_relative_path_against_vault(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)
    script = sv.path / ".bulut" / "beyin.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    # Verilen komut aynen döner ve çağıran listeyi değiştirse köprü etkilenmez.
    given = list(sv.command)
    bridge = VaultBridge(vault_path=sv.path, command=given)
    command = bridge._command()
    assert command == sv.command
    assert command[0] == sys.executable and command[1].endswith("sahte_beyin_cli.py")
    command.append("kirlet")
    assert bridge._command() == sv.command

    # Vault içinde var olan göreli ilk öğe mutlak yola çevrilir.
    resolved = VaultBridge(vault_path=sv.path, command=[".bulut/beyin.sh", "--x"])._command()
    assert resolved == [str(sv.path / ".bulut" / "beyin.sh"), "--x"]
    assert Path(resolved[0]).is_absolute()

    # Vault içinde olmayan göreli yol ve mutlak yol olduğu gibi kalır.
    assert VaultBridge(vault_path=sv.path, command=["./diger/beyin.sh"])._command() == ["./diger/beyin.sh"]
    assert VaultBridge(vault_path=sv.path, command=[sys.executable])._command() == [sys.executable]


def test_command_default_ends_with_beyin_py(tmp_path: Path) -> None:
    sv = sahte_vault(tmp_path)

    for command in (None, []):
        default = VaultBridge(vault_path=sv.path, command=command)._command()
        assert default == [sys.executable, str(sv.path / "beyin.py")]
        assert default[-1].endswith("beyin.py")
