"""Backup automatico del SQLite locale di AgriMessina QDC.

Strategia minimalista ma robusta:
- Al boot dell'app si controlla quando è stato fatto l'ultimo backup.
- Se più vecchio di BACKUP_INTERVAL_HOURS, si copia `local.db` in
  `~/.agrimessina/backups/local-YYYYMMDD-HHMMSS.db`.
- Si tengono solo gli ultimi MAX_BACKUPS, i precedenti vengono cancellati.

Niente backup incrementali o crittografia: il DB locale è di una singola
postazione, vogliamo qualcosa di debuggabile senza tool speciali (sqlite3
e basta) e con file leggibili.

NOTE: la copia avviene durante il boot dell'app, prima di aprire qualunque
connessione SQLAlchemy. Questo garantisce che il file non sia in mezzo a
una scrittura attiva quando facciamo la copia. Per scenari più complessi
si potrebbe usare l'API SQLite VACUUM INTO o BACKUP, ma sarebbe overkill.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"
BACKUP_FILENAME_FMT = "local-%Y%m%d-%H%M%S.db"
BACKUP_INTERVAL_HOURS = 24
MAX_BACKUPS = 7


def _backup_dir(db_path: Path) -> Path:
    p = db_path.parent / BACKUP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _existing_backups(db_path: Path) -> list[Path]:
    """Lista dei backup esistenti, ordinati dal più recente al più vecchio."""
    return sorted(
        _backup_dir(db_path).glob("local-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def backup_if_due(db_path: Path) -> Path | None:
    """Se il DB esiste e l'ultimo backup è più vecchio di BACKUP_INTERVAL_HOURS,
    crea un nuovo backup e ruota.

    Ritorna il Path del nuovo backup, o None se non era il momento (o se il
    DB non esiste, o se la copia è fallita).
    """
    if not db_path.exists():
        return None

    backups = _existing_backups(db_path)
    if backups:
        last_mtime = datetime.fromtimestamp(backups[0].stat().st_mtime)
        if datetime.now() - last_mtime < timedelta(hours=BACKUP_INTERVAL_HOURS):
            return None

    dest = _backup_dir(db_path) / datetime.now().strftime(BACKUP_FILENAME_FMT)
    try:
        # shutil.copy2 preserva i metadati (mtime importante per la rotation).
        shutil.copy2(db_path, dest)
        log.info("[backup] creato %s (%.1f MB)", dest.name,
                 dest.stat().st_size / 1024 / 1024)
    except Exception as e:
        log.warning("[backup] fallito: %s", e)
        return None

    _rotate(db_path)
    return dest


def _rotate(db_path: Path) -> None:
    """Tiene solo gli ultimi MAX_BACKUPS, cancella i più vecchi."""
    backups = _existing_backups(db_path)
    for old in backups[MAX_BACKUPS:]:
        try:
            old.unlink()
            log.info("[backup] ruotato (rimosso) %s", old.name)
        except Exception as e:
            log.warning("[backup] rotazione fallita su %s: %s", old.name, e)


def list_backups(db_path: Path) -> list[tuple[Path, datetime, int]]:
    """Utility per UI: elenco backup con timestamp e size in bytes.
    Ritorna lista di tuple (path, datetime, size_bytes)."""
    return [
        (p, datetime.fromtimestamp(p.stat().st_mtime), p.stat().st_size)
        for p in _existing_backups(db_path)
    ]
