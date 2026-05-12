"""Logica di scarico automatico del magazzino.

Estratta da ui_trattamenti.py in modo da essere importabile anche da sync.py
senza trascinarsi PyQt6.

Esposto:
  - sincronizza_scarico(engine_or_conn, trattamento_id, operazione)
    Crea o ricrea gli SCARICHI in registro_magazzino per un trattamento,
    seguendo la catena di magazzini configurata in config.py
    (WAREHOUSE_PRIORITY + WAREHOUSE_ALIASES).

Chi lo chiama:
  - ui_trattamenti.py al salvataggio/modifica/eliminazione di un trattamento (UI locale)
  - sync.py: dopo aver scaricato un trattamento dal server (es. creato da mobile)

In tutti i casi gli SCARICHI esistenti per quel trattamento_id vengono
prima cancellati (idempotenza) e poi ricreati. Se l'operazione è DELETE
la pulizia è l'unico passo (il CASCADE FK lo farebbe comunque, ma essere
espliciti aiuta).
"""
from __future__ import annotations

from sqlalchemy import text


def _lookup_azienda_id(conn, nome: str):
    """Lookup case-insensitive di un'azienda per nome. Ritorna None se assente."""
    if not nome:
        return None
    row = conn.execute(text(
        "SELECT id FROM aziende WHERE LOWER(TRIM(nome)) = LOWER(TRIM(:n))"
    ), {"n": nome}).first()
    return row[0] if row else None


def _build_warehouse_chain(conn, src_az_id, src_az_nome):
    """Costruisce la catena di magazzini in ordine di priorità per uno scarico.

    Risolve eventuali alias (es. Deflorio Ciccopinto → Messina Alfio), poi
    aggiunge gli altri magazzini secondo `WAREHOUSE_PRIORITY` escludendo il
    primario.

    Ritorna lista di tuple [(azienda_id, azienda_nome_canonico), ...].
    Il primo elemento è sempre il magazzino primario.
    """
    from config import WAREHOUSE_PRIORITY, resolve_warehouse_alias

    canonical_name = resolve_warehouse_alias(src_az_nome or "")
    canonical_id = src_az_id
    if canonical_name.lower() != (src_az_nome or "").lower():
        looked = _lookup_azienda_id(conn, canonical_name)
        if looked is not None:
            canonical_id = looked

    chain = [(canonical_id, canonical_name)]
    seen_lower = {canonical_name.lower()}
    for nome in WAREHOUSE_PRIORITY:
        if nome.lower() in seen_lower:
            continue
        az_id = _lookup_azienda_id(conn, nome)
        if az_id is None:
            continue
        chain.append((az_id, nome))
        seen_lower.add(nome.lower())
    return chain


def _giacenze_per_prodotto(conn, prodotto_id):
    """Ritorna dict {azienda_id: saldo} per il prodotto. Saldo = CARICO - SCARICO."""
    rows = conn.execute(text("""
        SELECT azienda_id,
               SUM(CASE WHEN tipo_movimento = 'CARICO' THEN quantita ELSE -quantita END)
        FROM registro_magazzino
        WHERE prodotto_id = :pid
        GROUP BY azienda_id
    """), {"pid": prodotto_id}).fetchall()
    return {r[0]: float(r[1] or 0) for r in rows}


def _esegui_logica_scarico(conn, trattamento_id, operazione):
    # 1. Pulizia: elimina i record che hanno QUESTO trattamento_id
    conn.execute(text("DELETE FROM registro_magazzino WHERE trattamento_id = :tid"),
                 {"tid": trattamento_id})

    if operazione == "DELETE":
        return

    # 2. Raggruppa per (prodotto, azienda del tendone). Una trattamento con
    # tendoni di aziende diverse genera N gruppi indipendenti, ciascuno con
    # la sua catena di fallback.
    righe = conn.execute(text("""
        SELECT t.data_trattamento, t.prodotto_id,
               az.id AS azienda_id, az.nome AS azienda_nome,
               SUM(dt.quantita_sostanza) AS qta_richiesta
        FROM dettaglio_trattamenti dt
        JOIN tendoni ten ON ten.id = dt.tendone_id
        JOIN contrade c ON c.id = ten.contrada_id
        JOIN agri ag ON ag.id = c.agro_id
        JOIN aziende az ON az.id = ag.azienda_id
        JOIN trattamenti t ON t.id = dt.trattamento_id
        WHERE dt.trattamento_id = :tid
          AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)
        GROUP BY az.id, t.prodotto_id, t.data_trattamento, az.nome
    """), {"tid": trattamento_id}).fetchall()

    for data_t, prod_id, src_az_id, src_az_nome, qta_richiesta in righe:
        qta_rimanente = float(qta_richiesta or 0)
        if qta_rimanente <= 0:
            continue

        chain = _build_warehouse_chain(conn, src_az_id, src_az_nome)
        if not chain:
            continue

        giacenze = _giacenze_per_prodotto(conn, prod_id)

        # 3. Attingi dai magazzini in ordine di priorità
        for idx, (az_id, az_nome) in enumerate(chain):
            if qta_rimanente <= 0.0001:
                break
            saldo = giacenze.get(az_id, 0.0)
            if saldo <= 0:
                continue
            prelievo = min(qta_rimanente, saldo)
            if idx == 0:
                nota = f"Scarico automatico T#{trattamento_id}"
            else:
                # Magazzino di fallback. azienda_id = magazzino prestatore,
                # nota = azienda destinataria originale (tendone).
                nota = f"PRESTITO DA {src_az_nome} - T#{trattamento_id}"
            conn.execute(text("""
                INSERT INTO registro_magazzino (prodotto_id, trattamento_id, azienda_id, data_movimento, tipo_movimento, quantita, note)
                VALUES (:pid, :tid, :azid, :d, 'SCARICO', :qta, :nota)
            """), {
                "pid": prod_id, "tid": trattamento_id, "azid": az_id,
                "d": data_t, "qta": prelievo, "nota": nota,
            })
            qta_rimanente -= prelievo
            giacenze[az_id] = saldo - prelievo

        # 4. Forzatura in negativo se la catena non è bastata
        if qta_rimanente > 0.0001:
            primary_id, primary_nome = chain[0]
            nota = f"Scarico T#{trattamento_id} FORZATURA IN NEGATIVO ({primary_nome})"
            conn.execute(text("""
                INSERT INTO registro_magazzino (prodotto_id, trattamento_id, azienda_id, data_movimento, tipo_movimento, quantita, note)
                VALUES (:pid, :tid, :azid, :d, 'SCARICO', :qta, :nota)
            """), {
                "pid": prod_id, "tid": trattamento_id, "azid": primary_id,
                "d": data_t, "qta": qta_rimanente, "nota": nota,
            })


def sincronizza_scarico(engine_or_conn, trattamento_id, operazione="UPDATE"):
    """API pubblica: ricrea gli SCARICHI in registro_magazzino per il trattamento.

    Accetta sia un Engine SQLAlchemy che una Connection attiva. Se Engine,
    apre una propria transazione; se Connection, partecipa a quella esistente.
    """
    if hasattr(engine_or_conn, "execute"):
        _esegui_logica_scarico(engine_or_conn, trattamento_id, operazione)
    else:
        with engine_or_conn.begin() as conn:
            _esegui_logica_scarico(conn, trattamento_id, operazione)


def cleanup_magazzino_completo(engine) -> tuple[int, int]:
    """Pulisce e ricalcola da zero TUTTI gli scarichi automatici.

    1. Cancella le righe di `registro_magazzino` che sono SCARICHI AUTOMATICI,
       riconosciute con un OR di due criteri (robusto a stati inconsistenti):
         a) `trattamento_id IS NOT NULL`  (caso normale)
         b) `note` matcha uno dei pattern di sincronizza_scarico:
            - "Scarico automatico T#..."
            - "PRESTITO DA ... - T#..."
            - "Scarico T#... FORZATURA IN NEGATIVO ..."
       Le righe con `trattamento_id` NULL **e** note diversa (es. CARICO
       manuale) NON vengono toccate.
    2. Per ogni trattamento esistente, richiama `sincronizza_scarico` che
       reinserisce gli scarichi corretti seguendo la catena magazzini.

    Ritorna (n_scarichi_eliminati, n_trattamenti_ricalcolati).
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        try:
            # 1. Nuke degli scarichi automatici (anche orfani con tid=NULL).
            n_del = conn.execute(text("""
                DELETE FROM registro_magazzino
                WHERE trattamento_id IS NOT NULL
                   OR note LIKE 'Scarico automatico T#%'
                   OR note LIKE 'PRESTITO DA %'
                   OR note LIKE 'Scarico T#%'
            """)).rowcount or 0

            # 2. Ripulisci anche le pending_operations MOVIMENTO che sono
            # artefatti del vecchio trigger bug (gli scarichi automatici
            # finiti per errore in coda). Match per note dentro payload_json.
            conn.execute(text("""
                DELETE FROM pending_operations
                WHERE entity_type = 'MOVIMENTO'
                  AND (payload_json LIKE '%Scarico automatico T#%'
                       OR payload_json LIKE '%PRESTITO DA %'
                       OR payload_json LIKE '%FORZATURA IN NEGATIVO%')
            """))

            # 3. Ricalcola per ogni trattamento (in locale, senza enqueue
            # grazie al trigger aggiornato che ignora trattamento_id valorizzato)
            tids = [r[0] for r in conn.execute(
                text("SELECT id FROM trattamenti")
            ).fetchall()]
            for tid in tids:
                _esegui_logica_scarico(conn, tid, "UPDATE")
            return n_del, len(tids)
        finally:
            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))


def cleanup_magazzino_once_at_boot(engine) -> tuple[int, int] | None:
    """Wrapper one-off di `cleanup_magazzino_completo`: esegue una sola volta
    nella vita del DB locale, marcato in `_sync_flags.cleanup_magazzino_v4_done`.

    Ritorna (n_eliminati, n_ricalcolati) se ha eseguito, None se già fatto.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT value FROM _sync_flags WHERE key = 'cleanup_magazzino_v4_done'"
        )).first()
        if row and row[0]:
            return None

    result = cleanup_magazzino_completo(engine)

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT OR REPLACE INTO _sync_flags (key, value) "
            "VALUES ('cleanup_magazzino_v4_done', 1)"
        ))
    return result


def repull_movimenti_for_origine_once(engine) -> int | None:
    """One-off: cancella i record locali con `trattamento_id IS NOT NULL` e
    azzera sync_state.magazzino, così al prossimo pull arrivano dal server le
    versioni con il nuovo campo `azienda_id_origine` popolato.

    Da invocare dopo l'aggiornamento del backend che ha aggiunto
    `azienda_id_origine` a registro_magazzino. Marker:
    `_sync_flags.cleanup_origine_repull_done`.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT value FROM _sync_flags WHERE key = 'cleanup_origine_repull_done'"
        )).first()
        if row and row[0]:
            return None

    with engine.begin() as conn:
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        try:
            n_del = conn.execute(text(
                "DELETE FROM registro_magazzino WHERE trattamento_id IS NOT NULL"
            )).rowcount or 0
            conn.execute(text("DELETE FROM sync_state WHERE entity = 'magazzino'"))
            conn.execute(text(
                "INSERT OR REPLACE INTO _sync_flags (key, value) "
                "VALUES ('cleanup_origine_repull_done', 1)"
            ))
            return n_del
        finally:
            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))


def cleanup_for_server_authoritative_once(engine) -> int | None:
    """Migrazione one-off al modello Server-Authoritative.

    A partire dalla migrazione backend `migrazione_magazzino_v1`, il server è
    diventato la fonte di verità per gli auto-scarichi. Sul desktop bisogna:

      1. Cancellare TUTTI i record locali con `trattamento_id IS NOT NULL`
         (sono gli auto-scarichi calcolati localmente dal vecchio modello).
      2. Lasciare intatti i CARICHI/SCARICHI manuali (trattamento_id NULL).
      3. Pulire pending_operations MOVIMENTO orfane: la nuova logica filtra
         lato trigger (NEW.trattamento_id IS NULL), ma in coda potrebbero
         essercene di vecchie.
      4. Al primo pull dal server, gli auto-scarichi calcolati server-side
         verranno ri-popolati con trattamento_id valorizzato.

    Marcato in `_sync_flags.cleanup_server_authoritative_done`.
    Ritorna n_eliminati locali, o None se già fatto.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT value FROM _sync_flags WHERE key = 'cleanup_server_authoritative_done'"
        )).first()
        if row and row[0]:
            return None

    with engine.begin() as conn:
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        try:
            # 1. Cancella tutti gli scarichi automatici locali
            n_del = conn.execute(text(
                "DELETE FROM registro_magazzino WHERE trattamento_id IS NOT NULL"
            )).rowcount or 0

            # 2. Pulisci pending_operations MOVIMENTO che erano auto-scarichi
            conn.execute(text("""
                DELETE FROM pending_operations
                WHERE entity_type = 'MOVIMENTO'
                  AND (payload_json LIKE '%Scarico automatico T#%'
                       OR payload_json LIKE '%PRESTITO DA %'
                       OR payload_json LIKE '%FORZATURA IN NEGATIVO%')
            """))

            # 3. Forza re-download completo del registro_magazzino dal server:
            # azzero il sync_state per i movimenti così il prossimo pull
            # riceverà TUTTO il registro server-side (con trattamento_id).
            conn.execute(text(
                "DELETE FROM sync_state WHERE entity = 'magazzino'"
            ))

            # 4. Marca done
            conn.execute(text(
                "INSERT OR REPLACE INTO _sync_flags (key, value) "
                "VALUES ('cleanup_server_authoritative_done', 1)"
            ))
            return n_del
        finally:
            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
