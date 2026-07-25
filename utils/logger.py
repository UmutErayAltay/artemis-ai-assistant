"""Merkezi logging konfigürasyonu.

Uygulama genelinde her modül `logging.getLogger(__name__)` ile kendi
logger'ını alır; bu modül yalnızca ortak formatter/handler kurulumunu
(console + dönen dosya) tek bir yerden yapar. Böylece her yeni tool/plugin
kendi logging kurulumunu tekrar yazmak zorunda kalmaz.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Root logger'ı console ve dönen dosya (rotating file) handler'larıyla kurar.

    Args:
        log_dir: Log dosyalarının yazılacağı klasör (yoksa oluşturulur).
        level: Kök logger seviyesi (varsayılan: INFO).
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_LOG_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_dir / "artemis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
