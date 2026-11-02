; Companion Sync: Windows installer (Inno Setup 6), built by .github/workflows/build.yml
;   iscc /DBuild=<run number> build\companion-sync.iss
;
; Installs for the current user only (no admin prompt), always to the same
; folder, so Windows keeps the tray icon where you put it after updates.
; The app's "Update Companion Sync..." runs this silently, then it reopens the app.

#ifndef Build
  #define Build "dev"
#endif
#ifndef Version
  #define Version "0.0.0"
#endif
#define AppName "Companion Sync"
#define AppExe "Companion Sync.exe"

[Setup]
AppId={{6B0C3E52-6F7A-4C1B-9E43-2B1D5A7C9F10}
AppName={#AppName}
AppVersion={#Version}
AppVerName={#AppName} v{#Version} (build {#Build})
VersionInfoVersion={#Version}
AppPublisher=Nige Isted
AppPublisherURL=https://github.com/nisted/companion-sync
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Companion-Sync-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no

[InstallDelete]
; old copies moved aside by earlier updates (see CurStepChanged)
Type: files; Name: "{app}\*.old-*"

[UninstallDelete]
; everything in the install folder (incl. old copies), and the app's own rclone copy
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{userappdata}\{#AppName}\bin"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Add a desktop shortcut"; Flags: unchecked

[Registry]
; the app's own "Start at login" entry: removed when uninstalled
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue

[Run]
; also runs after a silent update, so the app comes back by itself
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall

[Code]
// The app holds this mutex while it's open. Wait for it to close (an update
// quits it first); if it's still open, ask (or give up when silent).
function AppClosed(Silent: Boolean): Boolean;
var
  i: Integer;
begin
  Result := True;
  if Silent then
    for i := 1 to 60 do begin              // up to 15 s for the app to quit
      if not CheckForMutexes('CompanionSyncRunning') then
        Break;
      Sleep(250);
    end;
  while CheckForMutexes('CompanionSyncRunning') do begin
    if Silent then begin
      Result := False;
      Exit;
    end;
    if MsgBox('Companion Sync is open. Quit it from its tray icon menu, then click OK.',
              mbInformation, MB_OKCANCEL) = IDCANCEL then begin
      Result := False;
      Exit;
    end;
  end;
  Sleep(1500);                             // let it finish closing and free its file
end;

// Move the old .exe aside before copying the new one. Windows allows renaming
// a file that's still open, so nothing left running can block the update; the
// old copy is deleted on the next install.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Exe: String;
begin
  if CurStep = ssInstall then begin
    Exe := ExpandConstant('{app}\{#AppExe}');
    if FileExists(Exe) then
      RenameFile(Exe, Exe + '.old-' + GetDateTimeString('yyyymmddhhnnss', #0, #0));
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := AppClosed(WizardSilent);
end;

function InitializeUninstall(): Boolean;
begin
  Result := AppClosed(UninstallSilent);
end;
