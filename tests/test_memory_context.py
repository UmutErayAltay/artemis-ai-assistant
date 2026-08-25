"""`memory/context_memory.py` testleri.

Bu modül daha önce HİÇ doğrudan test edilmemişti — yalnızca dolaylı
olarak (`filesystem.open` gibi tool'lar üzerinden `remember_last_path`/
`get_last_path` çağrılarak) sınanıyordu. `memory.remember`/`recall`/
`forget` tool'ları (v3.9, bkz. `plugins/memory_plugin.py`) bu sınıfa
YENİ, kullanıcıya doğrudan açık bir sorumluluk yüklediği için önce
sınıfın kendisi doğrudan test edilir.

Gerçek bir SQLite dosyası kullanılır (`tmp_path`), mock yok.
"""

from __future__ import annotations

from pathlib import Path

from memory.context_memory import ContextMemory


def test_set_and_get_roundtrip(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    memory.set("renk", "mavi")

    assert memory.get("renk") == "mavi"


def test_get_missing_key_returns_none(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    assert memory.get("olmayan") is None


def test_set_overwrites_existing_value(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    memory.set("renk", "mavi")
    memory.set("renk", "kırmızı")

    assert memory.get("renk") == "kırmızı"


def test_delete_removes_key_and_reports_it_existed(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")
    memory.set("renk", "mavi")

    existed = memory.delete("renk")

    assert existed is True
    assert memory.get("renk") is None


def test_delete_missing_key_reports_false(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    existed = memory.delete("hiç-var-olmamış")

    assert existed is False


def test_persists_across_separate_connections(tmp_path: Path) -> None:
    """SQLite dosyası kalıcı olmalı — aynı yola ikinci bir `ContextMemory`
    açıldığında önceki verinin görünmesi gerekir (uygulama yeniden
    başlatıldığında hafızanın hayatta kalması budur)."""

    db_path = tmp_path / "m.db"
    ContextMemory(db_path).set("renk", "mavi")

    reopened = ContextMemory(db_path)

    assert reopened.get("renk") == "mavi"


def test_last_path_convenience_methods(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    memory.remember_last_path("C:/Users/x/Desktop/Orbit")

    assert memory.get_last_path() == "C:/Users/x/Desktop/Orbit"


def test_last_path_defaults_to_none(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    assert memory.get_last_path() is None


# --- fact:* — memory.remember/recall/forget'ın dayandığı namespace ---


def test_remember_fact_and_recall_fact_roundtrip(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    memory.remember_fact("wifi şifresi", "12345")

    assert memory.recall_fact("wifi şifresi") == "12345"


def test_recall_fact_missing_returns_none(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    assert memory.recall_fact("hiç hatırlanmamış") is None


def test_forget_fact_removes_it(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")
    memory.remember_fact("wifi şifresi", "12345")

    existed = memory.forget_fact("wifi şifresi")

    assert existed is True
    assert memory.recall_fact("wifi şifresi") is None


def test_forget_fact_missing_reports_false(tmp_path: Path) -> None:
    memory = ContextMemory(tmp_path / "m.db")

    assert memory.forget_fact("hiç hatırlanmamış") is False


def test_fact_namespace_never_collides_with_internal_last_path(tmp_path: Path) -> None:
    """KRİTİK REGRESYON KORUMASI: kullanıcı `memory.remember` ile
    key="last_path" gönderse bile (kazayla ya da kasıtlı), İÇ
    `remember_last_path`/`get_last_path`'in kullandığı GERÇEK `last_path`
    anahtarı ETKİLENMEMELİ — aksi halde `filesystem.open(location="last")`
    sessizce bozulurdu (bkz. `plugins/memory_plugin.py` modül dokümanı).
    """

    memory = ContextMemory(tmp_path / "m.db")
    memory.remember_last_path("C:/gercek/son/yol")

    # Kullanıcı "last_path" diye bir şey hatırlatmaya çalışıyor.
    memory.remember_fact("last_path", "kullanıcının kendi verisi")

    # İç mekanizma bozulmamalı.
    assert memory.get_last_path() == "C:/gercek/son/yol"
    # Kullanıcının verisi de kendi namespace'inde ayrı duruyor.
    assert memory.recall_fact("last_path") == "kullanıcının kendi verisi"
