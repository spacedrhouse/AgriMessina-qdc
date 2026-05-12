from datetime import datetime
from sqlalchemy import text
import os

# --- IMPORT COMPLETI E ORDINATI ---
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox, QDialog,
    QComboBox, QLabel, QLineEdit, QDoubleSpinBox, QFormLayout,
    QWidget, QDateEdit, QTableView, QFileDialog, QListWidget,
    QListWidgetItem, QHeaderView, QFrame, QScrollArea, QApplication,
    QStyledItemDelegate, QSpinBox, QStyle, QStyleOptionButton
)
from PyQt6.QtCore import Qt, QDate, QRect, QEvent, QPoint, QSortFilterProxyModel
from PyQt6.QtSql import QSqlQueryModel
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QColor, QPalette, QPen

# In ui_core.py, sostituisci STYLE_AGRIMESSINA con questa versione corretta
STYLE_AGRIMESSINA = """
    QMainWindow, QDialog, QWidget {
        background-color: #FDF9F3;
        color: #212121;
        font-family: 'Segoe UI', sans-serif;
    }

    /* --- TABELLE MODERNE --- */
    QTableView {
        background-color: #FFFFFF;
        alternate-background-color: #FAFAF7;
        selection-background-color: #E8F5E9;
        selection-color: #1B5E20;
        gridline-color: transparent;
        border: 1px solid #EAEAEA;
        border-radius: 10px;
        font-size: 13px;
        color: #212121;
        outline: 0;
    }

    QTableView::item {
        padding: 8px 10px;
        border: none;
        border-bottom: 1px solid #F0F0EC;
    }

    QTableView::item:selected {
        background-color: #E8F5E9;
        color: #1B5E20;
    }

    QTableView::item:hover {
        background-color: #F1F8E9;
    }

    QHeaderView {
        background-color: transparent;
        border: none;
    }

    QHeaderView::section {
        background-color: #FDF9F3;
        color: #2E7D32;
        padding: 12px 10px;
        border: none;
        border-bottom: 2px solid #2E7D32;
        font-weight: bold;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    QHeaderView::section:horizontal:first {
        border-top-left-radius: 10px;
    }

    QHeaderView::section:horizontal:last {
        border-top-right-radius: 10px;
    }

    QHeaderView::section:vertical {
        background-color: #FDF9F3;
        color: #9E9E9E;
        border-bottom: 1px solid #F0F0EC;
        border-right: 1px solid #EAEAEA;
        padding: 8px;
        font-weight: normal;
        text-transform: none;
        letter-spacing: 0;
    }

    QTableCornerButton::section {
        background-color: #FDF9F3;
        border: none;
        border-bottom: 2px solid #2E7D32;
        border-top-left-radius: 10px;
    }

    #Sidebar {
        background-color: #FFFFFF;
        border-right: 1px solid #E0E0E0;
    }

    #Sidebar QPushButton {
        background-color: transparent;
        border: none;
        color: #757575;
        text-align: left;
        padding: 15px 25px;
        font-size: 14px;
    }

    #Sidebar QPushButton:checked {
        background-color: #E8F5E9;
        color: #2E7D32;
        border-left: 5px solid #2E7D32;
        font-weight: bold;
    }

    #SearchBar {
        background-color: #FFFFFF;
        border: 1px solid #BDBDBD;
        border-radius: 10px;
        padding: 10px 15px;
        font-size: 14px;
        margin-bottom: 10px;
    }

    #TrattamentoCard {
        background-color: #FFFFFF;
        border: 1px solid #E0E0E0;
        border-radius: 15px;
    }

    QPushButton.success {
    background-color: #2E7D32;
    color: white;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: bold;
    }

    QPushButton.success:hover {
        background-color: #1B5E20;
    }

    QPushButton.danger {
        background-color: #D32F2F;
        color: white;
        border-radius: 8px;
        padding: 10px 20px;
        font-weight: bold;
    }

    QPushButton.danger:hover {
        background-color: #B71C1C;
    }

    QPushButton.warning {
        background-color: #F57C00;
        color: white;
        border-radius: 8px;
        padding: 10px 20px;
        font-weight: bold;
    }

    QPushButton.warning:hover {
        background-color: #E65100;
    }

    QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 2px;
    border: none;
    }

    QScrollBar::handle:vertical {
        background: #C8E6C9;
        border-radius: 5px;
        min-height: 30px;
    }

    QScrollBar::handle:vertical:hover {
        background: #81C784;
    }

    QScrollBar::handle:vertical:pressed {
        background: #2E7D32;
    }

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
        background: none;
        border: none;
    }

    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
    }

    QScrollBar:horizontal {
        background: transparent;
        height: 10px;
        margin: 2px 4px 2px 4px;
        border: none;
    }

    QScrollBar::handle:horizontal {
        background: #C8E6C9;
        border-radius: 5px;
        min-width: 30px;
    }

    QScrollBar::handle:horizontal:hover {
        background: #81C784;
    }

    QScrollBar::handle:horizontal:pressed {
        background: #2E7D32;
    }

    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        width: 0px;
        background: none;
        border: none;
    }

    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: transparent;
    }

    QPushButton.secondary {
    background-color: #1976D2;
    color: white;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: bold;
    }

    QPushButton.secondary:hover {
        background-color: #1565C0;
    }

    QPushButton.secondary:pressed {
        background-color: #0D47A1;
    }

    QPushButton.success:pressed {
    background-color: #1B5E20;
    }

    QPushButton.danger:pressed {
        background-color: #B71C1C;
    }

    QPushButton.warning:pressed {
        background-color: #BF360C;
    }

    #CustomTitleBar {
        background-color: #FFFFFF;
        border-bottom: 1px solid #EAEAEA;
    }

    #CustomTitleBar QPushButton {
        background-color: transparent;
        border: none;
        color: #212121;
        font-size: 16px;
        font-family: 'Segoe UI Symbol';
    }


"""

class DelegatoIntero(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setFrame(False)
        editor.setMinimum(0)
        editor.setMaximum(999999)
        return editor

class DelegatoDecimale(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QDoubleSpinBox(parent)
        editor.setFrame(False)
        editor.setMinimum(0.0)
        editor.setMaximum(999999.99)
        editor.setDecimals(2)
        return editor

from PyQt6.QtGui import QStandardItemModel, QStandardItem
from sqlalchemy import text

class PannelloBaseDialog(QWidget):
    COLONNE_NASCOSTE = []

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # --- SEZIONE PULSANTI ---
        btn_bar = QHBoxLayout()
        self.btn_nuovo    = QPushButton("➕ Nuovo")
        self.btn_modifica = QPushButton("✏️ Modifica")
        self.btn_elimina  = QPushButton("🗑️ Elimina")
        self.btn_aggiorna = QPushButton("🔄 Aggiorna")

        self.btn_nuovo.setProperty('class', 'success')
        self.btn_modifica.setProperty('class', 'warning')
        self.btn_elimina.setProperty('class', 'danger')
        self.btn_aggiorna.setProperty('class', 'secondary')

        self.btn_nuovo.clicked.connect(self._on_nuovo)
        self.btn_modifica.clicked.connect(self._on_modifica)
        self.btn_elimina.clicked.connect(self._on_elimina)
        self.btn_aggiorna.clicked.connect(self.aggiorna_dati)

        btn_bar.addWidget(self.btn_nuovo)
        btn_bar.addWidget(self.btn_modifica)
        btn_bar.addWidget(self.btn_elimina)
        btn_bar.addStretch()
        btn_bar.addWidget(self.btn_aggiorna)
        layout.addLayout(btn_bar)

        # --- SEZIONE TABELLA ---
        self.modello = QStandardItemModel() # <--- MODIFICATO QUI
        self.vista   = QTableView()
        self.vista.setModel(self.modello)
        self.vista.setAlternatingRowColors(True)
        self.vista.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.vista.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)

        header = self.vista.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(100)

        layout.addWidget(self.vista)

    # ---> NUOVO METODO PER ESEGUIRE LE QUERY <---
    def esegui_query(self, query_sql, engine):
        self.modello.clear()
        with engine.connect() as conn:
            result = conn.execute(text(query_sql))
            col_names = list(result.keys())
            self.modello.setHorizontalHeaderLabels(col_names)

            for row in result:
                items = []
                row_dict = dict(row._mapping) # Salva il dizionario della riga
                for val in row:
                    item = QStandardItem(str(val) if val is not None else "")
                    item.setEditable(False)
                    items.append(item)

                # Nascondiamo i dati originali nel primo elemento per recuperarli facilmente
                items[0].setData(row_dict, Qt.ItemDataRole.UserRole)
                self.modello.appendRow(items)

    def _on_nuovo(self):
        self.apri_dialog_nuovo()

    def _on_modifica(self):
        riga = self._riga_selezionata()
        if riga is not None:
            self.apri_dialog_modifica(riga)

    def _on_elimina(self):
        riga = self._riga_selezionata()
        if riga is not None:
            self.elimina_record(riga)

    def _riga_selezionata(self):
        idx = self.vista.currentIndex()
        if not idx.isValid():
            QMessageBox.warning(self, "Nessuna selezione", "Seleziona prima una riga.")
            return None

        item = self.modello.item(idx.row(), 0)
        if item:
            row_dict = item.data(Qt.ItemDataRole.UserRole)

            # Creiamo una classe finta per far credere al resto del codice di usare ancora QSqlRecord
            class DummyRecord:
                def __init__(self, d): self.d = d
                def value(self, k): return self.d.get(k)

            return DummyRecord(row_dict)
        return None

    def _nascondi_colonne(self):
        for col in self.COLONNE_NASCOSTE:
            self.vista.setColumnHidden(col, True)

        QApplication.processEvents()
        header = self.vista.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        for i in range(self.modello.columnCount()):
            if not self.vista.isColumnHidden(i):
                width = header.sectionSize(i)
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                header.resizeSection(i, width + 35)

    def aggiorna_dati(self):
        raise NotImplementedError

    def apri_dialog_nuovo(self):
        raise NotImplementedError

    def apri_dialog_modifica(self, riga):
        raise NotImplementedError

    def elimina_record(self, riga):
        raise NotImplementedError

class DelegateTendoniColorati(QStyledItemDelegate):
    def paint(self, painter, option, index):
        model = index.model()
        idx_col0 = model.index(index.row(), 0)
        dati = idx_col0.data(Qt.ItemDataRole.UserRole)

        if not dati or not isinstance(dati, dict):
            return super().paint(painter, option, index)

        stato = dati.get("Stato_Dose", 0)
        opt = option.__class__(option)

        colore = None
        if stato == 1: colore = QColor(210, 255, 210)  # Verde
        elif stato == 2: colore = QColor(255, 210, 210) # Rosso
        elif stato == 3: colore = QColor(210, 225, 255) # Blu

        if colore:
            painter.save()
            painter.fillRect(opt.rect, colore)
            painter.restore()
            opt.palette.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.Text, QColor(Qt.GlobalColor.black))
            opt.palette.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.HighlightedText, QColor(Qt.GlobalColor.black))

        super().paint(painter, opt, index)

class DelegateMovimenti(QStyledItemDelegate):
    def paint(self, painter, option, index):
        opt = option.__class__(option)
        self.initStyleOption(opt, index)

        testo_riga = index.model().data(index.model().index(index.row(), 2))

        if testo_riga == "SCARICO":
            bg_color = QColor("#FFCDD2")
        else:
            bg_color = QColor("#C8E6C9")

        painter.save()
        painter.fillRect(opt.rect, bg_color)
        painter.setPen(QColor("black"))
        painter.drawText(opt.rect.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, opt.text)
        painter.restore()

class MultiFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mappa_filtri = {}

    def imposta_filtro_colonna(self, col, testo):
        self.mappa_filtri[col] = testo.lower().strip()
        self.invalidateFilter()

    def filterAcceptsRow(self, r_src, p_src):
        for col, txt in self.mappa_filtri.items():
            if not txt: continue
            val = str(self.sourceModel().data(self.sourceModel().index(r_src, col, p_src))).lower()
            if txt not in val: return False
        return True

class DelegateConflittiStorico(QStyledItemDelegate):
    def paint(self, painter, option, index):
        riga_vera  = index.model().mapToSource(index).row()
        violazione = index.model().sourceModel().record(riga_vera).value("Violazione")
        if violazione == 1:
            painter.save()
            painter.fillRect(option.rect, QColor(255, 210, 210))
            painter.restore()
        super().paint(painter, option, index)

class DelegateCheckboxStorico(QStyledItemDelegate):
    def __init__(self, scheda, parent=None):
        super().__init__(parent)
        self.scheda = scheda

    def paint(self, painter, option, index):
        src_index = index.model().mapToSource(index)
        rec = index.model().sourceModel().record(src_index.row())
        tipo_viol = rec.value("TipoViolazione") or 0

        opt = option.__class__(option)
        self.initStyleOption(opt, index)

        colori_bg = {
            0: QColor("#FFFFFF"),
            1: QColor("#FFEB3B"),
            2: QColor("#F44336"),
            3: QColor("#2196F3"),
            4: QColor("#FFC107"),
            5: QColor("#212121"),
        }
        colore_sfondo = colori_bg.get(tipo_viol, QColor("#FFFFFF"))

        painter.save()
        painter.fillRect(opt.rect, colore_sfondo)

        if tipo_viol in (2, 3, 5):
            painter.setPen(QColor("white"))
        else:
            painter.setPen(QColor("black"))

        if index.column() == 1:
            cb_size = 16
            cb_rect = QRect(opt.rect.left() + 6, opt.rect.top() + (opt.rect.height() - cb_size) // 2, cb_size, cb_size)
            painter.drawRect(cb_rect)
            if rec.value("_ID_T") in self.scheda._checkbox_ids:
                painter.setPen(QPen(QColor("white") if tipo_viol in (2,3,5) else QColor("black"), 2))
                painter.drawLine(cb_rect.bottomLeft() + QPoint(3, -7), cb_rect.bottomLeft() + QPoint(7, -3))
                painter.drawLine(cb_rect.bottomLeft() + QPoint(7, -3), cb_rect.topRight() + QPoint(-3, 4))

            painter.drawText(opt.rect.adjusted(30, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, opt.text)
        else:
            painter.drawText(opt.rect.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, opt.text)

        painter.restore()

    def editorEvent(self, event, model, option, index):
        if index.column() == 1 and event.type() == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.LeftButton:
                cb_size = 16
                cb_rect = QRect(option.rect.left() + 6, option.rect.top() + (option.rect.height() - cb_size) // 2, cb_size, cb_size)
                if cb_rect.contains(event.position().toPoint()):
                    src_index = model.mapToSource(index)
                    id_t = model.sourceModel().record(src_index.row()).value("_ID_T")
                    if id_t is not None:
                        if id_t in self.scheda._checkbox_ids: self.scheda._checkbox_ids.discard(id_t)
                        else: self.scheda._checkbox_ids.add(id_t)
                        self.scheda._aggiorna_label_selezione()
                        model.dataChanged.emit(index, index)
                    return True
        return super().editorEvent(event, model, option, index)
