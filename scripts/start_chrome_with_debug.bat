@echo off
REM HandQ — start Chrome / Edge with remote debugging enabled.
REM
REM Run this BEFORE asking HandQ to use action='attach_browser'.
REM Once started, use Chrome normally throughout your session — agent
REM attach mode connects to this same instance and can see / operate on
REM your real cookies and login state.
REM
REM IMPORTANT: if Chrome is already running with your default profile,
REM the new launch will be ignored (Chrome reuses the existing instance).
REM You must close all Chrome windows first, then run this script.
REM
REM Edge is tried first (preinstalled on Windows 11). Falls back to Chrome.
REM If neither is found, install Edge or specify a custom path manually.

setlocal

set CDP_PORT=9222

set EDGE_PATH=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe
set CHROME_PATH=%ProgramFiles%\Google\Chrome\Application\chrome.exe
set CHROME_PATH_X86=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe

if exist "%EDGE_PATH%" (
    echo Starting Edge with --remote-debugging-port=%CDP_PORT% ...
    start "" "%EDGE_PATH%" --remote-debugging-port=%CDP_PORT%
    echo Done. HandQ can now use action='attach_browser'.
    exit /b 0
)

if exist "%CHROME_PATH%" (
    echo Starting Chrome with --remote-debugging-port=%CDP_PORT% ...
    start "" "%CHROME_PATH%" --remote-debugging-port=%CDP_PORT%
    echo Done. HandQ can now use action='attach_browser'.
    exit /b 0
)

if exist "%CHROME_PATH_X86%" (
    echo Starting Chrome ^(x86^) with --remote-debugging-port=%CDP_PORT% ...
    start "" "%CHROME_PATH_X86%" --remote-debugging-port=%CDP_PORT%
    echo Done. HandQ can now use action='attach_browser'.
    exit /b 0
)

echo ERROR: neither Edge nor Chrome found in standard install paths.
echo  Tried:
echo    %EDGE_PATH%
echo    %CHROME_PATH%
echo    %CHROME_PATH_X86%
echo.
echo Install Microsoft Edge ^(default on Windows 11^) or run manually:
echo    "C:\path\to\chrome.exe" --remote-debugging-port=%CDP_PORT%
exit /b 1
