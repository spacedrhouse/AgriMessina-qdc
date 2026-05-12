"""Costruzione payload per le sync di trattamenti verso il backend.

Estratta da `ui_trattamenti.py` perché la logica è puramente DB → dict, senza
Qt, e viene riusata da SchedaOperazioni e dai dialog. Tenendola separata
evita di trascinarsi dentro un modulo da 2k+ righe per usare una funzione.
"""
from __future__ import annotations

from sqlalchemy import text


def build_trattamento_payload(engine_or_conn, trattamento_id):
    """Ritorna il dict serializzabile per `PUT /trattamenti/{id}`.

    Accetta sia un Engine (apre una connessione propria) sia una Connection
    esistente, per permettere al chiamante di riusare la transazione corrente.
    """
    if hasattr(engine_or_conn, "execute"):
        return _esegui_build_payload(engine_or_conn, trattamento_id)
    with engine_or_conn.connect() as conn:
        return _esegui_build_payload(conn, trattamento_id)


def _esegui_build_payload(conn, trattamento_id):
    t = conn.execute(text(
        "SELECT data_trattamento, prodotto_id, operatore, tipo_trattamento, "
        "modalita_fertilizzazione, is_autorizzato, data_inserimento, scaricato_magazzino "
        "FROM trattamenti WHERE id = :id"
    ), {"id": trattamento_id}).first()
    if not t:
        return None

    det_rows = conn.execute(text(
        "SELECT tendone_id, quantita_sostanza, botti, dose_ha, is_bilanciamento "
        "FROM dettaglio_trattamenti WHERE trattamento_id = :id"
    ), {"id": trattamento_id}).fetchall()

    data_str = t[0].isoformat() if hasattr(t[0], "isoformat") else str(t[0])[:10]
    # Se data_inserimento è nullo (es. trattamento creato da app mobile senza
    # quel campo popolato), usa data_trattamento come fallback per evitare
    # 422/NOT NULL costraint lato server.
    data_ins_raw = t[6]
    if data_ins_raw:
        data_ins_str = data_ins_raw.isoformat() if hasattr(data_ins_raw, "isoformat") else str(data_ins_raw)
    else:
        data_ins_str = data_str
    # Nota: `is_autorizzato` NON è incluso nel payload generico di UPDATE.
    # È gestito solo via /trattamenti/{id}/autorizza e /revoca, così un edit
    # del desktop non sovrascrive un'autorizzazione appena fatta da un altro
    # client (es. app mobile).
    return {
        "data_trattamento": data_str,
        "data_inserimento": data_ins_str,
        "scaricato_magazzino": str(t[7] or "0"),
        "prodotto_id": int(t[1]) if t[1] is not None else None,
        "operatore": t[2],
        "tipo_trattamento": t[3] or "Difesa",
        "modalita_fertilizzazione": t[4],
        "dettagli": [
            {
                "tendone_id": int(d[0]) if d[0] is not None else None,
                "quantita_sostanza": float(d[1] or 0),
                "botti": float(d[2]) if d[2] is not None else None,
                "dose_ha": float(d[3]) if d[3] is not None else None,
                "is_bilanciamento": int(d[4] or 0),
            } for d in det_rows
        ],
    }
