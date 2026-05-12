"""Configurazione centralizzata dell'app.

Tutti i magic number e tempi di polling vivono qui. Permette override da
variabili d'ambiente (utile per testing/staging).
"""
from __future__ import annotations
import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SyncConfig:
    # Polling intervals (ms).
    # NOTA: dalla versione SSE (vedi events_listener.py) il PULL è guidato da
    # push real-time del server, non più dal timer. `autosync_ms` resta basso
    # perché serve solo a controllare la coda pending_operations (un semplice
    # SELECT COUNT, microsecondi): se trova qualcosa fa upload, altrimenti
    # esce subito. Senza polling veloce, gli upload dell'utente (delete,
    # update) attenderebbero il timer per essere inviati al server.
    # `reconcile_ms` è lungo perché copre solo i casi in cui SSE non basta
    # (rete giù, fantasmi residui): è un meccanismo di safety-net.
    autosync_ms: int = 3000          # 3 s — check coda upload, no pull (SSE gestisce)
    reconcile_ms: int = 1_800_000    # 30 min — fallback full reconcile (phantom-delete)
    status_update_ms: int = 1000     # 1 s — UI status bar (counter), no chiamate rete

    # Backoff offline: quando la rete è giù, raddoppia l'intervallo fino al cap
    offline_backoff_initial_ms: int = 10000
    offline_backoff_max_ms: int = 60000

    # Timeouts (s)
    quick_get_timeout_s: float = 3.0    # Pre-check su click Modifica/Revisiona/Elimina
    default_api_timeout_s: float = 20.0 # Chiamate REST normali

    # Politica retry per pending_operations
    pending_retry_max: int = 5          # Dopo N retry, sposta in dead_letter

    # Cache freschezza reconcile (s): entro questo lasso il pre-check viene saltato
    reconcile_freshness_s: float = 10.0

    @classmethod
    def from_env(cls) -> "SyncConfig":
        """Legge override da variabili d'ambiente (prefisso QDC_)."""
        return cls(
            autosync_ms=_env_int("QDC_AUTOSYNC_MS", cls.autosync_ms),
            reconcile_ms=_env_int("QDC_RECONCILE_MS", cls.reconcile_ms),
            status_update_ms=_env_int("QDC_STATUS_UPDATE_MS", cls.status_update_ms),
            offline_backoff_initial_ms=_env_int("QDC_OFFLINE_BACKOFF_INITIAL_MS", cls.offline_backoff_initial_ms),
            offline_backoff_max_ms=_env_int("QDC_OFFLINE_BACKOFF_MAX_MS", cls.offline_backoff_max_ms),
            quick_get_timeout_s=_env_float("QDC_QUICK_GET_TIMEOUT_S", cls.quick_get_timeout_s),
            default_api_timeout_s=_env_float("QDC_DEFAULT_API_TIMEOUT_S", cls.default_api_timeout_s),
            pending_retry_max=_env_int("QDC_PENDING_RETRY_MAX", cls.pending_retry_max),
            reconcile_freshness_s=_env_float("QDC_RECONCILE_FRESHNESS_S", cls.reconcile_freshness_s),
        )


# Singleton globale: si carica una sola volta da .env (caricato in main.py prima
# di importare questo modulo non c'è garanzia; chi vuole sicurezza usa from_env())
CONFIG = SyncConfig()


# ============================================================================
# Configurazione magazzini multi-azienda
# ============================================================================
# Quando un trattamento viene salvato, il sistema deduce automaticamente la
# sostanza dal magazzino dell'azienda a cui appartiene il tendone. Se quel
# magazzino non ha abbastanza scorta, il sistema attinge dagli altri magazzini
# nell'ordine di priorità definito qui sotto.
#
# Priorità globale (escluso il magazzino primario, che è sempre il primo):
#   1. Messina Alfio
#   2. Agrimessina
#   3. La Gazzella
#
# Aliases: alcune aziende non hanno un magazzino proprio e usano quello di
# un'altra. Es: i tendoni di "Deflorio Ciccopinto" prelevano dal magazzino di
# "Messina Alfio". Il match è case-insensitive.

WAREHOUSE_PRIORITY: tuple[str, ...] = (
    "Messina Alfio",
    "Agrimessina",
    "La Gazzella",
)

WAREHOUSE_ALIASES: dict[str, str] = {
    "Deflorio Ciccopinto": "Messina Alfio",
}


def resolve_warehouse_alias(nome: str) -> str:
    """Risolve eventuali alias case-insensitive. Se nome non è un alias noto
    lo ritorna invariato."""
    if not nome:
        return nome
    nome_norm = nome.strip().lower()
    for alias, canonical in WAREHOUSE_ALIASES.items():
        if alias.lower() == nome_norm:
            return canonical
    return nome

