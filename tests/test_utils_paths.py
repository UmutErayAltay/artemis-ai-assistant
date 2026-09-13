"""`utils/paths.py` testleri.

Ayrıntılı davranış (`resolve_location`/`safe_join`'in tüm kenar
durumları) zaten `tests/test_filesystem_plugin.py`'de, bu fonksiyonların
`plugins.filesystem_plugin`'den yeniden dışa verilen `_resolve_location`/
`_safe_join` takma adları üzerinden kapsanıyor. Bu dosya yalnızca
YENİDEN DIŞA VERMENİN gerçek bir kopya değil, AYNI fonksiyona işaret
ettiğini doğrular — `plugins/windows_plugin.py`'nin artık
`plugins.filesystem_plugin`'in private bir sembolüne değil, doğrudan bu
modüle bağlı olduğunu kanıtlayan regresyon testidir.
"""

from __future__ import annotations

from utils.paths import resolve_location, safe_join, unsafe_target_result


def test_filesystem_plugin_reexports_the_same_function_object() -> None:
    import plugins.filesystem_plugin as filesystem_plugin
    import plugins.windows_plugin as windows_plugin

    assert filesystem_plugin._resolve_location is resolve_location
    assert filesystem_plugin._safe_join is safe_join
    assert filesystem_plugin._unsafe_target_result is unsafe_target_result
    # `windows_plugin` artık `filesystem_plugin`'in private isim alanına
    # DEĞİL, doğrudan `utils.paths`'e bağlı olmalı.
    assert windows_plugin._resolve_location is resolve_location
