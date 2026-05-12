# PyInstaller spec per il client desktop AgriMessina QDC.
#
# Build locale:   pyinstaller AgriMessina.spec --noconfirm
# Output:         dist/AgriMessina/  (--onedir, viene poi impacchettato da Inno Setup)
#
# Note:
#  - console=False perché è un'app GUI: niente terminale nero al lancio.
#  - hidden imports per moduli che PyInstaller non riesce a dedurre staticamente
#    (es. pandas/openpyxl importati lazily nel codice export, sqlalchemy dialects).
#  - excludes per ridurre la dimensione: tutto ciò che non viene mai importato.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Risorse statiche che vanno copiate accanto all'eseguibile.
# Devono essere referenziate via percorsi relativi al file .exe a runtime
# (in main.py la funzione `get_risorsa()` risolve via sys._MEIPASS quando frozen).
datas = [
    ('icona.ico', '.'),
    ('icona.png', '.'),
    ('splash.png', '.'),
    ('.env', '.'),       # URL del backend, contiene solo API_BASE_URL (no segreti)
]

# Imports che PyInstaller può non vedere (import lazy, dialects SQLAlchemy).
hiddenimports = [
    'sqlalchemy.dialects.sqlite',
    'pandas',           # import lazy dentro _esporta_selezionati
    'openpyxl',         # engine per pd.ExcelWriter
    'openpyxl.cell._writer',
] + collect_submodules('sqlalchemy.dialects')

# Pacchetti grossi che non usiamo: tagliarli alleggerisce il bundle di ~30-100 MB.
excludes = [
    'tkinter',
    'pytest',
    'setuptools',
    'pip',
    'wheel',
    'PIL.ImageTk',          # PIL/Pillow usato solo per immagini, niente Tk
    'PyQt6.Qt3DCore',
    'PyQt6.Qt3DRender',
    'PyQt6.QtBluetooth',
    'PyQt6.QtNfc',
    'PyQt6.QtLocation',
    'PyQt6.QtPositioning',
    'PyQt6.QtWebEngine',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtQuick',
    'PyQt6.QtQuick3D',
    'PyQt6.QtQml',
    'PyQt6.QtMultimedia',
    'PyQt6.QtMultimediaWidgets',
    'PyQt6.QtCharts',
    'PyQt6.QtDataVisualization',
    'PyQt6.QtSensors',
]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AgriMessina',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX compatta ma rallenta startup e talvolta antivirus storce
    console=False,            # GUI app: nessuna console
    icon='icona.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AgriMessina',
)
