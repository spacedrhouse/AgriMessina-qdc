"""Finestra di login per l'app desktop. Stile coerente con il resto della app."""
from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QApplication
)
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from api_client import ApiClient, NotAuthenticatedError, NetworkError, ApiError


class LoginWorker(QThread):
    """Esegue la chiamata di login in un thread separato per non congelare la UI."""
    success = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, api: ApiClient, username: str, password: str):
        super().__init__()
        self.api = api
        self.username = username
        self.password = password

    def run(self):
        try:
            self.api.login(self.username, self.password)
            self.success.emit()
        except NotAuthenticatedError:
            self.error.emit("Credenziali non valide")
        except NetworkError as e:
            self.error.emit(f"Server non raggiungibile.\n{e}")
        except ApiError as e:
            self.error.emit(f"Errore: {e}")
        except Exception as e:
            self.error.emit(f"Errore inatteso: {e}")


class LoginDialog(QDialog):
    """Finestra modale di login. Si chiude con accept() solo se il login riesce."""

    def __init__(self, api: ApiClient, splash_pixmap: Optional[QPixmap] = None,
                 icon: Optional[QIcon] = None, parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("AgriMessina QDC — Accesso")
        if icon:
            self.setWindowIcon(icon)
        self.setFixedSize(420, 480)
        self.setModal(True)

        self._worker: Optional[LoginWorker] = None
        self._build_ui(splash_pixmap)

    def _build_ui(self, splash_pixmap: Optional[QPixmap]):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(15)

        # Logo
        if splash_pixmap and not splash_pixmap.isNull():
            lbl_logo = QLabel()
            scaled = splash_pixmap.scaled(
                280, 120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            lbl_logo.setPixmap(scaled)
            lbl_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl_logo)

        # Titolo
        lbl_title = QLabel("Accedi al sistema")
        lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2E7D32; padding-top: 10px;")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        # Username
        layout.addWidget(QLabel("Email:"))
        self.et_username = QLineEdit()
        self.et_username.setPlaceholderText("nome@esempio.it")
        self.et_username.setStyleSheet("padding: 8px; font-size: 13px;")
        layout.addWidget(self.et_username)

        # Password
        layout.addWidget(QLabel("Password:"))
        self.et_password = QLineEdit()
        self.et_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.et_password.setPlaceholderText("••••••••")
        self.et_password.setStyleSheet("padding: 8px; font-size: 13px;")
        self.et_password.returnPressed.connect(self._on_login)
        layout.addWidget(self.et_password)

        # Pulsante login
        self.btn_login = QPushButton("Accedi")
        self.btn_login.setStyleSheet("""
            QPushButton {
                background-color: #2E7D32; color: white; padding: 12px;
                font-size: 14px; font-weight: bold; border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #388E3C; }
            QPushButton:disabled { background-color: #A5D6A7; }
        """)
        self.btn_login.clicked.connect(self._on_login)
        layout.addWidget(self.btn_login)

        # Status label
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #757575; font-size: 11px; padding: 5px;")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        layout.addStretch()

    def _on_login(self):
        # Guard reentrancy: returnPressed sulla password può scattare anche
        # quando btn_login è disabilitato, e senza questa guardia un secondo
        # Invio creerebbe un secondo LoginWorker prima che il primo finisca.
        if self._worker is not None and self._worker.isRunning():
            return

        user = self.et_username.text().strip()
        # Niente .strip() sulla password: gli spazi possono far parte di una
        # passphrase valida e silenziosamente strapparli causerebbe failure
        # inspiegabili.
        pwd = self.et_password.text()
        if not user or not pwd:
            QMessageBox.warning(self, "Campi vuoti", "Inserisci email e password.")
            return

        self.btn_login.setEnabled(False)
        self.lbl_status.setText("Connessione in corso…")
        QApplication.processEvents()

        # Pulisci il worker precedente prima di crearne uno nuovo: senza,
        # un click ripetuto del pulsante creerebbe QThread orfani che restano
        # come child del dialog finché chiude.
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = LoginWorker(self.api, user, pwd)
        self._worker.success.connect(self._on_login_success)
        self._worker.error.connect(self._on_login_error)
        self._worker.start()

    def _on_login_success(self):
        self.lbl_status.setText("Accesso effettuato.")
        self.accept()

    def _on_login_error(self, msg: str):
        self.btn_login.setEnabled(True)
        self.lbl_status.setText("")
        QMessageBox.critical(self, "Login fallito", msg)
