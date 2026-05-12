"""Uploader della coda offline con logica di ID Swap (Estrazione Robusta)."""
from __future__ import annotations
import json
from typing import List
from sqlalchemy import text
from sqlalchemy.engine import Engine
from api_client import ApiClient, ApiError, NetworkError, NotAuthenticatedError
from app_logging import get_logger
from pending_types import EntityType, OpType, ENTITY_TO_TABLE
from config import CONFIG

log = get_logger(__name__)

def _payload_summary(entity_type: str, payload: dict) -> str:
    """Riassunto leggibile del payload per messaggi all'utente."""
    if not payload:
        return ""
    for key in ("nome", "nome_prodotto", "codice", "data_trattamento"):
        v = payload.get(key)
        if v:
            return str(v)
    return ""


def _list_pending(engine: Engine) -> List[dict]:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, entity_type, operation_type, entity_id, payload_json, "
            "       retry_count "
            "FROM pending_operations ORDER BY id ASC"
        )).fetchall()
    return [dict(r._mapping) for r in rows]


def _move_to_dead_letter(engine: Engine, op: dict, error: str, notifier=None) -> None:
    """Sposta una pending op fallita definitivamente in dead_letter_operations."""
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO dead_letter_operations
                (original_id, entity_type, operation_type, entity_id, payload_json, retry_count, last_error)
            VALUES (:oid, :et, :ot, :eid, :pj, :rc, :err)
        """), {
            "oid": op["id"], "et": op["entity_type"], "ot": op["operation_type"],
            "eid": op["entity_id"], "pj": op.get("payload_json"),
            "rc": op.get("retry_count", 0), "err": error[:500],
        })
        conn.execute(text("DELETE FROM pending_operations WHERE id = :id"), {"id": op["id"]})
    log.warning("Op %s spostata in dead-letter dopo %d retry: %s",
                op["id"], op.get("retry_count", 0), error)
    if notifier is not None:
        notifier.warning(
            f"{op['entity_type']}/{op['operation_type']} #{op.get('entity_id')} "
            f"fallita {op.get('retry_count', 0) + 1} volte: spostata in dead-letter"
        )

def _delete_op(engine: Engine, op_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pending_operations WHERE id = :id"), {"id": op_id})

def upload_pending(api: ApiClient, engine: Engine, notifier=None) -> tuple[int, int, bool]:
    if not api.is_authenticated:
        return (0, 0, False)

    sent_ok = 0
    had_insert = False
    pending = _list_pending(engine)

    table_map = ENTITY_TO_TABLE

    for op in pending:
        et, ot, eid = op["entity_type"], op["operation_type"], op["entity_id"]
        payload = json.loads(op["payload_json"]) if op.get("payload_json") else {}

        try:
            resp = None

            # --- ESECUZIONE OPERAZIONI PER TUTTE LE ENTITÀ ---
            if et == "AZIENDA":
                if ot == "INSERT": resp = api.create_azienda(payload.get("nome"))
                elif ot == "UPDATE": api.update_azienda(eid, payload.get("nome"))
                elif ot == "DELETE": api.delete_azienda(eid)
            elif et == "AGRO":
                if ot == "INSERT": resp = api.create_agro(payload.get("azienda_id"), payload.get("nome"))
                elif ot == "UPDATE": api.update_agro(eid, payload.get("azienda_id"), payload.get("nome"))
                elif ot == "DELETE": api.delete_agro(eid)
            elif et == "CONTRADA":
                if ot == "INSERT": resp = api.create_contrada(payload.get("agro_id"), payload.get("nome"))
                elif ot == "UPDATE": api.update_contrada(eid, payload.get("agro_id"), payload.get("nome"))
                elif ot == "DELETE": api.delete_contrada(eid)
            elif et == "TENDONE":
                if ot == "INSERT": resp = api.create_tendone(payload.get("contrada_id"), payload.get("codice"), payload.get("ettari"))
                elif ot == "UPDATE": api.update_tendone(eid, payload.get("contrada_id"), payload.get("codice"), payload.get("ettari"))
                elif ot == "DELETE": api.delete_tendone(eid)
            elif et == "PRODOTTO":
                if ot == "INSERT": resp = api.create_prodotto(payload)
                elif ot == "UPDATE": api.update_prodotto(eid, payload)
                elif ot == "DELETE": api.delete_prodotto(eid)
            elif et == "TRATTAMENTO":
                if ot == "INSERT":
                    resp = api.create_trattamento(payload)
                elif ot == "UPDATE":
                    # Bug fix C5: re-leggi is_autorizzato dal DB locale al momento
                    # dell'upload. Se nel frattempo reconcile ha portato giù un
                    # valore aggiornato dal server (es. mobile ha autorizzato),
                    # rispettiamo quel valore invece di sovrascriverlo con il
                    # vecchio. Necessario per backend PUT-replace.
                    with engine.connect() as conn:
                        is_aut = conn.execute(text(
                            "SELECT is_autorizzato FROM trattamenti WHERE id = :id"
                        ), {"id": eid}).scalar()
                    if is_aut is not None:
                        payload["is_autorizzato"] = int(is_aut)
                    api.update_trattamento(eid, payload)
                elif ot == "DELETE": api.delete_trattamento(eid)
                elif ot == "AUTORIZZA": api.autorizza_trattamento(eid)
                elif ot == "REVOCA": api.revoca_trattamento(eid)
            elif et == "MOVIMENTO":
                if ot == "INSERT": resp = api.create_movimento(payload)
                elif ot == "UPDATE": api.update_movimento(eid, payload)
                elif ot == "DELETE": api.delete_movimento(eid)

            # === LOGICA DI ID SWAP ROBUSTA ===
            if ot == "INSERT":
                had_insert = True
                new_id = None

                # 1. Tentiamo di estrarre l'ID in vari formati
                if isinstance(resp, dict):
                    new_id = resp.get("id")
                elif hasattr(resp, "json") and callable(resp.json): # Oggetto Response di requests
                    try:
                        new_id = resp.json().get("id")
                    except Exception:
                        pass
                elif hasattr(resp, "id"): # Modello Pydantic o custom
                    new_id = getattr(resp, "id")

                # 2. Applichiamo lo Swap se abbiamo trovato l'ID
                table = table_map.get(et)
                if new_id is not None:
                    # Confronto come stringhe per bypassare differenze int/str
                    if str(new_id) != str(eid) and table:
                        with engine.begin() as conn:
                            # Differisci i controlli FK al commit della transazione.
                            # Necessario perché UPDATE trattamenti SET id=:new fa
                            # un check immediato su dettaglio_trattamenti (FK
                            # senza ON UPDATE CASCADE), che fallirebbe. Con
                            # defer_foreign_keys=TRUE, il check avviene al COMMIT,
                            # quando abbiamo già propagato gli aggiornamenti su
                            # tutti i figli (dettaglio_trattamenti, avvisi, ecc).
                            conn.execute(text("PRAGMA defer_foreign_keys = TRUE"))
                            conn.execute(text("PRAGMA foreign_keys = ON"))

                            # Disabilita i trigger (già presente)
                            conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))

                            # --- INIZIO FIX COLLISIONE ---
                            # Controlla se il nuovo ID è già occupato da un altro record locale in attesa
                            conflitto = conn.execute(text(f"SELECT id FROM {table} WHERE id = :new"), {"new": new_id}).scalar()
                            if conflitto is not None:
                                # Trova un ID negativo per "parcheggiare" temporaneamente il record che intralcia
                                temp_id = conn.execute(text(f"SELECT MIN(id) - 1 FROM {table}")).scalar()
                                if temp_id is None or temp_id >= 0: temp_id = -1

                                # Sposta il record intralciante in parcheggio
                                conn.execute(text(f"UPDATE {table} SET id = :temp WHERE id = :conf"), {"temp": temp_id, "conf": conflitto})

                                # Aggiorna le Foreign Key figlie (se applicabile)
                                if et == "TRATTAMENTO":
                                    conn.execute(text("UPDATE dettaglio_trattamenti SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": eid})
                                    conn.execute(text("UPDATE avvisi_trattamenti SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": eid})
                                    # Questa riga è fondamentale se avviene una collisione:
                                    conn.execute(text("UPDATE registro_magazzino SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": eid})
                                elif et == "CONTRADA":
                                    conn.execute(text("UPDATE tendoni SET contrada_id = :temp WHERE contrada_id = :conf"), {"temp": temp_id, "conf": conflitto})
                                elif et == "AGRO":
                                    conn.execute(text("UPDATE contrade SET agro_id = :temp WHERE agro_id = :conf"), {"temp": temp_id, "conf": conflitto})
                                elif et == "AZIENDA":
                                    conn.execute(text("UPDATE agri SET azienda_id = :temp WHERE azienda_id = :conf"), {"temp": temp_id, "conf": conflitto})

                                # Aggiorna la coda del database
                                conn.execute(text("UPDATE pending_operations SET entity_id = :temp WHERE entity_type = :et AND entity_id = :conf"), {"temp": temp_id, "et": et, "conf": conflitto})

                                # Aggiorna la coda in memoria
                                for future_op in pending:
                                    if future_op["entity_type"] == et and str(future_op["entity_id"]) == str(conflitto):
                                        future_op["entity_id"] = temp_id

                                log.warning("Evitata collisione: record locale spostato in parcheggio (%s → %s)", conflitto, temp_id)
                            # --- FINE FIX COLLISIONE ---

                            # Aggiorna ID testata (ora lo slot è sicuramente libero!)
                            conn.execute(text(f"UPDATE {table} SET id = :new WHERE id = :old"), {"new": new_id, "old": eid})

                            # Aggiorna figli (Trattamenti)
                            if et == "TRATTAMENTO":
                                conn.execute(text("UPDATE dettaglio_trattamenti SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": eid})
                                conn.execute(text("UPDATE avvisi_trattamenti SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": eid})

                                # Aggiorna il legame numerico (Indispensabile per il CASCADE)
                                conn.execute(text("UPDATE registro_magazzino SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": eid})

                                # AGGIORNA ANCHE LA NOTA (Per coerenza visiva nel registro)
                                conn.execute(text("UPDATE registro_magazzino SET note = 'Scarico automatico T#' || :new WHERE note = 'Scarico automatico T#' || :old"), {"new": new_id, "old": eid})

                            # Aggiorna figli (Contrade -> Tendoni)
                            elif et == "CONTRADA":
                                conn.execute(text("UPDATE tendoni SET contrada_id = :new WHERE contrada_id = :old"), {"new": new_id, "old": eid})

                            # Aggiorna altre operazioni in coda
                            conn.execute(text(
                                "UPDATE pending_operations SET entity_id = :new "
                                "WHERE entity_type = :et AND entity_id = :old"
                            ), {"new": new_id, "et": et, "old": eid})

                            # Riabilita trigger
                            conn.execute(text("PRAGMA foreign_keys = ON"))
                            conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
                            log.info("ID swap %s: %s → %s", et, eid, new_id)

                            # Aggiorna anche le operazioni successive nella lista in memoria
                            for future_op in pending:
                                if future_op["entity_type"] == et and str(future_op["entity_id"]) == str(eid):
                                    future_op["entity_id"] = new_id
                            # --------------------------------
                    else:
                        log.debug("ID coincidente %s: %s (nessuno swap necessario)", et, eid)
                else:
                    # SE ARRIVIAMO QUI, L'API NON HA RESTITUITO UN FORMATO VALIDO
                    log.error("ID swap fallito: il server ha salvato ma non sono riuscito ad estrarre l'ID. resp=%r", resp)

            _delete_op(engine, op["id"])
            sent_ok += 1

        except NotAuthenticatedError:
            # 401/403: il token JWT è scaduto o revocato. Non ha senso continuare
            # né incrementare retry_count: tutte le op restanti darebbero lo stesso
            # errore. Lasciamo l'op corrente in coda così sarà ritentata dopo il
            # nuovo login, e propaghiamo l'eccezione al chiamante perché triggeri
            # un re-login (gestito in MainWindow._handle_session_expired).
            log.warning("Token JWT scaduto/non valido; interrompo l'upload")
            raise

        except ApiError as e:
            # --- PROTEZIONE DA DATABASE LOCKED ---
            # Se l'errore contiene la parola "locked", significa che la UI sta scrivendo.
            # In questo caso non dobbiamo cancellare l'operazione, ma solo aspettare.
            if "locked" in str(e).lower():
                log.debug("Database occupato (lock), riprovo al prossimo ciclo")
                # 'break' interrompe il ciclo for delle operazioni pendenti.
                # L'operazione corrente RIMANE nella tabella pending_operations.
                break

            # Se arriviamo qui, non è un lock, quindi è un errore reale dell'API o dei dati
            status = getattr(e, 'status_code', None)
            log.warning("Errore su op %s (%s/%s) status=%s: %s", op['id'], et, ot, status, e)

            if status == 404 and ot in ("UPDATE", "DELETE"):
                # Il record non esiste più sul server (cancellato da altra istanza):
                # rimuoviamo anche localmente. La pending op va scartata.
                table = table_map.get(et)
                if table:
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
                        if et == "TRATTAMENTO":
                            conn.execute(text("DELETE FROM dettaglio_trattamenti WHERE trattamento_id = :id"), {"id": eid})
                            conn.execute(text("DELETE FROM avvisi_trattamenti WHERE trattamento_id = :id"), {"id": eid})
                            conn.execute(text("DELETE FROM registro_magazzino WHERE trattamento_id = :id"), {"id": eid})
                        conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": eid})
                        conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
                        log.info("Fantasma rimosso localmente (record server gone): %s #%s", et, eid)
                if notifier is not None:
                    notifier.on_record_gone(et, int(eid) if eid is not None else 0, ot)
                _delete_op(engine, op["id"])

            elif ot == "INSERT" and status is not None and 400 <= status < 500:
                # INSERT rifiutato dal server con un 4xx persistente (es. duplicato,
                # validazione fallita). Non ha senso ritentare: cancelliamo sia la
                # pending op che il record locale per evitare loop infiniti.
                table = table_map.get(et)
                if table:
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
                        if et == "TRATTAMENTO":
                            conn.execute(text("DELETE FROM dettaglio_trattamenti WHERE trattamento_id = :id"), {"id": eid})
                            conn.execute(text("DELETE FROM avvisi_trattamenti WHERE trattamento_id = :id"), {"id": eid})
                            conn.execute(text("DELETE FROM registro_magazzino WHERE trattamento_id = :id"), {"id": eid})
                        conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": eid})
                        conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
                        log.warning("INSERT rifiutato dal server (%s): record locale %s #%s rimosso", status, et, eid)
                if notifier is not None:
                    summary = _payload_summary(et, payload)
                    notifier.on_insert_rejected(et, summary, status)
                _delete_op(engine, op["id"])

            else:
                # Errori "transienti" (5xx, 422 random, ecc): incrementa retry_count.
                # Oltre il max → dead-letter. Mantiene la pending op nella coda
                # (non rotazione: l'ordine cronologico ha senso).
                current_retry = int(op.get("retry_count", 0) or 0)
                new_retry = current_retry + 1
                if new_retry >= CONFIG.pending_retry_max:
                    _move_to_dead_letter(engine, op, str(e), notifier=notifier)
                else:
                    log.info("Op %s in retry %d/%d (status=%s)",
                             op['id'], new_retry, CONFIG.pending_retry_max, status)
                    with engine.begin() as conn:
                        conn.execute(text(
                            "UPDATE pending_operations SET retry_count = :rc, last_error = :err "
                            "WHERE id = :id"
                        ), {"rc": new_retry, "err": str(e)[:500], "id": op["id"]})

        except (NetworkError, NotAuthenticatedError):
            # Errori di rete o sessione: fermiamo tutto e riproveremo tra 2 secondi
            break

    return (sent_ok, len(_list_pending(engine)), had_insert)
