"""Paketleme sözleşmesinin bekçileri.

Buradaki her test, GERÇEKTEN yaşanmış bir kurulum arızasını sabitler.
Hiçbiri kod çalıştırmaz; yalnızca `pyproject.toml` ile `requirements.txt`
arasındaki tutarlılığı ve paket listesinin eksiksizliğini denetler.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _requirement_names(text: str) -> set[str]:
    """Bir requirements metninden paket ADLARINI çıkarır (sürüm/işaret hariç)."""

    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!;\s\[]", line, maxsplit=1)[0].strip()
        if name:
            names.add(name.lower().replace("_", "-"))
    return names


def test_importable_packages_are_all_declared() -> None:
    """`voice` ve `ui` bir dönem `packages.find`'da YOKTU.

    Sonuç: `pip install .` ile kurulan wheel `voice.router`'ı ya da
    `ui.overlay`'i import EDEMİYORDU — yani kurulum çalışmıyordu ve bunu
    hiçbir şey söylemiyordu.
    """

    include = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    declared = {entry.rstrip("*") for entry in include}

    # Depodaki her gerçek Python paketi (içinde __init__.py olan klasör)
    # bildirilmiş olmalı.
    for path in sorted(REPO_ROOT.iterdir()):
        if not path.is_dir() or not (path / "__init__.py").exists():
            continue
        if path.name in {"tests", "scripts", "skills"}:
            continue  # dağıtıma girmesi gerekmeyenler
        assert path.name in declared, (
            f"'{path.name}' bir Python paketi ama pyproject.toml::packages.find "
            "içinde bildirilmemiş; kurulan wheel onu import edemez."
        )


def test_runtime_prompt_file_is_shipped_as_package_data() -> None:
    """`core/prompt_builder.py` `prompts/system_prompt.md`'yi ÇALIŞMA
    ZAMANINDA okur; paket verisi olarak bildirilmezse wheel'e girmez ve
    asistan sistem promptunu bulamaz."""

    package_data = _pyproject()["tool"]["setuptools"]["package-data"]

    assert "prompts" in package_data
    assert any(pattern.endswith(".md") for pattern in package_data["prompts"])


def test_pyproject_and_requirements_do_not_drift() -> None:
    """İki bağımlılık listesi AYRIŞMAMALI.

    Bir dönem `pyproject.toml` 6, `requirements.txt` 15 paket sayıyordu:
    `pip install .` sesi, arayüzü ve MCP'yi olmayan çalışmayan bir kurulum
    üretiyordu. İki liste ayrı ayrı elle güncellendiği sürece bu tekrar
    olur; bu test onu yakalar.
    """

    declared = _requirement_names("\n".join(_pyproject()["project"]["dependencies"]))
    required = _requirement_names((REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"))

    # `pytest` yalnızca geliştirme bağımlılığıdır; requirements.txt'te
    # bulunması tarihsel bir kalıntı, dağıtım bağımlılığı değil.
    required -= {"pytest"}

    eksik = required - declared
    fazla = declared - required

    assert not eksik, f"requirements.txt'te olup pyproject.toml'da olmayan: {sorted(eksik)}"
    assert not fazla, f"pyproject.toml'da olup requirements.txt'te olmayan: {sorted(fazla)}"


def test_disruptive_marker_is_declared_and_excluded_by_default() -> None:
    """`disruptive` mekanizması, geliştiricinin ekranının her test turunda
    kilitlenmesi arızasının çözümüydü (CLAUDE.md). İki parçası da yerinde
    olmalı: işaret BİLDİRİLMİŞ ve varsayılan koşudan DIŞLANMIŞ."""

    pytest_config = _pyproject()["tool"]["pytest"]["ini_options"]

    assert any(marker.startswith("disruptive:") for marker in pytest_config["markers"])
    assert "not disruptive" in pytest_config["addopts"]


def test_unknown_markers_are_an_error() -> None:
    """`--strict-markers` olmadan `@pytest.mark.disruptiv` (yazım hatası)
    sessizce kaydedilir ve test ÇALIŞIR — yani `disruptive`'in var olma
    sebebi olan regresyon, bir harf eksikliğiyle geri gelir."""

    assert "--strict-markers" in _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
