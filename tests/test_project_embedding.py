"""`projects/embedding.py` ve vault köprüsünün embedding yolu (M4, ARCHITECTURE §46).

Gerçek model İNDİRİLMEZ: gömücü, sözcük torbasını 64 boyuta kıyılayan deterministik bir
sahtedir (ortak sözcük çok = kosinüs yüksek). Vault, dosya sistemi ve önbellek gerçektir.
"""

from __future__ import annotations

import logging
import re
import sys
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from projects import jobs
from projects.embedding import EmbeddingIndex, EmbeddingUnavailable, split_passages
from projects.interview import first_question
from projects.vault import VaultBridge
from tests.sahte_beyin import SahteVault, sahte_vault
from tests.test_projects_vault import _projeler

_DIMS = 64

_NOTES = {
    "knowledge/concepts/webrtc-arama.md": "# WebRTC\n\nArkadaşlarla görüntülü konuşma için peer to peer video arama mimarisi.",
    "knowledge/concepts/borsa-bot.md": "# Borsa botu\n\nHisse senedi alım satım stratejisi ve sanal para ile paper trading.",
    "knowledge/concepts/pdf-uretimi.md": "# PDF\n\nHTML sayfayı headless tarayıcı ile PDF belgesine çevirmek.",
    "knowledge/concepts/sesli-asistan.md": "# Sesli asistan\n\nUyandırma sözcüğü ve mikrofon ile komut dinleyen yardımcı.",
    "knowledge/concepts/push-bildirim.md": "# Bildirim\n\nTelefona anında itme bildirimi gönderen firebase altyapısı.",
    "knowledge/concepts/rls-kurallari.md": "# RLS\n\nSatır düzeyinde erişim kuralları ve kullanıcı verisi gizliliği.",
    "🏰 300-Projects/not-defteri.md": "---\ntitle: Not defteri\n---\nMarkdown notlarını klasörlerle düzenleyen küçük masaüstü uygulaması.",
}
_NOISE = {
    "daily/2026-09-01.md": "Ham oturum: görüntülü konuşma arkadaşlarla video arama denemesi.",
    "receipts/r1.md": "Receipt: görüntülü konuşma arkadaşlarla video arama.",
    "knowledge/index.md": "Dizin: görüntülü konuşma arkadaşlarla video arama.",
    "🔮 850-Companion/Journal.md": "Günlük: görüntülü konuşma arkadaşlarla video arama.",
}
_QUERY = "arkadaşlarımla görüntülü konuşabileceğim video uygulaması"


def fake_embed(texts: list[str], is_query: bool) -> np.ndarray[Any, Any]:
    """Sözcük torbası → 64 boyutlu kıyım. `hash()` her süreçte tuzlandığı için crc32 kullanılır."""
    out = np.zeros((len(texts), _DIMS), dtype=np.float32)
    for row, text in enumerate(texts):
        for word in re.findall(r"\w+", text.lower()):
            out[row, zlib.crc32(word.encode("utf-8")) % _DIMS] += 1.0
    return out


class CountingEmbedder:
    """Çağrıları sayan sahte gömücü: hangi metinlerin gömüldüğünü kaydeder."""

    def __init__(self) -> None:
        self.passages: list[str] = []
        self.queries: list[str] = []

    def __call__(self, texts: list[str], is_query: bool) -> np.ndarray[Any, Any]:
        (self.queries if is_query else self.passages).extend(texts)
        return fake_embed(texts, is_query)


def _write_notes(vault: Path, notes: dict[str, str]) -> None:
    for rel, text in notes.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _vault_files(vault: Path) -> set[str]:
    return {p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file()}


@pytest.fixture
def fake_vault(tmp_path: Path) -> SahteVault:
    vault = sahte_vault(
        tmp_path, [{"source": "knowledge/concepts/cli-notu.md", "text": "# CLI\n\nCLI yolundan geldi."}]
    )
    _write_notes(vault.path, _NOTES | _NOISE)
    return vault


def _index(
    fake_vault: SahteVault, tmp_path: Path, embedder: Any = fake_embed, model: str = "fake-model"
) -> EmbeddingIndex:
    return EmbeddingIndex(fake_vault.path, model, tmp_path / "onbellek", embedder)


# --- 1. sıralama -------------------------------------------------------------------


def test_ranking_puts_the_related_note_first_and_skips_noise(fake_vault: SahteVault, tmp_path: Path) -> None:
    ranked = _index(fake_vault, tmp_path).search(_QUERY, 10)

    assert ranked[0][0] == "knowledge/concepts/webrtc-arama.md"
    assert ranked[0][1] > ranked[1][1]
    assert "görüntülü" in ranked[0][2], "en iyi parça metni de döner"
    paths = [path for path, _, _ in ranked]
    assert "🏰 300-Projects/not-defteri.md" in paths, "300-Projects notları korpustadır"
    assert len(paths) == len(_NOTES)
    for noisy in _NOISE:
        assert noisy not in paths


def test_search_limit_and_empty_corpus(fake_vault: SahteVault, tmp_path: Path) -> None:
    assert len(_index(fake_vault, tmp_path).search(_QUERY, 3)) == 3
    empty = tmp_path / "bos-vault"
    empty.mkdir()
    assert EmbeddingIndex(empty, "fake-model", tmp_path / "bos-onbellek", fake_embed).search(_QUERY, 4) == []


def test_passages_split_on_paragraphs_and_never_inside_a_word() -> None:
    body = "\n\n".join(["alfa " * 100, "beta " * 100, "kısa paragraf"])
    chunks = split_passages(body, 800)

    assert len(chunks) >= 2
    assert all(len(chunk) <= 800 for chunk in chunks)
    words = {word for chunk in chunks for word in chunk.split()}
    assert words == {"alfa", "beta", "kısa", "paragraf"}, "hiçbir sözcük bölünmedi"
    assert split_passages("x" * 2000, 800) == ["x" * 2000], "boşluksuz devasa sözcük olduğu gibi kalır"
    assert split_passages("   \n\n  ") == []


# --- 2. e5 önekleri ----------------------------------------------------------------


def test_e5_models_get_query_and_passage_prefixes(fake_vault: SahteVault, tmp_path: Path) -> None:
    counting = CountingEmbedder()
    _index(fake_vault, tmp_path, counting, "intfloat/multilingual-e5-large").search(_QUERY, 4)

    assert counting.queries == ["query: " + _QUERY]
    assert counting.passages and all(text.startswith("passage: ") for text in counting.passages)


def test_other_models_get_no_prefix(fake_vault: SahteVault, tmp_path: Path) -> None:
    counting = CountingEmbedder()
    _index(fake_vault, tmp_path, counting, "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2").search(
        _QUERY, 4
    )

    assert counting.queries == [_QUERY]
    assert not any(text.startswith("passage: ") for text in counting.passages)


# --- 3. önbellek -------------------------------------------------------------------


def test_cache_embeds_only_the_query_on_second_search(fake_vault: SahteVault, tmp_path: Path) -> None:
    counting = CountingEmbedder()
    index = _index(fake_vault, tmp_path, counting)
    first = index.search(_QUERY, 10)
    assert len(counting.passages) >= len(_NOTES)

    counting.passages.clear()
    counting.queries.clear()
    second = _index(fake_vault, tmp_path, counting).search(_QUERY, 10)  # yeni nesne: kalıcı önbellek

    assert counting.passages == [], "değişmeyen korpus yeniden gömülmemeli"
    assert counting.queries == [_QUERY]
    assert [(p, round(s, 5)) for p, s, _ in second] == [(p, round(s, 5)) for p, s, _ in first]


def test_editing_one_note_reembeds_only_that_note(fake_vault: SahteVault, tmp_path: Path) -> None:
    counting = CountingEmbedder()
    _index(fake_vault, tmp_path, counting).search(_QUERY, 10)
    counting.passages.clear()

    (fake_vault.path / "knowledge/concepts/pdf-uretimi.md").write_text(
        "# PDF\n\nTamamen yeni içerik: görüntülü konuşma arkadaşlarla video arama.", encoding="utf-8"
    )
    ranked = _index(fake_vault, tmp_path, counting).search(_QUERY, 10)

    assert len(counting.passages) == 1 and "Tamamen yeni içerik" in counting.passages[0]
    assert ranked[0][0] in {"knowledge/concepts/pdf-uretimi.md", "knowledge/concepts/webrtc-arama.md"}
    assert "pdf-uretimi" in {Path(p).stem for p, _, _ in ranked[:2]}, "düzenlenen notun yeni içeriği aramaya yansır"


def test_deleted_note_is_dropped_and_new_note_is_added(fake_vault: SahteVault, tmp_path: Path) -> None:
    counting = CountingEmbedder()
    _index(fake_vault, tmp_path, counting).search(_QUERY, 10)
    counting.passages.clear()

    (fake_vault.path / "knowledge/concepts/borsa-bot.md").unlink()
    _write_notes(fake_vault.path, {"knowledge/concepts/yeni.md": "# Yeni\n\nYepyeni bir not."})
    paths = [p for p, _, _ in _index(fake_vault, tmp_path, counting).search(_QUERY, 20)]

    assert "knowledge/concepts/borsa-bot.md" not in paths
    assert "knowledge/concepts/yeni.md" in paths
    assert len(counting.passages) == 1


def test_cache_is_written_atomically_outside_the_vault(fake_vault: SahteVault, tmp_path: Path) -> None:
    before = _vault_files(fake_vault.path)
    cache_dir = tmp_path / "onbellek"
    _index(fake_vault, tmp_path).search(_QUERY, 4)

    assert _vault_files(fake_vault.path) == before, "vault'a tek dosya bile eklenmemeli"
    assert [p.name for p in cache_dir.iterdir()] == ["fake-model.npz"], "geçici dosya kalmamalı"
    assert not any(p.suffix == ".tmp" for p in cache_dir.iterdir())


def test_corrupt_cache_is_rebuilt_without_error(
    fake_vault: SahteVault, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first = _index(fake_vault, tmp_path).search(_QUERY, 10)
    (tmp_path / "onbellek" / "fake-model.npz").write_bytes(b"bu bir npz degil")

    counting = CountingEmbedder()
    with caplog.at_level(logging.WARNING, logger="projects.embedding"):
        rebuilt = _index(fake_vault, tmp_path, counting).search(_QUERY, 10)

    assert [p for p, _, _ in rebuilt] == [p for p, _, _ in first]
    assert len(counting.passages) >= len(_NOTES), "hepsi yeniden gömüldü"
    assert sum("önbelleği okunamadı" in r.message for r in caplog.records) == 1
    # Yeniden kurulan önbellek bir sonraki aramada kullanılır.
    again = CountingEmbedder()
    _index(fake_vault, tmp_path, again).search(_QUERY, 10)
    assert again.passages == []


def test_changing_the_model_name_does_not_reuse_another_models_cache(fake_vault: SahteVault, tmp_path: Path) -> None:
    _index(fake_vault, tmp_path, model="model-a").search(_QUERY, 4)
    counting = CountingEmbedder()
    _index(fake_vault, tmp_path, counting, model="model-b").search(_QUERY, 4)

    assert len(counting.passages) >= len(_NOTES)
    assert sorted(p.name for p in (tmp_path / "onbellek").iterdir()) == ["model-a.npz", "model-b.npz"]


# --- 4. eşik -----------------------------------------------------------------------


def _bridge(fake_vault: SahteVault, tmp_path: Path, min_score: float | None) -> VaultBridge:
    return VaultBridge(
        fake_vault.path,
        fake_vault.command,
        embedding=_index(fake_vault, tmp_path),
        embedding_min_score=min_score,
    )


def test_min_score_above_every_score_gives_no_notes_and_no_cli_call(fake_vault: SahteVault, tmp_path: Path) -> None:
    context = _bridge(fake_vault, tmp_path, 1.1).context_for(_QUERY, 4)

    assert context is not None
    assert context.notes == ()
    assert "Tercih 1" in context.preferences, "tercihler Core.md'den yine gelir"
    assert fake_vault.calls() == [], "eşik 'hiç vermemek' demektir: CLI'ya düşülmez"


def test_no_threshold_returns_top_max_notes_with_cleaned_excerpts(fake_vault: SahteVault, tmp_path: Path) -> None:
    context = _bridge(fake_vault, tmp_path, None).context_for(_QUERY, 3)

    assert context is not None
    assert len(context.notes) == 3
    assert context.notes[0].source == "knowledge/concepts/webrtc-arama.md"
    assert context.notes[0].excerpt.startswith("Arkadaşlarla görüntülü konuşma")
    assert all(len(note.excerpt) <= 300 and "\n" not in note.excerpt for note in context.notes)
    assert fake_vault.calls() == []


def test_threshold_keeps_only_notes_at_or_above_it(fake_vault: SahteVault, tmp_path: Path) -> None:
    ranked = _index(fake_vault, tmp_path).search(_QUERY, 10)
    threshold = (ranked[0][1] + ranked[1][1]) / 2

    context = _bridge(fake_vault, tmp_path, threshold).context_for(_QUERY, 4)

    assert context is not None
    assert context.sources() == [ranked[0][0]]


# --- 5. geri dönüş -----------------------------------------------------------------


def test_unavailable_embedder_warns_and_falls_back_to_the_cli(
    fake_vault: SahteVault, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(texts: list[str], is_query: bool) -> np.ndarray[Any, Any]:
        raise EmbeddingUnavailable("model yok")

    bridge = VaultBridge(fake_vault.path, fake_vault.command, embedding=_index(fake_vault, tmp_path, broken))
    with caplog.at_level(logging.WARNING, logger="projects.vault"):
        context = bridge.context_for(_QUERY, 4)

    assert context is not None
    assert context.sources() == ["knowledge/concepts/cli-notu.md"], "CLI yolunun sonucu döndü"
    warnings = [r for r in caplog.records if "Embedding araması kullanılamadı" in r.message]
    assert len(warnings) == 1
    assert len(fake_vault.calls()) == 1


# --- 6. kapalı ---------------------------------------------------------------------


def test_bridge_without_embedding_uses_only_the_cli(fake_vault: SahteVault, tmp_path: Path) -> None:
    context = VaultBridge(fake_vault.path, fake_vault.command).context_for(_QUERY, 4)

    assert context is not None
    assert context.sources() == ["knowledge/concepts/cli-notu.md"]
    assert not (tmp_path / "onbellek").exists()


def test_vault_bridge_factory_builds_an_index_only_when_asked(fake_vault: SahteVault, tmp_path: Path) -> None:
    base = _projeler_for(fake_vault, tmp_path)

    assert jobs.vault_bridge(base)._embedding is None, "model verilmediyse indeks yok"
    on = base.model_copy(update={"embedding_model": "fake-model"})
    assert jobs.vault_bridge(on)._embedding is not None
    assert jobs.vault_bridge(on, with_embedding=False)._embedding is None
    assert jobs.vault_bridge(on.model_copy(update={"vault_path": None}))._embedding is None, (
        "vault kapalıysa indeks yok"
    )


def _projeler_for(fake_vault: SahteVault, tmp_path: Path) -> Any:
    from config.settings import ProjelerSettings

    return ProjelerSettings(root=tmp_path / "Projeler", state_dir=tmp_path / "durum", vault_path=fake_vault.path)


# --- 7. görüşme uçtan uca ----------------------------------------------------------


def test_begin_interview_with_embedding_names_the_notes_it_read(
    fake_vault: SahteVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EmbeddingIndex, "_default_embedder", lambda self: fake_embed)
    projeler = _projeler(tmp_path, monkeypatch, fake_vault).model_copy(update={"embedding_model": "fake-model"})

    result = jobs.begin_interview(projeler, _QUERY, first_question())

    assert result.success, result.message
    sources = result.data["vault_sources"]
    assert sources[0] == "knowledge/concepts/webrtc-arama.md"
    assert len(sources) == 4 and not any(s.startswith(("daily/", "receipts/")) for s in sources)
    assert result.message.startswith(first_question() + " (Vault'tan tercihlerini ve 4 notu okudum: webrtc-arama,")
    assert fake_vault.calls() == [], "embedding yolunda CLI çağrılmadı"
    assert (projeler.state_dir / "embedding" / "fake-model.npz").exists(), "önbellek state_dir altında"
    interview = jobs.open_store(projeler).open_interview()
    assert interview is not None and "Arkadaşlarla görüntülü konuşma" in interview.transcript[0]["metin"]


# --- 8. fastembed yok --------------------------------------------------------------


def test_missing_fastembed_raises_embedding_unavailable(
    fake_vault: SahteVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)  # `import fastembed` → ImportError
    index = EmbeddingIndex(fake_vault.path, "fake-model", tmp_path / "onbellek")

    with pytest.raises(EmbeddingUnavailable, match="fastembed"):
        index.search(_QUERY, 4)


def test_missing_fastembed_makes_the_bridge_fall_back_to_the_cli(
    fake_vault: SahteVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)
    index = EmbeddingIndex(fake_vault.path, "fake-model", tmp_path / "onbellek")
    context = VaultBridge(fake_vault.path, fake_vault.command, embedding=index).context_for(_QUERY, 4)

    assert context is not None and context.sources() == ["knowledge/concepts/cli-notu.md"]
