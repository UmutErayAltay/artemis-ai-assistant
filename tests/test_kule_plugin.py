"""`plugins/kule_plugin.py` testleri.

CLAUDE.md'nin "gerçek dosya sistemi işlemleri için mock KULLANMA" kuralı
burada HTTP için de uygulanır: testler `http.server`'ı KENDİSİ başlatıp
GERÇEK bir soket üzerinden gerçek `requests` çağrıları yapılır. Böylece
URL birleştirme, gerçek HTTP durum kodları ve JSON ayrıştırma birlikte
sınanır; sahte bir `requests.get` yalnızca "hangi URL çağrıldı" sorusunu
yanıtlardı, sunucunun 500 döndüğünde ne olduğunu yanıtlamazdı.

Yalnızca iki şey mock'lanır ve ikisi de meşru: kule sunucusu YOKKEN
"bağlantı reddedildi" davranışını sınamak için boş bir port (deterministik
çünkü gerçekten dinleyen bir süreç yoktur) ve yanıtı bilerek bozuk
göndermek için gövde metni.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from config.settings import Settings
from core.dispatcher import ToolDispatcher
from memory.context_memory import ContextMemory

# Kule'nin gerçek `/api/summary` çıktısının kırılmamış bir örneği.
# Şekiller `kule/app/collectors/*.py` ve `kule/app/aggregator.py`'den
# birebir alınmıştır (bkz. modül dokümantasyonu).
FULL_SUMMARY: dict[str, Any] = {
    "git": [
        {
            "name": "artemis-ai-assistant",
            "path": "/home/user/artemis-ai-assistant",
            "branch": "main",
            "dirty_count": 3,
            "son_commit": "2 hours ago",
        },
        {
            "name": "kule",
            "path": "/home/user/kule",
            "branch": "main",
            "dirty_count": 0,
            "son_commit": "5 minutes ago",
        },
        {
            "name": "bozuk-repo",
            "path": "/home/user/bozuk",
            "error": "git bulunamadı",
        },
    ],
    "cor": {
        "reachable": True,
        "health": {"status": "ok"},
        "dashboard_health": {"status": "degraded"},
        "metrics": {"m": 1},
    },
    "borsasite": {
        "reachable": True,
        "health": {"status": "ok"},
        "db": {
            "last_trade_decision": "2026-09-28T09:00:00+03:00",
            "last_prediction": "2026-09-28T11:30:00+03:00",
        },
    },
    "readbunny": {
        "reachable": True,
        "last_updated": "2026-09-28T11:00:00+03:00",
        "error_count": 2,
        "pending_count": 0,
        "total_count": 145,
    },
    "vault": {
        "broken_link_count": 4,
        "orphan_note_count": 7,
        "open_threads": 3,
        "total_threads": 12,
    },
    "maintenance": {
        "disk": {
            "threshold_percent": 90.0,
            "full": [{"path": "/", "percent": 76.0, "free_gb": 12.3}],
        },
        "stale_processes": {
            "min_hours": 6.0,
            "names": ["ollama"],
            "items": [{"pid": 1, "name": "ollama", "hours": 30.5}],
        },
        "stale_git": {
            "min_days": 3.0,
            "items": [{"name": "kule", "dirty_count": 5, "age_days": 9.2}],
        },
    },
    "collected_at": 1759051200.0,
}


class _KuleHandler(BaseHTTPRequestHandler):
    """`/api/summary` dönen tek uç noktalı sahte kule sunucusu.

    `payload`/`status` sınıf düzeyinde ayarlanır: `ThreadingHTTPServer`
    her isteği kendi iş parçacığında servis eder, bu yüzden senkronizasyon
    gerekmez.
    """

    payload: object = FULL_SUMMARY
    status: int = 200
    raw_body: str | None = None
    delay_seconds: float = 0.0

    def do_GET(self) -> None:  # BaseHTTPRequestHandler'ın zorunlu adlandırması
        if self.path != "/api/summary":
            self.send_response(404)
            self.end_headers()
            return

        if self.delay_seconds:
            import time

            time.sleep(self.delay_seconds)

        if self.raw_body is not None:
            body = self.raw_body.encode("utf-8")
        else:
            body = json.dumps(self.payload).encode("utf-8")

        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Test çıktısını kirletmemek için erişim loglarını susturur."""


@pytest.fixture
def kule_server() -> Iterator[str]:
    """Gerçek bir yerel HTTP sunucusu başlatır, taban adresini döndürür."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KuleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_handler_state() -> Iterator[None]:
    """Sınıf düzeyindeki alanları her testten önce sıfırlar.

    Bunlar `ThreadingHTTPServer` iş parçacıkları arasında PAYLAŞILIR; sıfır
    olmazsa "bozuk JSON" yazan bir test, sonraki testin sunucusunu da
    bozuk yapardı (testler birbirinin durumunu kirletirdi).
    """
    _KuleHandler.payload = FULL_SUMMARY
    _KuleHandler.status = 200
    _KuleHandler.raw_body = None
    _KuleHandler.delay_seconds = 0.0
    yield


@pytest.fixture
def dispatcher_factory(tmp_path: Path):
    """Verilen kule adresine bakan bir dispatcher üreten fabrika."""

    def _make(base_url: str, timeout_seconds: float = 5.0) -> ToolDispatcher:
        settings = Settings(
            desktop_path=tmp_path,
            db_path=tmp_path / "memory.db",
            log_dir=tmp_path / "logs",
            kule={"base_url": base_url, "timeout_seconds": timeout_seconds},
        )
        return ToolDispatcher(settings=settings, memory=ContextMemory(settings.db_path))

    return _make


@pytest.fixture
def dispatcher(dispatcher_factory, kule_server: str) -> ToolDispatcher:
    return dispatcher_factory(kule_server)


# --- kayıt ve sözleşme ------------------------------------------------------


def test_tool_is_registered_under_expected_name() -> None:
    from core.plugin_loader import TOOL_REGISTRY
    from plugins.kule_plugin import KuleStatusTool

    assert TOOL_REGISTRY["kule.status"] is KuleStatusTool


def test_tool_is_safe_so_it_never_asks_for_confirmation() -> None:
    from core.enums import DangerLevel
    from plugins.kule_plugin import KuleStatusTool

    assert KuleStatusTool.danger_level is DangerLevel.SAFE


def test_manifest_advertises_the_tool_automatically() -> None:
    """`core/manifest.py` registry'den otomatik üretir; sistem promptuna
    elle hiçbir şey eklenmemelidir (CLAUDE.md)."""
    from core.manifest import build_tool_manifest_json

    manifest = json.loads(build_tool_manifest_json())
    entry = next(item for item in manifest if item["name"] == "kule.status")

    assert "kule" in entry["description"].lower()
    assert "topic" in entry["arguments_schema"]["properties"]


def test_topic_argument_is_optional(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is True


# --- başarılı yanıt ---------------------------------------------------------


def test_full_summary_is_summarized_in_turkish(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is True
    message = result.message
    assert "1 repoda commitlenmemiş değişiklik" in message
    assert "artemis-ai-assistant" in message
    assert "cor çalışıyor" in message
    assert "145 bağlantı kayıtlı" in message
    assert "2 tanesi hatalı" in message
    assert "4 kırık bağlantı" in message
    assert "Disk dolu" in message
    assert "Eski commitlenmemiş değişiklik" in message


def test_unreachable_source_is_reported_as_a_problem_not_as_success_quietly(
    dispatcher: ToolDispatcher, kule_server: str
) -> None:
    """`reachable: False` bir tool hatası değil, bir BULGUDUR: success=True
    olmalı ama mesaj sorunu açıkça söylemelidir."""
    _KuleHandler.payload = {**FULL_SUMMARY, "cor": {"reachable": False, "error": "kapalı"}}

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is True
    assert "cor erişilemiyor" in result.message
    assert "kapalı" in result.message


def test_topic_narrows_the_summary_to_a_single_section(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {"topic": "cor"}})

    assert result.success is True
    assert "cor çalışıyor" in result.message
    # Yalnızca istenen bölüm konuşulur: diğerlerinin varlığı bile cümle değil.
    assert "commitlenmemiş" not in result.message
    assert "Disk dolu" not in result.message
    assert result.data["unavailable"] == []


def test_summary_counts_are_spoken_as_turkish_ages(dispatcher: ToolDispatcher) -> None:
    _KuleHandler.payload = {
        **FULL_SUMMARY,
        "borsasite": {
            "reachable": True,
            "db": {
                "last_trade_decision": "2026-09-27T09:00:00+03:00",
                "last_prediction": None,
            },
        },
    }

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {"topic": "borsasite"}})

    assert "gün önce" in result.message
    # Ham ISO damgası sesli okumada karakter karakter söylenir — olmamalı.
    assert "2026-09-27" not in result.message


def test_empty_git_list_says_so_instead_of_implying_clean_repos(
    dispatcher: ToolDispatcher,
) -> None:
    """Boş repo listesi "tüm repolar temiz" DEĞİLDİR — hiç repo görülmedi.
    Kule'nin "veri yoksa 'veri yok' de" ilkesinin aynısı."""
    _KuleHandler.payload = {**FULL_SUMMARY, "git": []}

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {"topic": "git"}})

    assert "taranmış hiç repo yok" in result.message
    assert "temiz" not in result.message


# --- eksik / kısmi alanlar --------------------------------------------------


def test_absent_section_is_reported_as_missing_not_as_healthy(
    dispatcher: ToolDispatcher,
) -> None:
    """Kule'de `readbunny` anahtarı hiç yoksa, sessizce atlamak "sorun yok"
    gibi okunurdu. Bunun yerine AÇIKÇA "gelmedi" denir."""
    summary = {key: value for key, value in FULL_SUMMARY.items() if key != "readbunny"}
    _KuleHandler.payload = summary

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is True
    assert "readbunny" in result.data["unavailable"]
    assert "Şu bilgiler kule'den gelmedi" in result.message


def test_section_with_unexpected_shape_is_treated_as_missing(
    dispatcher: ToolDispatcher,
) -> None:
    """`vault` yerine geçersiz tip gelirse bu "vault'ta sorun yok" demek
    değildir; okunamadığı için "gelmedi" denir."""
    _KuleHandler.payload = {**FULL_SUMMARY, "vault": "bilinmiyor"}

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert "vault" in result.data["unavailable"]


def test_vault_error_is_surfaced_verbatim(dispatcher: ToolDispatcher) -> None:
    _KuleHandler.payload = {**FULL_SUMMARY, "vault": {"error": "vault yolu bulunamadı: /x"}}

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert "vault hata veriyor" in result.message
    assert "vault yolu bulunamadı" in result.message


def test_empty_summary_reports_nothing_was_readable(dispatcher: ToolDispatcher) -> None:
    _KuleHandler.payload = {}

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is True
    assert "gelmedi" in result.message


def test_partial_maintenance_names_the_missing_subsection(
    dispatcher: ToolDispatcher,
) -> None:
    """`maintenance` sözlüğü var ama alt bölümlerden biri eksik: hangisinin
    eksik olduğu AÇIKÇA adlandırılır, "bakım temiz" diye toplu geçilmez."""
    _KuleHandler.payload = {
        **FULL_SUMMARY,
        "maintenance": {
            "disk": {"full": [], "threshold_percent": 90.0},
            "stale_processes": None,
        },
    }

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {"topic": "maintenance"}})

    assert result.success is True
    assert "süreçler bilgisi kule'den gelmedi" in result.message
    assert "eski repo kontrolü bilgisi kule'den gelmedi" in result.message
    # Toplu "bakım temiz" ifadesi YANLIŞ olurdu: iki alt bölüm hiç okunmadı.
    assert "bakım bulgusu yok" not in result.message


def test_fully_clean_maintenance_says_so_explicitly(dispatcher: ToolDispatcher) -> None:
    """Üç alt bölüm de okunup hiç bulgu çıkmadığında "sorun yok" DENMELİ —
    aksi halde "temiz" ile "bilinmiyor" sesle ayırt edilemez."""
    _KuleHandler.payload = {
        **FULL_SUMMARY,
        "maintenance": {
            "disk": {"full": [], "threshold_percent": 90.0},
            "stale_processes": {"items": []},
            "stale_git": {"items": []},
        },
    }

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {"topic": "maintenance"}})

    assert "bakım bulgusu yok" in result.message
    assert result.data["unavailable"] == []


# --- kule'ye ulaşılamaz -----------------------------------------------------


def test_connection_refused_reports_kule_as_down(dispatcher_factory, tmp_path: Path) -> None:
    # Port 1'e bağlanma denemesi: hiçbir süreç dinlemiyor, yani hata
    # DETERMİNİSTIK olarak "bağlantı reddedildi" olur (kule'yi gerçekten
    # kapatmaya gerek yok, testler birbirine bağımlı olmaz).
    result = dispatcher_factory("http://127.0.0.1:1").dispatch(
        {"tool": "kule.status", "arguments": {}}
    )

    assert result.success is False
    assert "bağlanılamadı" in result.message
    assert "kule" in result.message.lower()


def test_timeout_is_reported_as_a_hang_not_as_a_crash(
    dispatcher_factory, kule_server: str
) -> None:
    _KuleHandler.delay_seconds = 2.0

    result = dispatcher_factory(kule_server, timeout_seconds=0.2).dispatch(
        {"tool": "kule.status", "arguments": {}}
    )

    assert result.success is False
    assert "yanıt vermedi" in result.message


def test_server_error_includes_kules_own_explanation(
    dispatcher: ToolDispatcher,
) -> None:
    """Kule config eksikken 500 döner ve gövdede ne yapılacağını söyler;
    ham HTTP kodundan çok daha kullanışlıdır, o yüzden mesaja eklenir."""
    _KuleHandler.status = 500
    _KuleHandler.payload = {
        "error": "config.yaml.example'ı config.yaml olarak kopyalayıp doldurun",
        "detail": "config.yaml bulunamadı",
    }

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is False
    assert "500" in result.message
    assert "bulunamadı" in result.message


def test_malformed_json_is_reported_as_unreadable(dispatcher: ToolDispatcher) -> None:
    _KuleHandler.raw_body = "{bu json değil"

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is False
    assert "okunamadı" in result.message


def test_non_object_json_is_reported_as_unexpected(dispatcher: ToolDispatcher) -> None:
    _KuleHandler.raw_body = "[1, 2, 3]"

    result = dispatcher.dispatch({"tool": "kule.status", "arguments": {}})

    assert result.success is False
    assert "beklenmeyen" in result.message


def test_unknown_topic_is_rejected_by_the_dispatcher_before_execution(
    dispatcher: ToolDispatcher,
) -> None:
    """Geçersiz enum dispatcher'da reddedilir (istisna DEĞİL, temiz bir
    `success=False` döner) ve tool'un execute()'ına hiç ulaşılmaz."""
    _KuleHandler.delay_seconds = 0.0

    result = dispatcher.dispatch(
        {"tool": "kule.status", "arguments": {"topic": "havadurumu"}}
    )

    assert result.success is False
    assert "topic" in result.message
    assert "havadurumu" in result.message
