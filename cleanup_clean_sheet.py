"""Clean sheet totale: cancella TUTTI i trattamenti e TUTTI i movimenti
di magazzino (sia automatici da trattamenti che CARICHI/SCARICHI manuali)
sia sul backend che in locale. Le anagrafiche (prodotti, aziende, agri,
contrade, tendoni) sono preservate.

USO:
    cd /home/sdh/Documents/AgriMessina/02-Progetti_Software/qdc/
    ./bin/python cleanup_clean_sheet.py

PRECONDIZIONE: l'app deve essere CHIUSA (per evitare lock su SQLite locale
e per evitare che il client sync stia girando in parallelo).

L'utente deve essere già loggato (token salvato dall'app desktop). Lo
script riusa lo stesso token.

OPERAZIONI:

  1. BACKEND
     a) Recupera tutti i trattamenti via /trattamenti?since=null.
     b) Per ognuno: DELETE /trattamenti/{id} (il backend crea tombstone
        automaticamente in `tombstones` per ogni record cancellato,
        propagandolo poi ai client al prossimo pull).
     c) Recupera tutti i movimenti via /magazzino/movimenti?since=null
        (limit alto).
     d) Per ognuno: DELETE /magazzino/movimenti/{id}.

  2. LOCALE
     a) Wipe `registro_magazzino_fittizio` (locale-only, non sincronizzato
        col backend).
     b) Wipe `pending_operations` (operazioni in coda non ancora inviate:
        ora orfane, riferirebbero record cancellati).
     c) Wipe `dead_letter_operations`.
     d) Reset dei marker `_sync_flags` di bootstrap (così al prossimo
        avvio l'app rifa il bootstrap pulito).
     e) Reset `sync_state` (forza pull completo).
     f) Reconcile finale: chiama `sync.reconcile_with_server` che
        scarica TUTTO il backend e cancella in locale tutto quello che
        sul backend non c'è (compresi i trattamenti e movimenti che
        abbiamo appena cancellato lato server).

EFFETTO FINALE:
  - Trattamenti: zero su server, zero in locale.
  - Movimenti: zero su server, zero in locale.
  - Magazzino fittizio: zero (locale).
  - Anagrafiche: invariate.
  - Altri client: alla loro prossima sync vedranno tombstones e
    cancelleranno tutto in locale.

ROLLBACK: nessuno. Operazione irreversibile. Fai un backup del DB MySQL
sul server prima di lanciare se vuoi sicurezza.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Setup path per import dei moduli dell'app
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sqlalchemy import text  # noqa: E402

import os  # noqa: E402

from api_client import ApiClient  # noqa: E402
from local_db import get_engine, init_local_database  # noqa: E402


def get_api_base_url() -> str:
    """Stessa logica di main.get_api_base_url: legge da env con default."""
    return os.environ.get("API_BASE_URL", "https://api.agrimessina.it").rstrip("/")


def conferma(msg: str) -> bool:
    print()
    print("─" * 70)
    print(msg)
    print("─" * 70)
    risposta = input("Confermi? scrivi 'WIPE' per procedere: ").strip()
    return risposta == "WIPE"


def main():
    print("┌─────────────────────────────────────────────────────────────────┐")
    print("│  CLEAN SHEET — trattamenti e movimenti magazzino                │")
    print("│  Cancella tutto su backend + locale. Le anagrafiche restano.   │")
    print("└─────────────────────────────────────────────────────────────────┘")

    if not conferma(
        "Questa operazione è IRREVERSIBILE e cancella TUTTI i trattamenti\n"
        "e TUTTI i movimenti di magazzino sul backend e su tutti i client\n"
        "(via tombstones). Le anagrafiche (prodotti, aziende, tendoni, ecc.)\n"
        "sono preservate.\n\n"
        "Prima di procedere assicurati che:\n"
        "  • l'app desktop sia CHIUSA su tutti i client\n"
        "  • tu abbia un backup del DB MySQL del server (consigliato)"
    ):
        print("Annullato.")
        sys.exit(0)

    # --- API client ---
    api = ApiClient(get_api_base_url())
    if not api.is_authenticated:
        print("\nERRORE: non sei loggato. Apri l'app, fai login, poi rilancia.")
        sys.exit(1)
    print(f"\nLogged as: {api.username} (ruolo: {api.ruolo})")
    if api.ruolo != "admin":
        print("ATTENZIONE: il ruolo non è admin. Il backend potrebbe rifiutare le DELETE.")

    # --- Backend: trattamenti ---
    print("\n[1/6] Recupero trattamenti dal backend…")
    resp = api.sync_trattamenti(since=None)
    trattamenti = resp.get("items", []) if isinstance(resp, dict) else resp
    print(f"      Trovati {len(trattamenti)} trattamenti.")
    if trattamenti:
        print("[2/6] DELETE per ogni trattamento (con tombstone automatico server-side)…")
        ok, fail = 0, 0
        for i, t in enumerate(trattamenti, 1):
            tid = t.get("id") if isinstance(t, dict) else t
            try:
                api.delete_trattamento(tid)
                ok += 1
            except Exception as e:
                print(f"      FAIL DELETE /trattamenti/{tid}: {e}")
                fail += 1
            if i % 20 == 0:
                print(f"      … {i}/{len(trattamenti)}")
        print(f"      Trattamenti: {ok} cancellati, {fail} falliti.")

    # --- Backend: movimenti ---
    print("\n[3/6] Recupero movimenti magazzino dal backend…")
    resp = api.sync_movimenti(since=None)
    movimenti = resp.get("items", []) if isinstance(resp, dict) else resp
    print(f"      Trovati {len(movimenti)} movimenti.")
    if movimenti:
        print("[4/6] DELETE per ogni movimento (con tombstone automatico)…")
        ok, fail = 0, 0
        for i, m in enumerate(movimenti, 1):
            mid = m.get("id") if isinstance(m, dict) else m
            try:
                api.delete_movimento(mid)
                ok += 1
            except Exception as e:
                print(f"      FAIL DELETE /magazzino/movimenti/{mid}: {e}")
                fail += 1
            if i % 50 == 0:
                print(f"      … {i}/{len(movimenti)}")
        print(f"      Movimenti: {ok} cancellati, {fail} falliti.")

    # --- Locale: wipe + reset marker ---
    print("\n[5/6] Pulizia DB locale…")
    engine = get_engine()
    init_local_database(engine)  # sicurezza: tabelle ci sono
    with engine.begin() as conn:
        # Disattiva i trigger per non accodare pending durante le DELETE locali.
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        try:
            # Magazzino fittizio: solo locale, va azzerato qui.
            n = conn.execute(text("DELETE FROM registro_magazzino_fittizio")).rowcount or 0
            print(f"      registro_magazzino_fittizio: {n} righe rimosse")

            # Pending operations (qualunque cosa fosse in coda è ora orfana).
            n = conn.execute(text("DELETE FROM pending_operations")).rowcount or 0
            print(f"      pending_operations: {n} righe rimosse")
            n = conn.execute(text("DELETE FROM dead_letter_operations")).rowcount or 0
            print(f"      dead_letter_operations: {n} righe rimosse")

            # Reset marker dei bootstrap così il prossimo avvio è pulito.
            for marker in ["bootstrap_due_registri_v2_done",
                           "cleanup_magazzino_v4_done",
                           "cleanup_origine_repull_done",
                           "cleanup_server_authoritative_done"]:
                conn.execute(text(
                    "DELETE FROM _sync_flags WHERE key = :k"
                ), {"k": marker})

            # Reset sync_state: forza pull completo al prossimo reconcile.
            n = conn.execute(text("DELETE FROM sync_state")).rowcount or 0
            print(f"      sync_state: {n} righe rimosse (forza pull completo)")
        finally:
            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))

    # --- Reconcile finale: scarica stato server in locale ---
    print("\n[6/6] Reconcile finale col backend (pull completo + applicazione tombstones)…")
    try:
        from sync import reconcile_with_server
        reconcile_with_server(api, engine)
        print("      Reconcile completato.")
    except Exception as e:
        print(f"      Reconcile fallito (non bloccante): {e}")
        print("      Al prossimo avvio dell'app il reconcile verrà rifatto comunque.")

    print()
    print("┌─────────────────────────────────────────────────────────────────┐")
    print("│  CLEAN SHEET COMPLETATO                                         │")
    print("│  Riapri l'app: trattamenti vuoti, magazzini a zero.            │")
    print("│  Gli altri client riceveranno tombstones al loro prossimo sync.│")
    print("└─────────────────────────────────────────────────────────────────┘")


if __name__ == "__main__":
    main()
