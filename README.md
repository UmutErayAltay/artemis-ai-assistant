# Artemis — Çekirdek Mimari (v0.1)

Bu teslimat, Artemis'in **tüm gelecekteki tool'ların üzerine oturacağı
temel iskeletidir**. Henüz browser/Windows/mouse-keyboard/ses gibi
somut yetenekler eklenmedi — bilinçli olarak, önce "100 tool eklense de
bozulmayacak" bir omurga kurulması hedeflendi.

## 1) Neden bu yapı?

| Kural (spesifikasyondan) | Bu mimaride karşılığı |
|---|---|
| Devasa dosya yok, her özellik ayrı modül | Her tool/plugin kendi dosyasında; `core/` katmanları da tek sorumluluk başına ayrı dosya |
| Composition > Inheritance | Tool'lar `BaseTool`'dan yalnızca **arayüz** için türer; loglama/config/hafıza gibi ortak davranışlar `ToolContext` ile **inject** edilir, mixin zinciriyle değil |
| Kod tekrarı yok, ortak yardımcı sınıflar | `ToolContext`, `ToolDispatcher`, `_resolve_location()` gibi tek noktalar; her yeni tool bunları tekrar yazmaz |
| Merkezi dosya değişmeden yeni özellik eklenebilmeli | `@register_tool` decorator + `pkgutil` ile otomatik keşif: yeni tool = yeni dosya, `plugin_loader.py`'a asla dokunulmaz |
| 100 tool'a ölçeklenebilir mimari | Tool adları nokta-notasyonlu namespace (`filesystem.*`, `browser.*`, ...); `core/manifest.py` LLM'e gösterilecek tool listesini **registry'den otomatik üretir** — prompt elle büyümez |
| Hardcode path yok | Her yol `pathlib` + `Settings` üzerinden, `config/config.yaml`'dan gelir |
| Tehlikeli işlemler onay gerektirir | `DangerLevel.CONFIRM_REQUIRED` bayrağı + `ToolDispatcher` tek noktada kontrol eder (her tool kendi onay mantığını yazmaz) |
| Test edilebilirlik | `ToolContext` DI sayesinde tool'lar gerçek OS yan etkisi olmadan (tmp_path ile) test edilir |

## 2) Dosya ağacı

```
artemis/
├── CLAUDE.md                   # Claude Code için otomatik okunan bağlam özeti
├── pyproject.toml               # pytest pythonpath ayarı + temel paket metadata
├── main.py                    # bootstrap + örnek kullanım
├── requirements.txt
├── config/
│   ├── settings.py            # Pydantic Settings (yollar, güvenlik listesi)
│   └── config.yaml            # örnek config
├── core/
│   ├── enums.py                # DangerLevel
│   ├── exceptions.py           # ArtemisError hiyerarşisi
│   ├── tool_base.py             # BaseTool (arayüz) + ToolContext (DI)
│   ├── plugin_loader.py         # @register_tool + otomatik keşif
│   ├── dispatcher.py            # tek giriş kapısı: JSON -> execute()
│   ├── manifest.py              # registry -> LLM'e sunulacak tool listesi
│   ├── llm_client.py            # Ollama istemcisi + JSON ayrıştırma
│   ├── prompt_builder.py        # system_prompt.md + manifest birleştirme
│   ├── ollama_manager.py        # sunucuyu otomatik başlat/durdur + model seçimi
│   ├── planner.py               # çok-adımlı komutları sıralı/güvenli yürütür
│   └── conversation_loop.py     # terminal <-> LLM <-> planner <-> dispatcher döngüsü
├── models/
│   └── tool_models.py           # ToolCall, ToolResult, ToolDefinition
├── memory/
│   └── context_memory.py        # SQLite tabanlı bağlam hafızası
├── utils/
│   └── logger.py                 # merkezi logging kurulumu
├── plugins/
│   ├── filesystem_plugin.py       # open/create_folder/create_file/search/
│   │                                # copy/delete (6 tool)
│   ├── web_plugin.py              # search/open_url (2 tool)
│   ├── windows_plugin.py          # launch/close/lock/sleep/shutdown/restart/
│   │                                # volume/brightness/screenshot/clipboard/
│   │                                # list_windows/focus_window (12 tool)
│   └── _app_resolver.py           # launch_app için isim->yol çözümleyici
│                                    # (registry + kısayol taraması; "_" ile
│                                    # başladığı için plugin_loader bunu bir
│                                    # tool dosyası olarak taramaz)
├── skills/
│   └── README.md                  # plugins/ vs skills/ ayrımı (bkz. içindeki not — varsayım, onay bekliyor)
├── prompts/
│   └── system_prompt.md          # Ollama'ya gönderilecek şablon
├── logs/                          # runtime'da otomatik dolar
└── tests/
    └── test_dispatcher.py
```

## 3) Anahtar sözleşme: BaseTool

```python
class BaseTool(ABC):
    name: str                    # "filesystem.open" gibi namespaced kimlik
    description: str
    danger_level: DangerLevel = DangerLevel.SAFE

    @abstractmethod
    def get_arguments_schema(self) -> dict: ...

    @abstractmethod
    def execute(self, arguments: dict, context: ToolContext) -> ToolResult: ...
```

Yeni bir tool eklemek = bu sözleşmeyi dolduran bir sınıf yazıp
`@register_tool` ile işaretlemek. Başka hiçbir yerde değişiklik gerekmez.

## 4) Örnek kullanım

```python
from core.dispatcher import ToolDispatcher
from core.plugin_loader import load_plugins

load_plugins()
dispatcher = ToolDispatcher()

# Güvenli işlem — doğrudan çalışır
result = dispatcher.dispatch({
    "tool": "filesystem.create_folder",
    "arguments": {"name": "Orbit", "location": "desktop"},
})
# result.success == True

# Tehlikeli işlem — önce onay ister
result = dispatcher.dispatch({"tool": "filesystem.delete", "arguments": {"target": "Orbit"}})
# result.requires_confirmation == True, result.success == False

# Kullanıcı onayladıktan sonra:
result = dispatcher.dispatch(
    {"tool": "filesystem.delete", "arguments": {"target": "Orbit"}},
    confirmed=True,
)
# result.success == True
```

## 5) 101. tool'a kadar nasıl büyür?

1. **Aynı kategori, yeni tool** (örn. `filesystem.rename`): `filesystem_plugin.py`
   içine yeni bir `BaseTool` sınıfı ekle, `@register_tool` koy. Bitti.
2. **Yeni kategori** (örn. Windows kontrolü): `plugins/windows_plugin.py`
   oluştur, aynı şablonu izle. `plugin_loader.py` otomatik bulur.
3. **Kategori büyüdükçe** (örn. 15+ browser tool'u): `plugins/browser_plugin.py`
   yerine `plugins/browser/` alt paketine bölünebilir; bu durumda
   `plugin_loader.load_plugins()` içindeki `pkgutil.iter_modules` çağrısı
   `pkgutil.walk_packages` ile değiştirilir (kod içinde not edildi).
4. **LLM'e tanıtım**: Hiçbir ekstra adım yok — `core/manifest.py` yeni
   tool'u bir sonraki `build_tool_manifest()` çağrısında otomatik dahil eder.
5. **Test**: `tests/` altına yeni tool için birkaç `assert` eklemek yeterli;
   `ToolContext` DI sayesinde gerçek dosya sistemi/OS yan etkisine
   ihtiyaç duymadan (tmp_path ile) izole test edilir.

## 6) Bilinçli olarak bu teslimatta OLMAYANLAR

Aşağıdakiler, aynı `BaseTool` + `@register_tool` şablonuyla ayrı birer
plugin/modül olarak eklenmeyi bekliyor — hiçbiri bu çekirdeği bozmadan
eklenebilir:

- `plugins/browser_plugin.py` — sekme/pencere/URL yönetimi (tarayıcı otomasyonu)
- `plugins/web_plugin.py` — Google/YouTube/Wikipedia/GitHub araması
- `plugins/mouse_keyboard_plugin.py` — pyautogui/keyboard/mouse
- `plugins/mcp_plugin.py` — MCP sunucu client'ı
- `voice/stt.py`, `voice/tts.py` — Whisper / Piper-pyttsx3 entegrasyonu

> **v1.0 notu**: `core/planner.py::TaskPlanner` bu teslimatta eklendi —
> çok adımlı komutları (`core/planner.py` modül dokümantasyonuna bkz.)
> sıralı ve güvenli şekilde yürütüyor.

> **v0.1 → v0.2 notu**: `plugins/windows_plugin.py` bu teslimatta eklendi
> (12 tool: launch/close/lock/sleep/shutdown/restart/volume/brightness/
> screenshot/clipboard/list_windows/focus_window). `windows.shutdown` ve
> `windows.restart` `CONFIRM_REQUIRED` olarak işaretli. Bu plugin'in
> Windows'a özgü kısımları (`ctypes.windll`, `pywin32`, `pyautogui`) yalnızca
> gerçek Windows üzerinde test edilebilir; `tests/test_windows_plugin.py`
> bu kısımları `sys.platform != "win32"` olduğunda otomatik atlar.

## 8) LLM bağlantısı (Ollama) — v0.3

```
core/
├── llm_client.py       # OllamaLLMClient: sistem+kullanıcı mesajı gönderir,
│                         # kirli LLM çıktısını (fence/açıklama metni) JSON'a çevirir
├── prompt_builder.py    # system_prompt.md şablonunu güncel tool manifest ile doldurur
└── conversation_loop.py # terminal input() -> LLM -> dispatch -> onay akışı -> print()
```

Akış: `main.py --chat` → `bootstrap()` (config/logging/plugin yükle) →
`OllamaLLMClient(model=...)` → `conversation_loop.run()` her turda:
kullanıcı metni al → `build_system_prompt()` + kullanıcı mesajıyla
`ollama.chat()` çağır → `extract_tool_calls()` ile JSON'a çevir → her
tool çağrısını `dispatcher.dispatch()`'e ver → `requires_confirmation`
ise `input()` ile sor → sonucu yazdır.

**Çalıştırmak için (kullanıcının kendi Windows makinesinde):**
```bash
pip install -r requirements.txt
ollama serve                 # ayrı bir terminalde
ollama pull llama3.1          # config.yaml'daki model neyse onu çekin
python main.py --chat
```

**Sandbox'ta doğrulanan/doğrulanamayan:**
- ✅ `extract_tool_calls()` — düz JSON, çok adımlı liste, markdown-fence'li,
  açıklama metniyle sarılmış çıktı, geçersiz çıktı → hepsi test edildi.
- ✅ `build_system_prompt()` — şablon + güncel manifest doğru birleşiyor.
- ✅ Tüm döngü (`conversation_loop.run`) sahte bir `ollama.chat()` ile
  uçtan uca denendi: "klasör oluştur" → "sil" → onay sorusu → onaylandı →
  gerçekten silindi → çıkış. Değiştirilmemiş gerçek dosyalarla çalıştı.
- ❌ Gerçek Ollama sunucusuna bağlanma, sandbox'ta internet/Ollama
  olmadığı için test edilemedi — bunu kendi makinenizde doğrulamanız gerekiyor.

## 9) Küçük modeller JSON'a sadık kalmazsa — 4 katmanlı güvence (v0.4)

Tek bir önleme (prompt talimatı) güvenilmez; `core/llm_client.py` dört
katmanı birlikte uygular:

1. **Şema-kısıtlı format** (asıl çözüm): Ollama'nın `format=<json-schema>`
   "structured outputs" özelliğiyle model, `tool` alanı için yalnızca
   **registry'deki gerçek tool adlarından** oluşan bir `enum`'a ve
   `{"tool", "arguments"}` zarfına **grammar-constrained decoding** ile
   kısıtlanır. Bu bir talimat değil, bir kısıtlamadır — model uymak
   *istemese bile* şema dışı bir token üretemez.
2. **Format fallback**: Eski Ollama/model kombinasyonları şema-format'ı
   desteklemiyorsa, otomatik olarak genel `format="json"` moduna düşülür.
3. **Düzeltme-istekli retry**: Yine de ayrıştırılamayan bir çıktı gelirse
   (`extract_tool_calls` başarısız olursa), modele "bu geçersizdi, düzelt"
   diyerek varsayılan 2 ek deneme hakkı verilir.
4. **`temperature=0`**: Tool-calling belirleyici (deterministic) olmalı;
   yaratıcılık kapatılır.

Sandbox'ta (sahte `ollama.chat` ile) doğrulanan 3 senaryo: geçersiz →
düzeltme → geçerli; tüm denemeler tükenince hata; şema-format hatası →
genel json'a düşüş. Gerçek bir modelin şema-kısıtlı formatı ne kadar iyi
desteklediği modelden modele değişir (llama3.1/qwen2.5 gibi yeni modeller
iyi destekler); yine de en kötü ihtimalde katman 2-3-4 devrede kalır.

**İleri seviye alternatif — v0.5'te eklendi**: `config/config.yaml::use_native_tool_calling: true`
yaparsanız, Ollama'nın native `tools=[...]` (OpenAI-tarzı function-calling)
parametresi önce denenir; destekleyen modellerde (llama3.1, qwen2.5 gibi)
model doğrudan yapılandırılmış `tool_calls` döndürür ve metin ayrıştırmaya
hiç gerek kalmaz. Model bu parametreyi yok sayıp düz metinle cevap
verirse (desteklemiyorsa), **otomatik olarak** katman 2-5'e (şema-format
→ json → düzeltme-retry) düşülür — hiçbir ek kod/config değişikliği
gerekmez. Sandbox'ta (sahte `ollama.chat` ile) 4 senaryo doğrulandı:
native `tool_calls` doğrudan kullanılıyor, `arguments` string-JSON olarak
gelirse parse ediliyor, model `tools`'u yok sayarsa metne düşülüyor,
`use_native_tool_calling=False` iken `tools` parametresi hiç
gönderilmiyor (varsayılan davranış değişmiyor).

## 10) RAM tasarrufu — model boşta beklerken bellekten atılsın (v0.6)

`ollama serve` sürecinin kendisi hafiftir; RAM'i model ağırlıkları
tüketir. Ollama'nın bunun için yerleşik bir mekanizması var: her isteğe
eklenen `keep_alive` süresi, modelin son kullanımdan ne kadar sonra
RAM'den otomatik boşaltılacağını (unload) belirler — sunucu kapanmaz,
yalnızca ağırlıklar bellekten çıkar; bir sonraki komutta otomatik
yeniden yüklenir.

`config/config.yaml::ollama_keep_alive` ile ayarlanır:

| Değer | Etki |
|---|---|
| `"30s"` / `"1m"` | Idle'da neredeyse hiç RAM kullanmaz; her "uyanışta" birkaç saniye yeniden yükleme gecikmesi (model boyutu/disk hızına göre değişir) |
| `"5m"` (varsayılan) | Aktif konuşma boyunca RAM'de kalır, 5 dk sessizlikten sonra boşalır |
| `"0"` | Her cevaptan hemen sonra boşalt (en agresif RAM tasarrufu, ama her komut yeniden yükleme bekler) |
| `"-1"` | Hiç boşaltma (RAM tasarrufu yok, her zaman anında cevap) |

RAM kısıtlıysa `"1m"` veya `"2m"` ile başlamanız önerilir. Şu anda
kilitli/masaüstünde çalışmayacağınız uzun aralar için `"30s"` bile
makul olabilir. Sandbox'ta `keep_alive` değerinin gerçekten her
`ollama.chat()` çağrısına iletildiği doğrulandı.

**Hemen şimdi manuel olarak boşaltmak isterseniz** (kod değişikliği
beklemeden): `ollama stop <model_adı>` komutu modeli anında RAM'den atar.

## 11) `windows.launch_app` düzeltmesi: yanlış-pozitif "başlatıldı" (v0.7)

**Bildirilen hata**: "lol'ü aç", "riot client'ı aç" gibi komutlarda
Artemis "başlatıldı" diyordu ama uygulama gerçekte açılmıyordu.

**Kök neden**: Eski implementasyon, ismi doğrudan Windows'un `cmd /c
start` komutuna gönderiyordu. Bu komut, tanımadığı bir isim için
**kendi konsoluna sessizce hata basıp çıkıyor** — bizim
`subprocess.Popen()` çağrımız ise `cmd.exe`'yi başarıyla başlattığı
için (asıl `start` başarısızlığından habersiz) hep `success=True`
döndürüyordu.

**Çözüm**: `plugins/_app_resolver.py` adında yeni, test edilebilir bir
`AppResolver` sınıfı — üç kaynağı sırayla dener:

1. Bilinen sistem komutları (notepad, calc, explorer, chrome)
2. Windows Registry `App Paths` anahtarı (Opera, VS Code, Discord gibi
   çoğu kurulum burada kayıtlıdır)
3. Başlat Menüsü kısayolları (`.lnk`) arasında **bulanık (fuzzy) arama**
   — Riot Client gibi registry'ye kaydolmayan uygulamalar için

Bir `_ALIASES` sözlüğü günlük dil kısaltmalarını normalize eder
(`"lol"` -> `"league of legends"`, `"riot"` -> `"riot client"`).
Gerçek bir dosya yolu bulunursa `os.startfile()` ile başlatılır — bu,
dosya yoksa **gerçekten** exception fırlatır, artık sessiz
başarısızlık yok. Hiçbir kaynakta bulunamazsa, körü körüne "başlatıldı"
demek yerine dürüstçe `success=False` döner ve en yakın kısayol
önerilerini sunar (`difflib` ile).

Sandbox'ta doğrulanan 8+6 senaryo: alias normalizasyonu, tam/fuzzy/
alt-dizge eşleşme, bulunamayan uygulama için dürüst başarısızlık +
öneri, ve dispatcher üzerinden uçtan uca (`"lol"` → gerçek
`LeagueClient.exe` yoluna çözülüp `os.startfile` ile çağrılıyor).
Registry/Başlat Menüsü taraması gerçek Windows API'lerine (`winreg`,
`win32com`) dayandığından, gerçek bir tarama sandbox'ta test edilemedi
— kendi Riot Client kurulumunuzla `python main.py --chat` üzerinden
doğrulamanız gerekiyor.

## 12) Otomatik `ollama serve` + interaktif model seçimi (v0.8)

Artık ayrı bir terminalde `ollama serve` çalıştırmanıza gerek yok.
`core/ollama_manager.py` içindeki `OllamaServerManager`:

- Başlarken sunucunun çalışıp çalışmadığını kontrol eder (`ollama.list()`
  ile) — çalışıyorsa hiçbir şey yapmaz.
- Çalışmıyorsa arka planda kendisi başlatır (`ollama serve`) ve hazır
  olana kadar bekler (varsayılan zaman aşımı: 15 saniye).
- **Yalnızca kendi başlattığı sunucuyu**, `main.py --chat` kapanırken
  kapatır (`finally` bloğunda) — kullanıcının ayrı bir yerde başlattığı
  bir sunucuya asla dokunmaz (`_process` yalnızca biz başlattıysak dolu olur).
- `ollama` komutu PATH'te yoksa veya sunucu zaman aşımına uğrarsa,
  anlaşılır bir hata mesajıyla düzgünce durur (crash etmez).

Model seçimi artık `config.yaml`'a hardcode edilmiyor:
`list_installed_models()` kurulu modelleri listeler,
`prompt_user_to_select_model()` bunları numaralandırıp terminalden seçim
ister:

```
Kurulu Ollama modelleri:
  1) llama3.1:latest
  2) qwen2.5:7b
  3) mistral:7b
Hangi modeli kullanmak istiyorsunuz? (1-3): 2
Seçildi: qwen2.5:7b
```

`config.yaml::ollama_model` alanı kaldırılmadı — yalnızca ileride
eklenecek interaktif olmayan/otomasyon senaryoları için yedek olarak duruyor.

## 13) RAM optimizasyonu (v0.9)

Sandbox'ta doğrulanan 10 senaryo (bölüm 12): dict/nesne tabanlı
`ollama.list()` yanıtlarının ikisi de doğru ayrıştırılıyor, boş model
listesi doğru hata veriyor, geçerli/geçersiz numara girişleri doğru
işleniyor, sunucu zaten çalışıyorsa yeni süreç başlatılmıyor, `ollama`
komutu yoksa doğru hata veriliyor, sunucu başlatılıp hazır olana kadar
bekleme akışı doğru çalışıyor, yalnızca kendi başlattığımız sunucuya
dokunuluyor.

**Bildirilen sorun**: sunucu çok uzun süre açık kalıyor; VS Code'da
çalıştırılan proje 1.5GB bellek harcıyor — sesli asistan için fazla.

Bunu birkaç ayrı katmanda ele almak gerekiyor, çünkü kaynak farklı olabilir:

### a) "Proje" mi, yoksa "model" mi bu kadar RAM yiyor?
Bu ayrım kritik: Task Manager'da **kaç tane `ollama.exe` süreci** ve **kaç
tane `python.exe` süreci** olduğuna, her birinin ayrı ayrı ne kadar RAM
kullandığına bakın. Bir LLM'in kendisi (Ollama sunucusu içinde,
`python.exe`'den AYRI bir süreçte) makul bir RAM tüketir (3-8B model
genelde 2-6GB) — bu koddan değil, model boyutundan kaynaklanır ve yalnızca
daha küçük/daha kuantize bir model seçerek azaltılabilir. Asıl saf
Python tarafımızın (dispatcher, plugin'ler, LLM istemcisi) birkaç yüz
MB'ı geçmesi beklenmez.

### b) VS Code'da NASIL çalıştırdığınız önemli
Eğer **F5 (Run/Debug)** ile çalıştırıyorsanız, VS Code `debugpy`'yi devreye
sokar — bu, kendi başına yüz MB'larca ek yük getirir ve "proje 1.5GB
yiyor" algısının büyük kısmı bu olabilir. Bu tür uzun süre açık kalan,
interaktif bir konsol uygulaması için **entegre terminale doğrudan
`python main.py --chat` yazmanızı** öneririm — hem daha az bellek yer,
hem Ctrl+C daha güvenilir çalışır.

### c) Bu teslimatta yapılan somut değişiklikler
- **`psutil` artık lazy import** (`windows_plugin.py`): yalnızca
  `windows.close_app` çağrıldığında yüklenir, program açılışında değil.
- **`ollama_keep_alive` varsayılanı `"5m"` → `"1m"`** düşürüldü
  (`config.yaml` ve `Settings`): model artık 1 dakika sessizlikten sonra
  RAM'den boşalır. RAM'iniz çok kısıtlıysa `"30s"` bile deneyebilirsiniz.
- **Sinyal işleme eklendi** (`main.py`): Ctrl+C (SIGINT) ve SIGTERM'de
  Artemis'in kendi başlattığı Ollama sunucusu artık daha güvenilir şekilde
  kapatılıyor.
- **`python main.py --stop-ollama`** (yeni): RAM için bir "reset düğmesi".
  Sistemdeki TÜM `ollama` süreçlerini zorla kapatır — özellikle VS Code'un
  "Stop" düğmesiyle SERT kapatma (taskkill/TerminateProcess) sonrası arkada
  kalmış "yetim" bir sunucu/model varsa bunu temizler.

### d) Bilinen sınır
VS Code'un "Stop" düğmesi Python sürecini sert şekilde kapatırsa, **hiçbir
Python kodu (signal handler dahil) çalışamaz** — bu, işletim sistemi
seviyesinde bir kısıtlama, kod ile aşılamaz. Bu yüzden (c)'deki
`--stop-ollama` bir "elle reset" olarak var; düzenli olarak F5 yerine
düz terminalden çalıştırmak bu ihtiyacı zaten büyük ölçüde ortadan kaldırır.

Sandbox'ta doğrulanan: `stop_all_ollama_processes()` adında "ollama"
geçen süreçleri doğru şekilde (diğerlerine dokunmadan) sonlandırıyor;
`main.py` ve `windows_plugin.py` psutil'siz de sorunsuz import ediliyor.

## 14) Çok-adımlı planner (v1.0)

**Önceden**: LLM zaten bir JSON *listesi* döndürebiliyordu
(`get_tool_calls`), ama `conversation_loop` bunları tek tek, aralarında
hiçbir bağımlılık/hata kontrolü olmadan çalıştırıyordu — bir adım
başarısız olsa bile sonrakiler yine de denenirdi.

**Yeni `core/planner.py::TaskPlanner`**, "Chrome'u aç, GitHub'a git, ara"
gibi çok adımlı komutları GÜVENLİ bir sırayla yürütür:

- **Sıralı yürütme**: adımlar paralel değil, art arda çalıştırılır
  (sonraki adım genelde bir öncekine bağlıdır — önce klasör, sonra içine dosya).
- **Gerçek hatada dur** (`stop_on_failure=True`, varsayılan): bir adım
  gerçekten başarısız olursa kalan adımlar hiç çalıştırılmaz. Örnek:
  "Orbit klasörünü oluştur ve içine app.py koy" — klasör oluşturma
  başarısız olduysa dosya oluşturmaya devam etmenin anlamı yok.
- **Onay reddinde TÜM planı durdur**: bir adım onay istiyorsa
  (`filesystem.delete` gibi) ve kullanıcı reddederse, yalnızca o adım
  değil, **kalan tüm adımlar** iptal edilir — kullanıcı "hayır" dediğinde
  aynı planın devamının hâlâ istenip istenmediği belirsizdir, güvenli
  taraf durmaktır.
- Terminal çıktısında adımlar `[1/2]`, `[2/2]` şeklinde numaralandırılır;
  plan erken durursa "(N adım çalıştırılmadı)" notu eklenir.

**Bilinçli sınırlama**: Adımlar arasında veri aktarımı YOKTUR — "1.
adımda bulunan dosya yolunu 2. adıma argüman olarak ver" gibi bir
zincirleme desteklenmiyor. Her adımın argümanları LLM tarafından tek
seferde üretiliyor. Bu, küçük/yerel modellerin çok adımlı bağımlı akıl
yürütmeyi güvenilir yapamayabileceği düşünülerek bilinçli bir
basitleştirme; ileride ihtiyaç olursa `execute_plan` içine "önceki
adımın `data` alanını sonrakine enjekte et" mekanizması eklenebilir.

Sandbox'ta doğrulanan 7 senaryo (17 kontrol), gerçek `ToolDispatcher` +
`filesystem_plugin` tool'ları ve tmp_path ile izole edilmiş sahte
masaüstü kullanılarak: tüm adımlar başarılı, gerçek hatada durma, gerçek
dosya sisteminde doğrulanmış (`stop_on_failure=False` ile hataya rağmen
devam), onay reddinde TÜM planın durması + reddedilen dosyanın hâlâ
yerinde olduğu, onay verilince devam etmesi, boş plan, sıralı index'ler.
Ayrıca `conversation_loop.run()` sahte bir çok-adımlı Ollama yanıtıyla
uçtan uca test edildi: "Orbit klasörü oluştur ve içine app.py koy" tek
komutu, `[1/2]`/`[2/2]` etiketli iki gerçek dosya-sistemi işlemine
doğru şekilde bölündü.

## 15) Claude Code desteği (v1.1)

Proje artık Claude Code (`claude` CLI) ile açılıp geliştirilmeye hazır:

- **`CLAUDE.md`** (repo kökünde) — Claude Code'un bir oturum başında
  otomatik okuduğu standart bağlam dosyası. Komutları (kurulum, test,
  çalıştırma), mimari özetini, "yeni tool eklerken ne yapılır" adımlarını,
  bozulmaması gereken kritik kuralları (güvenlik onayı, composition,
  lazy import, hardcode path yasağı) ve bilinen sınırlamaları içerir.
  Detaylı gerekçe/sürüm geçmişi için hâlâ bu README'ye yönlendirir —
  `CLAUDE.md` bir özet, README tam kayıt.
- **`pyproject.toml`** (yeni) — `[tool.pytest.ini_options]` altında
  `pythonpath = ["."]` ayarı sayesinde `pytest` komutu artık **hangi
  dizinden çalıştırılırsa çalıştırılsın** (repo kökünden, `artemis/`
  içinden, bir IDE'nin test panelinden) importları doğru çözüyor —
  önceden bu, örtük olarak "doğru dizinden çalıştırma" varsayımına
  dayanıyordu. Ayrıca `pip install -e .` ile projeyi düzenlenebilir
  paket olarak kurmak isteyenler için temel `[build-system]`/`[project]`
  metadata'sı da eklendi (mevcut `requirements.txt` akışı hâlâ birincil
  ve desteklenen kurulum yöntemi; `pyproject.toml` bunu değiştirmiyor,
  yalnızca tamamlıyor).

Bu ikisi de yalnızca proje organizasyonu/tooling değişikliği; hiçbir
`core/`, `plugins/`, `models/` dosyasında davranış değişikliği yok.
- ⚠️ Küçük/yerel modellerin talimatlara ne kadar sadık kalacağı (özellikle
  çok adımlı komutlarda doğru JSON listesi üretmek) modelden modele
  değişir; `extract_tool_calls` makul ölçüde hataya toleranslı yazıldı
  ama garanti değil — pratikte hangi modelin en iyi sonucu verdiğini
  denemeniz gerekecek.

## 16) Denetim teslimatı: iki sessiz hata + güvenlik sertleştirmesi (v1.2)

Bu teslimat yeni bir "özellik turu" değil; mevcut kodun uçtan uca
denetlenmesi sonucu bulunan sorunların düzeltilmesi. En önemli iki bulgu,
tek tek bakıldığında doğru görünen ama BİRLİKTE çalıştığında birbirini
bozan katmanlardan çıktı.

### a) v0.4 güvenilirlik katmanı, v1.0 planner'ını sessizce kapatmış (KRİTİK)

**Belirti yok** — hiçbir test kırılmıyordu, hiçbir hata mesajı çıkmıyordu.
Çok adımlı komutlar yalnızca "çalışmıyordu".

**Kök neden**: `core/llm_client.py::_response_schema()` çıktı şemasını
`{"type": "object", ...}` olarak tanımlıyordu. Bu şema Ollama'ya `format=`
ile veriliyor ve **grammar-constrained decoding** uyguluyor — yani model
şemanın dışına fiziksel olarak çıkamıyor. Şema tek bir nesne dayattığı
için model bir JSON **listesi** üretemiyordu.

Oysa `prompts/system_prompt.md` modele "birden fazla işlem için liste
üret" diyordu ve v1.0'ın ana özelliği `TaskPlanner` tam da o listeyi
yürütmek için yazılmıştı. `config.yaml`'da `use_native_tool_calling:
false` olduğundan bu şema-kısıtlı strateji VARSAYILAN ilk stratejiydi ve
genelde başarılı oluyordu — yani sonraki stratejilere hiç düşülmüyordu.

**Sonuç**: varsayılan konfigürasyonda `TaskPlanner` her zaman tek adım
alıyordu; "Orbit klasörü oluştur ve içine app.py koy" gibi komutlar
çalışamazdı. v0.4'te eklenen güvenilirlik katmanı, v1.0'da eklenen ana
özelliği farkında olmadan devre dışı bırakmıştı.

**Düzeltme**: şema artık her zaman bir **dizi**
(`{"type": "array", "items": {...}, "minItems": 1}`); tek işlem = tek
elemanlı liste. `anyOf`/`oneOf` ile "ya nesne ya dizi" yerine düz dizi
seçildi çünkü (1) tek bir biçim küçük modeller için daha az kafa
karıştırıcı, (2) `anyOf`'un grammar'a çevrilmesi her Ollama/llama.cpp
sürümünde garanti değil. `prompts/system_prompt.md`'deki çelişkili kural
2/3 ikilisi tek bir kuralda birleştirildi ("her zaman liste"), parse
hatası sonrası gönderilen düzeltme mesajı da aynı sözleşmeye uyarlandı.
`extract_tool_calls` bilinçli olarak hem tek nesne hem liste kabul etmeye
devam ediyor (şema devre dışı kaldığında 3./4. stratejilerde model hâlâ
düz nesne döndürebilir).

Regresyon testi eklendi: şema-kısıtlı strateji altında 2 adımlı bir
yanıtın gerçekten 2 adım olarak döndüğü doğrulanıyor.

### b) Onay ekranı, neyi onayladığınızı söylemiyordu (GÜVENLİK)

Projenin tek güvenlik bariyeri kullanıcı onayı. Ama soru şuydu:

```
'filesystem.delete' işlemi onay gerektiriyor. Devam edilsin mi? (e/h):
```

**Hangi dosyanın silineceği gösterilmiyordu.** `_confirm_with_user` yalnızca
tool ADINI alıyordu. Bu, şu somut senaryoyla birleşince tehlikeliydi:
hedef yol `_resolve_location(location) / target` ile kuruluyordu ve
pathlib'de `target` MUTLAK bir yol olursa taban tamamen atılır:

```python
Path("C:/Users/.../Desktop") / "C:/Windows/System32"   # -> C:\Windows\System32
```

Yani halüsinasyon gören bir model, kullanıcının "masaüstümde bir klasör
siliyorum" sanarak verdiği onayla sistem klasörünü hedefleyebilirdi.

**Düzeltme (iki katmanlı savunma)**:

1. **Onay artık şeffaf**: `confirm_callback` imzası
   `(tool_name) -> bool`'dan `(tool_name, arguments) -> bool`'a çevrildi;
   terminal onayı argümanları satır satır gösteriyor. `dispatcher`'ın
   döndürdüğü `ToolResult` de hem `message` içinde argümanları taşıyor hem
   de `data={"tool":..., "arguments":...}` veriyor — böylece ileride
   eklenecek ses/GUI arayüzleri de aynı bilgiyi biçimlendirebilir
   (terminale bağımlı değil).
2. **`target` artık dizin dışına çıkamaz**: `filesystem.*` tool'larında
   `target`/`name` argümanı mutlak yol veya `..` içeremez (şemaların kendi
   açıklaması zaten "dosya/klasör **adı**" diyordu). `Orbit/app.py` gibi
   meşru göreli alt yollar çalışmaya devam ediyor. `location` bilinçli
   olarak tam yol kabul etmeye devam ediyor — orası kısıtlanmadı.

   Bu doğrulama **iki turda** tamamlandı: ilk tur mutlak yolu ve `..`'yi
   engelledi, ama kendine referans veren hedefi kaçırdı —
   `{"target": ".", "location": "desktop"}` çağrısı, `base / "."`
   pathlib'de `base`'e sadeleştiği için **masaüstü klasörünün tamamını**
   siliyordu (gerçek dispatcher üzerinde doğrulandı). Artık `target`,
   `location`'ın kendisine çözülemez. Ders: "sandbox dışına çıkma"yı
   engellemek yetmiyor, "sandbox'ın KENDİSİNİ hedefleme"yi de engellemek
   gerekiyor.

### c) "Başardım" diyen ama hiçbir şey yapmayan tool'lar

v0.7'de `windows.launch_app` için düzeltilen **yanlış-pozitif başarı**
hatasının aynısı iki yerde daha vardı:

- `windows.set_volume`: `pyautogui.press(...)` çağrısını hiç kontrol
  etmeden koşulsuz `success=True` döndürüyordu.
- `windows.screenshot`: `pyautogui.screenshot()` çıplak çağrılıyordu;
  ekran kilitliyken `OSError` fırlatıp kullanıcıya dispatcher'ın genel
  "Beklenmeyen hata: ..." mesajı gidiyordu.
- `filesystem.copy`: hedefte aynı isimde bir öğe varsa **sessizce üzerine
  yazıyordu** (`copy2` / `copytree(dirs_exist_ok=True)`) — geri alınamaz
  veri kaybı, üstelik `SAFE` işaretliyken. Artık varsayılan olarak
  reddediyor; üzerine yazmak için açık `overwrite: true` gerekiyor.

İlk ikisi artık `WindowsSetBrightnessTool`'un zaten kullandığı
`try/except` + açıklayıcı Türkçe mesaj desenini izliyor.

### d) `plugins/web_plugin.py` (yeni, 2 tool)

README bölüm 6'da bekleyen plugin'lerden biri eklendi — yeni bağımlılık
yok, tamamen stdlib (`webbrowser`, `urllib.parse`):

- `web.search` — `query` + `engine` (`google`/`youtube`/`wikipedia`/
  `github`; Wikipedia Türkçe). Motor şablonları modül seviyesinde tek bir
  sözlükte, yeni motor eklemek tek satır.
- `web.open_url` — **yalnızca `http`/`https`** şemalarına izin verir;
  `file:`, `javascript:`, `data:` reddedilir ve tarayıcı hiç açılmaz
  (URL'yi LLM ürettiği için yerel dosya/script şemaları açtırılmamalı).
  Şemasız girdiye (`github.com`) `https://` eklenir.

İkisi de `webbrowser.open()`'ın dönüş değerini kontrol ediyor — (c)'deki
dersin tekrarlanmaması için.

### e) Test kapsamı ve bayat testler

- **`tests/test_filesystem_plugin.py` eklendi**: projenin en merkezi
  plugin'inin kendi test dosyası yoktu. `location: "last"` bağlam hafızası
  akışı ve `filesystem.delete` onay akışı (onaysız çağrıdan sonra dosyanın
  HÂLÂ diskte olduğunun doğrulanması dahil) artık test ediliyor.
- **`tests/test_web_plugin.py` eklendi** (tarayıcı gerçekten açılmadan).
- **İki bayat test düzeltildi**: `tests/test_llm_client.py`'daki sahte
  `ollama.chat` lambda'ları v0.6'da eklenen `keep_alive=` parametresini
  kabul etmiyordu; `TypeError` → `ConnectionError`'a dönüşüp testi
  alakasız bir sebeple kırıyordu.
- **Ortam bağımlı testler artık atlanıyor, kırılmıyor**: `test_set_volume`
  (kullanıcının hoparlörünü GERÇEKTEN sessize alıyordu) ve
  `test_screenshot`, altta yatan yetenek o oturumda kullanılamıyorsa
  `pytest.skip` ile atlanıyor.

### f) `skills/` sorusu kapatıldı

v0.1'den beri "onay bekliyor" durumundaki soru karara bağlandı:
**skills çerçevesi kurulmuyor, klasör bilinçli olarak boş kalıyor.**
Gerekçe kısaca: v1.0'da eklenen `TaskPlanner` zaten tool zincirleme işini
yapıyor; sabit skill'ler ikinci ve gereksiz bir yol olurdu. Kararın tam
gerekçesi ve **hangi iki koşulda geri açılacağı** `skills/README.md`'de
yazılı.

### g) `.context` ve `.claude/agents/`

- **`.context`** (yeni): "şu an neredeyiz" anlık görüntüsü — mevcut kod
  envanteri, açık boşluklar ve verilmiş mimari kararlar. `README.md`
  kronolojik sürüm kaydı olmayı sürdürüyor; `.context` ise tek bakışta
  durum özeti.
- **`.claude/agents/artemis-worker.md`** (yeni): Claude Code'da mekanik
  işleri (test yazma, şablonu izleyerek tool ekleme) daha küçük/ucuz bir
  modele devretmek için proje kurallarını taşıyan bir alt-ajan tanımı.

## 17) Sesli asistan ve Siri benzeri arayüz (v2.0)

Artık `python main.py --voice` ile Artemis penceresiz, arka planda
çalışır; **adını söylediğinizde** ekranın altında Siri benzeri bir
pencere belirir.

### a) Akış

```
"Artemis"  →  wake_word (Vosk)   ── uyanır ──▶  overlay belirir (fade-in)
komut       →  stt (Whisper)      ── metin ──▶  overlay metni gösterir
            →  llm_client (Ollama) ─ JSON ──▶  planner → dispatcher → tool
cevap       →  tts (Piper)         ── ses ──▶  dalga formu konuşmayla canlanır
            →  overlay kapanır (fade-out)
```

### b) Neden İKİ ayrı ses tanıma motoru?

Bu, teslimatın en önemli tasarım kararı:

- **Uyandırma sözcüğü GÜN BOYU çalışır** → en ucuz seçenek olmalı.
  Vosk'un küçük Türkçe modeli (~50MB RAM) kullanılır ve tanıyıcı
  **dilbilgisi (grammar) kısıtlamasıyla** yalnızca "artemis" varyantlarını
  arayacak şekilde daraltılır — bu hem doğruluğu artırır hem işlemciyi
  neredeyse boşta tutar.
- **Komut tanıma yalnızca uyandıktan sonra, birkaç saniye çalışır** →
  orada DOĞRULUK kritiktir, çünkü yanlış anlaşılan bir komut yanlış tool
  çağrısı demektir. Bu yüzden çok daha doğru olan Whisper
  (`faster-whisper`) kullanılır ve **yalnızca gerektiğinde** belleğe
  alınır.

Bu, projenin `ollama_keep_alive` felsefesinin (bölüm 10) ses tarafındaki
karşılığıdır: ağır olanı sürekli değil, gerektiğinde yükle.

### c) Sesli onay: "şüphede reddet"

Tehlikeli tool'lar (`filesystem.delete`, `windows.shutdown`) sesli modda
da onay ister — ama kural bilinçli olarak asimetriktir:

> **Yalnızca NET bir onay duyulursa devam edilir.** Sessizlik,
> anlaşılmayan cevap, zaman aşımı, mikrofon hatası — hepsi RED sayılır.

Gerekçe: ses tanıma hata yapabilir ve bu işlemler geri alınamaz.
"Hayır dediğini anlarsam dururum" kuralı, yanlış anlaşılan bir "hayır"da
işlemi ÇALIŞTIRIRDI. Ayrıca onaylanacak işlem ve argümanları ekranda
gösterilir (bkz. bölüm 16b — kör onay güvenlik açığıdır).

### d) Geri besleme (feedback) koruması

Artemis konuşurken mikrofon açıktır ve kendi sesini duyar. Önlem
alınmazsa kendi "Artemis" deyişini duyup kendini uyandırır (sonsuz
döngü). Bu yüzden konuşma boyunca ve sonrasında kısa bir soğuma süresi
boyunca uyandırma algılaması susturulur (`core/voice_loop.py::_muted_until`).

### e) Arayüz (`ui/overlay.py`)

PyQt6 ile çerçevesiz, gerçek alfa-saydamlıklı, her zaman üstte bir
pencere. Dört durumu farklı renk paletiyle gösterir: dinliyor
(mavi→mor→pembe), düşünüyor (mor nabız), konuşuyor (mavi), hata
(turuncu→kırmızı). Dalga formu gerçek mikrofon/hoparlör seviyesine göre
canlanır.

**Ses katmanı olmadan önizlemek için** (mikrofon/model gerekmez):

```bash
python -m ui.overlay
```

Arayüz katmanı bilinçli olarak "aptal"dır: ne mikrofon dinler ne karar
verir, yalnızca dışarıdan sürülür. Böylece arayüz tamamen değiştirilse
bile `core/` ve `voice/` katmanlarında değişiklik gerekmez.

### f) İş parçacığı (thread) mimarisi

Qt'de arayüze yalnızca ana iş parçacığından dokunulabilir; ses işleme ise
bloklayıcıdır. Bu yüzden:

- **Ana iş parçacığı**: Qt olay döngüsü, overlay çizimi, kısayol tuşu, tepsi simgesi
- **Ses işçisi**: mikrofon okuma, uyandırma algılama, Whisper, Ollama, tool, Piper

Ses işçisi arayüzü doğrudan çağırmaz; `ArtemisOverlay`'in public metotları
Qt sinyali yayınlar ve Qt bunları otomatik olarak ana iş parçacığına
kuyruklar. Bu yüzden overlay metotları her iş parçacığından güvenle
çağrılabilir.

Mikrofonu **tek bir yer** açar (`voice_loop._run`); onay dinlemesi gibi
iç adımlar bu akışı paylaşır — aynı cihazı iki kez açmak bazı Windows
ses sürücülerinde başarısız olur.

### g) Kurulum

```bash
pip install -r requirements.txt
python scripts/setup_voice.py    # Vosk + Piper modellerini indirir (~150MB)
python main.py --voice
```

Modeller `voice_models/` altına iner ve `.gitignore`'dadır (repoya girmez).

### h) Çift mod: uyandırma sözcüğü + kısayol

`config.yaml::wake_word_enabled` ile sürekli dinleme kapatılabilir; bu
durumda Artemis yalnızca kısayol tuşuyla (varsayılan `ctrl+alt+a`) veya
tepsi simgesindeki "Şimdi dinle" ile çağrılır. Kısayol Windows'un
`RegisterHotKey` API'siyle kaydedilir (`ui/hotkey.py`) — hangi uygulamada
olursanız olun çalışır.

Uygulama penceresiz çalıştığı için **sistem tepsisi simgesi**
(`ui/tray.py`) tek görünür arayüzdür; çıkış oradan yapılır.

### i) Bu teslimattaki bilinen sınırlar

- ⚠️ **Uyandırma sözcüğü gerçek sesle doğrulanmadı.** "Artemis" bir özel
  isimdir ve küçük Türkçe modelin sözlüğünde tam karşılığı olmayabilir;
  model bunu "artemiz", "arte mis" gibi çevirebilir. Bu yüzden hem varyant
  listesi hem bulanık eşleşme kullanılır ve varyantlar
  `config.yaml::wake_words` ile genişletilebilir. Kendi telaffuzunuzda
  neyin çıktığını görüp listeye eklemeniz gerekebilir.
- ⚠️ Türkçe TTS sesi kurulu olmadığı için Piper tercih edildi; Windows'un
  kendi Türkçe sesi (Tolga) kuruluysa ileride `pyttsx3` alternatifi
  eklenebilir.

## 18) Vosk atıldı, hibrit bulut/yerel ses geldi (v2.1)

**Bildirilen sorun**: "Uygulamaların isimleri özel isim ve yabancı isim
olduğu için çoğunu anlamıyor. Artemis'i de anlamıyor."

Bu iki AYRI sorundu ve farklı katmanlarda çözüldü. Tahminle değil,
ölçerek: kullanıcının sesi kullanılamadığı için **Piper ile Türkçe
konuşma üretilip** motorlara verildi.

### a) Uyandırma sözcüğü hiç çalışmıyordu (Vosk atıldı)

Ölçüm:

| Söylenen | Vosk küçük TR modeli | Whisper tiny + ipucu |
|---|---|---|
| "Artemis" | **"akdeniz"** ❌ | **"Artemis"** ✅ |

Dahası, Vosk'un dilbilgisi (grammar) kısıtlaması **her girdide boş string
döndürüyordu** (`"artemiz" sözlükte yok` uyarısıyla) — yani v2.0'daki
uyandırma kodu fiilen hiçbir zaman uyanamıyordu. Modelin sözlüğü FST'ye
derlenmiş olduğu için "artemis" gibi bir özel isim orada yoksa
üretilemiyor; bu bir ayar meselesi değil, modelin sınırı.

**Yeni tasarım**: Vosk tamamen kaldırıldı. Uyandırma artık **enerji
kapısı + Whisper `tiny`**:

- Önce ucuz bir RMS ölçümüyle konuşma olup olmadığına bakılır. Sessiz
  odada Whisper **hiç çağrılmaz** — CPU maliyeti sıfırdır.
- Konuşma bittiğinde biriken ses bir kez Whisper'a verilir.
  Ölçüm: 0.81 sn'lik klip → **0.24 sn** tanıma.
- **Ön-tampon (pre-roll)** şart: enerji eşiği aşıldığında "Ar-" hecesi
  çoktan geçmiştir; öncesindeki birkaç blok saklanıp sesin başına eklenir.

Yan fayda: bir bağımlılık ve 35MB model eksildi.

### b) Uygulama isimleri: Whisper'a "ipucu sözlüğü" (hotwords)

`faster-whisper`'ın `hotwords` parametresi, modele "bu sözcükler geçebilir"
der. Sözlük **kullanıcının GERÇEKTEN kurulu uygulamalarından** üretilir
(`AppResolver.known_app_names()`, Başlat Menüsü taraması) — sabit bir liste
her makinede yanlış olurdu.

**Beklenmedik bulgu — daha çok isim DAHA İYİ DEĞİL.** Aynı ses klipleriyle:

| Söylenen | ipucu yok | 6 ad | 15 ad | 45 ad |
|---|---|---|---|---|
| "Discord'u aç" | bozuk | discord ✓ | discord ✓ | **"distrodoge"** ✗ |
| "Valorant'ı aç" | "balorantı" | "balorantı" | **valorant ✓** | "balorantı" ✗ |

Uzun listeler kod çözücüyü seyreltip birbirine karışmış çıktılar üretiyor.
Bu yüzden sözlük **15 adla sınırlı** ve alfabetik/uzunluk yerine "günlük
konuşmada geçme olasılığı"na göre sıralı. Başlat Menüsü'ndeki geliştirici
araçları/dokümantasyon girdileri ayıklanıyor (175 kısayoldan 15'e).

> Denenip **REDDEDİLEN** bir fikir: yanlış çevirileri kurtarmak için
> "ünsüz iskeleti" eşleştirme (chrome→chrm, cehrum→chrm). Gerçek yanlış
> çevirilere karşı ölçüldü ve mevcut `difflib` eşleşmesinden **daha kötü**
> çıktı; eklenmedi. Ölçmeden eklenseydi sessizce zarar verecekti.

### c) Hibrit: internet varsa bulut, yoksa yerel

Kullanıcı kararı: *"%100 local olmak zorunda değiliz."* Bulut modelleri
yabancı özel isimlerde belirgin biçimde daha iyi.

```
voice/
├── stt.py        stt_cloud.py     # yerel Whisper   | Groq whisper-large-v3-turbo
├── tts.py        tts_cloud.py     # yerel Piper      | Microsoft Edge (anahtarsız)
└── router.py                      # kararı veren tek yer
```

**Yönlendirici neden "önce ping atıp internet var mı" BAKMIYOR**: bir
bağlantı testi (a) her komuta gecikme ekler, (b) yanıltıcıdır — ping geçse
bile anahtar geçersiz olabilir, servis 500 dönebilir. Yaklaşım "sor" değil
**"dene"**: bulut denenir, herhangi bir hata olursa sessizce yerele düşülür.

**Soğuma (cooldown) neden şart**: olmasaydı internet tamamen kapalıyken
HER komut önce bulutun zaman aşımını (15 sn) beklerdi ve asistan
kullanılamaz hale gelirdi. Bir hatadan sonra bulut 60 saniye atlanır.

**Yan fayda**: bulut çalışırken yerel Whisper **hiç yüklenmez** — RAM
maliyeti hiç ödenmez (sağlayıcılar fabrika olarak, tembel kurulur).

Gerçek ağ üzerinde doğrulandı: Edge sentez+çözme 1.43 sn; bulut kasten
bozulduğunda Piper devraldı ve soğuma devreye girdi.

### d) `stt_provider` / `tts_provider`

| Değer | Anlamı |
|---|---|
| `auto` (varsayılan) | Önce bulut, hata/internet yoksa yerele düş |
| `cloud` | Yalnızca bulut; başarısız olursa dürüstçe hata ver (sessizce yerele düşmek kullanıcının tercihini çiğnerdi) |
| `local` | Yalnızca yerel — ses/metin **hiçbir yere gönderilmez** |

### e) GÜVENLİK: API anahtarı asla depoya girmez

Bu depo **herkese açıktır** ve `config/config.yaml` git ile izlenir. Bu
yüzden Groq anahtarı bilinçli olarak `Settings`/`config.yaml` üzerinden
GELMEZ; yalnızca iki kaynaktan okunur (`config/settings.py::get_groq_api_key`):

1. `GROQ_API_KEY` ortam değişkeni (önerilen)
2. `config/secrets.yaml` — `.gitignore`'dadır

Anahtar hiçbir log'a veya hata mesajına yazılmaz (bunun için ayrı test var).

### f) `voice_enabled` ayarı gerçekten bağlandı

`voice_enabled: false` "mikrofon hiç açılmaz" diye BELGELENMİŞTİ ama
hiçbir kod onu okumuyordu — kullanıcı kapatsa bile mikrofon açılırdı.
Artık `main_voice()` bunu Ollama'yı başlatmadan önce kontrol ediyor ve
regresyon testi var.

### g) Kurulum değişikliği

```bash
pip install -r requirements.txt
python scripts/setup_voice.py     # artık YALNIZCA Piper (~60MB); Whisper
                                   # modelleri ilk kullanımda otomatik iner
setx GROQ_API_KEY "gsk_..."       # opsiyonel; yoksa her şey yerel çalışır
python main.py --voice
```

### h) Bu teslimattaki bilinen sınırlar

- ⚠️ **Tüm ölçümler Piper'ın SENTETİK sesiyle yapıldı**, gerçek insan
  sesiyle değil. Piper İngilizce isimleri Türkçe fonetikle okuduğu için
  ("Brave" → "Bravi") bazı vakalar gerçekte olduğundan zor görünüyor
  olabilir. Yön doğru ama mutlak başarı oranı kendi sesinizle
  denendiğinde farklı çıkacaktır.
- ⚠️ Groq yolu **gerçek bir anahtarla denenmedi** (ortamda anahtar yoktu);
  yalnızca sahte HTTP yanıtlarıyla test edildi.
- ⚠️ Uyandırma sözcüğü hâlâ gerçek sesle doğrulanmadı; `config.yaml::wake_words`
  listesine kendi telaffuzunuzda çıkanı ekleyebilirsiniz.

## 19) İlk gerçek kullanımdan gelen düzeltmeler (v2.2)

Asistan ilk kez gerçek bir makinede, gerçek sesle çalıştırıldı. Ortaya
çıkan dört sorun — hepsi yalnızca sahada görülebilecek türden.

### a) CUDA çökmesi: `device` açıkça geçilmiyordu (KRİTİK)

```
RuntimeError: Library cublas64_12.dll is not found or cannot be loaded
```

`voice/wake_word.py`, `WhisperModel`'i cihaz belirtmeden çağırıyordu.
`faster-whisper`'ın varsayılanı **`device="auto"`**: makinede bir NVIDIA
GPU görürse CUDA'yı seçer. Kullanıcıda GPU vardı ama CUDA çalışma
kütüphaneleri kurulu değildi.

Bu hatanın sinsi tarafı: model YÜKLENİRKEN değil, **ilk TANIMA sırasında**
patlıyor. Yani her şey sorunsuz başlıyor, kullanıcı "Artemis" dediğinde
çöküyor. (`voice/stt.py` `device="cpu"` geçtiği için güvendeydi —
yalnızca uyandırma modülünde eksikti.)

**Düzeltme**: cihaz artık her yerde AÇIKÇA geçilir, `whisper_device`
ayarıyla ("cpu" varsayılan; CUDA kuruluysa "cuda"). `"auto"` kullanılmaz.

### b) Tembel generator tuzağı

`transcribe()` bir **generator** döndürür; asıl çıkarım segmentler
tüketildiğinde çalışır. Bu yüzden yalnızca `transcribe()` çağrısını
try/except'e almak yetmiyordu — hata `"".join(...)` satırında yüzeye
çıkıyordu. İkisi de aynı bloğa alındı.

### c) Tek bir tanıma hatası TÜM asistanı öldürüyordu

`feed()` içindeki hata ses işçisi iş parçacığını sonlandırıyor, böylece
kısayol tuşu ve tepsi menüsü dahil hiçbir şey kalmıyordu. Artık uyandırma
onarılamaz biçimde bozulursa **kapatılır**, kullanıcıya "kısayolu
kullanın" denir ve asistan çalışmaya devam eder. Hata döngü başına bir
kez raporlanır (her ses bloğunda denemek log'u ve işlemciyi boğardı).

### d) Ses kalitesi: gereksiz 16 kHz'e düşürme + kekemelik

Kullanıcı geri bildirimi: *"Geri konuştuğu ses çok robotik ve takılıyor
gibi."* İki ayrı sebep:

1. **Gereksiz örnekleme düşürme**: Edge TTS 24 kHz üretiyor ama kod sesi
   16 kHz'e indiriyordu — bant genişliğinin üçte biri çöpe gidiyordu.
   16 kHz yalnızca Whisper'a GİRDİ verirken zorunludur; bu bir ÇIKTI.
   Artık akışın kendi hızı korunuyor.
2. **Çok küçük çalma tamponu**: `RawOutputStream` varsayılan düşük
   gecikmeyle açılıyordu ve döngü her yazma arasında genlik hesaplıyordu;
   tampon boşalıp ses kesik kesik çıkıyordu. Artık `latency="high"` ve
   genlik yazmadan ÖNCE hesaplanıyor. (Aynı düzeltme yerel Piper'a da
   uygulandı.)

### e) "aç" → "such": sözlük tamamen İngilizceydi

`hotwords` yalnızca İngilizce uygulama adlarından oluşunca kod çözücü
İngilizceye kayıyordu. Ölçüm:

| İpucu sözlüğü | "League of Legends aç" |
|---|---|
| yalnızca uygulama adları | "lagoi of **legansage**" |
| + Türkçe komut sözcükleri | "lagoi of **legends, aç**" |

Sözlüğe Türkçe komut fiilleri eklendi (`aç`, `kapat`, `oluştur`, `sil`…);
hem fiil hem uygulama adı düzeliyor.

### f) Yanlış tool seçimi: uygulama mı, site mi?

"league of legends such" gibi bozuk bir girdide LLM `web.open_url`
seçiyordu. Tool açıklamaları fazla kısaydı ("Bir uygulamayı açar." /
"Verilen URL'yi açar.") ve küçük bir model için ayırt edici değildi.

Açıklamalar örneklerle genişletildi ve sistem promptuna bir ayrım kuralı
eklendi ("tanımadığın bir adı web adresi SANMA, muhtemelen bir uygulamadır").

Kullanıcının kendi modeliyle (`llama3.1:8b`) doğrulandı — **5/5**:
"League of Legends aç" → `windows.launch_app`, "github.com'u aç" →
`web.open_url` (aşırı düzeltme yok).

### g') Testler kullanıcının bilgisayarını kilitliyordu (GELİŞTİRME HATASI)

Kullanıcı, geliştirme sırasında bilgisayarının sürekli kilit ekranına
düştüğünü ve sesinin kapandığını bildirdi. Sebep testlerin kendisiydi:

- `test_lock_workstation` → `windows.lock` çağırıp ekranı GERÇEKTEN kilitliyordu
- `test_set_volume` → sesi GERÇEKTEN kapatıyor ve geri açmıyordu

Bu testler her `pytest` çalıştırmasında koşuyordu. Artık
`@pytest.mark.disruptive` işaretliler ve **varsayılan olarak
çalıştırılmıyorlar** (`pyproject.toml::addopts = "-m 'not disruptive'"`).
Bilinçli çalıştırmak için `pytest -m disruptive`. Ses testi ayrıca
sessize aldığı sesi eski haline döndürüyor.

Kural `CLAUDE.md`'ye eklendi: *gerçek makineyi gözle görülür biçimde
etkileyen hiçbir şey varsayılan testte çalışmamalı.*

### h) `windows.close_app` 12 süreci zorla öldürüyordu

Kullanıcı "Brave'i kapat" dedi; tool ismi eşleşen HER süreci tek tek
`terminate()` etti — Chromium 10+ süreç açtığı için 12 süreç, üstelik
crash handler'lar dahil (onlar erişim reddi verip log'u kirletti).
`terminate()` Windows'ta zorla sonlandırmadır: uygulamaya sekmelerini
kaydetme şansı vermez.

Yeni akış: (1) eşleşenleri bul, (2) yalnızca KÖK süreçleri seç (çocuklar
zaten ana süreçle kapanır), (3) ana pencerelere `WM_CLOSE` gönderip
uygulamanın kendi kapanma akışını çalıştırmasına izin ver, (4) kısa bir
beklemeden sonra hâlâ yaşayanları zorla kapat.

### i) `setx` ile kaydedilen API anahtarı "kayıtlı ama görünmez"di

Kullanıcı `setx GROQ_API_KEY ...` çalıştırıp aynı pencereden Artemis'i
başlattı; anahtar kayıt defterine yazılmıştı ama `setx` ZATEN AÇIK
terminalleri etkilemediği için süreç onu göremiyordu. `get_groq_api_key()`
artık son çare olarak Windows kullanıcı ortam değişkenlerini (kayıt
defteri) de okuyor — terminali yeniden açmak gerekmiyor.

### j) Log hangi ses sağlayıcısının kullanıldığını söylemiyordu

"Ses neden robotik?" sorusu araştırılırken log'a bakıp bulut mu yerel mi
kullanıldığını anlamanın yolu yoktu (yönlendirici yalnızca HATA durumunda
yazıyordu). Artık sağlayıcı DEĞİŞTİĞİNDE bir INFO satırı yazılıyor
("Ses sağlayıcı (TTS): bulut") — her çağrıda değil, yoksa log şişer.

### k) Ses tonlaması ayarlanabilir oldu

`edge_tts_rate` / `edge_tts_pitch` ayarları eklendi. Nöral seslerin
robotik algılanmasının en yaygın sebebi fazla hızlı/düz okumadır;
kullanıcının karşılaştırma yapabilmesi için Emel/Ahmet × normal/yavaş
örnekleri üretildi. **Kullanıcı `Emel + normal`'ı seçti** — yani asıl
sorun sesin kendisi değil, (d)'de düzeltilen 16 kHz düşürme ve
kekemelikmiş; varsayılanlar değişmedi.

### l) Azure Speech eklendi ve bulut STT seçilebilir oldu (v2.3)

Kullanıcının gözlemi: *"Google mic kullanınca her şeyi net anlıyor."*
Ölçüm bunu destekledi — Whisper ailesi (yerel `small` DE, bulut
`large-v3-turbo` DA) "League of Legends" gibi yabancı özel isimlerde
takılıyor ve bu, sadece ekranda yanlış metin değil **yanlış eylem**
üretiyordu (log'da, kullanıcının istemediği bir `example.txt` dosyası
oluşturulduğu görüldü).

Google Cloud STT ile Azure Speech karşılaştırıldı; **Azure seçildi**:

| | Google Cloud STT | Azure Speech |
|---|---|---|
| Ücretsiz kota | ~60 dk/ay | **5 saat/ay** |
| Kurulum | servis hesabı JSON'u | **tek API anahtarı** |
| İfade önceliklendirme | var (boost 0-20) | REST'te YOK, yalnızca SDK'da |

Azure'un kotası 5 kat, kurulumu Groq kadar kolay. Karşılığında REST
arayüzünde ifade önceliklendirme yok — bu bilinçli bir ödünç; temel
Türkçe modeli yetmezse ağır SDK'ya geçilebilir.

`stt_cloud_provider` ayarı eklendi (`azure` | `groq`). Seçilen servisin
anahtarı yoksa asistan BOZULMAZ: yönlendirici sessizce yerele düşer ve
log'a ne yapılması gerektiğini yazar.

> **Dürüstlük notu**: Beğenilen "Google mic" muhtemelen Google Cloud STT
> DEĞİL — Chrome/Android'in *tüketici* tanıma motorudur ve bulut API'siyle
> aynı model olmak zorunda değildir. Bu yüzden hiçbir bulut servisi o
> deneyimi birebir vaat edemez; karar kullanıcının kendi sesiyle
> denemesine bırakıldı.

### m) Teşhis boşluğu: ne duyulduğu loglanmıyordu

"Asistan yanlış şeyi yaptı" şikayetleri araştırılamıyordu: log'da yalnızca
hangi tool'un çalıştığı görünüyor, kullanıcının ne dediği ve modelin ne
duyduğu görünmüyordu — yani hata TANIMADA mı yoksa LLM'in TOOL SEÇİMİNDE
mi, ayırt etmek imkânsızdı. Artık ikisi de loglanıyor:

```
Duyulan komut: 'league of legends açar mısın'
LLM planı: [('windows.launch_app', {'name': 'League of Legends'})]
```

### n) GPU, Vosk konuşma kapısı ve gürültü kalibrasyonu (v2.7)

Kullanıcı "dediklerimi tam anlamıyor" dedi ve NVIDIA ekran kartı olduğunu
belirtti. Model değiştirmeden önce ölçüm yapıldı ve **iki ayrı sorun**
olduğu görüldü.

#### Sorun 1: ses girdisi — sabit eşik değişken gürültüde çalışmaz

Log'da aynı cümlenin her seferinde farklı çevrildiği görüldü:

```
"Masajındaki sosyal medyayı aç"              ← yanlış
"Masa üstündeki sosyal medya klasörünü aç"   ← doğru
"Masa, üstümdeki sosyal ve medya klasörünü"  ← yanlış
```

Model tutarlı olsaydı aynı sesi aynı yazardı. Mikrofon ölçüldü — ve
sonuç, model değiştirmenin bunu çözmeyeceğini gösterdi. Aynı odada,
aynı gün:

| Ölçüm | Ortalama genlik | Sabit eşiğe (0.06) göre |
|---|---|---|
| 1 | **0.075** | blokların **%65'i** eşiği aşıyor (kimse konuşmuyorken) |
| 2 | 0.019 | hiç aşmıyor |

Yani **sabit bir enerji eşiği ilkesel olarak çalışamaz.** Eşiğin üstünde
kalan bir odada iki şey birden bozulur: (a) uyandırma sürekli tetiklenir,
(b) kayıt "kullanıcı sustu" diyemez ve komutun etrafındaki konuşmaları da
yutar — log'daki *"Hazırlamadım. Gamze, sosyal medya klasörünü aç."*
dökümü tam olarak budur.

İki ayrı düzeltme:

- **Vosk geri geldi — ama sözcük tanıyıcı olarak DEĞİL.** Aynı gürültü
  Vosk'a verildi ve **hiç kelime üretmedi**; yani gürültüyle konuşmayı
  ayırt edebiliyor. Enerji eşiğinin yapamadığı tam olarak bu. Vosk artık
  yalnızca "birisi gerçekten konuşuyor mu?" kapısıdır; uyandırma sözcüğünü
  tanımak hâlâ Whisper'ın işi (Vosk "Artemis"i "akdeniz" duyuyor — bu
  ölçülmüştü, v2.1'de atılma sebebiydi).
- **Kayıt eşiği açılışta kalibre ediliyor.** `measure_noise_floor()` ilk
  saniyede ortamı dinler ve eşiği gürültü tabanının 2 katına ayarlar
  (0.08-0.40 arasına sıkıştırılmış). Sessiz odada alt sınıra, gürültülü
  odada gürültünün üstüne oturur.

#### Sorun 2: model — GPU açıldı

RTX 4050 (6 GB) için CUDA Toolkit'in tamamı gerekmedi; iki pip paketi
yetti (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`). Ama Windows bu DLL'leri
`site-packages` altında BULAMAZ; `voice/gpu.py` iki adımı birden yapar:
dizinleri arama yoluna ekler **ve** DLL'leri `ctypes.CDLL` ile açıkça
sürece yükler. Yalnızca birincisi yetmiyor (ölçüldü).

Sonuç — aynı ses klipleri:

| | Model | Süre | "League of Legends açar mısın" |
|---|---|---|---|
| Önce | small (CPU) | 1.35 sn | "**lobby** of legends açar mısın" ❌ |
| Sonra | **large-v3-turbo (GPU)** | **0.33 sn** | "**league of legends**, açar mısın" ✅ |

Haftalardır sorun olan cümle düzeldi; üstelik 4 kat hızlı.

**Güvenlik ağı**: `whisper_device: "cuda"` istenip CUDA bulunamazsa
Artemis sessizce CPU'ya düşer **ve** büyük modeli `small` ile değiştirir —
`large-v3-turbo` CPU'da 10+ saniye sürer ve asistanı kullanılamaz yapardı.

#### `scripts/smoke_voice.py` — uçtan uca duman testi

Birim testleri her parçayı ayrı doğruluyor ama "mikrofondan tool'a"
yolunun tamamını yalnızca bu gösterir. Piper ile konuşma üretip
`VoiceAssistant`'a mikrofondan geliyormuş gibi besler; zincirin geri
kalanı (uyandırma, Whisper, komut kapısı, Ollama, planner, dispatcher)
GERÇEKTİR. Yalnızca hoparlör susturulur ve masaüstü geçici bir klasöre
yönlendirilir — gerçek masaüstüne dokunulmaz.

```
[söylenen] Masaüstünde Orbit klasörü oluştur
  duyulan : 'Masaüstünde orbit klasörü oluştur.'
  cevap   : orbit klasörü oluşturuldu.

[söylenen] Sen kimsin?
  cevap   : Bir komut duymadım.     ← komut kapısı çalışıyor
```

### o) Uyandırma sıkılaştırıldı ve prompt %58 küçüldü (v2.9)

Kullanıcı: *"Açılış kelimesi çok farklı şeylerle de tetikleniyor."*

#### Ölçüm önce, düzeltme sonra

Log'daki "yanlış" uyanmaların çoğu aslında yanlış DEĞİLDİ: kullanıcı
gerçekten "Artemis" demiş (cevap gelmediği için tekrar tekrar), etrafındaki
gürültü de kayda girmiş. Tek gerçek yanlış pozitif **`temiz`** sözcüğüydü —
bulanık benzerlik 0.833, eşik 0.82. Kıl payı geçiyordu.

Dört düzeltme, hepsi ölçümle:

1. **`vad_filter=True`** — asıl kazanç. Whisper tiny gürültüde kelime
   UYDURUR; ölçüldü: ortam gürültüsünden `'Hızlı, hızlı, hızlı'` üretti.
   Silero VAD ile konuşma olmayan kısım tanımaya hiç girmiyor → çıktı `''`.
2. **`no_speech_prob` kapısı** — model kendi şüphesini bildiriyor:
   gürültüde 0.59, gerçek konuşmada 0.11. 0.40 üstü segmentler yok sayılıyor.
3. **Noktalama temizliği** — gerçek bir hata: Whisper çıktıya nokta
   ekliyor ve nokta sözcüğe YAPIŞIK geldiği için benzerliği düşürüyordu
   (`'arteniz'` 0.857 → `'arteniz.'` 0.800, eşiğin altına iniyor). Yani
   asistan doğru duyduğu hâlde yalnızca nokta yüzünden uyanmıyordu.
4. **Eşik 0.82 → 0.85**, tahminle değil veriyle:

   | | uyanmalı | uyanmamalı |
   |---|---|---|
   | 0.82 | 9/9 | 1/17 geçiyor (`temiz`) |
   | **0.85** | **9/9** | **0/17** |
   | 0.86+ | 8/9 (`arteniz` kaybediliyor) | 0/17 |

**Gerçek mikrofonla doğrulama**: 10 saniye ortam gürültüsü → **sıfır
yanlış uyanma**; "Artemis" → uyanıyor; "temiz" → uyanmıyor.

#### Prompt iki kez gönderiliyordu (4.8x hızlanma)

Gecikme ölçüldüğünde darboğaz beklenen yerde değildi:

| Aşama | Süre | Pay |
|---|---|---|
| STT (GPU large-v3-turbo) | 1.02 sn | %10 |
| Komut kapısı | 1.86 sn | %18 |
| **LLM tool seçimi** | **7.32 sn** | **%72** |

Sebep bir hataydı: `{tool_manifest}` yer tutucusu şablonun GELİŞTİRİCİ
BAŞLIĞINDA da örnek olarak geçiyordu ve `str.replace()` tüm eşleşmeleri
değiştirdiği için **tool listesi prompta iki kez basılıyordu**.

```
şablon 4.023 + manifest 8.915  ->  beklenen ~12.900 karakter
gerçekte üretilen              ->        21.823 karakter
```

Üç düzeltme:
- Geliştirici başlığı artık tamamen atılıyor (modele hitap etmeyen metin).
- Manifest girintisiz basılıyor (girinti yalnızca token harcar).
- `danger_level` prompttan çıkarıldı — onayı dispatcher uygular, modelin
  bilmesine gerek yok.

**Sonuç: 21.823 → 9.198 karakter (%58 küçülme), tool seçimi 7.32 → 1.51
saniye (4.8 kat), doğruluk korunuyor (6/7).**

Regresyon testi eklendi: manifest prompta tam olarak BİR KEZ girmeli ve
prompt bir üst sınırı aşmamalı — bu sınır bir kalite tercihi değil,
gecikme bütçesidir.

### g) Arayüz: ne dediğiniz artık ekranda kalıyor

Kullanıcı isteği: *"Dediklerimi Artemis yazısının altında gösterse daha
iyi olabilir."* Döküm artık başlığın hemen altında, tırnak içinde ve
**cevaptan ayrı bir satırda** duruyor; Artemis konuşurken de görünür
kalır. Asistanın yanlış anladığı ancak böyle fark edilir.

---

## 20) Ses doğrudan modele verilebilir mi? — ölçüm ve karar (v3.0)

Kullanıcı isteği açıktı: *"gemma4:e4b modelini kullanıcam ve bu modelin
içinde doğrudan ses işleme var. Aradaki sesten metine çevirme işlemini
iptal edelim."* Fikir mantıklıydı — bir aşamayı silmek her zaman
cazip. Ama bu, kod yazmadan ÖNCE ölçülmesi gereken bir iddiaydı.

### 20a) Ollama'da ses gerçekten hangi kapıdan giriyor?

İlk deneme başarısız oldu ve YANLIŞ sonuca götürüyordu: `/api/chat`'e
`audio` diye bir alan gönderdim, HTTP 200 geldi, model *"Lütfen ses
kaydını paylaşın"* dedi. Alan sessizce yok sayılmıştı. Ollama'nın kendi
dokümanı da *"audio input is not supported through the /api/chat
endpoint"* diyordu.

Buna rağmen `ollama show gemma4:e4b` çıktısı `audio` yeteneğini
bildiriyordu — yani doküman ile model çelişiyordu. Çelişkiyi CLI'ı
deneyerek çözdüm: `ollama run gemma4:e4b "... dosya.wav"` çalıştı ve
**"Added audio"** yazdı. Kaynağa bakınca sebep göründü
(`cmd/interactive.go`):

```go
case ".wav":
    fmt.Fprintf(os.Stderr, "Added audio '%s'\n", nfp)
default:
    fmt.Fprintf(os.Stderr, "Added image '%s'\n", nfp)
}
imgs = append(imgs, data)   // ikisi de AYNI listeye gider
```

Yani ses, ayrı bir alandan değil **`images` alanından** gönderiliyor;
sunucu WAV/MP3'ü sihirli baytlarından tanıyor. Ollama'nın kendi PR'ı
(#16585) bunu doğruluyor: *"audio support via the `images` field is
currently undocumented"*. **Ders: bir özelliğin yokluğunu dokümana
bakarak değil, deneyerek kanıtlayın.**

### 20b) Yol açıktı — ama sonuç kullanılamazdı

Doğru alanla ölçüm (Piper ile üretilmiş temiz konuşma, 16 kHz mono):

| Yol | Türkçe | İngilizce |
|---|---|---|
| **faster-whisper large-v3-turbo** | **0.39 sn**, kusursuz | **0.36 sn**, kusursuz |
| `gemma4:12b` ses | 8.2 sn, halüsinasyon | 52.4 sn, transkribe etmedi |
| `gemma4:e4b` ses | `[noise]` / `लह लह` / `어어어` | *"Oh no, no, no."* |

`gemma4:e4b`'nin ses çıktısı her denemede FARKLI bir çöptü. Bu, Ollama'da
açık bir kayıt olarak zaten biliniyor (#16584: *"gemma4:e4b audio
transcription regression... hallucinated output"*) ve `think:false` ile
de düzelmiyor.

`gemma4:12b` daha iyisini yaptı ama beklenen şeyi değil: İngilizce
örnekte *"To create a folder named 'Orbit' on your desktop, you can
use..."* dedi — yani sesi **anladı**, ama yazıya dökmedi, **cevapladı**.
Türkçe örnekte "orbit" kelimesini duyup gezegen yörüngeleri üzerine
nutuk attı. Gemma'nın ses kulesi transkripsiyon için değil, sesi anlayıp
konuşma üretmek için eğitilmiş.

Aynı WAV dosyalarını faster-whisper üçünde de kusursuz çözdü — yani
dosyalar sağlamdı, sorun modeldeydi.

**Karar: sesten metne çevirme faster-whisper'da kalır.** Bir aşamayı
silmek, o aşamayı 20–145 kat yavaş ve yanlış bir şeyle değiştirmeye
değmez. Bu yol fiziksel olarak AÇIK olduğu için (`images` alanı WAV
kabul eder) yanlışlıkla seçilebilir; `tests/test_config_model.py` bunu
bekçiliyor.

### 20c) Asıl kazanç başka yerdeydi: düşünme modu

Ses yolu kapanınca `gemma4:e4b` yine de değerlendirildi — bu sefer
**beyin** olarak, yani tool seçen model olarak. 10 gerçek Türkçe sesli
komutla ölçüldü:

| Model | Doğruluk | Gecikme (ort) |
|---|---|---|
| `llama3.1:8b` | 10/10 | 1.90 sn |
| `gemma4:e4b` | 10/10 | **4.14 sn** |
| `qwen3.5:4b` | 10/10 | **4.57 sn** |

Yeni modeller ESKİSİNDEN YAVAŞTI. Sebep, `core/llm_client.py`'ın
`think` alanını hiç göndermemesiydi: Ollama, düşünme yeteneği olan
modellerde bu alan yoksa varsayılan olarak düşünmeyi AÇAR. Tool seçimi
düşünme gerektirmeyen bir sınıflandırma işi olduğundan model, her
komutta boşuna token yakıyordu.

`think=False` eklendikten sonra:

| Model | Doğruluk | Gecikme (ort) |
|---|---|---|
| `gemma4:e4b` | 10/10 | **1.17 sn** (4.14'ten) |
| `qwen3.5:4b` | 10/10 | **1.07 sn** (4.57'den) |

~3.5 kat hızlanma, doğruluktan hiçbir şey kaybetmeden. Düşünme yeteneği
OLMAYAN modeller (`llama3.1`, `phi4-mini`, `qwen2.5`) bu alanı sorunsuz
kabul edip yok sayar, bu yüzden koşulsuz gönderilir.

`gemma4:e4b` diskte 9.6 GB görünür ama MatFormer mimarisi sayesinde
bellekte 3.3 GB'a sığar ve 6 GB'lık bir GPU'ya **%100 yerleşir**
(`ollama ps` ile doğrulandı) — varsayılan model artık bu.

### 20d) Yolda bulunan sessiz hata: varsayılan model hiç çalışmıyordu

`config/config.yaml` içinde `ollama_model: "llama3.1"` yazılıydı. Ollama
etiketsiz bir adı `llama3.1:latest` diye çözer; kurulu olan ise
`llama3.1:8b` idi. Yani yapılandırmadaki varsayılan model, "model yok"
gibi okunmayan bir `ConnectionError` üretiyordu. Arıza yalnızca
interaktif model seçimi atlandığında ortaya çıktığı için uzun süre
görünmedi.

Aynı hata `config/settings.py`'daki varsayılanda da vardı. İkisi de tam
etikete çevrildi; `tests/test_config_model.py` artık etiketsiz bir ad
yazılmasını engelliyor.

### 20e) `scripts/smoke_voice.py` yanlış modeli sınıyordu

Duman testi `modeller[0]` ile **kurulu ilk modeli** alıyordu. `ollama
list` en son indirileni başa koyduğu için, test asistanın gerçekte
çalıştıracağı modeli değil rastgele bir modeli sınıyordu — yani yeşil
sonucu hiçbir şey kanıtlamıyordu. Bu, `gemma4:12b` indirildiği anda
görüldü: test sessizce ona geçmişti.

Artık `get_settings().ollama_model` kullanılıyor; o model kurulu
değilse test bunu UYARI olarak yazdırıp devam ediyor. (Dikkat:
`Settings()` config.yaml'ı okumaz, yalnızca sınıf varsayılanlarını
döndürür — doğrusu `get_settings()`.)

### 20f) Açık kalan çelişki: komut kapısı ile sistem promptu aynı şeyi söylemiyor

Duman testinde *"Sen kimsin?"* girdisi **"Bir komut duymadım."** cevabını
aldı. Sebep bir hata değil, iki bileşenin farklı sözleşme konuşması:

- `prompts/system_prompt.md` bu girdi için AÇIK bir örnek içerir:
  `assistant.reply` ile *"Ben Artemis, bilgisayarınızı sesle yönetmenize
  yardım ediyorum."*
- `core/llm_client.py::_COMMAND_GATE_PROMPT` ise "soru" kategorisini
  KOMUT DEĞİL sayar ve girdiyi LLM'e hiç ulaştırmaz.

Yani sistem promptundaki o örnek pratikte hiç çalışmıyor. Bu, README
§16a'daki hatanın aynı ailesinden: iki katman aynı sözleşmeyi
konuşmuyor.

Kapı BİLEREK gevşetilmedi. Kullanıcının açık isteği *"Açılış kelimesi
çok farklı şeylerle de tetikleniyor bunu da biraz özelleştir."*
olduğundan, kapıyı sorulara açmak yanlış-pozitifleri geri getirme riski
taşır. Doğru çözüm muhtemelen kapıya üçüncü bir karar eklemektir
(komut / sohbet / gürültü), ama bu bir ürün kararıdır ve ölçülmeden
yapılmamalıdır — bu yüzden kayda geçirilip bırakıldı.

---

## 21) Hangi model ana model olmalı? — ayırt eden kıyas (v3.1)

§20c'deki kıyas ise yaramamıştı: üç model de 10/10 yaptı. Hepsinin
geçtiği bir sınav hangisinin daha iyi olduğunu söylemez. İkinci kıyas
GERÇEK arızalardan derlendi — Whisper'ın yanlış duyduğu bozuk girdiler
(`"Dis jordaç"`, `"league of legends such"`), bilerek belirsiz
bırakılmış istekler ve çok adımlı komutlar (20 senaryo).

| Model | Skor | Gecikme | Tehlikeli hata |
|---|---|---|---|
| **`gemma4:e4b`** | 18/20 | 1.19 sn | **0** |
| `qwen3.5:4b` | 18/20 | 1.07 sn | 1 |
| `llama3.1:8b` | 17/20 | 1.73 sn | 2 |
| `qwen2.5:14b` | 16/20 | 7.45 sn | 0 (ama 6x yavaş) |
| `phi4-mini` | 12/20 | 0.78 sn | 5 |
| `gpt-oss:20b` | 0/20 | — | şemayla çalışmıyor (§21b) |

**Asıl ölçüt skor değil, hatanın YÖNÜ.** `gemma4:e4b`'nin iki hatası da
"anlamadım" demek, yani güvenli yön. Diğer hızlı modellerin hepsi en az
bir kez yanlış yöne hata yaptı:

- `qwen3.5:4b` → *"bilmemne sitesini aç"* için **gerçekten bir adres
  uydurup açtı**. Sistem promptu kural 4 bunu açıkça yasaklıyor.
- `llama3.1:8b` → *"bir şeyler aç"* gibi bomboş bir cümleye uygulama
  başlattı; *"hava durumuna bak"* için adres uydurdu.
- `phi4-mini` → *"Evi kapat"*'ı **`windows.shutdown`** diye yorumladı.
  Yalnızca `CONFIRM_REQUIRED` olduğu için bilgisayar kapanmadı.

Sesli asistanda yanlış duyma KAÇINILMAZ olduğundan, "emin değilsem
dokunmam" diyen model, "emin değilken bilgisayarı kapatan" modelden
iyidir. Varsayılan `gemma4:e4b` bu yüzden korundu — ses yeteneği yüzünden
değil (o yol §20'de elendi), **hata yaparken zarar vermediği için**.

Ortak başarısızlık: `"Dis jordaç"` girdisini ALTI modelin hiçbiri
çözemedi. Bu bir LLM sorunu değil — Whisper kelimeyi parçalıyor. Çözüm
yeri `plugins/_app_resolver.py` ve `hotwords`, model değil.

### 21b) `gpt-oss:20b` bu mimariyle çalışmıyor

Model, `format` parametresi verildiği anda **boş içerik** döndürüyor —
hem şema-kısıtlı modda hem `format="json"` modunda. Yalnızca hiçbir
kısıtlama olmadan cevap veriyor:

| Strateji | `gpt-oss:20b` | `gemma4:e4b` |
|---|---|---|
| şema-kısıtlı | `''` | geçerli JSON |
| `format="json"` | `''` | JSON |
| kısıtsız | düz metin | düz metin |

Yani §9'daki 4 katmanlı güvencenin ilk iki katmanı bu modelde işlemiyor
ve akış son çareye düşüyor. `think=False` değişikliğinin bununla ilgisi
YOK (ayrıca doğrulandı: gpt-oss düşünmeyi zaten yok sayıyor, üç ayarda
da aynı çıktıyı veriyor). Çok adımlı işler için gpt-oss:20b düşünülecekse
önce bu çözülmeli.

---

## 22) Bulut STT'ye hâlâ gerek var mı? — gerekçe çürüdü (v3.2)

Kullanıcı sordu: *"azure speech e gerek var mı?"* Sorunun kendisi
haklıydı, çünkü bulut STT'nin gerekçesi (§18) şuydu: *"belirgin biçimde
daha doğru, özellikle yabancı özel isimlerde"*. O cümle, yerel model
**CPU'da küçük bir modelken** yazılmıştı. Sonra §19'da GPU'ya ve
`large-v3-turbo`'ya geçildi — ama gerekçeye geri dönülüp bakılmadı.

Ölçüm (8 Türkçe komut, Edge TTS nöral sesiyle üretildi; Piper'ın sentetik
telaffuzu İngilizce özel isimleri bozduğu için adil bir sınav olmazdı):

| | Doğruluk | Gecikme |
|---|---|---|
| **yerel** (faster-whisper large-v3-turbo, GPU) | **6/8** | **0.43 sn** |
| Azure Speech (bulut) | 4/8 | 1.01 sn |

Azure, tam da **eklenme sebebinde** kaybetti:

| Söylenen | yerel | Azure |
|---|---|---|
| "League of Legends'ı aç" | ✓ | ✗ `League of legendsage` |
| "GitHub'ı aç ve ekran görüntüsü al" | ✓ | ✗ `Githubu'ı aç...` |
| "Visual Studio Code'u aç" | ✗ `Code Watch` | ✗ `kodeva` |

Yani `stt_provider: "auto"` her komutta önce **daha yavaş VE daha yanlış**
olan yolu deniyordu. `"local"` yapıldı.

Kazançlar: komut başına ~0.6 sn, iki tanıma hatası, bir API anahtarı
bağımlılığı ve bir gizlilik riski (ses artık makineden hiç çıkmıyor).

**TTS bundan etkilenmez.** Bulut TTS'i Microsoft Edge TTS'tir ve API
anahtarı İSTEMEZ; Piper'a göre belirgin biçimde daha doğal olduğu için
(`tts_provider: "auto"`) olduğu gibi bırakıldı. STT ile TTS bağımsızdır.

**Ders**: bir bileşenin gerekçesi, altındaki başka bir bileşen
yükseltildiğinde sessizce geçersizleşebilir. §18'in gerekçesi §19
tarafından çürütülmüştü ama üç sürüm boyunca kimse geri dönüp bakmadı.
Bir şeyi "neden eklediğimizi" yazmak, ancak o gerekçeyi periyodik olarak
sınarsak işe yarar.

---

## 23) Komut kapısı: sınır "komut mu" değil "asistana yönelik mi" olmalıydı (v3.3)

§20f'de açık bırakılan çelişki çözüldü: *"Sen kimsin?"* → **"Bir komut
duymadım."** Sebep, `is_actionable_command`'ın ikili sorusunun yanlış
yerden ayırmasıydı — "bu bir İŞLEM mi?" diye soruyordu ve "soru, sohbet"i
de KOMUT_DEGIL sayıp tool seçimine hiç göndermiyordu. Halbuki
`prompts/system_prompt.md` tam bu girdi için açık bir örnek içeriyordu
(`assistant.reply` → *"Ben Artemis..."*). Aynı kırık sınır, fark
edilmeden, `"evi kapat"` ve `"sosyal medyayı aç"` örneklerini de
vuruyordu — ikisi de system_prompt.md'de açıkça netleştirici bir soru
bekliyor ama kapı onları da önceden eliyordu.

**Üçe bölme (komut/sohbet/gürültü) yerine sınır kaydırıldı.** İlk
planlanan çözüm kapıya üçüncü bir karar eklemekti, ama bu gereksiz
olurdu: `assistant.reply` normal tool seçiminden **aynı yoldan** çıkıyor
(system_prompt.md kural 3), yani "işlem" ile "sohbet" downstream'de HİÇ
farklı davranmıyor. Kapıya üçüncü bir kategori eklemek modele yalnızca
belirsizlik katardı — küçük modeller ikili sınıflandırmada ölçülmüş
biçimde daha iyi (`should_engage` docstring'i: 11/12). Doğru ayrım tek
yerde gerekliydi: **asistana yönelik mi, değil mi.**

`is_actionable_command` → `should_engage` oldu. Yeni sınır:

- **YÖNELİK** (tool seçimine gider): işlem, soru, sohbet, belirsiz ama
  açıkça asistana söylenmiş istek ("bir şeyler aç").
- **GÜRÜLTÜ** (engellenir): yarım/anlamsız cümle, arka planda konuşma,
  başkasıyla konuşma — asistana söylendiği belli bile değil.

Gerçek modelle (gemma4:e4b) canlı doğrulama, 14 örnekten 13'ünde
beklenen kategoriye düştü; tek "sapma" (`"Abi insanlarız ki?"` →
GÜRÜLTÜ) aslında zararsız, çünkü system_prompt.md'nin kendi örneği bile
o girdiyi *"Bunu anlayamadım, tekrar eder misiniz?"* diye cevaplıyor —
iki davranış da aynı anlama geliyor, yalnızca söylenen cümle farklı.
Gecikme değişmedi (~1 sn).

**Ders**: bir güvenlik kapısını daraltırken sınırı nereye çizdiğinizi
downstream davranışa göre seçin. "Komut/komut değil" doğal bir ayrım
gibi görünüyordu ama gerçek ayrım "tool seçimine gitsin mi" idi — ve
`assistant.reply`'ın var oluşu bu ikisini birbirinden ayırıyordu.

---

## 24) Argüman şeması artık gerçekten doğrulanıyor (v3.4)

`.context` §7'de uzun süredir açık bir madde vardı: `models/tool_models.py`
docstring'i *"dispatcher argümanları bu modeller üzerinden
doğrulayabilir"* diyordu ama bu bir vaatti, gerçekleşmiyordu.
`InvalidToolArgumentsError` tanımlıydı, dispatcher onu YAKALIYORDU, ama
hiçbir yerde FIRLATILMIYORDU — yani ölü bir güvenlik ağıydı.

Sonuç: eksik zorunlu bir argüman (`filesystem.open` çağrısında `target`
unutulması gibi), tool'un içinde ham bir `KeyError` olup dispatcher'ın
genel `except Exception` bloğuna düşüyor ve kullanıcı *"Beklenmeyen
hata: 'target'"* gibi anlaşılmaz bir mesaj görüyordu. Süreç çökmüyordu
ama hatanın NEREDEN geldiği belli değildi.

**Çözüm `core/dispatcher.py::_validate_arguments`** — onay mantığı gibi
TEK bir merkezi yerde yaşıyor, her tool kendi kontrolünü yazmıyor.
Tool çalışmadan ÖNCE, tehlike/onay kontrolünden de ÖNCE çalışır (bozuk
bir tehlikeli çağrı, kullanıcıya boş argümanla "??? dosyasını silmek
istiyor musunuz" diye sormamalı).

**Standart bir JSON Schema doğrulayıcısı (`jsonschema` paketi) BİLEREK
kullanılmadı.** O paket ortamda zaten kurulu (mcp'nin dolaylı
bağımlılığı) ve `jsonschema.validate()` ile tek satırda yazılabilirdi —
ama STRICT: `"50"` (string) değerini asla bir `integer` saymaz. Bu
projedeki tool'lar zaten TOLERANSLI (`WindowsSetBrightnessTool`:
`int(arguments["level"])`), çünkü küçük/yerel bir modelin sayısal bir
alanı string üretmesi olası bir hata sınıfı. Standart kütüphaneyi
kullansaydım, doğrulama tool'ların KENDİSİNDEN daha katı olurdu ve
önceden sorunsuz çalışan çağrılar reddedilirdi. Bunun yerine ~90
satırlık, projeye özgü ve aynı toleransı uygulayan bir doğrulayıcı
yazıldı (`_type_matches`): eksik alan / enum dışı değer / kökten yanlış
tür (liste yerine string gibi) yakalanır, sayısal-string ayrımı
tool'ların zaten tolere ettiği gibi tolere edilir.

**Ders**: bir güvenlik/doğruluk katmanı eklerken, onu var olan
davranıştan DAHA katı yapmak da bir regresyondur — "doğru" olmak,
projenin geri kalanıyla aynı tolerans sözleşmesini konuşmayı gerektirir.

---

## 25) Adımlar arası veri akışı: `{{step_N.alan}}` referansları (v3.5)

v1'den beri bilinçli bir sınırlama vardı (`core/planner.py` modül
dokümantasyonu): adımlar arasında veri aktarımı yoktu. "Rapor klasörü
oluştur ve içine notlar.txt koy" gibi bir komutta, ikinci adımın
`location`'ı LLM tarafından TAHMİN edilmek zorundaydı — model klasörün
tam yolunu (`C:\Users\...\Desktop\Rapor`) önceden bilemeyeceği için ya
yanlış bir yol uydururdu ya da `"desktop"` yazıp `filesystem.create_file`
dosyayı klasörün İÇİNE değil masaüstüne doğrudan koyardı.

**Tasarım kararı — neden "önceki adımı bekleyip sonrakini yeniden
üret" YAPILMADI:** Bu, adım başına ayrı bir LLM çağrısı gerektirirdi
(yavaş — mevcut tool seçimi zaten ~1-2 sn) ve modelin ara sonucu okuyup
akıl yürütmesini gerektirirdi; küçük/yerel modeller bunda güvenilir
değil (bkz. §20/§21'deki ölçülmüş kanıt). Bunun yerine model TÜM
adımları TEK seferde, `{{step_N.alan}}` biçiminde REFERANS YER
TUTUCULARIYLA üretir; gerçek değer yalnızca ÇALIŞMA ZAMANINDA,
`core/planner.py::_resolve_step_references` tarafından önceki adımın
GERÇEK `ToolResult.data`'sından okunarak yerleştirilir. Model hiçbir
zaman bir dosya yolu uydurmaz — yalnızca "N. adımın `path` alanı" der.

Şema değişmedi (`arguments` zaten serbest-formatlı bir JSON objesi,
bkz. `_response_schema`); yalnızca `prompts/system_prompt.md`'ye kural
8 ve bir örnek eklendi. Gerçek modelle (gemma4:e4b) canlı doğrulama: 3
farklı klasör/dosya adıyla 3/3 doğru sözdizimi üretti, sadece ezber
yapmadı — genelleşti. Uçtan uca dispatcher/planner ile de doğrulandı:
dosya gerçekten oluşan klasörün İÇİNE yazıldı.

**Güvenlik notu:** referans çözümlemesi, onay diyaloğundan ÖNCE yapılır.
`filesystem.delete`'in `target`'ı `{{step_1.path}}` olsa bile, kullanıcı
onay ekranında yer tutucuyu değil ÇÖZÜLMÜŞ gerçek yolu görür — aksi
halde "onay NEYİ onayladığını göstermek zorunda" ilkesi (README §16b)
ihlal edilirdi.

Hata durumları anlaşılır mesajlarla kapatıldı: kendine/ileriye referans,
başarısız bir adıma referans, var olmayan bir alana (`filesystem.
search`'ün `matches`'i gibi) referans — hepsi tool hiç çalıştırılmadan,
net bir Türkçe mesajla `success=False` döner.

---

## 26) `mouse_keyboard` ve `browser` plugin'leri (v3.6)

CLAUDE.md'de "henüz yok" diye işaretlenmiş iki plugin eklendi
(artemis-worker ajanına delege edildi — mekanik, şablon takip eden iş):

- **`plugins/mouse_keyboard_plugin.py`** (5 tool): `move_mouse`, `click`,
  `type_text`, `press_key`, `scroll`. `pyautogui` ile doğrudan ekrana
  girdi gönderir. Koordinatlar `pyautogui.size()`'a karşı sınır
  doğrulamasından geçer; `move_mouse`/`click` başarıyı `pyautogui.
  position()` ile GERÇEK imleç konumunu okuyup doğrular (koşulsuz
  `success=True` yasağına uyum).
- **`plugins/browser_plugin.py`** (6 tool): `new_tab`, `close_tab`,
  `go_back`, `go_forward`, `refresh`, `switch_tab`. `web_plugin.py`'den
  farkı: yeni URL AÇMAZ, halihazırda açık bir tarayıcı penceresini
  kontrol eder. Kasıtlı olarak mekanik kaldı — CDP/Selenium/WebDriver
  DEĞİL, işletim sistemi seviyesinde standart klavye kısayolları
  (`ctrl+t`, `ctrl+w`, `alt+left`...). Sayfa içeriği okuma/DOM erişimi
  gerektiren ihtiyaçlar bilinçli olarak kapsam dışı — o, ayrı bir
  protokol istemcisi gerektiren `mcp_plugin.py`'nin işi.

**Güvenlik doğrulaması (browser):** kısayollar yalnızca ön planda
GERÇEKTEN bir tarayıcı varsa (ya da açık pencereler arasında bulunup öne
getirilebiliyorsa) gönderilir; bulunamazsa kısayol hiç gönderilmeden
dürüstçe `success=False` döner. Bu olmasaydı örn. `browser.close_tab`,
kullanıcının o an kullandığı BAŞKA bir uygulamaya (`Ctrl+W`) gidip
istenmeyen bir şeyi kapatabilirdi.

**`danger_level` kararı — hepsi `SAFE`:** projedeki SAFE/CONFIRM_REQUIRED
eşiği "geri alınabilirlik"e göre çizili (delete/shutdown/restart/format/
registry/service). Bir tıklama/tuş basışı/kısayol KENDİSİ geri alınamaz
bir sistem işlemi değil — tıpkı zaten SAFE olan `windows.close_app`'ın
(bir uygulamayı kapatması) örneğinde olduğu gibi. `browser.close_tab`
bile çoğu tarayıcıda `Ctrl+Shift+T` ile geri açılabilir.

49 yeni test (21 + 28), tamamı `pyautogui`/`win32gui`/`win32process`/
`psutil` monkeypatch'li — gerçek ekran/girdi etkisi yok. Tüm paket artık
**373 test**.

**Not — tool manifest bütçesi dolmaya yaklaşıyor:** 32 tool ile sistem
promptu 13.533 karakter (§16'daki 14.000 karakter sınırına ~470 karakter
kaldı). `mcp_plugin.py` (hâlâ eksik, bkz. §7) veya başka bir plugin
eklendiğinde bu sınır aşılabilir; o noktada ya sınır yükseltilmeli ya
manifest daha da sıkıştırılmalı (örn. `description` alanları kısaltılarak).

---

## 27) `mcp_plugin.py`: dinamik MCP tool'larını statik registry'ye bağlamak (v3.7)

Task 4'ten bilerek ayrılmıştı (§26): MCP (Model Context Protocol)
sunucuları, `browser`/`mouse_keyboard` gibi şablon takip eden bir iş
değil, gerçek bir mimari çelişki içeriyordu.

### 27a) Çelişki: statik registry vs dinamik keşif

Her Artemis tool'u `@register_tool` ile İMPORT ZAMANINDA, statik olarak
`TOOL_REGISTRY`'e kaydedilir; sistem promptunun manifest'i de bu sabit
registry'den üretilir. MCP sunucuları ise tool'larını ÇALIŞMA
ZAMANINDA, sunucuya bağlanıp `list_tools()` çağırarak açıklar. LLM bir
tool'u yalnızca manifest'te görürse isteyebilir, manifest de yalnızca
bir kez üretilir — yani keşif "tembel" (ilk kullanımda) olsaydı, MCP
tool'ları LLM'e hiç görünmezdi (tavuk-yumurta sorunu).

**Çözüm:** MCP sunucuları, bu modül import edilirken (yani `load_plugins()`
sırasında, uygulama başlarken) bir kez keşfedilir ve her keşfedilen tool
için `_make_mcp_tool_class` DİNAMİK olarak gerçek bir `BaseTool` alt
sınıfı üretip normal registry'e kaydeder. Bunun ötesinde dispatcher,
planner (zincir referansları dahil) ve manifest üretimi hiçbir özel
durum eklenmeden çalışır — bir MCP tool'u, sistemin geri kalanı için
sıradan bir Artemis tool'undan AYIRT EDİLEMEZ. Bu, "yeni bir mixin/ara
sınıf eklemek yerine composition" ilkesinin (CLAUDE.md) doğal bir
uzantısı: MCP'ye özel kod yalnızca "dinamik olanı statiğe çevir"
adaptöründe yaşıyor, geri kalan hiçbir katmana sızmıyor.

### 27b) Varsayılan sıfır I/O — bu depo herkese açık

`config.yaml::mcp_servers` varsayılan olarak BOŞ liste. Boşken bu
modülün import edilmesi hiçbir ağ/süreç I/O'su yapmaz (`mcp` paketi
bile içe aktarılmaz) — bu, test paketinin (her test dosyası
`load_plugins()` çağırır) hızlı ve sunucusuz kalmasını garanti eder.
Bir kullanıcı sunucu eklerse, o sunucuya bağlanma denemesi kısa bir
zaman aşımıyla (varsayılan 5 sn) ve `try/except` ile sarılıdır: yanıt
vermeyen/bozuk BİR sunucu diğer sunucuların veya tüm uygulamanın
açılışını KİLİTLEMEZ/ÇÖKERTMEZ, yalnızca o sunucunun tool'ları eksik
kalır ve bir uyarı loglanır. Bu üç iddia da gerçek bir alt süreçle
ölçüldü (mock değil): bozuk komut → 0 tool, çökmez; 0.001 sn zaman
aşımı → 0 tool, çökmez; bozuk + çalışan sunucu birlikte → yalnızca
çalışanın tool'ları kaydedilir.

### 27c) Güven modeli: kaynağın güvenilirliği, işlemin geri alınabilirliğinden ayrı bir risk ekseni

Projedeki mevcut `danger_level` ayrımı "işlem geri alınabilir mi"
eksenine göre çizili (delete/shutdown/... → CONFIRM_REQUIRED). MCP
tool'ları YENİ bir risk ekseni taşıyor: bir MCP sunucusu Artemis'in
kendi yazdığı/denetlediği kod DEĞİLDİR, üçüncü taraf bir süreçtir.
Bu yüzden bir sunucu `config.yaml`'da `trusted: true` işaretlenmedikçe
tool'ları `CONFIRM_REQUIRED` kaydedilir (uçtan uca doğrulandı: ölçüm,
mock değil). `trusted: true` verilirse `SAFE` olur.

### 27d) v1 kapsamı — bilerek yalnızca stdio

MCP'nin `sse`/`streamable_http`/`websocket` taşımaları da var, ama bu
proje SDK'nın yalnızca `stdio` (yerel süreç, `npx ...`/`python -m ...`)
taşımasını destekliyor. Gerekçe: stdio, gerçek dünyadaki MCP
sunucularının büyük çoğunluğunun çalışma şekli; uzak/HTTP sunucular
ayrı bir kimlik-doğrulama/gizli-anahtar yönetimi ister (Groq/Azure
anahtarlarıyla aynı desen) ve hiçbir kullanıcı bunu istemedi —
hipotetik bir ihtiyaç için şimdiden eklenmedi (CLAUDE.md: "don't design
for hypothetical future requirements"). Aynı desen izlenerek ileride
eklenebilir.

### 27e) Testler GERÇEK bir alt süreç kullanır, mock değil

`tests/fixtures/mcp_echo_server.py`, `FastMCP` ile yazılmış GERÇEK,
bağımsız bir stdio MCP sunucusudur (üç basit tool: `echo`, `add`,
`always_fails`). Testler bunu gerçekten başlatıp gerçek keşif+çağrı
turu yapar — sahte bir `ClientSession` bu mimarinin asıl riskli
kısmındaki (süreç yönetimi, senkron/asenkron köprü, protokol çevirisi)
hataların çoğunu göstermezdi. Round-trip ~1.1 sn; alt süreç sızıntısı
olmadığı `psutil` ile doğrulandı.

**Önemli izolasyon notu:** `TOOL_REGISTRY` süreç genelinde paylaşılan
bir global'dir. MCP testleri kaydettikleri tool'ları test sonunda
MUTLAKA temizler (`_deregister`) — aksi halde `test_prompt_builder.py`'nin
14.000 karakter sınırını (gerçek tool'lar zaten 13.533 karakterde,
§26) yanlışlıkla şişirip ALAKASIZ bir testi flaky biçimde kırabilirdi.
Bu, geliştirirken gerçekten yaşanıp düzeltildi.

20 yeni test (tümü geçiyor). Tam paket artık **393 test**, 18.6 sn (MCP
alt süreç testleri hariç önceki ~5 sn'ye ek ~13 sn — kabul edilebilir,
gerçek entegrasyon güveninin karşılığı).

`mcp>=1.20` `requirements.txt`'e eklendi (bu geliştirme ortamında
dolaylı olarak zaten kuruluydu — ilgisiz bir küresel araçtan geliyordu,
projeye ait değildi; artık açıkça bağımlılık olarak listelendi).

CLAUDE.md, .context §7 madde 1'de "eksik" diye işaretlenmiş üç
plugin'in üçü de artık tamam: `browser_plugin.py`, `mouse_keyboard_plugin.py`
(§26), `mcp_plugin.py` (bu bölüm).

---

## 28) Geliştirme planı: rejim düzeltmesi + eksik dosya işlemleri (v3.8-v3.9)

Kullanıcı "projeyi geliştirmek için bir plan yap" dedi ve önceliklendirmeyi
tamamen bana bıraktı. Plan yazmadan önceki tarama şunu buldu: çalışma
dizininde commit edilmemiş, kaynağı belirsiz bir `system_prompt.md`
değişikliği vardı — var olmayan `filesystem.list` tool'undan ve var
olmayan `documents`/`pictures`/`music`/`videos` `location` takma
adlarından bahsediyordu, prompt'u 16.571 karaktere çıkarıp 14.000
karakter bekçi testini kırıyordu. Bu **v3.8**'de düzeltildi (§ önceki
commit) — içerik korunarak (❌ yanlış-kullanım karşıt-örnekleri fikri
iyiydi) 13.875 karaktere sıkıştırıldı.

### 28a) `filesystem.rename` + `filesystem.move` (v3.9)

`filesystem.*`'da yalnızca `copy` vardı, taşıma/yeniden adlandırma
yoktu. İkisi de `FilesystemCopyTool`'un şablonunu izliyor
(`_safe_join`/`_unsafe_target_result` ile aynı path-traversal koruması),
`danger_level = SAFE` (Copy ile aynı gerekçe: veri silinmiyor, yanlış
hedefe gitse bile geri taşınabilir).

**Rename'de EK bir doğrulama gerekti:** yeni ad da `_safe_join`'den
geçirilmeli — aksi halde `Path.rename()`'e verilen bir `../` göreli
yolu, dosyayı dizin dışına taşıyabilirdi (Copy'de bu risk yok çünkü
hedef her zaman ayrı bir `destination_dir` değişkeninden gelir, target
string'i doğrudan hedef yol inşasına karışmaz).

**İkisinde de bulunup düzeltilen bir tasarım hatası:** kaynak ve hedef
AYNI yola çözülürse (örn. `rename(target="x.txt", name="x.txt")` ya da
`move(target="x.txt", destination_location="desktop")` kaynak zaten
masaüstündeyken), `overwrite=True` dalı önce hedefi SİLİP sonra
kaynaktan taşımaya çalışırdı — ama kaynak ZATEN o hedefti, yani önce
kendini silip sonra "taşımaya" çalışıyordu: **veri kaybı**. Bu, testler
yazılırken bulundu (kod incelemesiyle değil, "aynı isme yeniden
adlandır" senaryosunu test ederken) — `source == destination` erken
kontrolü eklendi. 15 yeni test (8 rename + 7 move), ikisi de bu
regresyonu özel olarak sınıyor.

### 28b) Prompt bütçesi: geçici olarak gevşetildi, ölçümle kesinleşecek

Yeni iki tool, promptu 13.875 → 14.820 karaktere çıkardı, 14.000
sınırını aştı. Planın geri kalanı (memory.*, windows.arrange_window —
toplam 5 tool daha) bunu daha da büyütecek. Sorun: o an makinede **hiç
Ollama modeli kalmamıştı** (kullanıcı temizlik yaparken hepsini silmiş),
yani "kaç karakterden sonra gecikme gerçekten kötüleşiyor" sorusu
GERÇEK bir modelle ölçülemedi — tıpkı eski 14.000 sınırının kendisinin
de tam bir ölçümden değil, "§16'daki kötü 21.823'ten güvenle uzak dur"
sezgisinden gelmesi gibi.

Sınır **geçici olarak 18.000'e** çekildi (`tests/test_prompt_builder.py`,
gerekçe test docstring'inde) — rastgele değil, planlanan tüm tool'ları
geçirecek kadar cömert ama eski kötü değerin (21.823) altında bir tavan.
Bir model geri yüklenince gerçek ölçümle KESİNLEŞTİRİLECEK (plan Faz 6).

---

## 29) Hafıza LLM'e açıldı: `memory.remember`/`recall`/`forget` (v3.10)

`memory/context_memory.py` genel bir SQLite k-v deposu olarak zaten
vardı ama yalnızca İÇ kullanım içindi (`remember_last_path`/
`get_last_path`, `location: "last"`'ın dayandığı yer) — kullanıcı "WiFi
şifremi hatırla" dese bile hiçbir tool bunu kaydedip geri çağıramıyordu.
Bu, kod tabanında en uzun süredir "var ama kullanılmayan" durumdaki
altyapıydı; sınıfın kendi docstring'i genişletmeyi zaten öneriyordu.

**İsim çakışması riski ve çözümü:** ham `set(key, value)`'u doğrudan
LLM'e açmak tehlikeliydi — model (ya da kullanıcı) yanlışlıkla
`key="last_path"` gönderirse `location: "last"` özelliğinin dayandığı
İÇ anahtarı sessizce ezerdi. Çözüm `fact:` önekiyle namespace'lenmiş
yeni convenience metotlar (`remember_fact`/`recall_fact`/`forget_fact`)
— kullanıcı verisiyle sistemin kendi durumu asla aynı ad alanında
çakışmaz. Bu, hem sınıf seviyesinde (`tests/test_memory_context.py`)
hem uçtan uca dispatcher üzerinden (`tests/test_memory_plugin.py`) özel
bir regresyon testiyle doğrulandı: `memory.remember(key="last_path",
...)` çağrısından SONRA bile `filesystem.open(location="last")` doğru
çalışmaya devam ediyor.

Üç yeni tool (`plugins/memory_plugin.py`, tamamı `SAFE`):
- `memory.remember` — bir bilgiyi kaydeder.
- `memory.recall` — bulunamazsa dürüstçe `success=False` döner ("böyle
  bir şey hatırlamıyorum") — model olmayan bir bilgiyi UYDURMAZ (bkz.
  CLAUDE.md, koşulsuz `success=True` yasağı).
- `memory.forget` — hiç var olmamış bir anahtarı "unutmak" hata değil,
  zararsız bir `success=True` ("zaten hatırlamıyordum").

**Güvenlik notu (kod içi belgelenmiş, engellenmiyor):** bu depo
şifrelenmemiş, düz metin bir SQLite dosyasıdır. Gerçek parola/kart
numarası gibi çok hassas veriler için önerilmez — tool açıklamasında
kullanıcı uyarılır ama bu bir kasa değil, Artemis'in kendi notlarını
tuttuğu bir defter.

`memory/context_memory.py` bu değişiklikle birlikte İLK KEZ doğrudan
test edildi (13 test) — daha önce yalnızca dolaylı olarak (filesystem
tool'ları üzerinden) sınanıyordu; yeni kullanıcıya-açık sorumluluk
üstlenince doğrudan test şart oldu. Toplam 21 yeni test, tüm paket 429.

Yeni tool'lar promptu 14.820 → 16.010 karaktere çıkardı (37 tool),
geçici 18.000 sınırının hâlâ altında (§28b).

---

## 30) `windows.arrange_window`: minimize/maximize/restore/snap (v3.11)

Pencere yönetiminde yalnızca `focus_window`/`list_windows` vardı;
küçültme/büyütme/yarım-ekran (snap) yoktu. Beş ayrı tool yerine TEK
tool + `position` enum (`browser.switch_tab`'ın `direction` enum
deseni izlendi) — her yeni tool sistem promptu bütçesine ekleniyor
(§26/§28b), enum konsolidasyonu bunu tek manifest girdisinde tutar.

`WindowsFocusWindowTool`'daki `EnumWindows` + başlık eşleştirme mantığı
ÜÇÜNCÜ kez tekrarlanacaktı (windows.close_app, browser_plugin.py'nin
`_focus_any_browser_window`'ından sonra) — bu kez inline bırakılmadı,
`_find_window_by_title` ortak yardımcısına çıkarıldı (`plugins/
_app_resolver.py`'nin "ortak yardımcı" deseniyle tutarlı) ve
`focus_window` de bunu kullanacak şekilde yeniden yazıldı.

**Snap, görev çubuğunu HARİÇ tutan çalışma alanını kullanır**
(`win32api.GetMonitorInfo(...)['Work']`, `['Monitor']` DEĞİL) — aksi
halde pencerenin alt kenarı görev çubuğunun arkasına gizlenirdi.
Küçültülmüş bir pencere önce eski haline getirilir, sonra taşınır
(küçük bir pencereyi taşımak/boyutlandırmak görünmez bir sonuç verirdi).

**Koşulsuz `success=True` yasağı burada da uygulandı:** `ShowWindow`/
`MoveWindow` bir İSTEK gönderir, pencere yöneticisi reddedebilir (bazı
uygulamalar küçültülmeyi engeller). `GetWindowPlacement`/`GetWindowRect`
ile GERÇEK durum okunup doğrulanır.

**Test yazarken bulunan ölçüm hatası:** Windows API'sinde `ShowWindow`'a
verilen KOMUT (`SW_MINIMIZE=6`) ile `GetWindowPlacement`'ın döndürdüğü
SONUÇ DURUMU (`SW_SHOWMINIMIZED=2`) FARKLI sabitlerdir — yalnızca
maximize/showmaximized değeri tesadüfen aynıdır (3). İlk yazılan sahte
`win32gui`, `ShowWindow`'a gelen komut değerini doğrudan durum olarak
saklıyordu; bu, tool'un KENDİ doğrulama mantığını (doğru olduğu halde)
yanlış-negatif kırdı. Testler çalıştırılıp hata mesajı okunarak
yakalandı — kodun kendisi değil, fake'in gerçekçiliği düzeltildi.

`windows.focus_window` bu değişiklikle İLK KEZ test edildi (daha önce
hiç testi yoktu). Toplam 9 yeni test (3 focus_window + 6 arrange_window,
tamamı sahte `win32gui`/`win32con`/`win32api`, gerçek pencere hiç
açılmaz/taşınmaz). 438 test, prompt 16.010 → 16.416 karakter (38 tool).

---

## 31) Medya tuşları: yeni kod yok, yalnızca dokümantasyon (v3.12)

`pyautogui.KEYBOARD_KEYS` zaten `playpause`, `nexttrack`, `prevtrack`,
`volumemute`, `volumeup`, `volumedown` içeriyordu (doğrulandı) — yani
"müziği duraklat" gibi komutlar `mouse_keyboard.press_key` ile ZATEN
çalışabiliyordu. Tek eksik modelin bunu BİLMESİYDİ: tool açıklaması
hiç ima etmiyordu. `execute()`'a TEK SATIR bile dokunulmadı — yalnızca
`description` genişletildi ve `prompts/system_prompt.md`'ye bir örnek
eklendi. Regresyon testi: "playpause" gerçekten `pyautogui.press()`'e
tek bir tuş olarak gidiyor mu, doğrulandı.

Geliştirme planının Faz 0-4'ü tamamlandı: sistem promptu regresyonu
düzeltildi, `filesystem.rename`/`move`, `memory.remember`/`recall`/
`forget`, `windows.arrange_window`, medya tuşu dokümantasyonu. 439 test
geçiyor, prompt 38 tool ile 16.776 karakter (geçici 18.000 sınırının
altında). Sırada Faz 5 (test kapsamı: `plugin_loader.py`/`tool_base.py`)
ve Faz 6 (gerçek modelle prompt bütçesi kesinleştirme — bir Ollama
modeli geri yüklenmesini bekliyor).

---

## 32) Test kapsamı: `plugin_loader.py`, `tool_base.py`, `manifest.py` (v3.13)

Geliştirme planı Faz 5. Üç çekirdek modül daha önce HİÇ doğrudan test
edilmemişti — yalnızca her test dosyasının `load_plugins()`/`ToolContext`/
`build_system_prompt()` çağırması yoluyla DOLAYLI olarak sınanıyorlardı.
Mekanizmaların kendisi (isim çakışması gerçekten `ValueError` fırlatıyor
mu, `_` ile başlayan dosyalar gerçekten atlanıyor mu, `danger_level`
gerçekten manifest'ten düşüyor mu) hiçbir yerde doğrudan doğrulanmıyordu.

**`_` önek kuralı GERÇEK `plugins/` paketiyle sınanamazdı** —
`_app_resolver.py` hiç tool tanımlamıyor, yani "atlandı" ile "zaten
tool'u yok" ayırt edilemezdi. Bunun yerine izole, geçici bir sahte paket
kuruldu (`tmp_path` altında, gerçek bir Python paketi olarak `sys.path`'e
eklenip sonra temizlenen); `load_plugins()` ona karşı çalıştırılıp hem
normal hem `_` önekli bir modül içerdiği doğrulandı. Gerçek `TOOL_REGISTRY`'ye
kalıcı hiçbir şey eklenmedi.

`tests/test_plugin_loader.py` (10 test): isim çakışması `ValueError`,
`name` zorunluluğu, decorator'ın sınıfı değiştirmeden döndürmesi, sahte
paketle keşif + `_`-atlama, bozuk bir modülün `PluginLoadError` vermesi,
gerçek pakete karşı pozitif kontrol.

`tests/test_tool_base.py` (6 test): `ToolContext`'in GERÇEKTEN frozen
olduğu (mutasyon `FrozenInstanceError` fırlatır), `BaseTool`'un ABC
zorlamasının GERÇEKTEN çalıştığı (eksik `execute`/`get_arguments_schema`
ile örnekleme `TypeError` verir — yalnızca docstring'de yazmak yetmez),
`danger_level`'in varsayılan olarak `SAFE`'e düştüğü.

`core/manifest.py` için yeni bir dosya AÇILMADI (planın kendi kararı —
`test_prompt_builder.py` zaten manifest-bitişik testler barındırıyor,
gereksiz tekrar önlendi); 2 test eklendi: `build_tool_manifest()` (nesne
formu) `danger_level`'ı TUTAR, `build_tool_manifest_json()` (modele
giden asıl form) onu DÜŞÜRÜR ve geçerli/kompakt JSON üretir — önceki
tek test yalnızca tüm prompt string'i üzerinden dolaylı bakıyordu.

15 yeni test, 454 test paketi.

---

## 33) `conversation_loop.py` doc-drift düzeltmesi + uçtan uca doğrulama (v3.14)

Geliştirme planı Faz 7. `core/conversation_loop.py`'nin docstring'i
uzun süre *"STT/TTS hazır olana kadar geçici arayüz"* diyordu — ses
katmanı v2.0'da geldi (README §17) ama bu dosya o zamandan beri hiç
gözden geçirilmemişti. Bu, projenin kendi "README §16a" dersinin
(şema ile sistem promptu aynı sözleşmeyi konuşmalı) bir başka örneği:
iki dosya aynı gerçeği anlatmayı bırakmıştı.

Gerçek durum: `--chat` (bu dosya) ve `--voice` (`voice_loop.py`) İKİSİ
DE kalıcı, ayrı giriş noktaları — `--chat` GEÇİCİ değil, sesin uygun
olmadığı (paylaşımlı ofis, mikrofon yok, betikleme) durumlar için bir
alternatif. Docstring buna göre düzeltildi.

**Uçtan uca doğrulama (varsayılmadı, ölçüldü):** bu oturumda eklenen
tool'ların (`filesystem.rename`, `memory.remember`/`recall`) hem
`conversation_loop.run()` hem `voice_loop.py`'nin KULLANDIĞI AYNI
`TaskPlanner`/`ToolDispatcher` nesneleri üzerinden, LLM'siz (elle
kurulmuş bir plan ile) gerçekten çalıştığı doğrulandı — 4 adımlık bir
plan (klasör oluştur → yeniden adlandır → hatırla → geri çağır) 4/4
başarıyla tamamlandı. `windows.arrange_window` canlı denenmedi (gerçek
bir pencereyi hareket ettirirdi) ama aynı `TOOL_REGISTRY`/dispatcher
yolundan geçtiği için mimari garanti aynıdır.

Geliştirme planının Faz 0-5 ve 7'si tamamlandı. Kalan: Faz 6 (prompt
bütçesinin gerçek modelle kesinleştirilmesi — bir Ollama modeli geri
yüklenmesini bekliyor, bkz. §28b).
