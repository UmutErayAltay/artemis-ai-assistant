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


def test_openrouter_modeli_tam_slug_ve_ucretsiz_etiket_icerir() -> None:
    """OpenRouter slug'ı YANLIŞ yazılırsa hata yardımcı olmaz: Ollama'daki
    "etiketsiz ad sessizce `:latest`'e düşer" arızasının bulut karşılığı,
    ama daha kötüsü — OpenRouter "model yok" demek yerine anlaşılmaz bir
    HTTP 400 döner. Aynı sınıf bir arıza olduğu için aynı sınıf bir
    denetim: slug KÜÇÜK HARF `sağlayıcı/model` biçiminde ve ücretsiz
    katalog için `:free` son ekiyle bitmeli.
    """

    model = _yapilandirma()["openrouter_model"]

    assert model == model.lower(), f"openrouter_model={model!r} — slug'lar büyük harf duyarlıdır."
    assert "/" in model, f"openrouter_model={model!r} 'sağlayıcı/model' biçiminde olmalı."
    assert model.endswith(":free"), (
        f"openrouter_model={model!r} ücretli — varsayılan model ÜCRETSİZ olmalıdır, "
        "aksi halde anahtarı olan kullanıcı farkında olmadan faturalandırılır."
    )


def test_llm_provider_degerleri_hybrid_ses_ayarlariyla_ayni() -> None:
    """`llm_provider` sesin üç değerini (`stt_provider`/`tts_provider`)
    yeniden keşfetmemeli: aynı üç seçenek, aynı adlandırma, aynı
    anlamlar. Beyin ile ses arasında "auto/cloud/local" ve
    "auto/bulut/yerel" gibi iki paralel sözlük kullanmak, kullanıcının
    "cloud yazdım, neden yerele düştü" diye soru sormasına yol açar.
    """

    yapilandirma = _yapilandirma()

    assert yapilandirma["llm_provider"] in {"auto", "cloud", "local"}
    assert yapilandirma["stt_provider"] in {"auto", "cloud", "local"}
    assert yapilandirma["tts_provider"] in {"auto", "cloud", "local"}
