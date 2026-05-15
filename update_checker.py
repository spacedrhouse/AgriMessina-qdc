"""Auto-update non intrusivo: chiede alla GitHub API se c'è una release più
recente di quella in esecuzione, e se sì mostra un dialog con link.

Il check NON blocca lo startup: gira in un QThread separato, e il dialog
viene emesso via signal Qt solo se l'utente ha effettivamente una versione
vecchia. Niente downgrade automatici, niente download silenziosi: l'utente
decide. Approccio "polite", più adatto a un'app interna.

Politica:
- Non disturbare l'utente in dev (versione "0.0.0-dev" → skip check).
- Niente popup se siamo offline o se l'API GitHub è down (silently skip).
- Niente popup se la versione installata == latest (skip).
- Cache breve in memoria: se l'utente apre/chiude l'app più volte, non
  ribussiamo a GitHub ogni volta.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)

# Repo da interrogare. Hardcoded in chiaro: il "valore" da proteggere è il
# token di accesso, non il nome del repo.
GITHUB_REPO = "spacedrhouse/AgriMessina-qdc"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Timeout breve: se GitHub è lento o irraggiungibile, NON vogliamo bloccare
# lo startup dell'app per qualche secondo di telemetria.
HTTP_TIMEOUT_SECONDS = 5.0


def _load_github_token() -> Optional[str]:
    """Ritorna il PAT GitHub se disponibile, altrimenti None.

    Sorgenti in ordine di precedenza:
      1. `_secrets.py` (iniettato dal workflow CI a build-time, gitignored).
      2. variabile d'ambiente `AGRIMESSINA_GH_TOKEN` (utile in dev).

    Per repo PUBBLICI il token non serve e le chiamate restano anonime.
    Per repo PRIVATI il token è obbligatorio: senza, GitHub risponde 404
    alle GET di `/releases/latest` e dei singoli asset, e l'auto-update
    tace (vedi `UpdateCheckWorker.run`).
    """
    try:
        from _secrets import GITHUB_TOKEN  # type: ignore[import-not-found]
        if GITHUB_TOKEN:
            return GITHUB_TOKEN.strip()
    except ImportError:
        pass
    env = os.environ.get("AGRIMESSINA_GH_TOKEN")
    return env.strip() if env else None


def _api_headers() -> dict[str, str]:
    """Headers comuni per le chiamate GitHub API. Auth solo se PAT presente."""
    headers = {"Accept": "application/vnd.github+json"}
    token = _load_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class UpdateInfo:
    latest_version: str
    download_url: str       # URL diretto dell'installer .exe (asset endpoint)
    release_url: str        # URL della pagina release (fallback se asset assente)
    release_notes: str      # Body markdown della release


def _normalize_version(v: str) -> tuple[int, ...]:
    """Converte '1.2.3' → (1, 2, 3) per confronto.
    Tollera prefisso 'v' e suffissi tipo '-dev', '-rc1'.
    """
    v = v.lstrip("vV").split("-")[0]
    parts: list[int] = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        if m:
            parts.append(int(m.group()))
    return tuple(parts) if parts else (0,)


def _is_newer(latest: str, current: str) -> bool:
    """True se `latest` è una versione successiva rispetto a `current`.

    Trattamento speciale: se current è '0.0.0-dev' (sviluppo locale) NON
    siamo interessati agli update — stiamo già lavorando alla prossima
    versione probabilmente.
    """
    if current.endswith("-dev") or current == "0.0.0":
        return False
    return _normalize_version(latest) > _normalize_version(current)


class UpdateCheckWorker(QThread):
    """QThread one-shot che fa il check e emette `update_available` solo se
    c'è davvero qualcosa di nuovo. Niente segnali in caso di errore: silenzio
    è la politica corretta (non vogliamo popup "errore aggiornamento" che
    annoiano l'utente).
    """

    update_available = pyqtSignal(object)  # UpdateInfo

    def __init__(self, current_version: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.current_version = current_version

    def run(self) -> None:
        try:
            resp = httpx.get(
                GITHUB_API_URL,
                timeout=HTTP_TIMEOUT_SECONDS,
                headers=_api_headers(),
            )
            if resp.status_code != 200:
                log.debug("[update_check] status %d, skip", resp.status_code)
                return
            data = resp.json()
        except Exception as e:
            log.debug("[update_check] errore rete: %s", e)
            return

        latest = str(data.get("tag_name") or "").strip()
        if not latest:
            return

        if not _is_newer(latest, self.current_version):
            log.info("[update_check] versione corrente %s ≥ latest %s, niente update",
                     self.current_version, latest)
            return

        # Cerca l'asset .exe (l'installer Inno Setup). Per repo privati il
        # `browser_download_url` da solo non basta (richiede comunque auth);
        # usiamo invece l'endpoint API per asset_id che funziona sia in
        # pubblico (redirect a CDN) sia in privato (auth header + redirect a
        # signed URL S3, dove httpx striperà l'Authorization su cross-origin).
        download_url = ""
        for asset in data.get("assets") or []:
            name = str(asset.get("name") or "")
            asset_id = asset.get("id")
            if asset_id and name.lower().endswith(".exe"):
                download_url = (
                    f"https://api.github.com/repos/{GITHUB_REPO}"
                    f"/releases/assets/{asset_id}"
                )
                break

        info = UpdateInfo(
            latest_version=latest.lstrip("vV"),
            download_url=download_url,
            release_url=str(data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"),
            release_notes=str(data.get("body") or ""),
        )
        log.info("[update_check] disponibile: %s", info.latest_version)
        self.update_available.emit(info)
