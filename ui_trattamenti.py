from sqlalchemy import text
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox,
                             QLabel, QLineEdit,
                             QWidget, QFileDialog,
                             QFrame, QScrollArea,
                             QCheckBox, QDateEdit)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QPainter, QPainterPath, QPen

from local_db import enqueue_operation
from app_logging import get_logger
from ui_trattamenti_dialogs import (
    DialogDettaglioTrattamento,
    DialogModificaTrattamento,
    DialogNuovoTrattamento,
)

log = get_logger(__name__)

# NOTA: lo scarico magazzino è ora server-authoritative (vedi
# app/magazzino_calculator.py nel backend). La UI desktop si limita a
# enqueue le operazioni; il server ricalcola e i client pullano via
# /magazzino/movimenti. La vecchia logica locale è stata rimossa.


class PulsanteTestoContornato(QPushButton):
    def __init__(self, testo, colore_font, con_contorno=False, parent=None):
        super().__init__(testo, parent)
        self.colore_font = QColor(colore_font)
        self.con_contorno = con_contorno
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # --- FIX: Specifichiamo il colore esatto nel CSS per le card bianche ---
        self.setStyleSheet(f"""
            background: transparent;
            border: none;
            font-weight: bold;
            font-size: 13px;
            padding: 0 5px;
            color: {colore_font};
        """)

    def paintEvent(self, event):
        # Se siamo su card bianca, usiamo il disegno standard (che ora userà il colore del CSS)
        if not self.con_contorno:
            return super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        metrics = self.fontMetrics()
        text = self.text()

        x = (self.width() - metrics.horizontalAdvance(text)) / 2
        y = (self.height() + metrics.ascent() - metrics.descent()) / 2

        path = QPainterPath()
        path.addText(x, y, self.font(), text)

        # --- CONTORNO MINIMALE (1.0px) ---
        penna = QPen(QColor(0, 0, 0, 200), 1.0)
        penna.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(penna)
        painter.drawPath(path)

        painter.fillPath(path, self.colore_font)

class WidgetSottoCardBilanciamento(QFrame):
    """Card secondaria per visualizzare un carico di bilanciamento sotto il trattamento padre."""
    def __init__(self, riga_dati, on_click, on_toggle_select, parent=None):
        super().__init__(parent)
        self.id_trattamento = riga_dati["_ID_T"]
        self.callback_click = on_click
        self.setObjectName("SottoCardBilanciamento")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(60)

        # Schema colori coerente
        tipo = riga_dati.get("TipoViolazione", 0)
        if tipo == 5:    # Blacklist
            bg, border_left, text_color, sub_color, badge_bg = "#212121", "#000000", "#FFFFFF", "#BDBDBD", "#FFFFFF"
            badge_text = "#212121"
        elif tipo == 2:  # Dose eccessiva
            bg, border_left, text_color, sub_color, badge_bg = "#FFEBEE", "#D32F2F", "#B71C1C", "#C62828", "#D32F2F"
            badge_text = "#FFFFFF"
        elif tipo == 3:  # Dose bassa (caso più frequente per i bilanciamenti)
            bg, border_left, text_color, sub_color, badge_bg = "#E3F2FD", "#1976D2", "#0D47A1", "#1565C0", "#1976D2"
            badge_text = "#FFFFFF"
        elif tipo == 1:  # Intervallo violato
            bg, border_left, text_color, sub_color, badge_bg = "#FFFDE7", "#F9A825", "#212121", "#5D4037", "#F9A825"
            badge_text = "#212121"
        else:            # Nessuna violazione
            bg, border_left, text_color, sub_color, badge_bg = "#F5F9FF", "#1976D2", "#424242", "#757575", "#1976D2"
            badge_text = "#FFFFFF"

        self.setStyleSheet(f"""
            #SottoCardBilanciamento {{
                background-color: {bg};
                border: 1px solid {border_left};
                border-left: 4px solid {border_left};
                border-radius: 6px;
            }}
            #SottoCardBilanciamento:hover {{
                background-color: {bg};
                border: 1px solid {border_left};
                border-left: 5px solid {border_left};
            }}
            QCheckBox {{ color: {text_color}; font-size: 12px; }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                background-color: white;
                border: 2px solid #757575;
                border-radius: 3px;
            }}
            QCheckBox::indicator:checked {{
                background-color: #2E7D32;
                border: 2px solid #2E7D32;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 8, 15, 8)
        layout.setSpacing(10)

        # Checkbox per export/eliminazione
        self.check_select = QCheckBox()
        self.check_select.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_select.stateChanged.connect(lambda state: on_toggle_select(self.id_trattamento, state))
        layout.addWidget(self.check_select)

        # Freccia di gerarchia
        lbl_freccia = QLabel("↳")
        lbl_freccia.setStyleSheet(f"color: {border_left}; font-size: 18px; font-weight: bold; background: transparent;")
        layout.addWidget(lbl_freccia)

        # Etichetta tipo
        lbl_tipo = QLabel("🔄 Bilanciamento")
        lbl_tipo.setStyleSheet(f"""
            background-color: {badge_bg};
            color: {badge_text};
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: bold;
        """)
        layout.addWidget(lbl_tipo)

        # ID del sub-trattamento. Stesso badge style della card principale.
        # Senza questo, l'utente vede solo l'ID del padre nel campo Operatore
        # (es. "SISTEMA: BILANCIAMENTO [360]") e per modificare via SQL deve
        # andare a cercare a mano nel DB. Mostrare entrambi gli ID rende
        # subito chiaro cosa si sta guardando.
        lbl_id_sub = QLabel(f"#{self.id_trattamento}")
        lbl_id_sub.setStyleSheet(
            f"color: {text_color}; font-size: 11px; "
            f"background: rgba(0,0,0,0.08); padding: 1px 6px; "
            f"border-radius: 8px; margin-left: 4px;"
        )
        layout.addWidget(lbl_id_sub)

        # Percorso
        lbl_percorso = QLabel(f"{riga_dati['Azienda']} › {riga_dati['Agro']} › {riga_dati['Contrada']} › {riga_dati['Tendoni']}")
        lbl_percorso.setStyleSheet(f"color: {text_color}; font-size: 12px; background: transparent;")
        layout.addWidget(lbl_percorso)

        layout.addStretch()

        # --- FIX: QUANTITÀ, DOSE EFFETTIVA E BOTTI ---
        um = riga_dati.get('Unità Misura') or ''
        um_pulita = um.split('/')[0] if '/' in um else um

        qta = riga_dati.get('Quantità Totale', 0)
        dose = riga_dati.get('Dose', 0)
        dose_cum = riga_dati.get('Dose Cumulativa', 0) # <--- Estrazione
        botti = riga_dati.get('Botti', 0)

        try: qta_fmt = f"{float(qta):.4g}"
        except: qta_fmt = str(qta)

        try: dose_fmt = f"{float(dose):.4g}"
        except: dose_fmt = str(dose)

        try: dose_cum_fmt = f"{float(dose_cum):.4g}" # <--- Definizione mancante
        except: dose_cum_fmt = str(dose_cum)

        try: botti_fmt = f"{float(botti):.3g}"
        except: botti_fmt = str(botti)

        testo_valori = f"+{qta_fmt} {um_pulita}   |   ⚖️ {dose_fmt} {um} (Tot: {dose_cum_fmt} {um})   |   🛢️ {botti_fmt} botti"

        lbl_qta = QLabel(testo_valori)
        lbl_qta.setStyleSheet(f"color: {text_color}; font-size: 13px; font-weight: bold; background: transparent;")
        layout.addWidget(lbl_qta)

        # Operatore
        op = riga_dati.get('Operatore') or '—'
        lbl_op = QLabel(f"👤 {op}")
        lbl_op.setStyleSheet(f"color: {sub_color}; font-size: 12px; background: transparent; padding-left: 10px;")
        layout.addWidget(lbl_op)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            widget_clicked = self.childAt(event.position().toPoint())
            if isinstance(widget_clicked, QCheckBox):
                super().mousePressEvent(event)
                return
            self.callback_click(self.id_trattamento)
        else:
            super().mousePressEvent(event)

class WidgetTrattamentoCard(QFrame):
    def __init__(self, riga_dati, on_modifica, on_elimina, on_click, on_toggle_select,
                 on_revisiona=None, on_operazione_click=None, parent=None):
        super().__init__(parent)
        self.id_trattamento = riga_dati["_ID_T"]
        self.callback_click = on_click
        self._on_operazione_click = on_operazione_click
        self._operazione_id = None
        self.setObjectName("TrattamentoCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Altezza FISSA per garantire che tutte e 3 le righe siano sempre visibili
        self.setFixedHeight(95)

        # --- LAYOUT VERTICALE SEMPLIFICATO (Indistruttibile) ---
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 8, 15, 8)
        layout.setSpacing(2)

        # Logica Colori
        tipo = riga_dati["TipoViolazione"]
        bg_color, text_color, sub_text_color = "#FFFFFF", "#212121", "#757575"

        if tipo == 5: bg_color, text_color, sub_text_color = "#212121", "#FFFFFF", "#BDBDBD"
        elif tipo == 2: bg_color, text_color, sub_text_color = "#F44336", "#FFFFFF", "#FFCDD2"
        elif tipo == 3: bg_color, text_color, sub_text_color = "#2196F3", "#FFFFFF", "#BBDEFB"
        elif tipo == 1: bg_color, text_color, sub_text_color = "#FFEB3B", "#212121", "#424242"

        self.setStyleSheet(f"""
            #TrattamentoCard {{
                background-color: {bg_color};
                border: 1px solid #E0E0E0;
                border-radius: 8px;
            }}
            QCheckBox {{ color: {text_color}; font-weight: bold; font-size: 14px; }}
            QCheckBox::indicator {{
                width: 18px; height: 18px;
                background-color: white;
                border: 2px solid #757575;
                border-radius: 4px;
            }}
            QCheckBox::indicator:checked {{
                background-color: #2E7D32;
                border: 2px solid #2E7D32;
            }}
            QPushButton {{
                background: transparent;
                border: none;
                padding: 2px 8px;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background: rgba(0,0,0,0.08);
                border-radius: 4px;
            }}
        """)
        # ==========================================
        # RIGA 1: Checkbox + Prodotto (Allineati) + Data
        # ==========================================
        riga1 = QHBoxLayout()

        self.check_select = QCheckBox()
        self.check_select.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_select.stateChanged.connect(lambda state: on_toggle_select(self.id_trattamento, state))
        riga1.addWidget(self.check_select)

        riga1.addSpacing(15) # Spazio tra la spunta e il nome prodotto

        lbl_prod = QLabel(riga_dati["Prodotto"])
        lbl_prod.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {text_color};")
        riga1.addWidget(lbl_prod)

        # ID del trattamento, accanto al nome prodotto. Utile per il match
        # con le note di registro_magazzino ("PRESTITO DA X - T#298") e per
        # comunicare l'ID specifico al supporto/altri client.
        lbl_id = QLabel(f"#{self.id_trattamento}")
        lbl_id.setStyleSheet(
            f"color: {sub_text_color}; font-size: 11px; "
            f"background: rgba(0,0,0,0.06); padding: 1px 6px; border-radius: 8px; "
            f"margin-left: 8px;"
        )
        riga1.addWidget(lbl_id)

        # Badge "Operazione N": id univoco dell'operazione in campo, allocato
        # dal server (vedi _alloca_operazione_id in trattamenti_router.py).
        # Tutti i trattamenti dello stesso gruppo condividono lo stesso
        # operazione_id; ogni singolo ha il suo. Click sul badge → popup
        # con la lista dei trattamenti collegati.
        op_n = int(riga_dati.get("OperazioneN") or 0)
        op_id = riga_dati.get("OperazioneId")
        if op_id is not None:
            self._operazione_id = int(op_id)
            lbl_op = QLabel(f"Operazione {int(op_id)}")
            tooltip_lines = [f"Operazione id_operazione = {int(op_id)}"]
            if op_n > 1:
                tooltip_lines.append(
                    f"{op_n} trattamenti collegati. Clicca per vedere la lista."
                )
            else:
                tooltip_lines.append(
                    "Nessun altro trattamento collegato (solo questo)."
                )
            lbl_op.setToolTip("\n".join(tooltip_lines))
            lbl_op.setStyleSheet(
                f"color: white; font-size: 11px; font-weight: bold; "
                f"background: #6A1B9A; padding: 1px 8px; border-radius: 8px; "
                f"margin-left: 6px;"
            )
            lbl_op.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl_op.mousePressEvent = lambda _e: self._mostra_operazione_collegati()
            riga1.addWidget(lbl_op)

        riga1.addStretch() # Spinge la data tutta a destra

        lbl_data = QLabel(str(riga_dati["Data"]))
        lbl_data.setStyleSheet(f"color: {sub_text_color}; font-size: 13px;")
        riga1.addWidget(lbl_data)

        layout.addLayout(riga1)

        # ==========================================
        # RIGA 2: Percorso (Sotto il prodotto)
        # ==========================================
        lbl_percorso = QLabel(f"{riga_dati['Azienda']} › {riga_dati['Agro']} › {riga_dati['Contrada']} › {riga_dati['Tendoni']}")
        lbl_percorso.setStyleSheet(f"color: {text_color}; font-size: 13px;")
        lbl_percorso.setWordWrap(True)
        layout.addWidget(lbl_percorso)

        # --- LOGICA VISIBILITÀ MODIFICA ---
        has_bil = riga_dati.get("HasBil", 0) > 0
        is_solo_bil = riga_dati.get("_SOLO_BIL") == 1

        # In Revisionati mostriamo sempre Modifica (ma limitata),
        # in Proposte la nascondiamo se ci sono bilanciamenti attivi.
        if getattr(self.parent(), 'tipo_vista', '') == "AUTORIZZATI":
            mostra_modifica = not is_solo_bil # I figli di bilanciamento rimangono non modificabili
        else:
            mostra_modifica = not (has_bil or is_solo_bil)

        # ==========================================
        # RIGA 3: Footer (Operatore + Quantità + Bottoni)
        # ==========================================
        riga3 = QHBoxLayout()

        # --- FIX: ESTRAZIONE E FORMATTAZIONE PULITA (Quantità, Dose, Botti) ---
        um = riga_dati.get('Unità Misura') or ''
        um_pulita = um.split('/')[0] if '/' in um else um

        qta = riga_dati.get('Quantità Totale', 0)
        dose = riga_dati.get('Dose', 0)
        dose_cum = riga_dati.get('Dose Cumulativa', 0) # <--- Estrazione
        botti = riga_dati.get('Botti', 0)

        try: qta_fmt = f"{float(qta):.4g}"
        except: qta_fmt = str(qta)

        try: dose_fmt = f"{float(dose):.4g}"
        except: dose_fmt = str(dose)

        try: dose_cum_fmt = f"{float(dose_cum):.4g}" # <--- Definizione mancante
        except: dose_cum_fmt = str(dose_cum)

        try: botti_fmt = f"{float(botti):.3g}"
        except: botti_fmt = str(botti)

        op = riga_dati.get('Operatore') or '—'

        testo_valori = f"👤 {op}   |   {qta_fmt} {um_pulita}   |   ⚖️ {dose_fmt} {um} (Tot: {dose_cum_fmt} {um})   |   🛢️ {botti_fmt} botti"

        lbl_info = QLabel(testo_valori)
        lbl_info.setStyleSheet(f"color: {sub_text_color}; font-size: 13px;")
        riga3.addWidget(lbl_info)

        riga3.addStretch()

        # Determiniamo se serve il contorno (solo se la card è colorata)
        serve_contorno = (bg_color.upper() != "#FFFFFF")

        # --- PULSANTE MODIFICA ---
        color_mod = '#90CAF9' if text_color == '#FFFFFF' else '#1976D2'
        self.btn_mod = PulsanteTestoContornato("Modifica", color_mod, serve_contorno)
        self.btn_mod.clicked.connect(lambda: on_modifica(self.id_trattamento))

        # APPLICA LA VISIBILITÀ
        self.btn_mod.setVisible(mostra_modifica)

        riga3.addWidget(self.btn_mod)

        # --- PULSANTE ELIMINA ---
        color_del = '#FF8A80' if text_color == '#FFFFFF' else '#D32F2F'
        self.btn_del = PulsanteTestoContornato("Elimina", color_del, serve_contorno)
        self.btn_del.clicked.connect(lambda: on_elimina(self.id_trattamento))
        riga3.addWidget(self.btn_del)

        # --- PULSANTE REVISIONA ---
        color_rev = '#A5D6A7' if text_color == '#FFFFFF' else '#2E7D32'
        self.btn_rev = PulsanteTestoContornato("Revisiona", color_rev, serve_contorno)
        gia_autorizzato = int(riga_dati.get("IsAut") or 0) == 1
        if on_revisiona is not None and not gia_autorizzato:
            def _on_rev_click():
                self.btn_rev.setEnabled(False)  # evita doppi click
                on_revisiona(self.id_trattamento)
            self.btn_rev.clicked.connect(_on_rev_click)
        else:
            self.btn_rev.setVisible(False)
        riga3.addWidget(self.btn_rev)

        layout.addLayout(riga3)


    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Non chiamare il callback se il click è su un widget figlio interattivo
            widget_clicked = self.childAt(event.position().toPoint())
            if isinstance(widget_clicked, (QPushButton, QCheckBox)):
                super().mousePressEvent(event)
                return
            self.callback_click(self.id_trattamento)
        else:
            super().mousePressEvent(event)

    def _mostra_operazione_collegati(self):
        if self._on_operazione_click and self._operazione_id:
            self._on_operazione_click(self._operazione_id)


class SchedaOperazioni(QWidget):
    # Finestra di "freschezza" dei dati post-reconcile: entro questi secondi
    # il pre-check su Modifica/Revisiona/Elimina viene saltato per evitare
    # latenza HTTP inutile (i dati locali sono allineati al server).
    RECONCILE_FRESHNESS_SECONDS = 10.0

    def __init__(self, engine, db, tipo_vista, api, notifier):
        super().__init__()
        self.engine, self.db, self.tipo_vista = engine, db, tipo_vista
        self.api = api  # usato per pre-check su Modifica/Revisiona/Elimina
        self.notifier = notifier  # serve last_successful_reconcile_ts
        self._selezionati = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        # --- RICERCA + FILTRI SALVATI ---
        h_filtri = QHBoxLayout()
        self.search_bar = QLineEdit()
        self.search_bar.setObjectName("SearchBar")
        self.search_bar.setPlaceholderText("🔍 Cerca trattamenti per prodotto, tendone o azienda...")
        # Debounce 300ms sulla ricerca: `aggiorna_dati` rilancia una query
        # SQL pesante (subquery annidate + GROUP_CONCAT). Senza debounce
        # parte 4-5 volte mentre l'utente digita "polysul" → UI freeze.
        # Pattern: QTimer singleShot, ad ogni keystroke restartiamo il timer.
        from PyQt6.QtCore import QTimer
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self.aggiorna_dati)
        self.search_bar.textChanged.connect(
            lambda _t: self._search_debounce.start(300)
        )
        h_filtri.addWidget(self.search_bar, stretch=1)

        # --- FILTRO PERIODO (Da / A) — inline, prima dei filtri salvati ---
        # Attivo solo quando la checkbox è spuntata; i due QDateEdit restano
        # disabilitati altrimenti per evitare interazioni casuali. Date
        # inclusive su entrambi i lati. Le card vengono filtrate lato SQL →
        # "Seleziona tutto" prende automaticamente solo ciò che è nel range.
        self.chk_periodo = QCheckBox("📅 Periodo")
        self.chk_periodo.setToolTip("Filtra i trattamenti per intervallo di data")
        self.chk_periodo.toggled.connect(self._on_toggle_periodo)
        h_filtri.addWidget(self.chk_periodo)

        h_filtri.addWidget(QLabel("Da:"))
        self.date_da = QDateEdit()
        self.date_da.setCalendarPopup(True)
        self.date_da.setDisplayFormat("dd/MM/yyyy")
        self.date_da.setDate(QDate.currentDate().addMonths(-1))
        self.date_da.setEnabled(False)
        self.date_da.dateChanged.connect(lambda _d: self._on_periodo_changed())
        h_filtri.addWidget(self.date_da)

        h_filtri.addWidget(QLabel("A:"))
        self.date_a = QDateEdit()
        self.date_a.setCalendarPopup(True)
        self.date_a.setDisplayFormat("dd/MM/yyyy")
        self.date_a.setDate(QDate.currentDate())
        self.date_a.setEnabled(False)
        self.date_a.dateChanged.connect(lambda _d: self._on_periodo_changed())
        h_filtri.addWidget(self.date_a)

        layout.addLayout(h_filtri)

        # --- PULSANTI AZIONE ---
        # --- PULSANTI AZIONE ---
        h_azioni = QHBoxLayout()
        self.btn_nuovo = QPushButton("+ NUOVO TRATTAMENTO")
        self.btn_nuovo.setProperty("class", "success")
        self.btn_nuovo.clicked.connect(self.apri_dialog_nuovo)

        # Nasconde "Nuovo trattamento" nella vista Revisionati.
        if self.tipo_vista == "AUTORIZZATI":
            self.btn_nuovo.hide()

        self.btn_sel_tutti = QPushButton("☑️ SELEZIONA TUTTO")
        self.btn_sel_tutti.setProperty("class", "secondary")
        self.btn_sel_tutti.clicked.connect(self._toggle_seleziona_tutto)

        self.btn_exp = QPushButton("📊 ESPORTA SELEZIONATI (0)")
        self.btn_exp.setProperty("class", "success")
        self.btn_exp.clicked.connect(self._esporta_selezionati)

        self.btn_elim_sel = QPushButton("🗑️ ELIMINA SELEZIONATI (0)")
        self.btn_elim_sel.setProperty("class", "danger")
        self.btn_elim_sel.clicked.connect(self._elimina_selezionati)

        self.btn_aggiorna = QPushButton("🔄 AGGIORNA")
        self.btn_aggiorna.setProperty("class", "secondary")
        self.btn_aggiorna.clicked.connect(self.aggiorna_dati)

        h_azioni.addWidget(self.btn_nuovo)
        h_azioni.addWidget(self.btn_sel_tutti)
        h_azioni.addWidget(self.btn_exp)
        h_azioni.addWidget(self.btn_elim_sel)
        h_azioni.addStretch()
        h_azioni.addWidget(self.btn_aggiorna)
        layout.addLayout(h_azioni)

        # --- AREA CARD ---
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("background: transparent; border: none;")

        self.container_card = QWidget()
        self.container_card.setStyleSheet("background: transparent;")
        self.layout_card = QVBoxLayout(self.container_card)
        self.layout_card.setContentsMargins(0, 0, 0, 0)
        self.layout_card.setSpacing(10)

        self.layout_card.addStretch()
        self.scroll.setWidget(self.container_card)
        layout.addWidget(self.scroll)

        self.aggiorna_dati()

    def gestisci_selezione(self, id_t, state):
        if state == 2:
            self._selezionati.add(id_t)
        else:
            self._selezionati.discard(id_t)
        self.btn_exp.setText(f"📊 ESPORTA SELEZIONATI ({len(self._selezionati)})")
        self.btn_elim_sel.setText(f"🗑️ ELIMINA SELEZIONATI ({len(self._selezionati)})")
        if hasattr(self, 'btn_sel_tutti'):
            self.btn_sel_tutti.setText("☑️ SELEZIONA TUTTO")

    def _on_toggle_periodo(self, attivo: bool):
        """Abilita/disabilita i due QDateEdit e rilancia il refresh."""
        self.date_da.setEnabled(attivo)
        self.date_a.setEnabled(attivo)
        self.aggiorna_dati()

    def _on_periodo_changed(self):
        """Refresh solo se il filtro periodo è effettivamente attivo,
        per evitare query ridondanti quando l'utente scorre il calendario
        a checkbox spenta."""
        if self.chk_periodo.isChecked():
            self.aggiorna_dati()

    def _toggle_seleziona_tutto(self):
        """Seleziona tutte le card visibili, oppure deseleziona se sono già tutte selezionate."""
        cards_visibili = []
        for i in range(self.layout_card.count()):
            item = self.layout_card.itemAt(i)
            widget = item.widget() if item else None
            if widget is None:
                continue
            if hasattr(widget, 'check_select') and hasattr(widget, 'id_trattamento'):
                cards_visibili.append(widget)
            else:
                for child in widget.findChildren(WidgetSottoCardBilanciamento):
                    cards_visibili.append(child)

        if not cards_visibili:
            return

        tutte_selezionate = all(c.check_select.isChecked() for c in cards_visibili)
        nuovo_stato = not tutte_selezionate

        for c in cards_visibili:
            c.check_select.setChecked(nuovo_stato)

        if nuovo_stato:
            self.btn_sel_tutti.setText("☐ DESELEZIONA TUTTO")
        else:
            self.btn_sel_tutti.setText("☑️ SELEZIONA TUTTO")

    def _verifica_esistenza_server(self, trattamento_id) -> str:
        """Pre-check prima di un'azione sul trattamento (modifica/revisiona/elimina).

        Ritorna:
          - "ok"        → record presente sul server (o dati locali "freschi")
          - "gone"      → record assente sul server; la card viene rimossa
          - "offline"   → impossibile verificare; si procede comunque

        Ottimizzazioni:
          - Se reconcile è andato a buon fine entro RECONCILE_FRESHNESS_SECONDS,
            saltiamo la chiamata HTTP (i dati locali sono affidabili).
          - Se il notifier sa che siamo offline, restituiamo "offline" subito.
          - La chiamata HTTP usa un timeout corto (3s) per non bloccare la UI.
        """
        # Cache "recente": se reconcile è appena passato, fidati dei dati locali
        import time
        if (time.time() - self.notifier.last_successful_reconcile_ts) < self.RECONCILE_FRESHNESS_SECONDS:
            return "ok"
        # Se la rete è marcata down, evita la chiamata HTTP
        if not self.notifier.online:
            return "offline"

        # Cursore "attesa" durante la chiamata (max 3s grazie a quick_get)
        from PyQt6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            data = self.api.quick_get_trattamento(trattamento_id)
            if isinstance(data, dict) and data.get("id"):
                return "ok"
            return "gone"
        except Exception as e:
            # 404 → record gone; resto → offline (non bloccare l'utente)
            if getattr(e, "status_code", None) == 404:
                return "gone"
            msg = str(e).lower()
            if "404" in msg or "not found" in msg:
                return "gone"
            return "offline"
        finally:
            QApplication.restoreOverrideCursor()

    def _handle_gone(self, trattamento_id):
        """Pulisce la cache locale di un trattamento che il server non ha più."""
        from local_db import set_downloading
        set_downloading(self.engine, True)
        try:
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM dettaglio_trattamenti WHERE trattamento_id = :id"), {"id": trattamento_id})
                conn.execute(text("DELETE FROM avvisi_trattamenti WHERE trattamento_id = :id"), {"id": trattamento_id})
                conn.execute(text("DELETE FROM trattamenti WHERE id = :id"), {"id": trattamento_id})
        except Exception as e:
            log.error("handle_gone errore: %s", e, exc_info=True)
        finally:
            # Garantisce il reset del flag anche se la DELETE fallisce: altrimenti
            # tutte le scritture UI successive non verrebbero accodate.
            set_downloading(self.engine, False)

        QMessageBox.warning(
            self, "Trattamento non disponibile",
            "Questo trattamento è stato cancellato da un altro utente. "
            "La voce è stata rimossa dall'elenco.",
        )
        self.aggiorna_dati()
        self._notify_trattamento_changed()

    def apri_dialog_modifica(self, trattamento_id):
        # Pre-check: il trattamento esiste ancora sul server?
        check = self._verifica_esistenza_server(trattamento_id)
        if check == "gone":
            self._handle_gone(trattamento_id)
            return

        # In Revisionati permettiamo l'apertura anche se ci sono bilanciamenti
        if self.tipo_vista == "PROPOSTE":
            with self.engine.connect() as conn:
                check_bil = conn.execute(text(
                    "SELECT COUNT(*) FROM dettaglio_trattamenti WHERE trattamento_id = :id AND is_bilanciamento = 1"
                ), {"id": trattamento_id}).scalar()

            if check_bil > 0:
                QMessageBox.warning(self, "Azione Bloccata", "Elimina i bilanciamenti per modificare i dati tecnici.")
                return

        dialog = DialogModificaTrattamento(self.engine, trattamento_id, self)
        if dialog.exec():
            self.aggiorna_dati()
            self._notify_trattamento_changed()

    def elimina_record(self, trattamento_id):
        # Pre-check server: se già cancellato altrove, pulisci e basta
        check = self._verifica_esistenza_server(trattamento_id)
        if check == "gone":
            self._handle_gone(trattamento_id)
            return

        # Prima identifico se ci sono figli di bilanciamento collegati
        with self.engine.connect() as conn:
            figli_collegati = self._trova_figli_bilanciamento(conn, {trattamento_id})

        n_figli = len(figli_collegati)
        if n_figli > 0:
            messaggio = (
                f"Questo trattamento ha {n_figli} bilanciament{'o' if n_figli == 1 else 'i'} collegat{'o' if n_figli == 1 else 'i'}.\n\n"
                f"Eliminando il trattamento, verr{'à' if n_figli == 1 else 'anno'} eliminat{'o' if n_figli == 1 else 'i'} "
                f"automaticamente anche i bilanciament{'o' if n_figli == 1 else 'i'} su altri tendoni.\n\n"
                f"Vuoi davvero procedere?"
            )
        else:
            messaggio = "Vuoi davvero eliminare questo trattamento?"

        risposta = QMessageBox.question(
            self, "Conferma eliminazione", messaggio,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if risposta != QMessageBox.StandardButton.Yes:
            return

        try:
            with self.engine.begin() as conn:
                ids_da_eliminare = list(figli_collegati) + [trattamento_id]
                ids_str = ",".join(map(str, ids_da_eliminare))

                # Pulisci il registro magazzino prima del DELETE: per ogni
                # trattamento, cancella le righe dal registro in cui erano
                # (reale se in Storico, fittizio se in Revisionati). Il
                # registro "non coinvolto" resta intatto (es. reale di un
                # Revisionato eliminato → snapshot conservato).
                from magazzino_logic import (
                    cancella_scarico_reale, cancella_scarico_fittizio,
                )
                stati = dict(conn.execute(text(
                    f"SELECT id, is_autorizzato FROM trattamenti WHERE id IN ({ids_str})"
                )).fetchall())
                for tid in ids_da_eliminare:
                    if stati.get(tid, 0) == 0:
                        cancella_scarico_reale(conn, tid)
                    else:
                        cancella_scarico_fittizio(conn, tid)

                conn.execute(text(f"DELETE FROM dettaglio_trattamenti WHERE trattamento_id IN ({ids_str})"))
                conn.execute(text(f"DELETE FROM avvisi_trattamenti WHERE trattamento_id IN ({ids_str})"))
                conn.execute(text(f"DELETE FROM trattamenti WHERE id IN ({ids_str})"))

            # 3. Notifica al server Cloud e aggiornamento interfaccia
            for tid in ids_da_eliminare:
                enqueue_operation(self.engine, "TRATTAMENTO", "DELETE", entity_id=tid)

            self.aggiorna_dati()
            self._notify_trattamento_changed()

            info_msg = "Trattamento rimosso con successo."
            if n_figli > 0:
                info_msg = f"Trattamento e {n_figli} bilanciamenti collegati rimossi con successo."
            QMessageBox.information(self, "Eliminato", info_msg)

        except Exception as e:
            log.exception("Errore eliminazione trattamento")
            QMessageBox.critical(self, "Errore", f"Impossibile eliminare: {str(e)}")

    def apri_dialog_nuovo(self):
        if DialogNuovoTrattamento(self.engine, self).exec():
            self.aggiorna_dati()
            self._notify_trattamento_changed()

    def mostra_dettagli(self, trattamento_id):
        dialog = DialogDettaglioTrattamento(self.engine, trattamento_id, self.tipo_vista, self)
        dialog.exec()

    def mostra_operazione_collegati(self, operazione_id):
        """Popup con i trattamenti che condividono lo stesso `operazione_id`.

        L'utente clicca il badge 🔗 sulla card per capire al volo quali altri
        trattamenti facevano parte della stessa operazione in campo (più
        prodotti versati nella stessa botte → registrati come N record
        distinti dal backend, ma con lo stesso operazione_id generato
        client-side al momento dell'inserimento).
        """
        with self.engine.connect() as conn:
            righe = conn.execute(text("""
                SELECT
                    t.id,
                    t.data_trattamento,
                    p.nome_prodotto,
                    t.operatore,
                    (SELECT GROUP_CONCAT(DISTINCT ten.codice)
                       FROM dettaglio_trattamenti dt
                       JOIN tendoni ten ON ten.id = dt.tendone_id
                      WHERE dt.trattamento_id = t.id
                        AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)
                    ) AS tendoni
                FROM trattamenti t
                JOIN prodotti p ON p.id = t.prodotto_id
                WHERE t.operazione_id = :op
                ORDER BY t.id
            """), {"op": operazione_id}).fetchall()

        if not righe:
            QMessageBox.information(
                self, "Operazione multi-prodotto",
                "Nessun trattamento collegato trovato.",
            )
            return

        lines = []
        for r in righe:
            ten = r[4] or "—"
            op = r[3] or "—"
            lines.append(f"• <b>T#{r[0]}</b> — {r[2]} ({r[1]}) — {ten} — 👤 {op}")
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("Operazione multi-prodotto")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            f"<b>{len(righe)} trattamenti</b> con id_operazione = "
            f"<b>{int(operazione_id)}</b>:<br><br>"
            + "<br>".join(lines)
        )
        msg.exec()

    def _notify_trattamento_changed(self):
        """Segnala che i trattamenti sono cambiati. I pannelli magazzino (e
        altri eventuali consumatori) vi si agganciano per refreshare le
        giacenze, dato che ogni cambio trattamento riscrive registro_magazzino."""
        if self.notifier is not None:
            self.notifier.trattamento_changed.emit()

    def revisiona_record(self, trattamento_id):
        # Pre-check server: evita /autorizza su record già cancellato
        check = self._verifica_esistenza_server(trattamento_id)
        if check == "gone":
            self._handle_gone(trattamento_id)
            return

        risposta = QMessageBox.question(
            self, "Revisiona trattamento",
            "Confermi la revisione di questo trattamento?\nVerrà marcato come autorizzato.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if risposta != QMessageBox.StandardButton.Yes:
            return

        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text("UPDATE trattamenti SET is_autorizzato = 1 WHERE id = :id"),
                    {"id": trattamento_id},
                )
                # Doppio scarico: il reale resta com'è (creato durante Storico),
                # il fittizio riceve ora il proprio scarico iniziale con la
                # qta corrente (incluso eventuali bilanciamenti già applicati).
                from magazzino_logic import scarica_fittizio
                scarica_fittizio(conn, trattamento_id)

            # Usa l'endpoint dedicato /trattamenti/{id}/autorizza invece di UPDATE
            # generico: evita di rispedire l'intero payload (incluso dettagli)
            # che potrebbe sovrascrivere modifiche fatte da altri client.
            enqueue_operation(self.engine, "TRATTAMENTO", "AUTORIZZA",
                              entity_id=trattamento_id)

            self.aggiorna_dati()
            self._notify_trattamento_changed()
        except Exception as e:
            log.exception("Revisione trattamento fallita")
            QMessageBox.critical(self, "Errore", f"Impossibile revisionare: {str(e)}")

    def aggiorna_dati(self):
        while self.layout_card.count() > 1:
            item = self.layout_card.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if hasattr(self, 'btn_sel_tutti'):
            self.btn_sel_tutti.setText("☑️ SELEZIONA TUTTO")

        # NOTA: gli avvisi sono autorevoli lato server (vedi _replace_avvisi
        # in sync.py). Il ricalcolo locale via `ricalcola_avvisi_globali` è
        # stato rimosso da qui perché sovrascriveva i dati appena scaricati
        # dal server ad ogni refresh della UI (e il refresh è frequente).

        testo_cerca = self.search_bar.text().lower()

        if self.tipo_vista == "PROPOSTE":
            filtro_dettaglio = "AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)"
            filtro_inner = "AND (dt2.is_bilanciamento = 0 OR dt2.is_bilanciamento IS NULL)"
            filtro_autorizzato = ""
        else:
            filtro_dettaglio = ""
            filtro_inner = ""
            # Nella vista AUTORIZZATI includiamo:
            #  - i trattamenti autorizzati (t.is_autorizzato = 1)
            #  - i trattamenti SOLO-bilanciamento (tg.solo_bilanciamenti = 1),
            #    cioè i "figli" creati da DialogCompensaDisavanzo. Hanno
            #    is_autorizzato = 0 ma vanno mostrati come sotto-card sotto
            #    il padre autorizzato. Senza questa OR, il filtro li escludeva
            #    completamente dalla query e la sotto-card non compariva mai.
            filtro_autorizzato = "AND (t.is_autorizzato = 1 OR tg.solo_bilanciamenti = 1)"

        # Filtro periodo (BETWEEN inclusivo). Solo se la checkbox è attiva.
        params_periodo: dict[str, str] = {}
        if self.chk_periodo.isChecked():
            filtro_periodo = "AND t.data_trattamento BETWEEN :date_da AND :date_a"
            params_periodo = {
                "date_da": self.date_da.date().toString("yyyy-MM-dd"),
                "date_a": self.date_a.date().toString("yyyy-MM-dd"),
            }
        else:
            filtro_periodo = ""

        query_sql = f"""
            SELECT
                t.id AS "_ID_T",
                t.data_trattamento AS "Data",
                t.operatore AS "Operatore",
                p.nome_prodotto AS "Prodotto",
                tg.aziende AS "Azienda",
                tg.agri AS "Agro",
                tg.contrade AS "Contrada",
                tg.codici_tendoni AS "Tendoni",
                tg.qta_totale AS "Quantità Totale",
                tg.botti_totale AS "Botti",
                tg.dose_effettiva AS "Dose",
                tg.dose_cum_max AS "Dose Cumulativa",
                p.unita_misura AS "Unità Misura",
                tg.solo_bilanciamenti AS "_SOLO_BIL",
                t.is_autorizzato AS "IsAut",
                t.operazione_id AS "OperazioneId",
                -- Quanti trattamenti condividono la stessa operazione (incluso questo).
                -- Quando operazione_id è NULL la condizione t_op.operazione_id = NULL
                -- è sempre falsa → conta 0 → niente badge sulla card.
                (SELECT COUNT(*) FROM trattamenti t_op
                 WHERE t.operazione_id IS NOT NULL
                   AND t_op.operazione_id = t.operazione_id) AS "OperazioneN",
                (SELECT COUNT(*) FROM dettaglio_trattamenti WHERE trattamento_id = t.id AND is_bilanciamento = 1) AS "HasBil",
                CASE tg.priorita_max
                    WHEN 5 THEN 5
                    WHEN 4 THEN 2
                    WHEN 3 THEN 3
                    WHEN 2 THEN 1
                    ELSE 0
                END AS TipoViolazione
            FROM trattamenti t
            JOIN prodotti p ON p.id = t.prodotto_id
            JOIN (
                SELECT
                    tvpt.trattamento_id,
                    GROUP_CONCAT(DISTINCT tvpt.az_n) AS aziende,
                    GROUP_CONCAT(DISTINCT tvpt.ag_n) AS agri,
                    GROUP_CONCAT(DISTINCT tvpt.c_n) AS contrade,
                    GROUP_CONCAT(DISTINCT tvpt.codice) AS codici_tendoni,
                    SUM(tvpt.qta_tendone) AS qta_totale,
                    SUM(tvpt.botti_tendone) AS botti_totale,
                    MAX(tvpt.dose_effettiva) AS dose_effettiva,
                    MAX(tvpt.dose_cumulativa_storica) AS dose_cum_max,
                    MIN(tvpt.solo_bil) AS solo_bilanciamenti,
                    MAX(
                        CASE
                            WHEN LOWER(TRIM(tvpt.blacklist)) = 'si' THEN 5
                            WHEN tvpt.viol_intervallo = 1 THEN 2
                            -- ORA IL COLORE SI BASA SULLA DOSE TOTALE (Tot: ...)
                            WHEN tvpt.max_s > 0 AND tvpt.dose_cumulativa_storica > tvpt.max_s THEN 4
                            WHEN tvpt.min_s > 0 AND tvpt.dose_cumulativa_storica < tvpt.min_s THEN 3
                            ELSE 1
                        END
                    ) AS priorita_max
                FROM (
                    SELECT
                        vpt.trattamento_id, vpt.az_n, vpt.ag_n, vpt.c_n, vpt.codice,
                        vpt.qta_tendone, vpt.botti_tendone, vpt.solo_bil,
                        vpt.blacklist, vpt.min_s, vpt.max_s, vpt.viol_intervallo,
                        vpt.dose_cumulativa_storica,
                        ROUND(
                            CASE
                                WHEN LOWER(vpt.unita_misura) LIKE '%/hl' AND vpt.botti_tendone > 0 THEN vpt.qta_tendone / (vpt.botti_tendone * 10.0)
                                WHEN LOWER(vpt.unita_misura) LIKE '%/hl' AND vpt.ten_ettari > 0 THEN vpt.qta_tendone / (vpt.ten_ettari * 10.0)
                                WHEN vpt.ten_ettari > 0 THEN vpt.qta_tendone / vpt.ten_ettari
                                ELSE 0
                            END, 4
                        ) AS dose_effettiva
                    FROM (
                        SELECT
                            dt.trattamento_id, dt.tendone_id,
                            MAX(az.nome) AS az_n, MAX(ag.nome) AS ag_n, MAX(c.nome) AS c_n, MAX(ten.codice) AS codice,
                            MAX(ten.ettari) AS ten_ettari,
                            SUM(dt.quantita_sostanza) AS qta_tendone,
                            SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END) AS botti_tendone,
                            MIN(COALESCE(dt.is_bilanciamento, 0)) AS solo_bil,
                            MAX(p.min_sostanza) AS min_s, MAX(p.max_sostanza) AS max_s, MAX(p.blacklist) AS blacklist, MAX(p.unita_misura) AS unita_misura,
                            (
                                -- Per /hl il volume d'acqua è SOMMA(botti) × 10 hl
                                -- (botti reali del tendone), coerente con la dose
                                -- effettiva del singolo trattamento. Fallback a
                                -- ettari × 10 solo quando non risultano botti
                                -- (record storici incompleti). Usare gli ettari
                                -- quando esistono botti reali falsa il "Tot".
                                SELECT ROUND(
                                    CASE
                                        WHEN LOWER(p2.unita_misura) LIKE '%/hl' THEN
                                            CASE WHEN COALESCE(SUM(CASE WHEN dt2.botti > 0 THEN dt2.botti ELSE 0 END), 0) > 0
                                                 THEN SUM(dt2.quantita_sostanza)
                                                      / (SUM(CASE WHEN dt2.botti > 0 THEN dt2.botti ELSE 0 END) * 10.0)
                                                 -- Fallback ettari × 10: protetto da check ettari > 0
                                                 -- per evitare NULL silenzioso (tendone mal configurato
                                                 -- con ettari=0 propagava NULL nella priorita_max).
                                                 WHEN ten2.ettari > 0
                                                 THEN SUM(dt2.quantita_sostanza) / (ten2.ettari * 10.0)
                                                 ELSE 0
                                            END
                                        ELSE
                                            CASE WHEN ten2.ettari > 0
                                                 THEN SUM(dt2.quantita_sostanza) / ten2.ettari
                                                 ELSE 0
                                            END
                                    END, 4
                                )
                                FROM dettaglio_trattamenti dt2
                                JOIN trattamenti t2 ON t2.id = dt2.trattamento_id
                                JOIN prodotti p2 ON p2.id = t2.prodotto_id
                                JOIN tendoni ten2 ON ten2.id = dt2.tendone_id
                                WHERE dt2.tendone_id = dt.tendone_id AND t2.prodotto_id = t.prodotto_id
                                -- Esclude trattamenti "scaduti" (più vecchi di intervallo_min_tratt giorni):
                                -- non vanno conteggiati nel cumulativo per tendone (evita falsi positivi
                                -- di sovradose su trattamenti remoti nel tempo).
                                AND (p2.intervallo_min_tratt IS NULL OR p2.intervallo_min_tratt <= 0
                                     OR julianday('now') - julianday(t2.data_trattamento) <= p2.intervallo_min_tratt)
                                {filtro_inner}
                                GROUP BY p2.unita_misura, ten2.ettari
                            ) AS dose_cumulativa_storica,
                            MAX(
                                CASE WHEN p.intervallo_min_tratt IS NOT NULL AND p.intervallo_min_tratt > 0 AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL) THEN
                                    CASE WHEN (julianday(t.data_trattamento) - julianday((
                                        SELECT t2.data_trattamento
                                        FROM trattamenti t2
                                        JOIN dettaglio_trattamenti dt2 ON t2.id = dt2.trattamento_id
                                        WHERE t2.prodotto_id = t.prodotto_id
                                        AND dt2.tendone_id = dt.tendone_id
                                        AND (dt2.is_bilanciamento = 0 OR dt2.is_bilanciamento IS NULL)
                                        AND (t2.data_trattamento < t.data_trattamento OR (t2.data_trattamento = t.data_trattamento AND t2.id < t.id))
                                        ORDER BY t2.data_trattamento DESC, t2.id DESC
                                        LIMIT 1
                                    ))) < p.intervallo_min_tratt THEN 1 ELSE 0 END
                                ELSE 0 END
                            ) AS viol_intervallo
                        FROM dettaglio_trattamenti dt
                        JOIN trattamenti t ON t.id = dt.trattamento_id
                        JOIN prodotti p ON p.id = t.prodotto_id
                        JOIN tendoni ten ON ten.id = dt.tendone_id
                        JOIN contrade c ON c.id = ten.contrada_id
                        JOIN agri ag ON ag.id = c.agro_id
                        JOIN aziende az ON az.id = ag.azienda_id
                        WHERE 1=1 {filtro_dettaglio}
                        GROUP BY dt.trattamento_id, dt.tendone_id
                    ) vpt
                    WHERE vpt.qta_tendone > 0
                ) tvpt
                GROUP BY tvpt.trattamento_id
            ) tg ON tg.trattamento_id = t.id
            WHERE (tg.qta_totale > 0 OR EXISTS (
                    SELECT 1 FROM dettaglio_trattamenti
                    WHERE trattamento_id = t.id AND is_bilanciamento = 1
                  ))
              AND NOT (tg.priorita_max = 5 AND :vista_corrente = 'AUTORIZZATI')
              {filtro_autorizzato}
              {filtro_periodo}
            ORDER BY t.data_trattamento DESC, t.id DESC
        """

        try:
            with self.engine.connect() as conn:
                # --- MODIFICA QUESTA RIGA ---
                risultati = conn.execute(text(query_sql), {"vista_corrente": self.tipo_vista, **params_periodo}).mappings().all()

                trattamenti_veri = []
                bilanciamenti_per_padre = {}

                if self.tipo_vista == "AUTORIZZATI":
                    mappa_padri = self._calcola_mappa_padri(conn)
                else:
                    mappa_padri = {}

                for riga in risultati:
                    dati_card = dict(riga)

                    if testo_cerca and testo_cerca not in f"{dati_card['Prodotto']} {dati_card['Tendoni']} {dati_card['Azienda']}".lower():
                        continue

                    if dati_card.get("_SOLO_BIL") == 1 and self.tipo_vista == "AUTORIZZATI":
                        id_padre = mappa_padri.get(dati_card["_ID_T"])
                        if id_padre:
                            bilanciamenti_per_padre.setdefault(id_padre, []).append(dati_card)
                        else:
                            trattamenti_veri.append(dati_card)
                    else:
                        trattamenti_veri.append(dati_card)

                for dati_card in trattamenti_veri:
                    card = WidgetTrattamentoCard(
                        dati_card,
                        self.apri_dialog_modifica,
                        self.elimina_record,
                        self.mostra_dettagli,
                        self.gestisci_selezione,
                        self.revisiona_record,
                        self.mostra_operazione_collegati,
                    )
                    if dati_card["_ID_T"] in self._selezionati:
                        card.check_select.setChecked(True)
                    self.layout_card.insertWidget(self.layout_card.count() - 1, card)

                    figli = bilanciamenti_per_padre.get(dati_card["_ID_T"], [])
                    for figlio in figli:
                        wrapper = QWidget()
                        wrapper_layout = QHBoxLayout(wrapper)
                        wrapper_layout.setContentsMargins(40, 0, 0, 0)
                        wrapper_layout.setSpacing(0)

                        sotto_card = WidgetSottoCardBilanciamento(figlio, self.mostra_dettagli, self.gestisci_selezione)
                        if figlio["_ID_T"] in self._selezionati:
                            sotto_card.check_select.setChecked(True)
                        wrapper_layout.addWidget(sotto_card)

                        self.layout_card.insertWidget(self.layout_card.count() - 1, wrapper)

        except Exception as e:
            log.error("Errore caricamento card: %s", e, exc_info=True)

    def _calcola_mappa_padri(self, conn):
        """Per ogni trattamento figlio, estrae l'ID del padre dal campo operatore (es: 'SISTEMA [123]')."""
        mappa = {}

        # Peschiamo tutti i trattamenti di bilanciamento che hanno delle parentesi quadre nell'operatore
        righe = conn.execute(text("""
            SELECT t.id, t.operatore
            FROM trattamenti t
            WHERE t.operatore LIKE '%[%]%'
              AND EXISTS (
                  SELECT 1 FROM dettaglio_trattamenti dt
                  WHERE dt.trattamento_id = t.id AND dt.is_bilanciamento = 1
              )
        """)).fetchall()

        # Pattern stesso usato in `_esporta_selezionati`: cerca un blocco
        # [<digits>] ovunque nella stringa. Vince l'ultimo match (rfind-like
        # via list[-1]) — coerente col formato "SISTEMA: BILANCIAMENTO [123]"
        # dove l'id padre è in coda. Vecchia implementazione `rfind('[')` /
        # `rfind(']')` fallava silenziosa su "[abc]" o "[1][2]" perché
        # estraeva substring non numerica e il ValueError veniva ignorato →
        # il legame figlio↔padre andava perso e la sotto-card non compariva.
        import re
        pattern_id_padre = re.compile(r"\[(\d+)\]")

        for r in righe:
            id_figlio = r[0]
            op = r[1] or ""
            matches = pattern_id_padre.findall(op)
            if matches:
                # In presenza di più [123][456], scegliamo l'ULTIMO (id padre
                # appare in coda al formato di sistema).
                try:
                    mappa[id_figlio] = int(matches[-1])
                except ValueError:
                    log.warning("[mappa_padri] id padre non valido in operatore=%r", op)

        return mappa

    def _trova_figli_bilanciamento(self, conn, ids_padri):
        """Dato un set di ID di trattamenti padri, restituisce gli ID dei figli di bilanciamento collegati.

        Un figlio è un trattamento che:
        - ha solo dettagli con is_bilanciamento=1
        - usa lo stesso prodotto del padre
        - è stato creato per ricevere lo scarico del padre

        Strategia: ricostruisco la mappa padri completa e filtro per i padri richiesti.
        """
        if not ids_padri:
            return set()

        mappa_padri = self._calcola_mappa_padri(conn)
        # mappa_padri è {id_figlio: id_padre}
        # invertiamo: vogliamo {id_padre: [id_figli]}
        ids_padri_set = set(ids_padri)
        figli = set()
        for id_figlio, id_padre in mappa_padri.items():
            if id_padre in ids_padri_set:
                figli.add(id_figlio)
        return figli

    def _esporta_selezionati(self):
        if not self._selezionati:
            QMessageBox.warning(self, "Attenzione", "Nessun trattamento selezionato.")
            return

        # In Revisionati i prodotti in blacklist NON vanno mai esportati,
        # anche se l'utente li ha selezionati prima che il flag fosse
        # impostato (o se è arrivato via sync). Hard-block con feedback.
        is_revisionati = (self.tipo_vista == "AUTORIZZATI")
        blacklist_esclusi: list[tuple[int, str]] = []
        if is_revisionati:
            ids_in = ",".join(str(int(i)) for i in self._selezionati)
            with self.engine.connect() as conn:
                rows = conn.execute(text(f"""
                    SELECT t.id, p.nome_prodotto
                    FROM trattamenti t
                    JOIN prodotti p ON p.id = t.prodotto_id
                    WHERE t.id IN ({ids_in})
                      AND LOWER(TRIM(COALESCE(p.blacklist, ''))) = 'si'
                """)).fetchall()
                blacklist_esclusi = [(int(r[0]), r[1] or "") for r in rows]
            if len(blacklist_esclusi) == len(self._selezionati):
                nomi = ", ".join(sorted({n for _, n in blacklist_esclusi if n}))
                QMessageBox.warning(
                    self, "Esportazione bloccata",
                    "Tutti i trattamenti selezionati hanno prodotti in "
                    f"blacklist ({nomi}) e non possono essere esportati.",
                )
                return

        ids_da_esportare = self._selezionati - {bid for bid, _ in blacklist_esclusi}

        suffisso = "Storico" if self.tipo_vista == "PROPOSTE" else "Revisionati"
        nome_default = f"Trattamenti_{suffisso}.xlsx"

        # Doppio filtro: l'utente sceglie il formato dal dropdown del dialog
        # nativo. Default Excel (compatibile col flusso pre-esistente).
        f_p, selected_filter = QFileDialog.getSaveFileName(
            self, "Esporta", nome_default,
            "Excel (*.xlsx);;PDF (*.pdf)",
        )
        if not f_p:
            return
        # Determina il formato: prima da estensione, poi dal filtro selezionato.
        is_pdf = (
            f_p.lower().endswith('.pdf')
            or (not f_p.lower().endswith('.xlsx') and 'PDF' in selected_filter)
        )
        if is_pdf:
            if not f_p.lower().endswith('.pdf'):
                f_p += '.pdf'
        else:
            if not f_p.lower().endswith('.xlsx'):
                f_p += '.xlsx'

        try:
            import pandas as pd
            from datetime import timedelta

            with self.engine.connect() as conn:
                ids_str = ",".join(map(str, ids_da_esportare))

                # Flag per la vista: in Revisionati usiamo dose netta + qta netta + botti totali (post-bilanciamento)
                # In Storico usiamo dose originaria + qta originaria + botti originali (pre-bilanciamento)

                query = text(f"""
                    SELECT
                        t.id AS "id_trattamento",
                        a.aziende AS "Azienda",
                        a.agri AS "Agro",
                        a.contrade AS "Contrada",
                        a.tendoni AS "Tendoni",
                        ROUND(a.ettari_tot, 4) AS "Ettari Totali",
                        t.data_trattamento AS "Data",
                        p.nome_prodotto AS "Prodotto",
                        p.numero_registrazione AS "N. Registrazione",
                        p.sostanza_attiva AS "Sostanza Attiva",
                        p.avversita AS "Avversità",
                        p.phi_giorni AS "PHI (giorni)",
                        p.unita_misura AS "Unità Misura",
                        ROUND(a.dose_media, 4) AS "Dose",
                        a.botti_tot_finale AS "N. Botti",
                        (a.botti_tot_finale * 1000) AS "Q.tà Acqua (litri)",
                        ROUND(a.qta_totale, 4) AS "Q.tà Totale Prodotto",
                        t.operatore AS "Operatore",
                        t.operazione_id AS "_op_id_raw",
                        t.data_trattamento AS "_data_raw",
                        p.phi_giorni AS "_phi_raw"
                    FROM trattamenti t
                    JOIN prodotti p ON p.id = t.prodotto_id
                    JOIN (
                        SELECT
                            dpt.tratt_id,
                            GROUP_CONCAT(DISTINCT dpt.az_nome) AS aziende,
                            GROUP_CONCAT(DISTINCT dpt.ag_nome) AS agri,
                            GROUP_CONCAT(DISTINCT dpt.c_nome) AS contrade,
                            GROUP_CONCAT(DISTINCT dpt.ten_codice) AS tendoni,
                            SUM(dpt.ten_ettari) AS ettari_tot,
                            SUM({("dpt.qta_netta" if is_revisionati else "dpt.qta_orig")}) AS qta_totale,
                            AVG(dpt.dose_calc) AS dose_media,
                            SUM(CASE WHEN dpt.solo_bil = 1 THEN dpt.botti_tot ELSE {("dpt.botti_tot" if is_revisionati else "dpt.botti_orig")} END) AS botti_tot_finale
                        FROM (
                            SELECT
                                tratt_id, ten_id, ten_codice, ten_ettari, az_nome, ag_nome, c_nome,
                                qta_orig, qta_netta, solo_bil, botti_orig, botti_tot,
                                CASE
                                    WHEN solo_bil = 1 AND LOWER(um) LIKE '%/hl' AND botti_tot > 0 THEN qta_netta / (botti_tot * 10.0)
                                    WHEN solo_bil = 1 AND LOWER(um) LIKE '%/hl' AND ten_ettari > 0 THEN qta_netta / (ten_ettari * 10.0)
                                    WHEN solo_bil = 1 AND ten_ettari > 0 THEN qta_netta / ten_ettari
                                    WHEN LOWER(um) LIKE '%/hl' AND {("botti_tot" if is_revisionati else "botti_orig")} > 0 THEN {("qta_netta" if is_revisionati else "qta_orig")} / ({("botti_tot" if is_revisionati else "botti_orig")} * 10.0)
                                    WHEN LOWER(um) LIKE '%/hl' AND ten_ettari > 0 THEN {("qta_netta" if is_revisionati else "qta_orig")} / (ten_ettari * 10.0)
                                    WHEN ten_ettari > 0 THEN {("qta_netta" if is_revisionati else "qta_orig")} / ten_ettari
                                    ELSE 0
                                END AS dose_calc
                            FROM (
                                SELECT
                                    t.id AS tratt_id, ten.id AS ten_id, ten.codice AS ten_codice, ten.ettari AS ten_ettari,
                                    az.nome AS az_nome, ag.nome AS ag_nome, c.nome AS c_nome,
                                    SUM(CASE WHEN dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL THEN dt.quantita_sostanza ELSE 0 END) AS qta_orig,
                                    SUM(CASE WHEN (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL) AND dt.botti > 0 THEN dt.botti ELSE 0 END) AS botti_orig,
                                    SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END) AS botti_tot,
                                    SUM(dt.quantita_sostanza) AS qta_netta,
                                    MIN(COALESCE(dt.is_bilanciamento, 0)) AS solo_bil,
                                    p.unita_misura AS um
                                FROM dettaglio_trattamenti dt
                                JOIN trattamenti t ON t.id = dt.trattamento_id
                                JOIN tendoni ten ON ten.id = dt.tendone_id
                                JOIN contrade c ON c.id = ten.contrada_id
                                JOIN agri ag ON ag.id = c.agro_id
                                JOIN aziende az ON az.id = ag.azienda_id
                                JOIN prodotti p ON p.id = t.prodotto_id
                                WHERE t.id IN ({ids_str})
                                GROUP BY t.id, ten.id, ten.codice, ten.ettari, az.nome, ag.nome, c.nome, p.unita_misura
                            ) PerTendone
                        ) dpt
                        WHERE {("dpt.qta_netta > 0.0001" if is_revisionati else "dpt.qta_orig > 0.0001")}
                        GROUP BY dpt.tratt_id
                    ) a ON a.tratt_id = t.id
                    ORDER BY t.data_trattamento DESC, t.id DESC
                """)
                df = pd.read_sql(query, conn)

                # Operatore dei BILANCIAMENTI: per i trattamenti figli generati
                # da DialogCompensaDisavanzo, `t.operatore` è
                # "SISTEMA: BILANCIAMENTO [<id_padre>]". Nell'export l'utente vuole
                # vedere l'operatore reale del trattamento di origine, quindi
                # risolviamo l'ID padre con una bulk-lookup. Solo nell'export:
                # il DB resta com'era (l'ID padre serve all'UI per accoppiare la
                # sotto-card via _calcola_mappa_padri).
                import re
                pattern_id_padre = re.compile(r"\[(\d+)\]")
                ids_padre = set()
                for op in df["Operatore"].dropna():
                    m = pattern_id_padre.search(str(op))
                    if m:
                        ids_padre.add(int(m.group(1)))
                mappa_op_padre: dict[int, str] = {}
                if ids_padre:
                    ids_in = ",".join(str(int(i)) for i in ids_padre)
                    rows = conn.execute(text(
                        f"SELECT id, operatore FROM trattamenti WHERE id IN ({ids_in})"
                    )).fetchall()
                    mappa_op_padre = {r[0]: (r[1] or "") for r in rows}

                def risolvi_operatore(op):
                    if not isinstance(op, str):
                        return op
                    m = pattern_id_padre.search(op)
                    if not m:
                        return op
                    id_padre = int(m.group(1))
                    return mappa_op_padre.get(id_padre, op)

                df["Operatore"] = df["Operatore"].apply(risolvi_operatore)

                def calc_giorno_utile(row):
                    try:
                        d = pd.to_datetime(row['_data_raw'])
                        phi = int(row['_phi_raw']) if pd.notna(row['_phi_raw']) else 0
                        return (d + timedelta(days=phi)).date()
                    except Exception:
                        return None

                df['Primo giorno utile raccolta'] = df.apply(calc_giorno_utile, axis=1)

                # Colonna "id_operazione": intero allocato dal server. Stesso
                # valore che appare sul badge della card desktop. Trattamenti
                # della stessa operazione hanno lo stesso valore. Vuota solo
                # per record legacy senza operazione_id assegnato.
                df['id_operazione'] = df['_op_id_raw'].apply(
                    lambda n: int(n) if pd.notna(n) else ''
                )

                colonne_ordinate = [
                    "id_trattamento", "id_operazione",
                    "Azienda", "Agro", "Contrada", "Tendoni", "Ettari Totali",
                    "Data", "Prodotto", "N. Registrazione", "Sostanza Attiva",
                    "Avversità", "PHI (giorni)", "Primo giorno utile raccolta",
                    "Unità Misura", "Dose", "N. Botti", "Q.tà Acqua (litri)",
                    "Q.tà Totale Prodotto", "Operatore"
                ]
                df = df[colonne_ordinate]

                # Avviso opzionale sui trattamenti esclusi per blacklist
                # (vedi pre-check a inizio metodo). Mostrato solo se almeno
                # uno è stato escluso e qualcuno è comunque finito nell'export.
                nota_bl = ""
                if blacklist_esclusi:
                    nomi_bl = ", ".join(sorted({n for _, n in blacklist_esclusi if n}))
                    nota_bl = (
                        f"\n\n{len(blacklist_esclusi)} trattamenti esclusi "
                        f"perché il prodotto è in blacklist: {nomi_bl}."
                    )

                # Dispatch in base al formato scelto dall'utente.
                if is_pdf:
                    self._scrivi_pdf(df, f_p, suffisso)
                    QMessageBox.information(
                        self, "Esportazione",
                        f"File PDF salvato correttamente:\n{f_p}{nota_bl}",
                    )
                    return

                with pd.ExcelWriter(f_p, engine='openpyxl') as writer:
                    df.to_excel(writer, sheet_name=suffisso, index=False)

                    ws = writer.sheets[suffisso]
                    from openpyxl.styles import Font, PatternFill, Alignment

                    header_font = Font(bold=True, color="FFFFFF")
                    header_fill = PatternFill(start_color="2E7D32", end_color="2E7D32", fill_type="solid")
                    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

                    for cell in ws[1]:
                        cell.font = header_font
                        cell.fill = header_fill
                        cell.alignment = header_align

                    for col_idx, column in enumerate(ws.columns, start=1):
                        max_length = max(
                            (len(str(cell.value)) for cell in column if cell.value is not None),
                            default=10
                        )
                        col_letter = ws.cell(row=1, column=col_idx).column_letter
                        ws.column_dimensions[col_letter].width = min(max_length + 2, 35)

                    ws.row_dimensions[1].height = 30
                    ws.freeze_panes = "A2"

            QMessageBox.information(self, "Esportazione", f"File Excel salvato correttamente:\n{f_p}{nota_bl}")
        except ImportError as e:
            # Messaggio specifico in base alla libreria mancante.
            missing = "reportlab" if "reportlab" in str(e) else "pandas/openpyxl"
            QMessageBox.critical(
                self, "Errore",
                f"Manca la libreria '{missing}'.\nInstallala con: pip install {missing}",
            )
        except PermissionError:
            # Caso comune: l'utente ha il file aperto in Excel/LibreOffice.
            # Messaggio dedicato evita all'utente di pensare a un bug grave.
            QMessageBox.critical(
                self, "File in uso",
                f"Impossibile scrivere il file:\n{f_p}\n\n"
                "È aperto in Excel o LibreOffice. Chiudilo e riprova.",
            )
        except Exception as e:
            log.exception("Esportazione trattamenti fallita")
            QMessageBox.critical(self, "Errore", f"Esportazione fallita:\n{str(e)}")

    def _scrivi_pdf(self, df, file_path: str, suffisso: str) -> None:
        """Rendering PDF del dataframe esportato. Usa reportlab.

        Layout: A4 landscape, header verde marchio, tabella con tutte le
        colonne, riga di intestazione ripetuta su ogni pagina automatico
        grazie a `repeatRows=1`. Footer con data generazione + pagina N/M.
        """
        # Import lazy: reportlab pesa ~5 MB, lo carichiamo solo se serve.
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph,
        )
        from datetime import datetime as _dt

        styles = getSampleStyleSheet()
        # Stile titolo: verde marchio (#2E7D32) coerente con UI.
        title_style = ParagraphStyle(
            "title", parent=styles["Heading1"],
            textColor=colors.HexColor("#2E7D32"),
            fontSize=16, spaceAfter=4,
        )
        meta_style = ParagraphStyle(
            "meta", parent=styles["Normal"],
            textColor=colors.HexColor("#616161"),
            fontSize=9, spaceAfter=10,
        )

        # Costruisce i dati come lista di liste, con la riga header in cima.
        # Le colonne sono già nell'ordine giusto perché df è stato riordinato.
        header = list(df.columns)
        rows = df.fillna("—").astype(str).values.tolist()
        data = [header] + rows

        # Calcolo larghezze: distribuiamo equamente sulla pagina A4 landscape.
        page_w, _ = landscape(A4)
        usable_w = page_w - 20 * mm  # margini sinistro+destro
        col_widths = [usable_w / len(header)] * len(header)

        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E7D32")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            # Righe dati
            ("FONTSIZE", (0, 1), (-1, -1), 7),
            ("VALIGN", (0, 1), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F5F5F5")]),
            # Griglia leggera
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BDBDBD")),
        ]))

        # Footer con data + pagina X/Y. ReportLab passa canvas e doc; usiamo
        # una closure semplice.
        def _draw_footer(canvas, doc):
            canvas.saveState()
            canvas.setFont("Helvetica", 7)
            canvas.setFillColor(colors.HexColor("#9E9E9E"))
            data_gen = _dt.now().strftime("%d/%m/%Y %H:%M")
            canvas.drawString(10 * mm, 8 * mm,
                              f"Generato il {data_gen} — AgriMessina QDC")
            canvas.drawRightString(page_w - 10 * mm, 8 * mm,
                                    f"Pagina {doc.page}")
            canvas.restoreState()

        doc = SimpleDocTemplate(
            file_path, pagesize=landscape(A4),
            leftMargin=10 * mm, rightMargin=10 * mm,
            topMargin=12 * mm, bottomMargin=15 * mm,
        )

        story = [
            Paragraph(f"Trattamenti — {suffisso}", title_style),
            Paragraph(
                f"Totale righe: {len(rows)} &nbsp;·&nbsp; "
                f"Generato: {_dt.now().strftime('%d/%m/%Y %H:%M')}",
                meta_style,
            ),
            table,
        ]
        doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)

    def _elimina_selezionati(self):
        if not self._selezionati:
            QMessageBox.warning(self, "Attenzione", "Nessun trattamento selezionato.")
            return

        # Calcolo i figli di bilanciamento collegati ai trattamenti selezionati
        with self.engine.connect() as conn:
            figli_collegati = self._trova_figli_bilanciamento(conn, self._selezionati)

        # Rimuovo dai figli quelli già selezionati esplicitamente (per evitare doppio conteggio nel messaggio)
        figli_aggiuntivi = figli_collegati - self._selezionati
        n = len(self._selezionati)
        n_figli = len(figli_aggiuntivi)

        if n_figli > 0:
            messaggio = (
                f"Hai selezionato {n} trattament{'o' if n == 1 else 'i'}.\n\n"
                f"Di questi, alcuni hanno {n_figli} bilanciament{'o' if n_figli == 1 else 'i'} collegat{'o' if n_figli == 1 else 'i'} che verr{'à' if n_figli == 1 else 'anno'} eliminat{'o' if n_figli == 1 else 'i'} automaticamente.\n\n"
                f"Totale che verrà eliminato: {n + n_figli} trattamenti.\n\n"
                f"Vuoi davvero procedere? L'operazione è irreversibile."
            )
        else:
            messaggio = (
                f"Vuoi davvero eliminare {n} trattament{'o' if n == 1 else 'i'} selezionat{'o' if n == 1 else 'i'}?\n\n"
                f"Questa operazione è irreversibile."
            )

        risposta = QMessageBox.question(
            self, "Conferma eliminazione", messaggio,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if risposta != QMessageBox.StandardButton.Yes:
            return

        try:
            with self.engine.begin() as conn:
                ids_da_eliminare = self._selezionati | figli_collegati
                ids_str = ",".join(map(str, ids_da_eliminare))

                # Pulisci il registro magazzino prima del DELETE (vedi nota
                # in elimina_record singolo).
                from magazzino_logic import (
                    cancella_scarico_reale, cancella_scarico_fittizio,
                )
                stati = dict(conn.execute(text(
                    f"SELECT id, is_autorizzato FROM trattamenti WHERE id IN ({ids_str})"
                )).fetchall())
                for tid in ids_da_eliminare:
                    if stati.get(tid, 0) == 0:
                        cancella_scarico_reale(conn, tid)
                    else:
                        cancella_scarico_fittizio(conn, tid)

                conn.execute(text(f"DELETE FROM dettaglio_trattamenti WHERE trattamento_id IN ({ids_str})"))
                conn.execute(text(f"DELETE FROM avvisi_trattamenti WHERE trattamento_id IN ({ids_str})"))
                conn.execute(text(f"DELETE FROM trattamenti WHERE id IN ({ids_str})"))

            # Accoda una DELETE per ogni trattamento eliminato
            for tid in ids_da_eliminare:
                enqueue_operation(self.engine, "TRATTAMENTO", "DELETE", entity_id=tid)

            tot_eliminati = len(ids_da_eliminare)
            self._selezionati.clear()
            self.btn_exp.setText("📊 ESPORTA SELEZIONATI (0)")
            self.btn_elim_sel.setText("🗑️ ELIMINA SELEZIONATI (0)")
            if hasattr(self, 'btn_sel_tutti'):
                self.btn_sel_tutti.setText("☑️ SELEZIONA TUTTO")
            self.aggiorna_dati()
            self._notify_trattamento_changed()
            QMessageBox.information(
                self, "Eliminazione completata",
                f"{tot_eliminati} trattament{'o' if tot_eliminati == 1 else 'i'} eliminat{'o' if tot_eliminati == 1 else 'i'} con successo."
            )
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Impossibile eliminare i trattamenti:\n{str(e)}")

