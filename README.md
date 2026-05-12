# AgriMessina QDC — Desktop (versione client REST)

App desktop migrata da accesso diretto a MySQL → **client REST puro** verso il backend FastAPI.

## Architettura

```
┌─────────────────────┐    HTTPS    ┌──────────────────┐    SQL    ┌──────────┐
│  App Desktop        │ ──REST───►  │  Backend API     │ ─pymysql─►│  MySQL   │
│  (PyQt6 + SQLite)   │             │  (FastAPI)       │           │   DB     │
└─────────────────────┘             └──────────────────┘           └──────────┘
       ▲                                     │
       │  ●   ●   ●   ●                      │
       └────────────────────── sync incrementale ─┘
       SQLite locale per offline-first
```

L'app **non parla mai direttamente con MySQL**. Tutte le scritture passano per
il backend; il SQLite locale è una cache per le letture veloci e il funzionamento
offline.

## Setup

### Requisiti
```bash
pip install PyQt6 SQLAlchemy httpx python-dotenv
```

### Configurazione
Crea un file `.env` nella stessa cartella di `main.py`:
```
API_BASE_URL=https://api.agrimessina.it
```

Per testing in locale puoi puntare al backend locale:
```
API_BASE_URL=http://127.0.0.1:8000
```

### Avvio
```bash
python main.py
```

Al primo avvio:
1. Splash
2. Login (richiede email + password — ATTENZIONE: il backend autentica per **email**, non username)
3. Sync iniziale (download di tutto)
4. Finestra principale

Ai successivi avvii il token è già salvato in `~/.agrimessina/auth.json`, quindi l'app entra direttamente.

### File creati a runtime
- `~/.agrimessina/auth.json` → token JWT + ruolo
- `~/.agrimessina/local.db` → SQLite cache
- `~/.agrimessina/` → cartella di config

## Come funziona la sync

### Letture
Tutto avviene da `~/.agrimessina/local.db`. È un mirror dello schema MySQL del backend, popolato automaticamente al login e ad ogni "Sincronizza".

### Scritture
Le UI esistenti (`ui_anagrafiche.py`, `ui_magazzino.py`, ecc.) **scrivono direttamente al SQLite** con SQL grezzo, **senza modifiche**. È la stessa cosa che facevano prima quando puntavano a MySQL: stesso schema, stesso codice.

Cosa cambia rispetto a prima: dei **trigger SQLite** (definiti in `local_db.py`) intercettano ogni `INSERT/UPDATE/DELETE` su `aziende`, `agri`, `contrade`, `tendoni`, `prodotti`, `registro_magazzino` e accodano l'operazione nella tabella `pending_operations`.

Al successivo "Sincronizza" o riavvio:
1. `pending_uploader.py` legge la coda e chiama l'API per ogni operazione
2. `sync.py` scarica i nuovi dati dal server
3. La UI viene rinfrescata

### ID server-generated
Quando crei un'azienda nuova, il SQLite locale le assegna un id `42`, ma il server al `POST /aziende` le darà l'id reale `17`. Per riallineare, dopo ogni upload con INSERT l'app forza un `reset_sync_state` + sync completa. Questo non è ottimale per la rete (ridownload tutto), ma è semplice e robusto.

## Trattamenti — gestione speciale

I trattamenti sono diversi: hanno relazione testata-dettagli, e l'API li accetta come unico oggetto JSON con `dettagli` array. Per questo NON ci sono trigger SQLite su `trattamenti`/`dettaglio_trattamenti`.

**Già implementato**: `ui_trattamenti.py` chiama `enqueue_operation()` nei 5 punti di scrittura:

| Funzione | Operazione accodata |
|---|---|
| Salvataggio nuovo trattamento (`DialogNuovoTrattamento.salva`) | `INSERT` con tutti i dettagli |
| Compensa disavanzo (`DialogCompensaDisavanzo.salva`) | `UPDATE` trattamento origine + `INSERT/UPDATE` trattamento destinazione |
| Eliminazione singola | `DELETE` per trattamento + `DELETE` per ogni figlio bilanciamento |
| Eliminazione multipla | `DELETE` per ogni trattamento selezionato + figli |
| Annulla bilanciamento (`_annulla_bilanciamento`) | `UPDATE` per ogni trattamento toccato (con i dettagli di bilanciamento rimossi) |

La helper `_build_trattamento_payload(engine, trattamento_id)` (nelle prime righe del file) costruisce il payload JSON completo nel formato atteso dall'API leggendo testata + dettagli dal SQLite locale.

## File del progetto

```
agrimessina_desktop/
├── main.py                  ← NUOVO: entry point con login dialog
├── api_client.py            ← NUOVO: wrapper httpx, gestione token, decode JWT
├── local_db.py              ← NUOVO: SQLite locale + trigger di accodamento
├── sync.py                  ← NUOVO: sync incrementale via since=server_time
├── pending_uploader.py      ← NUOVO: upload delle modifiche locali
├── login_dialog.py          ← NUOVO: finestra di login PyQt6 (con QThread)
│
├── ui_core.py               ← invariato (tuo codice esistente)
├── ui_anagrafiche.py        ← invariato (tuo codice esistente)
├── ui_magazzino.py          ← invariato (tuo codice esistente)
└── ui_trattamenti.py        ← invariato (tuo codice esistente, ma vedi sezione "Trattamenti")
```

I file vecchi `database.py` (init MySQL) e il vecchio `main.py` (con engine MySQL) **non servono più**: il nuovo `main.py` li sostituisce e `local_db.py` rimpiazza `database.py`.

## Limiti conosciuti / TODO

1. **Avvisi** (`ricalcola_avvisi_globali` del vecchio `database.py`) non sono più automatici. La logica è complessa e specifica del SQL MySQL. O la porti sul backend (preferibile, così tutti i client la vedono uguale), o la riadatti per SQLite locale chiamandola dopo `sync_all`.
2. **Trattamenti** — vedi sezione sopra.
3. **Conflict resolution** — se due dispositivi modificano lo stesso record, vince l'ultimo che chiama il server (last-write-wins). Per il tuo caso d'uso è sufficiente.
4. **Nessun re-login automatico** quando il token JWT scade. L'app mostra un errore e l'utente deve riavviare.

## Risoluzione problemi

**"Impossibile contattare il server"**:
- Verifica `API_BASE_URL` nel `.env`
- Prova a curl-are il backend: `curl https://api.agrimessina.it/health`

**"Credenziali non valide"**:
- Il backend cerca per **email**, non per username
- Verifica con il backend admin che l'utente esista nella tabella `users`

**Reset completo dei dati locali**:
```bash
rm -rf ~/.agrimessina
```
Al prossimo avvio sarà come una prima installazione.
