# CLAUDE.md

Bu dosya, Claude Code bu repoda çalışırken bağlam olarak otomatik okunur.
Detaylı mimari açıklaması ve sürüm geçmişi için `README.md`'ye bakın; bu
dosya yalnızca "hızlıca doğru şeyi yapmak" için gereken özet bilgidir.

## Proje nedir

Artemis: yerel çalışan (offline-first), Türkçe konuşan, Ollama tabanlı
bir masaüstü AI asistanı. LLM hiçbir zaman doğrudan işlem yapmaz —
yalnızca niyeti analiz edip bir JSON tool-call üretir; gerçek işlemleri
(dosya, Windows, ileride browser/mouse-keyboard) Python tarafı yapar.

## Komutlar

```bash
pip install -r requirements.txt      # bağımlılıklar
pytest                                 # tüm testler (repo kökünden VEYA artemis/ altından çalışır)
pytest tests/test_dispatcher.py -v     # tek dosya
python main.py                         # LLM'siz, tek bir örnek dispatch (demo)
python main.py --chat                  # gerçek Ollama sohbet döngüsü (sunucuyu otomatik başlatır, model seçtirir)
python main.py --stop-ollama           # RAM temizliği: yetim ollama süreçlerini kapatır
```

Not: `pyproject.toml`'daki `pythonpath = ["."]` ayarı sayesinde
`pytest`, hangi dizinden çağrılırsa çağrılsın importları doğru çözer;
ekstra `PYTHONPATH` ayarına veya `sys.path` hack'ine gerek yoktur.

## Mimari — bir bakışta

```
core/            # dispatcher, plugin_loader, tool_base, llm_client, planner, ollama_manager...
config/          # Settings (pydantic + pathlib), config.yaml
models/          # ToolCall, ToolResult, ToolDefinition (pydantic)
memory/          # SQLite tabanlı bağlam hafızası
plugins/         # her dosya bir "kategori" (filesystem_plugin.py, windows_plugin.py...)
tests/           # her core modülü ve her plugin için ayrı test dosyası
```

Akış: kullanıcı girdisi → `core/llm_client.py` (Ollama'ya sorar) →
`core/planner.py` (çok adımlı sırayı yönetir) → `core/dispatcher.py`
(güvenlik/onay kontrolü + tool'u çalıştırır) → `plugins/*.py` (gerçek iş).

## Yeni bir tool eklerken (EN SIK YAPILACAK İŞ)

1. İlgili `plugins/<kategori>_plugin.py` dosyasını aç (yoksa yenisini oluştur).
2. `BaseTool`'dan türeyen bir sınıf yaz: `name` (nokta-notasyonlu, örn.
   `"filesystem.rename"`), `description`, `danger_level`,
   `get_arguments_schema()`, `execute(arguments, context)`.
3. Sınıfın üzerine `@register_tool` koy.
4. **Başka HİÇBİR dosyayı değiştirme** — `plugin_loader.py` otomatik
   keşfeder, `core/manifest.py` LLM'e otomatik tanıtır.
5. `tests/test_<kategori>_plugin.py`'a birkaç senaryo ekle (gerçek
   dosya sistemi/tmp_path ile, mock'lamadan — bkz. mevcut testler).

## Kritik kurallar (bunları asla bozma)

- **Güvenlik**: `delete`, `shutdown`, `restart`, `format`, `registry`,
  `service` gibi geri alınamaz işlemler `danger_level = DangerLevel.CONFIRM_REQUIRED`
  olmalı. Onay mantığı yalnızca `core/dispatcher.py`'da yaşar — her tool
  kendi onay kontrolünü yazmaz.
- **Onay, NEYİ onayladığını göstermek zorunda**: `confirm_callback`
  imzası `(tool_name, arguments) -> bool`. Argümanları düşüren bir
  "sadeleştirme" YAPMAYIN — kullanıcıya yalnızca tool adı gösteren bir
  onay, güvenlik sağlamaz, yalnızca güvenlik hissi verir (bkz. README §16b).
- **Yol kuran her argüman doğrulanır**: `target`/`name` üç şeyi
  yapamaz — mutlak yol olamaz (`Desktop / "C:/Windows/System32"`
  pathlib'de tabanı atar), `..` içeremez, ve `location`'ın KENDİSİNE
  çözülemez (`target="."` tüm masaüstünü siliyordu). Bunu tek tek
  kontrol etmeyin; `filesystem_plugin.py`'daki ortak `_safe_join`
  yardımcısını kullanın. `location` ise bilinçli olarak tam yol kabul
  eder — onu kısıtlamayın.
- **Koşulsuz `success=True` YASAK**: altta yatan çağrının (subprocess,
  `pyautogui`, `webbrowser.open`, ...) gerçekten başardığını doğrulayın.
  Bu hata projede üç kez tekrarlandı (bkz. README §11 ve §16c);
  "başardım" deyip hiçbir şey yapmamak, dürüstçe başarısız olmaktan kötüdür.
- **Şema ile sistem promptu aynı sözleşmeyi konuşmalı**: `llm_client.
  _response_schema()` grammar-constrained decoding uygular — yani
  `prompts/system_prompt.md`'nin modelden istediği biçimi şema
  YASAKLIYORSA, model o biçimi fiziksel olarak üretemez. Biri
  değişirse diğeri de değişmeli (bkz. README §16a — bu hata v1.0
  planner'ını sessizce devre dışı bırakmıştı).
- **Composition > inheritance**: ortak ihtiyaçlar (log, config, hafıza)
  `ToolContext` ile `execute()`'a enjekte edilir; yeni bir mixin/ara
  sınıf eklemek yerine `ToolContext`'e alan eklenir.
- **Hardcode path yok**: her yol `pathlib` + `Settings` üzerinden gelir.
- **Windows'a özgü bağımlılıklar lazy import edilir** (`pywin32`,
  `pyautogui`, `ctypes.windll`, `psutil`) — dosya başında değil, ilgili
  `execute()` içinde. Bu hem Linux/CI'da import hatası vermeden test
  edilebilmeyi hem de gereksiz RAM/açılış yükünü önler.
- **Tool adları çakışmamalı**: `plugin_loader.register_tool` zaten aynı
  isimde ikinci bir kayıtta `ValueError` fırlatır — yeni bir tool
  eklerken registry'de zaten var olan bir isim kullanma.

## Test yazma kuralları

- Gerçek dosya sistemi işlemleri için mock KULLANMA — `tmp_path` fixture'ı
  ile izole bir sahte masaüstü kurup gerçek `ToolDispatcher` üzerinden test et
  (bkz. `tests/test_dispatcher.py`, `tests/test_planner.py`).
- Windows'a özgü davranış (registry, win32gui, ctypes.windll) test
  edilecekse `@pytest.mark.skipif(sys.platform != "win32", ...)` ile
  işaretle (bkz. `tests/test_windows_plugin.py`).
- **Testin çalıştığı GERÇEK makineyi etkileyen hiçbir şey varsayılan
  olarak çalışmamalı.** Ekran kilitleme, ses açma/kapatma, uygulama
  başlatma, dosya açma gibi gözle görülür yan etkileri ya monkeypatch ile
  engelle (bkz. `os.startfile` deseni) ya da `@pytest.mark.disruptive`
  ile işaretle — bu işaretliler `pytest`'te otomatik atlanır
  (`pyproject.toml::addopts`), bilinçli çalıştırmak için `pytest -m disruptive`.
  Gerekçe: `windows.lock` testi bir dönem varsayılan olarak çalışıyordu ve
  her test turunda geliştiricinin ekranı kilitleniyordu. Yan etkisi geri
  alınabilir olanlar (ses kapatma gibi) testin sonunda eski haline
  döndürülmeli.
- LLM/Ollama'ya bağımlı testler gerçek sunucu gerektirmemeli — `ollama`
  modülünü `monkeypatch.setitem(sys.modules, "ollama", fake_module)` ile
  sahte bir modülle değiştir (bkz. `tests/test_llm_client.py`,
  `tests/test_ollama_manager.py`).

## Bilinen sınırlamalar (bilerek eklenmedi, şaşırma)

- Plan adımları arasında veri aktarımı `{{step_N.alan}}` referanslarıyla
  VAR (v3.5) — model bir yolu tahmin etmez, yalnızca "N. adımın X alanı"
  der; gerçek değer çalışma zamanında yerleştirilir (bkz. `core/planner.py`
  modül dokümantasyonu, README §25).
- `plugins/browser_plugin.py`, `plugins/mouse_keyboard_plugin.py`,
  `plugins/mcp_plugin.py` VAR (v3.6/v3.7). MCP v1 yalnızca stdio
  taşımasını destekler, `config.yaml::mcp_servers` boş olduğu sürece
  sıfır I/O yapar (README §27). Ses katmanı (`voice/stt.py`/`voice/tts.py`)
  de VAR, hibrit bulut/yerel mimarinin parçası.
- `skills/` klasörü **bilinçli olarak boş** — bu artık açık bir soru
  değil, verilmiş bir karar: `core/planner.py::TaskPlanner` zaten
  tool zincirleme işini yapıyor, ayrı bir skills çerçevesi ikinci ve
  gereksiz bir yol olurdu. Kararın gerekçesi ve hangi koşulda geri
  açılacağı `skills/README.md`'de yazılı — oraya bakmadan skills
  klasörüne bir şey eklemeyin.

Sürüm geçmişi ve her değişikliğin hangi soruna çözüm olduğu için
`README.md`'nin ilgili bölümlerine bakılabilir (dosya kronolojik olarak
büyüyen bölümler halinde tutuluyor, en yeni değişiklik en sonda).
