@echo off
setlocal
echo ============================================
echo  List Scanner - Build Script
echo ============================================
echo.

set VENV_PY=.venv\Scripts\python.exe
set VENV_PIP=.venv\Scripts\pip.exe
set VENV_PYI=.venv\Scripts\pyinstaller.exe

if not exist "%VENV_PY%" (
    echo Creating virtual environment...
    python -m venv .venv
)

echo [1/3] Installing Python dependencies...
"%VENV_PIP%" install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install dependencies.
    pause & exit /b 1
)

echo.
echo [2/3] Installing PyInstaller (>=6.0)...
"%VENV_PIP%" install "pyinstaller>=6.0"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install PyInstaller.
    pause & exit /b 1
)

echo.
echo [2.5/3] Locating Tesseract OCR to bundle...
set TESSERACT_DIR=
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    set TESSERACT_DIR=C:\Program Files\Tesseract-OCR
) else if exist "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe" (
    set TESSERACT_DIR=C:\Program Files (x86)\Tesseract-OCR
) else if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" (
    set TESSERACT_DIR=%LOCALAPPDATA%\Programs\Tesseract-OCR
)

if "%TESSERACT_DIR%"=="" (
    echo.
    echo ERROR: Tesseract OCR was not found on this machine.
    echo The build machine needs Tesseract installed to bundle it.
    echo Download from: https://github.com/UB-Mannheim/tesseract/wiki
    echo Install to the default path: C:\Program Files\Tesseract-OCR
    pause & exit /b 1
)
echo Found Tesseract at: %TESSERACT_DIR%

echo.
echo [3/3] Building executable (bundling Tesseract from %TESSERACT_DIR%)...
"%VENV_PYI%" --clean ListScanner.spec

echo.
if exist "dist\ListScanner.exe" (
    echo ============================================
    echo  BUILD SUCCESSFUL
    echo  Executable: dist\ListScanner.exe
    echo  Tesseract is bundled - users need nothing extra.
    echo ============================================
    echo.
) else (
    echo ============================================
    echo  BUILD FAILED - check output above
    echo ============================================
)

pause
