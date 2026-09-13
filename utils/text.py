"""Türkçe metin normalleştirme ve onay cevabı yorumlama.

NEDEN AYRI BİR MODÜL: Python'ın `str.lower()`'ı Türkçe yerel ayarını
KULLANMAZ ve bu, bu projede iki ayrı yerde gerçek arızaya yol açtı.
Sorun tek bir harfte: Türkçede büyük `I`'nın küçüğü noktasız `ı`,
büyük `İ`'nin küçüğü noktalı `i`'dir. Python ikisini de İngilizce
kurallarına göre çevirir::

    "ÇIKIŞ".lower()  ->  "çikiş"   (beklenen: "çıkış")
    "HAYIR".lower()  ->  "hayir"   (beklenen: "hayır")

Sonuç, kullanıcının BÜYÜK HARFLE yazdığı ya da konuşma tanıyıcının
büyük harfle döndürdüğü sözcüklerin sabit listelerle eşleşmemesidir —
ve iki kullanım yeri de bir KARAR noktası: sohbetten çıkış ve geri
alınamaz bir işlemin onayı.

`is_clear_affirmative_answer` de BİLEREK burada: onun Türkçe olumsuzluk-
eki sezgiseli `core/voice_loop.py` için yazılmıştı, ama aynı güvenlik
muhakemesi `core/conversation_loop.py::_confirm_with_user`'da (metin
modu) HİÇ yoktu — kullanıcı "hayır tamam" yazdığında yalnızca düz
`.lower()` + sabit küme eşleşmesi kullanılıyordu, ses yolunun eklediği
olumsuzluk vetosu metin yolunda hiç yoktu. Fonksiyon paylaşılan bir
altyapıya taşınınca iki yol da AYNI güvenlik kapısından geçer.
"""

from __future__ import annotations

import re

_TURKISH_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})


def turkish_lower(text: str) -> str:
    """Metni Türkçe kurallarına göre küçük harfe çevirir.

    Önce `I`/`İ` elle eşlenir, sonra kalan her şey için standart
    `str.lower()` uygulanır (diğer harflerde Python zaten doğru:
    `Ç->ç`, `Ş->ş`, `Ğ->ğ`, `Ö->ö`, `Ü->ü`).

    Args:
        text: Ham metin.

    Returns:
        Türkçe kurallarına göre küçültülmüş metin.
    """

    return text.translate(_TURKISH_LOWER_MAP).lower()


def lower_variants(text: str) -> set[str]:
    """Metnin hem Türkçe hem İngilizce kurallarına göre küçük hâlini verir.

    NEDEN İKİSİ BİRDEN: bu projede karşılaştırılan sabit listeler her iki
    dilden sözcük içeriyor (`{"çıkış", "cikis", "exit", "quit"}`). Tek bir
    kural seçmek diğerini bozar::

        turkish_lower("EXIT")  -> "exıt"   (İngilizce sözcük bozuldu)
        str.lower("ÇIKIŞ")     -> "çikiş"  (Türkçe sözcük bozuldu)

    Girdinin hangi dilde yazıldığını önceden bilemeyiz; bu yüzden ikisi de
    denenir. Yanlış eşleşme riski yok: iki çeviri yalnızca `I`/`İ`
    harflerinde ayrışıyor.

    Args:
        text: Ham metin.

    Returns:
        En fazla iki elemanlı bir küme; üyelik testi için kullanılır.
    """

    return {text.lower(), turkish_lower(text)}


AFFIRMATIVE_WORDS = frozenset(
    {"evet", "onayla", "onaylıyorum", "onayliyorum", "tamam", "olur", "kabul"}
)
"""Onayda kabul edilen sözcükler. Bunun DIŞINDAKİ her şey RED sayılır.

`"yap"` ve `"devam"` BİLEREK ÇIKARILDI: ikisi de tek başına net bir onay
değil ve olumsuz çekimlerinin (`"yapma"`, `"devam etme"`) kökü aynı olduğu
için ayırt edilmeleri kırılgan. Bir onay sözcüğü, YANLIŞ duyulduğunda/
okunduğunda geri alınamaz bir işlemi çalıştıracak kadar net olmalıdır.
"""

_NEGATIVE_WORDS = frozenset(
    {
        "hayır",
        "hayir",
        "yapma",
        "etme",
        "iptal",
        "dur",
        "durdur",
        "vazgeç",
        "vazgectim",
        "vazgeçtim",
        "olmaz",
        "istemiyorum",
        "onaylamıyorum",
        "onaylamiyorum",
        "hayır!",
    }
)
"""Duyulduğunda/okunduğunda onayı KOŞULSUZ reddeden sözcükler.

Türkçe eklemeli bir dildir ve olumsuzluk bir EKTİR: `"yapma"` sözcüğü
`"yap"`ı, `"onaylamıyorum"` `"onayla"`yı, `"tamamen"` `"tamam"`ı ALT DİZE
olarak içerir. Bu yüzden onay kontrolü alt dize araması OLAMAZ (bir dönem
öyleydi ve *"hayır yapma"* cevabı işlemi ÇALIŞTIRIYORDU — bkz. README §35).

Ayrıca `"hayir"`/`"vazgectim"` gibi noktasız yazımlar da listede: Whisper
Türkçe çıktısında noktalama/aksan tutarlı değildir, ve `str.lower()` Türkçe
yerel ayarı kullanmaz (`"HAYIR".lower()` -> `"hayir"`).
"""

_WORD_SPLIT_RE = re.compile(r"[^\wçğıöşüÇĞİÖŞÜ]+", re.UNICODE)
"""Onay cevabını sözcüklere ayırır; noktalama ayırıcı sayılır, Türkçe
harfler sözcüğün parçası kalır."""

_NEGATION_SUFFIX_RE = re.compile(
    r"(m[ıiuü]yor|m[ae]z|m[ae]yece|m[ae]m$|[a-zçğıöşü]{2,}m[ae]$)",
    re.UNICODE,
)
"""Türkçe olumsuzluk EKİNİ sözcük sonunda yakalar.

`_NEGATIVE_WORDS` sabit bir liste; ama Türkçe eklemeli bir dil olduğu için
olumsuzluk sonsuz sayıda çekim üretir ve hepsini listelemek mümkün DEĞİL::

    "kabul etmiyorum"   -> etmiyorum   (m + ıyor)
    "onaylamıyorum"     -> onaylamıyorum
    "yapmam"            -> yapmam      (m + am)
    "olmaz"             -> olmaz       (m + az)
    "yapmayacağım"      -> yapmayaca...(m + ayaca)
    "yapma"             -> yapma       (kök + ma)

Son dal (`[a-zçğıöşü]{2,}m[ae]$`) en az iki harflik bir kökten sonra gelen
`-ma`/`-me` emir olumsuzunu yakalar (`"yapma"`, `"etme"`, `"silme"`).

Bu SEZGİSEL bir kural, tam bir çekim çözümleyici değil; bu yüzden
`AFFIRMATIVE_WORDS`'teki sözcükler bu testten MUAF tutulur (bkz.
`is_clear_affirmative_answer`). Aksi halde `"tamam"` sözcüğü `m[ae]m$`
dalına takılır ("ta-**mam**") ve geçerli bir onay reddedilirdi.

YANLIŞ POZİTİF BİLİNÇLİ OLARAK TERCİH EDİLİR: bu kapı asimetriktir
(`.context` §6.5). Yanlışlıkla reddedilen bir onay kullanıcıya yalnızca
"tekrar söyle" dedirtir; yanlışlıkla kabul edilen bir onay geri alınamaz
bir işlemi çalıştırır.
"""


def is_clear_affirmative_answer(answer: str) -> bool:
    """Bir onay cevabının NET bir onay olup olmadığına karar verir.

    Kural asimetriktir ve bu bilinçlidir (bkz. `.context` §6.5): onay
    istenen tool'lar geri alınamaz işlemler yapar ve hem ses tanıma hem
    serbest metin girdisi hata/belirsizlik içerebilir. Bu yüzden
    **yalnızca net bir onay duyulursa/okunursa** True döner; sessizlik,
    anlaşılmayan cevap, boş dize — hepsi False'tur.

    İKİ KATMAN, BU SIRAYLA:

    1. **Olumsuzluk vetosu**: cevapta `_NEGATIVE_WORDS`'ten bir sözcük
       varsa, başka ne varsa olsun RED. *"Hayır, tamam boş ver"* gibi bir
       cevapta hem olumsuz hem olumlu sözcük geçer; böyle bir belirsizlikte
       güvenli taraf durmaktır.
    2. **Sözcük seviyesinde** (ALT DİZE DEĞİL) olumlu eşleşme.

    Alt dize araması NEDEN OLMAZ: Türkçede olumsuzluk bir ektir, yani
    olumlu kök olumsuz sözcüğün İÇİNDE geçer::

        "yapma"        icerir "yap"
        "onaylamıyorum" icerir "onayla"
        "tamamen"      icerir "tamam"
        "devam etme"   icerir "devam"

    Bu fonksiyon `core/voice_loop.py` için yazılmıştı (`_is_affirmative`
    olarak); `core/conversation_loop.py::_confirm_with_user` (metin modu)
    ise yalnızca düz `.lower()` + sabit küme eşleşmesi kullanıyordu — aynı
    güvenlik muhakemesi bir yolda vardı, diğerinde yoktu. Paylaşılan bu
    fonksiyona taşınınca ikisi de aynı kapıdan geçiyor.

    Args:
        answer: Ham cevap metni (duyulamadıysa/girilmediyse boş dize).
            Küçültme burada, `turkish_lower()` ile yapılır — çağıran
            taraf KENDİSİ küçültmemeli (bir dönem `core/voice_loop.py`
            burada ÇİFTE küçültme yapıyordu: `str.lower()` sonra
            `turkish_lower()` — `"İ".lower()` -> `"i" + BİRLEŞTİRİCİ
            NOKTA (U+0307)` ürettiği için `turkish_lower()` bunu bir
            daha düzeltemiyordu).

    Returns:
        Yalnızca net bir onay duyulduysa/okunduysa True.
    """

    words = {word for word in _WORD_SPLIT_RE.split(turkish_lower(answer.strip())) if word}
    if not words:
        return False
    if words & _NEGATIVE_WORDS:
        return False
    # Sezgisel ek testi yalnızca TANIMADIĞIMIZ sözcüklere uygulanır:
    # `"tamam"` sözcüğü `m[ae]m$` dalına takılır ("ta-mam"), oysa geçerli
    # bir onaydır. Muafiyet güvenliği zayıflatmaz — muaf tutulan küme,
    # zaten olumsuzluk içermeyen 7 sabit sözcüktür.
    unknown_words = words - AFFIRMATIVE_WORDS
    if any(_NEGATION_SUFFIX_RE.search(word) for word in unknown_words):
        return False
    return bool(words & AFFIRMATIVE_WORDS)
