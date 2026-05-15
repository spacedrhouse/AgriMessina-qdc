"""Auto-update: download dell'installer da GitHub Release + esecuzione silent.

Sequence di update completa (lato app):
  1. `update_checker.UpdateCheckWorker` rileva una nuova release.
  2. UI offre "Aggiorna e riavvia".
  3. `UpdateDownloadWorker` scarica il .exe in temp con progresso a chunk.
  4. `apply_update(path)` lancia l'installer Inno Setup con /SILENT e
     fa sys.exit(0) per liberare i file in {app}.
  5. L'installer rimpiazza i file e — grazie al secondo [Run] con
     `Check: WizardSilent` nel .iss — rilancia AgriMessina.exe come
     processo dell'utente originale (no admin escalation).

Senza il secondo [Run] dedicato al silent mode, in /SILENT la postinstall
checkbox del wizard non viene mostrata e l'app non si rilancia da sola
(comportamento di default di Inno Setup per `postinstall skipifsilent`).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)

# Timeout di download: il file installer è ~50-100MB, su connessioni lente
# servono minuti. 5 minuti come safety net.
DOWNLOAD_TIMEOUT_SECONDS = 300.0
CHUNK_BYTES = 64 * 1024


class UpdateDownloadWorker(QThread):
    """Scarica un file (l'installer .exe) in background, emettendo progresso.

    Segnali:
      - `progress(int done, int total)`: byte scaricati / totali. `total` può
        essere 0 se il server non manda Content-Length.
      - `finished_ok(str path)`: download completato, percorso del file su disco.
      - `failed(str message)`: errore di rete o file system.
    """

    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, download_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = download_url
        self._cancel = False

    def cancel(self) -> None:
        """Marca per interruzione. Il thread esce al prossimo chunk."""
        self._cancel = True

    def run(self) -> None:
        # Nome file deterministico in temp: se l'utente fa "Aggiorna" più volte
        # nello stesso boot, riusiamo lo stesso path invece di accumulare .exe.
        dest = Path(tempfile.gettempdir()) / "AgriMessina_Update.exe"
        try:
            with httpx.stream(
                "GET", self._url,
                follow_redirects=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                headers={"Accept": "application/octet-stream"},
            ) as resp:
                if resp.status_code != 200:
                    self.failed.emit(f"HTTP {resp.status_code} su GET installer")
                    return
                total = int(resp.headers.get("content-length", 0) or 0)
                done = 0
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=CHUNK_BYTES):
                        if self._cancel:
                            try:
                                dest.unlink(missing_ok=True)
                            except Exception:
                                pass
                            self.failed.emit("Download annullato dall'utente.")
                            return
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            self.progress.emit(done, total)
        except Exception as e:
            log.exception("[auto_updater] errore download")
            self.failed.emit(str(e))
            return

        log.info("[auto_updater] download completato: %s (%d bytes)", dest, done)
        self.finished_ok.emit(str(dest))


def apply_update(installer_path: str) -> None:
    """Lancia l'installer in modalità silent e termina l'app.

    L'installer Inno Setup:
      - `/SILENT` mostra una barra di progresso minimale (no wizard pages)
      - `/SUPPRESSMSGBOXES` evita popup di conferma
      - `/NORESTART` non riavvia il PC anche se richiesto
      - rimpiazza i file in {app} e (grazie al secondo [Run] WizardSilent
        nel .iss) rilancia AgriMessina.exe come utente normale

    Spawniamo il processo "detached" (DETACHED_PROCESS | CREATE_NO_WINDOW)
    così sopravvive alla nostra sys.exit. Senza, l'installer verrebbe
    ucciso con l'app padre.

    Su non-Windows (dev/test): apre l'installer con xdg-open / open. È
    inteso solo per lo sviluppo — la release è solo Windows.
    """
    log.info("[auto_updater] launch installer: %s", installer_path)
    if sys.platform == "win32":
        # Detached: il figlio sopravvive al sys.exit del padre.
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [installer_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
        )
    elif sys.platform == "darwin":
        subprocess.Popen(["open", installer_path], close_fds=True)
    else:
        subprocess.Popen(["xdg-open", installer_path], close_fds=True)

    # Piccola attesa: il figlio detached deve iniziare prima che il padre
    # esca, altrimenti su alcuni Windows può perdersi.
    time.sleep(0.3)
    sys.exit(0)
