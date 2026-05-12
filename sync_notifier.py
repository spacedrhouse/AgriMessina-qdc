"""Notifier centralizzato per eventi di sincronizzazione.

Le funzioni in `sync.py` e `pending_uploader.py` ricevono opzionalmente
un `SyncNotifier` e ne chiamano i metodi quando succedono eventi notevoli
(fantasmi rimossi, INSERT rifiutate, record gone dal server, ecc.).

La `FinestraPrincipale` connette i segnali del notifier alla status bar
e a eventuali popup. Le funzioni di sync non importano PyQt: ricevono solo
un oggetto duck-typed con i metodi giusti.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal


@dataclass
class SyncEvent:
    """Evento singolo di sync (per il log nel pannello dettagli)."""
    timestamp: datetime
    level: str   # "info" | "warning" | "error"
    message: str


class SyncNotifier(QObject):
    """Bus di segnali per gli eventi di sincronizzazione.

    Vive nel main thread (Qt single-thread). I metodi di notifica vengono
    chiamati dalle funzioni di sync (anche da background, ma in questa app
    siamo single-thread quindi non serve thread-safety).
    """

    # Stato globale di sync: "idle" | "syncing" | "error"
    state_changed = pyqtSignal(str)

    # Conteggio operazioni pendenti (modifiche locali non ancora inviate)
    pending_count_changed = pyqtSignal(int)

    # Evento generico per il log
    event_logged = pyqtSignal(object)  # SyncEvent

    # Notifiche specifiche (per popup mirati)
    insert_rejected = pyqtSignal(str, str, int)  # entity_type, summary, http_status
    record_gone = pyqtSignal(str, int, str)  # entity_type, entity_id, op_type
    ghosts_removed = pyqtSignal(str, int)  # entity_type, count

    # Trattamenti modificati: scatta su INSERT/UPDATE/DELETE (locale o da sync).
    # I pannelli magazzino vi si collegano per refreshare le giacenze, perché
    # ogni cambio trattamento riscrive registro_magazzino via sincronizza_scarico.
    trattamento_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._events: list[SyncEvent] = []
        self._max_events = 200
        # Timestamp dell'ultima reconcile riuscita. La UI lo usa per saltare
        # il pre-check su Modifica/Revisiona/Elimina quando i dati sono "freschi".
        self.last_successful_reconcile_ts: float = 0.0
        # Flag: la rete è attualmente raggiungibile?
        self.online: bool = True

    # ---- API per i moduli di sync (duck typing-safe) ----

    def _log(self, level: str, message: str) -> None:
        ev = SyncEvent(timestamp=datetime.now(), level=level, message=message)
        self._events.append(ev)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
        self.event_logged.emit(ev)

    def state(self, new_state: str) -> None:
        self.state_changed.emit(new_state)

    def pending(self, count: int) -> None:
        self.pending_count_changed.emit(count)

    def info(self, msg: str) -> None:
        self._log("info", msg)

    def warning(self, msg: str) -> None:
        self._log("warning", msg)

    def error(self, msg: str) -> None:
        self._log("error", msg)

    def on_insert_rejected(self, entity_type: str, summary: str, status: int) -> None:
        self.warning(f"{entity_type}: INSERT rifiutato dal server (HTTP {status}) — {summary}")
        self.insert_rejected.emit(entity_type, summary, status)

    def on_record_gone(self, entity_type: str, entity_id: int, op_type: str) -> None:
        self.warning(f"{entity_type} #{entity_id}: record cancellato dal server, {op_type} locale scartato")
        self.record_gone.emit(entity_type, entity_id, op_type)

    def on_ghosts_removed(self, entity_type: str, count: int) -> None:
        if count > 0:
            self.info(f"{entity_type}: {count} record fantasma rimossi")
            self.ghosts_removed.emit(entity_type, count)

    def recent_events(self, limit: int = 50) -> list[SyncEvent]:
        return list(reversed(self._events[-limit:]))
