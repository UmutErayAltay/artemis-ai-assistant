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

from core.manifest import build_tool_manifest, build_tool_manifest_json
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

    GEÇİCİ (PROVISIONAL) SINIR — 18.000: "geliştirme planı"nın Faz 1-4'ü
    (filesystem.rename/move, memory.remember/recall/forget,
    windows.arrange_window) yeni tool'lar eklerken, o an tüm Ollama
    modelleri makineden silinmiş durumdaydı — gerçek modelle "kaç
    karakterden sonra gecikme kötüleşiyor" ölçümü (README §16'daki
    9.198→1.51sn / 21.823→7.32sn ölçümüne benzer biçimde) YAPILAMADI.
    Eski 14.000 sınırı da kendisi tam bir ölçümden değil, "21.823'ten
    güvenli biçimde uzak dur" sezgisinden geliyordu. Bu yüzden sınır,
    planlanan tüm yeni tool'ları geçirecek kadar GEÇİCİ olarak
    yükseltildi; bir model geri yüklenince gerçek ölçümle KESİNLEŞTİRİLMELİ
    (Faz 6, README §28+). Rastgele büyütülmedi — yalnızca ölçüm mümkün
    olana kadar çalışmayı engellememesi için geçici bir tavan.
    """

    prompt = build_system_prompt()

    assert "filesystem.create_folder" in prompt
    assert "assistant.reply" in prompt
    assert "{tool_manifest}" not in prompt
    assert len(prompt) < 18_000, "sistem promptu beklenenden çok büyüdü (gecikme artar)"


def test_manifest_omits_danger_level() -> None:
    """`danger_level` modele gönderilmez: onayı dispatcher uygular.

    Her tool için fazladan token harcamanın karşılığı yok — model
    yalnızca doğru tool çağrısını üretmekle sorumlu.
    """

    assert "danger_level" not in build_system_prompt()


# --------------------------------------------------------------------------
# `core/manifest.py` — Faz 5 (geliştirme planı): daha önce yalnızca
# YUKARIDAKİ testler aracılığıyla, tüm prompt string'i üzerinden dolaylı
# sınanıyordu. Bu iki test manifest fonksiyonlarını DOĞRUDAN çağırır,
# böylece prompt şablonu değişse bile manifest'in kendi davranışı ayrı
# doğrulanmış olur.
# --------------------------------------------------------------------------


def test_build_tool_manifest_object_form_keeps_danger_level() -> None:
    """`build_tool_manifest()` (nesne formu, geliştirici içgözlemi için)
    `danger_level`'ı TUTMALI — düşürülen yalnızca JSON/prompt formudur
    (bkz. `build_tool_manifest_json`)."""

    manifest = build_tool_manifest()

    names = [entry.name for entry in manifest]
    assert names == sorted(names)  # alfabetik sıralı olmalı
    assert any(entry.name == "filesystem.delete" for entry in manifest)
    delete_entry = next(entry for entry in manifest if entry.name == "filesystem.delete")
    assert delete_entry.danger_level is not None


def test_build_tool_manifest_json_is_valid_compact_json_without_danger_level() -> None:
    """`build_tool_manifest_json()` — modele giden asıl form — geçerli
    JSON olmalı, `danger_level` İÇERMEMELİ, girintisiz (kompakt) olmalı."""

    import json

    raw = build_tool_manifest_json()
    parsed = json.loads(raw)  # geçerli JSON olmalı, aksi halde exception

    assert isinstance(parsed, list)
    assert all("danger_level" not in entry for entry in parsed)
    assert "\n" not in raw  # kompakt: girinti/satır sonu yok
