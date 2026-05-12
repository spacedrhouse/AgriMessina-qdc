# AgriMessina QDC — Desktop

Client desktop per la gestione trattamenti e magazzino fitosanitari. PyQt6 +
SQLite locale + REST client verso il backend FastAPI.

---

## Architettura

```
┌────────────────────┐                         ┌──────────────────────┐
│  Desktop (PyQt6)   │  ───── HTTPS REST ────► │  Backend (FastAPI)   │
│                    │                         │                      │
│  SQLite locale     │  ◄──── SSE push ─────── │   MySQL              │
│  (cache offline)   │                         │                      │
└────────────────────┘                         └──────────────────────┘
```

Il desktop **non parla mai direttamente con MySQL**. Tutte le scritture
passano per il backend; il SQLite locale è una cache per letture veloci e
operatività offline.

### Principi

- **Server-authoritative**: la logica di business critica (scarichi magazzino,
  alias warehouse, tombstone tracking) gira sul backend. Tutti i client la
  consumano in sola lettura via `/magazzino/movimenti`.
- **Offline-first**: scritture locali vengono accodate in `pending_operations`
  e inviate quando c'è rete. UI sempre reattiva, sync in background.
- **SSE push real-time**: l'app mantiene una connessione a `/events`.
  Quando un altro client (es. app Android di un operatore in campo) crea
  un trattamento, il desktop lo vede entro 1-2 secondi. I timer di polling
  ci sono ma fungono solo da fallback offline.

---

## Setup sviluppo

### Requisiti
```bash
python3 -m venv venv
source venv/bin/activate    # Linux/macOS
# venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

### Configurazione
Il file `.env` (committato, contiene solo URL pubblico) imposta:
```env
API_BASE_URL=https://api.agrimessina.it
```

Per testing su backend locale:
```env
API_BASE_URL=http://127.0.0.1:8000
```

### Avvio
```bash
python main.py
```

Primo avvio: splash → login → sync iniziale → finestra principale.
Successivi avvii: il token JWT salvato in `~/.agrimessina/auth.json` permette
l'auto-login. Quando scade, l'app intercetta il 401 e mostra il dialog di
re-login automaticamente.

### Reset dati locali
```bash
rm -rf ~/.agrimessina
```

---

## Flusso scritture

```
[UI] → INSERT/UPDATE/DELETE locale su SQLite
        │
        ├─ trigger SQLite → riga in pending_operations
        │
        ├─ enqueue_operation() esplicito per trattamenti (UI sa serializzare)
        │
        ▼
[pending_uploader] (ogni 3 s) → POST/PUT/DELETE all'API
        │
        ▼
[Backend] applica + emette evento SSE
        │
        ▼
[Tutti i client connessi] ricevono "trattamenti_changed" / "magazzino_changed"
        │
        ▼
[Desktop] pull_trattamenti → upsert SQLite → UI si aggiorna
```

### Magazzino: solo il server calcola

Gli auto-scarichi (`SCARICO` con `trattamento_id` valorizzato) sono **sempre**
calcolati server-side da `app/magazzino_calculator.sincronizza_scarico`.
Il desktop li legge in sola lettura via `/magazzino/movimenti`. Il pulsante
"🔧 Ricalcola scarichi" sui pannelli magazzino delega a `POST /magazzino/ricalcola`.

I CARICHI manuali (con `trattamento_id` NULL) restano gestiti dalla UI desktop.

---

## Build & release (CI Windows)

Tutto via GitHub Actions `.github/workflows/build-windows.yml`.

### Fare una release
```bash
git tag v1.2.3
git push origin v1.2.3
```

Il workflow:
1. Inietta la versione in `_version.py`
2. PyInstaller `--onedir` produce `dist/AgriMessina/`
3. Inno Setup impacchetta in `installer/output/AgriMessina_Setup_1.2.3.exe`
4. Pubblica come **GitHub Release** con installer allegato

Gli utenti finali aprono l'app: l'`update_checker.py` controlla `releases/latest`
all'avvio e mostra un dialog non invasivo se c'è una versione più recente.

### Test manuale del workflow
GitHub Actions → "Build Windows Installer" → Run workflow → versione di prova.
Genera artifact CI di durata 7 giorni (non release).

---

## Struttura del repo

```
qdc/
├── main.py                  Entry point: splash, login, timer, listener SSE
├── _version.py              Versione (riscritto dal CI prima del build)
├── api_client.py            Wrapper httpx + gestione token JWT
├── login_dialog.py          Finestra di login PyQt6
├── update_checker.py        Controllo aggiornamenti GitHub Releases
├── events_listener.py       Listener SSE per push real-time dal server
│
├── local_db.py              Schema SQLite + trigger di accodamento
├── pending_uploader.py      Upload pending_operations al server
├── sync.py                  Sync incrementale + reconcile + pull_trattamenti
├── sync_notifier.py         Signal Qt per stato sync nella status bar
├── database.py              Helper avvisi locali (legacy ma usato al boot)
├── magazzino_logic.py       Migration helpers one-off (legacy)
│
├── ui_core.py               Base classes PyQt6 (PannelloBaseDialog, ecc.)
├── ui_anagrafiche.py        CRUD aziende/agri/contrade/tendoni
├── ui_magazzino.py          Pannelli "Magazzino <Azienda>" con prodotti/giacenze
├── ui_trattamenti.py        Storico/Revisionati/Bilanciamenti/Dialog
│
├── config.py                Polling intervals + WAREHOUSE_PRIORITY/ALIASES
├── pending_types.py         Tipi/enum per le pending_operations
├── app_logging.py           Setup logging file/console
├── diagnose.py              Tool diagnostico standalone
│
├── AgriMessina.spec         PyInstaller spec
├── installer/AgriMessina.iss Inno Setup script
├── codemagic.yaml           CI alternativo (non attivo)
├── .github/workflows/       GitHub Actions workflows
│
├── icona.ico, splash.png    Risorse grafiche
├── requirements.txt         Dipendenze Python
└── LICENSE                  Proprietaria, "All rights reserved"
```

---

## Configurazione CI

Le variabili (`autosync_ms`, ecc.) in `config.py` sono override-abili via env
con prefisso `QDC_`:

```bash
QDC_AUTOSYNC_MS=10000 python main.py   # polling upload ogni 10 s invece di 3
```

Vedi `config.py` per la lista completa.

---

## Troubleshooting

**"Sessione scaduta" in loop** → il backend ha rigenerato il `JWT_SECRET`
o l'utente è stato cancellato. Il client mostra il dialog di re-login; basta
fare login con credenziali valide.

**"Impossibile contattare il server"** → verifica `.env` + curl il backend:
```bash
curl https://api.agrimessina.it/health
```

**Stato SSE "Polling" in status bar** → SSE non si connette. Possibili cause:
proxy/firewall che blocca le connessioni keep-alive, oppure backend offline.
L'app continua a funzionare via polling fallback (3s upload, 30min reconcile),
solo gli update da altri client tardano qualche minuto.

**Stati incoerenti del magazzino dopo bug noti** → click "🔧 Ricalcola scarichi"
sul pannello magazzino. Triggera `POST /magazzino/ricalcola` server-side e
ri-pulla la lista completa col phantom-delete.

---

## Test backend

Il backend (`app/`) ha pytest in `app/tests/`:
```bash
cd ../  # root del progetto, dove c'è pytest.ini
pytest app/tests -v
```

Il workflow CI `.github/workflows/test-backend.yml` esegue questi test su
ogni push/PR che tocca `app/`.
