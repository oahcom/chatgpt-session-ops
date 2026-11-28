# -*- coding: utf-8 -*-
"""日志：控制台 + 文件，统一脱敏。"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*[^\s,;\"']+"), r"\1=***"),
    (re.compile(r"(?i)(access_token|refresh_token|session-token|authjs\.session-token)="
                r"[^;\s]+"), r"\1=***"),
    (re.compile(r"(?i)(api[_-]?key|clientKey|apikey)\s*[=:]\s*[^\s,;\"']+"), r"\1=***"),
    (re.compile(r"(?i)(x-pm-human-verification-token)\s*[=:]\s*[^\s,;\"']+"), r"\1=***"),
    (re.compile(r"gAAAAAB[A-Za-z0-9_\-~=\.]{20,}"), "***PROOF***"),
]


def redact(text: str) -> str:
    out = str(text)
    for pattern, repl in SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path | None, verbose: bool = False, name: str = "proton_batch") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "batch.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-5s [%(threadName)s] %(message)s"))
        logger.addHandler(fh)

    return logger
