"""Proje atölyesi bildirim taşıması: Telegram mesajı ve Windows toast.

Arka planda çalışan bir kodlama işi (ayrı süreç, bkz. `projects/runner.py`) kullanıcı
bilgisayarın başında DEĞİLKEN ya da Artemis kapalıyken biter. Bu modül kullanıcıya iki
yoldan haber verir: uzaktayken Telegram mesajı, bilgisayarın başındayken Windows toast'ı.

Bu modül YALNIZCA taşımadır: hazır metni alır ve teslim etmeyi dener. Mesajın ne
söyleyeceği başka yerde kurulur.

Tasarım ilkeleri:

- **Asla exception fırlatmaz.** Her kanal kendi hatalarını yakalar, Türkçe TEK bir uyarı
  loglar ve `False` döner. Bildirim bir işin yan ürünüdür; bildirim gönderilemedi diye
  iş sonucunun kaydı (store, vault) yarıda kalmamalı.
- **Başarıyı uydurmaz.** `True` yalnızca teslimin kanıtı varsa döner: Telegram için HTTP 200
  VE cevap gövdesinde `"ok": true`; toast için PowerShell çıkış kodu 0. `Notifier.notify`
  gerçekten teslim eden kanalların adlarını döner; çağıran "bildirildi" diye işaretlemeyi
  buna bakarak yapar.
- **Telegram bot token'ı yalnızca ortam değişkeninden okunur** (`build_notifier`), asla bir
  CLI argümanı olarak alınmaz: argv süreç listesinde herkese görünür. Token ayrıca log'a,
  `repr`'e ve hata mesajlarına SIZMAZ (bkz. `TelegramChannel`).
- Yalnızca standart kütüphane: yeni bağımlılık yok. Windows'a özgü hiçbir şey import
  edilmez; toast, `powershell.exe` alt süreci olarak çalışır.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
# Telegram'ın sert sınırı 4096; kesme işareti ve olası sayım farkları için pay bırakılır.
TELEGRAM_TEXT_LIMIT = 4000
TOAST_TITLE_LIMIT = 100
TOAST_BODY_LIMIT = 300

CHANNEL_TELEGRAM = "telegram"
CHANNEL_DESKTOP = "masaustu"

# Toast betiği SABİTTİR ve başlık/gövde metnini ASLA içermez. Metin yalnızca ortam
# değişkenleriyle gelir. Neden: kullanıcı ya da modelin ürettiği bir metni betiğe
# gömmek kod enjeksiyonudur (`"; calc; "` gibi bir başlık betiği yönetir); ortam
# değişkeni ise VERİDİR, yorumlanmaz. XML olarak da kaçırılır (`SecurityElement.Escape`),
# yoksa `<` ve `&` içeren bir metin toast XML'ini bozar.
#
# Windows PowerShell 5.1 (`powershell.exe`) gerekir: PowerShell 7 (`pwsh`) WinRT tiplerini
# yükleyemez. `$appId` PowerShell'in kendi kayıtlı uygulama kimliğidir; kayıtsız bir kimlikle
# gösterilen toast sessizce yutulur.
_TOAST_SCRIPT = r"""$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$title = [System.Security.SecurityElement]::Escape($env:ARTEMIS_TOAST_TITLE)
$body = [System.Security.SecurityElement]::Escape($env:ARTEMIS_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

_STDERR_TAIL = 300


def _shorten(text: str, limit: int) -> str:
    """Metni en çok `limit` karaktere indirir; kesilirse sonuna "…" koyar."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clean_toast_text(text: str, limit: int) -> str:
    """Toast metnini ortam değişkenine ve XML'e güvenli hale getirir, sonra keser.

    NUL karakteri ortam değişkeninde `ValueError` ("embedded null") üretir; XML 1.0 da
    sekme/satır sonu dışındaki kontrol karakterlerini kabul etmez ve `LoadXml` patlar.
    İkisi de modelin ürettiği metinde gelebileceği için baştan ayıklanır.
    """
    cleaned = "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32)
    return _shorten(cleaned, limit)


class TelegramChannel:
    """Telegram Bot API `sendMessage` ile mesaj gönderir.

    Bot token'ı URL'nin içindedir (`/bot<token>/sendMessage`). Bu yüzden URL, istek ya da
    `urllib` hatalarının `str()`'i ASLA loglanmaz: loglar paylaşılır, token sızarsa bot ele
    geçirilir. Yalnızca durum kodu ve istisna TÜRÜNÜN adı loglanır.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        timeout_seconds: float = 10.0,
        api_base: str = TELEGRAM_API_BASE,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout_seconds
        self._api_base = api_base.rstrip("/")

    def __repr__(self) -> str:
        # Varsayılan `repr` yerine elle yazılır: bir hata ayıklama çıktısı ya da log
        # satırı nesneyi gösterirse token yine görünmesin.
        return f"TelegramChannel(chat_id={self._chat_id!r}, token=***)"

    def send(self, text: str) -> bool:
        """Mesajı gönderir; yalnızca Telegram `ok: true` dediyse `True` döner."""
        if not text.strip():
            logger.warning("Telegram bildirimi boş metin olduğu için gönderilmedi.")
            return False

        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": _shorten(text, TELEGRAM_TEXT_LIMIT),
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        url = f"{self._api_base}/bot{self._token}/sendMessage"

        try:
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # HTTPError, URLError'ın alt sınıfıdır: önce o yakalanmalı. `str(exc)` URL'yi
            # (yani token'ı) içerebilir; yalnızca durum kodu loglanır.
            exc.close()
            logger.warning("Telegram bildirimi reddedildi (HTTP %s).", exc.code)
            return False
        except urllib.error.URLError as exc:
            # `reason` genelde bir OSError'dır ve metni URL taşımaz; yine de yalnızca TÜRÜ
            # loglanır. Düz metin bir `reason` ise (ör. "unknown url type: ...") token'ı
            # içerebilir, bu yüzden yalnızca token içermiyorsa gösterilir.
            reason = exc.reason
            if isinstance(reason, str):
                detail = "gizlendi" if self._token and self._token in reason else reason
            else:
                detail = type(reason).__name__
            logger.warning("Telegram bildirimi gönderilemedi (URLError: %s).", detail)
            return False
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError: zaman aşımı (TimeoutError), bağlantı kopması. HTTPException:
            # sunucu bozuk bir durum satırı gönderdi. ValueError: geçersiz `api_base`.
            # Hepsinde mesaj metni URL taşıyabilir: yalnızca tür adı loglanır.
            logger.warning("Telegram bildirimi gönderilemedi (%s).", type(exc).__name__)
            return False

        if status != 200:
            logger.warning("Telegram bildirimi beklenmeyen cevap verdi (HTTP %s).", status)
            return False
        try:
            parsed = json.loads(raw)
        except ValueError:  # JSONDecodeError ve UnicodeDecodeError ikisi de ValueError'dır
            logger.warning("Telegram bildirimi beklenmeyen cevap verdi (JSON değil).")
            return False
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            logger.warning("Telegram bildirimi beklenmeyen cevap verdi (ok: true değil).")
            return False
        return True


class DesktopChannel:
    """Windows toast bildirimi gösterir (Windows PowerShell 5.1 + WinRT).

    `runner` `subprocess.run` imzasındadır; testler gerçek bir toast göstermemek için
    sahte bir çalıştırıcı verir.
    """

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        powershell: str = "powershell.exe",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._timeout = timeout_seconds
        self._powershell = powershell
        self._runner = runner

    def send(self, title: str, body: str) -> bool:
        """Toast'ı gösterir; yalnızca PowerShell çıkış kodu 0 ise `True` döner."""
        clean_title = _clean_toast_text(title, TOAST_TITLE_LIMIT)
        clean_body = _clean_toast_text(body, TOAST_BODY_LIMIT)
        if not clean_title.strip() and not clean_body.strip():
            logger.warning("Masaüstü bildirimi başlık ve gövde boş olduğu için gösterilmedi.")
            return False

        # `-EncodedCommand`: betik UTF-16-LE + base64 olarak verilir; tırnak/kaçış sorunu yoktur.
        encoded = base64.b64encode(_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
        command = [
            self._powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
        env = {**os.environ, "ARTEMIS_TOAST_TITLE": clean_title, "ARTEMIS_TOAST_BODY": clean_body}

        try:
            completed = self._runner(
                command,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
                # Konsol penceresi kısa süre yanıp sönmesin. POSIX'te bayrak 0 olmak zorunda.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            logger.warning("Masaüstü bildirimi %.1f sn içinde tamamlanmadı (zaman aşımı).", self._timeout)
            return False
        except (OSError, ValueError) as exc:
            # OSError: powershell.exe yok / başlatılamadı. ValueError: ortam değişkeni geçersiz.
            logger.warning("Masaüstü bildirimi başlatılamadı (%s): %s", type(exc).__name__, exc)
            return False

        if completed.returncode != 0:
            tail = (completed.stderr or "").strip()[-_STDERR_TAIL:]
            logger.warning("Masaüstü bildirimi başarısız (çıkış kodu %s): %s", completed.returncode, tail)
            return False
        return True


class Notifier:
    """Yapılandırılmış kanalların hepsine aynı bildirimi yollar."""

    def __init__(self, telegram: TelegramChannel | None = None, desktop: DesktopChannel | None = None) -> None:
        self._telegram = telegram
        self._desktop = desktop

    @property
    def enabled(self) -> bool:
        """En az bir kanal yapılandırılmış mı?"""
        return self._telegram is not None or self._desktop is not None

    @property
    def channels(self) -> list[str]:
        """Yapılandırılmış kanalların adları (teslim edilenler değil; bkz. `notify`)."""
        names: list[str] = []
        if self._telegram is not None:
            names.append(CHANNEL_TELEGRAM)
        if self._desktop is not None:
            names.append(CHANNEL_DESKTOP)
        return names

    def notify(self, title: str, body: str, telegram_text: str | None = None) -> list[str]:
        """Bildirimi yollar; GERÇEKTEN teslim eden kanalların adlarını döner.

        Dönüş `["telegram", "masaustu"]` kümesinin bu sıradaki bir alt kümesidir. Telegram,
        verildiyse `telegram_text`'i, verilmediyse `f"{title}\\n{body}"` metnini alır; masaüstü
        `(title, body)` alır. Kanallar kendi hatalarını yakaladığı için burada genel bir
        `try` yoktur.
        """
        delivered: list[str] = []
        if self._telegram is not None:
            text = telegram_text if telegram_text is not None else f"{title}\n{body}"
            if self._telegram.send(text):
                delivered.append(CHANNEL_TELEGRAM)
        if self._desktop is not None and self._desktop.send(title, body):
            delivered.append(CHANNEL_DESKTOP)
        return delivered


def build_notifier(
    *,
    telegram_chat_id: str | None,
    telegram_token_env: str,
    desktop: bool,
    timeout_seconds: float = 10.0,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    telegram_api_base: str = TELEGRAM_API_BASE,
) -> Notifier:
    """Ayarlardan bir `Notifier` kurar.

    Telegram kanalı için sohbet kimliği VE token birlikte gerekir. Token, adı
    `telegram_token_env` olan ortam değişkeninden okunur (bkz. modül dokümantasyonu: argv
    ile alınmaz). Sohbet kimliği var ama token yoksa kullanıcının nedenini anlaması için
    ortam değişkeninin ADI (değeri değil) uyarıyla bildirilir. Masaüstü kanalı yalnızca
    Windows'ta kurulur.
    """
    source = os.environ if env is None else env
    current_platform = sys.platform if platform is None else platform

    telegram: TelegramChannel | None = None
    chat_id = (telegram_chat_id or "").strip()
    if chat_id:
        token = source.get(telegram_token_env, "").strip()
        if token:
            telegram = TelegramChannel(
                token=token, chat_id=chat_id, timeout_seconds=timeout_seconds, api_base=telegram_api_base
            )
        else:
            logger.warning(
                "Telegram sohbet kimliği ayarlı ama %s ortam değişkeni boş; Telegram bildirimi kapalı.",
                telegram_token_env,
            )

    desktop_channel = DesktopChannel() if desktop and current_platform == "win32" else None
    return Notifier(telegram=telegram, desktop=desktop_channel)
