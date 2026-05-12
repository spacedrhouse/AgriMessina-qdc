"""Diagnostica una tantum dello stato locale del DB.

Esegui: python diagnose.py

Stampa: lista trattamenti, righe registro_magazzino con trattamento_id,
pending_operations, sync_flags. Utile per capire se ci sono trattamenti
duplicati locali, pending op in retry/dead-letter, e se i cleanup one-off
sono stati marcati come fatti.
"""
from sqlalchemy import create_engine, text
from pathlib import Path


def main():
    db_path = Path.home() / ".agrimessina" / "local.db"
    if not db_path.exists():
        print(f"DB non trovato: {db_path}")
        return

    engine = create_engine(f"sqlite:///{db_path}")

    print("=" * 70)
    print("TRATTAMENTI LOCALI (ultimi 20)")
    print("=" * 70)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT t.id, t.data_trattamento, p.nome_prodotto,
                   GROUP_CONCAT(DISTINCT az.nome) AS aziende,
                   GROUP_CONCAT(DISTINCT ten.codice) AS tendoni,
                   ROUND(SUM(dt.quantita_sostanza), 2) AS qta_totale,
                   t.is_autorizzato
            FROM trattamenti t
            LEFT JOIN prodotti p ON p.id = t.prodotto_id
            LEFT JOIN dettaglio_trattamenti dt ON dt.trattamento_id = t.id
            LEFT JOIN tendoni ten ON ten.id = dt.tendone_id
            LEFT JOIN contrade c ON c.id = ten.contrada_id
            LEFT JOIN agri ag ON ag.id = c.agro_id
            LEFT JOIN aziende az ON az.id = ag.azienda_id
            GROUP BY t.id, t.data_trattamento, p.nome_prodotto, t.is_autorizzato
            ORDER BY t.id DESC
            LIMIT 20
        """)).fetchall()
        for r in rows:
            print(f"  id={r[0]:>5}  data={r[1]}  prod={r[2]}  az={r[3]}  ten={r[4]}  qta={r[5]}  is_aut={r[6]}")

    print()
    print("=" * 70)
    print("REGISTRO MAGAZZINO (TUTTE le righe, anche con trattamento_id NULL)")
    print("=" * 70)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT rm.id, rm.trattamento_id, p.nome_prodotto,
                   az.nome AS azienda, rm.tipo_movimento, rm.quantita,
                   rm.data_movimento, rm.note
            FROM registro_magazzino rm
            LEFT JOIN prodotti p ON p.id = rm.prodotto_id
            LEFT JOIN aziende az ON az.id = rm.azienda_id
            ORDER BY rm.id DESC
            LIMIT 50
        """)).fetchall()
        if not rows:
            print("  (vuoto)")
        for r in rows:
            tid = str(r[1]) if r[1] is not None else "NULL"
            print(f"  id={r[0]:>5}  tratt={tid:>5}  prod={r[2]}  az={r[3]}  {r[4]}={r[5]}  data={r[6]}  note={r[7]!r}")

    print()
    print("=" * 70)
    print("PENDING OPERATIONS")
    print("=" * 70)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, entity_type, operation_type, entity_id, retry_count, last_error
            FROM pending_operations ORDER BY id ASC
        """)).fetchall()
        if not rows:
            print("  (vuoto)")
        for r in rows:
            err = (r[5] or "")[:80]
            print(f"  id={r[0]}  {r[1]}/{r[2]}  entity_id={r[3]}  retry={r[4]}  err={err!r}")

    print()
    print("=" * 70)
    print("DEAD LETTER OPERATIONS")
    print("=" * 70)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, entity_type, operation_type, entity_id, retry_count, last_error
            FROM dead_letter_operations ORDER BY id ASC
        """)).fetchall()
        if not rows:
            print("  (vuoto)")
        for r in rows:
            err = (r[5] or "")[:80]
            print(f"  id={r[0]}  {r[1]}/{r[2]}  entity_id={r[3]}  retry={r[4]}  err={err!r}")

    print()
    print("=" * 70)
    print("SYNC FLAGS")
    print("=" * 70)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT key, value FROM _sync_flags")).fetchall()
        for r in rows:
            print(f"  {r[0]} = {r[1]}")

    print()
    print("=" * 70)
    print("SYNC STATE (server_time per entità)")
    print("=" * 70)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT entity, server_time FROM sync_state")).fetchall()
        for r in rows:
            print(f"  {r[0]} → {r[1]}")


if __name__ == "__main__":
    main()
