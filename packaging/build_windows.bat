@echo off
setlocal EnableDelayedExpansion
REM ============================================================================
REM HandQ Nuitka Build Script for Windows
REM ============================================================================
REM Produces a standalone handq.dist\ directory (mirrors build_cross_platform.sh).
REM Entry point: handq_win.py  (Windows-specific foreground runner; handq.py is
REM Linux/tmux only).  Output binary is named handq.exe via --output-filename.
REM
REM Prerequisites:
REM   1. Python 3.10 or higher installed
REM   2. Nuitka installed: pip install nuitka
REM   3. A C compiler (MSVC recommended, MinGW64 also works)
REM   4. All dependencies installed: pip install -r requirements.txt
REM
REM Environment variables (optional):
REM   PYTHON          Python interpreter to use (default: python)
REM   CLEAN_BUILD=1   Wipe build cache before compiling (default: incremental)
REM   NUITKA_VERBOSE=1  Enable --show-progress --show-memory (default: off)
REM   JOBS            Override parallel job count
REM
REM Usage:
REM   cd packaging
REM   build_windows.bat
REM
REM Output layout:
REM   ..\dist\windows-x64\
REM     handq.dist\          — standalone binary + all dependencies
REM     handq_config.yaml    — users edit this before running
REM     VERSION_INFO.txt
REM     nuitka-build-report.xml
REM   ..\dist\.nuitka_cache\ — intermediate Nuitka files (not for distribution)
REM ============================================================================

echo ============================================================================
echo HandQ Windows Build Script (standalone)
echo ============================================================================
echo.

REM ── Python interpreter ────────────────────────────────────────────────────────
if "%PYTHON%"=="" set PYTHON=python

REM ── Parallel job count (cap at 16) ───────────────────────────────────────────
if not "%JOBS%"=="" (
    set NUITKA_JOBS=%JOBS%
) else (
    set /a NUITKA_JOBS=%NUMBER_OF_PROCESSORS%
    if !NUITKA_JOBS! GTR 16 set NUITKA_JOBS=16
)

REM ── Verbose flags ─────────────────────────────────────────────────────────────
set VERBOSE_FLAGS=
if "%NUITKA_VERBOSE%"=="1" (
    set VERBOSE_FLAGS=--show-progress --show-memory
    echo [INFO] Verbose mode enabled ^(NUITKA_VERBOSE=1^)
)

echo   Python interpreter : %PYTHON%
echo   Parallel jobs      : %NUITKA_JOBS%
if "%CLEAN_BUILD%"=="1" (
    echo   Incremental build  : NO ^(CLEAN_BUILD=1^)
) else (
    echo   Incremental build  : YES
)
if "%NUITKA_VERBOSE%"=="1" (
    echo   Verbose output     : YES
) else (
    echo   Verbose output     : NO
)
echo.

REM ── Sanity checks ────────────────────────────────────────────────────────────
echo [INFO] Checking prerequisites...

%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python interpreter not found: %PYTHON%
    exit /b 1
)
for /f "tokens=*" %%v in ('%PYTHON% -c "import sys; print(\".\".join(map(str, sys.version_info[:2])))"') do set PYTHON_VERSION=%%v
echo [INFO] Python interpreter: %PYTHON% (%PYTHON_VERSION%)

%PYTHON% -c "import nuitka" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Nuitka is not installed. Install it with: pip install nuitka
    exit /b 1
)
for /f "tokens=*" %%v in ('%PYTHON% -m nuitka --version 2^>^&1') do (
    set NUITKA_VERSION=%%v
    goto :nuitka_version_done
)
:nuitka_version_done
echo [SUCCESS] Nuitka: %NUITKA_VERSION%

echo [INFO] Parallel jobs: %NUITKA_JOBS%

REM ── Directory setup ───────────────────────────────────────────────────────────
set OUTPUT_DIR=windows-x64
set BUILD_CACHE_DIR=dist\.nuitka_cache
set DIST_DIR=dist\%OUTPUT_DIR%

cd ..

REM ── Incremental build support ─────────────────────────────────────────────────
if "%CLEAN_BUILD%"=="1" (
    echo [INFO] CLEAN_BUILD=1 -- removing build cache...
    if exist %BUILD_CACHE_DIR% rmdir /s /q %BUILD_CACHE_DIR%
) else (
    echo [INFO] Incremental build ^(set CLEAN_BUILD=1 to force a full rebuild^)
)

if not exist %BUILD_CACHE_DIR% mkdir %BUILD_CACHE_DIR%
if not exist %DIST_DIR% mkdir %DIST_DIR%
echo [INFO] Build cache  : %BUILD_CACHE_DIR%  ^(intermediate Nuitka files^)
echo [INFO] Dist package : %DIST_DIR%  ^(final distributable^)

REM ── clcache: compiler-level object file cache (the bulk of build time) ───────
REM   Install once with: pip install clcache
REM   Without it, MSVC recompiles every .c → .obj on every build regardless of
REM   whether the source changed.  With it, unchanged files hit the cache and
REM   incremental builds drop from 5-15 min to ~1-2 min.
where clcache >nul 2>&1
if %ERRORLEVEL%==0 (
    set CLCACHE_FLAG=--clcache
    echo [INFO] clcache found — compiler cache enabled
) else (
    set CLCACHE_FLAG=
    echo [WARNING] clcache not found — install with: pip install clcache
    echo [WARNING] Without clcache every build recompiles all C files ^(slow^)
)

echo.
echo [INFO] Starting Nuitka compilation for Windows (standalone)...
echo [INFO] Parallel jobs: %NUITKA_JOBS%
echo [WARNING] First build: 5-15 min.  Subsequent builds with clcache: 1-2 min.
echo.

REM ── Capture build start time ─────────────────────────────────────────────────
for /f "tokens=1-4 delims=:.," %%a in ("%time%") do set BUILD_START_S=%%a%%b%%c

REM ── Nuitka build ─────────────────────────────────────────────────────────────
%PYTHON% -m nuitka ^
    --standalone ^
    --assume-yes-for-downloads ^
    %VERBOSE_FLAGS% ^
    %CLCACHE_FLAG% ^
    --jobs=%NUITKA_JOBS% ^
    --output-dir=%BUILD_CACHE_DIR% ^
    --company-name="HandQ" ^
    --product-name="HandQ Agent" ^
    --file-version=2.0.0.0 ^
    --product-version=2.0.0.0 ^
    --file-description="HandQ AI Task Execution Agent" ^
    --windows-console-mode=force ^
    --include-package=src ^
    --include-package=yaml ^
    --include-package=rich ^
    --include-package=json_repair ^
    --include-package=anthropic ^
    --include-package=httpx ^
    --include-package=paramiko ^
    --include-package=keyring ^
    --include-package=cffi ^
    --include-package=cryptography ^
    --nofollow-import-to=pytest ^
    --nofollow-import-to=tests ^
    --nofollow-import-to=setuptools ^
    --nofollow-import-to=pip ^
    --nofollow-import-to=wheel ^
    --nofollow-import-to=tkinter ^
    --nofollow-import-to=turtle ^
    --nofollow-import-to=idlelib ^
    --nofollow-import-to=wx ^
    --nofollow-import-to=PyQt5 ^
    --nofollow-import-to=PyQt6 ^
    --nofollow-import-to=PySide2 ^
    --nofollow-import-to=PySide6 ^
    --nofollow-import-to=pdb ^
    --nofollow-import-to=pdbpp ^
    --nofollow-import-to=profile ^
    --nofollow-import-to=cProfile ^
    --nofollow-import-to=pstats ^
    --nofollow-import-to=timeit ^
    --nofollow-import-to=trace ^
    --nofollow-import-to=distutils ^
    --nofollow-import-to=ensurepip ^
    --nofollow-import-to=venv ^
    --nofollow-import-to=lib2to3 ^
    --nofollow-import-to=xmlrpc ^
    --nofollow-import-to=ftplib ^
    --nofollow-import-to=imaplib ^
    --nofollow-import-to=poplib ^
    --nofollow-import-to=smtplib ^
    --nofollow-import-to=telnetlib ^
    --nofollow-import-to=nntplib ^
    --nofollow-import-to=sndhdr ^
    --nofollow-import-to=sunau ^
    --nofollow-import-to=aifc ^
    --nofollow-import-to=audioop ^
    --nofollow-import-to=IPython ^
    --nofollow-import-to=ipykernel ^
    --nofollow-import-to=notebook ^
    --nofollow-import-to=nbformat ^
    --nofollow-import-to=nbconvert ^
    --python-flag=no_site ^
    --disable-plugin=anti-bloat ^
    --lto=auto ^
    --report=%BUILD_CACHE_DIR%\nuitka-build-report.xml ^
    --output-filename=handq ^
    handq_win.py

set BUILD_RESULT=%errorlevel%

if %BUILD_RESULT% neq 0 (
    echo.
    echo [ERROR] Windows Nuitka compilation failed!
    cd packaging
    exit /b 1
)

REM ── Assemble the distributable package ───────────────────────────────────────
set STANDALONE_SRC=%BUILD_CACHE_DIR%\handq_win.dist

if not exist %STANDALONE_SRC% (
    echo [ERROR] Expected standalone dist dir not found: %STANDALONE_SRC%
    cd packaging
    exit /b 1
)

echo [INFO] Assembling distributable package in %DIST_DIR%\...

REM Copy standalone dist directory into the clean dist dir
if exist %DIST_DIR%\handq.dist rmdir /s /q %DIST_DIR%\handq.dist
xcopy /s /e /q %STANDALONE_SRC% %DIST_DIR%\handq.dist\ >nul
if exist %BUILD_CACHE_DIR%\nuitka-build-report.xml (
    copy /y %BUILD_CACHE_DIR%\nuitka-build-report.xml %DIST_DIR%\nuitka-build-report.xml >nul
)

REM Config at top level — easy for users to find and edit
copy /y handq_config.yaml %DIST_DIR%\handq_config.yaml >nul

REM Create version info file
(
    echo HandQ Windows Build Information
    echo ==============================
    echo Build Date   : %date% %time%
    echo System       : Windows x64
    echo Python       : %PYTHON_VERSION%
    echo Nuitka       : %NUITKA_VERSION%
    echo Mode         : standalone ^(directory^)
    echo.
    echo Package contents:
    echo   handq.dist\         — standalone binary + all dependencies
    echo   handq_config.yaml   — edit API key / model settings here
    echo   VERSION_INFO.txt
    echo   nuitka-build-report.xml
    echo.
    echo Quick Start:
    echo   1. Edit handq_config.yaml     ^# set your API key env var, models, etc.
    echo   2. set ANTHROPIC_API_KEY=...  ^# or set in your environment
    echo   3. handq.dist\handq.exe       ^# run HandQ
    echo.
    echo Build report: nuitka-build-report.xml
) > %DIST_DIR%\VERSION_INFO.txt

cd packaging

echo.
echo ============================================================================
echo [SUCCESS] Windows build completed successfully!
echo ============================================================================
echo.
echo   Dist package : dist\%OUTPUT_DIR%\
echo     handq.dist\         -- standalone binary + all dependencies
echo     handq_config.yaml   -- edit API key / model settings here
echo     VERSION_INFO.txt
echo     nuitka-build-report.xml
echo.
echo   Build cache  : dist\.nuitka_cache\  ^(intermediate files, not for distribution^)
echo.
echo   IMPORTANT: This build targets the current Windows x64 architecture.
echo.
echo   To package for distribution:
echo     tar -czf handq-windows-x64.tar.gz -C dist windows-x64
echo     ^(or use 7-Zip / WinRAR to zip dist\windows-x64\^)
echo ============================================================================
echo.

exit /b 0
