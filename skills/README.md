# skills/ klasörü — karar verildi: şimdilik boş kalıyor

> **Durum**: Bu not v0.1'de bir *açık soru* olarak yazılmıştı ("varsayım,
> onay bekliyor"). v1.0'da `core/planner.py` eklendikten sonra soru
> yeniden değerlendirildi ve **kapatıldı**. Aşağıda hem karar hem de
> kararı hangi koşulda geri açmak gerektiği yazılı.

## Orijinal varsayım (v0.1)

Spesifikasyonda hem `plugins/` hem `skills/` listelenmiş ama aralarındaki
fark tanımlanmamıştı. O zaman şu ayrım varsayılmıştı:

- **plugins/** → ham, tek-amaçlı tool'lar (`filesystem.open` gibi).
- **skills/** → birden fazla tool'u zincirleyen kompozit davranışlar
  ("GitHub'da repo araştır ve not olarak kaydet" gibi).

## Neden bu varsayım artık geçerli değil

v0.1'de bu ayrım mantıklıydı, çünkü tool'ları zincirleyecek **hiçbir
mekanizma yoktu**. v1.0'da `core/planner.py::TaskPlanner` eklendi ve
tam olarak bu işi yapıyor: LLM tek bir komuttan bir tool-call *listesi*
üretiyor, `TaskPlanner` bunu sırayla, hata/onay kontrolüyle yürütüyor.

Yani "kompozit davranış" ihtiyacı **zaten karşılandı** — üstelik daha
esnek biçimde, çünkü zinciri önceden birinin yazmış olması gerekmiyor;
LLM komuta göre üretiyor. `skills/` altına sabit zincirler yazmak, şu an
ikinci ve büyük ölçüde **gereksiz** bir yol açardı.

## Skills'in planner'a göre TEK gerçek avantajı

Bir tanesi var ve önemli: **adımlar arası veri aktarımı**. `TaskPlanner`
bunu bilinçli olarak desteklemiyor (bkz. `core/planner.py` modül
dokümantasyonundaki "BİLİNÇLİ SINIRLAMA") — her adımın argümanları LLM
tarafından baştan, tek seferde üretilir; 1. adımın çıktısı 2. adıma
argüman olarak geçemez.

Python'da yazılmış bir "skill" bunu yapabilirdi:

```python
# temsilî — henüz implemente EDİLMEDİ
sonuc = dispatcher.dispatch({"tool": "windows.screenshot", ...})
dispatcher.dispatch({"tool": "filesystem.copy",
                      "arguments": {"source": sonuc.data["path"], ...}})
```

## Karar

**Şimdilik bu klasör boş kalıyor; skills çerçevesi KURULMUYOR.** Gerekçe:

1. Somut bir ihtiyaç yok — bugüne kadar hiç kimse belirli bir kompozit
   davranış talep etmedi. Kullanıcısı olmayan bir soyutlama kurmak
   (speculative generality), projenin "gereksiz katman ekleme"
   ilkesine aykırı (bkz. `CLAUDE.md`).
2. Maliyeti düşük değil: bir skill'in başka tool'ları çağırabilmesi için
   `ToolContext`'e `dispatcher` alanı eklenmesi gerekir. Bu, projenin
   en merkezi sözleşmesini (her tool'un gördüğü nesne) değiştirmek ve
   `dispatcher.py` ↔ `tool_base.py` arasında dairesel import'u
   `TYPE_CHECKING` ile yönetmek demek. Karşılığında somut bir kazanç
   olmadan yapılacak bir değişiklik değil.
3. Veri aktarımı ihtiyacı ortaya çıktığında, bunu skills ile çözmek
   muhtemelen **yanlış yer** olur — çünkü sorun yalnızca önceden yazılmış
   zincirlerde değil, LLM'in ürettiği HER çok adımlı planda var. Doğru
   çözüm `TaskPlanner`'ın kendisine bir referans mekanizması eklemek
   olurdu (aşağıya bakın), skills'e değil.

## Bu kararı ne zaman geri açmalı

Aşağıdakilerden biri gerçekleşirse:

- **Tetikleyici A**: Belirli bir çok adımlı komut dizisini yerel modelin
  tekrar tekrar YANLIŞ ürettiği gözlemlenirse. O zaman o dizinin
  deterministik olarak (LLM'e her seferinde yeniden ürettirmeden) sabit
  bir skill olarak yazılması gerçek bir kazanç sağlar.
- **Tetikleyici B**: Adımlar arası veri aktarımı gereken komutlar
  yaygınlaşırsa ("ekran görüntüsü al ve şu klasöre taşı",
  "aradığın dosyayı aç"). Bu durumda **önce** `TaskPlanner`'a bir
  referans mekanizması (örn. bir argüman değerinin önceki adımın
  `result.data` alanına atıfta bulunabilmesi) eklemek değerlendirilmeli;
  skills yalnızca bu yetmezse.

Not: Tetikleyici B'nin çözümü, `core/planner.py`'de belgelenmiş bilinçli
bir sadeleştirmeyi geri almak anlamına gelir. O sadeleştirmenin gerekçesi
"küçük/yerel modeller adım-bağımlı akıl yürütmeyi güvenilir yapamayabilir"
idi; dolayısıyla bu değişiklik, kullanılan modelin bunu gerçekten
becerebildiği gözlemlendikten SONRA yapılmalı — varsayımla değil.
