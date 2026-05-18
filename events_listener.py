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
        # Client httpx attivo (creato in _listen_one_session, vivo solo per la
        # durata della sessione). Tenuto come attributo per poterlo chiudere
        # dal main thread in stop(): senza, un read bloccato in iter_lines()
        # non si scongela finché non scade il read_timeout (90s), e l'app
        # restava appesa sul shutdown / re-login per quei secondi.
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    def stop(self) -> None:
        """Richiede la chiusura del thread. Sveglia i wait di backoff e chiude
        il client httpx attivo: un eventuale iter_lines() bloccato esce con
        ReadError, fa partire l'except del run() che vede _stop_event=True
        e ritorna. Esito: il thread esce in <100ms invece di attendere il
        read_timeout (fino a 90s)."""
        self._stop_event.set()
        with self._client_lock:
            client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                # Chiusura idempotente: se il client è già chiuso o il
                # socket non risponde, log e via — non vogliamo che stop()
                # sollevi durante uno shutdown.
                log.debug("[events] errore chiudendo client httpx in stop() (atteso se già chiuso)")

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

        # Connect timeout 10s per non bloccare indefinitamente se il server è
        # giù. read=90s copre il keepalive del backend (30s di silenzio = ok),
        # senza il read resterebbe appeso all'infinito se la rete cade
        # brutalmente (drop senza FIN/RST).
        timeout = httpx.Timeout(connect=10.0, read=_READ_TIMEOUT_SECONDS,
                                write=10.0, pool=10.0)
        # Client esplicito (invece di httpx.stream module-level): così stop()
        # dal main thread può chiamare client.close() e sbloccare un
        # iter_lines() congelato. Pre-check: se stop è già stato richiesto
        # tra due sessioni, non creare nemmeno il client.
        if self._stop_event.is_set():
            return
        client = httpx.Client(timeout=timeout)
        with self._client_lock:
            self._client = client
        # Re-check dopo aver pubblicato il client: se stop() era già arrivato
        # tra il check di riga 162 e l'assegnamento sopra, avrebbe trovato
        # self._client=None e saltato il close(), bloccandoci poi su iter_lines().
        # Con questo re-check chiudiamo noi e usciamo subito.
        if self._stop_event.is_set():
            with self._client_lock:
                self._client = None
            client.close()
            return
        try:
            with client.stream("GET", url, headers=headers) as r:
                if r.status_code == 401:
                    # Token scaduto. Solleviamo: il chiamante (MainWindow)
                    # gestirà con un dialog di re-login. Niente retry su 401.
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
        finally:
            # Rilascia il riferimento PRIMA di close(): se stop() arriva ora,
            # chiama close() su un client già chiuso (idempotente, gestito).
            with self._client_lock:
                self._client = None
            try:
                client.close()
            except Exception:
                pass
