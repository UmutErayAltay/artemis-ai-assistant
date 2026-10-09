"""Testlerde kullanılan sahte vault ve sahte `beyin.py` CLI'si.

Gerçek dosya sistemiyle çalışır: `tmp_path` altına bir vault dizini, vault'un
DIŞINA da çalıştırılabilir bir CLI betiği kurar. Betik her çağrıyı bir JSONL
log dosyasına yazar; böylece testler köprünün CLI'yı hangi argümanlarla ve
hangi çalışma dizininde çağırdığını (ya da hiç çağırmadığını) doğrulayabilir.

NEDEN BETİK DÜZ BİR SABİT: betik daha önce `str.format` / f-string ile üretiliyordu
ve içindeki JSON süslü parantezleri kaçırılmadığı için `KeyError: '"argv"'` ile
çöküyordu. Betik artık HİÇBİR şablonlama görmeyen ham bir dizedir; seçenekler
(çıkış kodu, bekleme, receipt durumu...) yanındaki JSON yapılandırma dosyasından
okunur, yani parantez/tırnak kaçırma sorunu yapısal olarak ortadan kalktı.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORE_MD = """# Artemis: Core

## What I should never forget
<!-- bu satır bir HTML yorumudur, modele gitmemeli -->
- Tercih 1: Kısa ve net cevaplar.
- Tercih 2: Türkçe yanıtlamak.

## Başka bölüm
- Bu bölüm tercihlere karışmamalı.
"""

# Ham dize (r'''...'''): içindeki "\n" betik kaynağında "\n" olarak kalır, yani
# betiği çalıştıran Python onu satır sonu olarak yorumlar. `.format` ÇAĞRILMAZ.
_CLI_SCRIPT = r'''"""Sahte beyin.py CLI'si (test amaçlı)."""

import json
import os
import sys
import time
from pathlib import Path

# Köprü stdout'u UTF-8 çözer; Windows'ta boru çıktısı varsayılan olarak ANSI kodlamasındadır.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CONFIG = json.loads(Path(__file__).with_name("sahte_beyin_config.json").read_text(encoding="utf-8"))


def option_value(argv, name):
    """`--file x` gibi bir seçeneğin değerini döndürür; yoksa None."""
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def main():
    argv = sys.argv[1:]
    command = argv[0] if argv else ""

    receipt = None
    receipt_path = None
    if command == "receipt":
        receipt_path = option_value(argv, "--file")
        if receipt_path is not None:
            try:
                receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                receipt = {"error": "receipt dosyası okunamadı"}

    # Çağrı, bekleme ve yanıttan ÖNCE loglanır: zaman aşımıyla öldürülen bir
    # çağrı bile logda görünür.
    entry = {"argv": argv, "cwd": os.getcwd(), "receipt": receipt, "receipt_path": receipt_path}
    with Path(CONFIG["log_path"]).open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    time.sleep(CONFIG["sleep_seconds"])

    if command == "context":
        if CONFIG["context_exit"] != 0:
            print("sahte context hatası", file=sys.stderr)
            return CONFIG["context_exit"]
        if CONFIG["context_stdout"] is not None:
            print(CONFIG["context_stdout"])
        else:
            print(json.dumps({"records": CONFIG["records"]}))
        return 0

    if command == "receipt":
        print(json.dumps({"status": CONFIG["receipt_status"], "source": "receipts/sahte.md"}))
        return 0

    print("bilinmeyen komut: " + repr(argv), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


@dataclass
class SahteVault:
    """Sahte vault'a ve sahte CLI'sına tek elden erişim."""

    path: Path  # vault dizini
    command: list[str]  # [sys.executable, "<betik yolu>"]
    log: Path  # JSONL: CLI çağrısı başına bir satır

    def calls(self) -> list[dict[str, Any]]:
        """Logdaki çağrıları sırayla döndürür; hiç çağrı olmadıysa `[]`.

        Her öğe: `{"argv", "cwd", "receipt", "receipt_path"}`. `receipt`,
        yalnızca `receipt` komutunda dolu olan ve CLI'nın `--file`'dan okuduğu
        yüktür; `receipt_path` o dosyanın yoludur.
        """
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


def sahte_vault(
    tmp_path: Path,
    records: list[dict[str, Any]] | None = None,
    *,
    context_exit: int = 0,
    context_stdout: str | None = None,
    receipt_status: str = "succeeded",
    sleep_seconds: float = 0.0,
) -> SahteVault:
    """`tmp_path` altında sahte bir vault ve onun sahte CLI'sını kurar.

    Aynı `tmp_path` ile ikinci kez çağrılırsa yapılandırma ve log sıfırlanır;
    farklı seçenekli iki vault gerekiyorsa farklı alt dizinler verin.

    Args:
        tmp_path: Vault'un (`tmp_path/"vault"`) ve betiğin kurulacağı dizin.
        records: `context` komutunun döndüreceği `records` listesi.
        context_exit: `context` komutunun çıkış kodu (0 dışı hata benzetir).
        context_stdout: Verilirse `context` stdout'u aynen budur (bozuk çıktı testi).
        receipt_status: `receipt` komutunun yanıtındaki `status` değeri.
        sleep_seconds: Her çağrıda (loglamadan sonra) beklenecek süre; zaman aşımı testi için.
    """
    vault_dir = tmp_path / "vault"
    companion_dir = vault_dir / "🔮 850-Companion"
    companion_dir.mkdir(parents=True, exist_ok=True)
    (companion_dir / "Core.md").write_text(CORE_MD, encoding="utf-8")
    (vault_dir / "🏰 300-Projects").mkdir(exist_ok=True)

    # Betik, yapılandırma ve log vault'un DIŞINDA durur: köprü vault'a yalnızca
    # kendi yazdığı notları bırakmalı, test donanımı onu kirletmemeli.
    script_path = tmp_path / "sahte_beyin_cli.py"
    config_path = tmp_path / "sahte_beyin_config.json"
    log_path = tmp_path / "sahte_beyin_log.jsonl"

    config = {
        "records": records or [],
        "context_exit": context_exit,
        "context_stdout": context_stdout,
        "receipt_status": receipt_status,
        "sleep_seconds": sleep_seconds,
        "log_path": str(log_path),
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    script_path.write_text(_CLI_SCRIPT, encoding="utf-8")
    log_path.unlink(missing_ok=True)

    # `sys.executable` sayesinde komut Windows'ta da POSIX'te de aynı çalışır.
    return SahteVault(path=vault_dir, command=[sys.executable, str(script_path)], log=log_path)
