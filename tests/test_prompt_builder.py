"""`core/prompt_builder.py` testleri: geliştirici başlığı ve manifest gömme.

Buradaki testlerin çoğu tek bir GERÇEK arızanın etrafında toplanır:
şablonun geliştirici başlığında `{tool_manifest}` ifadesi ÖRNEK olarak
geçiyordu ve `str.replace()` tüm eşleşmeleri değiştirdiği için tool
listesi prompta İKİ KEZ basılıyordu. Sonuç, her istekte ~8.900 karakter
(~2.200 token) israf ve tool listesini çift gören bir modeldi; ölçülen
etki, tool seçiminin 7.3 saniyeden 1.5 saniyeye inmesiydi.
"""

from __future__ import annotations

from pathlib import Path

from core.plugin_loader import load_plugins
from core.prompt_builder import build_system_prompt, strip_developer_header

load_plugins()


def test_developer_header_is_stripped() -> None:
    """Ayıraçtan önceki bölüm modele GÖNDERİLMEMELİ."""

    template = "Bu bir geliştirici notudur.\n---\nAsıl prompt burada."

    assert strip_developer_header(template) == "Asıl prompt burada."


def test_template_without_separator_is_returned_unchanged() -> None:
    """Ayıraç yoksa metin sessizce boşalmamalı."""

    template = "Ayıraçsız şablon."

    assert strip_developer_header(template) == template


def test_manifest_is_embedded_exactly_once(tmp_path: Path) -> None:
    """REGRESYON: tool listesi prompta BİR KEZ girmeli.

    Şablonun başlığında yer tutucudan söz edilmesi, `str.replace()`
    yüzünden manifesti ikinci kez gömüyordu. Başlık atıldığı için artık
    yalnızca gövdedeki yer tutucu doldurulur.
    """

    template = tmp_path / "sablon.md"
    template.write_text(
        "Başlıkta `{tool_manifest}` yer tutucusundan söz ediliyor.\n"
        "---\n"
        "Kurallar...\n"
        "{tool_manifest}\n",
        encoding="utf-8",
    )

    prompt = build_system_prompt(template)

    # Registry'deki bilinen bir tool adı tam olarak bir kez geçmeli.
    assert prompt.count('"filesystem.create_folder"') == 1
    assert "{tool_manifest}" not in prompt
    assert "Başlıkta" not in prompt


def test_prompt_contains_tools_and_stays_compact() -> None:
    """Gerçek şablon: tool'lar gömülü olmalı ama prompt şişmemeli.

    Üst sınır bir kalite tercihi değil, GECİKME bütçesidir: prompt her
    istekte baştan işlenir ve doğrudan tool seçimi süresine yansır.
    """

    prompt = build_system_prompt()

    assert "filesystem.create_folder" in prompt
    assert "assistant.reply" in prompt
    assert "{tool_manifest}" not in prompt
    assert len(prompt) < 14_000, "sistem promptu beklenenden çok büyüdü (gecikme artar)"


def test_manifest_omits_danger_level() -> None:
    """`danger_level` modele gönderilmez: onayı dispatcher uygular.

    Her tool için fazladan token harcamanın karşılığı yok — model
    yalnızca doğru tool çağrısını üretmekle sorumlu.
    """

    assert "danger_level" not in build_system_prompt()
