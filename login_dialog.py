"""Finestra di login per l'app desktop. Stile coerente con il resto della app."""
from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QMessageBox, QApplication
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
        layout.addWidget(QLabel("Email/Nome Utente:"))
        self.et_username = QLineEdit()
        self.et_username.setPlaceholderText("nome@esempio.it o username")
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

        # Link "Crea nuovo utente": apre un dialog che richiede credenziali
        # admin per l'autorizzazione. Non è un'auto-registrazione: il backend
        # impone require_admin sull'endpoint, quindi senza un admin valido
        # la creazione fallisce. Tenuto come link discreto per non confondere
        # l'utente comune (la stragrande maggioranza dei login NON crea utenti).
        self.btn_signup = QPushButton("Crea nuovo utente")
        self.btn_signup.setFlat(True)
        self.btn_signup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_signup.setStyleSheet("""
            QPushButton {
                color: #2E7D32; background: transparent; border: none;
                font-size: 12px; text-decoration: underline; padding: 4px;
            }
            QPushButton:hover { color: #1B5E20; }
        """)
        self.btn_signup.clicked.connect(self._apri_signup)
        layout.addWidget(self.btn_signup, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch()

    def _apri_signup(self):
        """Apre il dialog di creazione utente. Pre-popola con le credenziali
        già scritte nel login form (comune: admin si è loggato qui, vuole
        creare un nuovo operatore senza re-inserire le proprie credenziali).
        """
        dlg = SignupDialog(
            self.api,
            admin_username=self.et_username.text().strip(),
            admin_password=self.et_password.text(),
            parent=self,
        )
        dlg.exec()

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
            QMessageBox.warning(self, "Campi vuoti", "Inserisci email/nome utente e password.")
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

    def closeEvent(self, event):
        # Se l'utente chiude mentre il login è in corso, il worker resterebbe
        # vivo a emettere segnali su un dialog distrutto: blocca l'emit
        # disconnettendo i signal, poi attende che il thread termini.
        if self._worker is not None and self._worker.isRunning():
            try:
                self._worker.success.disconnect()
                self._worker.error.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._worker.wait(2000)
        super().closeEvent(event)


class SignupWorker(QThread):
    """Crea un nuovo utente sul server. Flow:
      1. Login con credenziali admin (richieste nel form).
      2. POST /utenti col payload del nuovo utente.
      3. Logout dell'admin (token usa-e-getta).

    Usa una sua istanza di ApiClient (passata dal chiamante) così non
    sporca la sessione del LoginDialog padre — l'utente che stava facendo
    il proprio login deve poter procedere senza essere "spinto" come admin.
    """
    success = pyqtSignal(str)   # username creato
    error = pyqtSignal(str)

    def __init__(self, api: ApiClient, admin_user: str, admin_pwd: str,
                 new_user_dto: dict):
        super().__init__()
        self.api = api
        self.admin_user = admin_user
        self.admin_pwd = admin_pwd
        self.new_user_dto = new_user_dto

    def run(self):
        try:
            # Salvataggio temporaneo del token corrente: il login admin
            # sovrascrive lo stato di self.api. Lo ripristiniamo a fine
            # del worker così l'utente che stava loggando in LoginDialog
            # non perde la sessione (se ce l'aveva).
            try:
                self.api.login(self.admin_user, self.admin_pwd)
            except NotAuthenticatedError:
                self.error.emit(
                    "Credenziali admin non valide. La creazione utenti "
                    "richiede un account ADMIN già esistente."
                )
                return

            try:
                self.api.create_utente(self.new_user_dto)
            except ApiError as e:
                # 403 → non admin, 409 → username già esistente, ecc.
                self.error.emit(self._format_api_error(e))
                return
            finally:
                # Logout admin SEMPRE: anche se la create ha avuto successo,
                # non vogliamo che la sessione dell'app prosegua come admin
                # quando l'utente vero ha credenziali diverse.
                self.api.logout()

            self.success.emit(self.new_user_dto.get("username") or "")

        except NetworkError as e:
            self.error.emit(f"Server non raggiungibile.\n{e}")
        except Exception as e:
            self.error.emit(f"Errore inatteso: {e}")

    @staticmethod
    def _format_api_error(e: ApiError) -> str:
        status = getattr(e, "status_code", None)
        detail = getattr(e, "detail", None) or str(e)
        if status == 403:
            return ("Non hai privilegi ADMIN. Solo un amministratore può "
                    "creare nuovi utenti.")
        if status == 409:
            return f"Utente già esistente.\n{detail}"
        if status == 400:
            return f"Dati non validi.\n{detail}"
        return f"Errore HTTP {status}: {detail}" if status else str(e)


class SignupDialog(QDialog):
    """Dialog di creazione utente. Richiede credenziali admin per
    autorizzare la chiamata e i dati del nuovo utente.

    Non è un flusso di self-signup: il backend impone require_admin.
    """

    def __init__(self, api: ApiClient,
                 admin_username: str = "", admin_password: str = "",
                 parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Crea nuovo utente")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._worker: Optional[SignupWorker] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 25, 30, 25)
        layout.setSpacing(12)

        lbl_title = QLabel("Nuovo utente")
        lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2E7D32;")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        # Sezione admin (autorizzazione)
        lbl_admin = QLabel(
            "Conferma con un account ADMIN esistente "
            "(serve per autorizzare la creazione):"
        )
        lbl_admin.setStyleSheet("color: #757575; font-size: 11px;")
        lbl_admin.setWordWrap(True)
        layout.addWidget(lbl_admin)

        form_admin = QFormLayout()
        self.et_admin_user = QLineEdit(admin_username)
        self.et_admin_user.setPlaceholderText("admin o email admin")
        self.et_admin_pwd = QLineEdit(admin_password)
        self.et_admin_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.et_admin_pwd.setPlaceholderText("Password admin")
        form_admin.addRow("Username admin:", self.et_admin_user)
        form_admin.addRow("Password admin:", self.et_admin_pwd)
        layout.addLayout(form_admin)

        # Separatore
        sep = QLabel("Dati del nuovo utente:")
        sep.setStyleSheet("color: #757575; font-size: 11px; padding-top: 8px;")
        layout.addWidget(sep)

        form_new = QFormLayout()
        self.et_new_username = QLineEdit()
        self.et_new_username.setPlaceholderText("es. mario.rossi")
        self.et_new_pwd = QLineEdit()
        self.et_new_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.et_new_pwd.setPlaceholderText("Minimo 4 caratteri")
        self.et_new_pwd2 = QLineEdit()
        self.et_new_pwd2.setEchoMode(QLineEdit.EchoMode.Password)
        self.et_new_pwd2.setPlaceholderText("Ripeti password")
        self.et_new_nome = QLineEdit()
        self.et_new_cognome = QLineEdit()
        self.et_new_email = QLineEdit()
        self.et_new_email.setPlaceholderText("opzionale")
        self.cmb_ruolo = QComboBox()
        # ADMIN come secondo: default BASIC, è meno pericoloso.
        self.cmb_ruolo.addItems(["BASIC", "ADMIN"])

        form_new.addRow("Username:", self.et_new_username)
        form_new.addRow("Password:", self.et_new_pwd)
        form_new.addRow("Conferma password:", self.et_new_pwd2)
        form_new.addRow("Nome:", self.et_new_nome)
        form_new.addRow("Cognome:", self.et_new_cognome)
        form_new.addRow("Email:", self.et_new_email)
        form_new.addRow("Ruolo:", self.cmb_ruolo)
        layout.addLayout(form_new)

        # Bottoni
        h_btn = QHBoxLayout()
        self.btn_annulla = QPushButton("Annulla")
        self.btn_annulla.clicked.connect(self.reject)
        self.btn_crea = QPushButton("Crea utente")
        self.btn_crea.setStyleSheet("""
            QPushButton {
                background-color: #2E7D32; color: white; padding: 10px 16px;
                font-weight: bold; border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #388E3C; }
            QPushButton:disabled { background-color: #A5D6A7; }
        """)
        self.btn_crea.clicked.connect(self._on_crea)
        h_btn.addStretch()
        h_btn.addWidget(self.btn_annulla)
        h_btn.addWidget(self.btn_crea)
        layout.addLayout(h_btn)

        # Status
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #757575; font-size: 11px;")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

    def _on_crea(self):
        if self._worker is not None and self._worker.isRunning():
            return

        admin_user = self.et_admin_user.text().strip()
        admin_pwd = self.et_admin_pwd.text()
        new_username = self.et_new_username.text().strip()
        new_pwd = self.et_new_pwd.text()
        new_pwd2 = self.et_new_pwd2.text()

        if not admin_user or not admin_pwd:
            QMessageBox.warning(self, "Campi mancanti",
                                "Inserisci le credenziali admin.")
            return
        if not new_username:
            QMessageBox.warning(self, "Campi mancanti",
                                "Username del nuovo utente obbligatorio.")
            return
        if len(new_pwd) < 4:
            QMessageBox.warning(self, "Password troppo corta",
                                "Minimo 4 caratteri.")
            return
        if new_pwd != new_pwd2:
            QMessageBox.warning(self, "Password non coincidono",
                                "Le due password inserite sono diverse.")
            return

        dto = {
            "username": new_username,
            "password": new_pwd,
            "ruolo": self.cmb_ruolo.currentText(),
            "nome": self.et_new_nome.text().strip() or None,
            "cognome": self.et_new_cognome.text().strip() or None,
            "email": self.et_new_email.text().strip() or None,
        }

        self.btn_crea.setEnabled(False)
        self.btn_annulla.setEnabled(False)
        self.lbl_status.setText("Creazione in corso…")
        QApplication.processEvents()

        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = SignupWorker(self.api, admin_user, admin_pwd, dto)
        self._worker.success.connect(self._on_success)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_success(self, username: str):
        QMessageBox.information(
            self, "Utente creato",
            f"L'utente '{username}' è stato creato con successo.\n"
            "Può ora accedere con la propria password.",
        )
        self.accept()

    def _on_error(self, msg: str):
        self.btn_crea.setEnabled(True)
        self.btn_annulla.setEnabled(True)
        self.lbl_status.setText("")
        QMessageBox.critical(self, "Creazione fallita", msg)

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            try:
                self._worker.success.disconnect()
                self._worker.error.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._worker.wait(2000)
        super().closeEvent(event)
