"""`projects/notify.py` testleri: gerçek yerel HTTP sunucusu, ağa çıkış YOK.

Telegram kanalı, `127.0.0.1` üzerinde bu dosyada açılan gerçek bir `ThreadingHTTPServer`'a
konuşur (`api_base` ile yönlendirilir; `api.telegram.org`'a hiçbir test dokunmaz). Masaüstü
kanalı ise HİÇBİR testte gerçek `subprocess.run` ile çağrılmaz: Windows geliştirici
makinesinde gerçek bir toast gösterirdi. Onun yerine `runner` olarak kayıt tutan sahte bir
çalıştırıcı verilir; betiğin içeriği ve alt sürece verilen ortam gerçek `base64` /
`-EncodedCommand` biçimiyle doğrulanır.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from projects import notify
from projects.notify import (
    TELEGRAM_TEXT_LIMIT,
    TOAST_BODY_LIMIT,
    TOAST_TITLE_LIMIT,
    DesktopChannel,
    Notifier,
    TelegramChannel,
    build_notifier,
)

TOKEN = "123:SIR-TOKEN-xyz"
CHAT_ID = "42"


@pytest.fixture(autouse=True)
def _bypass_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yerel sunucuya giden istekler ortamdaki bir HTTP vekilinden geçmesin."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


# ---------------------------------------------------------------------- #
# Sahte Telegram sunucusu
# ---------------------------------------------------------------------- #


@dataclass
class SeenRequest:
    method: str
    path: str
    content_type: str | None
    body: dict[str, Any]


@dataclass
class FakeTelegram:
    """Yerel sunucunun durumu: görülen istekler ve testin ayarladığı cevap."""

    port: int
    requests: list[SeenRequest] = field(default_factory=list)
    status: int = 200
    reply: bytes = b'{"ok": true}'
    delay: float = 0.0
    release: threading.Event = field(default_factory=threading.Event)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def respond(self, status: int, reply: bytes, delay: float = 0.0) -> None:
        self.status, self.reply, self.delay = status, reply, delay


class _Handler(BaseHTTPRequestHandler):
    server: Any  # `fake` alanını taşıyan ThreadingHTTPServer

    def do_POST(self) -> None:
        fake: FakeTelegram = self.server.fake
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        fake.requests.append(SeenRequest("POST", self.path, self.headers.get("Content-Type"), body))
        if fake.delay:
            fake.release.wait(fake.delay)
        try:
            self.send_response(fake.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(fake.reply)))
            self.end_headers()
            self.wfile.write(fake.reply)
        except (BrokenPipeError, ConnectionResetError):
            pass  # istemci zaman aşımına uğrayıp bağlantıyı kapattı: beklenen durum

    def log_message(self, format: str, *args: Any) -> None:
        pass  # test çıktısını kirletmesin


@pytest.fixture
def telegram_server() -> Iterator[FakeTelegram]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    fake = FakeTelegram(port=server.server_address[1])
    server.fake = fake  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        fake.release.set()  # uyuyan işleyiciler varsa hemen bırak
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port() -> int:
    """Bağlanıp kapatılmış, yani kimsenin dinlemediği bir port verir."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _channel(api_base: str, timeout: float = 5.0) -> TelegramChannel:
    return TelegramChannel(token=TOKEN, chat_id=CHAT_ID, timeout_seconds=timeout, api_base=api_base)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "projects.notify"]


# ---------------------------------------------------------------------- #
# 1-2. Telegram başarı ve kesme
# ---------------------------------------------------------------------- #


def test_telegram_success_posts_expected_request(telegram_server: FakeTelegram) -> None:
    """Başarılı gönderim: doğru yol, doğru JSON gövde, doğru başlık ve `True`."""
    assert _channel(telegram_server.api_base).send("Merhaba dünya") is True

    assert len(telegram_server.requests) == 1
    seen = telegram_server.requests[0]
    assert seen.method == "POST"
    assert seen.path == f"/bot{TOKEN}/sendMessage"
    assert seen.content_type == "application/json"
    assert seen.body == {"chat_id": CHAT_ID, "text": "Merhaba dünya", "disable_web_page_preview": True}


def test_telegram_long_text_is_cut_with_ellipsis(telegram_server: FakeTelegram) -> None:
    """5000 karakterlik metin ≤ 4000'e kesilir ve "…" ile biter."""
    assert _channel(telegram_server.api_base).send("a" * 5000) is True

    sent = telegram_server.requests[0].body["text"]
    assert len(sent) <= TELEGRAM_TEXT_LIMIT
    assert sent.endswith("…")
    assert sent.startswith("aaa")


def test_telegram_short_text_is_not_touched(telegram_server: FakeTelegram) -> None:
    """Sınırın altındaki metne "…" eklenmez."""
    text = "b" * TELEGRAM_TEXT_LIMIT
    assert _channel(telegram_server.api_base).send(text) is True
    assert telegram_server.requests[0].body["text"] == text


# ---------------------------------------------------------------------- #
# 3. Telegram başarısızlıkları: False, tek uyarı, token sızmaz
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "reply", "delay", "expected_in_log"),
    [
        pytest.param(401, b'{"ok": false}', 0.0, "401", id="http-401"),
        pytest.param(200, b'{"ok": false}', 0.0, "beklenmeyen cevap", id="ok-false"),
        pytest.param(200, b"not json", 0.0, "beklenmeyen cevap", id="invalid-json"),
        pytest.param(200, b'["ok", true]', 0.0, "beklenmeyen cevap", id="json-not-object"),
        pytest.param(200, b'{"ok": "true"}', 0.0, "beklenmeyen cevap", id="ok-not-boolean-true"),
        pytest.param(200, b'{"ok": true}', 1.0, "TimeoutError", id="timeout"),
    ],
)
def test_telegram_failures_return_false_and_never_log_token(
    telegram_server: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
    status: int,
    reply: bytes,
    delay: float,
    expected_in_log: str,
) -> None:
    """Her başarısızlık `False` döner, TEK uyarı loglar ve token log'a hiç girmez."""
    telegram_server.respond(status, reply, delay)
    channel = _channel(telegram_server.api_base, timeout=0.3)

    with caplog.at_level(logging.WARNING):
        assert channel.send("deneme") is False

    assert len(_warnings(caplog)) == 1
    assert expected_in_log in caplog.text
    assert TOKEN not in caplog.text
    assert "SIR-TOKEN" not in caplog.text


def test_telegram_204_is_not_success(telegram_server: FakeTelegram, caplog: pytest.LogCaptureFixture) -> None:
    """HTTP 200 dışındaki başarılı durum (ör. 204) de teslim kanıtı sayılmaz."""
    telegram_server.respond(204, b"")
    with caplog.at_level(logging.WARNING):
        assert _channel(telegram_server.api_base).send("deneme") is False
    assert len(_warnings(caplog)) == 1
    assert TOKEN not in caplog.text


def test_telegram_closed_port_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    """Kimsenin dinlemediği port: bağlantı reddi `False` döner, token sızmaz."""
    channel = _channel(f"http://127.0.0.1:{_free_port()}")

    with caplog.at_level(logging.WARNING):
        assert channel.send("deneme") is False

    assert len(_warnings(caplog)) == 1
    assert "URLError" in caplog.text
    assert TOKEN not in caplog.text


def test_telegram_invalid_api_base_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    """Geçersiz `api_base` bir exception değil `False` olur; mesajdaki URL/token loglanmaz."""
    channel = _channel("tanimsiz-sema")

    with caplog.at_level(logging.WARNING):
        assert channel.send("deneme") is False

    assert len(_warnings(caplog)) == 1
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------------- #
# 4-5. repr ve boş metin
# ---------------------------------------------------------------------- #


def test_repr_never_contains_token() -> None:
    channel = _channel("http://127.0.0.1:9")
    assert TOKEN not in repr(channel)
    assert "SIR-TOKEN" not in repr(channel)
    assert "SIR-TOKEN" not in str(channel)
    assert repr(channel) == "TelegramChannel(chat_id='42', token=***)"


@pytest.mark.parametrize("text", ["", "   ", "\n\t \n"])
def test_empty_text_returns_false_without_network(telegram_server: FakeTelegram, text: str) -> None:
    """Boş/boşluktan ibaret metin ağa hiç çıkmadan `False` döner."""
    assert _channel(telegram_server.api_base).send(text) is False
    assert telegram_server.requests == []


# ---------------------------------------------------------------------- #
# 6. Masaüstü kanalı (sahte çalıştırıcı)
# ---------------------------------------------------------------------- #


class FakeRunner:
    """`subprocess.run` yerine geçer: çağrıyı kaydeder, ayarlanan sonucu döner ya da fırlatır."""

    def __init__(self, returncode: int = 0, stderr: str = "", raises: BaseException | None = None) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, stdout="", stderr=self.stderr)


def _decode_script(command: list[str]) -> str:
    """`-EncodedCommand` değerini (base64, UTF-16-LE) çözüp betik metnini verir."""
    encoded = command[command.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


NASTY_TITLE = "Bitti \"proje\" 'x' $(calc) `whoami`; calc.exe"
NASTY_BODY = "<script>alert(1)</script> & </text><text>enjekte"


def test_desktop_success_builds_encoded_command_and_passes_text_via_env() -> None:
    """Başarı: komut biçimi doğru, betik metni İÇERMEZ, metin yalnızca ortam değişkenindedir."""
    runner = FakeRunner(returncode=0)

    assert DesktopChannel(runner=runner).send(NASTY_TITLE, NASTY_BODY) is True

    assert len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    assert command[0] == "powershell.exe"
    assert command[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"]
    assert len(command) == 7

    script = _decode_script(command)
    assert "ARTEMIS_TOAST_TITLE" in script
    assert "ARTEMIS_TOAST_BODY" in script
    assert "SecurityElement" in script
    assert "\\WindowsPowerShell\\v1.0\\powershell.exe" in script
    # Hiçbir parça betiğe gömülmemiş olmalı: enjeksiyon yüzeyi sıfır.
    assert NASTY_TITLE not in script
    assert NASTY_BODY not in script
    for fragment in ("calc", "whoami", "<script>", "alert", "enjekte", "proje"):
        assert fragment not in script

    assert kwargs["env"]["ARTEMIS_TOAST_TITLE"] == NASTY_TITLE
    assert kwargs["env"]["ARTEMIS_TOAST_BODY"] == NASTY_BODY
    assert kwargs["timeout"] == 15.0
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_desktop_script_is_identical_for_any_text() -> None:
    """Betik sabittir: farklı metinler aynı `-EncodedCommand` değerini üretir."""
    runner = FakeRunner()
    channel = DesktopChannel(runner=runner)

    assert channel.send("Bir", "iki") is True
    assert channel.send(NASTY_TITLE, NASTY_BODY) is True

    assert runner.calls[0][0] == runner.calls[1][0]
    assert _decode_script(runner.calls[0][0]) == notify._TOAST_SCRIPT


def test_desktop_custom_powershell_and_timeout_are_used() -> None:
    runner = FakeRunner()
    assert DesktopChannel(timeout_seconds=3.5, powershell="ps-ozel", runner=runner).send("a", "b") is True
    command, kwargs = runner.calls[0]
    assert command[0] == "ps-ozel"
    assert kwargs["timeout"] == 3.5


def test_desktop_nonzero_exit_returns_false_and_logs_stderr_tail(caplog: pytest.LogCaptureFixture) -> None:
    """Çıkış kodu ≠ 0: `False`; uyarı yalnızca stderr'in son 300 karakterini taşır."""
    runner = FakeRunner(returncode=1, stderr="x" * 500 + "SON-HATA-METNI")

    with caplog.at_level(logging.WARNING):
        assert DesktopChannel(runner=runner).send("Başlık", "Gövde") is False

    assert len(_warnings(caplog)) == 1
    assert "SON-HATA-METNI" in caplog.text
    assert "x" * 400 not in caplog.text


def test_desktop_oserror_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    runner = FakeRunner(raises=FileNotFoundError("powershell.exe yok"))
    with caplog.at_level(logging.WARNING):
        assert DesktopChannel(runner=runner).send("Başlık", "Gövde") is False
    assert len(_warnings(caplog)) == 1


def test_desktop_timeout_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd="powershell.exe", timeout=15))
    with caplog.at_level(logging.WARNING):
        assert DesktopChannel(runner=runner).send("Başlık", "Gövde") is False
    assert len(_warnings(caplog)) == 1


def test_desktop_text_is_cut_to_limits() -> None:
    """300 karakterlik başlık 100'e, 1000 karakterlik gövde 300'e kesilir; "…" ile biter."""
    runner = FakeRunner()

    assert DesktopChannel(runner=runner).send("T" * 300, "B" * 1000) is True

    env = runner.calls[0][1]["env"]
    assert len(env["ARTEMIS_TOAST_TITLE"]) == TOAST_TITLE_LIMIT
    assert env["ARTEMIS_TOAST_TITLE"].endswith("…")
    assert len(env["ARTEMIS_TOAST_BODY"]) == TOAST_BODY_LIMIT
    assert env["ARTEMIS_TOAST_BODY"].endswith("…")


def test_desktop_control_characters_are_stripped() -> None:
    """NUL ve XML'de geçersiz kontrol karakterleri ayıklanır; sekme/satır sonu kalır."""
    runner = FakeRunner()

    assert DesktopChannel(runner=runner).send("a\x00b\x01c", "satır1\nsatır2\tson") is True

    env = runner.calls[0][1]["env"]
    assert env["ARTEMIS_TOAST_TITLE"] == "abc"
    assert env["ARTEMIS_TOAST_BODY"] == "satır1\nsatır2\tson"


def test_desktop_blank_title_and_body_do_not_start_a_process() -> None:
    runner = FakeRunner()
    assert DesktopChannel(runner=runner).send("  ", "\n") is False
    assert runner.calls == []


# ---------------------------------------------------------------------- #
# 7. Notifier
# ---------------------------------------------------------------------- #


def test_notifier_both_channels_deliver_in_order(telegram_server: FakeTelegram) -> None:
    runner = FakeRunner()
    notifier = Notifier(_channel(telegram_server.api_base), DesktopChannel(runner=runner))

    assert notifier.enabled is True
    assert notifier.notify("Başlık", "Gövde") == ["telegram", "masaustu"]
    assert runner.calls[0][1]["env"]["ARTEMIS_TOAST_TITLE"] == "Başlık"
    assert runner.calls[0][1]["env"]["ARTEMIS_TOAST_BODY"] == "Gövde"


def test_notifier_default_telegram_text_is_title_newline_body(telegram_server: FakeTelegram) -> None:
    notifier = Notifier(telegram=_channel(telegram_server.api_base))

    assert notifier.notify("Başlık", "Gövde") == ["telegram"]
    assert telegram_server.requests[0].body["text"] == "Başlık\nGövde"


def test_notifier_telegram_text_overrides_default(telegram_server: FakeTelegram) -> None:
    runner = FakeRunner()
    notifier = Notifier(_channel(telegram_server.api_base), DesktopChannel(runner=runner))

    assert notifier.notify("Başlık", "Gövde", telegram_text="Ayrıntılı Telegram metni") == ["telegram", "masaustu"]
    assert telegram_server.requests[0].body["text"] == "Ayrıntılı Telegram metni"
    # Masaüstü yine kısa başlık/gövdeyi alır.
    assert runner.calls[0][1]["env"]["ARTEMIS_TOAST_TITLE"] == "Başlık"


def test_notifier_reports_only_channels_that_really_delivered(telegram_server: FakeTelegram) -> None:
    """Telegram reddederse yalnızca masaüstü; masaüstü başarısızsa yalnızca Telegram; ikisi de başarısızsa boş."""
    telegram_server.respond(401, b'{"ok": false}')
    ok_runner = FakeRunner()
    assert Notifier(_channel(telegram_server.api_base), DesktopChannel(runner=ok_runner)).notify("a", "b") == [
        "masaustu"
    ]

    telegram_server.respond(200, b'{"ok": true}')
    bad_runner = FakeRunner(returncode=1, stderr="hata")
    assert Notifier(_channel(telegram_server.api_base), DesktopChannel(runner=bad_runner)).notify("a", "b") == [
        "telegram"
    ]

    telegram_server.respond(401, b'{"ok": false}')
    assert Notifier(_channel(telegram_server.api_base), DesktopChannel(runner=bad_runner)).notify("a", "b") == []


def test_notifier_without_channels_is_disabled_and_returns_empty() -> None:
    notifier = Notifier()
    assert notifier.enabled is False
    assert notifier.notify("a", "b") == []


# ---------------------------------------------------------------------- #
# 8. build_notifier
# ---------------------------------------------------------------------- #


def test_build_notifier_without_chat_id_has_no_telegram(caplog: pytest.LogCaptureFixture) -> None:
    for chat_id in (None, "", "   "):
        with caplog.at_level(logging.WARNING):
            notifier = build_notifier(
                telegram_chat_id=chat_id,
                telegram_token_env="ARTEMIS_TG_TOKEN",
                desktop=False,
                env={"ARTEMIS_TG_TOKEN": "var-ama-kullanilmaz"},
            )
        assert notifier.enabled is False
    assert _warnings(caplog) == []  # sohbet kimliği hiç verilmediyse uyarı gürültüsü yok


def test_build_notifier_chat_id_without_token_warns_with_env_var_name_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sohbet kimliği var, token yok: Telegram yok; uyarı ortam değişkeninin ADINI söyler, değer sızmaz."""
    for env in ({}, {"ARTEMIS_TG_TOKEN": "   "}, {"BASKA_DEGISKEN": "baska-sir-deger"}):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            notifier = build_notifier(
                telegram_chat_id=CHAT_ID, telegram_token_env="ARTEMIS_TG_TOKEN", desktop=False, env=env
            )
        assert notifier.enabled is False
        assert len(_warnings(caplog)) == 1
        assert "ARTEMIS_TG_TOKEN" in caplog.text
        assert "baska-sir-deger" not in caplog.text


def test_build_notifier_with_token_builds_working_telegram(telegram_server: FakeTelegram) -> None:
    """Sohbet kimliği + token: kanal kurulur; kimlik ve token kırpılarak kullanılır."""
    notifier = build_notifier(
        telegram_chat_id=f" {CHAT_ID} ",
        telegram_token_env="ARTEMIS_TG_TOKEN",
        desktop=False,
        env={"ARTEMIS_TG_TOKEN": f"  {TOKEN}\n"},
        telegram_api_base=telegram_server.api_base,
    )

    assert notifier.enabled is True
    assert notifier.notify("Başlık", "Gövde") == ["telegram"]
    seen = telegram_server.requests[0]
    assert seen.path == f"/bot{TOKEN}/sendMessage"
    assert seen.body["chat_id"] == CHAT_ID


def test_build_notifier_reads_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch, telegram_server: FakeTelegram
) -> None:
    monkeypatch.setenv("ARTEMIS_TG_TOKEN_TEST", TOKEN)
    notifier = build_notifier(
        telegram_chat_id=CHAT_ID,
        telegram_token_env="ARTEMIS_TG_TOKEN_TEST",
        desktop=False,
        telegram_api_base=telegram_server.api_base,
    )
    assert notifier.notify("a", "b") == ["telegram"]


def test_build_notifier_desktop_only_on_windows() -> None:
    """Masaüstü kanalı yalnızca `win32`'de kurulur; `desktop=False` her platformda kapalıdır."""
    kwargs: dict[str, Any] = {"telegram_chat_id": None, "telegram_token_env": "X", "env": {}}

    assert build_notifier(desktop=True, platform="linux", **kwargs).enabled is False
    assert build_notifier(desktop=True, platform="darwin", **kwargs).enabled is False
    assert build_notifier(desktop=False, platform="win32", **kwargs).enabled is False

    windows = build_notifier(desktop=True, platform="win32", **kwargs)
    assert windows.enabled is True
    # Gerçek `DesktopChannel.send` çağrılmaz (Windows'ta toast gösterirdi): yalnızca türü doğrulanır.
    assert isinstance(windows._desktop, DesktopChannel)


def test_build_notifier_platform_defaults_to_sys_platform() -> None:
    notifier = build_notifier(telegram_chat_id=None, telegram_token_env="X", desktop=True, env={})
    assert notifier.enabled is (sys.platform == "win32")


def test_build_notifier_nothing_configured_is_disabled() -> None:
    notifier = build_notifier(telegram_chat_id=None, telegram_token_env="X", desktop=False, env={})
    assert notifier.enabled is False
    assert notifier.notify("a", "b") == []
