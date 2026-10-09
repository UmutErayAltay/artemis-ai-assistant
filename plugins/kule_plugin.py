"""kule (kontrol kulesi) durum tool'u.

Umut'un diğer projesi olan "kule", onun git repolarını, cor/borsasite/
readbunny/vault durumlarını ve bakım bulgularını TEK bir FastAPI uç
noktasında (`GET /api/summary`) toplayan bir panel sunucusudur. Bu plugin
o uç noktayı okuyup sonucu Türkçe, SESLE OKUNABİLİR kısa bir özete
çevirir — yani "projelerimin durumu ne?" sorusu yazıyla da sesle de tek
bir tool çağrısıyla yanıtlanır.

Tasarım kararları ve gerekçeleri:

**`success` ne demek?** — Tool'ın KENDİ işinin başarısı. kule'ye ulaşıldı
ve yanıt çözüldüyse `success=True`'dir; bu, projelerin sağlıklı olduğu
anlamına GELMEZ. "cor erişilemiyor" bir bulgudur, tool hatası değildir:
mesaj bunu açıkça söyler. Kule'ye hiç ulaşılamıyorsa ya da yanıt okunamıyorsa
`success=False` döner. Tersi de geçerlidir: kule'nin kendisine ulaşmak
bir ARIZA DEĞİLDİR, bu yüzden bağlantı hatası istisna değil, temiz bir
`ToolResult(success=False, ...)`'dir (CLAUDE.md: "koşulsuz success=True
yasak" — ama aynı sebeple çökmek de doğru değil).

**Eksik alan = eksik bilgi, "sorun yok" değil.** — Kule'nin kendi ilkesi
"veri yoksa 'veri yok' de, 'sorun yok' deme"dir (bkz. kule/app/collectors/).
Bu plugin aynı ilkeyi korur: bir bölüm yanıtta hiç yoksa ya da beklenen
şekilde değilse, o bölüm "sorun yok" listesine değil, "bilgi gelmedi"
 listesine girer. Aksi halde Artemis, kule'nin toplayamadığı bir veriyi
"temiz" diye okurdu — en kötü çeşit yanlış rahatlatma.

**`topic` argümanı opsiyoneldir.** — Sesli kullanımda uzun bir özet
konuşmak yorucudur; "sadece cor'un durumunu söyle" denildiğinde tek
bölümü özetlemek hem daha hızlı hem daha doğru cevap verir. Boş
bırakılırsa (veya verilmezse) tüm bölümler özetlenir.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from core.enums import DangerLevel
from core.plugin_loader import register_tool
from core.tool_base import BaseTool, ToolContext
from models.tool_models import ToolResult

logger = logging.getLogger(__name__)

SUMMARY_PATH = "/api/summary"
"""Kule'nin özet uç noktası. Taban adres `Settings.kule.base_url`'den gelir."""

DEFAULT_BASE_URL = "http://127.0.0.1:8790"
"""Kule'nin varsayılan dinleme adresi (kule/app/cli.py::DEFAULT_PORT)."""

MAX_LISTED = 4
"""Bir cümlede en çok kaç repo/süreç adı sayılır.

Sesli okuma için: 12 repoluk bir liste cümleyi okunmaz bir duvar yapar.
Fazlası "+N tane daha" olarak özetlenir — sayı YİNE de söylenir, yani
bilgi gizlenmez, sadece kısaltılır.
"""

MAX_DETAIL_CHARS = 60
"""Hata metinlerinin cümle içindeki azami uzunluğu.

Kule'nin kendi notifier'ı da aynı sınırlı kısaltmayı yapar: bir httpx
hata metni 300+ karakter olabilir ve tek başına cümleyi çöker.
"""


def _shorten(value: object) -> str:
    """Herhangi bir değeri, tek satırlık ve kısa bir metne çevirir."""
    text = " ".join(str(value).split())
    if len(text) <= MAX_DETAIL_CHARS:
        return text
    return text[: MAX_DETAIL_CHARS - 1] + "…"


def _list_names(items: list[dict], key: str = "name") -> str:
    """Ad listesini konuşulabilir kısa bir dizeye çevirir."""
    names = [str(item.get(key) or "?") for item in items]
    shown = ", ".join(names[:MAX_LISTED])
    hidden = len(names) - MAX_LISTED
    if hidden > 0:
        shown += f", +{hidden} tane daha"
    return shown


def _format_age(timestamp: object) -> str | None:
    """ISO 8601 zaman damgasını "3 saat önce" gibi bir ifadeye çevirir.

    Ham ISO damgası sesli okumada karakter karakter ("2026-09-28T12:00:00")
    söylenir — kullanılamaz. Parse edilemeyen bir değer `None` döner; çağıran
    bunu "sana bir şey söyleyemedim" olarak kullanır, UMRUGUNU UYDURMAZ.
    """
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    try:
        moment = datetime.fromisoformat(timestamp.strip())
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.astimezone()  # naive zaman damgası için yerel saat varsayılır

    seconds = (datetime.now(moment.tzinfo) - moment).total_seconds()
    if seconds < 0:
        return "henüz"
    if seconds < 60:
        return "az önce"
    if seconds < 3600:
        return f"{round(seconds / 60)} dakika önce"
    if seconds < 86400:
        return f"{round(seconds / 3600)} saat önce"
    return f"{round(seconds / 86400)} gün önce"


def _missing(label: str) -> str:
    """Bölümün yanıtta olmadığını/okunamadığını bildiren cümle."""
    return f"{label} bilgisi kule'den gelmedi."


MISSING = "__missing__"
"""Bir bölümün okunabilir cümle üretemediğinin işareti.

`_build_summary` "bu bölüm hakkında bir şey biliyoruz" ile "bu bölümü
tanıyoruz ama veri yok" ayrımını yapmak zorunda. Bunu cümle METNİYLE
kıyaslamak (`produced == [_missing(label)]`) kırılgandı — etiket ya da
kelime değişse sessizce "her şey yolunda" gibi davranmaya başlardı.
Bölüm özetleyicileri bu tek değeri döndürdüğünde kararı isim yapar,
metne bakmaz.
"""

NO_FINDINGS = "Dikkat gerektiren bir bakım bulgusu yok."
"""Bakım bölümünün tamamen temiz olduğunun cümlesi.

Bunu ayrı bir sabit yapmak, "temiz" ile "hiç toplanmadı" ayrımının
kodda okunur kalmasını sağlıyor: bakım bölümü üç alt bölümün bulgu
listesidir ve HİÇBİRİ cümle üretmediğinde tek başına "hiçbir şey
okunamadı" demek, "okundu ve bir şey bulunamadı" ile aynı görünürdü.
"""


def _error_sentence(label: str, value: dict) -> str:
    """Bir bölümün kendi `error` alanından dürüst bir cümle kurar."""
    error = value.get("error")
    detail = f" ({_shorten(error)})" if error else ""
    return f"{label} hata veriyor{detail}."


def _service_problem(label: str, value: dict) -> str | None:
    """`cor`/`borsasite`/`readbunny` gibi servis bölümlerinin ortak durumu.

    Bu üç kaynak aynı deseni paylaşır: `reachable: bool` ve sorun varsa
    `error` (bkz. kule/app/collectors/). Sağlıklıysa `None` döner.
    """
    error = value.get("error")
    detail = f" ({_shorten(error)})" if error else ""
    if value.get("reachable") is False:
        return f"{label} erişilemiyor{detail}."
    if error:
        return f"{label} hata veriyor{detail}."
    return None


def _status_word(value: dict) -> str | None:
    """`health`/`dashboard_health` altındaki `status` metnini verir.

    Kule bu alt sözlükleri "şeklini varsaymadan" geçirir (bkz.
    `cor_status.py`), yani `status` alanı her zaman YOKTUR diye varsayılamaz
    — ama varsa söylemeye değer. Anahtar yoksa/şey değilse `None`.
    """
    for key in ("health", "dashboard_health"):
        health = value.get(key)
        if isinstance(health, dict):
            status = health.get("status")
            if isinstance(status, str) and status.strip():
                return _shorten(status)
    return None


def _summarize_git(value: object) -> list[str]:
    """`git`: repo listesi. Hata repo BAŞINADIR, listenin kendisinde değil.

    Kule'nin kendi notifier'ı da bu üç ayrı kaynak şeklini ayrı ayrı
    ele alır; burada da aynı ayrım yapılır.
    """
    if isinstance(value, dict):
        return [_error_sentence("git", value)]  # collector'ın kendi çöktüğü durum
    if not isinstance(value, list):
        return [MISSING]

    if not value:
        return ["git: taranmış hiç repo yok."]

    ok = [item for item in value if isinstance(item, dict) and not item.get("error")]
    failed = [item for item in value if isinstance(item, dict) and item.get("error")]

    sentences: list[str] = []
    dirty = [item for item in ok if isinstance(item.get("dirty_count"), int) and item["dirty_count"] > 0]
    if dirty:
        sentences.append(
            f"git: {len(dirty)} repoda commitlenmemiş değişiklik var: {_list_names(dirty)}."
        )
    if ok:
        clean = len(ok) - len(dirty)
        sentences.append(f"git: toplam {len(ok)} repo tarandı, {clean} tanesi temiz.")
    if failed:
        sentences.append(f"git: {len(failed)} repo okunamadı: {_list_names(failed)}.")
    return sentences


def _summarize_cor(value: object) -> list[str]:
    """`cor`: claude-openrouter proxy'sinin sağlık durumu."""
    if not isinstance(value, dict):
        return [MISSING]
    problem = _service_problem("cor", value)
    if problem:
        return [problem]
    if value.get("reachable") is not True:
        # `reachable` alanı yok: kule bu bölümü ya toplayamadı ya da beklenmeyen
        # bir şekilde döndürdü. "çalışıyor" demek yanlış rahatlatma olurdu.
        return [MISSING]
    status = _status_word(value)
    suffix = f", durum {status}" if status else ""
    return [f"cor çalışıyor{suffix}."]


def _summarize_borsasite(value: object) -> list[str]:
    """`borsasite`: HTTP sağlık + isteğe bağlı veritabanı sinyalleri."""
    if not isinstance(value, dict):
        return [MISSING]
    problem = _service_problem("borsasite", value)
    if problem:
        return [problem]
    if value.get("reachable") is not True:
        return [MISSING]

    sentences = ["borsasite çalışıyor."]

    db = value.get("db")
    if not isinstance(db, dict):
        # `db: None` → kule'nin config'inde `database_url` boş, yani bu
        # sinyal BİLEREK toplanmıyor. "Sorgu başarısız oldu" demek yanlış
        # olurdu; bilgi yoktur.
        sentences.append("borsasite veritabanı bilgisi yok.")
        return sentences

    last_trade = _format_age(db.get("last_trade_decision"))
    last_prediction = _format_age(db.get("last_prediction"))
    if last_trade:
        sentences.append(f"Son işlem kararı {last_trade}.")
    if last_prediction:
        sentences.append(f"Son tahmin {last_prediction}.")
    if not last_trade and not last_prediction:
        sentences.append("borsasite veritabanında son kayıt bulunamadı.")
    return sentences


def _summarize_readbunny(value: object) -> list[str]:
    """`readbunny`: bağlantı durumu + hata/pending sayıları."""
    if not isinstance(value, dict):
        return [MISSING]
    problem = _service_problem("readbunny", value)
    if problem:
        return [problem]
    if value.get("reachable") is not True:
        return [MISSING]

    sentences = ["readbunny veritabanı erişilebilir."]
    total = value.get("total_count")
    if isinstance(total, int):
        sentences.append(f"{total} bağlantı kayıtlı.")
    error_count = value.get("error_count")
    pending_count = value.get("pending_count")
    if isinstance(error_count, int) and error_count > 0:
        sentences.append(f"{error_count} tanesi hatalı.")
    if isinstance(pending_count, int) and pending_count > 0:
        sentences.append(f"{pending_count} tanesi bekliyor.")

    age = _format_age(value.get("last_updated"))
    if age:
        sentences.append(f"Son güncelleme {age}.")
    elif value.get("last_updated") is not None:
        # Değer var ama çözümlenemedi (beklenmeyen biçim): "veri yok" demek de
        # yanlış, "güncelleme yapılmamış" demek de. Ortası: bilinemiyor.
        sentences.append("Son güncelleme zamanı okunamadı.")
    return sentences


def _summarize_vault(value: object) -> list[str]:
    """`vault`: kırık wikilink, yetim not ve açık hikâye sayıları.

    Vault bölümünün `reachable` alanı YOKTUR (başarılıyken yalnızca
    sayılar döner), dolayısıyla varlık kontrolü alan adına göre yapılır.
    """
    if not isinstance(value, dict):
        return [MISSING]
    if value.get("error"):
        return [_error_sentence("vault", value)]

    known = ("broken_link_count", "orphan_note_count", "open_threads", "total_threads")
    if not any(key in value for key in known):
        return [MISSING]

    sentences = ["vault okundu."]
    broken = value.get("broken_link_count")
    if isinstance(broken, int):
        sentences.append(f"{broken} kırık bağlantı." if broken else "Kırık bağlantı yok.")
    orphans = value.get("orphan_note_count")
    if isinstance(orphans, int) and orphans:
        sentences.append(f"{orphans} bağlantısız not var.")
    open_threads = value.get("open_threads")
    total_threads = value.get("total_threads")
    if isinstance(open_threads, int):
        if isinstance(total_threads, int) and total_threads:
            sentences.append(f"{total_threads} hikâyeden {open_threads} tanesi açık.")
        else:
            sentences.append(f"{open_threads} açık hikâye.")
    return sentences


def _summarize_maintenance(value: object) -> list[str]:
    """`maintenance`: dolu disk, unutulmuş süreç, eski commitlenmemiş repo.

    Kule'de bu bir kaynak değil, üç alt bölümden oluşan bir bulgu
    listesi; bu yüzden her alt bölüm kendi cümlesini üretir (birleştirilmiş
    tek cümle okunması zor bir yığın olurdu — bkz. kule/app/notifier.py).

    ÖNEMLİ AYRIM: bulgusuz bir alt bölüm ("disk dolu değil", "unutulmuş
    süreç yok") ile HİÇ toplanmamış bir alt bölüm (`None`) aynı şey
    DEĞİLDİR. Kule'nin ilkesi gereği ikincisi de cümle üretir; yalnızca
    bulgusuz olan sessiz kalır, çünkü "temiz" söylemek asıl olarak bakım
    bölümünün varlık sebebidir.
    """
    if not isinstance(value, dict):
        return [MISSING]

    sentences: list[str] = []

    disk = value.get("disk")
    if isinstance(disk, dict):
        full = [item for item in (disk.get("full") or []) if isinstance(item, dict)]
        if full:
            worst = full[0]
            where = _shorten(worst.get("path") or "bilinmeyen yol")
            percent = worst.get("percent")
            detail = f", yüzde {percent}" if percent is not None else ""
            sentences.append(f"Disk dolu: {where}{detail}.")
            if len(full) > 1:
                sentences.append(f"Toplam {len(full)} disk dolu.")
        if disk.get("error"):
            sentences.append(f"Disk bilgisi alınamadı ({_shorten(disk['error'])}).")
    else:
        sentences.append(_missing("disk"))

    processes = value.get("stale_processes")
    if isinstance(processes, dict):
        items = [item for item in (processes.get("items") or []) if isinstance(item, dict)]
        if items:
            names: dict[str, int] = {}
            for item in items:
                name = str(item.get("name") or "?")
                names[name] = names.get(name, 0) + 1
            listed = ", ".join(
                f"{_shorten(name)} {count} tane" if count > 1 else _shorten(name)
                for name, count in list(names.items())[:MAX_LISTED]
            )
            sentences.append(f"Uzun süredir açık süreçler: {listed}.")
        if processes.get("error"):
            sentences.append(f"Süreç bilgisi alınamadı ({_shorten(processes['error'])}).")
    else:
        sentences.append(_missing("süreçler"))

    stale_git = value.get("stale_git")
    if isinstance(stale_git, dict):
        items = [item for item in (stale_git.get("items") or []) if isinstance(item, dict)]
        if items:
            sentences.append(f"Eski commitlenmemiş değişiklik: {_list_names(items)}.")
        if stale_git.get("error"):
            sentences.append(f"Repo yaş kontrolü hata verdi ({_shorten(stale_git['error'])}).")
    else:
        sentences.append(_missing("eski repo kontrolü"))

    # Üç alt bölüm de okundu ve HİÇBİRİ bulgu ya da hata üretmedi: bakımın
    # asıl sıfır durumu. Sessizce hiçbir şey dememek, "temiz" ile
    # "bilmiyorum"u sesle ayırt edilemez hale getirirdi.
    all_read = all(
        isinstance(value.get(key), dict) for key in ("disk", "stale_processes", "stale_git")
    )
    if all_read and not sentences:
        return [NO_FINDINGS]

    return sentences


# Bölüm adı -> (konuşmada kullanılan etiket, özetleyici). `topic` argümanının
# enum değerleri ve "hepsi" sırası bu tek sözlükten türer; iki yer ayrı
# tutulsaydı biri güncellenip diğeri eskirdi.
_SUMMARIZERS: dict[str, tuple[str, Callable[[object], list[str]]]] = {
    "git": ("git", _summarize_git),
    "cor": ("cor", _summarize_cor),
    "borsasite": ("borsasite", _summarize_borsasite),
    "readbunny": ("readbunny", _summarize_readbunny),
    "vault": ("vault", _summarize_vault),
    "maintenance": ("bakım", _summarize_maintenance),
}

TOPIC_ANY = ""
"""`topic` argümanında "hepsi" anlamına gelen değer."""


def _fetch_summary(base_url: str, timeout_seconds: float) -> tuple[dict | None, str | None]:
    """Kule'nin `/api/summary` uç noktasını okur.

    Returns:
        (özet, hata). Biri diğerinin tersidir: başarılıysa hata `None`,
        başarısızsa özet `None` ve kullanıcıya gösterilecek Türkçe mesaj
        doludur. HİÇBİR koşulda raise etmez — kule'nin kapalı olması bir
        uygulama arızası değil, dürüstçe raporlanan bir durumdur.
    """
    import requests  # lazy import: ağ modülü yalnızca gerçekten istek atılırken yüklenir

    url = f"{base_url.rstrip('/')}{SUMMARY_PATH}"
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.exceptions.Timeout:
        return None, (
            f"Kule {timeout_seconds:g} saniye içinde yanıt vermedi — "
            "sunucu takılmış ya da çok yavaş."
        )
    except requests.exceptions.ConnectionError:
        return None, (
            f"Kule'ye bağlanılamadı ({base_url}). Sunucu kapalı görünüyor; "
            "başlatmak için kule klasöründe `kule` komutunu çalıştır."
        )
    except requests.exceptions.RequestException as exc:
        return None, f"Kule'ye ulaşılamadı: {_shorten(exc)}."

    if response.status_code != 200:
        # Kule config eksikken 500 döner ve gövdede ne yapılacağını söyler
        # (bkz. kule/app/main.py::CONFIG_HELP) — bu, kullanıcıya aksiyon
        # verebildiği için ham HTTP kodundan çok daha değerli.
        detail = _shorten(_error_detail(response))
        suffix = f": {detail}" if detail else ""
        return None, f"Kule {response.status_code} döndü{suffix}."

    try:
        payload = response.json()
    except ValueError:
        return None, "Kule'den gelen yanıt okunamadı (geçersiz JSON)."

    if not isinstance(payload, dict):
        return None, "Kule'den beklenmeyen bir biçimde yanıt geldi."

    return payload, None


def _error_detail(response: Any) -> str:
    """Hata gövdesinden okunabilir bir açıklama çıkarır.

    Gövde JSON değilse ham metin kullanılır; ikisi de alınamazsa boş
    dize döner (çağıran bunu parantez üretmemek için kullanır).
    """
    try:
        body = response.json()
    except ValueError:
        body = getattr(response, "text", "")

    if isinstance(body, dict):
        parts = [str(part) for part in (body.get("detail"), body.get("error")) if part]
        return ": ".join(parts)
    return str(body) if body else ""


@register_tool
class KuleStatusTool(BaseTool):
    """kule panelinin topladığı proje durumunu Türkçe özet olarak okur.

    Kule (Umut'un diğer projesi) git repolarını, cor/borsasite/readbunny/
    vault durumlarını ve bakım bulgularını tek uç noktada toplar; bu tool
    o özeti okunabilir bir paragrafa çevirir. Hiçbir şey DEĞİŞTİRMEZ —
    salt okumadır, onay gerektirmez.
    """

    name = "kule.status"
    description = (
        "Kule panelinin (projelerin durumunu toplayan sunucu) raporladığı durumu "
        "özetler: git repolarında commitlenmemiş değişiklikler, cor/borsasite/"
        "readbunny/vault sağlığı, dolu disk ve unutulmuş süreçler. "
        "Kullanıcı projelerinin, servislerinin veya genel durumunun ne "
        "olduğunu sorduğunda kullanılır."
    )
    danger_level = DangerLevel.SAFE

    def get_arguments_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "Yalnızca tek bir bölümün özetini isteyen kullanıcı sorusu "
                        "varsa doldurulur; genel bir durum sorusunda BOŞ BIRAKILIR. "
                        "Kule çalışmıyorsa bu tool 'çalışmıyor' der."
                    ),
                    # `""` "hepsi" demektir ve enum'da BULUNMAZSA dispatcher
                    # (grammar düşse bile) LLM'in dürüstçe boş string
                    # gönderdiği zararsız isteği reddedip hata döndürürdü.
                    "enum": [*sorted(_SUMMARIZERS), TOPIC_ANY],
                    "default": TOPIC_ANY,
                }
            },
            "required": [],
        }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        base_url = context.settings.kule.base_url
        timeout_seconds = context.settings.kule.timeout_seconds

        context.logger.info("kule durumu soruluyor: %s (topic=%r)", base_url, arguments.get("topic", ""))

        payload, error = _fetch_summary(base_url, timeout_seconds)
        if error is not None:
            return ToolResult(success=False, message=error, data={"base_url": base_url})

        sentences, unavailable = _build_summary(payload or {}, arguments.get("topic") or TOPIC_ANY)
        if not sentences and not unavailable:
            sentences = ["Kule'den okunabilir bir durum bilgisi gelmedi."]

        parts = list(sentences)
        if unavailable:
            # Eksik bölümler sona, TEK cümlede toplanır: her birini ayrı
            # cümle yapmak özeti uzatır, ama birleştirmek de sayıyı gizlemez.
            parts.append("Şu bilgiler kule'den gelmedi: " + ", ".join(unavailable) + ".")

        return ToolResult(
            success=True,
            message=" ".join(parts),
            data={
                "sentences": sentences,
                "unavailable": unavailable,
                "collected_at": (payload or {}).get("collected_at"),
            },
        )


def _build_summary(summary: dict, topic: str) -> tuple[list[str], list[str]]:
    """Özeti Türkçe cümlelere çevirir.

    Returns:
        (cümleler, gelmeyen_bölüm_etiketleri). Bir bölüm ya cümle üretir
        ya da "gelmedi" listesine girer — ikisini birden YAPMAZ; böylece
        "biliyorum ve sorun yok" ile "bilmiyorum" cümleleri karışmaz.
    """
    wanted = [topic] if topic and topic in _SUMMARIZERS else list(_SUMMARIZERS)

    sentences: list[str] = []
    unavailable: list[str] = []
    for key in wanted:
        label, summarize = _SUMMARIZERS[key]
        if key not in summary:
            unavailable.append(label)
            continue
        produced = summarize(summary[key])
        if not produced or produced == [MISSING]:
            # Bölüm var ama içinden okunabilir bir cümle çıkmadı: bu da
            # "bilgi gelmedi"dir, "sorun yok" değil.
            unavailable.append(label)
            continue
        sentences.extend(produced)

    return sentences, unavailable
