"""Database SQLite locale per l'app desktop. Mirror dello schema MySQL del backend.

Lo schema è creato dall'app al primo avvio. Le scritture passano sempre prima
dall'API; localmente serve solo per le letture (offline e per velocità UI).

Tabella speciale `sync_state(entity, server_time)` traccia l'ultimo timestamp
di sync per ogni entità: lo riusiamo come `since=...` alla prossima sync.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import Engine


def _db_path() -> Path:
    base = Path.home() / ".agrimessina"
    base.mkdir(parents=True, exist_ok=True)
    return base / "local.db"


def get_engine() -> Engine:
    return create_engine(
        f"sqlite:///{_db_path()}",
        connect_args={"check_same_thread": False},
        future=True,
    )


# Listener globale: abilita `PRAGMA foreign_keys=ON` ad ogni nuova connessione
# di QUALUNQUE Engine SQLite, una sola volta. La registrazione locale (dentro
# get_engine) c'era anche prima ma era duplicata — il listener girava DUE
# volte per ogni connessione.
@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL: lettori non bloccano scritture e viceversa. Senza, una transazione
        # lunga (sync_all, reconcile, ricalcolo magazzino) blocca i SELECT della
        # UI fino al commit, producendo errori "database is locked" che oggi
        # vengono solo loggati (vedi _check_and_sync). WAL è persistito sul DB
        # quindi basta settarlo una volta.
        cursor.execute("PRAGMA journal_mode=WAL")
        # NORMAL: trade-off ragionevole su WAL (fsync solo al checkpoint, non
        # ad ogni commit). FULL sarebbe più sicuro ma molto più lento.
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 5s di attesa prima di sollevare "locked" su SELECT: copre i piccoli
        # picchi di contesa senza far fallire la UI.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def init_local_database(engine: Engine) -> None:
    """Crea le tabelle locali se non esistono. Tenta di mantenere lo stesso schema
    del MySQL del backend, con due differenze necessarie per SQLite:
      - INTEGER PRIMARY KEY AUTOINCREMENT (non INT AUTO_INCREMENT)
      - REAL invece di FLOAT/DOUBLE
    """
    with engine.begin() as conn:
        # Anagrafiche
        conn.execute(text("""CREATE TABLE IF NOT EXISTS aziende (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL UNIQUE
        )"""))
        # NOTA: niente UNIQUE(azienda_id, nome) su `agri` e (agro_id, nome)
        # su `contrade`. Il backend non li impone, e averli localmente
        # bloccava silenziosamente upsert validi server-side. Vedi migration v2.
        conn.execute(text("""CREATE TABLE IF NOT EXISTS agri (
            id INTEGER PRIMARY KEY,
            azienda_id INTEGER REFERENCES aziende(id),
            nome TEXT NOT NULL
        )"""))
        conn.execute(text("""CREATE TABLE IF NOT EXISTS contrade (
            id INTEGER PRIMARY KEY,
            agro_id INTEGER REFERENCES agri(id),
            nome TEXT NOT NULL
        )"""))
        conn.execute(text("""CREATE TABLE IF NOT EXISTS tendoni (
            id INTEGER PRIMARY KEY,
            contrada_id INTEGER REFERENCES contrade(id),
            codice TEXT NOT NULL,
            ettari REAL NOT NULL
        )"""))

        # Prodotti
        # `unita_carico`: UM con cui l'utente inserisce i carichi manuali a
        # magazzino (es. "kg" anche se l'unità di misura del prodotto è "g/ha").
        # Le opzioni dipendono dal numeratore di `unita_misura` e sono imposte
        # dalla UI (mg/g/kg per massa, ml/l per volume, "Unità" per unità).
        conn.execute(text("""CREATE TABLE IF NOT EXISTS prodotti (
            id INTEGER PRIMARY KEY,
            nome_prodotto TEXT NOT NULL UNIQUE,
            categoria TEXT,
            numero_registrazione TEXT,
            sostanza_attiva TEXT,
            bio_convenzionale TEXT,
            avversita TEXT,
            titolo_n REAL, titolo_p REAL, titolo_k REAL,
            phi_giorni INTEGER,
            trattamenti_max INTEGER,
            intervallo_min_tratt INTEGER,
            unita_misura TEXT,
            unita_carico TEXT,
            min_sostanza REAL, max_sostanza REAL, qta_acqua REAL,
            blacklist TEXT DEFAULT 'No'
        )"""))

        # Migrazione idempotente: ALTER TABLE per DB pre-esistenti senza
        # `unita_carico`. CREATE TABLE IF NOT EXISTS è no-op se la tabella c'è
        # già, quindi serve ALTER esplicito.
        cols_p = {r[1] for r in conn.execute(text("PRAGMA table_info(prodotti)")).fetchall()}
        if "unita_carico" not in cols_p:
            conn.execute(text("ALTER TABLE prodotti ADD COLUMN unita_carico TEXT"))

        # Trattamenti + dettagli
        conn.execute(text("""CREATE TABLE IF NOT EXISTS trattamenti (
            id INTEGER PRIMARY KEY,
            data_trattamento DATE NOT NULL,
            data_inserimento DATETIME,
            prodotto_id INTEGER REFERENCES prodotti(id),
            operatore TEXT,
            tipo_trattamento TEXT,
            modalita_fertilizzazione TEXT,
            scaricato_magazzino TEXT DEFAULT '0',
            is_autorizzato INTEGER DEFAULT 0
        )"""))
        conn.execute(text("""CREATE TABLE IF NOT EXISTS dettaglio_trattamenti (
            id INTEGER PRIMARY KEY,
            trattamento_id INTEGER REFERENCES trattamenti(id) ON DELETE CASCADE,
            tendone_id INTEGER REFERENCES tendoni(id),
            quantita_sostanza REAL NOT NULL,
            botti REAL,
            dose_ha REAL,
            is_bilanciamento INTEGER DEFAULT 0,
            bilanciamento_group_id INTEGER
        )"""))

        # Magazzino con integrità totale (Cancellazione e Aggiornamento a catena)
        # azienda_id = MAGAZZINO che paga (post-alias resolution).
        # azienda_id_origine = azienda del TENDONE originale (pre-alias).
        # Per Deflorio Ciccopinto: azienda_id=Messina_Alfio, azienda_id_origine=Deflorio_Ciccopinto.
        conn.execute(text("""CREATE TABLE IF NOT EXISTS registro_magazzino (
            id INTEGER PRIMARY KEY,
            prodotto_id INTEGER REFERENCES prodotti(id) ON DELETE CASCADE,
            trattamento_id INTEGER REFERENCES trattamenti(id) ON DELETE CASCADE ON UPDATE CASCADE,
            azienda_id INTEGER REFERENCES aziende(id),
            azienda_id_origine INTEGER REFERENCES aziende(id),
            data_movimento DATE NOT NULL,
            tipo_movimento TEXT NOT NULL,
            quantita REAL NOT NULL,
            n_ddt TEXT,
            fornitore TEXT,
            note TEXT
        )"""))

        # Migrazione idempotente per DB pre-esistenti (creati da versioni
        # precedenti al campo `azienda_id_origine`): se la tabella esiste già
        # ma manca la colonna, CREATE TABLE IF NOT EXISTS è no-op e l'indice
        # successivo crasherebbe. ALTER TABLE è l'unico modo per aggiungerla
        # senza perdere i dati.
        cols_rm = {r[1] for r in conn.execute(text("PRAGMA table_info(registro_magazzino)")).fetchall()}
        if "azienda_id_origine" not in cols_rm:
            conn.execute(text(
                "ALTER TABLE registro_magazzino ADD COLUMN azienda_id_origine INTEGER REFERENCES aziende(id)"
            ))

        # Indici espliciti su colonne usate pesantemente in WHERE/IN dalle
        # query magazzino (`WHERE rm.azienda_id IN (...)`). SQLite NON
        # auto-indicizza le foreign key, e senza queste le viste per-azienda
        # fanno full-scan sulla tabella.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rm_azienda ON registro_magazzino(azienda_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rm_azienda_origine ON registro_magazzino(azienda_id_origine)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rm_prodotto ON registro_magazzino(prodotto_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rm_trattamento ON registro_magazzino(trattamento_id)"
        ))

        # MAGAZZINO FITTIZIO — schema identico al reale ma LOCAL-ONLY.
        #
        # Modello a due registri:
        #   • registro_magazzino           = REALE (Storico, is_autorizzato=0)
        #     Popolato all'INSERT/UPDATE del trattamento con qta originale
        #     (dt.is_bilanciamento=0). Sincronizzato col backend (per CARICHI
        #     manuali); gli SCARICHI automatici sono derivati e ricalcolati
        #     localmente. Si CONGELA al passaggio a Revisionati.
        #
        #   • registro_magazzino_fittizio  = FITTIZIO (Revisionati, is_aut=1)
        #     Popolato dal momento della REVISIONE in poi: scarico iniziale
        #     con qta corrente (incluso bilanciamenti applicati) + ogni
        #     bilanciamento successivo. Local-only, mai sincronizzato. Nessun
        #     CARICO manuale (UI read-only): si popola solo automaticamente.
        conn.execute(text("""CREATE TABLE IF NOT EXISTS registro_magazzino_fittizio (
            id INTEGER PRIMARY KEY,
            prodotto_id INTEGER REFERENCES prodotti(id) ON DELETE CASCADE,
            trattamento_id INTEGER REFERENCES trattamenti(id) ON DELETE CASCADE ON UPDATE CASCADE,
            azienda_id INTEGER REFERENCES aziende(id),
            azienda_id_origine INTEGER REFERENCES aziende(id),
            data_movimento DATE NOT NULL,
            tipo_movimento TEXT NOT NULL,
            quantita REAL NOT NULL,
            n_ddt TEXT,
            fornitore TEXT,
            note TEXT
        )"""))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rmf_azienda ON registro_magazzino_fittizio(azienda_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rmf_azienda_origine ON registro_magazzino_fittizio(azienda_id_origine)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rmf_prodotto ON registro_magazzino_fittizio(prodotto_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rmf_trattamento ON registro_magazzino_fittizio(trattamento_id)"
        ))

        # Avvisi (calcolati localmente con la stessa logica del backend desktop)
        conn.execute(text("""CREATE TABLE IF NOT EXISTS avvisi_trattamenti (
            id INTEGER PRIMARY KEY,
            trattamento_id INTEGER NOT NULL REFERENCES trattamenti(id) ON DELETE CASCADE,
            testo TEXT NOT NULL
        )"""))

        # Stato sync
        conn.execute(text("""CREATE TABLE IF NOT EXISTS sync_state (
            entity TEXT PRIMARY KEY,
            server_time TEXT
        )"""))

        # Coda offline: ogni write locale popola questa tabella tramite trigger.
        # retry_count: contatore retry per errori transienti; oltre il max
        # configurato (CONFIG.pending_retry_max) l'operazione viene spostata
        # in dead_letter_operations per audit.
        conn.execute(text("""CREATE TABLE IF NOT EXISTS pending_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            entity_id INTEGER,
            payload_json TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""))

        # Migrazione idempotente per DB pre-esistenti senza retry_count/last_error.
        # CREATE TABLE IF NOT EXISTS è no-op se la tabella esiste già, quindi
        # l'unica via per aggiungere i campi a un DB vecchio è ALTER TABLE.
        cols_po = {r[1] for r in conn.execute(text("PRAGMA table_info(pending_operations)")).fetchall()}
        if "retry_count" not in cols_po:
            conn.execute(text("ALTER TABLE pending_operations ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))
        if "last_error" not in cols_po:
            conn.execute(text("ALTER TABLE pending_operations ADD COLUMN last_error TEXT"))

        # Dead-letter: operazioni fallite definitivamente (oltre retry_count_max).
        # Conservate per audit/recupero manuale, mai ritentate automaticamente.
        conn.execute(text("""CREATE TABLE IF NOT EXISTS dead_letter_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id INTEGER,
            entity_type TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            entity_id INTEGER,
            payload_json TEXT,
            retry_count INTEGER,
            last_error TEXT,
            failed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""))

        # Indici per le query frequenti (join, filtri, ordini).
        # IF NOT EXISTS rende l'operazione idempotente al boot.
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_dt_trattamento ON dettaglio_trattamenti(trattamento_id)",
            "CREATE INDEX IF NOT EXISTS idx_dt_tendone ON dettaglio_trattamenti(tendone_id)",
            "CREATE INDEX IF NOT EXISTS idx_dt_isbil ON dettaglio_trattamenti(is_bilanciamento)",
            "CREATE INDEX IF NOT EXISTS idx_t_prodotto ON trattamenti(prodotto_id)",
            "CREATE INDEX IF NOT EXISTS idx_t_isaut ON trattamenti(is_autorizzato)",
            "CREATE INDEX IF NOT EXISTS idx_t_data ON trattamenti(data_trattamento)",
            "CREATE INDEX IF NOT EXISTS idx_av_trat ON avvisi_trattamenti(trattamento_id)",
            # idx_rm_trattamento e idx_rm_prodotto sono già creati sopra
            # nella sezione CREATE TABLE registro_magazzino: senza questa
            # nota, era facile aggiungere duplicati con nomi diversi.
            "CREATE INDEX IF NOT EXISTS idx_pend_entity ON pending_operations(entity_type, operation_type)",
            "CREATE INDEX IF NOT EXISTS idx_pend_entity_id ON pending_operations(entity_type, entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_ten_contrada ON tendoni(contrada_id)",
            "CREATE INDEX IF NOT EXISTS idx_c_agro ON contrade(agro_id)",
            "CREATE INDEX IF NOT EXISTS idx_a_azienda ON agri(azienda_id)",
        ]:
            conn.execute(text(idx_sql))

        # Flag per disabilitare i trigger durante la sync_all (download dal server),
        # altrimenti ogni inserimento "scaricato" verrebbe rimandato al server in loop.
        # Controllato via PRAGMA user_version o tabella di servizio. Usiamo una tabella
        # temporanea di flag per semplicità.
        conn.execute(text("""CREATE TABLE IF NOT EXISTS _sync_flags (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )"""))
        conn.execute(text("INSERT OR IGNORE INTO _sync_flags (key, value) VALUES ('downloading', 0)"))
        # Forza il reset del flag downloading: se l'app è crashata durante una
        # sync con flag=1, al riavvio i trigger continuerebbero a non accodare.
        conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))

        # Trigger: se siamo in modalità "downloading" (= sync da server in corso),
        # NON accodiamo. Altrimenti registriamo l'operazione.
        # Ogni trigger genera un payload JSON con i campi rilevanti per l'API.

        _install_triggers(conn)


def _install_triggers(conn) -> None:
    """Installa i trigger SQLite che popolano pending_operations.

    Pattern di ogni trigger:
      WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
      INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
      VALUES (...);
    """

    def _create(name: str, sql: str):
        conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        conn.execute(text(sql))

    # AZIENDE
    _create("trg_aziende_ai", """
        CREATE TRIGGER trg_aziende_ai AFTER INSERT ON aziende
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('AZIENDA', 'INSERT', NEW.id,
                json_object('nome', NEW.nome));
        END;
    """)
    _create("trg_aziende_au", """
        CREATE TRIGGER trg_aziende_au AFTER UPDATE ON aziende
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('AZIENDA', 'UPDATE', NEW.id,
                json_object('nome', NEW.nome));
        END;
    """)
    _create("trg_aziende_ad", """
        CREATE TRIGGER trg_aziende_ad AFTER DELETE ON aziende
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('AZIENDA', 'DELETE', OLD.id, NULL);
        END;
    """)

    # AGRI
    _create("trg_agri_ai", """
        CREATE TRIGGER trg_agri_ai AFTER INSERT ON agri
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('AGRO', 'INSERT', NEW.id,
                json_object('azienda_id', NEW.azienda_id, 'nome', NEW.nome));
        END;
    """)
    _create("trg_agri_au", """
        CREATE TRIGGER trg_agri_au AFTER UPDATE ON agri
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('AGRO', 'UPDATE', NEW.id,
                json_object('azienda_id', NEW.azienda_id, 'nome', NEW.nome));
        END;
    """)
    _create("trg_agri_ad", """
        CREATE TRIGGER trg_agri_ad AFTER DELETE ON agri
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('AGRO', 'DELETE', OLD.id, NULL);
        END;
    """)

    # CONTRADE
    _create("trg_contrade_ai", """
        CREATE TRIGGER trg_contrade_ai AFTER INSERT ON contrade
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('CONTRADA', 'INSERT', NEW.id,
                json_object('agro_id', NEW.agro_id, 'nome', NEW.nome));
        END;
    """)
    _create("trg_contrade_au", """
        CREATE TRIGGER trg_contrade_au AFTER UPDATE ON contrade
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('CONTRADA', 'UPDATE', NEW.id,
                json_object('agro_id', NEW.agro_id, 'nome', NEW.nome));
        END;
    """)
    _create("trg_contrade_ad", """
        CREATE TRIGGER trg_contrade_ad AFTER DELETE ON contrade
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('CONTRADA', 'DELETE', OLD.id, NULL);
        END;
    """)

    # TENDONI
    _create("trg_tendoni_ai", """
        CREATE TRIGGER trg_tendoni_ai AFTER INSERT ON tendoni
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('TENDONE', 'INSERT', NEW.id,
                json_object('contrada_id', NEW.contrada_id, 'codice', NEW.codice, 'ettari', NEW.ettari));
        END;
    """)
    _create("trg_tendoni_au", """
        CREATE TRIGGER trg_tendoni_au AFTER UPDATE ON tendoni
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('TENDONE', 'UPDATE', NEW.id,
                json_object('contrada_id', NEW.contrada_id, 'codice', NEW.codice, 'ettari', NEW.ettari));
        END;
    """)
    _create("trg_tendoni_ad", """
        CREATE TRIGGER trg_tendoni_ad AFTER DELETE ON tendoni
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('TENDONE', 'DELETE', OLD.id, NULL);
        END;
    """)

    # PRODOTTI
    _create("trg_prodotti_ai", """
        CREATE TRIGGER trg_prodotti_ai AFTER INSERT ON prodotti
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('PRODOTTO', 'INSERT', NEW.id,
                json_object(
                    'nome_prodotto', NEW.nome_prodotto, 'categoria', NEW.categoria,
                    'numero_registrazione', NEW.numero_registrazione, 'sostanza_attiva', NEW.sostanza_attiva,
                    'bio_convenzionale', NEW.bio_convenzionale, 'avversita', NEW.avversita,
                    'titolo_n', NEW.titolo_n, 'titolo_p', NEW.titolo_p, 'titolo_k', NEW.titolo_k,
                    'phi_giorni', NEW.phi_giorni, 'trattamenti_max', NEW.trattamenti_max,
                    'intervallo_min_tratt', NEW.intervallo_min_tratt, 'unita_misura', NEW.unita_misura,
                    'unita_carico', NEW.unita_carico,
                    'min_sostanza', NEW.min_sostanza, 'max_sostanza', NEW.max_sostanza,
                    'qta_acqua', NEW.qta_acqua, 'blacklist', NEW.blacklist
                ));
        END;
    """)
    _create("trg_prodotti_au", """
        CREATE TRIGGER trg_prodotti_au AFTER UPDATE ON prodotti
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('PRODOTTO', 'UPDATE', NEW.id,
                json_object(
                    'nome_prodotto', NEW.nome_prodotto, 'categoria', NEW.categoria,
                    'numero_registrazione', NEW.numero_registrazione, 'sostanza_attiva', NEW.sostanza_attiva,
                    'bio_convenzionale', NEW.bio_convenzionale, 'avversita', NEW.avversita,
                    'titolo_n', NEW.titolo_n, 'titolo_p', NEW.titolo_p, 'titolo_k', NEW.titolo_k,
                    'phi_giorni', NEW.phi_giorni, 'trattamenti_max', NEW.trattamenti_max,
                    'intervallo_min_tratt', NEW.intervallo_min_tratt, 'unita_misura', NEW.unita_misura,
                    'unita_carico', NEW.unita_carico,
                    'min_sostanza', NEW.min_sostanza, 'max_sostanza', NEW.max_sostanza,
                    'qta_acqua', NEW.qta_acqua, 'blacklist', NEW.blacklist
                ));
        END;
    """)
    _create("trg_prodotti_ad", """
        CREATE TRIGGER trg_prodotti_ad AFTER DELETE ON prodotti
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('PRODOTTO', 'DELETE', OLD.id, NULL);
        END;
    """)

    # MAGAZZINO (registro_magazzino)
    #
    # IMPORTANTE: i trigger accodano SOLO movimenti con `trattamento_id IS NULL`,
    # cioè i CARICHI/SCARICHI MANUALI inseriti dall'utente via DialogNuovoMovimento.
    #
    # Gli scarichi AUTOMATICI (con trattamento_id valorizzato, creati da
    # `sincronizza_scarico`) sono dati DERIVATI dai trattamenti: ogni client li
    # ricomputa indipendentemente dai trattamenti sincronizzati. Sincronizzarli
    # via API era un bug perché:
    #   a) Il payload del trigger non include trattamento_id (il campo non era
    #      catturato), quindi il server riceveva una copia senza la FK.
    #   b) Al successivo pull, il record locale veniva sovrascritto con la
    #      versione server (trattamento_id=NULL), diventando orfano.
    _create("trg_magazzino_ai", """
        CREATE TRIGGER trg_magazzino_ai AFTER INSERT ON registro_magazzino
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
          AND NEW.trattamento_id IS NULL
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('MOVIMENTO', 'INSERT', NEW.id,
                json_object(
                    'prodotto_id', NEW.prodotto_id, 'azienda_id', NEW.azienda_id,
                    'data_movimento', NEW.data_movimento, 'tipo_movimento', NEW.tipo_movimento,
                    'quantita', NEW.quantita, 'n_ddt', NEW.n_ddt,
                    'fornitore', NEW.fornitore, 'note', NEW.note
                ));
        END;
    """)
    _create("trg_magazzino_au", """
        CREATE TRIGGER trg_magazzino_au AFTER UPDATE ON registro_magazzino
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
          AND NEW.trattamento_id IS NULL
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('MOVIMENTO', 'UPDATE', NEW.id,
                json_object(
                    'prodotto_id', NEW.prodotto_id, 'azienda_id', NEW.azienda_id,
                    'data_movimento', NEW.data_movimento, 'tipo_movimento', NEW.tipo_movimento,
                    'quantita', NEW.quantita, 'n_ddt', NEW.n_ddt,
                    'fornitore', NEW.fornitore, 'note', NEW.note
                ));
        END;
    """)
    _create("trg_magazzino_ad", """
        CREATE TRIGGER trg_magazzino_ad AFTER DELETE ON registro_magazzino
        WHEN (SELECT value FROM _sync_flags WHERE key='downloading') = 0
          AND OLD.trattamento_id IS NULL
        BEGIN
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES ('MOVIMENTO', 'DELETE', OLD.id, NULL);
        END;
    """)

    # NOTA: per i TRATTAMENTI il payload include anche i dettagli, ma con i trigger
    # SQLite è scomodo aggregare la testata + righe figlie in un unico evento JSON.
    # Per i trattamenti lasciamo una soluzione esplicita in `ui_trattamenti.py`:
    # le funzioni di salvataggio chiameranno direttamente `enqueue_trattamento(...)`
    # da `pending_uploader` invece di affidarsi al trigger.
    # Questo mantiene la coerenza testata-dettagli.


# ---- Helpers per sync_state ------------------------------------------------

def get_sync_since(engine: Engine, entity: str) -> Optional[str]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT server_time FROM sync_state WHERE entity = :e"),
            {"e": entity},
        ).first()
        if row and row[0]:
            return row[0]
        return None


def set_sync_since(engine: Engine, entity: str, server_time: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO sync_state (entity, server_time) VALUES (:e, :t)
            ON CONFLICT(entity) DO UPDATE SET server_time = excluded.server_time
        """), {"e": entity, "t": server_time})


def reset_sync_state(engine: Engine) -> None:
    """Forza la prossima sync a essere una full sync."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sync_state"))


# ---- Toggle modalità "downloading" ----------------------------------------

def set_downloading(engine: Engine, on: bool) -> None:
    """Abilita/disabilita i trigger di accodamento.

    Quando `on=True`, le scritture sul DB locale (es. quelle fatte da sync_all
    durante il download dal server) NON vengono accodate in pending_operations.
    Da chiamare prima e dopo `sync_all`.

    Limitazione nota: se la UI scrivesse su anagrafiche/prodotti/magazzino
    mentre il flag è a 1 (sync in corso), il trigger NON accoderebbe e
    l'operazione andrebbe persa lato server. In pratica protetto dal fatto
    che Qt è single-thread e sync_all/reconcile_with_server non chiamano
    processEvents() — l'UI è congelata durante la sync. Da rivedere se in
    futuro la sync viene messa in QThread.
    """
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE _sync_flags SET value = :v WHERE key = 'downloading'"),
            {"v": 1 if on else 0},
        )


def is_downloading(engine: Engine) -> bool:
    # engine.connect() invece di engine.begin(): è un read-only e non serve
    # acquisire il lock di scrittura per un SELECT su una singola riga.
    with engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM _sync_flags WHERE key = 'downloading'")).first()
        return bool(row and row[0])


# ---- Helper per accodare manualmente operazioni complesse (es. trattamenti) ----

def enqueue_operation(engine_or_conn, entity_type, operation_type,
                      entity_id=None, payload=None):
    import json as _json
    def _do_work(c):
        c.execute(text("""
            INSERT INTO pending_operations (entity_type, operation_type, entity_id, payload_json)
            VALUES (:et, :ot, :eid, :pj)
        """), {
            "et": entity_type, "ot": operation_type, "eid": entity_id,
            "pj": _json.dumps(payload) if payload else None,
        })

    if hasattr(engine_or_conn, "execute"):
        _do_work(engine_or_conn)
    else:
        with engine_or_conn.begin() as conn:
            _do_work(conn)
