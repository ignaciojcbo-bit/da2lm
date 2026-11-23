@echo off
rem Builds the Windows release of da2lm: "DA2 Loader.exe" plus a zip ready for Nexus.
rem Needs Python 3.10+ (py launcher). Run it from this folder by double-clicking.
setlocal
cd /d "%~dp0\.."
for /f "tokens=2 delims==" %%v in ('findstr /b "__version__" da2lm\__init__.py') do set VER=%%~v
set VER=%VER: =%
set VER=%VER:"=%
echo Building da2lm %VER% ...

py -m pip install --upgrade pyinstaller PySide6-Essentials || goto :fail

if exist build rmdir /s /q build
if exist "dist\DA2 Loader" rmdir /s /q "dist\DA2 Loader"
py -m PyInstaller --noconfirm --clean --windowed --onedir --name "DA2 Loader" ^
    --icon packaging\da2lm.ico ^
    --add-data "da2lm\assets;da2lm\assets" ^
    --exclude-module tkinter --exclude-module unittest --exclude-module pytest ^
    packaging\entry.py || goto :fail

copy /y GUIDE.md "dist\DA2 Loader\" >nul
copy /y README.md "dist\DA2 Loader\" >nul
copy /y LICENSE "dist\DA2 Loader\" >nul

set ZIP=dist\da2lm-%VER%-beta-win64.zip
if exist "%ZIP%" del "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\DA2 Loader' -DestinationPath '%ZIP%'" || goto :fail

echo.
echo Done:
echo   dist\DA2 Loader\DA2 Loader.exe   (try it)
echo   %ZIP%   (upload this)
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
