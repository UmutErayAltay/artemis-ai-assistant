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
