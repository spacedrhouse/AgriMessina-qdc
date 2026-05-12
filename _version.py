"""Versione dell'app. In CI viene riscritta da .github/workflows/build-windows.yml
prima di PyInstaller, sostituendo il valore qui sotto con la versione del tag git.

Per sviluppo locale (run da sorgenti, non da .exe), resta "0.0.0-dev"
così l'update checker capisce che non deve mostrare alert "nuova versione
disponibile" a uno sviluppatore che lavora sui sorgenti.
"""
__version__ = "0.0.0-dev"
