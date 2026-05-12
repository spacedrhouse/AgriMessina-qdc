"""Mapping entity_type → tabella SQLite locale.

Le entity_type e operation_type viaggiano come stringhe in tutto il codebase
(SQL, JSON, comparazioni con '==') quindi è inutile farle StrEnum: un dict
basta. Usato da pending_uploader e dalla reconcile.
"""
from __future__ import annotations


ENTITY_TO_TABLE: dict[str, str] = {
    "AZIENDA": "aziende",
    "AGRO": "agri",
    "CONTRADA": "contrade",
    "TENDONE": "tendoni",
    "PRODOTTO": "prodotti",
    "TRATTAMENTO": "trattamenti",
    "MOVIMENTO": "registro_magazzino",
}
