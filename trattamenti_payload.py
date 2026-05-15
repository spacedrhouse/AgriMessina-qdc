"""Costruzione payload per le sync di trattamenti verso il backend.

Estratta da `ui_trattamenti.py` perché la logica è puramente DB → dict, senza
Qt, e viene riusata da SchedaOperazioni e dai dialog. Tenendola separata
evita di trascinarsi dentro un modulo da 2k+ righe per usare una funzione.
"""
from __future__ import annotations

from sqlalchemy import text


def build_trattamento_payload(engine_or_conn, trattamento_id, *,
                              include_is_autorizzato: bool = False):
    """Ritorna il dict serializzabile per `PUT /trattamenti/{id}`.

    Accetta sia un Engine (apre una connessione propria) sia una Connection
    esistente, per permettere al chiamante di riusare la transazione corrente.

    `include_is_autorizzato`: di default False per gli UPDATE generici (vedi
    nota in `_esegui_build_payload`). Va passato True solo dal flusso di
    INSERT, dove il valore iniziale del flag fa parte dello stato che il
    server deve persistere (altrimenti il server defaulta a 0 e al primo
    reconcile sovrascrive l'is_autorizzato locale → bug dei figli
    bilanciamento sempre "in storico").
    """
    if hasattr(engine_or_conn, "execute"):
        return _esegui_build_payload(engine_or_conn, trattamento_id,
                                     include_is_autorizzato)
    with engine_or_conn.connect() as conn:
        return _esegui_build_payload(conn, trattamento_id,
                                     include_is_autorizzato)


def _esegui_build_payload(conn, trattamento_id, include_is_autorizzato: bool):
    # `operazione_id` è server-allocated alla creazione e non viene inviato
    # nel payload outbound: il server lo ignora comunque (vedi
    # routers/trattamenti_router.py:create_trattamento). Lo legge solo il
    # desktop via sync downstream per il display.
    t = conn.execute(text(
        "SELECT data_trattamento, prodotto_id, operatore, tipo_trattamento, "
        "modalita_fertilizzazione, is_autorizzato, data_inserimento, "
        "scaricato_magazzino "
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
    # Nota: `is_autorizzato` NON è incluso nel payload di UPDATE generico.
    # È gestito solo via /trattamenti/{id}/autorizza e /revoca, così un edit
    # del desktop non sovrascrive un'autorizzazione appena fatta da un altro
    # client (es. app mobile). In INSERT invece va incluso (flag dedicato):
    # i figli bilanciamento nascono is_autorizzato=1, senza propagarlo il
    # server li crea a 0 e il prossimo reconcile li riporta in Storico.
    payload = {
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
    if include_is_autorizzato:
        payload["is_autorizzato"] = int(t[5] or 0)
    return payload


def build_operazione_payload(engine_or_conn,
                              local_trattamento_ids: list[int]) -> dict | None:
    """Ritorna il dict per `POST /operazioni` + i local_ids per lo swap.

    Formato:
        {
            "operazione": {"trattamenti": [...]},  # operazione_id allocato dal server
            "local_ids": [id_locale_1, id_locale_2, ...]
        }

    Lato server (operazioni_router.create_operazione) alloca `operazione_id`
    e lo applica a tutti gli N trattamenti del bundle. Il `local_ids` resta
    nel payload locale e viene usato dal pending_uploader durante lo swap
    post-success (mappa N locali ↔ N server-side).
    """
    if not local_trattamento_ids:
        return None
    if hasattr(engine_or_conn, "execute"):
        return _esegui_build_operazione(engine_or_conn, local_trattamento_ids)
    with engine_or_conn.connect() as conn:
        return _esegui_build_operazione(conn, local_trattamento_ids)


def _esegui_build_operazione(conn, local_ids: list[int]) -> dict | None:
    import uuid as _uuid
    trattamenti = []
    for tid in local_ids:
        # `include_is_autorizzato=False` perché lato POST /operazioni il flag
        # is_autorizzato non è accettato (server-controlled, default 0).
        p = _esegui_build_payload(conn, tid, include_is_autorizzato=False)
        if p is None:
            continue
        trattamenti.append(p)
    if not trattamenti:
        return None
    return {
        "operazione": {
            # idempotency_key: UUID generato qui una volta sola. Se la POST
            # raggiunge il server ma la response si perde (timeout di rete),
            # al prossimo retry il server riconosce la chiave e ritorna
            # l'operazione_id già allocato invece di duplicare la creazione.
            "idempotency_key": str(_uuid.uuid4()),
            "trattamenti": trattamenti,
        },
        "local_ids": list(local_ids),
    }
