; Inno Setup script for the per-user Vcad installer.
; Build with: python scripts/build_app.py (it generates the two included files first).

#include "..\build\version.iss"
#ifndef SourceDir
  #define SourceDir "..\dist\vcad"
#endif

[Setup]
AppId={{6F1C2E4B-9A37-4C51-8E0D-2B7A5C9E3D41}
AppName=Vcad
AppVersion={#AppVersion}
AppVerName=Vcad {#AppVersion}
AppPublisher=Vcad
DefaultDirName={autopf}\Vcad
; Upgrades from cadview (same AppId) move to the new folder.
UsePreviousAppDir=no
DefaultGroupName=Vcad
DisableProgramGroupPage=yes
; Per-user install: no administrator rights; file types are registered for this user only.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=vcad-setup-{#AppVersion}
SetupIconFile=vcad.ico
UninstallDisplayIcon={app}\vcad.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
CloseApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Vcad"; Filename: "{app}\vcad.exe"
Name: "{autodesktop}\Vcad"; Filename: "{app}\vcad.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\vcad.exe"; Description: "{cm:LaunchProgram,Vcad}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Vcad was called cadview: remove an earlier cadview install.
Type: filesandordirs; Name: "{autopf}\cadview"
Type: files; Name: "{autoprograms}\cadview.lnk"
Type: files; Name: "{autodesktop}\cadview.lnk"
Type: filesandordirs; Name: "{localappdata}\cadview"

[UninstallDelete]
; Running-instance state (port and token of the open viewer).
Type: filesandordirs; Name: "{localappdata}\vcad"

[Registry]
; The application, for "Open with" lists.
Root: HKA; Subkey: "Software\Classes\vcad.document"; ValueType: string; ValueData: "CAD document"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\vcad.document"; ValueName: "FriendlyTypeName"; ValueType: string; ValueData: "CAD document"
Root: HKA; Subkey: "Software\Classes\vcad.document\DefaultIcon"; ValueType: string; ValueData: "{app}\vcad.exe,0"
Root: HKA; Subkey: "Software\Classes\vcad.document\shell\open\command"; ValueType: string; ValueData: """{app}\vcad.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\vcad.exe"; ValueName: "FriendlyAppName"; ValueType: string; ValueData: "Vcad"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\vcad.exe\DefaultIcon"; ValueType: string; ValueData: "{app}\vcad.exe,0"
Root: HKA; Subkey: "Software\Classes\Applications\vcad.exe\shell\open\command"; ValueType: string; ValueData: """{app}\vcad.exe"" ""%1"""
; "Open with Vcad" on folders (Gerber output folders).
Root: HKA; Subkey: "Software\Classes\Directory\shell\vcad"; ValueName: "MUIVerb"; ValueType: string; ValueData: "Open with Vcad"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Directory\shell\vcad"; ValueName: "Icon"; ValueType: string; ValueData: "{app}\vcad.exe,0"
Root: HKA; Subkey: "Software\Classes\Directory\shell\vcad\command"; ValueType: string; ValueData: """{app}\vcad.exe"" ""%1"""
; Remove the cadview registrations.
Root: HKA; Subkey: "Software\Classes\cadview.document"; ValueType: none; Flags: deletekey
Root: HKA; Subkey: "Software\Classes\Applications\cadview.exe"; ValueType: none; Flags: deletekey
Root: HKA; Subkey: "Software\Classes\Directory\shell\cadview"; ValueType: none; Flags: deletekey
; Per file type entries are generated from FILE_TYPES in scripts/build_app.py.
#include "..\build\file_types.iss"
