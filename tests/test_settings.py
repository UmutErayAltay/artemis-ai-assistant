"""`config/settings.py` testleri: yükleme, doğrulama ve gizli anahtarlar.

Bu dosya bir boşluğu kapatıyor: `tests/test_config_model.py` `Settings`
sınıfını HİÇ import etmiyor — yalnızca `config.yaml`'ı `yaml.safe_load`
edip iki dizge özelliğini denetliyor. Yani `get_settings()`, YAML
birleştirme, doğrulama ve tüm gizli-anahtar çözümlemesi (projenin
güvenliğe en duyarlı kısmı) test edilmemişti.

Hiçbir test gerçek ortam değişkenlerine ya da gerçek `config/config.yaml`'a
dokunmaz: her biri `tmp_path` altında kendi dosyasını kurar.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import (
    ConfigError,
    Settings,
    get_azure_speech_credentials,
    get_groq_api_key,
    get_settings,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- Yükleme ve doğrulama -----------------------------------------------


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    settings = get_settings(tmp_path / "yok.yaml")

    assert settings.ollama_model == Settings().ollama_model


def test_values_from_yaml_override_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.yaml", 'ollama_model: "test:1b"\nvoice_enabled: false\n')

    settings = get_settings(path)

    assert settings.ollama_model == "test:1b"
    assert settings.voice_enabled is False


def test_unknown_key_is_rejected_instead_of_silently_ignored(tmp_path: Path) -> None:
    """SESSİZ YOK SAYMA YASAK: yazım hatası, ayarın uygulandığı yanılgısı yaratır.

    pydantic v2'nin varsayılanı `extra="ignore"`. Bu ayar olmadan
    `whisper_devcie: "cuda"` hiçbir uyarı üretmeden görmezden geliniyor
    ve kullanıcı, ayarı değiştirdiğini sanarak var olmayan bir hatayı
    ayıklıyordu. Aynı sessizlik `dangerous_tools` gibi GÜVENLİK ayarları
    için de geçerliydi.
    """

    path = _write(tmp_path / "c.yaml", 'whisper_devcie: "cuda"\n')

    with pytest.raises(ConfigError) as exc:
        get_settings(path)

    assert "whisper_devcie" in str(exc.value)


def test_invalid_enum_value_is_rejected_at_startup(tmp_path: Path) -> None:
    """`whisper_device: "auto"` açılışta reddedilmeli.

    Alan docstring'i "**'auto' KULLANMAYIN**" diye uyarıyordu: faster-whisper
    "auto"yu görünce CUDA'yı seçer ve hata MODEL YÜKLENİRKEN değil İLK
    TANIMA sırasında patlar. Bir prosa uyarısı yerine tip sistemi bunu
    zorlayabilir.
    """

    path = _write(tmp_path / "c.yaml", 'whisper_device: "auto"\n')

    with pytest.raises(ConfigError):
        get_settings(path)


def test_broken_yaml_gives_a_readable_error_not_a_traceback(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.yaml", "a: [1,\n")

    with pytest.raises(ConfigError) as exc:
        get_settings(path)

    assert str(path) in str(exc.value)


def test_yaml_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.yaml", "- bir\n- iki\n")

    with pytest.raises(ConfigError):
        get_settings(path)


def test_settings_are_read_only(tmp_path: Path) -> None:
    """Ayarlar TEK ve PAYLAŞILAN bir nesne; hiçbir tool onu değiştirememeli.

    `get_settings` `lru_cache`'li ve sonuç her tool'a `ToolContext` ile
    veriliyor. Dondurulmamış bir model, herhangi bir tool'un yolları ya da
    `dangerous_tools` listesini hiçbir iz bırakmadan değiştirebilmesi
    demekti.
    """

    settings = get_settings(tmp_path / "yok.yaml")

    with pytest.raises(ValidationError):
        settings.ollama_model = "baska"


def test_model_copy_is_the_supported_way_to_derive_a_variant(tmp_path: Path) -> None:
    """Dondurulmuş olmak "türetilemez" demek değil — testlerin yolu budur."""

    settings = get_settings(tmp_path / "yok.yaml")

    turetilmis = settings.model_copy(update={"voice_enabled": False})

    assert turetilmis.voice_enabled is False
    assert settings.voice_enabled is True, "orijinal nesne DEĞİŞMEMELİ"


# --- Gizli anahtarlar ----------------------------------------------------
#
# Anahtarlar BİLİNÇLİ olarak `Settings`/`config.yaml` üzerinden gelmez:
# `config.yaml` versiyon kontrolünde ve bu depo herkese açık.


def test_groq_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "  gizli-anahtar  ")

    assert get_groq_api_key() == "gizli-anahtar"


def test_groq_key_is_none_when_nowhere_to_be_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Anahtarın yokluğu bir HATA DEĞİLDİR — yalnızca bulut devre dışı kalır."""

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("config.settings.SECRETS_PATH", tmp_path / "yok.yaml")

    assert get_groq_api_key() is None


def test_groq_key_falls_back_to_the_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    secrets = _write(tmp_path / "secrets.yaml", 'groq_api_key: "dosyadan-gelen"\n')
    monkeypatch.setattr("config.settings.SECRETS_PATH", secrets)

    assert get_groq_api_key() == "dosyadan-gelen"


def test_environment_wins_over_the_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "ortamdan")
    monkeypatch.setattr(
        "config.settings.SECRETS_PATH", _write(tmp_path / "s.yaml", 'groq_api_key: "dosyadan"\n')
    )

    assert get_groq_api_key() == "ortamdan"


def test_corrupt_secrets_file_does_not_crash_the_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bozuk bir gizli dosya uygulamayı çökertmemeli, yalnızca anahtarsız bırakmalı."""

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("config.settings.SECRETS_PATH", _write(tmp_path / "s.yaml", "a: [1,\n"))

    assert get_groq_api_key() is None


def test_azure_credentials_need_both_key_and_region(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_SPEECH_KEY", "anahtar")
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.setattr("config.settings.SECRETS_PATH", tmp_path / "yok.yaml")

    key, region = get_azure_speech_credentials()

    assert key == "anahtar"
    assert region is None, "bölge yoksa None dönmeli (ikisinden biri eksikse bulut kullanılamaz)"
