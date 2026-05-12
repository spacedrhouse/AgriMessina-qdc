"""Configurazione logging centralizzata.

Scrive su file rotativo in ~/.agrimessina/qdc.log. Su esecuzione non-frozen
(sviluppo) duplica su console. Modulo importabile da chiunque via:

    from app_logging import get_logger
    log = get_logger(__name__)
    log.info("...")
"""
from __future__ import annotations
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_INITIALIZED = False


def setup_logging(log_level: int = logging.INFO) -> None:
    """Da chiamare una volta all'avvio dell'app (in main.run())."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    log_dir = Path.home() / ".agrimessina"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    # File handler rotativo (2 MB × 3 file)
    fh = RotatingFileHandler(
        log_dir / "qdc.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console solo in modalità sviluppo (non frozen)
    if not getattr(sys, "frozen", False):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    # Riduci verbosità delle librerie di terze parti
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Ritorna un logger per il modulo. Inizializza il sistema se non già fatto."""
    if not _INITIALIZED:
        setup_logging()
    return logging.getLogger(name)
