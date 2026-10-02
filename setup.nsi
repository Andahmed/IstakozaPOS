; ===== Istakoza POS - one-click installer (self-contained: the exe carries the web page inside) =====
; Build with build_all.bat  (needs IstakozaPOS.exe built by PyInstaller + NSIS)
Unicode true
!include "MUI2.nsh"
Name "Istakoza POS"
OutFile "IstakozaPOS_Setup.exe"
InstallDir "$PROGRAMFILES64\IstakozaPOS"
InstallDirRegKey HKLM "Software\IstakozaPOS" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
AutoCloseWindow true
ShowInstDetails nevershow
BrandingText "Istakoza POS"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetRegView 64
  SetShellVarContext all          ; so $APPDATA = C:\ProgramData (same place server.py stores the database)
  nsExec::Exec 'taskkill /F /IM IstakozaPOS.exe'
  Sleep 1000
  SetOutPath "$INSTDIR"
  ; remove leftovers of older installers (they must not shadow the page inside the exe)
  Delete "$INSTDIR\pos_istakoza.html"
  Delete "$INSTDIR\start_pos.bat"
  Delete "$INSTDIR\pos_client.bat"
  File "IstakozaPOS.exe"          ; the ONLY file: server + database engine + web page
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  ; database + backups folders (the server also creates them on first run) - writable by every Windows user
  CreateDirectory "$APPDATA\Istakoza"
  CreateDirectory "$APPDATA\Istakoza\backups"
  nsExec::Exec 'icacls "$APPDATA\Istakoza" /grant *S-1-5-32-545:(OI)(CI)M /T'
  ; firewall so other devices (PCs / phones) can open the POS
  nsExec::Exec 'netsh advfirewall firewall delete rule name="IstakozaPOS"'
  nsExec::Exec 'netsh advfirewall firewall add rule name="IstakozaPOS" dir=in action=allow protocol=TCP localport=8080 profile=any'
  ; shortcuts: the exe starts the server (if needed) AND opens the POS window with silent printing
  CreateShortCut "$DESKTOP\Istakoza POS.lnk" "$INSTDIR\IstakozaPOS.exe" "" "$INSTDIR\IstakozaPOS.exe" 0 SW_SHOWMINIMIZED
  CreateShortCut "$SMPROGRAMS\Istakoza POS.lnk" "$INSTDIR\IstakozaPOS.exe" "" "$INSTDIR\IstakozaPOS.exe" 0 SW_SHOWMINIMIZED
  Delete "$SMPROGRAMS\Istakoza POS Server.lnk"
  CreateShortCut "$SMSTARTUP\Istakoza POS Server.lnk" "$INSTDIR\IstakozaPOS.exe" "--no-browser" "$INSTDIR\IstakozaPOS.exe" 0 SW_SHOWMINIMIZED
  WriteRegStr HKLM "Software\IstakozaPOS" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IstakozaPOS" "DisplayName" "Istakoza POS"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IstakozaPOS" "UninstallString" '"$INSTDIR\Uninstall.exe"'
SectionEnd

; start the server and open the POS as soon as the install finishes (the exe opens the browser itself)
Function .onInstSuccess
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\IstakozaPOS.exe"'
FunctionEnd

Section "Uninstall"
  SetRegView 64
  SetShellVarContext all
  nsExec::Exec 'taskkill /F /IM IstakozaPOS.exe'
  Sleep 1000
  nsExec::Exec 'netsh advfirewall firewall delete rule name="IstakozaPOS"'
  Delete "$INSTDIR\IstakozaPOS.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$DESKTOP\Istakoza POS.lnk"
  Delete "$SMPROGRAMS\Istakoza POS.lnk"
  Delete "$SMSTARTUP\Istakoza POS Server.lnk"
  DeleteRegKey HKLM "Software\IstakozaPOS"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IstakozaPOS"
  ; NOTE: the database (ProgramData\Istakoza) is kept on purpose
SectionEnd
