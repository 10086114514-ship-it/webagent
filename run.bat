@echo off
rem webagent launcher: find a Python with Pillow (prefer DSH bundled runtime).
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if defined WEBAGENT_PYTHON set "PY=%WEBAGENT_PYTHON%"

if not defined PY (
  python -c "import PIL" >nul 2>nul && set "PY=python"
)

if not defined PY (
  for /d %%D in ("%USERPROFILE%\.dsh-beta\dsh-runtimes\*") do (
    if exist "%%D\dependencies\python\python.exe" set "PY=%%D\dependencies\python\python.exe"
  )
)

if not defined PY (
  py -c "import PIL" >nul 2>nul && set "PY=py"
)

if not defined PY (
  echo [webagent] No Python with Pillow found.
  echo   1. Install:  python -m pip install pillow
  echo   2. Or set:   set WEBAGENT_PYTHON=C:\path\to\python.exe
  exit /b 1
)

"%PY%" -m webagent %*
exit /b %ERRORLEVEL%
