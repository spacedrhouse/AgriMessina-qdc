import pandas as pd
from sqlalchemy import text
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox,
                             QDialog, QComboBox, QLabel, QLineEdit, QDoubleSpinBox,
                             QSpinBox, QFormLayout, QScrollArea, QWidget, QDateEdit,
                             QTableView, QFileDialog, QHeaderView)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtSql import QSqlQueryModel, QSqlDatabase
from PyQt6.QtGui import QStandardItemModel, QStandardItem

from ui_core import PannelloBaseDialog, DelegateMovimenti

class DialogProdotto(QDialog):
    CAMPI = [
        ("nome_prodotto",            "Nome Prodotto *",            "text",         None),
        ("categoria",                "Categoria",                  "text",         None),
        ("numero_registrazione",     "N° Registrazione",           "text",         None),
        ("sostanza_attiva",          "Sostanza Attiva",            "text",         None),
        ("bio_convenzionale",        "Bio / Convenzionale",        "combo",        ["", "Bio", "Conv"]),
        ("avversita",                "Avversità",                  "text",         None),
        ("titolo_n",                 "Titolo N",                   "decimal",      None),
        ("titolo_p",                 "Titolo P",                   "decimal",      None),
        ("titolo_k",                 "Titolo K",                   "decimal",      None),
        ("phi_giorni",               "PHI (giorni)",               "intero",       None),
        ("trattamenti_max",          "Max Trattamenti/Anno",       "intero",       None),
        ("intervallo_min_tratt",     "Intervallo Minimo (giorni)", "intero",       None),
        ("unita_misura",             "Unità di Misura",            "combo",        ["", "L/ha", "kg/ha", "ml/ha", "g/ha", "g/hl", "ml/hl", "unità/ha"]),
        ("min_sostanza",             "Dose Minima",                "decimal",      None),
        ("max_sostanza",             "Dose Massima",               "decimal",      None),
        ("qta_acqua",                "Quantità Acqua (L/ha)",      "decimal",      None),
        ("blacklist",                "Blacklist",                  "combo",        ["No", "Si"]),
    ]

    def __init__(self, engine, dati=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.dati   = dati or {}
        self.widget_map = {}

        self.setWindowTitle("Modifica Prodotto" if dati else "Nuovo Prodotto")
        self.setMinimumWidth(480)

        outer  = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        contenuto = QWidget()
        form = QFormLayout(contenuto)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)
        scroll.setWidget(contenuto)
        outer.addWidget(scroll)

        for campo, etichetta, tipo, opzioni in self.CAMPI:
            valore = self.dati.get(campo)
            if tipo == "text":
                w = QLineEdit()
                w.setText(str(valore) if valore is not None else "")
            elif tipo in ("combo", "combo_cascata", "combo_modale"):
                w = QComboBox()
                w.addItems(opzioni)
                if valore and str(valore) in opzioni:
                    w.setCurrentText(str(valore))
            elif tipo == "decimal":
                w = QDoubleSpinBox()
                w.setRange(0.0, 999999.99)
                w.setDecimals(2)
                w.setSpecialValueText("—")
                try: w.setValue(float(valore)) if valore is not None else w.setValue(0.0)
                except: w.setValue(0.0)
            elif tipo == "intero":
                w = QSpinBox()
                w.setRange(0, 999999)
                w.setSpecialValueText("—")
                try: w.setValue(int(valore)) if valore is not None else w.setValue(0)
                except: w.setValue(0)
            self.widget_map[campo] = (w, tipo)
            form.addRow(etichetta, w)

        btns = QHBoxLayout()
        btn_salva   = QPushButton("💾 Salva")
        btn_annulla = QPushButton("Annulla")

        btn_salva.setProperty('class', 'success')
        btn_annulla.setProperty('class', 'secondary')

        btn_salva.clicked.connect(self.salva)
        btn_annulla.clicked.connect(self.reject)
        btns.addWidget(btn_salva)
        btns.addWidget(btn_annulla)
        outer.addLayout(btns)

    def salva(self):
        valori = {}
        for campo, (w, tipo) in self.widget_map.items():
            if tipo == "text": v = w.text().strip() or None
            elif tipo in ("combo", "combo_cascata", "combo_modale"): v = w.currentText().strip() or None
            elif tipo == "decimal": v = w.value() if w.value() > 0.0 else None
            elif tipo == "intero": v = w.value() if w.value() > 0 else None
            valori[campo] = v

        if not valori.get("nome_prodotto"):
            QMessageBox.critical(self, "Errore", "Il campo 'Nome Prodotto' è obbligatorio!")
            return

        try:
            # L'uso di .begin() garantisce l'atomicità: o tutto o niente
            with self.engine.begin() as conn:
                if self.dati.get("id"):
                    # MODIFICA
                    set_clause = ", ".join(f"{c} = :{c}" for c in valori)
                    valori["id"] = self.dati["id"]
                    conn.execute(text(f"UPDATE prodotti SET {set_clause} WHERE id = :id"), valori)
                    op_type = "UPDATE"
                    entity_id = self.dati["id"]
                else:
                    # INSERIMENTO
                    cols = ", ".join(valori.keys())
                    ph   = ", ".join(f":{c}" for c in valori)
                    # Se questo execute fallisce (es. nome duplicato),
                    # il codice salta direttamente all'except e NON salva nulla
                    res = conn.execute(text(f"INSERT INTO prodotti ({cols}) VALUES ({ph})"), valori)
                    op_type = "INSERT"
                    entity_id = res.lastrowid # Recupera l'ID appena generato

            # Se siamo arrivati qui, il database locale è GIÀ AGGIORNATO e CHIUSO con successo.
            # Ora possiamo dire al sistema di sincronizzazione di inviarlo al server
            from local_db import enqueue_operation
            enqueue_operation(self.engine, "PRODOTTO", op_type, entity_id=entity_id, payload=valori)

            self.accept()

        except Exception as e:
            # Se siamo qui, il blocco 'with' ha fatto il ROLLBACK automatico in locale
            messaggio = str(e)
            if "UNIQUE constraint failed" in messaggio:
                QMessageBox.warning(self, "Prodotto Esistente",
                                    f"Errore: Il prodotto '{valori['nome_prodotto']}' esiste già.\n"
                                    "L'operazione è stata annullata e non è stato salvato nulla.")
            else:
                QMessageBox.critical(self, "Errore Database", f"Salvataggio fallito: {messaggio}")

class DialogNuovoMovimento(QDialog):
    def __init__(self, engine, prodotto_id, um, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.prodotto_id = prodotto_id
        self.setWindowTitle("Registra Movimento Magazzino")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.data_edit = QDateEdit(QDate.currentDate())
        self.data_edit.setCalendarPopup(True)
        self.combo_tipo = QComboBox()
        self.combo_tipo.addItems(["CARICO", "SCARICO"])

        self.combo_azienda = QComboBox()
        self.combo_azienda.addItem("Nessuna / Magazzino Generale", userData=None)
        with self.engine.connect() as conn:
            for az in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall():
                self.combo_azienda.addItem(az[1], userData=az[0])

        self.spin_qta = QDoubleSpinBox()
        self.spin_qta.setRange(0.01, 999999.99)
        self.spin_qta.setDecimals(4)
        self.spin_qta.setSuffix(f" {um}")

        self.edit_ddt = QLineEdit()
        self.edit_fornitore = QLineEdit()
        self.edit_note = QLineEdit()

        form.addRow("Data Movimento:", self.data_edit)
        form.addRow("Tipo Operazione:", self.combo_tipo)
        form.addRow("Azienda Proprietaria:", self.combo_azienda)
        form.addRow("Quantità:", self.spin_qta)
        form.addRow("N° DDT (Opzionale):", self.edit_ddt)
        form.addRow("Fornitore (Opzionale):", self.edit_fornitore)
        form.addRow("Note:", self.edit_note)
        layout.addLayout(form)

        btns = QHBoxLayout()
        btn_salva = QPushButton("💾 Registra Movimento")
        btn_salva.setProperty('class', 'success')
        btn_salva.clicked.connect(self.salva)
        btns.addWidget(btn_salva)
        layout.addLayout(btns)

    def salva(self):
        try:
            with self.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO registro_magazzino (prodotto_id, azienda_id, data_movimento, tipo_movimento, quantita, n_ddt, fornitore, note)
                    VALUES (:pid, :az, :d, :tipo, :q, :ddt, :fornitore, :note)
                """), {
                    "pid": self.prodotto_id, "az": self.combo_azienda.currentData(),
                    "d": self.data_edit.date().toPyDate(), "tipo": self.combo_tipo.currentText(),
                    "q": self.spin_qta.value(), "ddt": self.edit_ddt.text().strip() or None,
                    "fornitore": self.edit_fornitore.text().strip() or None, "note": self.edit_note.text().strip() or None
                })
            self.accept()
        except Exception as e: QMessageBox.critical(self, "Errore", str(e))


class DialogModificaMovimento(QDialog):
    def __init__(self, engine, dati, um, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.id_mov = dati['id']
        self.setWindowTitle("Modifica Movimento Magazzino")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.data_edit = QDateEdit()
        self.data_edit.setCalendarPopup(True)
        self.data_edit.setDate(QDate.fromString(dati['data'], Qt.DateFormat.ISODate) if isinstance(dati['data'], str) else dati['data'])

        self.combo_tipo = QComboBox()
        self.combo_tipo.addItems(["CARICO", "SCARICO"])
        self.combo_tipo.setCurrentText(dati['tipo'])
        self.combo_tipo.setEnabled(False)

        self.combo_azienda = QComboBox()
        self.combo_azienda.addItem("Nessuna / Magazzino Generale", userData=None)
        with self.engine.connect() as conn:
            for az in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall():
                self.combo_azienda.addItem(az[1], userData=az[0])
                if az[0] == dati['azienda_id']: self.combo_azienda.setCurrentIndex(self.combo_azienda.count() - 1)

        self.spin_qta = QDoubleSpinBox()
        self.spin_qta.setRange(0.01, 999999.99)
        self.spin_qta.setDecimals(4)
        self.spin_qta.setValue(dati['qta'])

        self.edit_ddt = QLineEdit(str(dati['ddt'] or ""))
        self.edit_fornitore = QLineEdit(str(dati['fornitore'] or ""))
        self.edit_note = QLineEdit(str(dati['note'] or ""))

        form.addRow("Data Movimento:", self.data_edit)
        form.addRow("Tipo Operazione:", self.combo_tipo)
        form.addRow("Azienda Proprietaria:", self.combo_azienda)
        form.addRow("Quantità:", self.spin_qta)
        form.addRow("N° DDT:", self.edit_ddt)
        form.addRow("Fornitore:", self.edit_fornitore)
        form.addRow("Note:", self.edit_note)
        layout.addLayout(form)

        btn_salva = QPushButton("💾 Salva Modifiche")
        btn_salva.setProperty('class', 'warning')
        btn_salva.clicked.connect(self.salva)
        layout.addWidget(btn_salva)

    def salva(self):
        try:
            with self.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE registro_magazzino SET azienda_id=:az, data_movimento=:d, tipo_movimento=:tipo,
                    quantita=:q, n_ddt=:ddt, fornitore=:fornitore, note=:note WHERE id=:id
                """), {
                    "az": self.combo_azienda.currentData(), "d": self.data_edit.date().toPyDate(),
                    "tipo": self.combo_tipo.currentText(), "q": self.spin_qta.value(),
                    "ddt": self.edit_ddt.text().strip() or None, "fornitore": self.edit_fornitore.text().strip() or None,
                    "note": self.edit_note.text().strip() or None, "id": self.id_mov
                })
            self.accept()
        except Exception as e: QMessageBox.critical(self, "Errore", str(e))


class DialogRegistroProdotto(QDialog):
    def __init__(self, engine, db, prodotto_id, nome_prodotto, um, parent=None,
                 azienda_filter: str | None = None, azienda_ids: list | None = None):
        super().__init__(parent)
        self.engine, self.db = engine, db
        self.prodotto_id, self.um = prodotto_id, str(um or "")
        self.azienda_filter = azienda_filter
        self.azienda_ids = azienda_ids or []

        # --- ESTRAIAMO L'UNITÀ DI MISURA ASSOLUTA (es. da "kg/ha" a "kg") ---
        self.um_pulita = self.um.split('/')[0].strip() if '/' in self.um else self.um

        titolo_filter = f" ({azienda_filter})" if azienda_filter else ""
        self.setWindowTitle(f"Registro Carico/Scarico — {nome_prodotto}{titolo_filter}")
        self.setMinimumSize(950, 500)

        layout = QVBoxLayout(self)
        self.lbl_giacenza = QLabel()
        self.lbl_giacenza.setStyleSheet("font-size: 18px; font-weight: bold; padding: 10px; border-bottom: 1px solid #555;")
        layout.addWidget(self.lbl_giacenza)

        h_tool = QHBoxLayout()
        btn_nuovo = QPushButton("➕ Nuovo Movimento")
        btn_modifica = QPushButton("✏️ Modifica")
        btn_elimina = QPushButton("🗑️ Elimina")

        btn_nuovo.setProperty('class', 'success')
        btn_modifica.setProperty('class', 'warning')
        btn_elimina.setProperty('class', 'danger')

        btn_nuovo.clicked.connect(self._nuovo_movimento)
        btn_modifica.clicked.connect(self._modifica_movimento)
        btn_elimina.clicked.connect(self._elimina_movimento)
        h_tool.addWidget(btn_nuovo); h_tool.addWidget(btn_modifica); h_tool.addWidget(btn_elimina); h_tool.addStretch()
        layout.addLayout(h_tool)

        self.vista = QTableView()
        self.vista.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.vista.setItemDelegate(DelegateMovimenti(self))

        header = self.vista.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)

        layout.addWidget(self.vista)
        self.aggiorna_dati()

    def aggiorna_dati(self):
        # Clausola per filtrare per magazzino (se passato dal pannello padre)
        clausola_az = ""
        if self.azienda_ids:
            ids_str = ",".join(str(int(i)) for i in self.azienda_ids)
            clausola_az = f"AND rm.azienda_id IN ({ids_str})"

        with self.engine.connect() as conn:
            giacenza = conn.execute(text(f"""
                SELECT SUM(CASE WHEN rm.tipo_movimento = 'CARICO' THEN rm.quantita ELSE -rm.quantita END)
                FROM registro_magazzino rm
                WHERE rm.prodotto_id = :pid {clausola_az}
            """), {"pid": self.prodotto_id}).scalar() or 0.0

        label_giacenza = f"Giacenza ({self.azienda_filter})" if self.azienda_filter else "Giacenza Attuale"
        self.lbl_giacenza.setText(
            f"{label_giacenza}: <span style='color:{'green' if giacenza>=0 else 'red'}'>"
            f"{giacenza:.4f} {self.um_pulita}</span>"
        )

        # La colonna "Azienda" mostra l'azienda DEL TENDONE originale (pre-alias):
        # COALESCE(az_orig, az_warehouse, 'Generale').
        # Esempio: per Deflorio Ciccopinto via Messina Alfio, mostra "Deflorio Ciccopinto"
        # (più informativo che "Messina Alfio" che è il solo magazzino).
        # Per i CARICHI manuali (azienda_id_origine NULL) fallback su azienda_id.
        query_sql = f"""
            SELECT rm.id, rm.data_movimento AS "Data", rm.tipo_movimento AS "Tipo",
                   COALESCE(az_orig.nome, az_wh.nome, 'Generale') AS "Azienda",
                   rm.quantita AS "Quantità ({self.um_pulita})",
                   rm.n_ddt AS "N. DDT", rm.fornitore AS "Fornitore",
                   rm.note AS "Note", rm.azienda_id
            FROM registro_magazzino rm
            LEFT JOIN aziende az_wh ON az_wh.id = rm.azienda_id
            LEFT JOIN aziende az_orig ON az_orig.id = rm.azienda_id_origine
            WHERE rm.prodotto_id = {self.prodotto_id} {clausola_az}
            ORDER BY rm.data_movimento DESC, rm.id DESC
        """

        # ---> INIZIO SOSTITUZIONE CON QStandardItemModel <---
        self.modello_registro = QStandardItemModel()
        from PyQt6.QtGui import QStandardItem

        with self.engine.connect() as conn:
            result = conn.execute(text(query_sql))
            col_names = list(result.keys())
            self.modello_registro.setHorizontalHeaderLabels(col_names)

            for row in result:
                items = []
                row_dict = dict(row._mapping)
                for val in row:
                    item = QStandardItem(str(val) if val is not None else "")
                    item.setEditable(False)
                    items.append(item)
                items[0].setData(row_dict, Qt.ItemDataRole.UserRole)
                self.modello_registro.appendRow(items)

        self.vista.setModel(self.modello_registro)
        self.vista.setColumnHidden(0, True)
        self.vista.setColumnHidden(8, True)
        self.vista.resizeColumnsToContents()

    # Per far funzionare il "finto record" anche nei due metodi sottostanti
    def _recupera_finto_record(self, idx):
        item = self.modello_registro.item(idx.row(), 0)
        dati = item.data(Qt.ItemDataRole.UserRole)
        class DummyRecord:
            def __init__(self, d): self.d = d
            def value(self, k): return self.d.get(k)
        return DummyRecord(dati)

    def _nuovo_movimento(self):
        # Passiamo l'unità pulita al form del nuovo movimento (così apparirà pulita anche di fianco al campo numerico)
        if DialogNuovoMovimento(self.engine, self.prodotto_id, self.um_pulita, self).exec(): self.aggiorna_dati()

    def _modifica_movimento(self):
        idx = self.vista.currentIndex()
        if not idx.isValid(): return
        r = self._recupera_finto_record(idx) # <--- MODIFICATO QUI

        if r.value("Tipo") == "SCARICO":
            QMessageBox.warning(self, "Bloccato", "Gli scarichi non possono essere modificati qui.")
            return

        dati = {"id": r.value("id"), "data": r.value("Data"), "tipo": r.value("Tipo"), "qta": float(r.value(f"Quantità ({self.um_pulita})")), "ddt": r.value("N. DDT"), "fornitore": r.value("Fornitore"), "note": r.value("Note"), "azienda_id": r.value("azienda_id")}
        if DialogModificaMovimento(self.engine, dati, self.um_pulita, self).exec(): self.aggiorna_dati()

    def _elimina_movimento(self):
        idx = self.vista.currentIndex()
        if not idx.isValid(): return
        r = self._recupera_finto_record(idx) # <--- MODIFICATO QUI

        if QMessageBox.question(self, "Conferma", "Vuoi eliminare?") == QMessageBox.StandardButton.Yes:
            with self.engine.begin() as conn: conn.execute(text("DELETE FROM registro_magazzino WHERE id=:id"), {"id": r.value("id")})
            self.aggiorna_dati()


class PannelloProdotti(PannelloBaseDialog):
    COLONNE_NASCOSTE = [0]

    def __init__(self, engine, db, azienda_filter: str | None = None, api=None):
        """
        Args:
            azienda_filter: se valorizzato (es. "Agrimessina"), il pannello
                mostra solo i movimenti/giacenze del magazzino di quell'azienda
                + degli alias che mappano su di essa (vedi WAREHOUSE_ALIASES).
                Se None: aggregato totale come comportamento legacy.
            api: ApiClient. Se valorizzato, il pulsante "Ricalcola scarichi"
                triggera il rebuild server-side via POST /magazzino/ricalcola
                seguito da un sync_all. Senza api fa un rebuild solo locale
                (legacy, divergente dal server: da evitare).
        """
        super().__init__()
        self.engine, self.db = engine, db
        self.api = api
        self.azienda_filter = azienda_filter
        self.azienda_ids = self._resolve_filter_ids()  # lista per IN clause SQL

        # Lo scarico magazzino è server-authoritative:
        #   - INSERT/UPDATE/DELETE di un trattamento → backend chiama
        #     magazzino_calculator.sincronizza_scarico (vedi app/routers/trattamenti_router.py)
        #   - Il desktop NON ricalcola più localmente: si limita a sync da server.
        # I vecchi pulsanti "Push Consumi" e "Annulla Push" sono stati rimossi.
        self.btn_export_tutti_mov = QPushButton("📊 Esporta Movimenti")
        self.btn_export_tutti_mov.setProperty('class', 'success')
        self.btn_export_tutti_mov.clicked.connect(self._esporta_tutti_movimenti)

        # Rebuild manuale degli scarichi automatici (per ripulire inconsistenze):
        # delega al server tramite POST /magazzino/ricalcola, poi pulla.
        self.btn_ricalcola = QPushButton("🔧 Ricalcola scarichi")
        self.btn_ricalcola.setProperty('class', 'warning')
        self.btn_ricalcola.clicked.connect(self._ricalcola_scarichi)

        top_layout = self.layout().itemAt(0).layout()
        top_layout.insertWidget(4, self.btn_export_tutti_mov)
        top_layout.insertWidget(5, self.btn_ricalcola)

        self.vista.doubleClicked.connect(self._on_doppio_click)
        self.aggiorna_dati()

    def _resolve_filter_ids(self) -> list[int]:
        """Risolve azienda_filter (nome) in lista di azienda_id da considerare.

        Include anche gli alias che mappano sull'azienda target (es. quando il
        filtro è 'Messina Alfio', include anche 'Deflorio Ciccopinto').
        Ritorna [] se filtro vuoto o azienda inesistente nel DB.
        """
        if not self.azienda_filter:
            return []
        from config import WAREHOUSE_ALIASES
        names = [self.azienda_filter]
        for alias, canonical in WAREHOUSE_ALIASES.items():
            if canonical.lower() == self.azienda_filter.lower():
                names.append(alias)
        ids = []
        with self.engine.connect() as conn:
            for n in names:
                row = conn.execute(
                    text("SELECT id FROM aziende WHERE LOWER(TRIM(nome)) = LOWER(TRIM(:n))"),
                    {"n": n}
                ).first()
                if row:
                    ids.append(row[0])
        return ids

    def _where_azienda_clause(self, table_alias: str) -> str:
        """Costruisce il pezzo SQL 'AND <alias>.azienda_id IN (...)'.
        Stringa vuota se nessun filtro. Usa int() coercion per safety."""
        if not self.azienda_ids:
            return ""
        ids_str = ",".join(str(int(i)) for i in self.azienda_ids)
        return f"AND {table_alias}.azienda_id IN ({ids_str})"

    def aggiorna_dati(self):
        # La colonna "Giacenza Totale" mostra la giacenza filtrata per i
        # magazzini di questa veduta. Se nessun filtro è applicato, mostra
        # la giacenza globale (comportamento legacy).
        giacenza_label = (
            f"Giacenza ({self.azienda_filter})" if self.azienda_filter else "Giacenza Totale"
        )
        clausola_az = self._where_azienda_clause("rm")  # es. "AND rm.azienda_id IN (1,5)"
        query = f"""
            SELECT p.id, p.nome_prodotto AS "Nome Prodotto",
                   ROUND(COALESCE((
                       SELECT SUM(CASE WHEN rm.tipo_movimento = 'CARICO' THEN rm.quantita ELSE -rm.quantita END)
                       FROM registro_magazzino rm
                       WHERE rm.prodotto_id = p.id {clausola_az}
                   ), 0), 4) AS "{giacenza_label}",
                   p.categoria AS "Categoria", p.unita_misura AS "Unità", p.numero_registrazione AS "N. Registrazione",
                   p.sostanza_attiva AS "Sostanza Attiva", p.bio_convenzionale AS "Bio/Conv", p.blacklist AS "Blacklist",
                   p.avversita AS "Avversità", p.titolo_n, p.titolo_p, p.titolo_k, p.phi_giorni, p.trattamenti_max, p.intervallo_min_tratt, p.min_sostanza, p.max_sostanza, p.qta_acqua
            FROM prodotti p ORDER BY p.nome_prodotto
        """
        self.esegui_query(query, self.engine)
        self._nascondi_colonne()
        self.vista.resizeColumnsToContents()

    def _ricalcola_scarichi(self):
        """Rebuild manuale di tutti gli scarichi automatici. Utile dopo bug o
        importazioni incoerenti. I CARICHI manuali non vengono toccati.

        Delega al server tramite POST /magazzino/ricalcola: cancella tutti gli
        scarichi server-side con trattamento_id valorizzato e li ricrea. Subito
        dopo pulla con sync_all per allineare il locale, altrimenti gli
        scarichi appena ricostruiti restano invisibili (e al prossimo sync
        automatico verrebbero comunque sincronizzati ma con ID nuovi, lasciando
        quelli vecchi come orfani nel locale).
        """
        if self.api is None:
            QMessageBox.warning(
                self, "Non disponibile",
                "Il pulsante richiede una sessione attiva con il server. "
                "Riavvia l'app e riprova.",
            )
            return
        if QMessageBox.question(
            self, "Ricalcola scarichi",
            "Vuoi davvero ricalcolare TUTTI gli scarichi automatici sul server?\n\n"
            "Il backend cancellerà ogni scarico con trattamento_id valorizzato "
            "e lo ricostruirà da zero in base ai trattamenti correnti. Subito "
            "dopo verrà fatto un sync per allineare il locale.\n\n"
            "I CARICHI manuali NON verranno toccati.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            # 1. Server-side rebuild. Crea anche i tombstone per gli ID
            # cancellati (vedi app/magazzino_calculator.sincronizza_scarico_globale).
            esito = self.api.ricalcola_scarichi()
            n_del = esito.get("eliminati", 0)
            n_ric = esito.get("ricalcolati", 0)

            # 2. Reconcile: pulla l'elenco completo dal server (since=None) e
            # fa phantom-delete dei record locali non presenti server-side.
            # Più robusto di sync_all per questo caso, perché copre anche lo
            # scenario in cui i tombstone non siano arrivati (es. backend vecchio).
            from sync import reconcile_with_server
            reconcile_with_server(self.api, self.engine)

            self.aggiorna_dati()
            QMessageBox.information(
                self, "Ricalcolo completato",
                f"Server: eliminati {n_del} scarichi automatici, "
                f"ricalcolati {n_ric} trattamenti.\n"
                f"Locale ri-sincronizzato.",
            )
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Ricalcolo fallito:\n{e}")

    def _esporta_tutti_movimenti(self):
        # Nome file di default include il magazzino, se filtrato
        default_name = (
            f"Movimenti_{self.azienda_filter.replace(' ', '_')}.xlsx"
            if self.azienda_filter else "Movimenti_Magazzino.xlsx"
        )
        f_p, _ = QFileDialog.getSaveFileName(self, "Esporta Movimenti", default_name, "Excel (*.xlsx)")
        if not f_p:
            return
        if not f_p.lower().endswith('.xlsx'):
            f_p += '.xlsx'

        try:
            import pandas as pd
            # Filtra per i magazzini di questa veduta
            clausola_az_where = ""
            if self.azienda_ids:
                ids_str = ",".join(str(int(i)) for i in self.azienda_ids)
                clausola_az_where = f"WHERE rm.azienda_id IN ({ids_str})"
            with self.engine.connect() as conn:
                query = text(f"""
                    SELECT
                        rm.id AS _id,
                        COALESCE(az_orig.nome, az_wh.nome, 'Generale') AS "Azienda",
                        rm.azienda_id AS _azienda_id,
                        rm.prodotto_id AS _prodotto_id,
                        p.nome_prodotto AS "Prodotto",
                        p.numero_registrazione AS "N. Registrazione",
                        p.sostanza_attiva AS "Sostanza Attiva",
                        rm.n_ddt AS "N. DDT",
                        rm.fornitore AS "Fornitore",
                        rm.data_movimento AS "Data",
                        rm.tipo_movimento AS "Tipo",
                        rm.quantita AS _qta_raw,
                        p.unita_misura AS _um
                    FROM registro_magazzino rm
                    JOIN prodotti p ON p.id = rm.prodotto_id
                    LEFT JOIN aziende az_wh ON az_wh.id = rm.azienda_id
                    LEFT JOIN aziende az_orig ON az_orig.id = rm.azienda_id_origine
                    {clausola_az_where}
                    ORDER BY rm.data_movimento ASC, rm.id ASC
                """)
                df = pd.read_sql(query, conn)

            if df.empty:
                QMessageBox.information(self, "Nessun dato", "Nessun movimento da esportare.")
                return

            def normalizza_tipo(t):
                t_upper = str(t).strip().upper() if t else ""
                if "SCARICO" in t_upper or "USCITA" in t_upper:
                    return "Scarico"
                return "Carico"

            df["Carico/Scarico"] = df["Tipo"].apply(normalizza_tipo)
            df["Quantità"] = df.apply(
                lambda r: -abs(r["_qta_raw"]) if r["Carico/Scarico"] == "Scarico" else abs(r["_qta_raw"]),
                axis=1
            )

            df["Giacenza"] = (
                df.groupby(["_azienda_id", "_prodotto_id"])["Quantità"]
                  .cumsum()
                  .round(4)
            )

            df["Unità"] = df["_um"]

            colonne_finali = [
                "Prodotto", "N. Registrazione", "Sostanza Attiva",
                "N. DDT", "Fornitore", "Data", "Carico/Scarico",
                "Quantità", "Unità", "Giacenza", "Azienda"
            ]
            df = df[colonne_finali]

            df = df.sort_values(
                by=["Azienda", "Data", "Prodotto"],
                ascending=[True, False, True]
            ).reset_index(drop=True)

            with pd.ExcelWriter(f_p, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name="Movimenti Magazzino", index=False)

                ws = writer.sheets["Movimenti Magazzino"]
                from openpyxl.styles import Font, PatternFill, Alignment

                header_font = Font(bold=True, color="FFFFFF")
                header_fill = PatternFill(start_color="2E7D32", end_color="2E7D32", fill_type="solid")
                header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

                for cell in ws[1]:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = header_align

                fill_carico = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
                fill_scarico = PatternFill(start_color="FFEBEE", end_color="FFEBEE", fill_type="solid")

                col_tipo_idx = colonne_finali.index("Carico/Scarico") + 1
                for row_idx in range(2, ws.max_row + 1):
                    tipo_val = ws.cell(row=row_idx, column=col_tipo_idx).value
                    fill_riga = fill_scarico if tipo_val == "Scarico" else fill_carico
                    for col_idx in range(1, len(colonne_finali) + 1):
                        ws.cell(row=row_idx, column=col_idx).fill = fill_riga

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

    def _on_doppio_click(self, index):
        # --- FIX: Recupero dati compatibile con QStandardItemModel ---
        item_col0 = self.modello.item(index.row(), 0)
        dati = item_col0.data(Qt.ItemDataRole.UserRole)

        if not dati:
            return

        # Creiamo un "finto record" per non dover riscrivere il resto della funzione
        class DummyRecord:
            def __init__(self, d): self.d = d
            def value(self, k): return self.d.get(k)

        r = DummyRecord(dati)
        # -------------------------------------------------------------

        DialogRegistroProdotto(
            self.engine, self.db, r.value("id"),
            r.value("Nome Prodotto"), r.value("Unità"), self,
            azienda_filter=self.azienda_filter,
            azienda_ids=self.azienda_ids,
        ).exec()
        self.aggiorna_dati()

    def apri_dialog_nuovo(self):
        if DialogProdotto(self.engine, parent=self).exec(): self.aggiorna_dati()

    def apri_dialog_modifica(self, riga):
        dati = {
            "id": riga.value("id"), "nome_prodotto": riga.value("Nome Prodotto"), "categoria": riga.value("Categoria"),
            "numero_registrazione": riga.value("N. Registrazione"), "sostanza_attiva": riga.value("Sostanza Attiva"),
            "bio_convenzionale": riga.value("Bio/Conv"), "blacklist": riga.value("Blacklist"), "avversita": riga.value("Avversità"),
            "unita_misura": riga.value("Unità"), "titolo_n": riga.value("titolo_n"), "titolo_p": riga.value("titolo_p"), "titolo_k": riga.value("titolo_k"),
            "phi_giorni": riga.value("phi_giorni"), "trattamenti_max": riga.value("trattamenti_max"), "intervallo_min_tratt": riga.value("intervallo_min_tratt"),
            "min_sostanza": riga.value("min_sostanza"), "max_sostanza": riga.value("max_sostanza"), "qta_acqua": riga.value("qta_acqua")
        }
        if DialogProdotto(self.engine, dati=dati, parent=self).exec(): self.aggiorna_dati()

    def elimina_record(self, riga):
        if QMessageBox.question(self, "Elimina", f"Eliminare {riga.value('Nome Prodotto')}?") == QMessageBox.StandardButton.Yes:
            with self.engine.begin() as conn: conn.execute(text("DELETE FROM prodotti WHERE id=:id"), {"id": riga.value("id")})
            self.aggiorna_dati()
