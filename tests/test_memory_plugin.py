"""`plugins/memory_plugin.py` içindeki üç tool'un dispatcher üzerinden testleri.

Gerçek `ToolDispatcher` + gerçek (tmp) SQLite kullanılır, mock yok.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from memory.context_memory import ContextMemory


@pytest.fixture
def dispatcher(tmp_path: Path) -> ToolDispatcher:
    settings = Settings(desktop_path=tmp_path, db_path=tmp_path / "memory.db", log_dir=tmp_path / "logs")
    return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))


def test_remember_then_recall_roundtrip(dispatcher: ToolDispatcher) -> None:
    remember_result = dispatcher.dispatch(
        {"tool": "memory.remember", "arguments": {"key": "wifi şifresi", "value": "12345"}}
    )
    assert remember_result.success is True

    recall_result = dispatcher.dispatch({"tool": "memory.recall", "arguments": {"key": "wifi şifresi"}})

    assert recall_result.success is True
    assert recall_result.message == "12345"
    assert recall_result.data == {"key": "wifi şifresi", "value": "12345"}


def test_recall_unknown_key_is_a_honest_failure_not_a_hallucination(dispatcher: ToolDispatcher) -> None:
    """Model, olmayan bir bilgiyi UYDURMAMALI — bulunamadı diye dürüstçe
    başarısız dönmeli (bkz. CLAUDE.md, koşulsuz success=True yasağı)."""

    result = dispatcher.dispatch({"tool": "memory.recall", "arguments": {"key": "hiç söylenmemiş şey"}})

    assert result.success is False
    assert "hatırlamıyorum" in result.message


def test_remember_overwrites_previous_value(dispatcher: ToolDispatcher) -> None:
    dispatcher.dispatch({"tool": "memory.remember", "arguments": {"key": "renk", "value": "mavi"}})
    dispatcher.dispatch({"tool": "memory.remember", "arguments": {"key": "renk", "value": "kırmızı"}})

    result = dispatcher.dispatch({"tool": "memory.recall", "arguments": {"key": "renk"}})

    assert result.message == "kırmızı"


def test_remember_rejects_empty_key_or_value(dispatcher: ToolDispatcher) -> None:
    empty_key = dispatcher.dispatch({"tool": "memory.remember", "arguments": {"key": "  ", "value": "x"}})
    empty_value = dispatcher.dispatch({"tool": "memory.remember", "arguments": {"key": "x", "value": "  "}})

    assert empty_key.success is False
    assert empty_value.success is False


def test_forget_removes_a_remembered_fact(dispatcher: ToolDispatcher) -> None:
    dispatcher.dispatch({"tool": "memory.remember", "arguments": {"key": "renk", "value": "mavi"}})

    forget_result = dispatcher.dispatch({"tool": "memory.forget", "arguments": {"key": "renk"}})
    assert forget_result.success is True
    assert "unutuldu" in forget_result.message

    recall_result = dispatcher.dispatch({"tool": "memory.recall", "arguments": {"key": "renk"}})
    assert recall_result.success is False


def test_forget_unknown_key_is_an_honest_failure_not_a_silent_success(dispatcher: ToolDispatcher) -> None:
    """Hiç hatırlanmamış bir şeyi 'unutmak' TEHLİKELİ bir işlem değil, ama
    KOŞULSUZ success=True de OLAMAZ (CLAUDE.md: "koşulsuz success=True
    yasak"). Bu tool bir dönem hiçbir şey silinmese de `success=True`
    dönüyordu — tam da `memory.recall`'un aynı dosyada dürüstçe
    `success=False` döndürdüğü "bulunamadı" durumunu. Mesaj zararsız ve
    bilgilendirici kalır; yalnızca `success` alanı artık gerçeği yansıtır.
    """

    result = dispatcher.dispatch({"tool": "memory.forget", "arguments": {"key": "hiç var olmamış"}})

    assert result.success is False
    assert "zaten" in result.message


def test_missing_required_argument_fails_cleanly(dispatcher: ToolDispatcher) -> None:
    """Merkezi argüman doğrulaması (README §24) memory.* için de çalışmalı."""

    result = dispatcher.dispatch({"tool": "memory.remember", "arguments": {"key": "x"}})

    assert result.success is False
    assert "value" in result.message


def test_remembering_key_named_last_path_does_not_break_location_last(dispatcher: ToolDispatcher) -> None:
    """UÇTAN UCA KRİTİK REGRESYON: `memory.remember` ile `key="last_path"`
    gönderilse bile, `filesystem.open(location="last")`'ın dayandığı
    GERÇEK son-yol bilgisi bozulmamalı (bkz. `tests/test_memory_context.py`
    aynı korumanın sınıf-seviyesi karşılığı; bu test dispatcher/tool
    katmanından uçtan uca doğrular).
    """

    # `filesystem.create_file`'ın KENDİSİ gerçek dosyayı oluşturur; ayrıca
    # ELLE bir ön-yazma YAPILMAZ — `create_file` artık üzerine yazmayı
    # `overwrite=True` verilmeden reddediyor (bkz. Faz 2 düzeltmesi), ayrı
    # bir ön-yazma bu dispatch'i sessizce başarısız kılıp `remember_last_path`
    # hiç çağrılmamasına yol açardı.
    dispatcher.dispatch({"tool": "filesystem.create_file", "arguments": {"name": "gercek.txt"}})

    # Kullanıcı kazayla (ya da meraktan) "last_path" adıyla bir şey hatırlatıyor.
    dispatcher.dispatch(
        {"tool": "memory.remember", "arguments": {"key": "last_path", "value": "kullanıcı verisi"}}
    )

    # Kullanıcının kendi verisi ayrı namespace'te duruyor.
    user_fact = dispatcher.dispatch({"tool": "memory.recall", "arguments": {"key": "last_path"}})
    assert user_fact.message == "kullanıcı verisi"

    # Gerçek iç mekanizma (location="last") hâlâ doğru dosyayı işaret ediyor.
    assert dispatcher.memory.get_last_path() is not None
    assert "kullanıcı verisi" not in dispatcher.memory.get_last_path()
