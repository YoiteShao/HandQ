<#
.SYNOPSIS
    Build HandQ: Python bridge (Nuitka / Python 3.12) + Electron frontend (electron-builder).

.DESCRIPTION
    Step 1 — Compile bridge_main.py into a standalone Windows exe via Nuitka.
             Output: dist\.nuitka_cache\bridge_main.dist\
             Post-step: rename bridge_main.exe → handq-bridge.exe (the name main.js expects).

    Step 2 — Run electron-builder inside electron\ to produce a Windows NSIS installer
             (and/or an unpacked dir target).  The builder picks up the bridge dist via
             the extraFiles config in electron\package.json.

    Layout produced (matches ARCHITECTURE.md §3):
        dist\installer\win-unpacked\
            HandQ.exe
            handq-bridge.exe     ← renamed from bridge_main.exe
            handq_config.yaml    ← shipped with bridge, overridden by %USERPROFILE%\HandQ\
            _internal\           ← Nuitka runtime deps
            resources\app.asar   ← Electron renderer/main

.PARAMETER Python
    Python 3.12 interpreter to call for Nuitka.
    Accepts a bare executable name ("python", "python3.12") or a full path.
    Default: "py -3.12" (Windows Launcher).
    Note: rapidocr-onnxruntime 1.4.x caps at Python <3.13 (see requirements.txt),
    so 3.12 is the highest supported version.

.PARAMETER Jobs
    Parallel C-compilation jobs passed to Nuitka --jobs.
    0 (default) = auto-detect from $env:NUMBER_OF_PROCESSORS, capped at 16.

.PARAMETER Clean
    Wipe dist\.nuitka_cache before compiling (forces a full rebuild).
    Without this flag, Nuitka uses its incremental cache.

.PARAMETER BridgeOnly
    Compile only the Python bridge; skip electron-builder.

.PARAMETER ElectronOnly
    Run only electron-builder; skip Nuitka (bridge dist must already exist).

.PARAMETER ShowProgress
    Pass --show-progress --show-memory to Nuitka for verbose build diagnostics.

.PARAMETER Release
    Enable LTO (link-time optimization) for a smaller / slightly faster shipping exe.
    Default (off) builds with --lto=no for fast iteration: the whole-program LTCG
    link step that LTO adds runs on every build and CANNOT be cached (clcache only
    caches the cl.exe compile step, not the link), so it dominates incremental
    rebuild time. Toggling LTO changes the cl.exe command line, so the first build
    after a toggle is a full clcache miss (~10-20 min); subsequent builds are fast.

.EXAMPLE
    # Full build (bridge + installer)
    .\packaging\build.ps1

    # Force clean rebuild
    .\packaging\build.ps1 -Clean

    # Release build (LTO on → smaller/faster exe, slower uncacheable link)
    .\packaging\build.ps1 -Release

    # Bridge only with explicit Python path
    .\packaging\build.ps1 -BridgeOnly -Python "C:\Python312\python.exe"

    # Electron only (reuse existing bridge dist)
    .\packaging\build.ps1 -ElectronOnly
#>
[CmdletBinding()]
param(
    [string]$Python       = 'py -3.12',
    [int]   $Jobs         = 0,
    [switch]$Clean,
    [switch]$BridgeOnly,
    [switch]$ElectronOnly,
    [switch]$Release,
    [switch]$ShowProgress
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Paths ─────────────────────────────────────────────────────────────────────
$SCRIPT_DIR   = $PSScriptRoot
$REPO_ROOT    = Split-Path $SCRIPT_DIR
$NUITKA_CACHE = "$REPO_ROOT\dist\.nuitka_cache"
$BRIDGE_SRC   = "$NUITKA_CACHE\bridge_main.dist"   # Nuitka output dir
$ELECTRON_DIR = "$REPO_ROOT\electron"

# ── Helpers ───────────────────────────────────────────────────────────────────
function Step  { param([string]$msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok    { param([string]$msg) Write-Host "    [OK]  $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "    [!!]  $msg" -ForegroundColor Yellow }
function Fail  { param([string]$msg) Write-Host "`n[FAIL] $msg" -ForegroundColor Red; exit 1 }

function Invoke-Checked {
    param([string]$Desc, [scriptblock]$Cmd)
    Write-Host "  $ $Cmd" -ForegroundColor DarkGray
    & $Cmd
    if ($LASTEXITCODE -ne 0) { Fail "$Desc failed (exit $LASTEXITCODE)" }
}

# ── Job count ─────────────────────────────────────────────────────────────────
if ($Jobs -le 0) {
    $Jobs = [math]::Min([int]$env:NUMBER_OF_PROCESSORS, 16)
}

# ── LTO mode ──────────────────────────────────────────────────────────────────
# Off by default: the LTO link step runs every build and is not cacheable, so it
# dominates incremental rebuilds. -Release turns it on (smaller/faster exe).
$ltoMode = if ($Release) { 'auto' } else { 'no' }

# ── Banner ────────────────────────────────────────────────────────────────────
Write-Host @"

============================================================
  HandQ Build Script
  Bridge : Nuitka standalone (Python 3.12)
  Frontend: electron-builder (Windows NSIS)
------------------------------------------------------------
  Repo root : $REPO_ROOT
  Nuitka cache: $NUITKA_CACHE
  Python      : $Python
  Jobs        : $Jobs
  LTO         : $ltoMode  (Release=$Release)
  Clean build : $Clean
  Bridge only : $BridgeOnly
  Electron only: $ElectronOnly
============================================================
"@ -ForegroundColor White

# ─────────────────────────────────────────────────────────────────────────────
# PRE-FLIGHT CHECKS
# ─────────────────────────────────────────────────────────────────────────────
Step 'Pre-flight checks'

# Split $Python into cmd + possible args (e.g. "py -3.12")
$pyParts = $Python -split ' ', 2
$pyCmd   = $pyParts[0]
$pyArgs  = if ($pyParts.Count -gt 1) { $pyParts[1] } else { '' }

function Invoke-Python {
    param([string]$Args)
    if ($pyArgs) {
        & $pyCmd $pyArgs.Split(' ') $Args.Split(' ')
    } else {
        & $pyCmd $Args.Split(' ')
    }
}

# Python version check
$pyVer = & $pyCmd ($pyArgs.Split(' ') + @('--version')) 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Python interpreter not found: $Python" }
if ($pyVer -notmatch '3\.12') { Warn "Expected Python 3.12, got: $pyVer  (continuing anyway)" }
Ok "Python: $pyVer"

if (-not $ElectronOnly) {
    # Nuitka check
    $nuitkaOut = & $pyCmd ($pyArgs.Split(' ') + @('-m', 'nuitka', '--version')) 2>&1
    $nuitkaExit = $LASTEXITCODE
    $nuitkaVer = $nuitkaOut | Select-Object -First 1
    if ($nuitkaExit -ne 0) {
        Fail "Nuitka not found for $Python. Install with: $Python -m pip install nuitka"
    }
    Ok "Nuitka: $nuitkaVer"

    # Compiler cache (C-compile step only): on MSVC, Nuitka auto-detects and uses
    # clcache, stored under %LOCALAPPDATA%\Nuitka\Nuitka\Cache\clcache. No flag is
    # needed (we do NOT pass --clcache). In practice it hits ~99%+ on incremental
    # builds, so the .c → .obj step is effectively free. IMPORTANT: clcache does
    # NOT cache the link step — with -Release (LTO on), the uncacheable LTCG link
    # still dominates rebuild time. To use ccache instead, install ccache 4.6+
    # (`choco install ccache`) and Nuitka will pick it up from PATH.
    if (Get-Command ccache -ErrorAction SilentlyContinue) {
        $ccVer = (& ccache --version 2>&1 | Select-Object -First 1)
        Ok "ccache (PATH): $ccVer"
    } else {
        Ok 'C-compile cache: clcache (auto-managed by Nuitka under %LOCALAPPDATA%\Nuitka)'
    }
}

if (-not $BridgeOnly) {
    # Node / npm
    $nodeVer = node --version 2>&1
    if ($LASTEXITCODE -ne 0) { Fail 'Node.js not found. Install from https://nodejs.org' }
    Ok "Node: $nodeVer"

    # electron-builder
    $ebCheck = & npm exec -- electron-builder --version 2>&1 | Select-Object -First 1
    # also accept locally installed
    $localEb = "$ELECTRON_DIR\node_modules\.bin\electron-builder.cmd"
    if (-not (Test-Path $localEb)) {
        Warn 'electron-builder not found in electron\node_modules.'
        Warn "Run first:  cd electron && npm install"
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — NUITKA: BUILD THE PYTHON BRIDGE
# ─────────────────────────────────────────────────────────────────────────────
if (-not $ElectronOnly) {
    Step 'Building Python bridge with Nuitka'

    if ($Clean -and (Test-Path $NUITKA_CACHE)) {
        Write-Host "  Removing Nuitka cache: $NUITKA_CACHE" -ForegroundColor DarkGray
        Remove-Item -Recurse -Force $NUITKA_CACHE
        Ok 'Nuitka cache wiped'
    }

    New-Item -ItemType Directory -Force $NUITKA_CACHE | Out-Null

    # ── Version stamp (single source of truth = electron/package.json) ──────────
    # The source handq_config.example.yaml ships a fixed 'version: 0.0.0'
    # placeholder; stamp the real release version into a temp copy and embed
    # THAT, so the shipped handq_config.yaml carries a correct version that was
    # never hand-maintained. Without this, bridge_main._merge_user_config_with_seed
    # would see 0.0.0 and never migrate an upgrading user's config.
    $APP_VERSION = (Get-Content "$ELECTRON_DIR\package.json" -Raw | ConvertFrom-Json).version
    if (-not $APP_VERSION) { Fail "Could not read version from electron\package.json" }
    $STAMPED_CONFIG = "$NUITKA_CACHE\handq_config.stamped.yaml"
    (Get-Content "$REPO_ROOT\handq_config.example.yaml") `
        -replace '^version:.*', "version: '$APP_VERSION'" `
        | Set-Content -Encoding utf8 $STAMPED_CONFIG
    Ok "Stamped version $APP_VERSION into shipped handq_config.yaml"

    $verboseFlags = @()
    if ($ShowProgress) { $verboseFlags = @('--show-progress', '--show-memory') }

    $nuitkaArgs = @(
        '-m', 'nuitka',

        # ── Mode ──────────────────────────────────────────────────────────────
        '--standalone',
        '--assume-yes-for-downloads',

        # ── Output ────────────────────────────────────────────────────────────
        "--output-dir=$NUITKA_CACHE",
        '--output-filename=bridge_main',        # → .exe renamed to handq-bridge.exe below
        '--windows-console-mode=force',
        '--company-name=HandQ',
        '--product-name=HandQ Bridge',
        '--file-version=1.0.0.0',
        '--product-version=1.0.0.0',
        '--file-description=HandQ AI Agent Bridge',

        # ── Build performance ─────────────────────────────────────────────────
        "--jobs=$Jobs",
        "--lto=$ltoMode",     # off by default (uncacheable LTCG link); -Release → auto

        # ── Our source package ────────────────────────────────────────────────
        '--include-package=src',

        # ── Always-required third-party packages ──────────────────────────────
        # All deps are imported eagerly at module top in V2 — Nuitka's static
        # analysis will pick them up. We list them anyway as a belt-and-braces
        # check so any future refactor that hides an import behind a function
        # still ships the package.
        '--include-package=yaml',           # PyYAML: stdio_bridge, config_manager, ssh_tool, browser_tool
        '--include-package=anthropic',      # LLM client: llm_service, anthropic_streaming_service
        '--include-package=httpx',          # HTTP: llm_service, vision/llm.py, teams_api, LTM embedding
        '--include-package=json_repair',    # JSON repair: utils.py (eager top-level)
        '--include-package=openai',         # vision/llm.py + LTM embedding (eager top-level)
        '--include-package=PIL',            # pillow: browser_tool, vision/llm.py, desktop_tool
        '--include-package=paramiko',       # ssh_tool, ssh_setup, remote_handq_tool
        '--include-package=keyring',        # ssh_tool, ssh_setup
        '--include-package=keyrings',       # keyrings.alt: file-based backend for headless envs
        '--include-package=cryptography',   # transitive dep of paramiko + httpx TLS
        '--include-package=cffi',           # transitive dep of cryptography
        '--include-package=mss',            # desktop_tool.py: fast screen capture (eager)
        '--include-package=pyautogui',      # desktop_tool.py: mouse/keyboard automation (eager)
        '--include-package=win32gui',       # desktop_tool.py (pywin32): window enumeration
        '--include-package=win32process',   # desktop_tool.py (pywin32): PID lookups
        '--include-package=win32con',       # desktop_tool.py (pywin32): win32 constants
        '--include-package=pywintypes',     # pywin32: shared C types required by win32gui etc.
        '--include-package=win32com',       # email_tool.py: Dispatch / EnsureDispatch (eager)
        '--include-package=pythoncom',      # email_tool.py: CoInitialize / CoUninitialize (eager)
        '--include-package-data=win32com',  # gen_py cache support under Nuitka (doc §13)
        '--include-package=playwright',     # browser_tool.py + teams_setup + teams_web_bridge (eager)
        '--include-package=pywinauto',      # desktop_tool.py: UIA control discovery (eager)
        '--include-package=comtypes',       # transitive dep of pywinauto: UIA COM bindings.
                                            # pywinauto imports comtypes.stream lazily inside
                                            # uia_element_info — Nuitka's static analysis misses
                                            # it and post-pack raises ModuleNotFoundError on the
                                            # first UIA call. Pull the whole package so all
                                            # submodules (stream, automation, gen, ...) ship.
        '--include-package=pdfplumber',     # read_tool.py: canonical PDF parser (eager)
        '--include-package=rapidocr_onnxruntime',  # vision/ocr.py: local OCR engine (eager)
        '--include-package=rapidfuzz',      # desktop_tool.py: fuzzy text match (eager)
        '--include-package=psutil',         # desktop_tool.py + personality.input_idle (eager)

        # ── Package data that must travel alongside the exe ───────────────────
        # rapidocr ships det/rec/cls *.onnx model files (~10 MB) as package data.
        # Without this flag Nuitka omits them; find_element fails post-pack with
        # "model not found".
        '--include-package-data=rapidocr_onnxruntime',

        # handq_config.yaml lands next to handq-bridge.exe in the standalone
        # dist so INSTALL_DIR/handq_config.yaml resolves correctly (ARCHITECTURE §1).
        # We ship the .example.yaml (API_KEY redacted) under the dist filename
        # `handq_config.yaml` — bridge_main._ensure_user_config_present() copies
        # it to %USERPROFILE%\HandQ\handq_config.yaml on first run, where the
        # user fills in their API key. Embedding the dev's working yaml would
        # leak whichever API key happens to be in it.
        # We embed the version-stamped temp copy (see $STAMPED_CONFIG above),
        # not the raw source file, so the shipped config carries the real
        # release version rather than the 0.0.0 placeholder.
        "--include-data-files=$STAMPED_CONFIG=handq_config.yaml",

        # uia_query.ps1 — PowerShell UI Automation worker script for the LTM
        # Dream/Personality pipeline. Spawned as a long-lived child process by
        # ``infrastructure.long_term_memory.uia_worker.UIAWorker`` to extract
        # ax_text / parsed_json / top_window_titles from foreground windows,
        # which augment OCR snapshots with structured signals (URLs in browsers,
        # paths in Explorer/VSCode, prompts in terminals).
        #
        # The worker resolves the script via ``Path(__file__).parent / "scripts"
        # / "uia_query.ps1"``; in the standalone dist, ``__file__`` for the
        # compiled uia_worker module maps to
        # ``<dist>/src/infrastructure/long_term_memory/uia_worker.py`` (virtual),
        # so the resource MUST land at the same relative path. Without this
        # directive the file is missing post-pack and ``_spawn`` logs
        # "UIA script not found" then permanently disables the worker — the
        # entire ax_text augmentation channel goes silently dark on every
        # fresh install.
        "--include-data-files=$REPO_ROOT\src\infrastructure\long_term_memory\scripts\uia_query.ps1=src\infrastructure\long_term_memory\scripts\uia_query.ps1",

        # ── Size reduction: trim unused sub-packages of large deps ────────────
        # Rules here only if the module is a genuinely separate entry-point that
        # the SDK core never imports itself. Fine-grained resource-level exclusions
        # (openai.resources.*, anthropic.resources.*) are NOT safe: the SDK's
        # _client.py imports all resource sub-packages at class-definition time,
        # so any one of them being missing causes an ImportError at startup.
        #
        # openai.cli  — standalone CLI script, never imported by the SDK core.
        '--nofollow-import-to=openai.cli',
        # openai._extras — DO NOT exclude. The comment used to claim it
        # was "all guarded with try/except in the SDK", but
        # ``openai.resources.embeddings`` does an unguarded top-level
        # import that resolves through _extras at module load. Excluding
        # it makes embeddings.create() raise ImportError on every call
        # (LTM warmup + retriage worker both fail silently to logs).
        # Keeping it costs ~tens of KB and makes the embedding path work.
        # playwright: async_api and sync_api are independent; we only use async.
        '--nofollow-import-to=playwright.sync_api',

        # ── Exclude dev / test / profiling tooling ────────────────────────────
        '--nofollow-import-to=pytest',
        '--nofollow-import-to=_pytest',
        '--nofollow-import-to=tests',
        '--nofollow-import-to=setuptools',
        '--nofollow-import-to=pip',
        '--nofollow-import-to=wheel',
        '--nofollow-import-to=distutils',
        '--nofollow-import-to=ensurepip',
        '--nofollow-import-to=venv',
        '--nofollow-import-to=lib2to3',
        '--nofollow-import-to=pdb',
        '--nofollow-import-to=pdbpp',
        '--nofollow-import-to=profile',
        '--nofollow-import-to=cProfile',
        '--nofollow-import-to=pstats',
        '--nofollow-import-to=timeit',
        '--nofollow-import-to=trace',

        # ── Exclude GUI toolkits ──────────────────────────────────────────────
        '--nofollow-import-to=tkinter',
        '--nofollow-import-to=turtle',
        '--nofollow-import-to=idlelib',
        '--nofollow-import-to=wx',
        '--nofollow-import-to=PyQt5',
        '--nofollow-import-to=PyQt6',
        '--nofollow-import-to=PySide2',
        '--nofollow-import-to=PySide6',

        # ── Exclude unused network / legacy-protocol stdlib modules ───────────
        '--nofollow-import-to=xmlrpc',
        '--nofollow-import-to=ftplib',
        '--nofollow-import-to=imaplib',
        '--nofollow-import-to=poplib',
        '--nofollow-import-to=smtplib',
        '--nofollow-import-to=telnetlib',
        '--nofollow-import-to=nntplib',

        # ── Exclude unused stdlib email / encoding helpers ────────────────────
        # email_tool reads Outlook items via win32com.client.Dispatch — it
        # never composes MIME from scratch, so the email package's writer
        # surface is unused. Keep the core (email.message / email.parser
        # are pulled in transitively if needed); just drop the writer-only
        # submodules.
        '--nofollow-import-to=email.contentmanager',
        '--nofollow-import-to=email.headerregistry',
        '--nofollow-import-to=mailbox',
        # mimetypes must NOT be excluded — httpx._multipart imports it at runtime.
        '--nofollow-import-to=uu',
        '--nofollow-import-to=quopri',

        # ── Exclude Jupyter / IPython ecosystem ───────────────────────────────
        '--nofollow-import-to=IPython',
        '--nofollow-import-to=ipykernel',
        '--nofollow-import-to=notebook',
        '--nofollow-import-to=nbformat',
        '--nofollow-import-to=nbconvert',

        # ── Python interpreter flags ──────────────────────────────────────────
        # no_docstrings: strip all docstrings → measurable size reduction for
        # docstring-heavy packages (anthropic, openai).
        '--python-flag=no_docstrings',
        # no_site: skip site.py at startup; not needed for an embedded bridge.
        '--python-flag=no_site',

        # ── Build report ──────────────────────────────────────────────────────
        "--report=$NUITKA_CACHE\nuitka-bridge-report.xml"
    )

    $nuitkaArgs += $verboseFlags

    # Must run from repo root so relative paths (bridge_main.py, src/, config) resolve.
    Push-Location $REPO_ROOT
    try {
        Write-Host "`n  [Nuitka] compiling bridge_main.py — first build ~10-20 min, incremental ~2 min" -ForegroundColor DarkGray
        # Build the python invocation argv. We avoid the PS7-only ternary
        # operator here so the script also runs under Windows PowerShell 5.1.
        $pyArgsArray = if ($pyArgs) { $pyArgs.Split(' ') } else { @() }
        & $pyCmd $pyArgsArray $nuitkaArgs 'bridge_main.py'
        if ($LASTEXITCODE -ne 0) { Fail 'Nuitka compilation failed' }
    }
    finally {
        Pop-Location
    }
    Ok 'Nuitka compilation complete'

    # ── Rename output exe ──────────────────────────────────────────────────────
    $builtExe   = "$BRIDGE_SRC\bridge_main.exe"
    $renamedExe = "$BRIDGE_SRC\handq-bridge.exe"

    if (-not (Test-Path $BRIDGE_SRC)) {
        Fail "Expected Nuitka output dir not found: $BRIDGE_SRC"
    }
    if (-not (Test-Path $builtExe)) {
        Fail "Expected compiled exe not found: $builtExe"
    }

    if (Test-Path $renamedExe) { Remove-Item -Force $renamedExe }
    Rename-Item -Path $builtExe -NewName 'handq-bridge.exe'
    Ok "Renamed bridge_main.exe → handq-bridge.exe"

    # ── Verify config is present ───────────────────────────────────────────────
    if (-not (Test-Path "$BRIDGE_SRC\handq_config.yaml")) {
        Warn "handq_config.yaml not found in $BRIDGE_SRC — electron-builder won't include it."
        Warn "Check the --include-data-files flag above."
    } else {
        Ok 'handq_config.yaml present in bridge dist'
    }

    Write-Host "`n  Bridge dist contents:" -ForegroundColor DarkGray
    Get-ChildItem $BRIDGE_SRC | Select-Object -ExpandProperty Name |
        ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — ELECTRON-BUILDER: PACKAGE THE FRONTEND
# ─────────────────────────────────────────────────────────────────────────────
if (-not $BridgeOnly) {
    Step 'Building Electron frontend with electron-builder'

    # Verify the bridge dist is in place (electron-builder needs it for extraFiles)
    if (-not (Test-Path "$BRIDGE_SRC\handq-bridge.exe")) {
        Fail "Bridge dist missing at $BRIDGE_SRC\handq-bridge.exe.`nRun without -ElectronOnly to build it first."
    }

    Push-Location $ELECTRON_DIR
    try {
        # Install node_modules if missing
        if (-not (Test-Path 'node_modules')) {
            Write-Host '  node_modules missing — running npm install...' -ForegroundColor DarkGray
            npm install
            if ($LASTEXITCODE -ne 0) { Fail 'npm install failed' }
        }

        # Run electron-builder via local npx (avoids global install requirement).
        # Default = NSIS installer only. To also produce an unpacked smoke-test
        # tree, run `npm run dist:dir` separately from electron/ — the dir target
        # is intentionally not part of the default build to keep dist/installer
        # to a single artifact.
        Write-Host '  Running electron-builder (NSIS target only)...' -ForegroundColor DarkGray
        npx electron-builder --win nsis --x64
        if ($LASTEXITCODE -ne 0) { Fail 'electron-builder failed' }
    }
    finally {
        Pop-Location
    }
    Ok 'electron-builder complete'
}

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
Write-Host @"

============================================================
  Build complete
------------------------------------------------------------
"@ -ForegroundColor Green

if (-not $ElectronOnly) {
    Write-Host "  Bridge dist : $BRIDGE_SRC" -ForegroundColor White
    Write-Host "  Build report: $NUITKA_CACHE\nuitka-bridge-report.xml" -ForegroundColor White
}
if (-not $BridgeOnly) {
    $installerOut = "$REPO_ROOT\dist\installer"
    Write-Host "  Installer   : $installerOut\HandQ Setup <ver>.exe" -ForegroundColor White
}

Write-Host @"

  Smoke-test (install on this machine):
    Run the installer from $REPO_ROOT\dist\installer
    OR for an unpacked tree without installing, run separately:
      cd $ELECTRON_DIR && npm run dist:dir

  First-run note:
    %USERPROFILE%\HandQ\handq_config.yaml does not exist yet.
    The bridge will fall back to the shipped default inside the
    installation directory (ARCHITECTURE.md §1, §4.3).
============================================================
"@ -ForegroundColor DarkGray
