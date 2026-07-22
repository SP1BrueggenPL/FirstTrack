@echo off
chcp 65001 >nul 2>&1
title FirstTrack - H. & J. Bruggen KG

:: ===================================================
::  FirstTrack - Skrypt startowy
::  H. & J. Bruggen KG
:: ===================================================

:: Przejdz do katalogu skryptu (dziala tez dla skrotow Windows)
cd /d "%~dp0"

cls
echo.
echo  =====================================================
echo   FirstTrack  -  H. ^& J. Bruggen KG
echo   Nadzorowanie Pierwszej Produkcji
echo  =====================================================
echo.

:: --- Sprawdz Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [BLAD] Python nie jest zainstalowany lub nie ma go w PATH.
    echo.
    echo  Zainstaluj Python 3.10+ ze strony: https://www.python.org/downloads/
    echo  Pamietaj zaznaczyc "Add Python to PATH" podczas instalacji!
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  [OK]  %PY_VER% znaleziony

:: --- Sprawdz Django ---
python -c "import django" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [BLAD] Django nie jest zainstalowany.
    echo.
    echo  Uruchom w tym katalogu:
    echo    pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python -c "import django; print(django.__version__)" 2^>^&1') do set DJ_VER=%%v
echo  [OK]  Django %DJ_VER% znaleziony

:: --- Sprawdz manage.py ---
if not exist "%~dp0manage.py" (
    echo  [BLAD] Nie znaleziono manage.py w katalogu: %~dp0
    echo  Upewnij sie ze skrypt jest w folderze C:\FirstTrack\
    echo.
    pause
    exit /b 1
)
echo  [OK]  Projekt Django znaleziony

:: --- Sprawdz port 8000 ---
netstat -ano | findstr ":8000 " >nul 2>&1
if %errorlevel% equ 0 (
    echo  [INFO] Port 8000 jest juz zajety - aplikacja moze byc juz uruchomiona.
    echo         Otwieram przegladarke...
    echo.
    start http://127.0.0.1:8000/
    pause
    exit /b 0
)

:: --- Klucz API Azure OpenAI (opcjonalny) ---
if defined AZURE_OPENAI_KEY (
    echo  [OK]  AZURE_OPENAI_KEY jest ustawiony - import AI z SAP aktywny
) else (
    echo  [INFO] AZURE_OPENAI_KEY nie ustawiony - ekstrakcja AI z SAP niedostepna
)

echo.
echo  Uruchamiam serwer...
echo.
echo  =====================================================
echo   Aplikacja:   http://127.0.0.1:8000/
echo   Admin panel: http://127.0.0.1:8000/admin/
echo   Login:       admin  /  admin123
echo  =====================================================
echo.
echo   Aby zatrzymac: zamknij to okno lub nacisnij CTRL+C
echo.

:: --- Otwieramy przegladarke po 3 sekundach (cicho, bez dodatkowego okna) ---
start "" powershell -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:8000/'"

:: --- Uruchom serwer Django ---
python manage.py runserver 8000

echo.
echo  Serwer zatrzymany.
pause
