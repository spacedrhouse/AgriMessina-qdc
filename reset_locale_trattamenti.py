"""Reset locale dei trattamenti sul DB desktop SQLite.

Da usare in tandem col reset del server: dopo aver wipato le tabelle
trattamenti/dettaglio_trattamenti/registro_magazzino (+ tombstones) sul
MariaDB lato API, questo script ripulisce le copie locali e forza una
full-sync alla prossima apertura dell'app.

USO:
    # Chiudi l'app desktop, poi:
    python reset_locale_trattamenti.py

COSA FA:
- DELETE su trattamenti, dettaglio_trattamenti, avvisi_trattamenti.
- DELETE su registro_magazzino e registro_magazzino_fittizio.
  - Sui carichi/scarichi manuali (trattamento_id IS NULL): lasciati intatti.
  - Sugli auto-scarichi (trattamento_id valorizzato): rimossi.
- DELETE su pending_operations e dead_letter_operations: gli id locali
  vecchi riferiscono record che non esistono più, sarebbero spazzatura.
- DELETE righe in sqlite_sequence (se esistono) per le tabelle pulite:
  così le prossime righe inserite ripartono da 1. Ininfluente per la
  visualizzazione perché i nuovi id arrivano dal server, ma utile per i
  record locali pre-upload (es. multi-prodotto offline).
- DELETE righe sync_state per le entità trattamenti/magazzino/avvisi:
  alla prossima sync il client chiede tutto (since=NULL).

NON tocca: aziende/agri/contrade/tendoni/prodotti/utenti, _schema_version,
i carichi manuali del magazzino.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text


def _db_path() -> Path:
    """Stessa convenzione di local_db._db_path."""
    return Path.home() / ".agrimessina" / "local.db"


def main() -> int:
    db = _db_path()
    if not db.exists():
        print(f"DB locale non trovato in {db}. Niente da fare.")
        return 0

    print(f"Reset locale su {db}")
    print("Assicurati che l'app QDC sia CHIUSA prima di procedere.")
    risposta = input("Procedo? [y/N] ").strip().lower()
    if risposta not in ("y", "yes", "s", "si"):
        print("Annullato.")
        return 1

    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        # Disabilita FK durante il wipe: alcune righe possono violare il
        # constraint mentre cancelliamo i padri, ma riaccendiamo dopo.
        conn.execute(text("PRAGMA foreign_keys = OFF"))

        # Wipe dati trattamenti e correlati.
        for tab in (
            "avvisi_trattamenti",
            "dettaglio_trattamenti",
            "trattamenti",
        ):
            n = conn.execute(text(f"DELETE FROM {tab}")).rowcount or 0
            print(f"  - {tab}: {n} righe cancellate")

        # Solo auto-scarichi (trattamento_id valorizzato): i carichi/scarichi
        # manuali restano.
        for tab in ("registro_magazzino", "registro_magazzino_fittizio"):
            n = conn.execute(text(
                f"DELETE FROM {tab} WHERE trattamento_id IS NOT NULL"
            )).rowcount or 0
            print(f"  - {tab} (auto-scarichi): {n} righe cancellate")

        # Coda upload: id vecchi orfani.
        for tab in ("pending_operations", "dead_letter_operations"):
            try:
                n = conn.execute(text(f"DELETE FROM {tab}")).rowcount or 0
                print(f"  - {tab}: {n} righe cancellate")
            except Exception:
                # tabella opzionale; non bloccare se non esiste
                pass

        # Reset autoincrement counter SQLite (sqlite_sequence). Per tabelle
        # senza AUTOINCREMENT esplicito le righe non esistono → DELETE no-op.
        for tab in (
            "trattamenti", "dettaglio_trattamenti", "avvisi_trattamenti",
            "registro_magazzino", "registro_magazzino_fittizio",
            "pending_operations", "dead_letter_operations",
        ):
            conn.execute(text(
                "DELETE FROM sqlite_sequence WHERE name = :n"
            ), {"n": tab})

        # Forza full sync alla prossima apertura.
        try:
            for entity in (
                "trattamenti", "magazzino_movimenti",
                "avvisi", "magazzino_carichi",
            ):
                conn.execute(text(
                    "DELETE FROM sync_state WHERE entity = :e"
                ), {"e": entity})
            print("  - sync_state: timestamp delle entità trattamento/magazzino azzerati")
        except Exception as e:
            # tabella opzionale; non bloccare
            print(f"  - sync_state: skip ({e})")

        conn.execute(text("PRAGMA foreign_keys = ON"))

    print("\nReset locale completato.")
    print("Apri l'app: al primo sync il desktop pulla tutto dal server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
