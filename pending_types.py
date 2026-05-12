"""Enums per i tipi di entità e di operazione usati in pending_operations.

StrEnum si serializza esattamente come una str: rimane compatibile con il
codice esistente che fa comparazioni "AZIENDA" == op.entity_type, con gli
insert SQL che memorizzano stringhe, e con i payload JSON dell'API.
"""
from __future__ import annotations
from enum import StrEnum


class EntityType(StrEnum):
    AZIENDA = "AZIENDA"
    AGRO = "AGRO"
    CONTRADA = "CONTRADA"
    TENDONE = "TENDONE"
    PRODOTTO = "PRODOTTO"
    TRATTAMENTO = "TRATTAMENTO"
    MOVIMENTO = "MOVIMENTO"


class OpType(StrEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    AUTORIZZA = "AUTORIZZA"
    REVOCA = "REVOCA"


# Mapping entity_type → tabella SQLite locale corrispondente.
# Usato da pending_uploader e da reconcile.
ENTITY_TO_TABLE: dict[str, str] = {
    EntityType.AZIENDA: "aziende",
    EntityType.AGRO: "agri",
    EntityType.CONTRADA: "contrade",
    EntityType.TENDONE: "tendoni",
    EntityType.PRODOTTO: "prodotti",
    EntityType.TRATTAMENTO: "trattamenti",
    EntityType.MOVIMENTO: "registro_magazzino",
}
