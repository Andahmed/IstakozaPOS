@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Builds IstakozaPOS.exe (web page embedded inside) and then the one-click installer.
rem Needs on THIS machine only: Python 3 (py launcher) + NSIS (https://nsis.sourceforge.io)
echo [1/3] Installing PyInstaller...
py -m pip install --upgrade pyinstaller || goto err
echo [2/3] Building IstakozaPOS.exe ...
rem --add-data embeds pos_istakoza.html inside the exe (found through sys._MEIPASS). Console is kept on purpose so the server window can be closed.
py -m PyInstaller --noconfirm --onefile --console --name IstakozaPOS --add-data "pos_istakoza.html;." server.py || goto err
copy /y dist\IstakozaPOS.exe IstakozaPOS.exe >nul
set NSIS=%ProgramFiles(x86)%\NSIS\makensis.exe
if not exist "%NSIS%" set NSIS=%ProgramFiles%\NSIS\makensis.exe
if not exist "%NSIS%" (echo NSIS not found - install it from https://nsis.sourceforge.io & goto err)
echo [3/3] Building installer...
"%NSIS%" setup.nsi || goto err
echo.
echo DONE: IstakozaPOS_Setup.exe is ready. Copy ONLY this file to a USB stick - it installs on a clean PC (no Python needed).
pause
exit /b 0
:err
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
