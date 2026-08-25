"""SQLite tabanlı basit bağlam hafızası.

Artemis'in "Masaüstündeki Orbit klasörünü aç" -> "İçindeki app.py
dosyasını aç" gibi ardışık komutları anlayabilmesi için son kullanılan
yol/klasör gibi bilgileri kalıcı olarak tutar.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class ContextMemory:
    """Anahtar-değer tabanlı, SQLite'ta kalıcı bağlam hafızası.

    Basit tutulmuştur: ileride konuşma geçmişi, kullanıcı tercihleri gibi
    yeni hafıza türleri eklenmek istendiğinde bu sınıfa yeni bir tablo
    ve yeni convenience metotları (örn. `remember_last_command`)
    eklemek yeterlidir; dispatcher veya tool'larda değişiklik gerekmez.

    Args:
        db_path: SQLite veritabanı dosyasının yolu.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def set(self, key: str, value: str) -> None:
        """Bir anahtar-değer çiftini kaydeder veya günceller."""

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO context_memory (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    def get(self, key: str) -> str | None:
        """Bir anahtara karşılık gelen değeri döndürür, yoksa None."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM context_memory WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def delete(self, key: str) -> bool:
        """Bir anahtarı siler.

        Returns:
            Anahtar gerçekten VAR olup silindiyse True; zaten yoksa
            False (çağıran tarafın "zaten yoktu" ile "silindi" ayrımını
            yapabilmesi için — bkz. `forget_fact`).
        """

        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM context_memory WHERE key = ?", (key,))
        return cursor.rowcount > 0

    # --- Sık kullanılan senaryolar için convenience metotlar ---

    def remember_last_path(self, path: str) -> None:
        """Son işlem yapılan dosya/klasör yolunu hatırlar."""

        self.set("last_path", path)

    def get_last_path(self) -> str | None:
        """Son hatırlanan dosya/klasör yolunu döndürür."""

        return self.get("last_path")

    # --- Kullanıcının LLM aracılığıyla hatırlattığı serbest bilgiler ---
    #
    # `fact:` ÖNEKİ BİLEREK VAR: `memory.remember` tool'u (bkz.
    # plugins/memory_plugin.py) kullanıcının söylediği HERHANGİ bir
    # anahtarı kabul eder. Önek olmadan, model (ya da kullanıcı) yanlışlıkla
    # `key="last_path"` gönderirse yukarıdaki `remember_last_path`'in
    # kullandığı İÇ anahtarı ezer ve `location: "last"` özelliğini
    # sessizce bozardı. Önek, kullanıcı-hatırlattığı verilerle sistemin
    # kendi iç durumunu AYNI ad alanında asla çakıştırmaz.

    def remember_fact(self, key: str, value: str) -> None:
        """Kullanıcının söylediği bir bilgiyi (`memory.remember`) hatırlar."""

        self.set(f"fact:{key}", value)

    def recall_fact(self, key: str) -> str | None:
        """Daha önce `remember_fact` ile kaydedilmiş bir bilgiyi döndürür."""

        return self.get(f"fact:{key}")

    def forget_fact(self, key: str) -> bool:
        """Daha önce hatırlanmış bir bilgiyi siler.

        Returns:
            Bilgi gerçekten var olup silindiyse True, zaten hatırlanmıyorsa
            False (tool'un "zaten hatırlamıyordum" ile "unuttum" mesajını
            doğru seçebilmesi için).
        """

        return self.delete(f"fact:{key}")
