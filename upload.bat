@echo off
setlocal EnableDelayedExpansion

rem Runs github_upload.py with the username and token from Creds.txt.
rem Any arguments are passed through, e.g.:
rem   upload.bat "C:\path\to\project" --repo my-repo -m "Fix typo"
rem You can also drag a folder onto this file.

set "HERE=%~dp0"
set "CREDS=%HERE%Creds.txt"

if not exist "%CREDS%" (
    echo Creds.txt not found next to this file: "%CREDS%"
    pause
    exit /b 1
)

set "GH_USER="
set "GITHUB_TOKEN="
for /f "usebackq tokens=1,* delims=:" %%A in ("%CREDS%") do (
    set "KEY=%%A"
    set "VAL=%%B"
    set "KEY=!KEY: =!"
    set "VAL=!VAL: =!"
    if /i "!KEY!"=="Username" set "GH_USER=!VAL!"
    if /i "!KEY!"=="AccessToken" set "GITHUB_TOKEN=!VAL!"
)

if not defined GH_USER (
    echo Could not find "Username : ..." in Creds.txt
    pause
    exit /b 1
)
if not defined GITHUB_TOKEN (
    echo Could not find "Access Token : ..." in Creds.txt
    pause
    exit /b 1
)

set "PY=python"
where python >nul 2>nul || set "PY=py"

rem --exclude Creds.txt keeps the token file from ever being uploaded.
%PY% "%HERE%github_upload.py" --user "%GH_USER%" --exclude Creds.txt %*
set "RC=%ERRORLEVEL%"

echo.
pause
endlocal & exit /b %RC%
