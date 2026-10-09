"""`utils/tool_labels.py` testleri: adım etiketleri Türkçe, bilinmeyen ad ham kalır."""

from __future__ import annotations

from utils.tool_labels import tool_label


def test_known_tools_get_turkish_labels() -> None:
    assert tool_label("web.open_url") == "Tarayıcıda açma"
    assert tool_label("filesystem.create_folder") == "Klasör oluşturma"


def test_unknown_tool_falls_back_to_its_raw_name() -> None:
    assert tool_label("mcp.bilinmeyen_arac") == "mcp.bilinmeyen_arac"


def test_every_registered_builtin_tool_has_a_label() -> None:
    """Yeni bir tool eklenince etiket tablosu da güncellenmeli (aksi halde ham ad görünür)."""

    from core.plugin_loader import TOOL_REGISTRY, load_plugins

    load_plugins()
    unlabeled = [name for name in TOOL_REGISTRY if tool_label(name) == name and not name.startswith("mcp.")]

    assert unlabeled == [], f"Etiketi eksik tool'lar: {unlabeled}"
