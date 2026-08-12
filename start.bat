@echo off
setlocal EnableExtensions
REM ===========================================================================
REM factory-ontology-kit one-click startup (Windows)
REM
REM Flow: install deps -> build web frontend -> start api_server
REM
REM Usage:
REM   start.bat                 full: install deps + build web + start service
REM   start.bat build-only      rebuild web only (when deps already installed)
REM   start.bat serve-only      start service only (deps and web ready)
REM
REM After start:
REM   - REST API + mobile APP : http://localhost:8000
REM   - Modern Web UI (opt)   : cd web && npm start -> http://localhost:3001
REM
REM NOTE: before opening to public, set env FOOD_ADMIN_KEY / FOOD_READ_KEY.
REM       Otherwise /api/* returns 401 (see docs/新机器部署验收.md).
REM ===========================================================================

set "ROOT=%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

REM ---------- [1/3] install deps ----------
if /i "%MODE%"=="all"      goto install
if /i "%MODE%"=="build"    goto build_only
if /i "%MODE%"=="build-only" goto build_only
if /i "%MODE%"=="install"  goto install
if /i "%MODE%"=="serve"    goto serve
if /i "%MODE%"=="serve-only" goto serve
echo Unknown mode: %MODE%  (use all / build-only / serve-only)
exit /b 1

:install
echo [1/3] Installing Python deps...
python -m pip install --upgrade pip
if errorlevel 1 goto err
python -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 goto err

:build_only
echo [2/3] Building Web frontend (Svelte5)...
if exist "%ROOT%web\node_modules" goto build_run
call npm --version >nul 2>&1
if errorlevel 1 goto nonpm
pushd "%ROOT%web"
call npm install
if errorlevel 1 goto err
popd

:build_run
pushd "%ROOT%web"
call npm run build
if errorlevel 1 goto err
popd

echo Done.
exit /b 0

:serve
echo [3/3] Starting api_server...
echo    REST API + mobile APP: http://localhost:8000
echo    Modern Web UI (opt): open another terminal, cd web ^&^& npm start
pushd "%ROOT%codes"
python api_server.py
goto done

:nonpm
echo npm not found. Please install Node.js 18+ and add it to PATH.
exit /b 1

:err
echo.
echo Startup failed. Check the error messages above.
exit /b 1

:done
endlocal
