"""Logica di scarico automatico del magazzino — modello a due registri.

DUE REGISTRI (tabelle separate nel DB):
  • `registro_magazzino`           = REALE (Storico, is_autorizzato=0)
                                      Sincronizzato col backend.
  • `registro_magazzino_fittizio`  = FITTIZIO (Revisionati, is_autorizzato=1)
                                      Local-only, mai sincronizzato.

REGOLE DI SCRITTURA:
  • INSERT/UPDATE trattamento `is_aut=0`:
      → scarico nel REALE (qta = SUM dei dt is_bilanciamento=0, qta originale)
  • AUTORIZZA `0→1`:
      → reale invariato (congelato)
      → scarico nel FITTIZIO (qta = SUM di TUTTI i dt, incluso bilanciamenti)
  • UPDATE trattamento `is_aut=1` o bilanciamento:
      → reale invariato (congelato)
      → fittizio aggiornato
  • DELETE trattamento `is_aut=0`: cancella riga reale
  • DELETE trattamento `is_aut=1`: cancella riga fittizio (reale resta)
  • REVOCA `1→0`: cancella riga fittizio

CARICHI MANUALI: solo nel REALE (UI fittizio è read-only).

CATENA DI SCARICO (warehouse fallback, prestiti tra aziende):
  Per ogni gruppo (prodotto, azienda del tendone), prova prima il magazzino
  primario dell'azienda; se saldo insufficiente passa ai fallback secondo
  config.WAREHOUSE_PRIORITY annotando "PRESTITO DA …". Se nemmeno la catena
  basta, forza in negativo sul primario con la stessa nota dello scarico
  ordinario ("Scarico automatico T#…").
"""
from __future__ import annotations

from sqlalchemy import text

from app_logging import get_logger

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
# Helper: catena warehouse e giacenze
# ────────────────────────────────────────────────────────────────────


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
    aggiunge gli altri magazzini secondo WAREHOUSE_PRIORITY escludendo il
    primario. Ritorna [(azienda_id, nome_canonico), ...] con primario in posizione 0.
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


def _giacenze_per_prodotto(conn, prodotto_id, table):
    """Ritorna dict {azienda_id: saldo} per il prodotto, dal `table` indicato.

    Saldo = SUM(CARICO) - SUM(SCARICO). Usato come pool da cui attingere
    durante la catena di scarichi: il primo magazzino paga finché ha saldo,
    poi si passa al fallback.
    """
    rows = conn.execute(text(f"""
        SELECT azienda_id,
               SUM(CASE WHEN tipo_movimento = 'CARICO' THEN quantita ELSE -quantita END)
        FROM {table}
        WHERE prodotto_id = :pid
        GROUP BY azienda_id
    """), {"pid": prodotto_id}).fetchall()
    return {r[0]: float(r[1] or 0) for r in rows}


# ────────────────────────────────────────────────────────────────────
# Core: ricalcolo SCARICO per un trattamento in una tabella specifica
# ────────────────────────────────────────────────────────────────────


def _ricalcola_scarico(conn, trattamento_id, table, includi_bilanciamenti):
    """Riscrive le righe SCARICO automatiche per `trattamento_id` nel `table`.

    Cancella le righe esistenti (con quel trattamento_id) e le ricrea dal
    SUM dei dt, raggruppando per (prodotto, azienda del tendone). Per ogni
    gruppo applica la catena di magazzini con fallback e forzatura negativa.

    `includi_bilanciamenti`:
      - False → considera SOLO dt is_bilanciamento=0 (qta originale del
        trattamento, snapshot pre-bilanciamenti). Usato per il REALE.
      - True  → considera TUTTI i dt incluso bilanciamenti. Usato per il
        FITTIZIO (qta corrente con tutte le modifiche).
    """
    # 1. Pulizia righe SCARICO esistenti del trattamento.
    conn.execute(text(f"DELETE FROM {table} WHERE trattamento_id = :tid"),
                 {"tid": trattamento_id})

    filtro_bil = "" if includi_bilanciamenti else \
        "AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)"

    # 2. Raggruppa per (azienda_tendone, prodotto). Un trattamento con
    # tendoni di aziende diverse genera N gruppi indipendenti.
    righe = conn.execute(text(f"""
        SELECT t.data_trattamento, t.prodotto_id,
               az.id AS azienda_id, az.nome AS azienda_nome,
               SUM(dt.quantita_sostanza) AS qta_richiesta
        FROM dettaglio_trattamenti dt
        JOIN tendoni ten ON ten.id = dt.tendone_id
        JOIN contrade c ON c.id = ten.contrada_id
        JOIN agri ag ON ag.id = c.agro_id
        JOIN aziende az ON az.id = ag.azienda_id
        JOIN trattamenti t ON t.id = dt.trattamento_id
        WHERE dt.trattamento_id = :tid {filtro_bil}
        GROUP BY az.id, t.prodotto_id, t.data_trattamento, az.nome
    """), {"tid": trattamento_id}).fetchall()

    for data_t, prod_id, src_az_id, src_az_nome, qta_richiesta in righe:
        qta_rimanente = float(qta_richiesta or 0)
        if qta_rimanente <= 0:
            continue

        chain = _build_warehouse_chain(conn, src_az_id, src_az_nome)
        if not chain:
            continue

        giacenze = _giacenze_per_prodotto(conn, prod_id, table)

        # 3. Attingi dai magazzini in ordine di priorità.
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
                nota = f"PRESTITO DA {src_az_nome} - T#{trattamento_id}"
            conn.execute(text(f"""
                INSERT INTO {table} (prodotto_id, trattamento_id, azienda_id,
                    azienda_id_origine, data_movimento, tipo_movimento, quantita, note)
                VALUES (:pid, :tid, :azid, :azidori, :d, 'SCARICO', :qta, :nota)
            """), {
                "pid": prod_id, "tid": trattamento_id, "azid": az_id,
                "azidori": src_az_id, "d": data_t, "qta": prelievo, "nota": nota,
            })
            qta_rimanente -= prelievo
            giacenze[az_id] = saldo - prelievo

        # 4. Forzatura in negativo se la catena non è bastata. La nota usa
        # lo stesso testo del caso "giacenza disponibile" sul primario:
        # all'utente non interessa distinguere il forzato in negativo,
        # vede sempre uno scarico automatico associato al trattamento.
        if qta_rimanente > 0.0001:
            primary_id, _ = chain[0]
            nota = f"Scarico automatico T#{trattamento_id}"
            conn.execute(text(f"""
                INSERT INTO {table} (prodotto_id, trattamento_id, azienda_id,
                    azienda_id_origine, data_movimento, tipo_movimento, quantita, note)
                VALUES (:pid, :tid, :azid, :azidori, :d, 'SCARICO', :qta, :nota)
            """), {
                "pid": prod_id, "tid": trattamento_id, "azid": primary_id,
                "azidori": src_az_id, "d": data_t, "qta": qta_rimanente, "nota": nota,
            })


# ────────────────────────────────────────────────────────────────────
# API pubblica (chiamata dai dialog di salvataggio)
# ────────────────────────────────────────────────────────────────────


def scarica_reale(engine_or_conn, trattamento_id):
    """Aggiorna le righe SCARICO del trattamento nel registro REALE.

    Da chiamare a INSERT/UPDATE di un trattamento `is_autorizzato=0`.
    Il calcolo usa SOLO i dt non-bilanciamento (qta originale). Una volta
    che il trattamento viene REVISIONATO, NON va più chiamata: il reale
    è congelato.
    """
    def _run(conn):
        log.info("scarica_reale tid=%s", trattamento_id)
        _ricalcola_scarico(conn, trattamento_id, "registro_magazzino",
                           includi_bilanciamenti=False)

    if hasattr(engine_or_conn, "execute"):
        _run(engine_or_conn)
    else:
        with engine_or_conn.begin() as conn:
            _run(conn)


def scarica_fittizio(engine_or_conn, trattamento_id):
    """Aggiorna le righe SCARICO del trattamento nel registro FITTIZIO.

    Da chiamare:
      - alla REVISIONE (passaggio is_aut 0→1): scarico iniziale del fittizio.
      - dopo ogni bilanciamento del trattamento revisionato.
      - a ogni UPDATE di un trattamento `is_autorizzato=1`.

    Il calcolo include TUTTI i dt (anche bilanciamenti): è lo stato corrente.
    """
    def _run(conn):
        log.info("scarica_fittizio tid=%s", trattamento_id)
        _ricalcola_scarico(conn, trattamento_id, "registro_magazzino_fittizio",
                           includi_bilanciamenti=True)

    if hasattr(engine_or_conn, "execute"):
        _run(engine_or_conn)
    else:
        with engine_or_conn.begin() as conn:
            _run(conn)


def cancella_scarico_reale(engine_or_conn, trattamento_id):
    """Rimuove le righe SCARICO del trattamento dal registro REALE.

    Da chiamare alla DELETE di un trattamento `is_autorizzato=0`. Per i
    trattamenti revisionati il reale è congelato → non va chiamata.
    """
    def _run(conn):
        n = conn.execute(text(
            "DELETE FROM registro_magazzino WHERE trattamento_id = :tid"
        ), {"tid": trattamento_id}).rowcount or 0
        log.info("cancella_scarico_reale tid=%s righe=%d", trattamento_id, n)

    if hasattr(engine_or_conn, "execute"):
        _run(engine_or_conn)
    else:
        with engine_or_conn.begin() as conn:
            _run(conn)


def cancella_scarico_fittizio(engine_or_conn, trattamento_id):
    """Rimuove le righe SCARICO del trattamento dal registro FITTIZIO.

    Da chiamare:
      - alla DELETE di un trattamento `is_autorizzato=1` (il prodotto torna
        nella giacenza fittizia, il reale resta come snapshot storico).
      - alla REVOCA di una revisione (`is_aut 1→0`).
    """
    def _run(conn):
        n = conn.execute(text(
            "DELETE FROM registro_magazzino_fittizio WHERE trattamento_id = :tid"
        ), {"tid": trattamento_id}).rowcount or 0
        log.info("cancella_scarico_fittizio tid=%s righe=%d", trattamento_id, n)

    if hasattr(engine_or_conn, "execute"):
        _run(engine_or_conn)
    else:
        with engine_or_conn.begin() as conn:
            _run(conn)


# ────────────────────────────────────────────────────────────────────
# Migrazione one-shot
# ────────────────────────────────────────────────────────────────────


def bootstrap_due_registri_once(engine) -> dict | None:
    """Migrazione one-shot al modello a due registri.

    Per ogni trattamento esistente nel DB:
      - is_autorizzato=0 → ricalcola scarico nel REALE.
      - is_autorizzato=1 → ricalcola scarico in ENTRAMBI (reale come
        snapshot dell'iniziale, fittizio con qta corrente).

    Esegue una sola volta nella vita del DB locale (marker `_sync_flags`).
    Ritorna {'reale': N, 'fittizio': M} con i conteggi se eseguito, None
    se già fatto.
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT value FROM _sync_flags WHERE key = 'bootstrap_due_registri_v2_done'"
        )).first()
        if row and row[0]:
            return None

    with engine.begin() as conn:
        # Disattiva pending durante il rebuild
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        try:
            # Wipe scarichi automatici esistenti in entrambi i registri.
            pattern = """
                trattamento_id IS NOT NULL
                OR note LIKE 'Scarico automatico T#%'
                OR note LIKE 'PRESTITO DA %'
                OR note LIKE 'Scarico T#%'
            """
            conn.execute(text(f"DELETE FROM registro_magazzino WHERE {pattern}"))
            conn.execute(text(f"DELETE FROM registro_magazzino_fittizio WHERE {pattern}"))

            # Per ogni trattamento, ricalcola le tabelle dovute.
            trattamenti = conn.execute(text(
                "SELECT id, is_autorizzato FROM trattamenti"
            )).fetchall()
            n_reale = 0
            n_fittizio = 0
            for tid, is_aut in trattamenti:
                _ricalcola_scarico(conn, tid, "registro_magazzino",
                                   includi_bilanciamenti=False)
                n_reale += 1
                if (is_aut or 0) == 1:
                    _ricalcola_scarico(conn, tid, "registro_magazzino_fittizio",
                                       includi_bilanciamenti=True)
                    n_fittizio += 1

            conn.execute(text(
                "INSERT OR REPLACE INTO _sync_flags (key, value) "
                "VALUES ('bootstrap_due_registri_v2_done', 1)"
            ))
            return {"reale": n_reale, "fittizio": n_fittizio}
        finally:
            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))


def ricalcola_tutti(engine) -> dict:
    """Rebuild manuale di TUTTI gli scarichi automatici (entrambi i registri).

    Usato dal bottone "🔧 Ricalcola scarichi" nel pannello magazzino.
    A differenza del bootstrap one-shot, è idempotente e rieseguibile.

    NOTA: per i trattamenti `is_aut=1` ricostruisce sia il reale (snapshot
    qta originale) che il fittizio (qta corrente). Se il reale era "congelato"
    su uno stato diverso post-revisione (es. dopo bilanciamenti pre-revisione),
    quel valore va PERSO — il rebuild ricostruisce sempre dalla qta originale.
    È una scelta accettabile: il caso "bilanciare un trattamento pre-revisione"
    non esiste perché il dialog bilanciamento è abilitato solo per Revisionati.
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        try:
            pattern = """
                trattamento_id IS NOT NULL
                OR note LIKE 'Scarico automatico T#%'
                OR note LIKE 'PRESTITO DA %'
                OR note LIKE 'Scarico T#%'
            """
            conn.execute(text(f"DELETE FROM registro_magazzino WHERE {pattern}"))
            conn.execute(text(f"DELETE FROM registro_magazzino_fittizio WHERE {pattern}"))

            trattamenti = conn.execute(text(
                "SELECT id, is_autorizzato FROM trattamenti"
            )).fetchall()
            n_reale = 0
            n_fittizio = 0
            for tid, is_aut in trattamenti:
                _ricalcola_scarico(conn, tid, "registro_magazzino",
                                   includi_bilanciamenti=False)
                n_reale += 1
                if (is_aut or 0) == 1:
                    _ricalcola_scarico(conn, tid, "registro_magazzino_fittizio",
                                       includi_bilanciamenti=True)
                    n_fittizio += 1
            return {"reale": n_reale, "fittizio": n_fittizio}
        finally:
            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
