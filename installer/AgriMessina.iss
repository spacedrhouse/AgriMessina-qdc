; Inno Setup script per AgriMessina QDC.
; Impacchetta l'output PyInstaller (dist\AgriMessina\) in un installer Windows.
;
; Build locale:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.2.3 installer\AgriMessina.iss
;
; In CI: la versione arriva da github.ref_name (tag git) o workflow_dispatch input.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#define AppName       "AgriMessina QDC"
#define AppPublisher  "AgriMessina"
#define AppURL        "https://agrimessina.it"
#define AppExeName    "AgriMessina.exe"

[Setup]
; AppId è il fingerprint dell'app per il sistema di disinstallazione di Windows.
; NON va mai cambiato dopo il primo release, altrimenti gli upgrade non
; riconoscono la versione vecchia e finisce con due copie installate.
AppId={{2E7A0F4C-1B5F-4D8A-B1F3-89F2C5AB6E12}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\AgriMessina
DefaultGroupName=AgriMessina
DisableProgramGroupPage=auto
OutputBaseFilename=AgriMessina_Setup_{#AppVersion}
OutputDir=installer\output
SetupIconFile=icona.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Su Windows 11/10 64-bit modern: meglio installare in Program Files 64-bit
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Permette upgrade in-place senza chiedere la disinstallazione manuale
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; "{#SourceDir}" è il path al risultato di PyInstaller. Di default `dist\AgriMessina`
; relativo alla cartella di lavoro di ISCC. Il workflow CI lo lancia dalla
; root del repo dove esiste già `dist\AgriMessina\`.
Source: "..\dist\AgriMessina\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent
