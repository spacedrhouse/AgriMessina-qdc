from sqlalchemy import text
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox,
                             QDialog, QComboBox, QLabel, QLineEdit, QDoubleSpinBox,
                             QFormLayout, QTableView, QHeaderView)
from PyQt6.QtGui import QStandardItemModel, QStandardItem
from PyQt6.QtCore import Qt

# Importiamo le fondamenta dal nostro core
from ui_core import PannelloBaseDialog, DelegateTendoniColorati

class DialogAzienda(QDialog):
    def __init__(self, engine, azienda_id=None, nome_attuale="", parent=None):
        super().__init__(parent)
        self.engine = engine
        self.azienda_id = azienda_id
        self.setWindowTitle("Modifica Azienda" if azienda_id else "Nuova Azienda")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # CRITICO: Assegna il widget a self.input_nome
        self.input_nome = QLineEdit(nome_attuale)
        form.addRow("Nome Azienda:", self.input_nome)

        layout.addLayout(form)

        btns = QHBoxLayout()
        btn_salva = QPushButton("💾 Salva")
        btn_salva.clicked.connect(self.salva)
        btns.addWidget(btn_salva)
        layout.addLayout(btns)

    def salva(self):
        # Ora self.input_nome esiste e può essere letto
        nome = self.input_nome.text().strip()
        if not nome:
            QMessageBox.warning(self, "Errore", "Il nome è obbligatorio.")
            return

        try:
            # I trigger SQL su aziende accodano automaticamente l'operazione in
            # pending_operations (vedi local_db._install_triggers). NON aggiungere
            # un enqueue_operation manuale qui: produrrebbe una pending op
            # duplicata e quindi un INSERT/UPDATE duplicato sul server.
            with self.engine.begin() as conn:
                if self.azienda_id:
                    conn.execute(text("UPDATE aziende SET nome = :n WHERE id = :id"),
                                 {"n": nome, "id": self.azienda_id})
                else:
                    conn.execute(text("INSERT INTO aziende (nome) VALUES (:n)"),
                                 {"n": nome})
            self.accept()

        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                QMessageBox.warning(self, "Duplicato", f"L'azienda '{nome}' esiste già.")
            else:
                QMessageBox.critical(self, "Errore Database", str(e))

class DialogGiacenzaAzienda(QDialog):
    def __init__(self, engine, azienda_id, nome_azienda, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.azienda_id = azienda_id
        self.setWindowTitle(f"Giacenza Prodotti — {nome_azienda}")
        self.setMinimumSize(600, 400)

        layout = QVBoxLayout(self)
        self.vista = QTableView()
        self.vista.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.vista.setAlternatingRowColors(True)
        layout.addWidget(self.vista)

        self.modello = QStandardItemModel()
        self.vista.setModel(self.modello)

        header = self.vista.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self._carica_dati()

    def _carica_dati(self):
        query = text("""
            SELECT p.nome_prodotto AS "Prodotto",
                   ROUND(SUM(CASE WHEN rm.tipo_movimento = 'CARICO' THEN rm.quantita ELSE -rm.quantita END), 4) AS "Giacenza",
                   p.unita_misura AS "Unità"
            FROM prodotti p
            JOIN registro_magazzino rm ON p.id = rm.prodotto_id
            WHERE rm.azienda_id = :az_id
            GROUP BY p.id
            HAVING Giacenza != 0
            ORDER BY p.nome_prodotto
        """)

        self.modello.clear()
        self.modello.setHorizontalHeaderLabels(["Prodotto", "Giacenza", "Unità"])

        with self.engine.connect() as conn:
            result = conn.execute(query, {"az_id": self.azienda_id})
            for row in result:
                items = [QStandardItem(str(val)) for val in row]
                # Allineamento numerico per la giacenza
                items[1].setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.modello.appendRow(items)

class PannelloAziende(PannelloBaseDialog):
    COLONNE_NASCOSTE = [0]
    def __init__(self, engine, db):
        super().__init__()
        self.engine, self.db = engine, db
        # Collega il segnale di doppio click
        self.vista.doubleClicked.connect(self._on_doppio_click)
        self.aggiorna_dati()

    def _on_doppio_click(self, index):
        # Recupera i dati della riga tramite l'item colonna 0 (dove abbiamo salvato il dizionario)
        item = self.modello.item(index.row(), 0)
        dati = item.data(Qt.ItemDataRole.UserRole)

        if dati:
            # Apri il nuovo dialog di giacenza
            dialog = DialogGiacenzaAzienda(self.engine, dati["id"], dati["nome"], self)
            dialog.exec()

    def aggiorna_dati(self):
        # Utilizziamo esegui_query del PannelloBaseDialog per gestire il modello
        self.esegui_query("SELECT id, nome FROM aziende ORDER BY nome", self.engine)
        self._nascondi_colonne()
        self.vista.resizeColumnsToContents()

    def apri_dialog_nuovo(self):
        if DialogAzienda(self.engine, parent=self).exec(): self.aggiorna_dati()

    def apri_dialog_modifica(self, riga):
        if DialogAzienda(self.engine, dati={"id": riga.value("id"), "nome": riga.value("nome")}, parent=self).exec(): self.aggiorna_dati()

    def elimina_record(self, riga):
        if QMessageBox.question(self, "Conferma", f"Eliminare l'azienda «{riga.value('nome')}» e tutto ciò che le è collegato?") == QMessageBox.StandardButton.Yes:
            with self.engine.begin() as conn: conn.execute(text("DELETE FROM aziende WHERE id=:id"), {"id": riga.value("id")})
            self.aggiorna_dati()

class DialogAgro(QDialog):
    def __init__(self, engine, agro_id=None, nome_attuale="", az_id_attuale=None, parent=None):
        super().__init__(parent)
        self.engine, self.agri_id = engine, agro_id # Usiamo agri_id internamente per coerenza col DB
        self.setWindowTitle("Modifica Agro" if agro_id else "Nuovo Agro")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Definiamo i widget e li assegniamo a self
        self.input_nome = QLineEdit(nome_attuale)
        self.combo_az = QComboBox()

        # Popolamento combo aziende
        with self.engine.connect() as conn:
            for az in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall():
                self.combo_az.addItem(az[1], userData=az[0])
                if az[0] == az_id_attuale:
                    self.combo_az.setCurrentIndex(self.combo_az.count() - 1)

        form.addRow("Nome Agro:", self.input_nome)
        form.addRow("Azienda:", self.combo_az)
        layout.addLayout(form)

        btn_salva = QPushButton("💾 Salva")
        btn_salva.clicked.connect(self.salva)
        layout.addWidget(btn_salva)

    def _carica_aziende(self):
        with self.engine.connect() as conn:
            for r in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall():
                self.combo_azienda.addItem(r[1], userData=r[0])

    def salva(self):
        nome = self.input_nome.text().strip()
        az_id = self.combo_az.currentData()

        if not nome or not az_id:
            QMessageBox.warning(self, "Campi Mancanti", "Compila tutti i campi obbligatori.")
            return

        try:
            # I trigger SQL su agri si occupano dell'enqueue (vedi
            # local_db._install_triggers). Non aggiungere un enqueue manuale:
            # produrrebbe una pending op duplicata. Inoltre la chiamata
            # precedente usava "AGRI" come entity_type, che non corrispondeva
            # a nessuna entry di ENTITY_TO_TABLE: la pending op duplicata
            # restava in coda finché non veniva scartata.
            with self.engine.begin() as conn:
                if self.agri_id:
                    conn.execute(text("UPDATE agri SET nome=:n, azienda_id=:az WHERE id=:id"),
                                 {"n": nome, "az": az_id, "id": self.agri_id})
                else:
                    conn.execute(text("INSERT INTO agri (nome, azienda_id) VALUES (:n, :az)"),
                                 {"n": nome, "az": az_id})

            self.accept()

        except Exception as e:
            # Gestione errore duplicato specifica per SQLite
            if "UNIQUE constraint failed" in str(e):
                QMessageBox.warning(self, "Duplicato",
                                    f"L'agro '{nome}' esiste già per l'azienda selezionata.")
            else:
                QMessageBox.critical(self, "Errore Database", f"Impossibile salvare: {str(e)}")

class PannelloAgri(PannelloBaseDialog):
    COLONNE_NASCOSTE = [0, 1]
    def __init__(self, engine, db):
        super().__init__()
        self.engine, self.db = engine, db
        self.aggiorna_dati()

    def aggiorna_dati(self):
        self.esegui_query('SELECT ag.id, ag.azienda_id, az.nome AS "Azienda", ag.nome AS "Agro" FROM agri ag JOIN aziende az ON az.id = ag.azienda_id ORDER BY az.nome, ag.nome', self.engine)
        self._nascondi_colonne()
        self.vista.resizeColumnsToContents()

    def apri_dialog_nuovo(self):
        if DialogAgro(self.engine, parent=self).exec(): self.aggiorna_dati()

    def apri_dialog_modifica(self, riga):
        if DialogAgro(self.engine, dati={"id": riga.value("id"), "nome": riga.value("Agro"), "azienda_id": riga.value("azienda_id")}, parent=self).exec(): self.aggiorna_dati()

    def elimina_record(self, riga):
        if QMessageBox.question(self, "Conferma", f"Eliminare l'agro «{riga.value('Agro')}»?") == QMessageBox.StandardButton.Yes:
            with self.engine.begin() as conn: conn.execute(text("DELETE FROM agri WHERE id=:id"), {"id": riga.value("id")})
            self.aggiorna_dati()

class DialogContrada(QDialog):
    def __init__(self, engine, contrada_id=None, nome_attuale="", agri_id_attuale=None, parent=None):
        super().__init__(parent)
        self.engine, self.contrada_id = engine, contrada_id
        self.setWindowTitle("Modifica Contrada" if contrada_id else "Nuova Contrada")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.input_nome = QLineEdit(nome_attuale) # Assegnazione a self
        self.combo_agri = QComboBox() # Assegnazione a self

        with self.engine.connect() as conn:
            for ag in conn.execute(text("SELECT id, nome FROM agri ORDER BY nome")).fetchall():
                self.combo_agri.addItem(ag[1], userData=ag[0])
                if ag[0] == agri_id_attuale:
                    self.combo_agri.setCurrentIndex(self.combo_agri.count() - 1)

        form.addRow("Nome Contrada:", self.input_nome)
        form.addRow("Agro:", self.combo_agri)
        layout.addLayout(form)

        btn_salva = QPushButton("💾 Salva")
        btn_salva.clicked.connect(self.salva)
        layout.addWidget(btn_salva)

    def _carica_aziende(self):
        self.combo_azienda.blockSignals(True)
        with self.engine.connect() as conn:
            for r in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall(): self.combo_azienda.addItem(r[1], userData=r[0])
        self.combo_azienda.blockSignals(False)
        self._carica_agri()

    def _carica_agri(self):
        self.combo_agro.clear()
        id_az = self.combo_azienda.currentData()
        if id_az:
            with self.engine.connect() as conn:
                for r in conn.execute(text("SELECT id, nome FROM agri WHERE azienda_id=:id ORDER BY nome"), {"id": id_az}).fetchall(): self.combo_agro.addItem(r[1], userData=r[0])

    def _preseleziona(self, agro_id):
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT id, azienda_id FROM agri WHERE id=:id"), {"id": agro_id}).fetchone()
        if not row: return
        for i in range(self.combo_azienda.count()):
            if self.combo_azienda.itemData(i) == row[1]: self.combo_azienda.setCurrentIndex(i); break
        self._carica_agri()
        for i in range(self.combo_agro.count()):
            if self.combo_agro.itemData(i) == agro_id: self.combo_agro.setCurrentIndex(i); break

    def salva(self):
        nome = self.input_nome.text().strip()
        agri_id = self.combo_agri.currentData()
        if not nome or not agri_id: return

        try:
            # Trigger su contrade già accoda — niente enqueue manuale qui.
            with self.engine.begin() as conn:
                if self.contrada_id:
                    conn.execute(text("UPDATE contrade SET nome=:n, agro_id=:ag WHERE id=:id"),
                                 {"n": nome, "ag": agri_id, "id": self.contrada_id})
                else:
                    conn.execute(text("INSERT INTO contrade (nome, agro_id) VALUES (:n, :ag)"),
                                 {"n": nome, "ag": agri_id})
            self.accept()
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                QMessageBox.warning(self, "Duplicato", f"La contrada '{nome}' esiste già in questo agro.")
            else:
                QMessageBox.critical(self, "Errore", str(e))

class PannelloContrade(PannelloBaseDialog):
    COLONNE_NASCOSTE = [0, 1]
    def __init__(self, engine, db):
        super().__init__()
        self.engine, self.db = engine, db
        self.aggiorna_dati()

    def aggiorna_dati(self):
        self.esegui_query('SELECT c.id, c.agro_id, az.nome AS "Azienda", ag.nome AS "Agro", c.nome AS "Contrada" FROM contrade c JOIN agri ag ON ag.id = c.agro_id JOIN aziende az ON az.id = ag.azienda_id ORDER BY az.nome, ag.nome, c.nome', self.engine)
        self._nascondi_colonne()
        self.vista.resizeColumnsToContents()

    def apri_dialog_nuovo(self):
        if DialogContrada(self.engine, parent=self).exec(): self.aggiorna_dati()

    def apri_dialog_modifica(self, riga):
        if DialogContrada(self.engine, dati={"id": riga.value("id"), "nome": riga.value("Contrada"), "agro_id": riga.value("agro_id")}, parent=self).exec(): self.aggiorna_dati()

    def elimina_record(self, riga):
        if QMessageBox.question(self, "Conferma", f"Eliminare la contrada «{riga.value('Contrada')}»?") == QMessageBox.StandardButton.Yes:
            with self.engine.begin() as conn: conn.execute(text("DELETE FROM contrade WHERE id=:id"), {"id": riga.value("id")})
            self.aggiorna_dati()

class DialogTendone(QDialog):
    def __init__(self, engine, tendone_id=None, dati=None, parent=None):
        super().__init__(parent)
        self.engine, self.tendone_id = engine, tendone_id
        self.setWindowTitle("Modifica Tendone" if tendone_id else "Nuovo Tendone")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Inizializzazione widget con assegnazione a self
        self.input_codice = QLineEdit(dati.get('codice', '') if dati else "")
        self.spin_ettari = QDoubleSpinBox()
        self.spin_ettari.setRange(0.0001, 99.9999)
        self.spin_ettari.setDecimals(4)
        if dati: self.spin_ettari.setValue(dati.get('ettari', 0.0))

        self.combo_contrada = QComboBox()
        with self.engine.connect() as conn:
            for c in conn.execute(text("SELECT id, nome FROM contrade ORDER BY nome")).fetchall():
                self.combo_contrada.addItem(c[1], userData=c[0])
                if dati and c[0] == dati.get('contrada_id'):
                    self.combo_contrada.setCurrentIndex(self.combo_contrada.count() - 1)

        form.addRow("Codice Tendone:", self.input_codice)
        form.addRow("Superficie (ha):", self.spin_ettari)
        form.addRow("Contrada:", self.combo_contrada)
        layout.addLayout(form)

        btn_salva = QPushButton("💾 Salva")
        btn_salva.clicked.connect(self.salva)
        layout.addWidget(btn_salva)

    def _carica_aziende(self):
        self.combo_azienda.blockSignals(True)
        with self.engine.connect() as conn:
            for r in conn.execute(text("SELECT id, nome FROM aziende ORDER BY nome")).fetchall(): self.combo_azienda.addItem(r[1], userData=r[0])
        self.combo_azienda.blockSignals(False)
        self._carica_agri()

    def _carica_agri(self):
        self.combo_agro.blockSignals(True); self.combo_agro.clear()
        id_az = self.combo_azienda.currentData()
        if id_az:
            with self.engine.connect() as conn:
                for r in conn.execute(text("SELECT id, nome FROM agri WHERE azienda_id=:id ORDER BY nome"), {"id": id_az}).fetchall(): self.combo_agro.addItem(r[1], userData=r[0])
        self.combo_agro.blockSignals(False)
        self._carica_contrade()

    def _carica_contrade(self):
        self.combo_contrada.clear()
        id_ag = self.combo_agro.currentData()
        if id_ag:
            with self.engine.connect() as conn:
                for r in conn.execute(text("SELECT id, nome FROM contrade WHERE agro_id=:id ORDER BY nome"), {"id": id_ag}).fetchall(): self.combo_contrada.addItem(r[1], userData=r[0])

    def _preseleziona(self, contrada_id):
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT c.agro_id, ag.azienda_id FROM contrade c JOIN agri ag ON ag.id=c.agro_id WHERE c.id=:id"), {"id": contrada_id}).fetchone()
        if not row: return
        for i in range(self.combo_azienda.count()):
            if self.combo_azienda.itemData(i) == row[1]: self.combo_azienda.setCurrentIndex(i); break
        self._carica_agri()
        for i in range(self.combo_agro.count()):
            if self.combo_agro.itemData(i) == row[0]: self.combo_agro.setCurrentIndex(i); break
        self._carica_contrade()
        for i in range(self.combo_contrada.count()):
            if self.combo_contrada.itemData(i) == contrada_id: self.combo_contrada.setCurrentIndex(i); break

    def salva(self):
        codice = self.input_codice.text().strip().upper()
        ettari = self.spin_ettari.value()
        contrada_id = self.combo_contrada.currentData()
        if not codice or not contrada_id: return

        try:
            # Trigger su tendoni già accoda — niente enqueue manuale qui.
            with self.engine.begin() as conn:
                payload = {"codice": codice, "ettari": ettari, "contrada_id": contrada_id}
                if self.tendone_id:
                    conn.execute(text("UPDATE tendoni SET codice=:codice, ettari=:ettari, contrada_id=:contrada_id WHERE id=:id"),
                                 {**payload, "id": self.tendone_id})
                else:
                    conn.execute(text("INSERT INTO tendoni (codice, ettari, contrada_id) VALUES (:codice, :ettari, :contrada_id)"),
                                 payload)
            self.accept()
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                QMessageBox.warning(self, "Duplicato", f"Il codice tendone '{codice}' è già in uso.")
            else:
                QMessageBox.critical(self, "Errore Database", str(e))

class PannelloTendoni(PannelloBaseDialog):
    COLONNE_NASCOSTE = [0, 5, 7]

    def __init__(self, engine, db):
        super().__init__()
        self.engine, self.db = engine, db
        self.modalita_analisi = False # Flag di stato: inizia in modalità "Lista Base"

        # --- 1. SETUP BARRA FILTRO (INIZIALMENTE NASCOSTA) ---
        self.layout_filtro = QHBoxLayout()
        self.label_filtro = QLabel("🔍 Analisi Utilizzo Prodotto:")
        self.combo_filtro = QComboBox()
        self.combo_filtro.addItem("Nessuna Selezione", userData=None)

        self.layout_filtro.addWidget(self.label_filtro)
        self.layout_filtro.addWidget(self.combo_filtro)
        self.layout().insertLayout(0, self.layout_filtro)

        # NASCONDIAMO il filtro all'avvio
        self.label_filtro.hide()
        self.combo_filtro.hide()

        with self.engine.connect() as conn:
            for p in conn.execute(text("SELECT id, nome_prodotto FROM prodotti ORDER BY nome_prodotto")).fetchall():
                self.combo_filtro.addItem(p[1], userData=p[0])

        self.combo_filtro.currentIndexChanged.connect(self.aggiorna_dati)

        # --- 2. SETUP PULSANTE LISTA TRATTAMENTI ---
        self.btn_lista = QPushButton("📋 Lista Trattamenti")

        # Applichiamo direttamente lo stile CSS per garantire che sia colorato (Blu in stile 'Primary')
        self.btn_lista.setStyleSheet("""
            QPushButton {
                background-color: #1976D2; color: white; border: none;
                padding: 6px 12px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background-color: #1565C0; }
        """)

        # Colleghiamo il click alla nuova funzione "interruttore"
        self.btn_lista.clicked.connect(self._toggle_vista)

        # Inseriamo il pulsante nella barra degli strumenti
        try:
            layout_pulsanti = self.layout().itemAt(1).layout()
            layout_pulsanti.insertWidget(3, self.btn_lista)
        except AttributeError:
            pass

        # --- 3. SETUP TABELLA ---
        self.vista.setItemDelegate(DelegateTendoniColorati())
        self.vista.doubleClicked.connect(self._on_doppio_click)

        # All'avvio popola la tabella con i dati base
        self.aggiorna_dati()

    def _toggle_vista(self):
        # Invertiamo lo stato logico (da False a True, o da True a False)
        self.modalita_analisi = not self.modalita_analisi

        if self.modalita_analisi:
            # ---> PASSIAMO ALLA VISTA ANALISI (Tutti i Prodotti)
            self.label_filtro.show()
            self.combo_filtro.show()
            self.btn_lista.setText("🏕️ Lista Tendoni") # Cambia il nome del tasto
        else:
            # ---> TORNIAMO ALLA VISTA STANDARD (Solo anagrafica)
            self.label_filtro.hide()
            self.combo_filtro.hide()
            self.combo_filtro.setCurrentIndex(0) # Resetta la tendina su "Nessuna Selezione"
            self.btn_lista.setText("📋 Lista Trattamenti") # Ripristina il nome del tasto

        # Aggiorniamo la tabella in base al nuovo stato
        self.aggiorna_dati()

    def _on_doppio_click(self, index):
        item = self.modello.item(index.row(), 0)
        dati = item.data(Qt.ItemDataRole.UserRole)
        if not dati:
            return

        class DummyRecord:
            def __init__(self, d): self.d = d
            def value(self, k): return self.d.get(k)

        riga = DummyRecord(dati)

        # Passiamo l'id prodotto solo se siamo in modalità analisi e c'è una selezione
        prod_id = self.combo_filtro.currentData() if self.modalita_analisi else None

        from ui_trattamenti import DialogStoricoProdottiTendone
        DialogStoricoProdottiTendone(self.engine, riga.value("id"), riga.value("Codice"), riga.value("Superficie (ha)"), prod_id, self).exec()

    def aggiorna_dati(self):
        if not self.modalita_analisi:
            # VISTA BASE: Nessuna rimanenza, solo anagrafica tendoni
            query = 'SELECT t.id, az.nome AS "Azienda", ag.nome AS "Agro", c.nome AS "Contrada", t.codice AS "Codice", t.contrada_id, t.ettari AS "Superficie (ha)", 0 AS Stato_Dose FROM tendoni t JOIN contrade c ON c.id = t.contrada_id JOIN agri ag ON ag.id = c.agro_id JOIN aziende az ON az.id = ag.azienda_id ORDER BY az.nome, ag.nome, c.nome, t.codice'
            self.COLONNE_NASCOSTE = [0, 5, 7]
        else:
            # VISTA ANALISI: Mostra i prodotti e le rimanenze
            prod_id = self.combo_filtro.currentData()

            # Se prod_id è None ("Nessuna Selezione"), il filtro_sql è vuoto e calcola per TUTTI i prodotti
            filtro_sql = f"WHERE tr.prodotto_id = {prod_id}" if prod_id else ""

            query = f"""
                SELECT t.id, az.nome AS "Azienda", ag.nome AS "Agro", c.nome AS "Contrada", t.codice AS "Codice", t.contrada_id, t.ettari AS "Superficie (ha)",
                       p.nome_prodotto AS "Prodotto",
                       ROUND(((CASE WHEN p.unita_misura LIKE '%/hl' THEN p.max_sostanza * (sp.q_acq / 100.0) ELSE p.max_sostanza END) * t.ettari) - sp.q_usata, 4) AS "Rimanenza",
                       CASE
                           WHEN sp.q_usata IS NULL THEN 0

                           -- CASO 1: Prodotti in concentrazione (es. g/hl)
                           WHEN p.unita_misura LIKE '%/hl' THEN
                               CASE
                                   -- USIAMO IL VOLUME TEORICO (Ettari * volume per ettaro) IGNORANDO LE BOTTI REALI
                                   WHEN p.max_sostanza > 0 AND (ROUND(sp.q_usata / (t.ettari * (sp.q_acq / 100.0)), 4) > p.max_sostanza) THEN 2
                                   WHEN p.min_sostanza > 0 AND (ROUND(sp.q_usata / (t.ettari * (sp.q_acq / 100.0)), 4) < p.min_sostanza) THEN 3
                                   ELSE 1
                               END

                           -- CASO 2: Prodotti standard (es. L/ha)
                           ELSE
                               CASE
                                   WHEN p.max_sostanza > 0 AND (ROUND(sp.q_usata / t.ettari, 4) > p.max_sostanza) THEN 2
                                   WHEN p.min_sostanza > 0 AND (ROUND(sp.q_usata / t.ettari, 4) < p.min_sostanza) THEN 3
                                   ELSE 1
                               END
                       END AS Stato_Dose
                FROM tendoni t
                JOIN contrade c ON c.id = t.contrada_id
                JOIN agri ag ON ag.id = c.agro_id
                JOIN aziende az ON az.id = ag.azienda_id
                JOIN (
                    SELECT dt.tendone_id, tr.prodotto_id, SUM(dt.quantita_sostanza) AS q_usata, SUM(dt.botti) AS b_tot, MAX(COALESCE(p2.qta_acqua, 1000)) AS q_acq
                    FROM dettaglio_trattamenti dt
                    JOIN trattamenti tr ON tr.id = dt.trattamento_id
                    JOIN prodotti p2 ON p2.id = tr.prodotto_id
                    {filtro_sql}
                    GROUP BY dt.tendone_id, tr.prodotto_id
                ) sp ON sp.tendone_id = t.id
                JOIN prodotti p ON p.id = sp.prodotto_id
                ORDER BY az.nome, ag.nome, c.nome, t.codice, p.nome_prodotto
            """
            # Con l'aggiunta della colonna "Prodotto", Stato_Dose slitta all'indice 9
            self.COLONNE_NASCOSTE = [0, 5, 9]

        self.esegui_query(query, self.engine)
        self._nascondi_colonne()
        self.vista.resizeColumnsToContents()

    def apri_dialog_nuovo(self):
        if DialogTendone(self.engine, parent=self).exec(): self.aggiorna_dati()

    def apri_dialog_modifica(self, riga):
        if DialogTendone(self.engine, dati={"id": riga.value("id"), "codice": riga.value("Codice"), "ettari": riga.value("Superficie (ha)"), "contrada_id": riga.value("contrada_id")}, parent=self).exec(): self.aggiorna_dati()

    def elimina_record(self, riga):
        if QMessageBox.question(self, "Conferma", f"Eliminare il tendone «{riga.value('Codice')}» e tutti i trattamenti ad esso associati?") == QMessageBox.StandardButton.Yes:
            with self.engine.begin() as conn: conn.execute(text("DELETE FROM tendoni WHERE id=:id"), {"id": riga.value("id")})
            self.aggiorna_dati()
