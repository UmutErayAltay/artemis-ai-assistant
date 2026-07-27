"""`config/config.yaml` içindeki model adının kullanılabilir olmasını sınar.

Buradaki tek test GERÇEK bir arızadan doğdu: dosyada `ollama_model:
"llama3.1"` yazılıydı. Ollama etiketsiz bir adı `llama3.1:latest` diye
çözer; kurulu olan ise `llama3.1:8b` idi. Sonuç, "model yok" gibi
okunmayan bir `ConnectionError`'dı — ve arıza yalnızca interaktif model
seçimi atlandığında ortaya çıktığı için uzun süre görünmedi.

Test canlı bir Ollama sunucusu gerektirmez; yalnızca yapılandırmanın
kendi kendine yeter olup olmadığına bakar.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def _yapilandirma() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_model_adi_acik_etiket_icerir() -> None:
    """REGRESYON: model adı `ad:etiket` biçiminde tam yazılmalı.

    Etiket yazılmazsa Ollama sessizce `:latest`'i dener; o etiket kurulu
    değilse asistan, sebebi belli olmayan bir bağlantı hatasıyla düşer.
    """

    model = _yapilandirma()["ollama_model"]

    assert ":" in model, (
        f"ollama_model={model!r} etiketsiz — Ollama bunu '{model}:latest' diye "
        "çözer ve o etiket kurulu olmayabilir. `ollama list` çıktısındaki adı "
        "birebir yazın (örn. 'gemma4:e4b')."
    )


def test_ses_modeli_llm_den_bagimsiz_kalir() -> None:
    """Sesten metne çevirme faster-whisper'da kalmalı, LLM'e devredilmemeli.

    Gemma4 "audio" yeteneği bildirir ve Ollama, WAV baytlarını `images`
    alanından kabul eder — yani bu yol fiziksel olarak AÇIKTIR ve yanlışlıkla
    seçilebilir. Ölçüldüğünde transkripsiyon çöp çıktı (gemma4:e4b) ya da
    halüsinasyon üretip 8-52 saniye sürdü (gemma4:12b); aynı ses dosyalarını
    faster-whisper 0.4 saniyede kusursuz çözüyor. Gerekçe: README,
    "Ses doğrudan modele verilebilir mi?".
    """

    yapilandirma = _yapilandirma()

    assert yapilandirma.get("whisper_model_size"), (
        "whisper_model_size boşaltılmış — sesten metne çevirme faster-whisper'da kalmalı."
    )
