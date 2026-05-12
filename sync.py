"""SyncManager per il client desktop.

Stessa logica del SyncManager Android:
- Per ogni entità chiede al server `GET /<entity>?since=<server_time_ultima_sync>`.
- Riceve un SyncResponse con `items`, `deleted_ids`, `server_time`.
- Applica gli upsert e le delete sul SQLite locale.
- Salva il `server_time` per usarlo come `since` la prossima volta.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from api_client import ApiClient, ApiError, NetworkError, NotAuthenticatedError
from local_db import get_sync_since, set_sync_since, set_downloading
from app_logging import get_logger

log = get_logger(__name__)


# Etichette stabili (= chiavi della tabella sync_state)
E_AZIENDE = "aziende"
E_AGRI = "agri"
E_CONTRADE = "contrade"
E_TENDONI = "tendoni"
E_PRODOTTI = "prodotti"
E_TRATTAMENTI = "trattamenti"
E_MAGAZZINO = "magazzino"
E_AVVISI = "avvisi"


from dataclasses import dataclass


@dataclass(frozen=True)
class SyncResult:
    """Esito sintetico per la UI."""
    ok: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _upsert_aziende(engine: Engine, items: list) -> None:
    if not items:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO aziende (id, nome) VALUES (:id, :nome)
            ON CONFLICT(id) DO UPDATE SET nome=excluded.nome
        """), items)


def _upsert_agri(engine: Engine, items: list) -> None:
    if not items:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO agri (id, azienda_id, nome) VALUES (:id, :azienda_id, :nome)
            ON CONFLICT(id) DO UPDATE SET
                azienda_id=excluded.azienda_id, nome=excluded.nome
        """), items)


def _upsert_contrade(engine: Engine, items: list) -> None:
    if not items:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO contrade (id, agro_id, nome) VALUES (:id, :agro_id, :nome)
            ON CONFLICT(id) DO UPDATE SET
                agro_id=excluded.agro_id, nome=excluded.nome
        """), items)


def _upsert_tendoni(engine: Engine, items: list) -> None:
    if not items:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO tendoni (id, contrada_id, codice, ettari)
            VALUES (:id, :contrada_id, :codice, :ettari)
            ON CONFLICT(id) DO UPDATE SET
                contrada_id=excluded.contrada_id,
                codice=excluded.codice, ettari=excluded.ettari
        """), items)


def _upsert_prodotti(engine: Engine, items: list) -> None:
    if not items:
        return
    with engine.begin() as conn:
        # ON CONFLICT aggiorna senza cancellare l'ID: i trattamenti collegati
        # non vengono toccati. SQLAlchemy esegue executemany batchando.
        conn.execute(text("""
            INSERT INTO prodotti (
                id, nome_prodotto, categoria, numero_registrazione,
                sostanza_attiva, bio_convenzionale, avversita,
                titolo_n, titolo_p, titolo_k,
                phi_giorni, trattamenti_max, intervallo_min_tratt,
                unita_misura, min_sostanza, max_sostanza, qta_acqua, blacklist
            ) VALUES (
                :id, :nome_prodotto, :categoria, :numero_registrazione,
                :sostanza_attiva, :bio_convenzionale, :avversita,
                :titolo_n, :titolo_p, :titolo_k,
                :phi_giorni, :trattamenti_max, :intervallo_min_tratt,
                :unita_misura, :min_sostanza, :max_sostanza, :qta_acqua, :blacklist
            )
            ON CONFLICT(id) DO UPDATE SET
                nome_prodotto=excluded.nome_prodotto,
                categoria=excluded.categoria,
                numero_registrazione=excluded.numero_registrazione,
                sostanza_attiva=excluded.sostanza_attiva,
                bio_convenzionale=excluded.bio_convenzionale,
                avversita=excluded.avversita,
                titolo_n=excluded.titolo_n,
                titolo_p=excluded.titolo_p,
                titolo_k=excluded.titolo_k,
                phi_giorni=excluded.phi_giorni,
                trattamenti_max=excluded.trattamenti_max,
                intervallo_min_tratt=excluded.intervallo_min_tratt,
                unita_misura=excluded.unita_misura,
                min_sostanza=excluded.min_sostanza,
                max_sostanza=excluded.max_sostanza,
                qta_acqua=excluded.qta_acqua,
                blacklist=excluded.blacklist
        """), items)


def _upsert_trattamenti(engine: Engine, items: list) -> None:
    """Upsert dei trattamenti scaricati dal server.

    Non chiama più `sincronizza_scarico` localmente: il server è ora
    autoritativo, calcola gli scarichi server-side e li espone via
    `/magazzino/movimenti`. Il client li pulla via `_upsert_movimenti`.

    NOTA CASCADE: la testata viene aggiornata con `INSERT ... ON CONFLICT(id)
    DO UPDATE`, NON con DELETE+INSERT. Motivo: in `local_db.py` lo schema
    `registro_magazzino` ha `trattamento_id REFERENCES trattamenti(id)
    ON DELETE CASCADE`. Un DELETE+INSERT della testata cancellerebbe a
    cascata tutti gli scarichi locali legati a quel trattamento, e il
    successivo pull movimenti non avviene in `pull_trattamenti` — gli
    scarichi resterebbero invisibili fino al prossimo `sync_all`.
    L'UPSERT vero (ON CONFLICT) lascia intatte le righe figlie.

    I DETTAGLI invece restano DELETE+INSERT: la loro FK non ha tabelle
    figlie con CASCADE (registro_magazzino guarda al trattamento, non
    al dettaglio), quindi è safe.
    """
    if not items:
        return

    with engine.begin() as conn:
        for t in items:
            tid = t["id"]
            try:
                # Savepoint: se questo record esplode, annulla solo lui.
                with conn.begin_nested():
                    # Testata: UPSERT vero, niente CASCADE su registro_magazzino.
                    conn.execute(text("""
                        INSERT INTO trattamenti (id, data_trattamento, data_inserimento, prodotto_id,
                            operatore, tipo_trattamento, modalita_fertilizzazione,
                            scaricato_magazzino, is_autorizzato)
                        VALUES (:id, :data_trattamento, :data_inserimento, :prodotto_id,
                            :operatore, :tipo_trattamento, :modalita_fertilizzazione,
                            :scaricato_magazzino, :is_autorizzato)
                        ON CONFLICT(id) DO UPDATE SET
                            data_trattamento=excluded.data_trattamento,
                            data_inserimento=excluded.data_inserimento,
                            prodotto_id=excluded.prodotto_id,
                            operatore=excluded.operatore,
                            tipo_trattamento=excluded.tipo_trattamento,
                            modalita_fertilizzazione=excluded.modalita_fertilizzazione,
                            scaricato_magazzino=excluded.scaricato_magazzino,
                            is_autorizzato=excluded.is_autorizzato
                    """), {
                        "id": tid,
                        "data_trattamento": t.get("data_trattamento"),
                        "data_inserimento": t.get("data_inserimento"),
                        "prodotto_id": t.get("prodotto_id"),
                        "operatore": t.get("operatore"),
                        "tipo_trattamento": t.get("tipo_trattamento"),
                        "modalita_fertilizzazione": t.get("modalita_fertilizzazione"),
                        "scaricato_magazzino": t.get("scaricato_magazzino", "0"),
                        "is_autorizzato": t.get("is_autorizzato", 0),
                    })
                    # Dettagli: ricreati ad ogni upsert (sostituzione completa).
                    # registro_magazzino NON ha FK verso dettaglio_trattamenti,
                    # quindi questo DELETE è safe.
                    conn.execute(text(
                        "DELETE FROM dettaglio_trattamenti WHERE trattamento_id = :tid"
                    ), {"tid": tid})
                    dettagli = t.get("dettagli") or []
                    if dettagli:
                        conn.execute(text("""
                            INSERT INTO dettaglio_trattamenti
                                (trattamento_id, tendone_id, quantita_sostanza, botti, dose_ha, is_bilanciamento)
                            VALUES (:tid, :tendone_id, :quantita_sostanza, :botti, :dose_ha, :is_bilanciamento)
                        """), [
                            {
                                "tid": tid,
                                "tendone_id": d.get("tendone_id"),
                                "quantita_sostanza": d.get("quantita_sostanza", 0),
                                "botti": d.get("botti"),
                                "dose_ha": d.get("dose_ha"),
                                "is_bilanciamento": d.get("is_bilanciamento", 0),
                            }
                            for d in dettagli
                        ])
            except IntegrityError as e:
                log.warning("Scartato Trattamento corrotto dal server (ID %s): %s", tid, e)


def _upsert_movimenti(engine: Engine, items: list) -> None:
    """Upsert dei movimenti magazzino scaricati dal server.

    Include `trattamento_id` (valorizzato per gli SCARICHI automatici, NULL
    per i CARICHI/SCARICHI manuali). Il server è ora autoritativo: calcola
    gli auto-scarichi via `magazzino_calculator.sincronizza_scarico` ad ogni
    create/update/delete di trattamento, e li espone via /magazzino/movimenti.
    """
    if not items:
        return

    # UPSERT via ON CONFLICT(id): evita il vecchio DELETE+INSERT (due statement).
    # Manteniamo il savepoint per-record per scartare singoli record corrotti
    # senza far cadere l'intero lotto (es. FK verso un trattamento non ancora
    # arrivato).
    upsert_sql = text("""
        INSERT INTO registro_magazzino
            (id, prodotto_id, trattamento_id, azienda_id, azienda_id_origine,
             data_movimento, tipo_movimento, quantita, n_ddt, fornitore, note)
        VALUES (:id, :prodotto_id, :trattamento_id, :azienda_id, :azienda_id_origine,
             :data_movimento, :tipo_movimento, :quantita, :n_ddt, :fornitore, :note)
        ON CONFLICT(id) DO UPDATE SET
            prodotto_id=excluded.prodotto_id,
            trattamento_id=excluded.trattamento_id,
            azienda_id=excluded.azienda_id,
            azienda_id_origine=excluded.azienda_id_origine,
            data_movimento=excluded.data_movimento,
            tipo_movimento=excluded.tipo_movimento,
            quantita=excluded.quantita,
            n_ddt=excluded.n_ddt,
            fornitore=excluded.fornitore,
            note=excluded.note
    """)
    with engine.begin() as conn:
        for m in items:
            try:
                with conn.begin_nested():
                    conn.execute(upsert_sql, {
                        "id": m["id"],
                        "prodotto_id": m.get("prodotto_id"),
                        "trattamento_id": m.get("trattamento_id"),
                        "azienda_id": m.get("azienda_id"),
                        "azienda_id_origine": m.get("azienda_id_origine"),
                        "data_movimento": m.get("data_movimento"),
                        "tipo_movimento": m.get("tipo_movimento"),
                        "quantita": m.get("quantita"),
                        "n_ddt": m.get("n_ddt"),
                        "fornitore": m.get("fornitore"),
                        "note": m.get("note"),
                    })
            except IntegrityError as e:
                log.warning("Scartato Movimento corrotto dal server (ID %s): %s", m.get('id'), e)


def _replace_avvisi(engine: Engine, items: list) -> None:
    """Full-replace della tabella avvisi_trattamenti.

    Strategia coerente con quella del backend: gli avvisi vengono ricalcolati
    interamente lato server, quindi qui cancelliamo tutto e re-inseriamo.

    Ogni INSERT è isolato da un savepoint: se un avviso referenzia un
    trattamento che non esiste localmente (FK violation), viene scartato
    senza far fallire l'intero batch.
    """
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM avvisi_trattamenti"))
        for a in items:
            try:
                with conn.begin_nested():
                    conn.execute(text("""
                        INSERT INTO avvisi_trattamenti (id, trattamento_id, testo)
                        VALUES (:id, :trattamento_id, :testo)
                    """), {
                        "id": a.get("id"),
                        "trattamento_id": a["trattamento_id"],
                        "testo": a["testo"],
                    })
            except IntegrityError:
                log.warning("Avviso scartato (trattamento_id %s non locale)", a.get('trattamento_id'))


def _delete_ids(engine: Engine, table: str, ids: list) -> None:
    if not ids:
        return
    # int() coercion protegge da SQL injection sui valori; la table arriva da
    # callsite hard-coded. Una sola DELETE batchata invece di N statement.
    ids_list = ",".join(str(int(i)) for i in ids)
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table} WHERE id IN ({ids_list})"))


def _protected_ids_for(engine: Engine, entity_type: str) -> set:
    """Ritorna gli ID con qualsiasi pending op per la singola entity_type.

    Convenienza per i callsite che lavorano su una sola entità (es. pull
    incrementale). Per reconcile, che le scorre tutte, usare invece
    `_all_protected_ids` (una sola query)."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT entity_id FROM pending_operations WHERE entity_type = :e"
        ), {"e": entity_type}).fetchall()
    return {r[0] for r in rows if r[0] is not None}


def _all_protected_ids(engine: Engine) -> dict:
    """Ritorna un dict entity_type → set[entity_id] con tutte le pending op.

    Una sola query invece di N per entità: usato dal reconcile completo che
    altrimenti farebbe 7 SELECT consecutivi su pending_operations."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT entity_type, entity_id FROM pending_operations "
            "WHERE entity_id IS NOT NULL"
        )).fetchall()
    by_entity: dict = {}
    for et, eid in rows:
        by_entity.setdefault(et, set()).add(eid)
    return by_entity


def _reconcile_entity(engine: Engine, table: str, entity_type: str,
                      server_items: list, protected: set, notifier=None) -> int:
    """Cancella localmente i record assenti dal server. Ritorna n. di righe rimosse.

    `protected` è il set degli ID con qualsiasi pending op (precomputato dal
    chiamante per coerenza con il filtraggio degli upsert).
    """
    server_ids = {it["id"] for it in server_items if it.get("id") is not None}

    with engine.begin() as conn:
        local_ids = {r[0] for r in conn.execute(text(f"SELECT id FROM {table}")).fetchall()}
        phantom_ids = local_ids - server_ids - protected
        if not phantom_ids:
            return 0
        # int() coercion protegge da SQL injection.
        ids_list = ",".join(str(int(i)) for i in phantom_ids)
        conn.execute(text(f"DELETE FROM {table} WHERE id IN ({ids_list})"))
        if notifier is not None:
            notifier.on_ghosts_removed(entity_type, len(phantom_ids))
        return len(phantom_ids)


def _cleanup_orfani_dettagli(engine: Engine) -> int:
    """Rimuove dettaglio_trattamenti orfani (senza testata). Difensivo."""
    with engine.begin() as conn:
        result = conn.execute(text(
            "DELETE FROM dettaglio_trattamenti "
            "WHERE trattamento_id NOT IN (SELECT id FROM trattamenti)"
        ))
        return result.rowcount or 0


def reconcile_with_server(api: ApiClient, engine: Engine, notifier=None) -> SyncResult:
    """Allinea completamente il DB locale con lo stato corrente sul server:
      - Cancella record locali che non esistono più sul server (fantasmi).
      - Aggiorna record locali con la versione corrente del server (upsert).
      - Pulisce dettaglio_trattamenti orfani.

    Note:
      - I trigger di accodamento vengono disattivati durante l'operazione.
      - Gli ID con INSERT/UPDATE pendente sono preservati: rappresentano
        scritture locali non ancora propagate al server.
      - Sostituisce il pulsante manuale "Sincronizza col Cloud": è l'unica
        fonte di pull periodico dei dati server-side, quindi deve catturare
        anche aggiornamenti (non solo cancellazioni) fatti da altre istanze.
    """
    if not api.is_authenticated:
        return SyncResult(False, "Non autenticato")

    rimossi_totali = 0
    set_downloading(engine, True)
    try:
        # Pre-carica in un'unica query tutte le pending op (raggruppate per
        # entity_type). Senza, ogni iterazione faceva un SELECT su
        # pending_operations: 7 query consecutive identiche.
        protected_by_entity = _all_protected_ids(engine)

        # Per ogni entità: scarica lo stato corrente completo (since=None),
        # upserta gli items e cancella i fantasmi.
        for fetcher, upserter, table, entity_type in [
            (api.sync_aziende, _upsert_aziende, "aziende", "AZIENDA"),
            (api.sync_agri, _upsert_agri, "agri", "AGRO"),
            (api.sync_contrade, _upsert_contrade, "contrade", "CONTRADA"),
            (api.sync_tendoni, _upsert_tendoni, "tendoni", "TENDONE"),
            (api.sync_prodotti, _upsert_prodotti, "prodotti", "PRODOTTO"),
            (api.sync_trattamenti, _upsert_trattamenti, "trattamenti", "TRATTAMENTO"),
            (api.sync_movimenti, _upsert_movimenti, "registro_magazzino", "MOVIMENTO"),
        ]:
            resp = fetcher(None)
            items = resp.get("items", [])

            # ID con pending op: non vanno toccati né dall'upsert né dal
            # phantom-delete. Preserva scritture locali non ancora propagate.
            protected = protected_by_entity.get(entity_type, set())
            safe_items = [it for it in items if it.get("id") not in protected]
            upserter(engine, safe_items)

            rimossi = _reconcile_entity(engine, table, entity_type, items, protected, notifier=notifier)
            if rimossi:
                log.info("Reconcile %s: rimossi %d record fantasma", table, rimossi)
                rimossi_totali += rimossi

        # Avvisi: full-replace come in sync_all
        resp_avvisi = api.sync_avvisi(since=None)
        _replace_avvisi(engine, resp_avvisi.get("items", []))

        # Cleanup dettagli orfani (sicurezza)
        orfani = _cleanup_orfani_dettagli(engine)
        if orfani:
            log.info("Reconcile: rimossi %d dettagli orfani", orfani)

        msg = f"Reconciliation OK ({rimossi_totali} fantasmi)" if rimossi_totali else "Reconciliation OK"
        return SyncResult(True, msg)

    except NotAuthenticatedError as e:
        return SyncResult(False, f"Sessione scaduta: {e}")
    except NetworkError as e:
        return SyncResult(False, f"Errore di rete: {e}")
    except ApiError as e:
        return SyncResult(False, f"Errore API: {e} ({e.detail or ''})")
    except Exception as e:
        return SyncResult(False, f"Errore inatteso: {e}")
    finally:
        set_downloading(engine, False)


def pull_trattamenti(api: ApiClient, engine: Engine, notifier=None) -> tuple:
    """Pull incrementale dei SOLI trattamenti (+ avvisi) dal server.

    Pensata per essere chiamata con alta frequenza (es. ogni 5s) per ottenere
    quasi-real-time delle modifiche fatte da altri client (es. app mobile che
    carica un nuovo trattamento).

    Ritorna (n_aggiunti_o_modificati, n_cancellati).

    Differenze con sync_all:
      - Tocca solo entity TRATTAMENTI e AVVISI (non aziende/agri/contrade/etc.)
      - Usa il `since` esistente per avere il delta minimo
      - Rispetta i pending op locali (non sovrascrive scritture utente in coda)
    """
    if not api.is_authenticated:
        return (0, 0)

    set_downloading(engine, True)
    try:
        since = get_sync_since(engine, E_TRATTAMENTI)
        resp = api.sync_trattamenti(since)
        items = resp.get("items", [])
        deleted_ids = resp.get("deleted_ids", [])

        # Rispetta i pending op locali (no overwrite di scritture non ancora caricate)
        protected = _protected_ids_for(engine, "TRATTAMENTO")
        safe_items = [it for it in items if it.get("id") not in protected]
        safe_deletes = [i for i in deleted_ids if i not in protected]

        _delete_ids(engine, "trattamenti", safe_deletes)
        _upsert_trattamenti(engine, safe_items)
        set_sync_since(engine, E_TRATTAMENTI, resp["server_time"])

        # Se ci sono modifiche, refresha anche gli avvisi (lato server vengono
        # ricalcolati dopo ogni cambio trattamento)
        if safe_items or safe_deletes:
            try:
                resp_avvisi = api.sync_avvisi(since=None)
                _replace_avvisi(engine, resp_avvisi.get("items", []))
            except Exception as e:
                log.warning("Pull: errore refresh avvisi: %s", e)

        return (len(safe_items), len(safe_deletes))
    finally:
        set_downloading(engine, False)


def sync_all(api: ApiClient, engine: Engine) -> SyncResult:
    """Esegue una sync incrementale di tutte le entità."""
    if not api.is_authenticated:
        return SyncResult(False, "Non autenticato")

    # Disattiva i trigger di accodamento durante il download dal server,
    # altrimenti ogni record che arriva genera un'operazione "INSERT" da rimandare al server.
    set_downloading(engine, True)
    try:
        # 1) AZIENDE
        since = get_sync_since(engine, E_AZIENDE)
        resp = api.sync_aziende(since)
        _upsert_aziende(engine, resp.get("items", []))
        _delete_ids(engine, "aziende", resp.get("deleted_ids", []))
        set_sync_since(engine, E_AZIENDE, resp["server_time"])

        # 2) AGRI
        since = get_sync_since(engine, E_AGRI)
        resp = api.sync_agri(since)
        _upsert_agri(engine, resp.get("items", []))
        _delete_ids(engine, "agri", resp.get("deleted_ids", []))
        set_sync_since(engine, E_AGRI, resp["server_time"])

        # 3) CONTRADE
        since = get_sync_since(engine, E_CONTRADE)
        resp = api.sync_contrade(since)
        _upsert_contrade(engine, resp.get("items", []))
        _delete_ids(engine, "contrade", resp.get("deleted_ids", []))
        set_sync_since(engine, E_CONTRADE, resp["server_time"])

        # 4) TENDONI
        since = get_sync_since(engine, E_TENDONI)
        resp = api.sync_tendoni(since)
        _upsert_tendoni(engine, resp.get("items", []))
        _delete_ids(engine, "tendoni", resp.get("deleted_ids", []))
        set_sync_since(engine, E_TENDONI, resp["server_time"])

        # 5) PRODOTTI
        since = get_sync_since(engine, E_PRODOTTI)
        resp = api.sync_prodotti(since)
        _upsert_prodotti(engine, resp.get("items", []))
        _delete_ids(engine, "prodotti", resp.get("deleted_ids", []))
        set_sync_since(engine, E_PRODOTTI, resp["server_time"])

        # 6) TRATTAMENTI
        since = get_sync_since(engine, E_TRATTAMENTI)
        resp = api.sync_trattamenti(since)
        _delete_ids(engine, "trattamenti", resp.get("deleted_ids", []))
        _upsert_trattamenti(engine, resp.get("items", []))
        set_sync_since(engine, E_TRATTAMENTI, resp["server_time"])

        # 7) MAGAZZINO
        since = get_sync_since(engine, E_MAGAZZINO)
        resp = api.sync_movimenti(since=since)
        _upsert_movimenti(engine, resp.get("items", []))
        _delete_ids(engine, "registro_magazzino", resp.get("deleted_ids", []))
        set_sync_since(engine, E_MAGAZZINO, resp["server_time"])

        # 8) AVVISI - strategia full-replace
        # Gli avvisi vengono ricalcolati interamente sul backend dopo ogni cambio
        # trattamento, quindi qui cancelliamo tutto e re-inseriamo. Volume basso.
        # Ignoro `since`: il server restituisce sempre tutto.
        resp = api.sync_avvisi(since=None)
        _replace_avvisi(engine, resp.get("items", []))
        set_sync_since(engine, E_AVVISI, resp["server_time"])

        return SyncResult(True, "Sync completata")

    except NotAuthenticatedError as e:
        return SyncResult(False, f"Sessione scaduta: {e}")
    except NetworkError as e:
        return SyncResult(False, f"Errore di rete: {e}")
    except ApiError as e:
        return SyncResult(False, f"Errore API: {e} ({e.detail or ''})")
    except Exception as e:
        return SyncResult(False, f"Errore inatteso: {e}")
    finally:
        # Riattiva i trigger di accodamento per le scritture future fatte dalla UI.
        set_downloading(engine, False)
