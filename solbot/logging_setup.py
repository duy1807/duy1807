"""Logging with secret redaction.

Any registered secret (wallet key in every encoding, API tokens) is replaced by ``[REDACTED]``
in every log record. A heuristic also masks long base58 strings that look like 64-byte
secret keys (87-88 chars), and URL query tokens (api-key=...).
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

_secrets: set[str] = set()
_lock = threading.Lock()
_B58_SECRET = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{86,88}\b")
_URL_KEY = re.compile(r"((?:api[-_]?key|token|x-token|auth)=)[^&\s\"']+", re.I)


def register_secret(s: str | None) -> None:
    if s and len(s) >= 8:
        with _lock:
            _secrets.add(s)


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "[REDACTED]")
    text = _B58_SECRET.sub("[REDACTED-B58]", text)
    text = _URL_KEY.sub(r"\1[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(RedactingFilter())
    root.addHandler(sh)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "solbot.log", maxBytes=20_000_000, backupCount=5)
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
