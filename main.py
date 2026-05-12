"""Entry point dell'app desktop AgriMessina QDC.

Flusso al primo avvio (utente non loggato):
  1. Splash screen
  2. Login dialog → richiede email/password al backend
  3. Sync iniziale dei dati dal server (può fallire silenziosamente: si lavora con i dati locali)
  4. Finestra principale

Flusso ai successivi avvii (token salvato):
  1. Splash screen
  2. Tentativo di sync (background)
  3. Finestra principale

L'app NON parla mai direttamente con MySQL. Tutto passa per ApiClient (REST).
Il SQLite locale è solo per la lettura veloce/offline.
"""
from __future__ import annotations
import sys
import os
import time

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QSplashScreen, QLabel, QFrame, QPushButton, QMessageBox,
    QDialog, QTextEdit, QDialogButtonBox, QSystemTrayIcon, QMenu
)
from PyQt6.QtGui import QColor, QPixmap, QIcon, QAction
from PyQt6.QtCore import Qt, QTimer, QSettings

from dotenv import load_dotenv

from api_client import ApiClient, NetworkError, NotAuthenticatedError
from events_listener import EventsListener
from update_checker import UpdateCheckWorker, UpdateInfo
try:
    from _version import __version__ as APP_VERSION
except ImportError:
    APP_VERSION = "0.0.0-dev"
from local_db import get_engine, init_local_database, reset_sync_state
from sync import sync_all, reconcile_with_server, pull_trattamenti
from pending_uploader import upload_pending
from login_dialog import LoginDialog
from sync_notifier import SyncNotifier
from app_logging import setup_logging, get_logger
from config import CONFIG

log = get_logger(__name__)

# Le UI esistenti restano (aggiornate per leggere dal SQLite locale).
from ui_trattamenti import SchedaOperazioni
from ui_magazzino import PannelloProdotti
from ui_anagrafiche import PannelloAziende, PannelloAgri, PannelloContrade, PannelloTendoni
from ui_core import STYLE_AGRIMESSINA



# ----------------------------- helper ---------------------------------------

def get_risorsa(nome_file: str) -> str:
    """Risolve i percorsi delle risorse, gestendo PyInstaller (_MEIPASS)."""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.abspath(os.path.join(base_path, nome_file)))


def get_api_base_url() -> str:
    """Legge l'URL del backend da .env, con default sensato."""
    return os.environ.get("API_BASE_URL", "https://api.agrimessina.it").rstrip("/")


# ----------------------------- title bar ------------------------------------

class DraggableTitleBar(QWidget):
    """Barra del titolo personalizzata (frameless window). Drag + double-click maximize."""
    def __init__(self, parent_window, parent=None):
        super().__init__(parent)
        self.parent_window = parent_window

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.parent_window.windowHandle()
            if handle:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.parent_window._toggle_maximize()
            event.accept()


# ----------------------------- finestra principale --------------------------

class FinestraPrincipale(QMainWindow):
    def __init__(self, api: ApiClient, engine, notifier: SyncNotifier):
        super().__init__()
        self.api = api
        self.engine = engine
        self.notifier = notifier

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("AgriMessina QDC")
        # MODIFICA QUI: usa icona.ico invece di icona.png
        self.setWindowIcon(QIcon(get_risorsa("icona.ico")))

        # Geometria: prima prova a ripristinarla dalle preferenze utente
        self._qsettings = QSettings("AgriMessina", "QDC")
        saved_geom = self._qsettings.value("window/geometry")
        if saved_geom is not None:
            self.restoreGeometry(saved_geom)
        else:
            self.setGeometry(100, 100, 1400, 900)

        self.db = None
        self._is_syncing = False  # Flag per evitare accavallamenti
        self._sync_state = "idle"
        self._pending_count = 0

        self._build_ui()
        self._wire_notifier()

        # Backoff offline: si attiva quando reconcile/pull falliscono per rete down
        self._offline_backoff_ms = CONFIG.offline_backoff_initial_ms

        # --- Timer per Auto-Sync in background ---
        # I timer servono ora solo come FALLBACK: la sincronizzazione principale
        # è guidata dagli eventi SSE pushati dal server (vedi EventsListener).
        # Per questo motivo `autosync_ms` e `reconcile_ms` possono essere
        # impostati molto più alti (vedi config.py): l'app non polla più ogni
        # 5s ma reagisce in millisecondi ai cambi server-side.
        self.timer_autosync = QTimer(self)
        self.timer_autosync.timeout.connect(self._check_and_sync)
        self.timer_autosync.start(CONFIG.autosync_ms)

        # Timer veloce per aggiornare la status bar (solo COUNT, no sync)
        self.timer_status = QTimer(self)
        self.timer_status.timeout.connect(self._aggiorna_pending_count)
        self.timer_status.start(CONFIG.status_update_ms)

        # Timer di reconciliation: full-sync col server (upsert + delete fantasmi)
        self.timer_reconcile = QTimer(self)
        self.timer_reconcile.timeout.connect(self._esegui_reconcile)
        self.timer_reconcile.start(CONFIG.reconcile_ms)

        # --- SSE listener: push real-time dal server ---
        self.events_listener = EventsListener(self.api, parent=self)
        self.events_listener.event_received.connect(self._on_server_event)
        self.events_listener.event_received.connect(self._on_event_notify)
        self.events_listener.connection_changed.connect(self._on_events_connection)
        self.events_listener.start()

        # --- Tray icon + notifiche sistema ---
        # Mostra notifiche native (toast Linux/Windows) per gli eventi
        # SSE rilevanti. Setting `notifications_enabled` persistito in
        # QSettings: l'utente può disabilitarle da menu tray.
        self._setup_tray()
        # Throttle: tempo dell'ultima notifica per evitare spam se arrivano
        # eventi a raffica (es. import bulk).
        self._last_notify_ts: float = 0.0

        # --- Check aggiornamenti GitHub Releases ---
        # Avviato in background, non blocca lo startup. Se trova una versione
        # nuova mostra un dialog non invasivo (l'utente può posticipare).
        self._update_worker = UpdateCheckWorker(APP_VERSION, parent=self)
        self._update_worker.update_available.connect(self._on_update_available)
        self._update_worker.start()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- Title bar
        title_bar = DraggableTitleBar(self)
        title_bar.setFixedHeight(40)
        title_bar.setObjectName("CustomTitleBar")
        title_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        title_bar.setStyleSheet("background-color: #FFFFFF; border-bottom: 1px solid #EAEAEA;")

        ltitle = QHBoxLayout(title_bar)
        ltitle.setContentsMargins(15, 0, 10, 0)

        lbl = QLabel(f"AgriMessina QDC — {self.api.username or 'Dashboard'}")
        lbl.setStyleSheet("color: #757575; font-size: 12px; font-weight: bold; background: transparent;")
        ltitle.addWidget(lbl)
        ltitle.addStretch()

        # Pulsante logout
        btn_logout = QPushButton("Esci")
        btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_logout.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #C62828; "
            "font-size: 12px; padding: 8px 12px; }"
            "QPushButton:hover { background-color: #FFEBEE; }"
        )
        btn_logout.clicked.connect(self._on_logout_clicked)
        ltitle.addWidget(btn_logout)

        # Window controls
        for txt, slot, hover in [
            ("–", self.showMinimized, "#E0E0E0"),
            ("▢", self._toggle_maximize, "#E0E0E0"),
            ("✕", self.close, "#E81123"),
        ]:
            b = QPushButton(txt)
            b.setFixedSize(45, 40)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            color = "white" if hover == "#E81123" else "#212121"
            b.setStyleSheet(
                f"QPushButton {{ border: none; font-size: 16px; background: transparent; color: #212121; }}"
                f"QPushButton:hover {{ background-color: {hover}; color: {color}; }}"
            )
            b.clicked.connect(slot)
            ltitle.addWidget(b)

        main_layout.addWidget(title_bar)

        # --- Status bar di sincronizzazione (sotto la title bar)
        self.status_bar = QFrame()
        self.status_bar.setObjectName("SyncStatusBar")
        self.status_bar.setFixedHeight(26)
        self.status_bar.setStyleSheet(
            "#SyncStatusBar { background-color: #FAFAFA; border-bottom: 1px solid #EAEAEA; }"
        )
        lstatus = QHBoxLayout(self.status_bar)
        lstatus.setContentsMargins(15, 0, 15, 0)
        lstatus.setSpacing(12)

        self.lbl_sync_state = QLabel("🟢 Sincronizzato")
        self.lbl_sync_state.setStyleSheet("color: #424242; font-size: 11px; background: transparent;")
        lstatus.addWidget(self.lbl_sync_state)

        # Indicatore connessione SSE (push real-time dal server).
        # Aggiornato da _on_events_connection. Pallino + tooltip esplicativo,
        # così l'utente capisce a colpo d'occhio se sta ricevendo gli update
        # in tempo reale o sta operando solo via polling fallback.
        self.lbl_sse_state = QLabel("⚪ Connessione…")
        self.lbl_sse_state.setStyleSheet("color: #757575; font-size: 11px; background: transparent;")
        self.lbl_sse_state.setToolTip(
            "Stato connessione push real-time col server.\n"
            "Verde: ricevi gli update immediatamente.\n"
            "Grigio: stai usando solo il polling periodico come fallback."
        )
        lstatus.addWidget(self.lbl_sse_state)

        self.lbl_sync_pending = QLabel("")
        self.lbl_sync_pending.setStyleSheet("color: #757575; font-size: 11px; background: transparent;")
        lstatus.addWidget(self.lbl_sync_pending)

        lstatus.addStretch()

        self.btn_sync_log = QPushButton("📋 Log sync")
        self.btn_sync_log.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sync_log.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #1976D2; "
            "font-size: 11px; padding: 2px 8px; }"
            "QPushButton:hover { text-decoration: underline; }"
        )
        self.btn_sync_log.clicked.connect(self._mostra_log_sync)
        lstatus.addWidget(self.btn_sync_log)

        main_layout.addWidget(self.status_bar)

        # --- Body: sidebar + stacked
        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        main_layout.addLayout(body_layout)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(240)
        lside = QVBoxLayout(sidebar)

        lbl_logo = QLabel()
        pix = QPixmap(get_risorsa("splash.png"))
        if not pix.isNull():
            lbl_logo.setPixmap(pix.scaled(200, 100, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            lbl_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_logo.setStyleSheet("background: transparent; padding: 20px 10px;")
            lside.addWidget(lbl_logo)

        self.btn_group = []
        menu = [
            ("📊 Storico", 0),
            ("✅ Revisionati", 1),
            ("📦 Mag. Agrimessina", 2),
            ("📦 Mag. La Gazzella", 3),
            ("📦 Mag. Messina Alfio", 4),
            ("🍇 Tendoni", 5),
            ("📍 Contrade", 6),
            ("🌍 Agri", 7),
            ("🏢 Aziende", 8),
        ]
        for testo, idx in menu:
            b = QPushButton(testo)
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.clicked.connect(lambda _, i=idx: self._cambia_pagina(i))
            lside.addWidget(b)
            self.btn_group.append(b)

        lside.addStretch()
        body_layout.addWidget(sidebar)

        # Pagine
        self.pagine = QStackedWidget()
        self.pagine.addWidget(SchedaOperazioni(self.engine, self.db, tipo_vista="PROPOSTE", api=self.api, notifier=self.notifier))
        self.pagine.addWidget(SchedaOperazioni(self.engine, self.db, tipo_vista="AUTORIZZATI", api=self.api, notifier=self.notifier))
        # Tre vedute magazzino, una per azienda. "Messina Alfio" include
        # automaticamente l'alias Deflorio Ciccopinto (vedi WAREHOUSE_ALIASES).
        self.pagine.addWidget(PannelloProdotti(self.engine, self.db, azienda_filter="Agrimessina", api=self.api))
        self.pagine.addWidget(PannelloProdotti(self.engine, self.db, azienda_filter="La Gazzella", api=self.api))
        self.pagine.addWidget(PannelloProdotti(self.engine, self.db, azienda_filter="Messina Alfio", api=self.api))
        self.pagine.addWidget(PannelloTendoni(self.engine, self.db))
        self.pagine.addWidget(PannelloContrade(self.engine, self.db))
        self.pagine.addWidget(PannelloAgri(self.engine, self.db))
        self.pagine.addWidget(PannelloAziende(self.engine, self.db))
        body_layout.addWidget(self.pagine)

        self.btn_group[0].setChecked(True)
        self.setStyleSheet(STYLE_AGRIMESSINA)

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _cambia_pagina(self, idx: int):
        """Cambia tab della sidebar e refresha il pannello target.

        Le query SQL dei pannelli vengono eseguite solo a `aggiorna_dati()`
        e al construttore. Refreshare al cambio pagina garantisce che vediamo
        sempre lo stato corrente del DB (utile dopo cleanup background o sync).
        """
        self.pagine.setCurrentIndex(idx)
        page = self.pagine.widget(idx)
        if page is not None and hasattr(page, "aggiorna_dati"):
            try:
                page.aggiorna_dati()
            except Exception as e:
                log.error("Refresh pagina %d al cambio tab: %s", idx, e)

    def closeEvent(self, event):
        """Salva geometria finestra e ferma i timer prima di chiudere."""
        try:
            self._qsettings.setValue("window/geometry", self.saveGeometry())
        except Exception as e:
            log.warning("Salvataggio geometria fallito: %s", e)
        for timer in (getattr(self, "timer_autosync", None),
                      getattr(self, "timer_status", None),
                      getattr(self, "timer_reconcile", None)):
            if timer is not None and timer.isActive():
                timer.stop()
        # Stop SSE listener pulitamente, altrimenti l'app resta appesa sul thread.
        events_listener = getattr(self, "events_listener", None)
        if events_listener is not None and events_listener.isRunning():
            events_listener.stop()
            events_listener.wait(2000)
        # Nascondi tray icon: senza, su alcuni DE resta "orfana" finché non
        # passi col mouse sopra.
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.hide()
        # Chiude il pool httpx — evita "Unclosed client" warning a shutdown.
        try:
            self.api.close()
        except Exception:
            pass
        log.info("Chiusura applicazione")
        super().closeEvent(event)

    # ---- Notifier wiring & status bar ----------------------------------

    def _wire_notifier(self):
        self.notifier.state_changed.connect(self._on_sync_state)
        self.notifier.pending_count_changed.connect(self._on_pending_count)
        self.notifier.insert_rejected.connect(self._on_insert_rejected)
        self.notifier.record_gone.connect(self._on_record_gone)
        # Quando un trattamento cambia (locale o da sync), refresh dei pannelli
        # magazzino: ogni cambio trattamento riscrive registro_magazzino via
        # sincronizza_scarico, quindi le giacenze possono essere variate.
        self.notifier.trattamento_changed.connect(self._refresh_pannelli_magazzino)
        # ghosts_removed lo lasciamo silente (solo log)

    # Indici delle pagine magazzino nel QStackedWidget. Sincronizzati col
    # menù sidebar in _build_ui (Agrimessina/La Gazzella/Messina Alfio).
    PAGINE_MAGAZZINO = (2, 3, 4)

    def _refresh_pannelli_magazzino(self):
        """Refresha le 3 vedute magazzino. Chiamato via signal trattamento_changed."""
        for i in self.PAGINE_MAGAZZINO:
            if i < self.pagine.count():
                page = self.pagine.widget(i)
                if hasattr(page, "aggiorna_dati"):
                    try:
                        page.aggiorna_dati()
                    except Exception as e:
                        log.error("Refresh magazzino pagina %d: %s", i, e)

    def _on_sync_state(self, state: str):
        self._sync_state = state
        self._refresh_status_label()

    def _on_pending_count(self, count: int):
        self._pending_count = count
        self._refresh_status_label()

    def _refresh_status_label(self):
        if self._sync_state == "syncing":
            self.lbl_sync_state.setText("🟡 Sincronizzazione in corso…")
        elif self._sync_state == "error":
            self.lbl_sync_state.setText("🔴 Errore di sincronizzazione")
        elif self._sync_state == "offline":
            self.lbl_sync_state.setText("🔌 Offline — i dati saranno sincronizzati al ritorno della rete")
        else:
            self.lbl_sync_state.setText("🟢 Sincronizzato")
        if self._pending_count > 0:
            self.lbl_sync_pending.setText(f"· {self._pending_count} modifica/e da inviare")
        else:
            self.lbl_sync_pending.setText("")

    def _on_insert_rejected(self, entity_type: str, summary: str, status: int):
        QMessageBox.warning(
            self, "Sincronizzazione",
            f"L'inserimento di «{summary}» ({entity_type}) è stato rifiutato "
            f"dal server (HTTP {status}).\nLa voce locale è stata rimossa.\n\n"
            "Probabili cause: già presente sul server, o dati non validi.",
        )

    def _on_record_gone(self, entity_type: str, entity_id: int, op_type: str):
        QMessageBox.information(
            self, "Sincronizzazione",
            f"Il record {entity_type} #{entity_id} è stato cancellato da un altro "
            f"utente. L'operazione {op_type} locale è stata annullata.",
        )

    def _mostra_log_sync(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Log di sincronizzazione")
        dlg.resize(700, 400)
        layout = QVBoxLayout(dlg)
        txt = QTextEdit()
        txt.setReadOnly(True)
        events = self.notifier.recent_events(100)
        if not events:
            txt.setPlainText("Nessun evento di sync registrato.")
        else:
            righe = []
            for ev in events:
                icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(ev.level, "•")
                righe.append(f"{ev.timestamp.strftime('%H:%M:%S')} {icon} {ev.message}")
            txt.setPlainText("\n".join(righe))
        layout.addWidget(txt)
        btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn.rejected.connect(dlg.reject)
        btn.accepted.connect(dlg.accept)
        layout.addWidget(btn)
        dlg.exec()

    def _aggiorna_pending_count(self):
        try:
            from sqlalchemy import text
            with self.engine.connect() as conn:
                n = conn.execute(text("SELECT COUNT(*) FROM pending_operations")).scalar() or 0
            self.notifier.pending(int(n))
        except Exception as e:
            # Errore non bloccante (es. DB lockato 1s): log debug, no popup.
            log.debug("pending_count fallito: %s", e)

    def _esegui_reconcile(self):
        """Allineamento automatico: scarica lo stato corrente dal server e
        sincronizza il DB locale (upsert + delete dei fantasmi).

        NOTA D3: l'esecuzione è sincrona (blocca la UI per la durata della
        reconcile, tipicamente 1-5s). Eseguirla in QThread non è sicuro
        finché le scritture su anagrafiche/prodotti/magazzino passano dai
        trigger SQLite + flag `downloading`: durante reconcile background
        le UI write verrebbero saltate dai trigger e poi cancellate come
        fantasmi al ciclo successivo. Risolvibile convertendo tutte le
        scritture a enqueue esplicito (refactor pianificato).
        """
        if not self.api.is_authenticated or self._is_syncing:
            return

        import time
        self._is_syncing = True
        self.notifier.state("syncing")
        try:
            result = reconcile_with_server(self.api, self.engine, notifier=self.notifier)
            if result.ok:
                self.notifier.online = True
                self.notifier.last_successful_reconcile_ts = time.time()
                self.notifier.state("idle")
                for i in range(self.pagine.count()):
                    page = self.pagine.widget(i)
                    if hasattr(page, "aggiorna_dati"):
                        try:
                            page.aggiorna_dati()
                        except Exception as e:
                            log.error("Reconcile-UI: errore refresh: %s", e)
            else:
                msg_low = result.message.lower()
                if "sessione scaduta" in msg_low or "sessione non valida" in msg_low:
                    # _is_syncing viene resettato dal finally; non duplicare qui.
                    self._handle_session_expired()
                    return
                if "rete" in msg_low:
                    self.notifier.online = False
                    self.notifier.state("offline")
                else:
                    self.notifier.state("error")
                    self.notifier.warning(f"Reconcile: {result.message}")
        except NotAuthenticatedError:
            self._handle_session_expired()
            return
        except Exception as e:
            self.notifier.error(f"Reconcile fallita: {e}")
            self.notifier.state("error")
        finally:
            self._is_syncing = False
            self._aggiorna_pending_count()

    def _next_autosync_interval(self) -> int:
        """Calcola il prossimo intervallo. Se offline applica backoff esponenziale
        per non sbattere ripetutamente contro la rete morta."""
        if self.notifier.online:
            # Reset al ritorno della rete
            self._offline_backoff_ms = CONFIG.offline_backoff_initial_ms
            return CONFIG.autosync_ms
        else:
            self._offline_backoff_ms = min(
                self._offline_backoff_ms * 2,
                CONFIG.offline_backoff_max_ms,
            )
            return self._offline_backoff_ms

    def _check_and_sync(self):
        """Tick periodico (3s) per UPLOAD-only.

        Il PULL è ora gestito da SSE (events_listener) — niente polling per
        scaricare. Qui ci limitiamo a controllare se l'utente ha generato
        operazioni offline da spedire al server; se sì, le inviamo.

        Senza questo polling rapido, dopo aver cancellato un trattamento sul
        desktop l'operazione resterebbe in coda finché il timer di reconcile
        (30 min) non se ne accorge — esperienza terribile. Il SELECT COUNT è
        microsecondi quindi 3s è gratis quando la coda è vuota.
        """
        if not self.api.is_authenticated or self._is_syncing:
            return

        try:
            from sqlalchemy import text
            with self.engine.connect() as conn:
                da_inviare = conn.execute(text("SELECT COUNT(*) FROM pending_operations")).scalar()

            self.notifier.pending(int(da_inviare or 0))

            if da_inviare > 0:
                self.timer_autosync.stop()
                self._esegui_sync(silenzioso=True)
                self.timer_autosync.start(self._next_autosync_interval())
            # Se da_inviare == 0 non facciamo nulla: SSE gestisce il pull.

        except Exception as e:
            if "locked" not in str(e).lower():
                log.warning("Auto-sync errore: %s", e)
            if not self.timer_autosync.isActive():
                self.timer_autosync.start(self._next_autosync_interval())

    def _pull_trattamenti_realtime(self):
        """Pull leggero dei trattamenti per quasi-real-time updates da altri
        client. Refresha la UI solo se sono arrivati cambiamenti."""
        self._is_syncing = True
        try:
            aggiunti, cancellati = pull_trattamenti(self.api, self.engine, notifier=self.notifier)
            if aggiunti or cancellati:
                self.notifier.info(f"Pull: +{aggiunti}/-{cancellati} trattamenti")
                # Refresh delle pagine trattamenti (Storico/Revisionati)
                for i in (0, 1):
                    if i < self.pagine.count():
                        page = self.pagine.widget(i)
                        if hasattr(page, "aggiorna_dati"):
                            try:
                                page.aggiorna_dati()
                            except Exception as e:
                                log.error("Pull-UI: errore refresh: %s", e)
                # Notifica per i pannelli magazzino: pull_trattamenti ha già
                # invocato sincronizza_scarico tramite _upsert_trattamenti,
                # quindi registro_magazzino può essere variato.
                self.notifier.trattamento_changed.emit()
                # Online state
                self.notifier.online = True
                self.notifier.state("idle")
            else:
                # Nessuna modifica: stato comunque "idle" se la rete risponde
                if not self.notifier.online:
                    self.notifier.online = True
                    self.notifier.state("idle")
        except NotAuthenticatedError:
            # Token JWT scaduto: niente più retry silenziosi, apri il dialog.
            self._is_syncing = False
            self._handle_session_expired()
            return
        except NetworkError as e:
            # NetworkError eredita da ApiError ma significa "rete giù", non
            # un errore di app: marca offline e non logga warning per non
            # spammare il log con ogni tick offline.
            self.notifier.online = False
            self.notifier.state("offline")
            log.debug("Pull realtime offline: %s", e)
        except Exception as e:
            log.warning("Pull errore: %s", e)
        finally:
            self._is_syncing = False

    def _esegui_sync(self, silenzioso=False):
        """Logica unificata di sincronizzazione (Upload + Download + Refresh Viste)."""
        self._is_syncing = True
        self.notifier.state("syncing")
        result = None
        try:
            # 1. Esegui l'upload dei dati pendenti
            try:
                sent, residual, had_insert = upload_pending(self.api, self.engine, notifier=self.notifier)
            except NotAuthenticatedError:
                self._handle_session_expired()
                return

            if sent > 0:
                log.info("Uploader: inviate %d operazioni, %d in coda residue", sent, residual)
                self.notifier.info(f"Inviate {sent} operazioni al server")

            # 2. Se è avvenuto un ID Swap (had_insert), aggiorna SUBITO la UI
            if had_insert:
                from local_db import reset_sync_state
                reset_sync_state(self.engine)

                # Forza il refresh delle card per allineare gli ID visibili a quelli nuovi del DB
                for i in range(self.pagine.count()):
                    page = self.pagine.widget(i)
                    if hasattr(page, "aggiorna_dati"):
                        try:
                            page.aggiorna_dati()
                        except Exception as e:
                            log.error("Sync-UI: errore refresh durante ID Swap: %s", e)

            # 3. Procedi con il download dei nuovi dati dal Cloud
            result = sync_all(self.api, self.engine)

            # 4. Aggiorna le viste per mostrare i dati scaricati dal server
            if result.ok:
                for i in range(self.pagine.count()):
                    page = self.pagine.widget(i)
                    if hasattr(page, "aggiorna_dati"):
                        try:
                            page.aggiorna_dati()
                        except Exception as e:
                            log.error("Refresh error on %r: %s", page, e)
        except Exception as e:
            log.error("Sync errore: %s", e, exc_info=True)
            self.notifier.error(f"Sync fallito: {e}")
        finally:
            # Senza il finally, una qualsiasi exception dopo l'upload lasciava
            # _is_syncing=True per sempre: i tick di _check_and_sync sarebbero
            # tornati subito e l'app restava in stato "syncing" fino al riavvio.
            self._is_syncing = False

        if result is None:
            return

        if result.ok:
            self.notifier.online = True
            self.notifier.state("idle")
        else:
            msg_low = result.message.lower()
            if "sessione scaduta" in msg_low or "sessione non valida" in msg_low:
                # Token scaduto: niente più retry/loop, chiediamo il re-login.
                self._aggiorna_pending_count()
                self._handle_session_expired()
                return
            if "rete" in msg_low:
                self.notifier.online = False
                self.notifier.state("offline")
            else:
                self.notifier.state("error")
                self.notifier.warning(f"Sync: {result.message}")
        self._aggiorna_pending_count()

        # Mostra il popup solo se l'operazione è stata avviata manualmente
        if not silenzioso:
            if result.ok:
                QMessageBox.information(self, "Sincronizzazione", "✅ Dati aggiornati.")
            else:
                QMessageBox.warning(self, "Sincronizzazione", f"❌ {result.message}")

    def _on_server_event(self, event_type: str, payload: dict):
        """Riceve un evento SSE dal backend e triggera un pull immediato.

        Esempi di event_type: trattamenti_changed, magazzino_changed,
        avvisi_changed. Il pull leggero (pull_trattamenti) è preferibile a
        un full sync_all perché bastano i delta — il timer di reconcile a
        intervallo lungo continua a fare il pulizia periodica fantasmi.
        """
        if self._is_syncing:
            # Già un sync in corso: l'evento verrà coperto dal sync corrente
            # o dal prossimo trigger. Niente race.
            return
        log.debug("[events] ricevuto %s: %s", event_type, payload)
        try:
            self._pull_trattamenti_realtime()
        except Exception as e:
            log.warning("[events] pull immediato fallito: %s", e)

    def _on_events_connection(self, connected: bool):
        """Aggiorna lo stato online/offline in base alla connessione SSE."""
        if connected:
            self.notifier.online = True
            self.notifier.state("idle")
            # Pallino verde, real-time attivo.
            if hasattr(self, "lbl_sse_state"):
                self.lbl_sse_state.setText("🟢 Real-time")
                self.lbl_sse_state.setStyleSheet(
                    "color: #2E7D32; font-size: 11px; background: transparent;"
                )
        else:
            # Disconnessione: il listener si auto-riconnette con backoff,
            # i timer di fallback coprono il vuoto temporaneo. Indichiamo
            # all'utente che siamo in modalità degraded (solo polling).
            if hasattr(self, "lbl_sse_state"):
                self.lbl_sse_state.setText("⚪ Polling")
                self.lbl_sse_state.setStyleSheet(
                    "color: #9E9E9E; font-size: 11px; background: transparent;"
                )

    # ---- System tray + notifiche -------------------------------------

    # Throttle minimo fra due notifiche consecutive (evita spam burst).
    _NOTIFY_THROTTLE_S = 5.0

    def _setup_tray(self):
        """Crea l'icona di system tray con un menu minimal (toggle + esci).
        Su sistemi senza tray (alcuni WM minimalisti) `isSystemTrayAvailable`
        ritorna False e saltiamo gracefully."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("[tray] non disponibile sul sistema, skip")
            self.tray = None
            return

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(QIcon(get_risorsa("icona.ico")))
        self.tray.setToolTip("AgriMessina QDC")

        menu = QMenu()
        act_show = QAction("Mostra finestra", self)
        act_show.triggered.connect(self._tray_show_window)
        menu.addAction(act_show)

        self.act_notify_toggle = QAction("Notifiche attive", self, checkable=True)
        self.act_notify_toggle.setChecked(self._notifications_enabled())
        self.act_notify_toggle.toggled.connect(self._toggle_notifications)
        menu.addAction(self.act_notify_toggle)

        menu.addSeparator()
        act_quit = QAction("Esci", self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        # Doppio click sull'icona riporta in foreground la finestra.
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show_window()

    def _tray_show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _notifications_enabled(self) -> bool:
        # QSettings restituisce string in alcuni casi → cast esplicito.
        v = self._qsettings.value("notifications/enabled", True)
        return str(v).lower() not in ("false", "0", "no")

    def _toggle_notifications(self, enabled: bool):
        self._qsettings.setValue("notifications/enabled", enabled)
        log.info("[tray] notifiche %s", "attive" if enabled else "disattivate")

    def _on_event_notify(self, event_type: str, payload: dict):
        """Mostra una notifica nativa per gli eventi rilevanti dal server.

        Filtro: solo eventi trattamenti_changed con op INSERT/DELETE (le
        UPDATE generano troppo rumore e l'utente le vede comunque nella UI).
        Throttle: niente notifiche più frequenti di una ogni 5s.
        """
        import time
        if not getattr(self, "tray", None):
            return
        if not self._notifications_enabled():
            return
        if event_type != "trattamenti_changed":
            return
        op = (payload or {}).get("op")
        if op not in ("INSERT", "DELETE"):
            return

        now = time.time()
        if now - self._last_notify_ts < self._NOTIFY_THROTTLE_S:
            return
        self._last_notify_ts = now

        tid = (payload or {}).get("id", "?")
        if op == "INSERT":
            title = "Nuovo trattamento"
            body = f"Trattamento #{tid} aggiunto da un altro client."
        else:
            title = "Trattamento cancellato"
            body = f"Trattamento #{tid} rimosso da un altro client."

        # 4000 ms = durata default del toast su Windows/Linux. Su macOS
        # è ignorato (li gestisce il Notification Center).
        self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _on_update_available(self, info: UpdateInfo):
        """Notifica all'utente che c'è una versione più recente disponibile.

        Non forziamo nulla: chiediamo se vuole aprire la pagina di download.
        L'app continua a funzionare normalmente anche se l'utente sceglie
        "più tardi" — il check si ripeterà al prossimo avvio.
        """
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("Aggiornamento disponibile")
        notes_excerpt = (info.release_notes or "").strip().split("\n\n", 1)[0]
        if len(notes_excerpt) > 400:
            notes_excerpt = notes_excerpt[:400] + "…"
        msg.setText(
            f"<b>AgriMessina QDC {info.latest_version}</b> è disponibile.<br>"
            f"Stai usando la versione <code>{APP_VERSION}</code>."
        )
        if notes_excerpt:
            msg.setInformativeText(f"Novità:\n{notes_excerpt}")
        btn_download = msg.addButton("Scarica", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Più tardi", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() is btn_download:
            import webbrowser
            # Se l'asset .exe non c'è nella release (es. build CI fallita),
            # ripieghiamo sulla pagina della release: l'utente vede comunque
            # cosa c'è e può scaricare manualmente.
            url = info.download_url or info.release_url
            webbrowser.open(url)

    def _handle_session_expired(self):
        """Gestisce la scadenza del token JWT.

        Stoppa i timer di sync, scarta il token salvato, apre il LoginDialog
        e riavvia i timer al re-login. Senza questa gestione il client entrava
        in loop infinito di 401 (visibile come popup "Sessione scaduta" su ogni
        ciclo di autosync da 5s).
        """
        # Reentrancy guard: se il dialog è già aperto, non aprirne un secondo.
        if getattr(self, "_relogin_in_progress", False):
            return
        self._relogin_in_progress = True
        try:
            log.warning("Sessione scaduta: stoppo i timer e chiedo nuovo login")
            for timer in (getattr(self, "timer_autosync", None),
                          getattr(self, "timer_reconcile", None),
                          getattr(self, "timer_status", None)):
                if timer and timer.isActive():
                    timer.stop()

            # Ferma anche il listener SSE: con token scaduto continuerebbe a
            # provare in loop (anche se il backoff lo rallenta).
            events_listener = getattr(self, "events_listener", None)
            if events_listener and events_listener.isRunning():
                events_listener.stop()
                events_listener.wait(2000)

            self.api.logout()

            from login_dialog import LoginDialog
            login = LoginDialog(self.api)
            if login.exec() != login.DialogCode.Accepted:
                # L'utente ha annullato: meglio chiudere che lasciare l'app
                # in stato inconsistente (ogni richiesta sarebbe 401).
                self.close()
                return

            # Login OK: riprendiamo i cicli di sync.
            for timer, interval in (
                (getattr(self, "timer_autosync", None), CONFIG.autosync_ms),
                (getattr(self, "timer_reconcile", None), CONFIG.reconcile_ms),
                (getattr(self, "timer_status", None), CONFIG.status_update_ms),
            ):
                if timer:
                    timer.start(interval)

            # Rilancia il listener SSE con il nuovo token. L'istanza precedente
            # è già stata stoppata sopra: la marchiamo per la cancellazione,
            # altrimenti resterebbe come child di self e si accumulerebbe a ogni
            # re-login.
            old_listener = getattr(self, "events_listener", None)
            if old_listener is not None:
                old_listener.deleteLater()
            self.events_listener = EventsListener(self.api, parent=self)
            self.events_listener.event_received.connect(self._on_server_event)
            # Stessa coppia di connessioni del setup iniziale: senza la seconda
            # connect, le notifiche di sistema (toast tray) smettevano di
            # funzionare dopo un re-login.
            self.events_listener.event_received.connect(self._on_event_notify)
            self.events_listener.connection_changed.connect(self._on_events_connection)
            self.events_listener.start()

            self.notifier.info("Sessione ripristinata")
        finally:
            self._relogin_in_progress = False

    def _on_logout_clicked(self):
        ans = QMessageBox.question(
            self, "Logout",
            "Vuoi davvero uscire?\nI dati locali resteranno disponibili al prossimo accesso.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.api.logout()
            QMessageBox.information(self, "Logout", "Sei stato disconnesso. Riavvia per riloggarti.")
            self.close()


# ----------------------------- main -----------------------------------------

def run():
    setup_logging()
    log.info("=== Avvio AgriMessina QDC ===")

    # Crash reporter PRIMA di tutto: cattura anche errori durante l'init.
    from crash_reporter import setup_crash_reporter
    setup_crash_reporter()

    app = QApplication(sys.argv)

    # --- AGGIUNGI QUESTA RIGA ---
    # Imposta l'icona globale a livello di applicazione (fondamentale per la taskbar di Windows)
    app.setWindowIcon(QIcon(get_risorsa("icona.ico")))

    # PyInstaller helpers
    if getattr(sys, 'frozen', False):
        os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(sys._MEIPASS)

    # Carica .env (es. API_BASE_URL=https://api.agrimessina.it)
    load_dotenv(dotenv_path=get_risorsa(".env"))

    # Splash
    start_time = time.time()
    splash_pix = QPixmap(get_risorsa("splash.png"))
    if splash_pix.isNull():
        splash_pix = QPixmap(600, 400)
        splash_pix.fill(QColor("#4CAF50"))
    splash = QSplashScreen(splash_pix, Qt.WindowType.WindowStaysOnTopHint)
    splash.show()
    splash.showMessage("Inizializzazione…", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, QColor("white"))
    app.processEvents()

    # 1. SQLite locale
    engine = get_engine()

    # Backup del DB locale PRIMA di toccarlo. Niente connessioni aperte
    # quando facciamo la copia → SQLite garantisce file autoconsistente.
    from backup_manager import backup_if_due
    from pathlib import Path
    db_path = Path.home() / ".agrimessina" / "local.db"
    backup_if_due(db_path)

    init_local_database(engine)

    # Migrazioni schema versionate. Idempotenti: niente effetti se non c'è
    # nulla da applicare. La prima volta su un DB pre-esistente imposta la
    # baseline alla versione corrente senza riapplicare nulla.
    from migrations import apply_pending_migrations
    apply_pending_migrations(engine)

    # 2. API client (con eventuale token già salvato)
    api = ApiClient(get_api_base_url())

    # MODIFICA QUI: usa icona.ico
    icon = QIcon(get_risorsa("icona.ico"))

    # 3. Login se serve
    if not api.is_authenticated:
        splash.hide()
        login = LoginDialog(api, splash_pixmap=splash_pix, icon=icon)
        if login.exec() != login.DialogCode.Accepted:
            # Annullato → esce
            sys.exit(0)
        splash.show()
    else:
        # Verifica che il token sia ancora valido con un ping al server
        try:
            api.health()
        except NotAuthenticatedError:
            api.logout()
            splash.hide()
            login = LoginDialog(api, splash_pixmap=splash_pix, icon=icon)
            if login.exec() != login.DialogCode.Accepted:
                sys.exit(0)
            splash.show()
        except Exception:
            # Se il server è giù, lavoriamo offline con i dati locali
            pass

    # Notifier condiviso fra moduli di sync e UI
    notifier = SyncNotifier()

    # 4. Sync iniziale (silenziosa: se fallisce, si lavora con i dati locali)
    splash.showMessage("Invio modifiche offline…", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, QColor("white"))
    app.processEvents()
    try:
        sent, residual, had_insert = upload_pending(api, engine, notifier=notifier)
        if sent > 0:
            log.info("Uploader iniziale: inviate %d operazioni, %d ancora in coda", sent, residual)
        # Se abbiamo inserito record nuovi, gli ID locali e quelli sul server divergono.
        # Forzare un re-sync completo riallinea tutto.
        if had_insert:
            log.info("Uploader: INSERT avvenute → re-sync completo per riallineare gli ID")
            reset_sync_state(engine)
    except Exception as e:
        log.error("Uploader iniziale: %s", e, exc_info=True)

    splash.showMessage("Sincronizzazione dati…", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, QColor("white"))
    app.processEvents()
    sync_result = sync_all(api, engine)
    if not sync_result.ok:
        log.warning("Sync iniziale: %s", sync_result.message)

    # Reconciliation iniziale: full-sync col server (upsert + delete fantasmi)
    splash.showMessage("Verifica allineamento…", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, QColor("white"))
    app.processEvents()
    rec_result = reconcile_with_server(api, engine, notifier=notifier)
    log.info("Reconcile iniziale: %s", rec_result.message)

    # 5. UI principale
    splash.showMessage("Pronto!", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, QColor("white"))
    app.processEvents()

    # Tempo minimo splash 2s
    while time.time() - start_time < 2.0:
        app.processEvents()

    finestra = FinestraPrincipale(api, engine, notifier)
    finestra.show()
    # Aggiorna il conteggio iniziale di pending operations dopo lo startup
    finestra._aggiorna_pending_count()
    splash.finish(finestra)
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
