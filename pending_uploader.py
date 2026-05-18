"""Uploader della coda offline con logica di ID Swap (Estrazione Robusta)."""
from __future__ import annotations
import json
from typing import Any, List, Optional
from sqlalchemy import text
from sqlalchemy.engine import Engine
from api_client import ApiClient, ApiError, NetworkError, NotAuthenticatedError
from app_logging import get_logger
from pending_types import ENTITY_TO_TABLE
from config import CONFIG

log = get_logger(__name__)


# ---- Helper di basso livello ------------------------------------------------

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


def _extract_new_id(resp: Any) -> Optional[int]:
    """Estrae l'ID assegnato dal server dalla risposta del create_*.
    Accetta dict, Response, o oggetto con attributo `.id`."""
    if isinstance(resp, dict):
        return resp.get("id")
    if hasattr(resp, "json") and callable(getattr(resp, "json")):
        try:
            return resp.json().get("id")
        except Exception:
            return None
    if hasattr(resp, "id"):
        return getattr(resp, "id")
    return None


def _cleanup_local_record(conn, et: str, eid) -> None:
    """Rimuove un record locale e le sue dipendenze (dettagli, avvisi, registro).
    Da chiamare DENTRO una transazione con _sync_flags.downloading=1 già attivo."""
    if et == "TRATTAMENTO":
        conn.execute(text("DELETE FROM dettaglio_trattamenti WHERE trattamento_id = :id"), {"id": eid})
        conn.execute(text("DELETE FROM avvisi_trattamenti WHERE trattamento_id = :id"), {"id": eid})
        conn.execute(text("DELETE FROM registro_magazzino WHERE trattamento_id = :id"), {"id": eid})
    table = ENTITY_TO_TABLE.get(et)
    if table:
        conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": eid})


def _delete_record_with_flag(engine: Engine, et: str, eid) -> None:
    """Cancella localmente un record e i suoi figli, sopprimendo i trigger di enqueue."""
    table = ENTITY_TO_TABLE.get(et)
    if not table:
        return
    with engine.begin() as conn:
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))
        _cleanup_local_record(conn, et, eid)
        conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))


# ---- Dispatch API -----------------------------------------------------------

def _call_api(api: ApiClient, engine: Engine, op: dict, payload: dict):
    """Esegue la chiamata API per (entity_type, operation_type). Ritorna `resp`
    (None per operazioni che non producono ID, dict/oggetto per INSERT)."""
    et, ot, eid = op["entity_type"], op["operation_type"], op["entity_id"]

    if et == "AZIENDA":
        if ot == "INSERT": return api.create_azienda(payload.get("nome"))
        if ot == "UPDATE": api.update_azienda(eid, payload.get("nome")); return None
        if ot == "DELETE": api.delete_azienda(eid); return None
    elif et == "AGRO":
        if ot == "INSERT": return api.create_agro(payload.get("azienda_id"), payload.get("nome"))
        if ot == "UPDATE": api.update_agro(eid, payload.get("azienda_id"), payload.get("nome")); return None
        if ot == "DELETE": api.delete_agro(eid); return None
    elif et == "CONTRADA":
        if ot == "INSERT": return api.create_contrada(payload.get("agro_id"), payload.get("nome"))
        if ot == "UPDATE": api.update_contrada(eid, payload.get("agro_id"), payload.get("nome")); return None
        if ot == "DELETE": api.delete_contrada(eid); return None
    elif et == "TENDONE":
        if ot == "INSERT": return api.create_tendone(payload.get("contrada_id"), payload.get("codice"), payload.get("ettari"))
        if ot == "UPDATE": api.update_tendone(eid, payload.get("contrada_id"), payload.get("codice"), payload.get("ettari")); return None
        if ot == "DELETE": api.delete_tendone(eid); return None
    elif et == "PRODOTTO":
        if ot == "INSERT": return api.create_prodotto(payload)
        if ot == "UPDATE": api.update_prodotto(eid, payload); return None
        if ot == "DELETE": api.delete_prodotto(eid); return None
    elif et == "TRATTAMENTO":
        if ot == "INSERT":
            return api.create_trattamento(payload)
        if ot == "UPDATE":
            # Re-leggi is_autorizzato dal DB locale: se reconcile ha portato giù
            # un valore aggiornato dal server (es. mobile ha autorizzato), non
            # vogliamo sovrascriverlo col vecchio (backend è PUT-replace).
            with engine.connect() as conn:
                is_aut = conn.execute(text(
                    "SELECT is_autorizzato FROM trattamenti WHERE id = :id"
                ), {"id": eid}).scalar()
            if is_aut is not None:
                payload["is_autorizzato"] = int(is_aut)
            api.update_trattamento(eid, payload)
            return None
        if ot == "DELETE": api.delete_trattamento(eid); return None
        if ot == "AUTORIZZA": api.autorizza_trattamento(eid); return None
        if ot == "REVOCA": api.revoca_trattamento(eid); return None
    elif et == "MOVIMENTO":
        if ot == "INSERT": return api.create_movimento(payload)
        if ot == "UPDATE": api.update_movimento(eid, payload); return None
        if ot == "DELETE": api.delete_movimento(eid); return None
    elif et == "OPERAZIONE":
        # Multi-prodotto atomico: il payload qui è un wrapper
        # {"operazione": OperazioneIn, "local_ids": [...]}; mandiamo al server
        # solo `operazione`, mentre `local_ids` serve allo swap post-success.
        if ot == "INSERT":
            return api.create_operazione(payload.get("operazione") or {})
        if ot == "DELETE":
            op_id = payload.get("operazione_id")
            if op_id is not None:
                api.delete_operazione(int(op_id))
            return None

    return None


# ---- ID swap ----------------------------------------------------------------

def _rebind_child_fks(conn, et: str, old_id, new_id) -> None:
    """Aggiorna le FK figlie dopo lo swap dell'ID di un padre."""
    if et == "TRATTAMENTO":
        conn.execute(text("UPDATE dettaglio_trattamenti SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": old_id})
        conn.execute(text("UPDATE avvisi_trattamenti SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": old_id})
        conn.execute(text("UPDATE registro_magazzino SET trattamento_id = :new WHERE trattamento_id = :old"), {"new": new_id, "old": old_id})
    elif et == "CONTRADA":
        conn.execute(text("UPDATE tendoni SET contrada_id = :new WHERE contrada_id = :old"), {"new": new_id, "old": old_id})


def _park_conflict(conn, et: str, table: str, conflict_id, pending: List[dict]) -> None:
    """Sposta in parcheggio (ID negativo) un record locale che occupa già il
    nuovo ID assegnato dal server. Aggiorna anche le FK figlie e la coda."""
    temp_id = conn.execute(text(f"SELECT MIN(id) - 1 FROM {table}")).scalar()
    if temp_id is None or temp_id >= 0:
        temp_id = -1

    conn.execute(text(f"UPDATE {table} SET id = :temp WHERE id = :conf"), {"temp": temp_id, "conf": conflict_id})

    if et == "TRATTAMENTO":
        conn.execute(text("UPDATE dettaglio_trattamenti SET trattamento_id = :temp WHERE trattamento_id = :conf"), {"temp": temp_id, "conf": conflict_id})
        conn.execute(text("UPDATE avvisi_trattamenti SET trattamento_id = :temp WHERE trattamento_id = :conf"), {"temp": temp_id, "conf": conflict_id})
        conn.execute(text("UPDATE registro_magazzino SET trattamento_id = :temp WHERE trattamento_id = :conf"), {"temp": temp_id, "conf": conflict_id})
    elif et == "CONTRADA":
        conn.execute(text("UPDATE tendoni SET contrada_id = :temp WHERE contrada_id = :conf"), {"temp": temp_id, "conf": conflict_id})
    elif et == "AGRO":
        conn.execute(text("UPDATE contrade SET agro_id = :temp WHERE agro_id = :conf"), {"temp": temp_id, "conf": conflict_id})
    elif et == "AZIENDA":
        conn.execute(text("UPDATE agri SET azienda_id = :temp WHERE azienda_id = :conf"), {"temp": temp_id, "conf": conflict_id})

    conn.execute(text("UPDATE pending_operations SET entity_id = :temp WHERE entity_type = :et AND entity_id = :conf"),
                 {"temp": temp_id, "et": et, "conf": conflict_id})

    for future_op in pending:
        if future_op["entity_type"] == et and str(future_op["entity_id"]) == str(conflict_id):
            future_op["entity_id"] = temp_id

    log.warning("Evitata collisione: record locale spostato in parcheggio (%s → %s)", conflict_id, temp_id)


def _apply_swap_inner(conn, et: str, table: str, old_id, new_id,
                      pending: List[dict]) -> None:
    """Inner swap logic. Il chiamante deve aver già:
      - aperto una transazione
      - settato PRAGMA defer_foreign_keys
      - settato _sync_flags.downloading=1
    NON cancella la pending op (responsabilità del chiamante)."""
    conflict = conn.execute(text(f"SELECT id FROM {table} WHERE id = :new"), {"new": new_id}).scalar()
    if conflict is not None:
        _park_conflict(conn, et, table, conflict, pending)

    # Aggiorna ID testata (slot ora libero) e FK figlie
    conn.execute(text(f"UPDATE {table} SET id = :new WHERE id = :old"), {"new": new_id, "old": old_id})
    _rebind_child_fks(conn, et, old_id, new_id)

    if et == "TRATTAMENTO":
        # Coerenza visiva delle note di scarico automatico
        conn.execute(text("UPDATE registro_magazzino SET note = 'Scarico automatico T#' || :new WHERE note = 'Scarico automatico T#' || :old"),
                     {"new": new_id, "old": old_id})

    # Rimappa le pending op successive che riferivano il vecchio ID
    conn.execute(text(
        "UPDATE pending_operations SET entity_id = :new "
        "WHERE entity_type = :et AND entity_id = :old"
    ), {"new": new_id, "et": et, "old": old_id})

    for future_op in pending:
        if future_op["entity_type"] == et and str(future_op["entity_id"]) == str(old_id):
            future_op["entity_id"] = new_id

    log.info("ID swap %s: %s → %s", et, old_id, new_id)


def _apply_id_swap(engine: Engine, et: str, table: str, old_id, new_id,
                   op_id: int, pending: List[dict]) -> None:
    """Esegue lo swap completo dell'ID locale → ID server e cancella la pending
    op nella STESSA transazione. La transazionalità è critica: senza, un crash
    fra commit dello swap e DELETE della pending op causerebbe un INSERT duplicato
    sul server al retry."""
    with engine.begin() as conn:
        # Defer FK al commit: UPDATE trattamenti SET id=:new altrimenti farebbe
        # check immediato su dettaglio_trattamenti (FK senza ON UPDATE CASCADE).
        conn.execute(text("PRAGMA defer_foreign_keys = TRUE"))
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))

        _apply_swap_inner(conn, et, table, old_id, new_id, pending)

        conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
        # DELETE atomico della pending op nella stessa transazione dello swap
        conn.execute(text("DELETE FROM pending_operations WHERE id = :pid"), {"pid": op_id})


def _apply_operazione_swap(engine: Engine, local_ids: list, server_trattamenti: list,
                           op_id: int, pending: List[dict]) -> None:
    """Multi-swap atomico per OPERAZIONE INSERT.

    Il server restituisce `trattamenti[]` (in ordine = local_ids[i]). Per ogni
    coppia (local_id, server.id) eseguiamo lo swap dei trattamenti col loro
    cascade FK; poi popoliamo `operazione_numero`/`operazione_id` sui record
    (ora identificati dai server ID). Tutto in UNA transazione, così un crash
    a metà non lascia trattamenti mezzo-swappati.
    """
    with engine.begin() as conn:
        conn.execute(text("PRAGMA defer_foreign_keys = TRUE"))
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(text("UPDATE _sync_flags SET value = 1 WHERE key = 'downloading'"))

        for local_id, st in zip(local_ids, server_trattamenti):
            sid = st.get("id")
            if sid is None or str(sid) == str(local_id):
                continue
            _apply_swap_inner(conn, "TRATTAMENTO", "trattamenti", local_id, sid, pending)

        # Aggiorna operazione_id su tutti i trattamenti del bundle (dopo
        # swap, gli ID locali nella tabella corrispondono ora a quelli del
        # server). Il server ha allocato un operazione_id condiviso.
        for st in server_trattamenti:
            sid = st.get("id")
            if sid is None:
                continue
            conn.execute(text(
                "UPDATE trattamenti SET operazione_id = :u WHERE id = :id"
            ), {
                "u": st.get("operazione_id"),
                "id": sid,
            })

        conn.execute(text("UPDATE _sync_flags SET value = 0 WHERE key = 'downloading'"))
        conn.execute(text("DELETE FROM pending_operations WHERE id = :pid"), {"pid": op_id})


# ---- Error handling ---------------------------------------------------------

def _handle_record_gone(engine: Engine, et: str, eid, ot: str, op_id: int,
                        notifier) -> None:
    """Server ha risposto 404 su UPDATE/DELETE: rimuove anche dal locale."""
    _delete_record_with_flag(engine, et, eid)
    log.info("Fantasma rimosso localmente (record server gone): %s #%s", et, eid)
    if notifier is not None:
        notifier.on_record_gone(et, int(eid) if eid is not None else 0, ot)
    _delete_op(engine, op_id)


def _handle_insert_rejected(engine: Engine, et: str, eid, status: int,
                            payload: dict, op_id: int, notifier) -> None:
    """Server ha respinto un INSERT con 4xx persistente: cancella sia la
    pending op che il record locale per evitare loop infiniti."""
    _delete_record_with_flag(engine, et, eid)
    log.warning("INSERT rifiutato dal server (%s): record locale %s #%s rimosso", status, et, eid)
    if notifier is not None:
        notifier.on_insert_rejected(et, _payload_summary(et, payload), status)
    _delete_op(engine, op_id)


def _handle_transient_error(engine: Engine, op: dict, error: str, status,
                            notifier) -> None:
    """Errori transienti (5xx, 422 random): incrementa retry_count; oltre il
    max sposta in dead-letter."""
    current_retry = int(op.get("retry_count", 0) or 0)
    new_retry = current_retry + 1
    if new_retry >= CONFIG.pending_retry_max:
        _move_to_dead_letter(engine, op, error, notifier=notifier)
    else:
        log.info("Op %s in retry %d/%d (status=%s)",
                 op['id'], new_retry, CONFIG.pending_retry_max, status)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE pending_operations SET retry_count = :rc, last_error = :err "
                "WHERE id = :id"
            ), {"rc": new_retry, "err": error[:500], "id": op["id"]})


# ---- Loop principale --------------------------------------------------------

def upload_pending(api: ApiClient, engine: Engine, notifier=None) -> tuple[int, int, bool]:
    if not api.is_authenticated:
        return (0, 0, False)

    sent_ok = 0
    had_insert = False
    pending = _list_pending(engine)

    for op in pending:
        et, ot, eid = op["entity_type"], op["operation_type"], op["entity_id"]
        try:
            payload = json.loads(op["payload_json"]) if op.get("payload_json") else {}
        except (json.JSONDecodeError, TypeError) as e:
            # Payload corrotto: senza catch, JSONDecodeError (ValueError) non
            # è in nessun except sotto e abortirebbe l'intero ciclo, bloccando
            # tutte le pending op successive. Sposta in dead-letter e continua.
            _move_to_dead_letter(engine, op, f"payload_json corrotto: {e}", notifier=notifier)
            continue

        try:
            resp = _call_api(api, engine, op, payload)

            pending_op_deleted = False
            # ID effettivo lato server dopo eventuale swap: per INSERT è il
            # nuovo id assegnato dal backend, per UPDATE/DELETE/AUTORIZZA/REVOCA
            # è l'eid che avevamo già (record sincronizzato in precedenza).
            server_id = eid
            server_ids_per_eco: list = []  # per OPERAZIONE: N trattamenti
            if ot == "INSERT":
                had_insert = True
                if et == "OPERAZIONE":
                    # Bundle multi-prodotto: la response ha `trattamenti[]`
                    # con N ID server. local_ids del payload (parallelo) ci
                    # dice quale local va swappato in quale server.
                    op_resp = resp if isinstance(resp, dict) else {}
                    local_ids = payload.get("local_ids", []) or []
                    server_trattamenti = op_resp.get("trattamenti", []) or []
                    if len(local_ids) != len(server_trattamenti):
                        # Mismatch grave: il server HA già salvato (siamo nel
                        # success-path della POST), ritentare duplicherebbe.
                        # Swap parziale sui primi N (corretti per ordine);
                        # cancelliamo comunque la pending op così non ritentiamo.
                        # Eventuali trattamenti orfani arriveranno via reconcile
                        # (potranno apparire come duplicati locali: ispezione
                        # manuale, ma niente data-loss server-side).
                        log.error(
                            "OPERAZIONE INSERT swap: len locali %d != len server %d — swap parziale",
                            len(local_ids), len(server_trattamenti),
                        )
                        n = min(len(local_ids), len(server_trattamenti))
                        _apply_operazione_swap(engine, local_ids[:n],
                                               server_trattamenti[:n],
                                               op["id"], pending)
                        pending_op_deleted = True
                        server_ids_per_eco = [
                            st.get("id") for st in server_trattamenti
                            if st.get("id") is not None
                        ]
                    else:
                        _apply_operazione_swap(engine, local_ids, server_trattamenti,
                                               op["id"], pending)
                        pending_op_deleted = True
                        # Per la soppressione eco SSE: prendiamo gli ID dei
                        # trattamenti creati (ognuno emette il suo evento).
                        server_ids_per_eco = [
                            st.get("id") for st in server_trattamenti
                            if st.get("id") is not None
                        ]
                else:
                    new_id = _extract_new_id(resp)
                    table = ENTITY_TO_TABLE.get(et)
                    if new_id is not None and table:
                        server_id = new_id
                        if str(new_id) != str(eid):
                            _apply_id_swap(engine, et, table, eid, new_id, op["id"], pending)
                            pending_op_deleted = True
                        else:
                            log.debug("ID coincidente %s: %s (nessuno swap necessario)", et, eid)
                    elif new_id is None:
                        log.error("ID swap fallito: il server ha salvato ma non sono riuscito ad estrarre l'ID. resp=%r", resp)

            if not pending_op_deleted:
                _delete_op(engine, op["id"])
            sent_ok += 1

            # Segna l'op come "nostra" per sopprimere l'eco SSE che arriverà
            # da lì a pochi millisecondi. Senza, il toast tray dice
            # "Trattamento #X aggiunto/eliminato da un altro client" anche
            # quando il modificatore sei tu.
            if notifier is not None and hasattr(notifier, "mark_recent_local"):
                if et == "OPERAZIONE" and server_ids_per_eco:
                    # Ogni trattamento dell'operazione genera un evento SSE.
                    for sid in server_ids_per_eco:
                        notifier.mark_recent_local("TRATTAMENTO", sid)
                else:
                    notifier.mark_recent_local(et, server_id)

        except NotAuthenticatedError:
            # 401/403: token JWT scaduto/revocato. Non incrementiamo retry_count:
            # tutte le op successive avrebbero lo stesso esito. Propaghiamo
            # al chiamante perché triggeri il re-login.
            log.warning("Token JWT scaduto/non valido; interrompo l'upload")
            raise

        except NetworkError as e:
            # NetworkError eredita da ApiError: va catturato PRIMA del clause
            # ApiError generico. Altrimenti finirebbe in _handle_transient_error
            # che incrementerebbe retry_count per ogni errore di rete, fino a
            # spedire l'op in dead-letter dopo N tick offline. Qui rompiamo
            # il for: la coda resta intatta e ritenteremo al prossimo tick.
            log.debug("Errore di rete durante upload, riprovo al prossimo ciclo: %s", e)
            break

        except ApiError as e:
            # "database locked": la UI sta scrivendo. Non cancellare l'op,
            # solo aspettare il prossimo ciclo.
            if "locked" in str(e).lower():
                log.debug("Database occupato (lock), riprovo al prossimo ciclo")
                break

            status = getattr(e, 'status_code', None)
            log.warning("Errore su op %s (%s/%s) status=%s: %s", op['id'], et, ot, status, e)

            if status == 404 and ot in ("UPDATE", "DELETE", "REVOCA", "AUTORIZZA"):
                # REVOCA/AUTORIZZA 404 = il trattamento non esiste più server-side:
                # è come UPDATE/DELETE su record già rimosso, va pulito anche localmente
                # invece di lasciarlo finire in dead-letter dopo retry inutili.
                _handle_record_gone(engine, et, eid, ot, op["id"], notifier)
            elif ot == "INSERT" and status is not None and 400 <= status < 500:
                _handle_insert_rejected(engine, et, eid, status, payload, op["id"], notifier)
            else:
                _handle_transient_error(engine, op, str(e), status, notifier)

    return (sent_ok, len(_list_pending(engine)), had_insert)
