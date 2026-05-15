"""Schema migrations versionate per il SQLite locale.

Funziona così:
- Una tabella `_schema_version` (singola riga) traccia la versione corrente del DB.
- Al boot, `apply_pending_migrations()` legge la versione e applica tutte
  le migrazioni con numero > corrente, in ordine. Bumpa la versione dopo
  ogni successo.
- Ogni migrazione è una funzione `_migrate_vN(conn)` registrata in MIGRATIONS.
- Le migrazioni sono SEMPRE idempotenti (CREATE TABLE IF NOT EXISTS,
  ALTER TABLE solo se la colonna manca, ecc): se vengono applicate due
  volte non rompono nulla.

Rapporto con `local_db.init_local_database`:
- `init_local_database` crea lo schema base con CREATE TABLE IF NOT EXISTS.
  Per i DB nuovi, dopo init lo schema è completo: la migration manager poi
  registra che il DB è alla versione MAX(MIGRATIONS).
- Per i DB esistenti pre-migration-system, le ALTER inline in
  `init_local_database` sono ridondanti ma innocue: anche `_migrate_vN`
  le rifà idempotenti. Quando saremo certi che tutti gli utenti sono
  passati per qui, le inline si possono rimuovere.

Aggiungere una nuova migration:
1. Definire `_migrate_vN(conn)` qui sotto con l'ALTER/CREATE necessario.
2. Aggiungerla alla lista `MIGRATIONS` in coda con il numero successivo.
3. Idempotenza: SE la migration tocca colonne, controlla con
   PRAGMA table_info che la colonna manchi PRIMA di ALTER TABLE.
4. Commit. Al prossimo avvio sul DB di un utente, la migration scatta.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def _ensure_version_table(conn) -> int:
    """Crea la tabella di versione se manca, ritorna la versione corrente.

    Per i DB esistenti che non hanno mai visto la migration manager, la
    versione iniziale è MAX(MIGRATIONS) — cioè diamo per scontato che
    `init_local_database` (chiamato prima) abbia già fatto tutto il lavoro.
    Senza questa logica un DB pre-esistente con tutte le ALTER inline
    applicate riapplicherebbe inutilmente tutte le migration.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER NOT NULL
        )
    """))
    row = conn.execute(text("SELECT version FROM _schema_version LIMIT 1")).first()
    if row is None:
        # Primo avvio post-introduzione del sistema. Inizializziamo alla
        # versione corrente: lo schema è già stato portato lì dalle ALTER
        # inline in init_local_database.
        baseline = MIGRATIONS[-1][0] if MIGRATIONS else 0
        conn.execute(text("INSERT INTO _schema_version (version) VALUES (:v)"),
                      {"v": baseline})
        log.info("[migration] baseline impostata a v%d (DB pre-esistente)", baseline)
        return baseline
    return int(row[0])


def _set_version(conn, version: int) -> None:
    conn.execute(text("DELETE FROM _schema_version"))
    conn.execute(text("INSERT INTO _schema_version (version) VALUES (:v)"),
                  {"v": version})


# ============================================================================
# MIGRATIONS — aggiungere in coda con numero progressivo
# ============================================================================
# Le migration v1 (baseline) e v2 (drop UNIQUE su agri/contrade) sono state
# applicate a tutti gli utenti in produzione e rimosse: oggi `init_local_database`
# crea già lo schema target. Le nuove migration partono da v3 in poi.


def _migrate_v3(conn) -> None:
    """Sana il flag is_autorizzato sui figli bilanciamento esistenti.

    Storia del bug: il payload INSERT verso il backend ometteva
    `is_autorizzato`, quindi il server creava i figli con default 0 e al
    primo reconcile sovrascriveva il valore locale (creato a 1). Risultato:
    i figli risultavano "in Storico" e non erano ri-bilanciabili.

    Definizione di "figlio bilanciamento": un trattamento i cui dettagli
    sono TUTTI is_bilanciamento=1 (MIN(is_bilanciamento) = 1). Sono per
    costruzione gli output di `DialogCompensaDisavanzo`, e vivono nella
    vista Revisionati (vedi filtro_autorizzato in ui_trattamenti.py).

    Effetto: locale-only. Il server resta out-of-sync finché non passa un
    UPDATE (e il payload UPDATE generico omette comunque is_autorizzato,
    per non sovrascrivere autorizza/revoca da altri client). Il fix
    applicativo a regime è in trattamenti_payload.build_trattamento_payload
    (include_is_autorizzato=True solo su INSERT) + nel gate di
    _apri_compensazione che ora accetta anche solo_bilanciamenti=1.
    """
    res = conn.execute(text("""
        UPDATE trattamenti
        SET is_autorizzato = 1
        WHERE is_autorizzato = 0
          AND id IN (
            SELECT t.id
            FROM trattamenti t
            JOIN dettaglio_trattamenti dt ON dt.trattamento_id = t.id
            GROUP BY t.id
            HAVING MIN(COALESCE(dt.is_bilanciamento, 0)) = 1
          )
    """))
    n = res.rowcount or 0
    if n:
        log.info("[migration v3] is_autorizzato=1 su %d figli bilanciamento", n)


def _migrate_v4(conn) -> None:
    """Aggiunge `bilanciamento_group_id` su dettaglio_trattamenti.

    Lega ogni dt di bilanciamento (positivo sul sub-trattamento, negativi
    sui source) al sub di destinazione. Senza questo riferimento esplicito
    non è possibile, in fase di annullamento di un sub in sottodose,
    cancellare anche i dt negativi gemelli sui source: il prodotto
    resterebbe "in traccia" come bilanciamento contabile.
    """
    cols = {row[1] for row in conn.execute(
        text("PRAGMA table_info(dettaglio_trattamenti)")
    ).fetchall()}
    if "bilanciamento_group_id" not in cols:
        conn.execute(text(
            "ALTER TABLE dettaglio_trattamenti "
            "ADD COLUMN bilanciamento_group_id INTEGER"
        ))
        log.info("[migration v4] aggiunta colonna bilanciamento_group_id")


def _migrate_v5(conn) -> None:
    """Aggiunge `operazione_id` su trattamenti.

    Identifica l'operazione "in campo" da cui sono stati generati N
    trattamenti separati. L'app Android consente di inserire più prodotti
    in una singola operazione (versati insieme in botte): il backend
    splitta in N record (uno per prodotto), ma tutti condividono lo stesso
    `operazione_id` (UUID/stringa opaca generato client-side al momento
    dell'inserimento). Permette di ricostruire l'unità logica originale
    per reportistica, viste raggruppate e operazioni di bilanciamento
    coerenti.

    Tipo TEXT (non INTEGER) per consentire la generazione offline lato
    Android senza round-trip al server. NULL = trattamento creato senza
    grouping (desktop, sub-bilanciamenti, record storici pre-feature).
    """
    cols = {row[1] for row in conn.execute(
        text("PRAGMA table_info(trattamenti)")
    ).fetchall()}
    if "operazione_id" not in cols:
        conn.execute(text(
            "ALTER TABLE trattamenti ADD COLUMN operazione_id TEXT"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_t_operazione "
            "ON trattamenti(operazione_id)"
        ))
        log.info("[migration v5] aggiunta colonna operazione_id + indice")


def _migrate_v6(conn) -> None:
    """Aggiunge `operazione_numero` su trattamenti.

    NB: questa colonna viene rinominata in `operazione_id` (INT) nella v7
    consolidante. v6 resta per compatibilità con DB intermedi.
    """
    cols = {row[1] for row in conn.execute(
        text("PRAGMA table_info(trattamenti)")
    ).fetchall()}
    if "operazione_numero" not in cols and "operazione_id" not in cols:
        conn.execute(text(
            "ALTER TABLE trattamenti ADD COLUMN operazione_numero INTEGER"
        ))
        log.info("[migration v6] aggiunta colonna operazione_numero")


def _migrate_v7(conn) -> None:
    """Consolida operazione_id (TEXT/UUID) + operazione_numero (INTEGER) in
    un'unica colonna `operazione_id` di tipo INTEGER.

    Procedimento SQLite:
      1. Se esiste ancora operazione_id TEXT → DROP COLUMN (SQLite 3.35+).
      2. Se operazione_numero esiste → RENAME COLUMN a operazione_id.
      3. Ricrea idx_t_operazione su operazione_id.

    Il backfill (assegnare un operazione_id a tutti i NULL) NON viene fatto
    qui: il desktop riceve i dati via sync dal server, che fa il proprio
    backfill server-side al primo restart post-migration.
    """
    cols = {row[1]: (row[2] or "").upper() for row in conn.execute(
        text("PRAGMA table_info(trattamenti)")
    ).fetchall()}

    # Step 1: drop operazione_id TEXT se ancora presente.
    # SQLite 3.35+ supporta DROP COLUMN. Python 3.12 spedisce SQLite molto più
    # nuovo, ma se per qualche motivo siamo su un binding vecchio (es. utenti
    # con Python custom-build) il DROP fallisce: in quel caso preferiamo
    # interrompere subito invece che proseguire con uno schema misto INT+TEXT
    # che farebbe poi crashare le query downstream con "no such column" o
    # type mismatch silenzioso.
    if "operazione_id" in cols and cols["operazione_id"] in ("TEXT", "VARCHAR", "CHAR"):
        conn.execute(text("DROP INDEX IF EXISTS idx_t_operazione"))
        try:
            conn.execute(text("ALTER TABLE trattamenti DROP COLUMN operazione_id"))
            log.info("[migration v7] drop colonna operazione_id (TEXT/UUID)")
        except Exception:
            log.exception(
                "[migration v7] DROP COLUMN operazione_id fallito. "
                "Probabile SQLite < 3.35. Migrazione interrotta."
            )
            raise

    # Re-read cols dopo drop.
    cols = {row[1] for row in conn.execute(
        text("PRAGMA table_info(trattamenti)")
    ).fetchall()}

    # Step 2: rinomina operazione_numero → operazione_id.
    if "operazione_numero" in cols and "operazione_id" not in cols:
        conn.execute(text("DROP INDEX IF EXISTS idx_t_op_numero"))
        conn.execute(text(
            "ALTER TABLE trattamenti RENAME COLUMN operazione_numero TO operazione_id"
        ))
        log.info("[migration v7] rename operazione_numero → operazione_id (INTEGER)")

    # Step 3: idx unico.
    conn.execute(text("DROP INDEX IF EXISTS idx_t_operazione"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_t_operazione ON trattamenti(operazione_id)"
    ))


MIGRATIONS: list[tuple[int, Callable]] = [
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
    (6, _migrate_v6),
    (7, _migrate_v7),
]


def apply_pending_migrations(engine: Engine) -> int:
    """Applica tutte le migration con versione > corrente, in ordine.
    Ritorna il numero di migrazioni applicate."""
    applied = 0
    with engine.begin() as conn:
        current = _ensure_version_table(conn)
        for version, fn in MIGRATIONS:
            if version <= current:
                continue
            log.info("[migration] applico v%d: %s", version, fn.__name__)
            fn(conn)
            _set_version(conn, version)
            applied += 1
            current = version

    if applied:
        log.info("[migration] applicate %d migration, schema ora a v%d",
                 applied, current)
    return applied


def current_schema_version(engine: Engine) -> int:
    """Utility per UI/diagnostic: legge la versione corrente."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT version FROM _schema_version LIMIT 1")
        ).first()
        return int(row[0]) if row else 0
