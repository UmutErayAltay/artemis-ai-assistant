"""Proje atölyesinin veri modelleri: görüşmeden çıkan spec ve iş durumları.

`ProjectSpec`, proje görüşmesindeki LLM'in ürettiği JSON ile arka plandaki
kodlayıcıya verilen `spec.md` arasındaki SÖZLEŞMEDİR: görüşme modeli ne
üretirse üretsin, buradan geçmeyen bir spec ne diske yazılır ne de bir
kodlayıcıyı başlatır (bkz. `projects/interview.py`).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_TR_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜâîû", "cgiosuCGIOSUaiu")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def slugify(text: str) -> str:
    """Türkçe bir proje adını klasör adına uygun bir kısa ada çevirir.

    "Fiyat Takip Eklentisi" -> "fiyat-takip-eklentisi". Sonuç yine de
    `utils.paths.safe_join` ile kök klasöre bağlanır; bu fonksiyon bir
    güvenlik sınırı değil, okunabilir bir ad üreticisidir.
    """

    ascii_text = text.translate(_TR_ASCII).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:48].rstrip("-")


class Coder(str, Enum):
    """Arka planda kodu kim yazacak.

    Attributes:
        CLAUDE: Claude Code (`claude -p`), ücretli, bütçe tavanıyla.
        UCRETSIZ: Aynı CLI, cor üzerinden ücretsiz bir OpenRouter modeliyle.
    """

    CLAUDE = "claude"
    UCRETSIZ = "ucretsiz"


class JobStatus(str, Enum):
    """Arka plandaki bir kodlama işinin durumu.

    `CALISIYOR` dışındaki her durum ya kullanıcı bekler (`SORU_BEKLIYOR`)
    ya da sondur. `YARIM_KALDI`: kayıt "çalışıyor" derken süreç artık yok
    (makine kapandı, süreç çöktü) — bunu "çalışıyor" diye göstermeye
    devam etmek, hiçbir şey yapmadan "başardım" demenin akrabasıdır.
    """

    CALISIYOR = "calisiyor"
    SORU_BEKLIYOR = "soru_bekliyor"
    TAMAMLANDI = "tamamlandi"
    BASARISIZ = "basarisiz"
    DURDURULDU = "durduruldu"
    YARIM_KALDI = "yarim_kaldi"


TERMINAL_STATUSES = frozenset({JobStatus.TAMAMLANDI, JobStatus.BASARISIZ, JobStatus.DURDURULDU, JobStatus.YARIM_KALDI})


class ProjectSpec(BaseModel):
    """Görüşmenin çıktısı; kodlayıcının çalışacağı tek kaynak.

    Attributes:
        ad: İnsan-okur proje adı.
        slug: Klasör adı (küçük harf, rakam, tire). Model geçersiz bir
            değer üretirse `ad`'dan türetilir.
        amac: Projenin çözdüğü dert, bir-iki cümle.
        kullanici: Kim kullanacak.
        ozellikler: M1'de olması gereken özellikler.
        teknoloji: Dil/çatı/veritabanı seçimi.
        m1_bitti_olcutu: M1'in bittiğini gösteren gözlenebilir ölçüt
            (örn. "pytest yeşil ve `python main.py` liste gösteriyor").
        kodlayici: Varsayılan kodlayıcı önerisi.
        notlar: Görüşmede çıkan ek kısıtlar.
    """

    ad: str = Field(min_length=1, max_length=80)
    slug: str = ""
    amac: str = Field(min_length=1)
    kullanici: str = Field(min_length=1)
    ozellikler: list[str] = Field(min_length=1)
    teknoloji: str = Field(min_length=1)
    m1_bitti_olcutu: str = Field(min_length=1)
    kodlayici: Literal["claude", "ucretsiz"] = "claude"
    notlar: str = ""

    @field_validator("ozellikler")
    @classmethod
    def _non_empty_features(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("en az bir özellik gerekli")
        return cleaned

    @model_validator(mode="after")
    def _derive_slug(self) -> ProjectSpec:
        # Model slug'ı sık sık boşluklu/Türkçe karakterli üretir; reddetmek
        # yerine `ad`'dan türetilir. Türetilemiyorsa (ad yalnızca simge)
        # spec geçersizdir — klasör adı tahmin edilmez.
        if not _SLUG_RE.match(self.slug or ""):
            self.slug = slugify(self.slug) or slugify(self.ad)
        if not _SLUG_RE.match(self.slug):
            raise ValueError(f"proje adı klasör adına çevrilemedi: {self.ad!r}")
        return self

    def to_markdown(self) -> str:
        """Kodlayıcıya verilecek `spec.md` içeriği."""

        features = "\n".join(f"- {item}" for item in self.ozellikler)
        notes = f"\n## Notlar\n\n{self.notlar.strip()}\n" if self.notlar.strip() else ""
        return (
            f"# {self.ad}\n\n"
            f"> Bu dosya Artemis'in proje görüşmesinden üretildi; kodlayıcının sözleşmesidir.\n\n"
            f"## Amaç\n\n{self.amac.strip()}\n\n"
            f"## Kim kullanacak\n\n{self.kullanici.strip()}\n\n"
            f"## M1 özellikleri\n\n{features}\n\n"
            f"## Teknoloji\n\n{self.teknoloji.strip()}\n\n"
            f"## M1 bitti ölçütü\n\n{self.m1_bitti_olcutu.strip()}\n"
            f"{notes}"
        )

    def summary(self) -> str:
        """Kullanıcıya okunacak kısa özet (ses modunda da kısa kalsın)."""

        features = "; ".join(self.ozellikler[:5])
        return (
            f"{self.ad} ({self.slug}): {self.amac.strip()} "
            f"Teknoloji: {self.teknoloji.strip()}. M1: {features}. "
            f"Bitti ölçütü: {self.m1_bitti_olcutu.strip()}"
        )
