"""`ui/settings_window.py` için testler: `config.yaml`'ı metin seviyesinde
yamalayan saf fonksiyonlar.

NEDEN SADECE BU İKİ FONKSİYON: `apply_config_overrides` ve
`save_overrides` dosya üzerinde çalışan saf yardımcılardır — QWidget
gerektirmezler, dolayısıyla PyQt6'nın kurulu olmadığı bir CI makinesinde
de (bu modülü içe aktarmadan) çalışırlar. `SettingsWindow`'ın kendisi
canlı bir pencere kurar; onu kurmak için gerçek bir `QApplication`
gerekir, o da burada test edilMEZ (bkz. `tests/test_ui_hotkey.py`'nin
`importorskip` notu).

BU DOSYA `config/config.yaml`'IN KENDİSİNE DOKUNMAZ. Her test `tmp_path`
altında kendi kopyasını kurar; gerçek kullanıcı ayarlarının bozulması
bir testin yanlışlıkla yapabileceği en kötü şeydir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import settings as config_settings
from config.settings import get_settings
from ui.settings_window import apply_config_overrides, save_overrides

_ORIGINAL = """# Artemis örnek konfigürasyonu.
# Burada tanımlanmayan alanlar için varsayılanlar kullanılır.

# Not: `python main.py --chat` başlangıçta kurulu modelleri listeler.
ollama_model: "gemma4:e4b"

# --- Sesli asistan ayarları ---
voice_enabled: true
wake_word_enabled: true
voice_hotkey: "ctrl+alt+a"
"""


# --- apply_config_overrides: var olan anahtarı değiştirme ----------------


def test_replacing_a_key_changes_exactly_one_line_and_keeps_every_comment() -> None:
    """Yorum satırları ve DİĞER anahtarlar ELDE KALMALI.

    Bu testin varlık sebebi: `config.yaml` bu deponun kullanıcıya dönük
    ana belgelendirme yüzeyi. Bir ayarı değiştirmek o metni silmemeli.
    """

    updated = apply_config_overrides(_ORIGINAL, {"ollama_model": "qwen3:8b"})

    assert updated == _ORIGINAL.replace('ollama_model: "gemma4:e4b"', 'ollama_model: "qwen3:8b"')

    # Yorum satırları ve komşu ayarlar olduğu gibi.
    assert "# Artemis örnek konfigürasyonu." in updated
    assert "# Not: `python main.py --chat` başlangıçta kurulu modelleri listeler." in updated
    assert 'voice_hotkey: "ctrl+alt+a"' in updated


def test_only_the_edited_line_differs_from_the_original() -> None:
    """Satır sayısı ve satır içeriği dışında hiçbir şey değişmemeli.

    Toplu metin karşılaştırması yerine satır bazlı fark, bir yazma
    hatasının (kaydırılan satır, düşen boş satır, eklenen yorum) fark
    edilmesini sağlar.
    """

    updated = apply_config_overrides(_ORIGINAL, {"voice_hotkey": "ctrl+shift+space"})

    before = _ORIGINAL.splitlines()
    after = updated.splitlines()
    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert changed == [('voice_hotkey: "ctrl+alt+a"', 'voice_hotkey: "ctrl+shift+space"')]


# --- apply_config_overrides: biçim ---------------------------------------


def test_bool_is_written_in_lowercase_not_pythons_True() -> None:
    """`str(True)` -> "True" GEÇERSİZ DEĞİL ama bu dosyanın kuralı değil.

    Dosya küçük harfli `true`/`false` kullanıyor (`voice_enabled: true`).
    """

    updated = apply_config_overrides(_ORIGINAL, {"voice_enabled": False})

    assert "voice_enabled: false" in updated
    assert "True" not in updated and "False" not in updated


def test_strings_are_written_quoted() -> None:

    updated = apply_config_overrides(_ORIGINAL, {"ollama_model": "llama3.1:8b"})

    assert 'ollama_model: "llama3.1:8b"' in updated


def test_embedded_quote_is_stripped_instead_of_emitting_invalid_yaml() -> None:
    """Savunmacı davranış: kullanıcının yazdığı bir tırnak dosyayı BOZMAZ.

    Boşluğu olan bir model yazılırsa ("tr-TR-Emel Neural") tırnak
    içindeki tırnak kapanır ve dosya geçersiz YAML olur — Artemis bir
    sonraki açılışta `ConfigError` ile düşer. Tırnak düşürülür.
    """

    updated = apply_config_overrides(_ORIGINAL, {"edge_tts_voice": 'tr-TR-"Emel"Neural'})

    assert 'edge_tts_voice: "tr-TR-EmelNeural"' in updated
    assert updated.count('"tr-TR-EmelNeural"') == 1


def test_an_inline_trailing_comment_on_the_value_line_is_preserved() -> None:
    """Satır sonunda yorum varsa düşürülmez.

    Bugün `config.yaml`'da böyle bir satır YOK; bu, sonradan biri
    eklediğinde sessizce yorumunu kaybetmeyelim diye.
    """

    updated = apply_config_overrides(
        'ollama_model: "gemma4:e4b"  # ETİKET TAM YAZILMALI\n',
        {"ollama_model": "qwen3:8b"},
    )

    assert 'ollama_model: "qwen3:8b"  # ETİKET TAM YAZILMALI' in updated


# --- apply_config_overrides: eksik anahtarı ekleme ----------------------


def test_a_missing_key_is_appended_at_the_end_with_a_comment() -> None:

    updated = apply_config_overrides(_ORIGINAL, {"stt_provider": "local"})

    assert '# Ayarlar penceresinden eklendi:\nstt_provider: "local"' in updated
    # Var olan hiçbir şey değişmemeli.
    assert updated.startswith(_ORIGINAL.rstrip("\n"))


def test_appending_keeps_the_file_parseable() -> None:
    """Eklenen blok, YAML olarak gerçekten okunabilmeli."""

    import yaml

    updated = apply_config_overrides(_ORIGINAL, {"stt_provider": "local", "groq_model": "whisper-tiny"})

    parsed = yaml.safe_load(updated)
    assert parsed["stt_provider"] == "local"
    assert parsed["groq_model"] == "whisper-tiny"
    assert parsed["ollama_model"] == "gemma4:e4b"


def test_empty_file_starts_from_nothing() -> None:
    """Dosya yoksa `save_overrides` boş metinden başlar; burada o yolun kendisi."""

    updated = apply_config_overrides("", {"voice_enabled": True})

    assert "voice_enabled: true" in updated
    assert updated.startswith("# Ayarlar penceresinden eklendi:")


def test_text_without_a_trailing_newline_is_not_joined_into_the_last_line() -> None:
    """Eksik son satır sonu, eklenen anahtarı yapıştırmaz."""

    updated = apply_config_overrides("ollama_model: \"a\"", {"voice_enabled": False})

    assert "\nvoice_enabled: false" in updated
    assert '"a"# Ayarlar' not in updated


# --- Idempotency ---------------------------------------------------------


def test_applying_twice_is_byte_identical() -> None:
    """İkinci turda anahtar ZATEN bulunur, yani "ekle" değil "değiştir"
    yoluna girer; aksi halde her tıkta dosyanın sonu bir blok büyürdü."""

    once = apply_config_overrides(_ORIGINAL, {"stt_provider": "local", "voice_enabled": False})
    twice = apply_config_overrides(once, {"stt_provider": "local", "voice_enabled": False})

    assert twice == once
    assert twice.count("# Ayarlar penceresinden eklendi:") == 1


# --- save_overrides ------------------------------------------------------


def test_save_overrides_creates_the_file_and_leaves_no_tmp_behind(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    save_overrides({"ollama_model": "qwen3:8b"}, path=path)

    assert 'ollama_model: "qwen3:8b"' in path.read_text(encoding="utf-8")
    assert not (tmp_path / "config.yaml.tmp").exists(), "atomik yazım artığı kalmamalı"


def test_save_overrides_preserves_comments_of_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_ORIGINAL, encoding="utf-8")

    save_overrides({"voice_hotkey": "ctrl+alt+s"}, path=path)

    written = path.read_text(encoding="utf-8")
    assert '# Artemis örnek konfigürasyonu.' in written
    assert "# --- Sesli asistan ayarları ---" in written
    assert 'voice_hotkey: "ctrl+alt+s"' in written
    assert 'ollama_model: "gemma4:e4b"' in written


def test_save_overrides_clears_the_lru_cache_so_new_values_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bayat `lru_cache` en sinsi hatadır: kaydetme "başarılı" görünür ama
    çalışan süreçteki herhangi bir `get_settings()` çağrısı ESKİ değeri
    görür. Burada YENİ değerin görünürlüğüyle kanıtlanır (çağrıyı
    saymaktan daha güçlü bir kanıt)."""

    path = tmp_path / "config.yaml"
    path.write_text('ollama_model: "eski"\n', encoding="utf-8")
    monkeypatch.setattr(config_settings, "DEFAULT_CONFIG_PATH", path)

    # Önbelleği eski değerle doldur.
    assert get_settings().ollama_model == "eski"

    save_overrides({"ollama_model": "yeni"})

    assert get_settings().ollama_model == "yeni"
    get_settings.cache_clear()


def test_save_overrides_defaults_to_the_shared_config_path_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yol `config/settings.py::DEFAULT_CONFIG_PATH`'den gelir; burada
    YENİDEN İCAT EDİLMEZ (kurulumun yeri değiştiğinde yanlış dosya
    yazılmasın)."""

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_settings, "DEFAULT_CONFIG_PATH", path)

    save_overrides({"voice_enabled": False})

    assert "voice_enabled: false" in path.read_text(encoding="utf-8")
    get_settings.cache_clear()
