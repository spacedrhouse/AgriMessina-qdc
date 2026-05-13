"""Dialog modali per la gestione dei trattamenti.

Estratti da `ui_trattamenti.py` per ridurne la dimensione (era 2700+ righe).
Contiene tutti i `Dialog*` legati alla scheda trattamenti:

- `DialogCompensaDisavanzo`   — bilanciamento del disavanzo verso un altro tendone
- `DialogDettaglioTrattamento` — visualizzazione completa di un trattamento
- `DialogStoricoProdottiTendone` — storico prodotti su un tendone + bilanciamento
- `DialogAlertTrattamento`    — popup di avvisi/violazioni
- `DialogNuovoTrattamento`    — creazione di un nuovo trattamento
- `DialogModificaTrattamento` — modifica di un trattamento esistente

Il pannello principale (`SchedaOperazioni`) rimane in `ui_trattamenti.py`.
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import text
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox,
    QDialog, QComboBox, QLabel, QLineEdit, QDoubleSpinBox,
    QWidget, QDateEdit, QTableView,
    QListWidget, QListWidgetItem, QHeaderView, QFrame, QScrollArea,
)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QColor

from config import CONFIG
from database import ricalcola_avvisi_globali
from local_db import enqueue_operation
from trattamenti_payload import build_trattamento_payload as _build_trattamento_payload


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
            # 1. Recupero metadati prodotto e limiti
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

            # 2. Situazione Tendone Origine
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

            # 3. Estrazione Dati Grezzi per i Candidati (somme semplici in SQL)
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

                    -- Ultimo ID e operatore (anche se il record è un bilanciamento)
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

            # 4. Matematica e Filtri gestiti in Python
            for row in tendoni:
                t_id, t_cod, t_ettari, az_nome, net_qty, net_botti, num_real, last_date, l_id, l_op = row
                t_ettari = float(t_ettari)
                net_qty = float(net_qty)
                net_botti = float(net_botti)

                # Esclusioni
                if p_bio == 'conv' and az_nome.strip() == 'agrimessina':
                    continue

                if p_max_tratt and num_real >= p_max_tratt:
                    continue

                if p_int_min and last_date:
                    d_u = datetime.strptime(str(last_date)[:10], '%Y-%m-%d').date()
                    if (oggi - d_u).days < p_int_min:
                        continue

                # Calcolo Spazio esatto
                botti_calc = net_botti if net_botti > 0 else t_ettari
                if '/hl' in self.um:
                    max_consentito = self.max_s * botti_calc * 10.0
                else:
                    max_consentito = self.max_s * t_ettari

                spazio = round(max_consentito - net_qty, 4)

                if spazio <= 0:
                    continue

                # x_max è il minimo tra quanto disavanzo hai e quanto spazio ha lui
                x_max = round(min(self.src_max_removable, spazio), 4)
                if x_max <= 0:
                    continue

                gia_trattato_vero = num_real > 0
                testo = f"{'★ ' if gia_trattato_vero else ''}{t_cod} (Disp: {t_ettari:.4f} ha | Spazio: {spazio:.2f} {self.um.split('/')[0]})"

                candidati.append((
                    gia_trattato_vero,
                    t_ettari,
                    testo,
                    {'id': t_id, 'x_max': x_max, 'x_min': 0.0001, 'ettari': t_ettari,
                     'spazio': spazio, 'tratt_id': l_id, 'operatore': l_op}
                ))

            # Stelle (già trattati) in cima, poi i più grandi
            candidati.sort(key=lambda c: (c[0], c[1]), reverse=True)

            for _, _, testo, dati in candidati:
                self.combo_target.addItem(testo, userData=dati)

        # 5. Gestione Stato UI
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
        if not dati:
            return
        self.spin_qta.setMinimum(dati['x_min']); self.spin_qta.setMaximum(dati['x_max'])
        self.spin_qta.setValue(max(dati['x_min'], min(self.deficit, dati['x_max'])))
        if dati.get('tratt_id'):
            self.input_operatore.setText(str(dati.get('operatore') or "").strip())
            self.input_operatore.setEnabled(False)
        else:
            self.input_operatore.clear()
            self.input_operatore.setEnabled(True)

    # --- Distribuzione proporzionale "fino al minimo" -----------------------
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
        if n == 0:
            return [], qta_da_scaricare, []

        qta_min_etichetta = (self.min_s * self.src_ettari) if self.min_s > 0 else 0.0

        spazi = []
        for r in righe_origine:
            t_id, q_orig = int(r[0]), float(r[3])

            # Soglia di sopravvivenza dinamica (1% della quantità originale o 0.001)
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

        righe_riepilogo = []
        for d in dettaglio:
            icona = "⚠️" if d["sotto_min"] else "✅"
            righe_riepilogo.append(
                f"  {icona} Trattamento del {d['data']} ({d['qta_originale']:.2f} L) "
                f"→ residuo {d['qta_residua']:.2f} L → dose {d['dose_residua']:.2f} {self.um}"
            )

        # Messaggio adattivo: la causa può essere min-etichetta o soglia di sopravvivenza (EPS)
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
        if not dati:
            return
        qta_richiesta, tratt_id_dest = self.spin_qta.value(), dati['tratt_id']

        try:
            with self.engine.connect() as conn:
                # Race-check: ricalcolo spazio target real-time, lo stato può
                # essere cambiato fra apertura e salva (altri client).
                check = conn.execute(text("""
                    SELECT COALESCE(SUM(dt.quantita_sostanza), 0),
                           COALESCE(SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END), 0)
                    FROM dettaglio_trattamenti dt
                    JOIN trattamenti tr ON tr.id = dt.trattamento_id
                    WHERE dt.tendone_id = :tid AND tr.prodotto_id = :pid
                """), {"tid": dati['id'], "pid": self.prodotto_id}).fetchone()

                net_qty, net_botti = float(check[0]), float(check[1])
                botti_calc = net_botti if net_botti > 0 else dati['ettari']

                if '/hl' in self.um:
                    max_cons = self.max_s * botti_calc * 10.0
                else:
                    max_cons = self.max_s * dati['ettari']

                spazio_attuale = round(max_cons - net_qty, 4)

                if qta_richiesta > spazio_attuale:
                    QMessageBox.warning(self, "Disponibilità Cambiata",
                        f"Attenzione: il tendone destinazione ha ora solo {spazio_attuale} {self.um.split('/')[0]} di spazio.\n"
                        "I dati verranno aggiornati.")
                    self._carica_target()
                    return

                # Distribuzione
                righe_origine = self._carica_righe_origine(conn)
                if not righe_origine:
                    QMessageBox.critical(self, "Errore", "Nessun trattamento origine valido.")
                    return

                quote, residuo, dettaglio = self._calcola_distribuzione_fino_al_minimo(righe_origine, qta_richiesta)

            if residuo > 0:
                tendone_dest_codice = self.combo_target.currentText().split('(')[0].strip().lstrip("★").strip()
                if not self._conferma_scarico_parziale(qta_richiesta, residuo, dettaglio, tendone_dest_codice):
                    return

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
                # Se la testata destinazione esiste già, il bilanciamento NON apporta nuove botti
                botti_dest = 0.0

                if not tratt_id_dest:
                    res = conn.execute(text("INSERT INTO trattamenti (data_trattamento, data_inserimento, prodotto_id, operatore, tipo_trattamento) VALUES (:d, :di, :p, :op, 'Difesa')"), {"d": datetime.now().date(), "di": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "p": self.prodotto_id, "op": operatore_val})
                    tratt_id_dest = res.lastrowid
                    tratt_dest_was_new = True
                    # Botti calcolate solo se creiamo una testata completamente nuova
                    botti_dest = max(1.0, math.ceil(dati['ettari'] * 2) / 2.0)

                conn.execute(text("""
                    INSERT INTO dettaglio_trattamenti (trattamento_id, tendone_id, quantita_sostanza, botti, is_bilanciamento)
                    VALUES (:tr, :te, :q, :b, 1)
                """), {"tr": tratt_id_dest, "te": dati['id'], "q": qta_effettiva, "b": botti_dest})

            # Backend Ops
            for src_tratt_id in src_tratt_ids_modificati:
                payload_src = _build_trattamento_payload(self.engine, src_tratt_id)
                if payload_src:
                    enqueue_operation(self.engine, "TRATTAMENTO", "UPDATE", entity_id=src_tratt_id, payload=payload_src)

            payload_dest = _build_trattamento_payload(self.engine, tratt_id_dest)
            if payload_dest:
                op = "INSERT" if tratt_dest_was_new else "UPDATE"
                enqueue_operation(self.engine, "TRATTAMENTO", op, entity_id=tratt_id_dest, payload=payload_dest)

            ricalcola_avvisi_globali(self.engine)
            self.accept()
            QMessageBox.information(self, "Successo", "Compensazione eseguita!")

        except Exception as e:
            # Rollback UI: ricarica i target per riallineare lo stato.
            self._carica_target()
            QMessageBox.critical(self, "Errore", f"Operazione fallita:\n{str(e)}")


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

            if r['blacklist'] == 'si':
                avvisi.append(f"⛔ ALLARME BLACKLIST su {codice}: Il prodotto «{prodotto}» è VIETATO.")

            if r['bio_conv'] == 'conv' and r['azienda_nome'] == 'agrimessina':
                avvisi.append(f"⚠️ Tendone {codice}: Prodotto Convenzionale («{prodotto}») in azienda Biologica.")

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

        # Intervallo minimo: estratto dal testo pre-calcolato di avvisi_trattamenti
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

            titolo = QLabel(d['nome_prodotto'] or "Prodotto sconosciuto")
            titolo.setStyleSheet("font-size: 24px; font-weight: bold; color: #212121;")
            self.v_layout.addWidget(titolo)

            self._add_riga("Data", d['data_trattamento'])
            self._add_riga("Operatore", d['operatore'])
            self._add_riga("Tendoni", d['tendoni_originali'])

            if getattr(self, 'nascondi_bilanciamenti', False) is False:
                # Sezione: Scarichi di Bilanciamento.
                # Riscritta a 2 step (lista scarichi + lookup destinazione per
                # ognuno) perché la versione monolitica con correlated subquery
                # + ORDER BY CASE su outer alias fallisce su SQLite < 3.39
                # (Windows: alcuni build di Python embeddano versioni vecchie).
                # Più verbosa ma portabile + più leggibile.
                scarichi_raw = conn.execute(text("""
                    SELECT dt.id AS scarico_id,
                           dt.quantita_sostanza,
                           p.id AS prodotto_id,
                           p.unita_misura
                    FROM dettaglio_trattamenti dt
                    JOIN trattamenti t ON t.id = dt.trattamento_id
                    JOIN prodotti p ON p.id = t.prodotto_id
                    WHERE dt.trattamento_id = :tid
                      AND dt.is_bilanciamento = 1
                      AND dt.quantita_sostanza < 0
                    ORDER BY dt.id
                """), {"tid": tid}).mappings().all()

                def _trova_tendone_dest(scarico_id: int, qta_abs: float, prodotto_id: int) -> str | None:
                    """Per ogni scarico (qta negativa), trova il tendone di carico
                    corrispondente: prima match esatto di quantità, altrimenti
                    primo carico successivo (id maggiore) sullo stesso prodotto."""
                    # Pass 1: match esatto di quantità (carico positivo == scarico in modulo).
                    match = conn.execute(text("""
                        SELECT ten.codice
                        FROM dettaglio_trattamenti dt
                        JOIN tendoni ten ON ten.id = dt.tendone_id
                        JOIN trattamenti t ON t.id = dt.trattamento_id
                        WHERE dt.is_bilanciamento = 1
                          AND dt.quantita_sostanza > 0
                          AND t.prodotto_id = :pid
                          AND ABS(dt.quantita_sostanza - :qta_abs) < 0.001
                        ORDER BY dt.id ASC
                        LIMIT 1
                    """), {"pid": prodotto_id, "qta_abs": qta_abs}).scalar()
                    if match:
                        return match
                    # Pass 2: fallback temporale — primo carico con id maggiore.
                    return conn.execute(text("""
                        SELECT ten.codice
                        FROM dettaglio_trattamenti dt
                        JOIN tendoni ten ON ten.id = dt.tendone_id
                        JOIN trattamenti t ON t.id = dt.trattamento_id
                        WHERE dt.is_bilanciamento = 1
                          AND dt.quantita_sostanza > 0
                          AND t.prodotto_id = :pid
                          AND dt.id > :sid
                        ORDER BY dt.id ASC
                        LIMIT 1
                    """), {"pid": prodotto_id, "sid": scarico_id}).scalar()

                scarichi = []
                for s in scarichi_raw:
                    qta_abs = abs(float(s['quantita_sostanza']))
                    dest = _trova_tendone_dest(int(s['scarico_id']), qta_abs, int(s['prodotto_id']))
                    scarichi.append({
                        'quantita_sostanza': s['quantita_sostanza'],
                        'unita_misura': s['unita_misura'],
                        'tendone_destinazione': dest,
                    })

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

                # Sezione: Carichi di Bilanciamento
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

            # Sezione: Avvisi e Violazioni.
            # In Storico: usa avvisi pre-calcolati (stato originale).
            # In Revisionati: ricalcola lo stato post-bilanciamento per ogni tendone.
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

                    -- (Somma Totale Quantità) / (Somma Totale Volumi)
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
            rimanenza = round((max_s - d_cum) * self.ettari * (10.0 if '/hl' in um else 1.0), 4)

            if max_s > 0 and d_cum > max_s:
                bg = QColor(255, 210, 210)
            elif float(riga[7] or 0) > 0 and d_cum < float(riga[7] or 0):
                bg = QColor(210, 225, 255)
            else:
                bg = QColor(210, 255, 210)

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
            # Senza messaggio, la selezione persa dopo _carica() darebbe l'impressione
            # di "non succede nulla" al secondo click.
            QMessageBox.information(
                self, "Seleziona un prodotto",
                "Clicca prima su una riga della tabella per scegliere il prodotto "
                "da bilanciare, poi premi 'Bilancia Disavanzo'.",
            )
            return
        nome_prodotto = self.tabella.model().item(idx.row(), 0).text()

        with self.engine.connect() as conn:
            limiti = conn.execute(text("SELECT id, max_sostanza, unita_misura FROM prodotti WHERE nome_prodotto=:n"), {"n": nome_prodotto}).fetchone()

            # Blocco se manca la dose massima: senza limiti d'etichetta non si bilancia
            if not limiti[1] or float(limiti[1]) <= 0:
                QMessageBox.warning(self, "Operazione non consentita",
                                    f"Il prodotto '{nome_prodotto}' non ha una dose massima definita in anagrafica.\n"
                                    "Il bilanciamento è possibile solo per prodotti con limiti di etichetta.")
                return

            stats = conn.execute(text("SELECT SUM(quantita_sostanza), SUM(botti) FROM dettaglio_trattamenti dt JOIN trattamenti tr ON tr.id = dt.trattamento_id WHERE tr.prodotto_id = :pid AND dt.tendone_id = :tid"), {"pid": limiti[0], "tid": self.tendone_id}).fetchone()

        qta_tot, botti_tot, max_s, um = float(stats[0] or 0), float(stats[1] or 0), float(limiti[1] or 0), str(limiti[2]).lower()
        if '/hl' in um:
            deficit = round(qta_tot - (max_s * (botti_tot if botti_tot > 0 else self.ettari) * 10.0), 4)
        else:
            deficit = round(qta_tot - (max_s * self.ettari), 4)

        if deficit > 0:
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

        self._inizializza_dati()

    def _inizializza_dati(self):
        with self.engine.connect() as conn:
            for p in conn.execute(text("SELECT id, nome_prodotto, unita_misura FROM prodotti ORDER BY nome_prodotto")).fetchall():
                self.combo_prodotto.addItem(p[1], userData={'id': p[0], 'um': p[2]})
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
        if not id_co:
            return
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
            # Sospende il timer del parent per sicurezza extra
            if self.parent() and hasattr(self.parent(), 'timer_autosync'):
                self.parent().timer_autosync.stop()

            with self.engine.begin() as conn:
                # 1. Testata
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

                # 2. Dettagli
                for t in tendoni_sel:
                    pro_quota = t['e'] / tot_area
                    conn.execute(text("""
                        INSERT INTO dettaglio_trattamenti (trattamento_id, tendone_id, quantita_sostanza, botti, dose_ha, is_bilanciamento)
                        VALUES (:tr, :te, :q, :b, :d, 0)
                    """), {
                        "tr": tratt_id, "te": t['id'], "q": round(qta_tot * pro_quota, 4),
                        "b": round(botti_tot * pro_quota, 4), "d": dose_ha
                    })

                # Tutto dentro al with usando 'conn'
                payload = _build_trattamento_payload(conn, tratt_id)
                if payload:
                    enqueue_operation(conn, "TRATTAMENTO", "INSERT", entity_id=tratt_id, payload=payload)

                # NOTA: lo scarico magazzino è ora server-side: quando il backend
                # riceve l'INSERT del trattamento, esegue sincronizza_scarico e
                # popola registro_magazzino. Il client lo riceve al pull successivo.
                ricalcola_avvisi_globali(conn)

            # Riabilita il timer (usa CONFIG.autosync_ms, non un valore hardcoded
            # che era divergente).
            if self.parent() and hasattr(self.parent(), 'timer_autosync'):
                self.parent().timer_autosync.start(CONFIG.autosync_ms)
            self.accept()

        except Exception as e:
            if self.parent() and hasattr(self.parent(), 'timer_autosync'):
                self.parent().timer_autosync.start(CONFIG.autosync_ms)
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
            # 1. Testata
            t = conn.execute(text("""
                SELECT data_trattamento, operatore, prodotto_id
                FROM trattamenti WHERE id = :id
            """), {"id": self.trattamento_id}).mappings().first()

            if not t:
                return

            self.date_edit.setDate(QDate.fromString(str(t['data_trattamento']), Qt.DateFormat.ISODate))
            self.input_operatore.setText(t['operatore'] or "")

            for i in range(self.combo_prodotto.count()):
                if self.combo_prodotto.itemData(i).get('id') == t['prodotto_id']:
                    self.combo_prodotto.setCurrentIndex(i)
                    break

            # 2. Dettagli (solo non-bilanciamento)
            dettagli = conn.execute(text("""
                SELECT tendone_id, quantita_sostanza, botti
                FROM dettaglio_trattamenti
                WHERE trattamento_id = :id AND (is_bilanciamento = 0 OR is_bilanciamento IS NULL)
            """), {"id": self.trattamento_id}).fetchall()

            qta_tot = sum(d[1] for d in dettagli)
            botti_tot = sum(d[2] or 0 for d in dettagli)

            self.spin_qta_totale.setValue(qta_tot)
            self.spin_botti.setValue(botti_tot)

            # In Revisionati, blocchiamo la modifica di tutto tranne la testata
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

        if '/hl' in dati_prod.get('um', '').lower():
            divisore = (botti_tot if botti_tot > 0 else tot_area) * 10.0
            dose_ha = round(qta_tot / divisore, 4) if divisore > 0 else 0
        else:
            dose_ha = round(qta_tot / tot_area, 4) if tot_area > 0 else 0

        try:
            with self.engine.begin() as conn:
                # 1. Testata
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

                # 2. Sostituzione dettagli (solo normali, non bilanciamento)
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

            # NOTA: lo scarico magazzino è ora server-side. Il backend, al PUT
            # del trattamento, ricalcola via sincronizza_scarico e aggiorna
            # registro_magazzino. Il client lo pulla via /magazzino.

            ricalcola_avvisi_globali(self.engine)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Errore", str(e))
