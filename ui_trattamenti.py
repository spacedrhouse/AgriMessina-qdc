from datetime import datetime
from sqlalchemy import text
import os
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox,
                             QDialog, QComboBox, QLabel, QLineEdit, QDoubleSpinBox,
                             QFormLayout, QWidget, QDateEdit, QTableView, QFileDialog,
                             QListWidget, QListWidgetItem, QHeaderView, QFrame, QScrollArea,
                             QCheckBox, QSizePolicy, QGraphicsDropShadowEffect) # <-- AGGIUNGI QUESTO
from PyQt6.QtCore import Qt, QDate, QRect
from PyQt6.QtSql import QSqlQueryModel
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QColor
from PyQt6.QtGui import QPainter, QPainterPath, QPen

from ui_core import (PannelloBaseDialog, DelegateCheckboxStorico,
                     DelegateConflittiStorico, MultiFilterProxyModel)
from database import ricalcola_avvisi_globali
from local_db import enqueue_operation
from app_logging import get_logger

log = get_logger(__name__)
from PyQt6.QtWidgets import QScrollArea, QLineEdit
from PyQt6.QtWidgets import QCheckBox
from PyQt6.QtWidgets import QSizePolicy
import math

# --- INIZIO FILE ui_trattamenti.py ---

# Logica di scarico estratta in magazzino_logic.py per essere riusabile da
# sync.py (non vogliamo trascinarci PyQt6). Manteniamo l'alias storico
# `_sincronizza_scarico_magazzino` per non rompere i call site esistenti.
from magazzino_logic import sincronizza_scarico as _sincronizza_scarico_magazzino  # noqa: F401

def _build_trattamento_payload(engine_or_conn, trattamento_id):
    # Modificato per usare la connessione esistente se fornita
    if hasattr(engine_or_conn, "execute"):
        return _esegui_build_payload(engine_or_conn, trattamento_id)
    else:
        with engine_or_conn.connect() as conn:
            return _esegui_build_payload(conn, trattamento_id)

def _esegui_build_payload(conn, trattamento_id):
    t = conn.execute(text(
        "SELECT data_trattamento, prodotto_id, operatore, tipo_trattamento, "
        "modalita_fertilizzazione, is_autorizzato, data_inserimento, scaricato_magazzino "
        "FROM trattamenti WHERE id = :id"
    ), {"id": trattamento_id}).first()
    if not t: return None

    det_rows = conn.execute(text(
        "SELECT tendone_id, quantita_sostanza, botti, dose_ha, is_bilanciamento "
        "FROM dettaglio_trattamenti WHERE trattamento_id = :id"
    ), {"id": trattamento_id}).fetchall()

    data_str = t[0].isoformat() if hasattr(t[0], "isoformat") else str(t[0])[:10]
    # Se data_inserimento è nullo (es. trattamento creato da app mobile senza
    # quel campo popolato), usa data_trattamento come fallback per evitare
    # 422/NOT NULL costraint lato server.
    data_ins_raw = t[6]
    if data_ins_raw:
        data_ins_str = data_ins_raw.isoformat() if hasattr(data_ins_raw, "isoformat") else str(data_ins_raw)
    else:
        data_ins_str = data_str
    # Nota: `is_autorizzato` NON viene incluso nel payload generico di UPDATE.
    # È un campo gestito esclusivamente tramite gli endpoint dedicati
    # /trattamenti/{id}/autorizza e /trattamenti/{id}/revoca. In questo modo
    # un edit dei dettagli da parte del desktop non sovrascrive un'autorizzazione
    # appena fatta da un altro client (es. app mobile).
    return {
        "data_trattamento": data_str,
        "data_inserimento": data_ins_str,
        "scaricato_magazzino": str(t[7] or "0"),
        "prodotto_id": int(t[1]) if t[1] is not None else None,
        "operatore": t[2],
        "tipo_trattamento": t[3] or "Difesa",
        "modalita_fertilizzazione": t[4],
        "dettagli": [
            {
                "tendone_id": int(d[0]) if d[0] is not None else None,
                "quantita_sostanza": float(d[1] or 0),
                "botti": float(d[2]) if d[2] is not None else None,
                "dose_ha": float(d[3]) if d[3] is not None else None,
                "is_bilanciamento": int(d[4] or 0),
            } for d in det_rows
        ],
    }



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

class DialogCompensaDisavanzo(QDialog):
    def __init__(self, engine, prodotto_id, nome_prodotto, tendone_origine_id, tendone_origine_codice, deficit, parent=None):
        super().__init__(parent)
        self.engine, self.prodotto_id, self.tendone_origine_id, self.deficit = engine, prodotto_id, tendone_origine_id, deficit
        self.setWindowTitle("Bilancia e Compensa Disavanzo")
        self.setMinimumWidth(650)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"Il tendone <b>{tendone_origine_codice}</b> ha un disavanzo di <b>{deficit:.4f}</b>."))
        self.combo_target = QComboBox()
        layout.addWidget(QLabel("Destinazione:")); layout.addWidget(self.combo_target)

        self.spin_qta = QDoubleSpinBox()
        self.spin_qta.setRange(0.0001, 99999.9999)
        self.spin_qta.setDecimals(4)
        layout.addWidget(QLabel("Quantità:")); layout.addWidget(self.spin_qta)

        self.input_operatore = QLineEdit()
        self.input_operatore.setPlaceholderText("SISTEMA: BILANCIAMENTO")
        layout.addWidget(QLabel("Operatore:")); layout.addWidget(self.input_operatore)

        btn_salva = QPushButton("⚖️ Esegui Compensazione")
        btn_salva.setProperty('class', 'success')
        btn_salva.clicked.connect(self.salva)
        layout.addWidget(btn_salva)

        self.combo_target.currentIndexChanged.connect(self._on_target_changed)
        self._carica_target()

    def _carica_target(self):
        self.combo_target.clear()

        with self.engine.connect() as conn:
            # ==========================================
            # 1. Recupero metadati prodotto e limiti
            # ==========================================
            prod = conn.execute(text("""
                SELECT min_sostanza, max_sostanza, unita_misura, bio_convenzionale,
                       trattamenti_max, intervallo_min_tratt, blacklist
                FROM prodotti WHERE id = :pid
            """), {"pid": self.prodotto_id}).fetchone()

            self.min_s = float(prod[0] or 0)
            self.max_s = float(prod[1] or 0)
            self.um = str(prod[2] or "").lower()
            p_bio = str(prod[3] or "").strip().lower()
            p_max_tratt = prod[4]
            p_int_min = prod[5]
            p_blacklist = str(prod[6] or "").strip().lower() == 'si'

            if p_blacklist:
                self.combo_target.addItem("⛔ Prodotto in BLACKLIST")
                self.combo_target.setEnabled(False)
                self.spin_qta.setEnabled(False)
                self.input_operatore.setEnabled(False)
                return

            # ==========================================
            # 2. Situazione Tendone Origine
            # ==========================================
            src = conn.execute(text("""
                SELECT t.ettari,
                       COALESCE((SELECT SUM(dt.quantita_sostanza) FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE tr.prodotto_id = :pid AND dt.tendone_id = t.id), 0),
                       COALESCE((SELECT SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END) FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE tr.prodotto_id = :pid AND dt.tendone_id = t.id), 0)
                FROM tendoni t
                WHERE t.id = :tid
            """), {"pid": self.prodotto_id, "tid": self.tendone_origine_id}).fetchone()

            self.src_ettari = float(src[0])
            self.src_qta_tot = float(src[1])
            self.src_botti_tot = float(src[2])

            if '/hl' in self.um:
                q_min = self.min_s * (self.src_botti_tot if self.src_botti_tot > 0 else self.src_ettari) * 10.0
            else:
                q_min = self.min_s * self.src_ettari

            self.src_max_removable = max(0.0, round(self.src_qta_tot - q_min, 4))

            # ==========================================
            # 3. Estrazione Dati Grezzi per i Candidati
            # (Zero calcoli complessi in SQL, solo somme!)
            # ==========================================
            tendoni = conn.execute(text("""
                SELECT
                    t.id, t.codice, t.ettari, LOWER(az.nome) AS az_nome,

                    -- A) Quantità netta totale (somma di tutto: veri + bilanciamenti)
                    COALESCE((SELECT SUM(dt.quantita_sostanza)
                     FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id
                     WHERE dt.tendone_id = t.id AND tr.prodotto_id = :pid), 0) AS net_qty,

                    -- B) Botti nette totali
                    COALESCE((SELECT SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END)
                     FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id
                     WHERE dt.tendone_id = t.id AND tr.prodotto_id = :pid), 0) AS net_botti,

                    -- C) Numero di trattamenti VERI (ignora i bilanciamenti per i limiti di etichetta)
                    (SELECT COUNT(DISTINCT tr.id)
                     FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id
                     WHERE dt.tendone_id = t.id AND tr.prodotto_id = :pid
                       AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)) AS num_real,

                    -- D) Dati Ultimo trattamento VERO (per le date)
                    (SELECT tr.data_trattamento FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE dt.tendone_id = t.id AND tr.prodotto_id = :pid AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL) ORDER BY tr.data_trattamento DESC, tr.id DESC LIMIT 1) AS last_date,

                    -- FIX 7: Prendiamo l'ultimo ID e operatore a prescindere che sia VERO o BILANCIAMENTO
                    (SELECT tr.id FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE dt.tendone_id = t.id AND tr.prodotto_id = :pid ORDER BY tr.data_trattamento DESC, tr.id DESC LIMIT 1) AS l_id,
                    (SELECT tr.operatore FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE dt.tendone_id = t.id AND tr.prodotto_id = :pid ORDER BY tr.data_trattamento DESC, tr.id DESC LIMIT 1) AS l_op

                FROM tendoni t
                JOIN contrade c ON c.id = t.contrada_id
                JOIN agri ag ON ag.id = c.agro_id
                JOIN aziende az ON az.id = ag.azienda_id
                WHERE t.id != :tid
            """), {"pid": self.prodotto_id, "tid": self.tendone_origine_id}).fetchall()

            oggi = datetime.now().date()
            candidati = []

            # ==========================================
            # 4. Matematica e Filtri gestiti in Python
            # ==========================================
            for row in tendoni:
                t_id, t_cod, t_ettari, az_nome, net_qty, net_botti, num_real, last_date, l_id, l_op = row
                t_ettari = float(t_ettari)
                net_qty = float(net_qty)
                net_botti = float(net_botti)

                # --- Esclusioni ---
                if p_bio == 'conv' and az_nome.strip() == 'agrimessina':
                    continue

                if p_max_tratt and num_real >= p_max_tratt:
                    continue

                if p_int_min and last_date:
                    d_u = datetime.strptime(str(last_date)[:10], '%Y-%m-%d').date()
                    if (oggi - d_u).days < p_int_min:
                        continue

                # --- Calcolo Spazio esatto ---
                botti_calc = net_botti if net_botti > 0 else t_ettari
                if '/hl' in self.um:
                    max_consentito = self.max_s * botti_calc * 10.0
                else:
                    max_consentito = self.max_s * t_ettari

                spazio = round(max_consentito - net_qty, 4)

                # Se non c'è spazio, scartiamo
                if spazio <= 0:
                    continue

                # x_max è il minimo tra quanto disavanzo hai e quanto spazio ha lui
                x_max = round(min(self.src_max_removable, spazio), 4)
                if x_max <= 0:
                    continue

                # --- Generazione Voce Tendina ---
                gia_trattato_vero = num_real > 0
                testo = f"{'★ ' if gia_trattato_vero else ''}{t_cod} (Disp: {t_ettari:.4f} ha | Spazio: {spazio:.2f} {self.um.split('/')[0]})"

                candidati.append((
                    gia_trattato_vero,
                    t_ettari,
                    testo,
                    {'id': t_id, 'x_max': x_max, 'x_min': 0.0001, 'ettari': t_ettari,
                     'spazio': spazio, 'tratt_id': l_id, 'operatore': l_op}
                ))

            # Ordinamento: le stelle (già trattati) in cima, poi i più grandi
            candidati.sort(key=lambda c: (c[0], c[1]), reverse=True)

            for _, _, testo, dati in candidati:
                self.combo_target.addItem(testo, userData=dati)

        # ==========================================
        # 5. Gestione Stato UI
        # ==========================================
        if self.combo_target.count() == 0:
            self.combo_target.addItem("Nessun tendone idoneo disponibile!")
            self.combo_target.setEnabled(False)
            self.spin_qta.setEnabled(False)
            self.input_operatore.setEnabled(False)
        else:
            self.combo_target.setEnabled(True)
            self.spin_qta.setEnabled(True)
            self.input_operatore.setEnabled(True)
            self._on_target_changed()

    def _on_target_changed(self):
        dati = self.combo_target.currentData()
        if not dati: return
        self.spin_qta.setMinimum(dati['x_min']); self.spin_qta.setMaximum(dati['x_max'])
        self.spin_qta.setValue(max(dati['x_min'], min(self.deficit, dati['x_max'])))
        if dati.get('tratt_id'):
            self.input_operatore.setText(str(dati.get('operatore') or "").strip())
            self.input_operatore.setEnabled(False)
        else:
            self.input_operatore.clear()
            self.input_operatore.setEnabled(True)

    # ------------------------------------------------------------------
    # DISTRIBUZIONE PROPORZIONALE "FINO AL MINIMO"
    # ------------------------------------------------------------------
    def _carica_righe_origine(self, conn):
        """Restituisce per ogni trattamento sul tendone origine la quantità
        netta caricata (esclusi i dettagli di bilanciamento), la data e l'ettaraggio."""
        return conn.execute(text("""
            SELECT dt.trattamento_id,
                   tr.data_trattamento,
                   ten.ettari,
                   SUM(dt.quantita_sostanza) AS qta
            FROM dettaglio_trattamenti dt
            JOIN trattamenti tr ON tr.id = dt.trattamento_id
            JOIN tendoni ten   ON ten.id = dt.tendone_id
            WHERE tr.prodotto_id = :pid
              AND dt.tendone_id  = :tid
              AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)
              AND dt.quantita_sostanza > 0
            GROUP BY dt.trattamento_id
            HAVING SUM(dt.quantita_sostanza) > 0
            ORDER BY tr.data_trattamento ASC, tr.id ASC
        """), {"pid": self.prodotto_id, "tid": self.tendone_origine_id}).fetchall()

    # Soglia minima di "sopravvivenza" di un trattamento dopo bilanciamento.
    # Un trattamento non può MAI scendere sotto questo valore, altrimenti
    # sparirebbe dalla vista Revisionati (filtro qta_tendone > 0) e perderebbe
    # significato storico.
    EPS_SOPRAVVIVENZA = 0.0001

    def _calcola_distribuzione_fino_al_minimo(self, righe_origine, qta_da_scaricare):
        n = len(righe_origine)
        if n == 0: return [], qta_da_scaricare, []

        qta_min_etichetta = (self.min_s * self.src_ettari) if self.min_s > 0 else 0.0

        spazi = []
        for r in righe_origine:
            t_id, q_orig = int(r[0]), float(r[3])

            # FIX 8: Soglia di sopravvivenza dinamica (1% della quantità originale o 0.001)
            soglia_tecnica = max(0.001, q_orig * 0.01)
            pavimento = max(qta_min_etichetta, soglia_tecnica)

            spazio = max(0.0, q_orig - pavimento)
            spazi.append((t_id, q_orig, spazio))

        spazio_totale = sum(s for _, _, s in spazi)
        scaricabile = min(qta_da_scaricare, spazio_totale)
        residuo = round(qta_da_scaricare - scaricabile, 4)

        quote = []
        accumulato = 0.0
        for idx, (t_id, q_orig, sp) in enumerate(spazi):
            # FIX 3: Guardia esplicita contro Division by Zero
            if spazio_totale <= 0:
                quota = 0.0
            elif idx < n - 1:
                quota = round((sp / spazio_totale) * scaricabile, 4)
                accumulato += quota
            else:
                quota = round(scaricabile - accumulato, 4)

            quota = max(0.0, min(quota, sp))
            if quota > 0:
                quote.append((t_id, quota, q_orig))

        # (Mantieni qui la logica di 'dettaglio_residui' che avevi prima)
        dettaglio_residui = []
        for (t_id, quota, q_orig), r in zip(quote, righe_origine):
            data_t = r[1]
            ett = float(r[2] or 0)
            qta_residua = round(q_orig - quota, 4)
            dose_residua = (qta_residua / ett) if ett > 0 else 0.0
            sotto_min = (self.min_s > 0 and dose_residua < self.min_s - 1e-6)
            dettaglio_residui.append({
                "trattamento_id": t_id, "data": str(data_t)[:10] if data_t else "",
                "qta_originale": q_orig, "quota_scaricata": quota, "qta_residua": qta_residua,
                "dose_residua": dose_residua, "sotto_min": sotto_min,
            })

        return quote, residuo, dettaglio_residui

    def _conferma_scarico_parziale(self, qta_richiesta, residuo, dettaglio,
                                    tendone_dest_codice):
        """Mostra un QMessageBox riepilogativo quando lo scarico richiesto
        eccede lo spazio disponibile sui trattamenti origine senza azzerarli.

        L'utente può scegliere se accettare uno scarico PARZIALE (= qta-residuo)
        o annullare e ridurre manualmente la quantità.

        Ritorna True se l'utente conferma di procedere col parziale."""
        scaricabile_effettivo = round(qta_richiesta - residuo, 4)
        sotto = [d for d in dettaglio if d["sotto_min"]]

        # Riepilogo per-trattamento
        righe_riepilogo = []
        for d in dettaglio:
            if d["sotto_min"]:
                icona = "⚠️"
            else:
                icona = "✅"
            righe_riepilogo.append(
                f"  {icona} Trattamento del {d['data']} ({d['qta_originale']:.2f} L) "
                f"→ residuo {d['qta_residua']:.2f} L → dose {d['dose_residua']:.2f} {self.um}"
            )

        # Costruisco un messaggio adattivo: la causa del residuo può essere
        # sia "min etichetta" sia "soglia di sopravvivenza" (= EPS)
        if self.min_s > 0 and len(sotto) > 0:
            causa = (f"Per scaricare l'intera quantità richiesta dovrei portare "
                     f"almeno un trattamento sotto la dose minima etichetta "
                     f"({self.min_s} {self.um}).")
        else:
            causa = ("Per scaricare l'intera quantità richiesta dovrei azzerare "
                     "almeno un trattamento, facendolo sparire dallo storico.")

        riga_avviso_min = ""
        if len(sotto) > 0:
            riga_avviso_min = (
                f"Verrà generato un avviso «Dose Bassa» su {len(sotto)} "
                f"trattament{'o' if len(sotto)==1 else 'i'}, da verificare "
                f"manualmente.<br><br>"
            )

        testo = (
            f"<b>Scarico parziale richiesto.</b><br><br>"
            f"{causa}<br><br>"
            f"Per non distruggere i trattamenti origine, lo scarico verso "
            f"<b>{tendone_dest_codice}</b> sarà ridotto:<br><br>"
            f"  • Richiesto:  <b>{qta_richiesta:.4f} L</b><br>"
            f"  • Scaricabile: <b>{scaricabile_effettivo:.4f} L</b><br>"
            f"  • Resterà sul tendone origine: <b>{residuo:.4f} L</b><br><br>"
            f"<b>Stato finale dei trattamenti origine:</b><br>"
            f"<pre>{chr(10).join(righe_riepilogo)}</pre>"
            f"{riga_avviso_min}"
            f"Per smaltire i {residuo:.4f} L rimanenti potrai eseguire un "
            f"secondo bilanciamento verso un altro tendone target.<br><br>"
            f"<b>Vuoi procedere col scarico parziale?</b>"
        )

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Scarico parziale")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(testo)
        btn_proc = msg.addButton(f"Scarica {scaricabile_effettivo:.2f} L",
                                  QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Annulla", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        return msg.clickedButton() is btn_proc

    def salva(self):
        dati = self.combo_target.currentData()
        if not dati: return
        qta_richiesta, tratt_id_dest = self.spin_qta.value(), dati['tratt_id']

        try:
            with self.engine.connect() as conn:
                # --- FIX 2: Race Condition Check (Ricalcolo Spazio Target Real-Time) ---
                check = conn.execute(text("""
                    SELECT COALESCE(SUM(dt.quantita_sostanza), 0),
                           COALESCE(SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END), 0)
                    FROM dettaglio_trattamenti dt
                    JOIN trattamenti tr ON tr.id = dt.trattamento_id
                    WHERE dt.tendone_id = :tid AND tr.prodotto_id = :pid
                """), {"tid": dati['id'], "pid": self.prodotto_id}).fetchone()

                net_qty, net_botti = float(check[0]), float(check[1])
                botti_calc = net_botti if net_botti > 0 else dati['ettari']

                if '/hl' in self.um: max_cons = self.max_s * botti_calc * 10.0
                else: max_cons = self.max_s * dati['ettari']

                spazio_attuale = round(max_cons - net_qty, 4)

                if qta_richiesta > spazio_attuale:
                    QMessageBox.warning(self, "Disponibilità Cambiata",
                        f"Attenzione: il tendone destinazione ha ora solo {spazio_attuale} {self.um.split('/')[0]} di spazio.\n"
                        "I dati verranno aggiornati.")
                    self._carica_target() # FIX 9: Rinfresca UI
                    return

                # --- Distribuzione ---
                righe_origine = self._carica_righe_origine(conn)
                if not righe_origine:
                    QMessageBox.critical(self, "Errore", "Nessun trattamento origine valido.")
                    return

                quote, residuo, dettaglio = self._calcola_distribuzione_fino_al_minimo(righe_origine, qta_richiesta)

            if residuo > 0:
                tendone_dest_codice = self.combo_target.currentText().split('(')[0].strip().lstrip("★").strip()
                if not self._conferma_scarico_parziale(qta_richiesta, residuo, dettaglio, tendone_dest_codice):
                    return

            # --- FIX 4: Unbounded qta_effettiva (Clamp a zero) ---
            qta_effettiva = max(0.0, round(qta_richiesta - residuo, 4))
            if qta_effettiva <= 0:
                QMessageBox.warning(self, "Errore", "Nessuna quantità scaricabile.")
                return

            src_tratt_id_nominal = max(tid for tid, _, _ in quote)
            operatore_base = self.input_operatore.text().strip() or 'SISTEMA: BILANCIAMENTO'
            operatore_val = f"{operatore_base} [{src_tratt_id_nominal}]"

            with self.engine.begin() as conn:
                src_tratt_ids_modificati = []
                for src_tratt_id, quota, _ in quote:
                    conn.execute(text("""
                        INSERT INTO dettaglio_trattamenti (trattamento_id, tendone_id, quantita_sostanza, botti, is_bilanciamento)
                        VALUES (:tr, :te, :q, 0, 1)
                    """), {"tr": src_tratt_id, "te": self.tendone_origine_id, "q": -quota})
                    src_tratt_ids_modificati.append(src_tratt_id)

                tratt_dest_was_new = False
                # --- FIX 1: Botti Inconsistenti ---
                botti_dest = 0.0 # Se il target esiste già, il bilanciamento NON apporta nuove botti

                if not tratt_id_dest:
                    res = conn.execute(text("INSERT INTO trattamenti (data_trattamento, data_inserimento, prodotto_id, operatore, tipo_trattamento) VALUES (:d, :di, :p, :op, 'Difesa')"), {"d": datetime.now().date(), "di": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "p": self.prodotto_id, "op": operatore_val})
                    tratt_id_dest = res.lastrowid
                    tratt_dest_was_new = True
                    # Assegna botti calcolate SOLO se sta creando una testata totalmente nuova
                    botti_dest = max(1.0, math.ceil(dati['ettari'] * 2) / 2.0)

                conn.execute(text("""
                    INSERT INTO dettaglio_trattamenti (trattamento_id, tendone_id, quantita_sostanza, botti, is_bilanciamento)
                    VALUES (:tr, :te, :q, :b, 1)
                """), {"tr": tratt_id_dest, "te": dati['id'], "q": qta_effettiva, "b": botti_dest})

            # --- Backend Ops ---
            for src_tratt_id in src_tratt_ids_modificati:
                payload_src = _build_trattamento_payload(self.engine, src_tratt_id)
                if payload_src: enqueue_operation(self.engine, "TRATTAMENTO", "UPDATE", entity_id=src_tratt_id, payload=payload_src)

            payload_dest = _build_trattamento_payload(self.engine, tratt_id_dest)
            if payload_dest:
                op = "INSERT" if tratt_dest_was_new else "UPDATE"
                enqueue_operation(self.engine, "TRATTAMENTO", op, entity_id=tratt_id_dest, payload=payload_dest)

            ricalcola_avvisi_globali(self.engine)
            self.accept()
            QMessageBox.information(self, "Successo", "Compensazione eseguita!")

        except Exception as e:
            # --- FIX 9: Rollback UI ---
            self._carica_target()
            QMessageBox.critical(self, "Errore", f"Operazione fallita:\n{str(e)}")

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
    def __init__(self, riga_dati, on_modifica, on_elimina, on_click, on_toggle_select, on_revisiona=None, parent=None):
        super().__init__(parent)
        self.id_trattamento = riga_dati["_ID_T"]
        self.callback_click = on_click
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

        # FIX 2: In Revisionati mostriamo sempre Modifica (ma limitata),
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

class DialogDettaglioTrattamento(QDialog):
    def __init__(self, engine, trattamento_id, tipo_vista="PROPOSTE", parent=None):
        super().__init__(parent)
        self.engine = engine
        self.tipo_vista = tipo_vista
        self.setWindowTitle("Dettaglio Trattamento")
        self.setMinimumWidth(500)
        self.setStyleSheet("background-color: #FDF9F3;")

        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")

        container = QWidget()
        self.v_layout = QVBoxLayout(container)
        self.v_layout.setSpacing(15)

        self._carica_e_disegna(trattamento_id)

        scroll.setWidget(container)
        layout.addWidget(scroll)

        btn_chiudi = QPushButton("OK")
        btn_chiudi.setProperty("class", "success")
        btn_chiudi.clicked.connect(self.accept)
        layout.addWidget(btn_chiudi)
    def _calcola_avvisi_revisionati(self, conn, tid):
        """Calcola gli avvisi di dose nello stato POST-bilanciamento per il trattamento dato."""
        avvisi = []

        # Usiamo subquery per calcolare la dose cumulativa totale su quel tendone
        righe = conn.execute(text("""
            SELECT
                ten.codice,
                ten.ettari,
                p.nome_prodotto,
                p.unita_misura,
                p.min_sostanza,
                p.max_sostanza,
                p.intervallo_min_tratt,
                LOWER(TRIM(p.blacklist)) AS blacklist,
                LOWER(TRIM(p.bio_convenzionale)) AS bio_conv,
                LOWER(TRIM(az.nome)) AS azienda_nome,
                (
                    SELECT SUM(dt2.quantita_sostanza)
                    FROM dettaglio_trattamenti dt2
                    JOIN trattamenti t2 ON t2.id = dt2.trattamento_id
                    WHERE dt2.tendone_id = ten.id AND t2.prodotto_id = p.id
                ) AS qta_netta_cumulativa,
                (
                    SELECT SUM(CASE WHEN dt2.botti > 0 THEN dt2.botti ELSE 0 END)
                    FROM dettaglio_trattamenti dt2
                    JOIN trattamenti t2 ON t2.id = dt2.trattamento_id
                    WHERE dt2.tendone_id = ten.id AND t2.prodotto_id = p.id
                ) AS botti_tot_cumulativi
            FROM dettaglio_trattamenti dt
            JOIN trattamenti t ON t.id = dt.trattamento_id
            JOIN tendoni ten ON ten.id = dt.tendone_id
            JOIN contrade c ON c.id = ten.contrada_id
            JOIN agri ag ON ag.id = c.agro_id
            JOIN aziende az ON az.id = ag.azienda_id
            JOIN prodotti p ON p.id = t.prodotto_id
            WHERE dt.trattamento_id = :tid
            GROUP BY ten.id, p.id
        """), {"tid": tid}).mappings().all()

        for r in righe:
            codice = r['codice']
            prodotto = r['nome_prodotto']
            um = (r['unita_misura'] or '').strip().lower()
            qta = float(r['qta_netta_cumulativa'] or 0)
            ettari = float(r['ettari'] or 0)
            botti_tot = float(r['botti_tot_cumulativi'] or 0)
            min_s = float(r['min_sostanza'] or 0)
            max_s = float(r['max_sostanza'] or 0)

            # Blacklist
            if r['blacklist'] == 'si':
                avvisi.append(f"⛔ ALLARME BLACKLIST su {codice}: Il prodotto «{prodotto}» è VIETATO.")

            # Bio/Conv
            if r['bio_conv'] == 'conv' and r['azienda_nome'] == 'agrimessina':
                avvisi.append(f"⚠️ Tendone {codice}: Prodotto Convenzionale («{prodotto}») in azienda Biologica.")

            # Dose
            if '/hl' in um:
                divisore = botti_tot * 10.0 if botti_tot > 0 else (ettari * 10.0)
                dose_calc = qta / divisore if divisore > 0 else 0
            else:
                dose_calc = qta / ettari if ettari > 0 else 0

            dose_round = round(dose_calc, 1)
            if min_s > 0 and dose_round < round(min_s, 4):
                avvisi.append(f"⚠️ Dose Bassa su {codice}: Calcolata {dose_round} {um} per «{prodotto}» (minimo etichetta: {min_s}).")
            elif max_s > 0 and dose_round > round(max_s, 4):
                avvisi.append(f"⚠️ Dose Eccessiva su {codice}: Calcolata {dose_round} {um} per «{prodotto}» (massimo etichetta: {max_s}).")

        # Intervallo minimo
        avviso_intervallo = conn.execute(text("""
            SELECT testo FROM avvisi_trattamenti WHERE trattamento_id = :tid
        """), {"tid": tid}).scalar()
        if avviso_intervallo:
            for riga in avviso_intervallo.split("\n"):
                if "Intervallo minimo" in riga:
                    avvisi.append(riga)

        return list(dict.fromkeys(avvisi))

    def _add_sezione(self, titolo):
        lbl = QLabel(titolo.upper())
        lbl.setStyleSheet("color: #2E7D32; font-weight: bold; font-size: 12px; margin-top: 10px;")
        linea = QFrame()
        linea.setFrameShape(QFrame.Shape.HLine)
        linea.setStyleSheet("color: #E0E0E0;")
        self.v_layout.addWidget(lbl)
        self.v_layout.addWidget(linea)

    def _add_riga(self, etichetta, valore):
        h = QHBoxLayout()
        lbl_e = QLabel(f"<b>{etichetta}:</b>")
        lbl_v = QLabel(str(valore) if valore not in (None, "") else "—")
        lbl_e.setStyleSheet("color: #757575;")
        h.addWidget(lbl_e); h.addStretch(); h.addWidget(lbl_v)
        self.v_layout.addLayout(h)

    def _carica_e_disegna(self, tid):
        with self.engine.connect() as conn:
            # Query principale: prodotto + dati base trattamento
            d = conn.execute(text("""
                SELECT t.data_trattamento, t.operatore,
                       p.nome_prodotto, p.unita_misura,
                       (SELECT GROUP_CONCAT(DISTINCT ten.codice)
                        FROM dettaglio_trattamenti dt
                        JOIN tendoni ten ON ten.id = dt.tendone_id
                        WHERE dt.trattamento_id = t.id
                          AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)
                       ) AS tendoni_originali
                FROM trattamenti t
                JOIN prodotti p ON p.id = t.prodotto_id
                WHERE t.id = :tid
            """), {"tid": tid}).mappings().first()

            if not d:
                self.v_layout.addWidget(QLabel("Trattamento non trovato."))
                return

            # Titolo
            titolo = QLabel(d['nome_prodotto'] or "Prodotto sconosciuto")
            titolo.setStyleSheet("font-size: 24px; font-weight: bold; color: #212121;")
            self.v_layout.addWidget(titolo)

            self._add_riga("Data", d['data_trattamento'])
            self._add_riga("Operatore", d['operatore'])
            self._add_riga("Tendoni", d['tendoni_originali'])

            # --- IL BLOCCO IF CON L'INDENTAZIONE CORRETTA ---
            if getattr(self, 'nascondi_bilanciamenti', False) == False:
                # --- SEZIONE: SCARICHI DI BILANCIAMENTO ---
                scarichi = conn.execute(text("""
                SELECT
                    dt_scarico.quantita_sostanza,
                    p.unita_misura,
                    -- FIX Destinazione: cerca il match esatto di qta, altrimenti usa la sequenza temporale
                    (SELECT ten_dest.codice
                     FROM dettaglio_trattamenti dt_carico
                     JOIN tendoni ten_dest ON ten_dest.id = dt_carico.tendone_id
                     JOIN trattamenti t_carico ON t_carico.id = dt_carico.trattamento_id
                     WHERE dt_carico.is_bilanciamento = 1
                       AND dt_carico.quantita_sostanza > 0
                       AND t_carico.prodotto_id = p.id
                       AND (
                           ABS(dt_carico.quantita_sostanza - ABS(dt_scarico.quantita_sostanza)) < 0.001
                           OR dt_carico.id > dt_scarico.id
                       )
                     ORDER BY
                       CASE WHEN ABS(dt_carico.quantita_sostanza - ABS(dt_scarico.quantita_sostanza)) < 0.001 THEN 0 ELSE 1 END ASC,
                       dt_carico.id ASC
                     LIMIT 1
                    ) AS tendone_destinazione
                FROM dettaglio_trattamenti dt_scarico
                JOIN trattamenti t ON t.id = dt_scarico.trattamento_id
                JOIN prodotti p ON p.id = t.prodotto_id
                WHERE dt_scarico.trattamento_id = :tid
                  AND dt_scarico.is_bilanciamento = 1
                  AND dt_scarico.quantita_sostanza < 0
                ORDER BY dt_scarico.id
            """), {"tid": tid}).mappings().all()

            if scarichi:
                self._add_sezione("Scarichi di Bilanciamento")
                for s in scarichi:
                    qta_assoluta = abs(s['quantita_sostanza'])
                    um = s['unita_misura'] or ''
                    um_pulita = um.split('/')[0] if '/' in um else um
                    dest = s['tendone_destinazione'] or "(?)"
                    testo_scarico = QLabel(f"− {qta_assoluta:.4g} {um_pulita} verso il tendone {dest}")
                    testo_scarico.setStyleSheet("""
                        background-color: #FFF3E0;
                        color: #E65100;
                        border-left: 3px solid #FB8C00;
                        padding: 8px 12px;
                        border-radius: 4px;
                        font-size: 13px;
                    """)
                    self.v_layout.addWidget(testo_scarico)

                # --- SEZIONE: CARICHI DI BILANCIAMENTO ---
                carichi = conn.execute(text("""
                    SELECT dt.quantita_sostanza, ten.codice AS tendone_destinazione,
                           p.unita_misura
                    FROM dettaglio_trattamenti dt
                    JOIN tendoni ten ON ten.id = dt.tendone_id
                    JOIN trattamenti t ON t.id = dt.trattamento_id
                    JOIN prodotti p ON p.id = t.prodotto_id
                    WHERE dt.trattamento_id = :tid
                      AND dt.is_bilanciamento = 1
                      AND dt.quantita_sostanza > 0
                    ORDER BY dt.id
                """), {"tid": tid}).mappings().all()

                if carichi:
                    self._add_sezione("Carichi di Bilanciamento")
                    for c in carichi:
                        um = c['unita_misura'] or ''
                        um_pulita = um.split('/')[0] if '/' in um else um
                        testo_carico = QLabel(f"+ {c['quantita_sostanza']:.4g} {um_pulita} sul tendone {c['tendone_destinazione']}")
                        testo_carico.setStyleSheet("""
                            background-color: #E8F5E9;
                            color: #1B5E20;
                            border-left: 3px solid #2E7D32;
                            padding: 8px 12px;
                            border-radius: 4px;
                            font-size: 13px;
                        """)
                        self.v_layout.addWidget(testo_carico)

            # --- SEZIONE: AVVISI E VIOLAZIONI ---
            # In Storico: usa gli avvisi pre-calcolati (stato originale)
            # In Revisionati: ricalcola al volo lo stato post-bilanciamento per ogni tendone
            if self.tipo_vista == "PROPOSTE":
                avvisi_testo = conn.execute(
                    text("SELECT testo FROM avvisi_trattamenti WHERE trattamento_id = :tid"),
                    {"tid": tid}
                ).scalar()
                avvisi_lista = avvisi_testo.split("\n") if avvisi_testo else []
            else:
                avvisi_lista = self._calcola_avvisi_revisionati(conn, tid)

            if avvisi_lista:
                self._add_sezione("Avvisi e Violazioni")
                testo_avvisi = "\n".join(avvisi_lista)
                lbl_alert = QLabel(testo_avvisi)
                lbl_alert.setWordWrap(True)
                lbl_alert.setStyleSheet("""
                    background-color: #FFEBEE;
                    color: #B71C1C;
                    border: 1px solid #FFCDD2;
                    padding: 12px;
                    border-radius: 8px;
                    font-size: 13px;
                """)
                self.v_layout.addWidget(lbl_alert)

class DialogStoricoProdottiTendone(QDialog):
    def __init__(self, engine, tendone_id, tendone_codice, ettari, prodotto_filtrato_id=None, parent=None):
        super().__init__(parent)
        self.engine, self.tendone_id = engine, tendone_id
        self.ettari, self.prodotto_filtrato_id = float(ettari), prodotto_filtrato_id
        self.setWindowTitle(f"Storico — {tendone_codice}")
        self.setMinimumSize(850, 450)
        layout = QVBoxLayout(self)

        self.tabella = QTableView()
        self.tabella.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        header = self.tabella.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)

        layout.addWidget(self.tabella)

        btns = QHBoxLayout()
        btn_compensa = QPushButton("⚖️ Bilancia Disavanzo")
        btn_undo = QPushButton("↩️ Annulla Ultimo")
        btn_compensa.setProperty('class', 'warning')
        btn_undo.setProperty('class', 'danger')

        btn_compensa.clicked.connect(self._apri_compensazione)
        btn_undo.clicked.connect(self._annulla_bilanciamento)
        btns.addWidget(btn_compensa); btns.addWidget(btn_undo); btns.addStretch()
        layout.addLayout(btns)
        self._carica()

    def _carica(self):
        with self.engine.connect() as conn:
            filtro_sql = f"AND p.id = {self.prodotto_filtrato_id}" if self.prodotto_filtrato_id else ""
            righe = conn.execute(text(f"""
                SELECT
                    p.nome_prodotto, p.unita_misura, COUNT(DISTINCT t.id),
                    ROUND(SUM(dt.quantita_sostanza), 4),

                    -- RISOLTO A MONTE: (Somma Totale Quantità) / (Somma Totale Volumi)
                    ROUND(CASE
                        WHEN LOWER(p.unita_misura) LIKE '%/hl'
                        THEN SUM(dt.quantita_sostanza) / (:ettari * 10.0)
                        ELSE SUM(dt.quantita_sostanza) / :ettari
                    END, 1) as d_cum,

                    0, MAX(t.data_trattamento), p.min_sostanza, p.max_sostanza
                FROM dettaglio_trattamenti dt
                JOIN trattamenti t ON t.id = dt.trattamento_id
                JOIN prodotti p ON p.id = t.prodotto_id
                WHERE dt.tendone_id = :tid {filtro_sql}
                GROUP BY p.id
                ORDER BY MAX(t.data_trattamento) DESC
            """), {"tid": self.tendone_id, "ettari": self.ettari}).fetchall()

        if not righe:
            self.tabella.setModel(QStandardItemModel(1, 1))
            return

        intestazioni = ["Prodotto", "Unità", "N° Trattamenti", "Qtà Totale", "Dose Cumulativa", "Rimanenza", "Ultimo"]
        modello = QStandardItemModel(len(righe), len(intestazioni))
        modello.setHorizontalHeaderLabels(intestazioni)

        for r, riga in enumerate(righe):
            d_cum, max_s = float(riga[4] or 0), float(riga[8] or 0)
            um = str(riga[1] or "").lower()
            # Calcolo rimanenza coerente con l'anagrafica
            rimanenza = round((max_s - d_cum) * self.ettari * (10.0 if '/hl' in um else 1.0), 4)

            # Colori di stato
            if max_s > 0 and d_cum > max_s: bg = QColor(255, 210, 210)
            elif float(riga[7] or 0) > 0 and d_cum < float(riga[7] or 0): bg = QColor(210, 225, 255)
            else: bg = QColor(210, 255, 210)

            dati_visualizzati = [riga[0], riga[1], riga[2], riga[3], d_cum, rimanenza, riga[7]]
            for c, val in enumerate(dati_visualizzati):
                item = QStandardItem(str(val if val is not None else "—"))
                item.setEditable(False)
                item.setBackground(bg)
                modello.setItem(r, c, item)
        self.tabella.setModel(modello)
        self.tabella.resizeColumnsToContents()

    def _apri_compensazione(self):
        idx = self.tabella.currentIndex()
        if not idx.isValid():
            # La selezione si perde dopo _carica() (setModel() rinnova la tabella).
            # Senza messaggio l'utente vede solo "non succede nulla" al secondo click.
            QMessageBox.information(
                self, "Seleziona un prodotto",
                "Clicca prima su una riga della tabella per scegliere il prodotto "
                "da bilanciare, poi premi 'Bilancia Disavanzo'.",
            )
            return
        nome_prodotto = self.tabella.model().item(idx.row(), 0).text()

        with self.engine.connect() as conn:
            limiti = conn.execute(text("SELECT id, max_sostanza, unita_misura FROM prodotti WHERE nome_prodotto=:n"), {"n": nome_prodotto}).fetchone()

            # --- FIX 5 e 6: Blocco se manca la dose massima ---
            if not limiti[1] or float(limiti[1]) <= 0:
                QMessageBox.warning(self, "Operazione non consentita",
                                    f"Il prodotto '{nome_prodotto}' non ha una dose massima definita in anagrafica.\n"
                                    "Il bilanciamento è possibile solo per prodotti con limiti di etichetta.")
                return

            stats = conn.execute(text("SELECT SUM(quantita_sostanza), SUM(botti) FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE tr.prodotto_id = :pid AND dt.tendone_id = :tid"), {"pid": limiti[0], "tid": self.tendone_id}).fetchone()

        qta_tot, botti_tot, max_s, um = float(stats[0] or 0), float(stats[1] or 0), float(limiti[1] or 0), str(limiti[2]).lower()
        if '/hl' in um: deficit = round(qta_tot - (max_s * (botti_tot if botti_tot > 0 else self.ettari) * 10.0), 4)
        else: deficit = round(qta_tot - (max_s * self.ettari), 4)

        if deficit > 0:
            from ui_trattamenti import DialogCompensaDisavanzo
            if DialogCompensaDisavanzo(self.engine, limiti[0], nome_prodotto, self.tendone_id, "T", deficit, self).exec():
                self._carica()
        else:
            QMessageBox.information(
                self, "Nessun disavanzo",
                f"Il prodotto '{nome_prodotto}' è già entro la dose massima "
                f"(disavanzo: {deficit:.4f}). Niente da bilanciare.",
            )

    def _annulla_bilanciamento(self):
        idx = self.tabella.currentIndex()
        if not idx.isValid():
            QMessageBox.information(
                self, "Seleziona un prodotto",
                "Clicca prima su una riga della tabella per scegliere il prodotto "
                "su cui annullare l'ultimo bilanciamento.",
            )
            return
        nome_prodotto = self.tabella.model().item(idx.row(), 0).text()
        with self.engine.begin() as conn:
            pid = conn.execute(text("SELECT id FROM prodotti WHERE nome_prodotto=:n"), {"n": nome_prodotto}).scalar()
            # Raccogli prima gli ID dei trattamenti che verranno toccati, così
            # possiamo accodare un UPDATE per ognuno DOPO la cancellazione.
            tratt_ids_toccati = [r[0] for r in conn.execute(text("""
                SELECT DISTINCT trattamento_id FROM dettaglio_trattamenti
                WHERE is_bilanciamento = 1
                  AND trattamento_id IN (SELECT id FROM trattamenti WHERE prodotto_id = :p)
            """), {"p": pid}).fetchall()]

            conn.execute(text("DELETE FROM dettaglio_trattamenti WHERE is_bilanciamento = 1 AND trattamento_id IN (SELECT id FROM trattamenti WHERE prodotto_id = :p)"), {"p": pid})

        # Per ogni trattamento toccato, accoda un UPDATE col nuovo set di dettagli
        for tid in tratt_ids_toccati:
            payload = _build_trattamento_payload(self.engine, tid)
            if payload:
                enqueue_operation(self.engine, "TRATTAMENTO", "UPDATE",
                                  entity_id=tid, payload=payload)

        self._carica()

class DialogAlertTrattamento(QDialog):
    def __init__(self, alert_text, violazione, prodotto, data, tendoni, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚠️ Dettagli Alert")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Prodotto:</b> {prodotto}<br><b>Data:</b> {data}<br><b>Tendoni:</b> {tendoni}"))
        if alert_text:
            layout.addWidget(QLabel("<b>🔔 Messaggi:</b>"))
            for riga in alert_text.strip().split("\n"):
                if riga.strip():
                    l = QLabel(riga.strip()); l.setWordWrap(True); layout.addWidget(l)
        if violazione:
            l = QLabel("🚨 Grave violazione rilevata."); l.setStyleSheet("color: red; font-weight: bold;"); layout.addWidget(l)
        btn = QPushButton("Chiudi"); btn.clicked.connect(self.accept); layout.addWidget(btn)

class SchedaOperazioni(QWidget):
    # Finestra di "freschezza" dei dati post-reconcile: entro questi secondi
    # il pre-check su Modifica/Revisiona/Elimina viene saltato per evitare
    # latenza HTTP inutile (i dati locali sono allineati al server).
    RECONCILE_FRESHNESS_SECONDS = 10.0

    def __init__(self, engine, db, tipo_vista="PROPOSTE", api=None, notifier=None):
        super().__init__()
        self.engine, self.db, self.tipo_vista = engine, db, tipo_vista
        self.api = api  # opzionale: usato per pre-check su click Modifica/Revisiona/Elimina
        self.notifier = notifier  # opzionale: per leggere last_successful_reconcile_ts
        self._selezionati = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        # --- SOLO RICERCA ---
        h_filtri = QHBoxLayout()
        self.search_bar = QLineEdit()
        self.search_bar.setObjectName("SearchBar")
        self.search_bar.setPlaceholderText("🔍 Cerca trattamenti per prodotto, tendone o azienda...")
        self.search_bar.textChanged.connect(self.aggiorna_dati)
        h_filtri.addWidget(self.search_bar)
        layout.addLayout(h_filtri)

        # --- PULSANTI AZIONE ---
        # --- PULSANTI AZIONE ---
        h_azioni = QHBoxLayout()
        self.btn_nuovo = QPushButton("+ NUOVO TRATTAMENTO")
        self.btn_nuovo.setProperty("class", "success")
        self.btn_nuovo.clicked.connect(self.apri_dialog_nuovo)

        # FIX 1: Nascondi il tasto se siamo in Revisionati
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
        if self.api is None:
            return "offline"

        # Cache "recente": se reconcile è appena passato, fidati dei dati locali
        if self.notifier is not None:
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

                # 1. STORNO MAGAZZINO
                # Lo scarico automatico è ora gestito server-side: alla DELETE
                # del trattamento via API, il CASCADE FK lato server pulisce
                # registro_magazzino. Il client lo riceve via pull successivo.
                # Pulizia locale: elimino solo eventuali residui di vecchio
                # 'Push Consumi' (note con timestamp), per legacy compatibility.
                for tid in ids_da_eliminare:
                    ts_scarico = conn.execute(text("SELECT scaricato_magazzino FROM trattamenti WHERE id = :id"), {"id": tid}).scalar()
                    if ts_scarico and str(ts_scarico) != "0":
                        conn.execute(text("DELETE FROM registro_magazzino WHERE note LIKE '%' || :ts"), {"ts": ts_scarico})

                # 2. Pulizia tabelle collegate e testata
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
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Errore", f"Impossibile eliminare: {str(e)}")

    def apri_dialog_nuovo(self):
        from ui_trattamenti import DialogNuovoTrattamento
        if DialogNuovoTrattamento(self.engine, self).exec():
            self.aggiorna_dati()
            self._notify_trattamento_changed()

    def mostra_dettagli(self, trattamento_id):
        dialog = DialogDettaglioTrattamento(self.engine, trattamento_id, self.tipo_vista, self)
        dialog.exec()

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

            # Usa l'endpoint dedicato /trattamenti/{id}/autorizza invece di UPDATE
            # generico: evita di rispedire l'intero payload (incluso dettagli)
            # che potrebbe sovrascrivere modifiche fatte da altri client.
            enqueue_operation(self.engine, "TRATTAMENTO", "AUTORIZZA",
                              entity_id=trattamento_id)

            self.aggiorna_dati()
            self._notify_trattamento_changed()
        except Exception as e:
            import traceback
            traceback.print_exc()
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
                                SELECT ROUND(
                                    CASE
                                        WHEN LOWER(p2.unita_misura) LIKE '%/hl' THEN
                                            SUM(dt2.quantita_sostanza) / (ten2.ettari * 10.0)
                                        ELSE
                                            SUM(dt2.quantita_sostanza) / ten2.ettari
                                    END, 4
                                )
                                FROM dettaglio_trattamenti dt2
                                JOIN trattamenti t2 ON t2.id = dt2.trattamento_id
                                JOIN prodotti p2 ON p2.id = t2.prodotto_id
                                JOIN tendoni ten2 ON ten2.id = dt2.tendone_id
                                WHERE dt2.tendone_id = dt.tendone_id AND t2.prodotto_id = t.prodotto_id
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
            ORDER BY t.data_trattamento DESC, t.id DESC
        """

        try:
            with self.engine.connect() as conn:
                # --- MODIFICA QUESTA RIGA ---
                risultati = conn.execute(text(query_sql), {"vista_corrente": self.tipo_vista}).mappings().all()

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
            import traceback
            traceback.print_exc()

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

        for r in righe:
            id_figlio = r[0]
            op = r[1] or ""
            try:
                # Cerca l'ultimo blocco [ID] nella stringa
                start = op.rfind('[') + 1
                end = op.rfind(']')
                if start > 0 and end > start:
                    id_padre = int(op[start:end])
                    mappa[id_figlio] = id_padre
            except ValueError:
                pass

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

        suffisso = "Storico" if self.tipo_vista == "PROPOSTE" else "Revisionati"
        nome_default = f"Trattamenti_{suffisso}.xlsx"

        f_p, _ = QFileDialog.getSaveFileName(self, "Esporta", nome_default, "Excel (*.xlsx)")
        if not f_p:
            return
        if not f_p.lower().endswith('.xlsx'):
            f_p += '.xlsx'

        try:
            import pandas as pd
            from datetime import timedelta

            with self.engine.connect() as conn:
                ids_str = ",".join(map(str, self._selezionati))

                # Flag per la vista: in Revisionati usiamo dose netta + qta netta + botti totali (post-bilanciamento)
                # In Storico usiamo dose originaria + qta originaria + botti originali (pre-bilanciamento)
                is_revisionati = (self.tipo_vista == "AUTORIZZATI")

                query = text(f"""
                    SELECT
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

                colonne_ordinate = [
                    "Azienda", "Agro", "Contrada", "Tendoni", "Ettari Totali",
                    "Data", "Prodotto", "N. Registrazione", "Sostanza Attiva",
                    "Avversità", "PHI (giorni)", "Primo giorno utile raccolta",
                    "Unità Misura", "Dose", "N. Botti", "Q.tà Acqua (litri)",
                    "Q.tà Totale Prodotto", "Operatore"
                ]
                df = df[colonne_ordinate]

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

            QMessageBox.information(self, "Esportazione", f"File Excel salvato correttamente:\n{f_p}")
        except ImportError:
            QMessageBox.critical(self, "Errore", "Per esportare in Excel servono le librerie pandas e openpyxl.\nInstallale con: pip install pandas openpyxl")
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Errore", f"Esportazione fallita:\n{str(e)}")

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

                # 1. STORNO MAGAZZINO
                # Server-side via CASCADE FK alla DELETE del trattamento via API.
                # Qui pulisco solo eventuali residui legacy del vecchio Push Consumi.
                for tid in ids_da_eliminare:
                    ts_scarico = conn.execute(text("SELECT scaricato_magazzino FROM trattamenti WHERE id = :id"), {"id": tid}).scalar()
                    if ts_scarico and str(ts_scarico) != "0":
                        conn.execute(text("DELETE FROM registro_magazzino WHERE note LIKE '%' || :ts"), {"ts": ts_scarico})

                # 2. Eliminazione tabelle
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

class DialogNuovoTrattamento(QDialog):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine, self._selezione = engine, {}
        self.setWindowTitle("Nuovo Trattamento")
        self.setMinimumWidth(650)
        layout = QVBoxLayout(self)

        # Campi input principali
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.input_operatore = QLineEdit()
        self.input_operatore.setPlaceholderText("Nome dell'operatore...")

        self.combo_prodotto = QComboBox()

        # Selezione Posizione a Cascata
        self.combo_azienda = QComboBox()
        self.combo_agro = QComboBox()
        self.combo_contrada = QComboBox()
        h_loc = QHBoxLayout()
        h_loc.addWidget(self.combo_azienda)
        h_loc.addWidget(self.combo_agro)
        h_loc.addWidget(self.combo_contrada)

        # Lista Tendoni con Checkbox
        self.list_tendoni = QListWidget()
        self.lbl_riepilogo = QLabel("Ettari Selezionati: 0.0000 ha")
        self.lbl_riepilogo.setStyleSheet("font-weight: bold; color: #2196F3;")

        # Dosi e Botti
        self.spin_qta_totale = QDoubleSpinBox()
        self.spin_qta_totale.setRange(0.00, 99999.99)
        self.spin_qta_totale.setDecimals(4)

        self.spin_botti = QDoubleSpinBox()
        self.spin_botti.setRange(0.0, 9999.9)
        self.spin_botti.setSuffix(" Botti")

        # Composizione Layout
        layout.addWidget(QLabel("<b>Data Trattamento:</b>"))
        layout.addWidget(self.date_edit)
        layout.addWidget(QLabel("<b>Operatore:</b>"))
        layout.addWidget(self.input_operatore)
        layout.addWidget(QLabel("<b>Prodotto:</b>"))
        layout.addWidget(self.combo_prodotto)
        layout.addWidget(QLabel("<b>Filtra per Posizione:</b>"))
        layout.addLayout(h_loc)
        layout.addWidget(QLabel("<b>Seleziona Tendoni:</b>"))
        layout.addWidget(self.list_tendoni)
        layout.addWidget(self.lbl_riepilogo)

        h_dosi = QHBoxLayout()
        h_dosi.addWidget(QLabel("<b>Quantità Totale:</b>"))
        h_dosi.addWidget(self.spin_qta_totale)
        h_dosi.addWidget(QLabel("<b>N. Botti:</b>"))
        h_dosi.addWidget(self.spin_botti)
        layout.addLayout(h_dosi)

        btn_salva = QPushButton("💾 REGISTRA TRATTAMENTO")
        btn_salva.setProperty('class', 'success')
        btn_salva.setMinimumHeight(40)
        btn_salva.clicked.connect(self.salva)
        layout.addWidget(btn_salva)

        # Connessioni segnali
        self.combo_azienda.currentIndexChanged.connect(self.carica_agri)
        self.combo_agro.currentIndexChanged.connect(self.carica_contrade)
        self.combo_contrada.currentIndexChanged.connect(self.carica_tendoni)
        self.list_tendoni.itemChanged.connect(self.gestisci_spunta)

        # Popolamento iniziale
        self._inizializza_dati()

    def _inizializza_dati(self):
        with self.engine.connect() as conn:
            # Carica Prodotti
            for p in conn.execute(text("SELECT id, nome_prodotto, unita_misura FROM prodotti ORDER BY nome_prodotto")).fetchall():
                self.combo_prodotto.addItem(p[1], userData={'id': p[0], 'um': p[2]})
            # Carica Aziende
            for az in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall():
                self.combo_azienda.addItem(az[1], userData=az[0])

    def carica_agri(self):
        self.combo_agro.clear()
        id_az = self.combo_azienda.currentData()
        if id_az:
            with self.engine.connect() as conn:
                for r in conn.execute(text("SELECT id, nome FROM agri WHERE azienda_id=:id"), {"id": id_az}).fetchall():
                    self.combo_agro.addItem(r[1], userData=r[0])

    def carica_contrade(self):
        self.combo_contrada.clear()
        id_ag = self.combo_agro.currentData()
        if id_ag:
            with self.engine.connect() as conn:
                for r in conn.execute(text("SELECT id, nome FROM contrade WHERE agro_id=:id"), {"id": id_ag}).fetchall():
                    self.combo_contrada.addItem(r[1], userData=r[0])

    def carica_tendoni(self):
        id_co = self.combo_contrada.currentData()
        if not id_co: return
        self.list_tendoni.blockSignals(True)
        self.list_tendoni.clear()
        with self.engine.connect() as conn:
            for t in conn.execute(text("SELECT id, codice, ettari FROM tendoni WHERE contrada_id = :id ORDER BY codice"), {"id": id_co}).fetchall():
                item = QListWidgetItem(f"{t[1]} ({t[2]:.4f} ha)")
                item.setData(Qt.ItemDataRole.UserRole, {'id': t[0], 'e': t[2], 'c': t[1]})
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.list_tendoni.addItem(item)
        self.list_tendoni.blockSignals(False)

    def gestisci_spunta(self, item):
        dati = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._selezione[dati['id']] = dati
        else:
            self._selezione.pop(dati['id'], None)

        tot_ettari = sum(d['e'] for d in self._selezione.values())
        self.lbl_riepilogo.setText(f"Ettari Selezionati: {tot_ettari:.4f} ha")

    def salva(self):
        tendoni_sel = list(self._selezione.values())
        if not tendoni_sel or self.spin_qta_totale.value() <= 0:
            QMessageBox.warning(self, "Attenzione", "Seleziona almeno un tendone e inserisci la quantità del prodotto.")
            return

        tot_area = sum(d['e'] for d in tendoni_sel)
        dati_prod = self.combo_prodotto.currentData()
        qta_tot = self.spin_qta_totale.value()
        botti_tot = self.spin_botti.value()

        # Calcolo Dose Applicata
        if '/hl' in dati_prod.get('um', '').lower():
            divisore = (botti_tot if botti_tot > 0 else tot_area) * 10.0
            dose_ha = round(qta_tot / divisore, 4) if divisore > 0 else 0
        else:
            dose_ha = round(qta_tot / tot_area, 4) if tot_area > 0 else 0

        try:
            # FERMA IL TIMER DEL PADRE (se possibile) per sicurezza extra
            if self.parent() and hasattr(self.parent(), 'timer_autosync'):
                self.parent().timer_autosync.stop()

            with self.engine.begin() as conn:
                # 1. Inserimento testata
                res = conn.execute(text("""
                    INSERT INTO trattamenti (data_trattamento, data_inserimento, prodotto_id, operatore, tipo_trattamento)
                    VALUES (:d, :di, :p, :o, 'Difesa')
                """), {
                    "d": self.date_edit.date().toPyDate(),
                    "di": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "p": dati_prod['id'],
                    "o": self.input_operatore.text().strip()
                })
                tratt_id = res.lastrowid

                # 2. Inserimento dettagli
                for t in tendoni_sel:
                    pro_quota = t['e'] / tot_area
                    conn.execute(text("""
                        INSERT INTO dettaglio_trattamenti (trattamento_id, tendone_id, quantita_sostanza, botti, dose_ha, is_bilanciamento)
                        VALUES (:tr, :te, :q, :b, :d, 0)
                    """), {
                        "tr": tratt_id, "te": t['id'], "q": round(qta_tot * pro_quota, 4),
                        "b": round(botti_tot * pro_quota, 4), "d": dose_ha
                    })

                # --- TUTTO DENTRO IL WITH USANDO 'conn' ---
                payload = _build_trattamento_payload(conn, tratt_id) # Usa conn!
                if payload:
                    enqueue_operation(conn, "TRATTAMENTO", "INSERT", entity_id=tratt_id, payload=payload)

                # NOTA: lo scarico magazzino è ora server-side. Quando il
                # backend riceve l'INSERT del trattamento, esegue il proprio
                # sincronizza_scarico e popola registro_magazzino. Il client
                # vedrà gli scarichi al prossimo pull /magazzino/movimenti.
                ricalcola_avvisi_globali(conn) # Usa conn!

            # Dopo il successo, riabilita il timer e chiudi
            if self.parent() and hasattr(self.parent(), 'timer_autosync'):
                self.parent().timer_autosync.start(2000)
            self.accept()

        except Exception as e:
            # Riabilita il timer anche in caso di errore
            if self.parent() and hasattr(self.parent(), 'timer_autosync'):
                self.parent().timer_autosync.start(2000)
            QMessageBox.critical(self, "Errore Database", str(e))

class DialogModificaTrattamento(DialogNuovoTrattamento):
    def __init__(self, engine, trattamento_id, parent=None):
        super().__init__(engine, parent)
        self.trattamento_id = trattamento_id
        self.setWindowTitle(f"Modifica Trattamento #{trattamento_id}")
        self._carica_dati_esistenti()

    def _carica_dati_esistenti(self):
        """Popola la UI con i dati attuali del database locale."""
        with self.engine.connect() as conn:
            # 1. Carica testata
            t = conn.execute(text("""
                SELECT data_trattamento, operatore, prodotto_id
                FROM trattamenti WHERE id = :id
            """), {"id": self.trattamento_id}).mappings().first()

            if not t: return

            self.date_edit.setDate(QDate.fromString(str(t['data_trattamento']), Qt.DateFormat.ISODate))
            self.input_operatore.setText(t['operatore'] or "")

            # Seleziona prodotto nella combo
            for i in range(self.combo_prodotto.count()):
                if self.combo_prodotto.itemData(i).get('id') == t['prodotto_id']:
                    self.combo_prodotto.setCurrentIndex(i)
                    break

            # 2. Carica dettagli (solo quelli non di bilanciamento)
            dettagli = conn.execute(text("""
                SELECT tendone_id, quantita_sostanza, botti
                FROM dettaglio_trattamenti
                WHERE trattamento_id = :id AND (is_bilanciamento = 0 OR is_bilanciamento IS NULL)
            """), {"id": self.trattamento_id}).fetchall()

            # Calcola totali per la UI
            qta_tot = sum(d[1] for d in dettagli)
            botti_tot = sum(d[2] or 0 for d in dettagli)

            self.spin_qta_totale.setValue(qta_tot)
            self.spin_botti.setValue(botti_tot)

            # Spunta i tendoni nella lista (questo richiede che i tendoni siano già caricati)
            ids_selezionati = {d[0] for d in dettagli}
            # Nota: a seconda di come carichi la lista, potresti dover forzare
            # il caricamento dei tendoni corretti prima di questa fase.

            # --- AGGIUNGI IL FIX 2 QUI ---
            if hasattr(self.parent(), 'tipo_vista') and self.parent().tipo_vista == "AUTORIZZATI":
                self.combo_prodotto.setEnabled(False)
                self.combo_azienda.setEnabled(False)
                self.combo_agro.setEnabled(False)
                self.combo_contrada.setEnabled(False)
                self.list_tendoni.setEnabled(False)
                self.spin_qta_totale.setEnabled(False)
                self.spin_botti.setEnabled(False)
                self.setWindowTitle(f"Modifica Autorizzato #{self.trattamento_id} (Solo Testata)")

    def salva(self):
        """Sovrascrive la logica di salvataggio per eseguire un UPDATE."""
        tendoni_sel = list(self._selezione.values())
        if not tendoni_sel or self.spin_qta_totale.value() <= 0:
            QMessageBox.warning(self, "Attenzione", "Seleziona almeno un tendone.")
            return

        # Pre-check D1: il record esiste ancora sul server al momento del save?
        # Tra l'apertura del dialog e ora un altro utente potrebbe averlo
        # cancellato. Sfrutta la cache di freschezza del parent.
        parent = self.parent()
        if hasattr(parent, '_verifica_esistenza_server') and hasattr(parent, '_handle_gone'):
            check = parent._verifica_esistenza_server(self.trattamento_id)
            if check == "gone":
                parent._handle_gone(self.trattamento_id)
                self.reject()
                return

        tot_area = sum(d['e'] for d in tendoni_sel)
        dati_prod = self.combo_prodotto.currentData()
        qta_tot = self.spin_qta_totale.value()
        botti_tot = self.spin_botti.value()

        # Calcolo Dose
        if '/hl' in dati_prod.get('um', '').lower():
            divisore = (botti_tot if botti_tot > 0 else tot_area) * 10.0
            dose_ha = round(qta_tot / divisore, 4) if divisore > 0 else 0
        else:
            dose_ha = round(qta_tot / tot_area, 4) if tot_area > 0 else 0

        try:
            with self.engine.begin() as conn:
                # 1. Update testata
                conn.execute(text("""
                    UPDATE trattamenti SET
                        data_trattamento = :d,
                        prodotto_id = :p,
                        operatore = :o
                    WHERE id = :id
                """), {
                    "d": self.date_edit.date().toPyDate(),
                    "p": dati_prod['id'],
                    "o": self.input_operatore.text().strip(),
                    "id": self.trattamento_id
                })

                # 2. Sostituzione dettagli (solo normali)
                conn.execute(text("""
                    DELETE FROM dettaglio_trattamenti
                    WHERE trattamento_id = :id AND (is_bilanciamento = 0 OR is_bilanciamento IS NULL)
                """), {"id": self.trattamento_id})

                for t in tendoni_sel:
                    pro_quota = t['e'] / tot_area
                    conn.execute(text("""
                        INSERT INTO dettaglio_trattamenti (trattamento_id, tendone_id, quantita_sostanza, botti, dose_ha, is_bilanciamento)
                        VALUES (:tr, :te, :q, :b, :d, 0)
                    """), {
                        "tr": self.trattamento_id, "te": t['id'],
                        "q": round(qta_tot * pro_quota, 4), "b": round(botti_tot * pro_quota, 4),
                        "d": dose_ha
                    })

            # 3. Accoda UPDATE per il server
            payload = _build_trattamento_payload(self.engine, self.trattamento_id)
            if payload:
                enqueue_operation(self.engine, "TRATTAMENTO", "UPDATE",
                                  entity_id=self.trattamento_id, payload=payload)

            # NOTA: lo scarico magazzino è ora server-side. Il backend, al
            # PUT del trattamento, ricalcola via sincronizza_scarico e
            # aggiorna registro_magazzino. Il client lo pulla via /magazzino.

            ricalcola_avvisi_globali(self.engine)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Errore", str(e))
