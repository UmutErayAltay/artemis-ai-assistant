"""Sistem promptu şablonunu doldurur.

`prompts/system_prompt.md` iki bölümden oluşur; ayıraç tek başına bir
`---` satırıdır:

    <geliştirici başlığı>      -> bu dosyanın ne olduğunu ANLATIR
    ---
    <asıl sistem promptu>      -> modele GÖNDERİLEN metin

Bu modül başlığı ATAR ve yalnızca ayıraçtan sonrasını gönderir; ardından
`{tool_manifest}` yer tutucusunu `core.manifest` üzerinden GÜNCEL registry
ile doldurur — yeni bir tool eklendiğinde şablon dosyasına dokunulmaz.

NEDEN BAŞLIK ATILIYOR (ölçülmüş bir hatanın çözümü):
    Başlıkta, yer tutucunun ne işe yaradığını anlatmak için `{tool_manifest}`
    ifadesi ÖRNEK olarak geçiyordu. `str.replace()` tüm eşleşmeleri
    değiştirdiği için tool listesi prompta İKİ KEZ basılıyordu:

        şablon 4.023 + manifest 8.915  ->  beklenen ~12.900 karakter
        gerçekte üretilen              ->        21.823 karakter

    Yani her istekte ~8.900 karakter (~2.200 token) boşa gidiyor ve model
    tool listesini çift görüyordu. Başlığı atmak hem bu kopyayı hem de
    modele hitap etmeyen geliştirici metnini prompttan çıkarır.
"""

from __future__ import annotations

from pathlib import Path

from core.manifest import build_tool_manifest_json

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"

_HEADER_SEPARATOR = "\n---\n"
"""Geliştirici başlığını asıl promptan ayıran işaret (tek başına bir `---` satırı)."""

_PLACEHOLDER = "{tool_manifest}"


def strip_developer_header(template: str) -> str:
    """Şablonun geliştiriciye yönelik başlığını atar.

    Ayıraç bulunamazsa metin olduğu gibi döner — böylece ayıraçsız bir
    şablon (ya da testlerdeki küçük parçalar) sessizce boşalmaz.
    """

    _, separator, body = template.partition(_HEADER_SEPARATOR)
    return body if separator else template


def build_system_prompt(template_path: Path | None = None) -> str:
    """Şablonu okur, geliştirici başlığını atar ve tool manifestini gömer.

    Args:
        template_path: Alternatif şablon yolu (testler için).

    Returns:
        Ollama'ya `system` rolüyle gönderilecek tam metin.
    """

    path = template_path or _TEMPLATE_PATH
    template = strip_developer_header(path.read_text(encoding="utf-8"))
    return template.replace(_PLACEHOLDER, build_tool_manifest_json())
