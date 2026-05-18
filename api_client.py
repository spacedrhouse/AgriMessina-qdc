"""Client HTTP per parlare col backend FastAPI.

Tutte le chiamate sono sincrone (httpx). PyQt6 non è async-friendly per default,
quindi usiamo le chiamate bloccanti dentro QThread o le chiamiamo dal main thread
sapendo che il backend è veloce. Le risposte 4xx/5xx alzano `ApiError`.

Il token viene tenuto in memoria E persistito in `~/.agrimessina/auth.json`.
"""
from __future__ import annotations
import os
import json
import base64
from typing import Optional, Any, Dict
from pathlib import Path

import httpx


# ---- Eccezioni dedicate -----------------------------------------------------

class ApiError(Exception):
    """Errore generico dell'API (4xx/5xx, parsing risposta, ecc.)."""
    def __init__(self, message: str, status_code: Optional[int] = None,
                 detail: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class NotAuthenticatedError(ApiError):
    """L'utente non ha un token valido (401/403)."""


class NetworkError(ApiError):
    """Problemi di connessione (DNS, timeout, server down)."""


# ---- Storage del token ------------------------------------------------------

def _config_dir() -> Path:
    """Percorso per il file di config persistente. Stesso su Windows/Linux/macOS."""
    base = Path.home() / ".agrimessina"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _auth_file() -> Path:
    return _config_dir() / "auth.json"


def _load_token_data() -> Dict[str, Any]:
    p = _auth_file()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_token_data(data: Dict[str, Any]) -> None:
    path = _auth_file()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    # Permessi restrittivi: solo l'utente owner può leggere/scrivere.
    # Su Windows os.chmod ha effetti limitati ma non causa errori.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---- Decode JWT senza verifica firma ---------------------------------------

def decode_jwt_payload(token: str) -> Dict[str, Any]:
    """Estrae il payload del JWT (parte centrale). Non verifica la firma:
    serve solo per leggere claim come `sub` (email) e `ruolo`. La verifica
    seria la fa il backend ad ogni richiesta protetta."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # Aggiungi padding se serve (il base64url standard può ometterlo)
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(payload_bytes)
    except Exception:
        return {}


# ---- Client principale ------------------------------------------------------

class ApiClient:
    """Wrapper sincrono attorno alle API del backend.

    Uso tipico:
        api = ApiClient("https://api.agrimessina.it")
        api.login("user@email", "password")
        aziende = api.get_aziende()  # SyncResponse-like dict
    """

    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        # Carica token da disco se esiste
        data = _load_token_data()
        self._token: Optional[str] = data.get("access_token")
        self._username: Optional[str] = data.get("username")
        self._ruolo: Optional[str] = data.get("ruolo")

    # ---- proprietà di stato -------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return bool(self._token)

    @property
    def username(self) -> Optional[str]:
        return self._username

    @property
    def ruolo(self) -> Optional[str]:
        return self._ruolo

    @property
    def is_admin(self) -> bool:
        """True se l'utente loggato è ADMIN. Property (non metodo) così
        l'uso `self.api.is_admin` ritorna sempre il bool, non l'oggetto
        metodo (che era truthy anche per BASIC → bypass dei permessi)."""
        return (self._ruolo or "").upper() == "ADMIN"

    # ---- chiamate base ------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _request(self, method: str, path: str, *,
                 json_body: Optional[Any] = None,
                 data: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        """Esegue una chiamata HTTP e ritorna il JSON parsato (o None se 204).
        Solleva NetworkError per problemi di rete, ApiError per 4xx/5xx."""
        try:
            resp = self._client.request(
                method, path,
                headers=self._headers(),
                json=json_body,
                data=data,
                params=params,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise NetworkError(f"Impossibile contattare il server: {e}")
        except httpx.HTTPError as e:
            raise NetworkError(f"Errore di rete: {e}")

        if resp.status_code in (401, 403):
            raise NotAuthenticatedError(
                "Sessione non valida o credenziali errate",
                status_code=resp.status_code,
                detail=_safe_detail(resp),
            )
        if resp.status_code >= 400:
            raise ApiError(
                f"HTTP {resp.status_code} su {method} {path}",
                status_code=resp.status_code,
                detail=_safe_detail(resp),
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            raise ApiError(f"Risposta non in JSON: {resp.text[:200]}")

    # ---- AUTH ---------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        """Login OAuth2 form-encoded. Salva token e ruolo persistenti.

        ATTENZIONE: il backend autentica usando l'EMAIL come username
        (vedi `auth_router.py: User.email == form_data.username`).
        """
        try:
            resp = self._client.post(
                "/auth/login",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise NetworkError(f"Impossibile contattare il server: {e}")
        except httpx.HTTPError as e:
            raise NetworkError(f"Errore di rete: {e}")

        if resp.status_code in (401, 403):
            raise NotAuthenticatedError(
                "Credenziali non valide",
                status_code=resp.status_code,
                detail=_safe_detail(resp),
            )
        if resp.status_code >= 400:
            raise ApiError(
                f"Login fallito (HTTP {resp.status_code})",
                status_code=resp.status_code,
                detail=_safe_detail(resp),
            )

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise ApiError("Risposta di login senza access_token")

        payload = decode_jwt_payload(token)
        # Il claim `sub` contiene l'email; `ruolo` se il backend lo aggiunge
        self._token = token
        self._username = username
        self._ruolo = payload.get("ruolo") or "BASIC"
        _save_token_data({
            "access_token": self._token,
            "username": self._username,
            "ruolo": self._ruolo,
        })

    def logout(self) -> None:
        """Cancella il token salvato. Non chiama il server (stateless JWT)."""
        self._token = None
        self._username = None
        self._ruolo = None
        try:
            _auth_file().unlink(missing_ok=True)
        except Exception:
            pass

    def close(self) -> None:
        """Chiude il pool di connessioni httpx. Chiamare allo shutdown app
        per evitare warning "Unclosed client". Non rimuove il token salvato."""
        try:
            self._client.close()
        except Exception:
            pass

    # ---- ANAGRAFICHE --------------------------------------------------------

    def sync_aziende(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/aziende", params={"since": since} if since else None)

    def create_azienda(self, nome: str) -> Dict[str, Any]:
        return self._request("POST", "/aziende", json_body={"nome": nome})

    def update_azienda(self, id: int, nome: str) -> Dict[str, Any]:
        return self._request("PUT", f"/aziende/{id}", json_body={"nome": nome})

    def delete_azienda(self, id: int) -> Any:
        return self._request("DELETE", f"/aziende/{id}")

    def sync_agri(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/agri", params={"since": since} if since else None)

    def create_agro(self, azienda_id: int, nome: str) -> Dict[str, Any]:
        return self._request("POST", "/agri", json_body={"azienda_id": azienda_id, "nome": nome})

    def update_agro(self, id: int, azienda_id: int, nome: str) -> Dict[str, Any]:
        return self._request("PUT", f"/agri/{id}", json_body={"azienda_id": azienda_id, "nome": nome})

    def delete_agro(self, id: int) -> Any:
        return self._request("DELETE", f"/agri/{id}")

    def sync_contrade(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/contrade", params={"since": since} if since else None)

    def create_contrada(self, agro_id: int, nome: str) -> Dict[str, Any]:
        return self._request("POST", "/contrade", json_body={"agro_id": agro_id, "nome": nome})

    def update_contrada(self, id: int, agro_id: int, nome: str) -> Dict[str, Any]:
        return self._request("PUT", f"/contrade/{id}", json_body={"agro_id": agro_id, "nome": nome})

    def delete_contrada(self, id: int) -> Any:
        return self._request("DELETE", f"/contrade/{id}")

    def sync_tendoni(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/tendoni", params={"since": since} if since else None)

    def create_tendone(self, contrada_id: int, codice: str, ettari: float) -> Dict[str, Any]:
        return self._request("POST", "/tendoni",
                             json_body={"contrada_id": contrada_id, "codice": codice, "ettari": ettari})

    def update_tendone(self, id: int, contrada_id: int, codice: str, ettari: float) -> Dict[str, Any]:
        return self._request("PUT", f"/tendoni/{id}",
                             json_body={"contrada_id": contrada_id, "codice": codice, "ettari": ettari})

    def delete_tendone(self, id: int) -> Any:
        return self._request("DELETE", f"/tendoni/{id}")

    # ---- PRODOTTI -----------------------------------------------------------

    def sync_prodotti(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/prodotti", params={"since": since} if since else None)

    def create_prodotto(self, dto: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/prodotti", json_body=dto)

    def update_prodotto(self, id: int, dto: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/prodotti/{id}", json_body=dto)

    def delete_prodotto(self, id: int) -> Any:
        return self._request("DELETE", f"/prodotti/{id}")

    # ---- TRATTAMENTI --------------------------------------------------------

    def sync_trattamenti(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/trattamenti", params={"since": since} if since else None)

    def get_trattamento(self, id: int) -> Dict[str, Any]:
        return self._request("GET", f"/trattamenti/{id}")

    def quick_get_trattamento(self, id: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        """GET con timeout corto: usato per pre-check pre-azione (modifica,
        revisiona, elimina). Se la rete è giù vogliamo fallire subito invece
        di bloccare la UI sul timeout default di 20s.

        Default da `CONFIG.quick_get_timeout_s` (env `QDC_QUICK_GET_TIMEOUT_S`):
        prima era hardcoded 3.0 e l'env-var veniva ignorata, frustrante per
        utenti su connessioni lente che non potevano allungare."""
        if timeout is None:
            from config import CONFIG
            timeout = CONFIG.quick_get_timeout_s
        try:
            resp = self._client.request(
                "GET", f"/trattamenti/{id}",
                headers=self._headers(),
                timeout=timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise NetworkError(f"Pre-check timeout: {e}")
        except httpx.HTTPError as e:
            raise NetworkError(f"Errore di rete: {e}")
        if resp.status_code in (401, 403):
            raise NotAuthenticatedError(
                "Sessione non valida", status_code=resp.status_code,
                detail=_safe_detail(resp))
        if resp.status_code >= 400:
            raise ApiError(
                f"HTTP {resp.status_code} su GET /trattamenti/{id}",
                status_code=resp.status_code, detail=_safe_detail(resp))
        return resp.json()

    def create_trattamento(self, dto: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/trattamenti", json_body=dto)

    def update_trattamento(self, id: int, dto: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/trattamenti/{id}", json_body=dto)

    def delete_trattamento(self, id: int) -> Any:
        return self._request("DELETE", f"/trattamenti/{id}")

    def autorizza_trattamento(self, id: int) -> Any:
        return self._request("POST", f"/trattamenti/{id}/autorizza")

    def revoca_trattamento(self, id: int) -> Any:
        return self._request("POST", f"/trattamenti/{id}/revoca")

    # ---- OPERAZIONI (bundle multi-prodotto atomico) ------------------------

    def create_operazione(self, dto: Dict[str, Any]) -> Dict[str, Any]:
        """POST /operazioni — crea atomicamente N trattamenti che condividono
        lo stesso `operazione_id` (allocato dal server). Risposta:
        `OperazioneOut` con `operazione_id` int e `trattamenti[]` (ognuno
        con id server-side e operazione_id allocato)."""
        return self._request("POST", "/operazioni", json_body=dto)

    def delete_operazione(self, operazione_id: int) -> Any:
        """DELETE /operazioni/{operazione_id} — cancella tutti i trattamenti
        dell'operazione in una sola transazione server-side."""
        return self._request("DELETE", f"/operazioni/{operazione_id}")

    # ---- UTENTI ------------------------------------------------------------

    def get_utenti(self) -> list[dict]:
        """GET /utenti — lista utenti per popolare la dropdown 'Operatore'.

        Risposta: lista di `UtenteOut` con `display_name` computato server-side
        (Nome Cognome, con disambiguazione data_nascita per omonimi).
        """
        resp = self._request("GET", "/utenti")
        return resp if isinstance(resp, list) else []

    def create_utente(self, dto: Dict[str, Any]) -> Dict[str, Any]:
        """POST /utenti — crea un nuovo utente. Richiede ruolo ADMIN.

        Campi attesi nel dto: username, password, ruolo (opz, default BASIC),
        email/nome/cognome/data_nascita (tutti opzionali).
        """
        return self._request("POST", "/utenti", json_body=dto)

    # ---- MAGAZZINO ----------------------------------------------------------

    def sync_movimenti(self, since: Optional[str] = None,
                       prodotto_id: Optional[int] = None) -> Dict[str, Any]:
        params = {}
        if since:
            params["since"] = since
        if prodotto_id is not None:
            params["prodotto_id"] = prodotto_id
        return self._request("GET", "/magazzino/movimenti", params=params or None)

    def create_movimento(self, dto: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/magazzino/movimenti", json_body=dto)

    def update_movimento(self, id: int, dto: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/magazzino/movimenti/{id}", json_body=dto)

    def delete_movimento(self, id: int) -> Any:
        return self._request("DELETE", f"/magazzino/movimenti/{id}")

    def get_giacenza(self, prodotto_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/magazzino/giacenza/{prodotto_id}")

    def ricalcola_scarichi(self) -> Dict[str, Any]:
        """Triggera il rebuild GLOBALE degli scarichi automatici lato SERVER.

        Endpoint admin-only. Il backend wipa tutti gli scarichi con
        trattamento_id valorizzato e li ricrea da zero seguendo la catena
        WAREHOUSE_PRIORITY. I CARICHI/SCARICHI manuali (trattamento_id NULL)
        non vengono toccati.

        Da chiamare in sequenza con un sync_all successivo: dopo il ricalcolo
        server-side serve un pull /magazzino/movimenti per allineare il locale.
        """
        return self._request("POST", "/magazzino/ricalcola")

    # ---- AVVISI -------------------------------------------------------------

    def sync_avvisi(self, since: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", "/avvisi", params={"since": since} if since else None)

    # ---- HEALTH -------------------------------------------------------------

    def health(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Pinga il backend. Usato allo startup per capire se siamo online.

        Timeout breve (default da `CONFIG.quick_get_timeout_s`, 3s di base)
        per non bloccare lo splash se il server è lento/down. Bypassa il
        timeout di default di self._client (20s)."""
        if timeout is None:
            from config import CONFIG
            timeout = CONFIG.quick_get_timeout_s
        try:
            resp = self._client.get("/health", headers=self._headers(), timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise NetworkError(f"health timeout: {e}")
        if resp.status_code in (401, 403):
            raise NotAuthenticatedError("Sessione non valida", status_code=resp.status_code)
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code} su /health", status_code=resp.status_code)
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}


# ---- Helper -----------------------------------------------------------------

def _safe_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except Exception:
        return resp.text[:300]
