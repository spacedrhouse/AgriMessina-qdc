"""Listener SSE per ricevere push notifications dal backend.

Sostituisce il polling pesante (timer ogni 5s) con una connessione HTTP
keep-alive su `/events`. Quando il server pubblica un evento, il listener
emette un signal Qt che la MainWindow ascolta per triggere un pull mirato.

Architettura:
    Backend FastAPI /events ──SSE─▶ EventsListener (QThread)
                                         │ event_received(type, payload)
                                         ▼
                                    MainWindow._on_server_event
                                         │
                                         ▼
                                  pull_trattamenti_realtime  /  sync_all

Reconnect automatico con backoff esponenziale (1s → 30s max) se la rete cade
o il backend si riavvia. Stoppato pulitamente alla chiusura dell'app.
"""
from __future__ import annotations

import json
import logging
import threading

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)

# Timeout per la lettura della singola riga SSE. Il server manda un keepalive
# ogni 30s, quindi 90s è ampio margine per dichiarare la connessione morta.
_READ_TIMEOUT_SECONDS = 90.0
_BACKOFF_INITIAL_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0


class EventsListener(QThread):
    """Thread che mantiene la connessione SSE al backend.

    Signals:
        event_received(str event_type, dict payload):
            Emesso ad ogni evento ricevuto. Il caller può ignorare quelli
            che non gli interessano.
        connection_changed(bool connected):
            Cambio di stato connessione. Utile per badge UI offline/online.
        auth_expired():
            Il server ha respinto la connessione con 401: il token JWT è
            scaduto o revocato. MainWindow deve aprire il LoginDialog.
            Senza questo segnale il listener restava in retry-loop silente
            e il timer di reconcile (30 min) era l'unico a notarsene.
    """

    event_received = pyqtSignal(str, dict)
    connection_changed = pyqtSignal(bool)
    auth_expired = pyqtSignal()

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        # threading.Event invece di bool: permette di svegliare immediatamente
        # i wait di backoff quando il main thread chiama stop(). Senza,
        # un listener bloccato in time.sleep(30s) ritardava di 30s la
        # chiusura dell'app o il restart post-login.
        self._stop_event = threading.Event()
        self._connected = False

    def stop(self) -> None:
        """Richiede la chiusura del thread. Sveglia immediatamente eventuali
        wait di backoff così il thread esce in <1s."""
        self._stop_event.set()

    def run(self) -> None:
        backoff = _BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            if not self.api.is_authenticated:
                # Senza token, aspettiamo. wait() torna True se stop richiesto.
                # NOTA: non raddoppiamo backoff qui — l'attesa è "tecnica" (manca
                # auth), non un errore. Senza reset, dopo qualche minuto offline il
                # backoff arrivava a 30s e il listener restava idle fino a 30s
                # post-relogin invece di connettersi subito.
                if self._stop_event.wait(timeout=_BACKOFF_INITIAL_SECONDS):
                    return
                continue

            try:
                self._listen_one_session()
                # Sessione chiusa pulitamente (es. server riavviato): reset del backoff.
                backoff = _BACKOFF_INITIAL_SECONDS
            except httpx.HTTPStatusError as e:
                # 401/403 dal server: token scaduto o revocato. Inutile riprovare,
                # serve il re-login. Emetti il segnale a MainWindow ed esci dal
                # ciclo: la finestra fermerà esplicitamente questo thread prima
                # di crearne uno nuovo post-login.
                self._set_connected(False)
                status = getattr(e.response, "status_code", None)
                if status in (401, 403):
                    log.warning("[events] auth scaduta (HTTP %s): triggero re-login", status)
                    self.auth_expired.emit()
                    return
                # Altri HTTPStatusError: tratta come transient e riprova.
                log.warning("[events] HTTP %s: riconnetto fra %.1fs", status, backoff)
                if self._stop_event.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            except Exception as e:
                self._set_connected(False)
                if self._stop_event.is_set():
                    return
                log.warning(
                    "[events] listener errore: %s — riconnetto fra %.1fs",
                    e, backoff,
                )
                if self._stop_event.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    def _set_connected(self, value: bool) -> None:
        if value != self._connected:
            self._connected = value
            self.connection_changed.emit(value)

    def _listen_one_session(self) -> None:
        url = self.api.base_url.rstrip("/") + "/events"
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        token = getattr(self.api, "_token", None)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # timeout=None per il read (lo stream resta aperto). Connect timeout
        # 10s per non bloccare indefinitamente se il server è giù.
        timeout = httpx.Timeout(connect=10.0, read=_READ_TIMEOUT_SECONDS,
                                write=10.0, pool=10.0)
        with httpx.stream("GET", url, headers=headers, timeout=timeout) as r:
            if r.status_code == 401:
                # Token scaduto. Solleviamo: il chiamante (MainWindow) gestirà
                # con un dialog di re-login. Niente retry su 401, sarebbe loop.
                raise httpx.HTTPStatusError("401", request=r.request, response=r)
            r.raise_for_status()
            self._set_connected(True)
            log.info("[events] stream connesso")
            for line in r.iter_lines():
                if self._stop_event.is_set():
                    return
                if not line or line.startswith(":"):
                    # Keepalive comment o riga vuota separatore.
                    continue
                if line.startswith("data: "):
                    payload_str = line[len("data: "):]
                    try:
                        msg = json.loads(payload_str)
                    except json.JSONDecodeError:
                        log.warning("[events] payload non-JSON ignorato: %r", payload_str)
                        continue
                    event_type = msg.get("type", "")
                    data = msg.get("payload") or {}
                    if event_type:
                        self.event_received.emit(event_type, data)
        # Uscita pulita dal context manager = server ha chiuso lo stream.
        self._set_connected(False)
