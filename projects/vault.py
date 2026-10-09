"""Artemis ↔ ikinci beyin (Obsidian vault) köprüsü.

Bu modül, kullanıcının vault'undaki `beyin.py` CLI'siyle konuşarak kalıcı
tercihleri (Core.md) ve ilgili proje notlarını getirir; kodlama sonuçlarını
bir proje notuna yazar ve ardından bir "receipt" (kanıt) gönderir.

Tasarım ilkeleri:

- Vault yolu verilmemişse ya da mevcut bir dizin değilse köprü **pasif** olur:
  hiçbir dış süreç başlatılmaz, public yöntemler `None` döner.
- Bütün alt süreç çağrıları `_run` içinde toplanır; `_run` asla exception
  fırlatmaz, Türkçe bir uyarı loglar ve `None` döner. Vault Artemis'in
  çalışması için ZORUNLU değildir: CLI çökse bile proje atölyesi devam etmeli.
- Kullanıcının notları **asla yeniden yazılmaz**. Var olan bir nota yalnızca
  ekleme modunda (`open("a")`) yazılır; yeni not da dışlayıcı modda (`open("x")`)
  açılır ki aynı anda Obsidian'de oluşturulmuş bir notun üstüne yazılmasın.
- Yol kuran her girdi (`slug`) `utils.paths.safe_join` ile doğrulanır.
- Yollar `pathlib` ile kurulur; vault içindeki yollar POSIX biçiminde döner.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.paths import safe_join

logger = logging.getLogger(__name__)

PREFERENCES_HEADING = "## What I should never forget"
PROJECT_LOG_HEADING = "## Artemis kodlama kayıtları"

NEW_NOTE_BANNER = "> Bu not Artemis'in proje atölyesi tarafından açıldı; kodlama kayıtları aşağıya eklenir."

_PREFERENCES_LIMIT = 1200
_EXCERPT_LIMIT = 300
_ENTRY_SUMMARY_LIMIT = 300
_RECEIPT_SUMMARY_LIMIT = 500

# Bağlam sorgusunda gürültü olan, ham oturum kayıtları ve üretilmiş dizin dosyaları.
_NOISE_PREFIXES = ("daily/", "receipts/")
_NOISE_FILES = ("knowledge/index.md", "knowledge/log.md")
_NOISE_FOLDER_MARK = "850-Companion/"

# YAML'da düz (tırnaksız) yazılırsa anlamı bozulan başlıklar: başta özel karakter,
# `: ` / ` #` içeren ya da `:` ile biten metinler.
_YAML_RISKY = re.compile(r"""^[\s\-?:,\[\]{}#&*!|>'"%@`]|:\s|\s#|:$""")


def _shorten(text: str, limit: int) -> str:
    """Metni en çok `limit` karaktere indirir; kesilirse sonuna "…" koyar."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    """Her türlü boşluk dizisini (satır sonları dahil) tek boşluğa indirir."""
    return " ".join(text.split())


def _yaml_scalar(value: str) -> str:
    """Frontmatter değeri: gerekiyorsa tırnaklar (JSON dizesi geçerli bir YAML dizesidir)."""
    if _YAML_RISKY.search(value):
        return json.dumps(value, ensure_ascii=False)
    return value


@dataclass(frozen=True)
class VaultNote:
    """Vault'tan dönen tek bir not özeti."""

    source: str  # vault-göreli yol, CLI'nin döndürdüğü gibi
    excerpt: str  # <= 300 karakter, tek satır


@dataclass(frozen=True)
class VaultContext:
    """Bir sorgu için toplanan bağlam: tercihler + filtrelenmiş notlar."""

    preferences: str = ""
    notes: tuple[VaultNote, ...] = ()

    def is_empty(self) -> bool:
        """Ne tercih ne de not varsa True."""
        return not self.preferences and not self.notes

    def to_prompt(self) -> str:
        """LLM istemine eklenecek Türkçe bloğu üretir; bağlam boşsa `""` döner.

        Her başlığın içeriği hemen bir alt satırda başlar, notlar ardışık
        satırlardır ve YALNIZCA iki bölüm arasında bir boş satır vardır::

            Umut'un kalıcı tercihleri (Core.md):
            <preferences>

            İlgili vault notları:
            - <source>: <excerpt>
            - <source>: <excerpt>

        Boş bölüm başlığıyla birlikte atlanır.
        """
        parts: list[str] = []
        if self.preferences:
            parts.append("Umut'un kalıcı tercihleri (Core.md):\n" + self.preferences)
        if self.notes:
            lines = "\n".join(f"- {note.source}: {note.excerpt}" for note in self.notes)
            parts.append("İlgili vault notları:\n" + lines)
        return "\n\n".join(parts)

    def sources(self) -> list[str]:
        """Not kaynak yollarını sırayla döndürür."""
        return [note.source for note in self.notes]


@dataclass(frozen=True)
class VaultRecord:
    """Bir `record_outcome` çağrısının sonucu."""

    note: str  # yazılan proje notunun vault-göreli POSIX yolu
    receipt_ok: bool  # receipt komutu `status == "succeeded"` yanıtladıysa True


class VaultBridge:
    """Vault CLI'sını (`beyin.py`) çağıran ve yüksek seviyeli işlemler sunan köprü."""

    def __init__(
        self,
        vault_path: Path | None,
        command: list[str] | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._vault_path = vault_path
        self._command_override = command
        self._timeout = timeout_seconds

    @property
    def enabled(self) -> bool:
        """Vault yolu verilmiş ve var olan bir dizin mi?"""
        return self._vault_path is not None and self._vault_path.is_dir()

    # ------------------------------------------------------------------ #
    # Alt süreç
    # ------------------------------------------------------------------ #

    def _command(self) -> list[str]:
        """CLI komutunu döndürür (her seferinde yeni bir liste).

        Verilen komutun ilk öğesi göreli bir yolsa ve vault içinde o dosya
        varsa mutlak yola çevrilir: alt süreç `cwd=vault` ile çalışır, ama
        köprünün kendi çalışma dizini farklı olabilir.
        """
        vault = self._vault_path
        if self._command_override:
            command = list(self._command_override)
            first = Path(command[0])
            if vault is not None and not first.is_absolute():
                candidate = vault / first
                if candidate.exists():
                    command[0] = str(candidate.absolute())
            return command
        base = vault if vault is not None else Path()
        return [sys.executable, str(base / "beyin.py")]

    def _run(self, args: list[str]) -> dict[str, Any] | None:
        """CLI'yı vault kökünde çalıştırır ve stdout'taki JSON nesnesini döndürür.

        Başlatılamama, zaman aşımı, sıfır olmayan çıkış kodu, boş/bozuk JSON ya
        da nesne olmayan JSON durumlarında Türkçe uyarı loglayıp `None` döner;
        asla exception fırlatmaz.
        """
        if not self.enabled:
            return None
        try:
            result = subprocess.run(
                [*self._command(), *args],
                cwd=self._vault_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except OSError as exc:
            logger.warning("Vault CLI başlatılamadı: %s", exc)
            return None
        except subprocess.TimeoutExpired:
            logger.warning("Vault CLI %.1f sn içinde yanıt vermedi (zaman aşımı).", self._timeout)
            return None

        if result.returncode != 0:
            logger.warning(
                "Vault CLI hata koduyla bitti (%d): %s", result.returncode, (result.stderr or "")[-300:].strip()
            )
            return None

        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("Vault CLI çıktısı JSON değil: %s", exc)
            return None
        if not isinstance(parsed, dict):
            logger.warning("Vault CLI çıktısı bir JSON nesnesi değil (%s).", type(parsed).__name__)
            return None
        return parsed

    # ------------------------------------------------------------------ #
    # Bağlam
    # ------------------------------------------------------------------ #

    def context_for(self, query: str, max_notes: int = 4) -> VaultContext | None:
        """Sorgu için kalıcı tercihleri ve filtrelenmiş vault notlarını getirir.

        Vault pasifse `None`. Aksi hâlde HER ZAMAN bir `VaultContext` döner:
        CLI çağrısı başarısız olsa bile Core.md'deki tercihler yine verilir.
        """
        vault = self._vault_path
        if vault is None or not self.enabled:
            return None

        preferences = self._read_preferences(vault)
        answer = self._run(["context", "--no-sync", "--limit", "15", "--budget-chars", "20000", query])

        notes: list[VaultNote] = []
        records = answer.get("records") if answer is not None else None
        # Bozuk bir CLI yanıtı (liste olmayan `records`, nesne olmayan kayıt)
        # AttributeError ile çökmek yerine sessizce boş sayılır.
        if isinstance(records, list):
            for record in records:
                if len(notes) >= max_notes:
                    break
                if not isinstance(record, dict):
                    continue
                source = record.get("source")
                if not isinstance(source, str) or self._is_noise(source):
                    continue
                text = record.get("text")
                excerpt = self._make_excerpt(text) if isinstance(text, str) else ""
                if excerpt:
                    notes.append(VaultNote(source=source, excerpt=excerpt))
        return VaultContext(preferences=preferences, notes=tuple(notes))

    @staticmethod
    def _read_preferences(vault: Path) -> str:
        """Core.md'deki tercih bölümünü okur; dosya yok/okunamıyorsa `""`."""
        candidates = sorted(vault.glob("*850-Companion/Core.md"))
        if not candidates:
            return ""
        try:
            content = candidates[0].read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Core.md okunamadı (%s): %s", candidates[0], exc)
            return ""
        return VaultBridge._extract_preferences(content)

    @staticmethod
    def _is_noise(source: str) -> bool:
        """Ham oturum kaydı, receipt, üretilmiş dizin ya da companion günlüğü mü?"""
        if not source.strip():
            return True
        # Windows'ta CLI ters bölü ile dönerse de aynı kurallar işlesin.
        normalized = source.replace("\\", "/")
        return normalized.startswith(_NOISE_PREFIXES) or normalized in _NOISE_FILES or _NOISE_FOLDER_MARK in normalized

    @staticmethod
    def _extract_preferences(content: str) -> str:
        """`## What I should never forget` bölümünü çıkarır.

        Bölüm bir sonraki `## ` satırında biter. HTML yorum satırları (tek
        satırlık ya da çok satırlı) atılır: yorumlar kullanıcının kendine
        notudur, modele gitmemeli.
        """
        collected: list[str] = []
        in_section = False
        in_comment = False
        for line in content.splitlines():
            stripped = line.strip()
            if not in_section:
                in_section = stripped == PREFERENCES_HEADING
                continue
            if line.startswith("## "):
                break
            if in_comment:
                in_comment = "-->" not in stripped
                continue
            if stripped.startswith("<!--"):
                in_comment = "-->" not in stripped
                continue
            collected.append(line.rstrip())
        return _shorten("\n".join(collected).strip(), _PREFERENCES_LIMIT)

    @staticmethod
    def _make_excerpt(text: str) -> str:
        """Notun gövdesinden tek satırlık, en çok 300 karakterlik özet üretir.

        Başlıklar ve frontmatter atılır: prompt'a giden şey notun KONUSU değil
        içeriği olmalı, başlık zaten kaynak yolunda görünüyor.
        """
        lines = text.splitlines()
        start = 0
        if lines and lines[0].strip() == "---":
            for index in range(1, len(lines)):
                if lines[index].strip() == "---":
                    start = index + 1
                    break
        body = [line for line in lines[start:] if not line.strip().startswith("#")]
        return _shorten(_one_line(" ".join(body)), _EXCERPT_LIMIT)

    # ------------------------------------------------------------------ #
    # Sonuç kaydı
    # ------------------------------------------------------------------ #

    def record_outcome(
        self,
        *,
        slug: str,
        title: str,
        status_label: str,
        summary: str,
        project_dir: Path,
        spec_markdown: str | None,
        event_key: str,
    ) -> VaultRecord | None:
        """Kodlama sonucunu proje notuna yazar ve receipt gönderir.

        Vault pasifse ya da not yazılamazsa `None`. Not yazıldıysa receipt
        başarısız olsa bile `VaultRecord` döner (`receipt_ok=False`): notun
        yazılmış olması, kanıtın kabul edilmesinden bağımsız bir gerçektir.
        """
        vault = self._vault_path
        if vault is None or not self.enabled:
            return None

        projects_dirs = [path for path in sorted(vault.glob("*300-Projects*")) if path.is_dir()]
        if not projects_dirs:
            logger.warning("Vault içinde proje klasörü (*300-Projects*) bulunamadı; kayıt yazılmadı.")
            return None
        projects_dir = projects_dirs[0]

        # `slug` dosya adı olur: mutlak yol, `..` ya da alt klasör içeremez; boş ya da
        # noktayla başlayan ad da Obsidian'de görünmeyen gizli bir not (`.md`) üretirdi.
        note_path = safe_join(projects_dir, f"{slug}.md")
        if not slug or slug.startswith(".") or note_path is None or note_path.parent != projects_dir:
            logger.warning("Güvensiz proje adı (%r); kayıt yazılmadı.", slug)
            return None
        rel_note = note_path.relative_to(vault).as_posix()

        # Tek satırlık özet bir kez hesaplanır: girdi 300, receipt 500 karakterlik pencere alır.
        summary_line = _one_line(summary)
        status = _one_line(status_label)
        entry_line = f"- {datetime.now():%Y-%m-%d %H:%M} · {status} · `{project_dir}`"
        if summary_line:
            entry_line += f" — {_shorten(summary_line, _ENTRY_SUMMARY_LIMIT)}"

        try:
            try:
                self._create_new_note(
                    note_path, title=_one_line(title) or slug, spec_markdown=spec_markdown, entry_line=entry_line
                )
            except FileExistsError:
                self._append_to_existing_note(note_path, entry_line)
        except OSError as exc:
            logger.warning("Proje notu yazılamadı (%s): %s", note_path, exc)
            return None

        receipt_summary = f"Artemis proje atölyesi: '{slug}' {status}."
        if summary_line:
            receipt_summary += f" {_shorten(summary_line, _RECEIPT_SUMMARY_LIMIT)}"
        payload = {
            "event_id": f"artemis-proje-{event_key}",
            "summary": f"{receipt_summary}\nÖğrenilen: yok",
            "refs": [rel_note],
        }
        return VaultRecord(note=rel_note, receipt_ok=self._send_receipt(payload))

    def _send_receipt(self, payload: dict[str, Any]) -> bool:
        """Receipt'i CLI'ya gönderir; yalnızca `status == "succeeded"` ise True.

        Yük, vault'u kirletmemek için SİSTEM geçici dizinindeki bir dosyaya
        yazılır ve ne olursa olsun (`finally`) silinir.
        """
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                json.dump(payload, tmp, ensure_ascii=False)
            result = self._run(["receipt", "--file", str(tmp_path), "--harness", "claude"])
        except OSError as exc:
            logger.warning("Receipt dosyası hazırlanamadı: %s", exc)
            return False
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Geçici receipt dosyası silinemedi (%s): %s", tmp_path, exc)
        return result is not None and result.get("status") == "succeeded"

    @staticmethod
    def _create_new_note(note_path: Path, *, title: str, spec_markdown: str | None, entry_line: str) -> None:
        """Yeni proje notunu oluşturur (frontmatter, başlık, uyarı, spec, kayıt başlığı, giriş).

        Dışlayıcı mod (`"x"`) kullanılır: not `exists()` kontrolünden sonra,
        yazmadan önce başka bir süreçte (ör. Obsidian) oluşturulmuşsa üstüne
        yazmak yerine `FileExistsError` fırlar ve çağıran ekleme yoluna geçer.
        """
        today = datetime.now().date().isoformat()
        lines = [
            "---",
            f"title: {_yaml_scalar(title)}",
            f"created: {today}",
            f"modified: {today}",
            "type: project",
            "status: active",
            "tags: [artemis, proje]",
            "---",
            f"# {title}",
            "",
            NEW_NOTE_BANNER,
        ]
        spec_lines = (spec_markdown or "").strip().splitlines()
        # Spec'in kendi başlığı notun `# başlık` satırıyla çakışmasın.
        if spec_lines and spec_lines[0].startswith("# "):
            spec_lines = spec_lines[1:]
        spec_body = "\n".join(spec_lines).strip()
        if spec_body:
            lines += ["", spec_body]
        lines += ["", PROJECT_LOG_HEADING, "", entry_line]
        with note_path.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    @staticmethod
    def _append_to_existing_note(note_path: Path, entry_line: str) -> None:
        """Var olan kullanıcı notunun SONUNA ekler; notu asla yeniden yazmaz.

        Neden yalnızca ekleme modu (`open("a")`): "oku, birleştir, `write_text`
        ile yaz" yöntemi dosyayı kısaltıp baştan yazar; kullanıcı notu o
        arada Obsidian'de düzenlediyse (okuma ile yazma arasında) düzenlemesi
        sessizce kaybolur. Ekleme modunda yazılan bayt dizisi dosyanın o anki
        SONUNA gider, mevcut baytlara dokunulmaz.

        Dosya yalnızca NE EKLENECEĞİNE karar vermek için okunur: sonu satır
        sonuyla bitiyor mu, kayıt başlığı zaten var mı?
        """
        existing = note_path.read_text(encoding="utf-8", errors="replace")
        addition = ""
        if existing and not existing.endswith("\n"):
            addition += "\n"
        if PROJECT_LOG_HEADING not in existing:
            addition += f"\n{PROJECT_LOG_HEADING}\n\n"
        addition += entry_line + "\n"
        with note_path.open("a", encoding="utf-8") as handle:
            handle.write(addition)
