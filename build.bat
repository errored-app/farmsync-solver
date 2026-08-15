@echo off
REM Local Windows build. release.yml runs the same four steps in CI.
cd /d "%~dp0"

echo [1/4] installing build dependencies
python -m pip install -r requirements-dev.txt pyinstaller
if errorlevel 1 goto fail

echo [2/4] running the test suite
python -m pytest
if errorlevel 1 goto fail

echo [3/4] freezing
python -m PyInstaller --noconfirm --clean FarmsyncSolver.spec
if errorlevel 1 goto fail

echo [4/4] checksum
certutil -hashfile dist\FarmsyncSolver.exe SHA256
if errorlevel 1 goto fail

echo.
echo Built: dist\FarmsyncSolver.exe
exit /b 0

:fail
echo.
echo BUILD FAILED
exit /b 1
