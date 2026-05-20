"""Dialog di verifica magazzino lato desktop.

Allinea il desktop alla feature webapp `/magazzino/verifica`:
- Mostra residui di giacenze negative (per cui esistono candidati o nessuno).
- Mostra scarichi conv su Agrimessina (regola bio-only).
- Per ogni issue propone candidati e permette di applicare la correzione.

L'auto-apply degli issue univocal viene fatto a monte (vedi
`run_verifica_magazzino_on_startup` chiamato in main.py). Questo dialog
riceve i RESIDUI ambigui e raccoglie input dall'utente.
"""
from __future__ import annotations
import logging
from typing import Any, Callable

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QFrame, QScrollArea, QWidget, QMessageBox, QSizePolicy,
)
from PyQt6.QtCore import Qt

log = logging.getLogger(__name__)


class MagazzinoVerificaDialog(QDialog):
    """Dialog read-only sui residui + per-issue dropdown + bottone Applica.

    `api` è l'ApiClient già autenticato. `residui` è il dict prodotto
    dall'endpoint /magazzino/verifica (o dal payload .residui di
    /magazzino/verifica/auto-apply).
    """

    def __init__(self, api, residui: dict[str, Any], on_changed: Callable[[], None] | None = None, parent=None):
        super().__init__(parent)
        self.api = api
        self.residui = residui or {"negativi": [], "bio_violations": []}
        self.on_changed = on_changed
        self.setWindowTitle("Verifica magazzino")
        self.resize(720, 560)

        layout = QVBoxLayout(self)
        tot = len(self.residui.get("negativi") or []) + len(self.residui.get("bio_violations") or [])
        if tot == 0:
            layout.addWidget(QLabel("✓ Nessuna anomalia rilevata. Magazzino conforme."))
        else:
            intro = QLabel(
                f"Trovati {tot} issue ambigui che richiedono il tuo input.\n"
                "Gli issue univocal (1 sola opzione) sono già stati applicati."
            )
            intro.setWordWrap(True)
            layout.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self._items_layout = QVBoxLayout(container)
        self._items_layout.setSpacing(8)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        # Bio prima (riducono saldi positivi che servono per i negativi).
        for iss in self.residui.get("bio_violations") or []:
            self._items_layout.addWidget(self._build_bio_row(iss))
        for iss in self.residui.get("negativi") or []:
            self._items_layout.addWidget(self._build_neg_row(iss))
        self._items_layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_rerun = QPushButton("🔁 Riapplica univoci")
        btn_rerun.clicked.connect(self._rerun_auto_apply)
        btn_close = QPushButton("Chiudi")
        btn_close.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_rerun)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    # ----- BIO -------------------------------------------------------------

    def _build_bio_row(self, iss: dict) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("background-color: rgba(255, 200, 100, 30);")
        v = QVBoxLayout(frame)

        head = QLabel(
            f"<b>BIO</b> · {iss.get('prodotto_nome')} · "
            f"#{iss.get('scarico_id')} · T#{iss.get('trattamento_id', '—')} · "
            f"{iss.get('data_movimento')}"
        )
        v.addWidget(head)
        desc = QLabel(
            f"Scarico di <b>{_fmt(iss.get('quantita'))}</b> su "
            f"<b>{iss.get('azienda_nome')}</b>"
            + (f" (tendone: {iss.get('azienda_nome_origine')})"
               if iss.get('azienda_nome_origine') else "")
        )
        desc.setWordWrap(True)
        v.addWidget(desc)

        candidati = iss.get("candidati") or []
        # Le violazioni bio sono auto-applicate via priorità Messina Alfio →
        # La Gazzella (regola mandatoria, indipendente dal saldo). Una bio
        # arriva qui residua SOLO se nessuna delle due aziende esiste in DB.
        if not candidati:
            err = QLabel(
                "⚠ Configurazione: né \"Messina Alfio\" né \"La Gazzella\" "
                "esistono come azienda. Creane almeno una in Anagrafiche "
                "per applicare la regola bio."
            )
            err.setStyleSheet("color: #b00;")
            err.setWordWrap(True)
            v.addWidget(err)
            return frame

        row = QHBoxLayout()
        combo = QComboBox()
        for c in candidati:
            text = f"{c['azienda_nome']} (saldo {_fmt(c['saldo'])})"
            combo.addItem(text, userData=c)
        row.addWidget(combo, 1)
        btn = QPushButton("Sposta")

        def apply_clicked():
            data = combo.currentData()
            if not data:
                return
            try:
                self.api.applica_verifica_bio(
                    scarico_id=iss["scarico_id"],
                    nuova_azienda_id=data["azienda_id"],
                )
            except Exception as e:
                log.exception("applica_verifica_bio fallito")
                QMessageBox.critical(self, "Errore", str(e))
                return
            btn.setEnabled(False)
            btn.setText("Applicato ✓")
            if self.on_changed:
                self.on_changed()

        btn.clicked.connect(apply_clicked)
        row.addWidget(btn)
        v.addLayout(row)
        return frame

    # ----- NEG -------------------------------------------------------------

    def _build_neg_row(self, iss: dict) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("background-color: rgba(255, 100, 100, 30);")
        v = QVBoxLayout(frame)

        head = QLabel(
            f"<b>NEG</b> · {iss.get('prodotto_nome')} · "
            f"su <b>{iss.get('azienda_nome')}</b> · "
            f"<span style='color:#b00'>{_fmt(iss.get('saldo'))}</span>"
        )
        v.addWidget(head)

        candidati = iss.get("candidati") or []
        if not candidati:
            err = QLabel(
                "⚠ Nessun magazzino ha saldo positivo di questo prodotto. Compensa con un CARICO manuale."
            )
            err.setStyleSheet("color: #b00;")
            err.setWordWrap(True)
            v.addWidget(err)
            return frame

        row = QHBoxLayout()
        combo = QComboBox()
        for c in candidati:
            text = f"{c['azienda_nome']} (saldo {_fmt(c['saldo'])})"
            if not c.get("sufficiente"):
                text += " — parziale"
            combo.addItem(text, userData=c)
        row.addWidget(combo, 1)
        qta_label = QLabel("")
        qta_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        row.addWidget(qta_label)

        def update_qta_label():
            data = combo.currentData()
            if not data:
                qta_label.setText("")
                return
            qta = min(float(iss.get("qta_da_compensare", 0)), float(data["saldo"]))
            qta_label.setText(f"presta {_fmt(qta)}")
            qta_label.setProperty("qta_value", qta)

        combo.currentIndexChanged.connect(lambda _i: update_qta_label())
        update_qta_label()

        btn = QPushButton("Applica")

        def apply_clicked():
            data = combo.currentData()
            if not data:
                return
            qta = float(qta_label.property("qta_value") or 0)
            if qta <= 0:
                QMessageBox.warning(self, "Verifica", "Quantità non valida.")
                return
            try:
                self.api.applica_verifica_negativo(
                    prodotto_id=iss["prodotto_id"],
                    azienda_neg_id=iss["azienda_id"],
                    azienda_pos_id=data["azienda_id"],
                    qta=qta,
                )
            except Exception as e:
                log.exception("applica_verifica_negativo fallito")
                QMessageBox.critical(self, "Errore", str(e))
                return
            btn.setEnabled(False)
            btn.setText("Applicato ✓")
            if self.on_changed:
                self.on_changed()

        btn.clicked.connect(apply_clicked)
        row.addWidget(btn)
        v.addLayout(row)
        return frame

    # ----- RERUN -----------------------------------------------------------

    def _rerun_auto_apply(self):
        try:
            resp = self.api.auto_apply_verifica()
        except Exception as e:
            log.exception("auto_apply fallito")
            QMessageBox.critical(self, "Errore", str(e))
            return
        applicati = resp.get("applicati", {}) if isinstance(resp, dict) else {}
        tot = (applicati.get("bio") or 0) + (applicati.get("negativi") or 0)
        if tot > 0:
            QMessageBox.information(
                self, "Verifica magazzino",
                f"Applicate {tot} correzioni univoche."
            )
        if self.on_changed:
            self.on_changed()
        # Chiude e riapre per riflettere i nuovi residui.
        self.residui = resp.get("residui", {}) if isinstance(resp, dict) else {}
        self.accept()


def run_verifica_magazzino_on_startup(api, parent=None) -> None:
    """Eseguita dal main subito dopo lo startup, solo se l'utente è ADMIN.

    Lancia auto-apply e, se restano residui ambigui, apre il dialog.
    Errori (es. 403 per non-admin, network) loggati e ignorati: non devono
    bloccare l'avvio.
    """
    is_admin = bool(getattr(api, "is_admin", False))
    if not is_admin:
        return
    try:
        resp = api.auto_apply_verifica()
    except Exception as e:
        log.warning("[verifica-magazzino] auto-apply fallito: %s", e)
        return
    if not isinstance(resp, dict):
        return
    applicati = resp.get("applicati") or {}
    tot_app = int(applicati.get("bio") or 0) + int(applicati.get("negativi") or 0)
    if tot_app > 0:
        log.info("[verifica-magazzino] auto-apply: %d correzioni applicate", tot_app)
    residui = resp.get("residui") or {}
    tot_res = len(residui.get("negativi") or []) + len(residui.get("bio_violations") or [])
    if tot_res == 0:
        return
    dlg = MagazzinoVerificaDialog(api, residui, parent=parent)
    dlg.exec()


def _fmt(n) -> str:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "—"
    if x != x or x in (float("inf"), float("-inf")):
        return "—"
    return f"{x:,.4f}".rstrip("0").rstrip(",.") or "0"
