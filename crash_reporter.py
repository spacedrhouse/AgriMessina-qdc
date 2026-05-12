"""Crash reporter globale per AgriMessina QDC.

Cattura eccezioni non gestite (sys.excepthook + threading.excepthook) e
le scrive in `~/.agrimessina/crashes/crash-YYYYMMDD-HHMMSS.txt` con info
di contesto utili al debug:
- Versione dell'app
- Versione Python e OS
- Traceback completo
- Ultime righe del log applicativo (se presente)

Se l'app GUI è già attiva, mostra un dialog con il path del file di crash
così l'utente può segnalarlo. Altrimenti, solo file.

Setup: `setup_crash_reporter()` all'inizio di main, PRIMA di tutto il resto.
"""
from __future__ import annotations

import datetime
import logging
import platform
import sys
import threading
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

CRASH_DIR = Path.home() / ".agrimessina" / "crashes"
LOG_TAIL_LINES = 100


def _ensure_dir() -> Path:
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    return CRASH_DIR


def _app_version() -> str:
    """Letta da _version.py se presente (build CI), altrimenti 'dev'."""
    try:
        from _version import __version__  # type: ignore[import-not-found]
        return __version__
    except ImportError:
        return "dev"


def _read_log_tail() -> str:
    """Cerca il file di log applicativo e ne ritorna le ultime righe.
    Utile per ricostruire cosa stava succedendo PRIMA del crash."""
    # Allineato con app_logging.setup_logging che scrive in qdc.log.
    log_path = Path.home() / ".agrimessina" / "qdc.log"
    if not log_path.exists():
        return "(nessun log applicativo trovato)"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-LOG_TAIL_LINES:])
    except Exception as e:
        return f"(errore lettura log: {e})"


def _write_crash(exc_type, exc_value, tb) -> Path:
    """Scrive il file di crash. Ritorna il path."""
    _ensure_dir()
    now = datetime.datetime.now()
    path = CRASH_DIR / now.strftime("crash-%Y%m%d-%H%M%S.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write("AgriMessina QDC — crash report\n")
            f.write("=" * 70 + "\n\n")
            f.write(f"Data:        {now.isoformat()}\n")
            f.write(f"App version: {_app_version()}\n")
            f.write(f"Python:      {sys.version.splitlines()[0]}\n")
            f.write(f"OS:          {platform.platform()}\n")
            f.write(f"Thread:      {threading.current_thread().name}\n\n")
            f.write("## Traceback\n\n")
            traceback.print_exception(exc_type, exc_value, tb, file=f)
            f.write("\n\n## Log applicativo (ultimi messaggi)\n\n")
            f.write(_read_log_tail())
    except Exception as e:
        # Disastro: anche il crash reporter è rotto. Stampiamo a stderr come
        # ultima risorsa così l'utente almeno vede qualcosa.
        print(f"[crash_reporter] impossibile scrivere {path}: {e}", file=sys.stderr)
    return path


def _show_dialog(path: Path, summary: str) -> None:
    """Se QApplication è attiva, mostra un dialog. Altrimenti silenzioso —
    il file è già stato scritto, basta."""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return
    if QApplication.instance() is None:
        return

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Critical)
    msg.setWindowTitle("Errore inatteso")
    msg.setText(
        "L'applicazione ha incontrato un problema imprevisto.\n\n"
        "Un file diagnostico è stato salvato in:\n"
        f"{path}\n\n"
        "Mandalo a chi gestisce l'app per la diagnosi."
    )
    msg.setDetailedText(summary)
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()


def _excepthook(exc_type, exc_value, tb):
    # KeyboardInterrupt/SystemExit non sono crash: lasciamo l'handler default.
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc_value, tb)
        return

    try:
        path = _write_crash(exc_type, exc_value, tb)
        summary = f"{exc_type.__name__}: {exc_value}"
        log.error("[crash] %s — log: %s", summary, path)
        _show_dialog(path, summary)
    except Exception:
        # Mai propagare un'eccezione dal crash reporter, sarebbe loop.
        pass

    # Anche dopo il logging, deferiamo all'handler default così l'output
    # console resta uguale e altri tool (es. pytest) vedono comunque l'errore.
    sys.__excepthook__(exc_type, exc_value, tb)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """threading.excepthook: cattura eccezioni in worker thread (QThread,
    background tasks, ecc.) che altrimenti sparirebbero silenziosamente."""
    if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
        return
    try:
        path = _write_crash(args.exc_type, args.exc_value, args.exc_traceback)
        summary = f"[thread {args.thread.name if args.thread else '?'}] " \
                  f"{args.exc_type.__name__}: {args.exc_value}"
        log.error("[crash] %s — log: %s", summary, path)
        # Niente dialog per i crash di thread: spesso sono in cascata e
        # mostrare 10 popup uccide l'utente.
    except Exception:
        pass


def setup_crash_reporter() -> None:
    """Installa gli hook globali. Idempotente: chiamarla due volte è ok."""
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    log.info("[crash_reporter] hook installati, crashes in %s", CRASH_DIR)
