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
    QDialog, QTextEdit, QDialogButtonBox
)
from PyQt6.QtGui import QKeySequence, QShortcut, QFont, QColor, QPixmap, QIcon
from PyQt6.QtCore import Qt, QCoreApplication, QTimer, QSettings

from dotenv import load_dotenv

from api_client import ApiClient, NotAuthenticatedError
from events_listener import EventsListener
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
        self.events_listener.connection_changed.connect(self._on_events_connection)
        self.events_listener.start()

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
        except Exception:
            pass

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
                    self._is_syncing = False
                    self._aggiorna_pending_count()
                    self._handle_session_expired()
                    return
                if "rete" in msg_low:
                    self.notifier.online = False
                    self.notifier.state("offline")
                else:
                    self.notifier.state("error")
                    self.notifier.warning(f"Reconcile: {result.message}")
        except NotAuthenticatedError:
            self._is_syncing = False
            self._aggiorna_pending_count()
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
        except Exception as e:
            msg = str(e).lower()
            if "rete" in msg or "timeout" in msg or "connect" in msg:
                self.notifier.online = False
                self.notifier.state("offline")
            else:
                log.warning("Pull errore: %s", e)
        finally:
            self._is_syncing = False

    def _esegui_sync(self, silenzioso=False):
        """Logica unificata di sincronizzazione (Upload + Download + Refresh Viste)."""
        self._is_syncing = True
        self.notifier.state("syncing")
        try:
            # 1. Esegui l'upload dei dati pendenti
            try:
                sent, residual, had_insert = upload_pending(self.api, self.engine, notifier=self.notifier)
            except NotAuthenticatedError:
                self._is_syncing = False
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

        except Exception as e:
            log.error("Uploader: %s", e, exc_info=True)
            self.notifier.error(f"Upload fallito: {e}")

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

        self._is_syncing = False
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
        # Niente azione su disconnessione: il listener si auto-riconnette,
        # e i timer di fallback coprono il vuoto temporaneo.

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

            # Rilancia il listener SSE con il nuovo token.
            self.events_listener = EventsListener(self.api, parent=self)
            self.events_listener.event_received.connect(self._on_server_event)
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
    init_local_database(engine)

    # Pulizia one-off del magazzino: ricalcola tutti gli scarichi automatici
    # da zero per rimediare a inconsistenze accumulate da bug pregressi
    # (note "T#<id_locale>" rimaste dopo lo swap di ID, scarichi duplicati
    # da pull di trattamenti server-side, ecc). Esegue una sola volta nella
    # vita del DB locale (marcato in _sync_flags).
    from magazzino_logic import (
        cleanup_magazzino_once_at_boot,
        cleanup_for_server_authoritative_once,
        repull_movimenti_for_origine_once,
    )
    cleanup_result = cleanup_magazzino_once_at_boot(engine)
    if cleanup_result is not None:
        n_del, n_ric = cleanup_result
        log.info("Cleanup magazzino one-off: eliminati %d scarichi, ricalcolati %d trattamenti", n_del, n_ric)

    # Migrazione al modello Server-Authoritative: il server ora calcola gli
    # auto-scarichi server-side. Cancello tutti gli scarichi locali con
    # trattamento_id valorizzato (sono frutto del vecchio modello client-side);
    # verranno ri-pullati dal server al prossimo /magazzino/movimenti con i
    # valori corretti.
    n_del_auth = cleanup_for_server_authoritative_once(engine)
    if n_del_auth is not None:
        log.info("Migrazione server-authoritative: cancellati %d auto-scarichi locali (verranno ripullati dal server)", n_del_auth)

    # Re-pull dei movimenti dopo l'aggiunta del campo azienda_id_origine al backend.
    # Cancella i record con trattamento_id valorizzato (saranno ri-pullati dal server
    # con il nuovo campo). Idempotente via marker `cleanup_origine_repull_done`.
    n_del_origine = repull_movimenti_for_origine_once(engine)
    if n_del_origine is not None:
        log.info("Re-pull origine: cancellati %d auto-scarichi locali (verranno ripullati con azienda_id_origine)", n_del_origine)

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
