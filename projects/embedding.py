"""Vault notları için isteğe bağlı embedding (anlamsal) araması — ARCHITECTURE §46.

NEDEN VAR: `beyin.py context` sözcük tabanlıdır; paraphrase ağırlıklı 30 soruluk altın
kümede hit@4 yalnızca 0.43 çıktı (e5-large tek başına 0.73, MiniLM 0.57 — bkz. §45).
Bu modül notları vektöre çevirip kosinüs benzerliğiyle sıralar. KAPALI gelir:
`projeler.embedding_model` verilmedikçe hiçbir şey import edilmez, hiçbir model yüklenmez.

Tasarım ilkeleri:

- `fastembed` İSTEĞE BAĞLIDIR ve yalnızca varsayılan gömücü ilk kullanıldığında import
  edilir (lazy). Paket/model yoksa `EmbeddingUnavailable` fırlar; çağıran (VaultBridge)
  bunu yakalayıp CLI aramasına döner — vault Artemis için zorunlu değildir.
- İndeks (matris + meta) vault'un DIŞINDAKİ `cache_dir` altında tutulur: vault bir git
  deposudur ve kullanıcının notlarıdır, üretilmiş ikili dosyalarla kirletilmemeli.
- Bellek dosyası TEK bir `.npz`'dir (meta JSON'u içine gömülü) ve geçici dosya + `os.replace`
  ile yazılır: matris ile meta ayrı iki dosya olsaydı, ikisi arasında kesilen bir yazma
  uyumsuz (kayık satır ofsetli) bir indeks bırakırdı.
- Artımlı: her not dosya baytlarının sha256'sıyla izlenir; yalnızca değişen/yeni notlar
  yeniden gömülür, silinenler düşer. Sorgu zamanı maliyeti tek sorgu gömmesidir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Gömücü: (metinler, sorgu_mu) -> (n, d) matris. `sorgu_mu` yalnızca gömücüye bilgi içindir;
# e5 önekleri metne BU modülde eklenir (gömücü önek bilmez).
Embedder = Callable[[list[str], bool], "np.ndarray[Any, Any]"]

_CHUNK_CHARS = 800
_BATCH_SIZE = 32
_CACHE_VERSION = 1  # parçalama/biçim değişirse artırılır; eski önbellek yeniden kurulur
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_TITLE_LINE = re.compile(r"^title:[ \t]*(.+)$", re.MULTILINE)
_H1_LINE = re.compile(r"^# (.+)$", re.MULTILINE)
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


class EmbeddingUnavailable(RuntimeError):  # noqa: N818 - ad sözleşmeden: "kullanılamıyor" bir durumdur
    """`fastembed` kurulu değil ya da model yüklenemedi (indirme/ağ/bozuk dosya)."""


@dataclass(frozen=True)
class _Note:
    """Diskten okunmuş bir notun gömülmeye hazır hâli."""

    path: str  # vault-göreli POSIX yol
    sha: str  # dosya baytlarının sha256'sı
    passages: list[str]  # görüntülenecek gövde parçaları (başlıksız)
    embed_texts: list[str]  # gömülecek metinler (başlık + parça)


def split_passages(body: str, limit: int = _CHUNK_CHARS) -> list[str]:
    """Gövdeyi paragraf sınırlarında yaklaşık `limit` karakterlik parçalara böler.

    Kelime ASLA bölünmez: tek başına `limit`ten uzun bir paragraf boşluklardan kesilir
    (boşluksuz devasa bir sözcük olduğu gibi bir parça olur).
    """
    pieces: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        current = ""
        for word in paragraph.split():
            if current and len(current) + 1 + len(word) > limit:
                pieces.append(current)
                current = word
            else:
                current = f"{current} {word}" if current else word
        if current:
            pieces.append(current)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _title_and_body(text: str, stem: str) -> tuple[str, str]:
    """Frontmatter `title`'ı (yoksa ilk `# ` satırı, o da yoksa dosya adı) ve frontmatter'sız gövde."""
    title: str | None = None
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        body = text[match.end() :]
        found = _TITLE_LINE.search(match.group(1))
        if found:
            title = found.group(1).strip().strip("\"'").strip()
    if not title:
        heading = _H1_LINE.search(body)
        title = heading.group(1).strip() if heading else stem
    return title or stem, body


def _safe_name(model_name: str) -> str:
    """Model adını dosya adına uygun hâle getirir (`org/model` → `org_model`)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model_name)


def _default_skip(path: str) -> bool:
    """Varsayılan gürültü kuralı: `projects.vault`'taki ile AYNI (tek doğruluk kaynağı).

    Lazy import: `vault.py` bu modülü import eder; üst düzey import döngü yapardı.
    """
    from projects.vault import is_noise

    return is_noise(path)


class EmbeddingIndex:
    """Vault notlarının kalıcı, artımlı embedding indeksi."""

    def __init__(
        self,
        vault: Path,
        model_name: str,
        cache_dir: Path,
        embedder: Embedder | None = None,
        skip: Callable[[str], bool] | None = None,
    ) -> None:
        self._vault = vault
        self._model_name = model_name
        self._cache_file = cache_dir / f"{_safe_name(model_name)}.npz"
        self._cache_dir = cache_dir
        self._embedder = embedder
        self._skip = skip
        self._e5 = "e5" in model_name.lower()

    # ------------------------------------------------------------------ #
    # Gömücü
    # ------------------------------------------------------------------ #

    def _embed(self, texts: list[str], *, is_query: bool) -> np.ndarray[Any, Any]:
        """Metinleri (e5 için önekli) gömer, L2-normalize eder; kosinüs = nokta çarpımı olur."""
        if self._embedder is None:
            self._embedder = self._default_embedder()
        prefix = ("query: " if is_query else "passage: ") if self._e5 else ""
        vectors = np.asarray(self._embedder([prefix + text for text in texts], is_query), dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise ValueError(f"Gömücü {len(texts)} metin için {vectors.shape} biçiminde bir sonuç döndürdü.")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.asarray(vectors / norms, dtype=np.float32)

    def _default_embedder(self) -> Embedder:
        """`fastembed.TextEmbedding`'i (lazy) yükler; olmazsa `EmbeddingUnavailable`."""
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "fastembed kurulu değil; embedding araması için `pip install fastembed` gerekir."
            ) from exc
        try:
            # Model dosyaları da vault dışındaki önbellek klasörüne iner; fastembed'in varsayılanı
            # sistem geçici dizinidir ve yeniden başlatmada silinip 2 GB'lık modeli yeniden indirtir.
            model = TextEmbedding(self._model_name, cache_dir=str(self._cache_dir / "models"))
        except (ValueError, OSError, RuntimeError) as exc:
            raise EmbeddingUnavailable(f"Embedding modeli yüklenemedi ({self._model_name}): {exc}") from exc

        def embed(texts: list[str], is_query: bool) -> np.ndarray[Any, Any]:
            return np.asarray(list(model.embed(texts, batch_size=len(texts))), dtype=np.float32)

        return embed

    # ------------------------------------------------------------------ #
    # Korpus
    # ------------------------------------------------------------------ #

    def _corpus_files(self) -> list[Path]:
        """`knowledge/concepts/*.md` + her `*300-Projects*` klasörünün altındaki tüm Markdown dosyaları."""
        skip = self._skip or _default_skip
        files = sorted((self._vault / "knowledge" / "concepts").glob("*.md"))
        for projects_dir in sorted(self._vault.glob("*300-Projects*")):
            if projects_dir.is_dir():
                files.extend(sorted(projects_dir.rglob("*.md")))
        return [f for f in files if f.is_file() and not skip(f.relative_to(self._vault).as_posix())]

    def _read_note(self, file: Path) -> _Note | None:
        """Notu okur ve parçalar; okunamıyorsa (silinmiş/izin yok) uyarıp `None`."""
        try:
            raw = file.read_bytes()
        except OSError as exc:
            logger.warning("Not embedding için okunamadı (%s): %s", file, exc)
            return None
        title, body = _title_and_body(raw.decode("utf-8", errors="replace"), file.stem)
        passages = split_passages(body)
        if not passages:
            passages = [title]  # gövdesi boş not yine de başlığıyla bulunabilsin
        return _Note(
            path=file.relative_to(self._vault).as_posix(),
            sha=hashlib.sha256(raw).hexdigest(),
            passages=passages,
            embed_texts=[f"{title}\n{passage}" for passage in passages],
        )

    # ------------------------------------------------------------------ #
    # Önbellek
    # ------------------------------------------------------------------ #

    def _load_cache(self) -> tuple[dict[str, Any], np.ndarray[Any, Any]] | None:
        """Önbelleği okur; yoksa/bozuksa/sürüm uyuşmuyorsa `None` (bozuksa bir kez uyarır)."""
        if not self._cache_file.exists():
            return None
        try:
            with np.load(self._cache_file, allow_pickle=False) as data:
                meta = json.loads(str(data["meta"]))
                matrix = np.asarray(data["matrix"], dtype=np.float32)
            notes = meta["notes"]
            if meta["version"] != _CACHE_VERSION or meta["model"] != self._model_name:
                return None
            total = sum(int(entry["count"]) for entry in notes.values())
            if matrix.ndim != 2 or matrix.shape[0] != total:
                raise ValueError("matris satır sayısı meta ile uyuşmuyor")
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile) as exc:
            logger.warning("Embedding önbelleği okunamadı, yeniden kurulacak (%s): %s", self._cache_file, exc)
            return None
        return meta, matrix

    def _save_cache(self, meta: dict[str, Any], matrix: np.ndarray[Any, Any]) -> None:
        """Önbelleği geçici dosyaya yazıp `os.replace` ile yerine koyar (yarım dosya kalmaz)."""
        tmp_path: str | None = None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=self._cache_dir, prefix=".embedding-", suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                np.savez(handle, matrix=matrix, meta=np.array(json.dumps(meta, ensure_ascii=False)))
            os.replace(tmp_path, self._cache_file)
            tmp_path = None
        except OSError as exc:
            # Yazılamayan önbellek aramayı bozmaz: sonuçlar bellekteki indeksten doğru çıkar.
            logger.warning("Embedding önbelleği yazılamadı (%s): %s", self._cache_file, exc)
        finally:
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Geçici önbellek dosyası silinemedi (%s): %s", tmp_path, exc)

    def _sync(self) -> tuple[list[tuple[str, list[str]]], np.ndarray[Any, Any]]:
        """Önbelleği korpusla eşitler; (not yolu + parçalar listesi, satır matrisi) döndürür.

        Yalnızca değişen/yeni notlar gömülür; silinenler düşer. Hiçbir şey değişmediyse
        önbellek yeniden yazılmaz.
        """
        cached = self._load_cache()
        old_meta, old_matrix = cached if cached is not None else ({"notes": {}}, np.zeros((0, 0), dtype=np.float32))
        old_notes: dict[str, Any] = old_meta["notes"]

        current: list[_Note] = []
        for file in self._corpus_files():
            note = self._read_note(file)
            if note is not None:
                current.append(note)

        reusable: dict[str, Any] = {
            note.path: old_notes[note.path]
            for note in current
            if note.path in old_notes and old_notes[note.path]["sha"] == note.sha
        }
        to_embed = [note for note in current if note.path not in reusable]
        changed = bool(to_embed) or set(old_notes) != {note.path for note in current}

        new_vectors: dict[str, np.ndarray[Any, Any]] = {}
        if to_embed:
            flat = [text for note in to_embed for text in note.embed_texts]
            parts = [self._embed(flat[i : i + _BATCH_SIZE], is_query=False) for i in range(0, len(flat), _BATCH_SIZE)]
            stacked = np.concatenate(parts, axis=0)
            row = 0
            for note in to_embed:
                new_vectors[note.path] = stacked[row : row + len(note.passages)]
                row += len(note.passages)

        blocks: list[np.ndarray[Any, Any]] = []
        notes_meta: dict[str, Any] = {}
        entries: list[tuple[str, list[str]]] = []
        offset = 0
        for note in current:
            if note.path in reusable:
                old = reusable[note.path]
                block = old_matrix[int(old["offset"]) : int(old["offset"]) + int(old["count"])]
                passages = list(old["passages"])
            else:
                block = new_vectors[note.path]
                passages = note.passages
            notes_meta[note.path] = {"sha": note.sha, "count": len(passages), "offset": offset, "passages": passages}
            blocks.append(block)
            entries.append((note.path, passages))
            offset += len(passages)

        matrix = np.concatenate(blocks, axis=0) if blocks else np.zeros((0, 0), dtype=np.float32)
        if changed or cached is None:
            meta = {"version": _CACHE_VERSION, "model": self._model_name, "notes": notes_meta}
            if blocks:
                self._save_cache(meta, matrix)
        return entries, matrix

    # ------------------------------------------------------------------ #
    # Arama
    # ------------------------------------------------------------------ #

    def search(self, query: str, limit: int) -> list[tuple[str, float, str]]:
        """Sorguya en yakın `limit` notu döndürür: (vault-göreli POSIX yol, skor, en iyi parça).

        Notun skoru, parçalarının en yüksek kosinüsüdür. Korpus boşsa `[]`.

        Raises:
            EmbeddingUnavailable: `fastembed`/model yok.
            OSError, ValueError: indeks/gömücü sorunu (çağıran yakalayıp CLI'ya döner).
        """
        entries, matrix = self._sync()
        if not entries or limit <= 0:
            return []
        query_vector = self._embed([query], is_query=True)[0]
        scores = matrix @ query_vector
        results: list[tuple[str, float, str]] = []
        row = 0
        for path, passages in entries:
            block = scores[row : row + len(passages)]
            best = int(np.argmax(block))
            results.append((path, float(block[best]), passages[best]))
            row += len(passages)
        results.sort(key=lambda item: item[1], reverse=True)
        return results[:limit]
