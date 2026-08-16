<#
.SYNOPSIS
    Upload HandQ source artifacts to the network share (overwrite mode).

.DESCRIPTION
    Copies the following items from the project root to the destination
    share, overwriting any existing files at the destination. Files that
    exist only at the destination are kept (use -Mirror to delete them).

      handq_linux.py
      handq_setup.sh
      handq_config.example.yaml
      packaging/   (recursive)
      src/         (recursive)

    Common build/cache junk (__pycache__, *.pyc, .pytest_cache, etc.) is
    excluded automatically.

.PARAMETER Destination
    UNC path to upload to. Defaults to the GENIE/latest share.

.PARAMETER Mirror
    Enable mirror mode: deletes files/dirs at the destination that are not
    present in the source. Off by default (pure overwrite).

.PARAMETER DryRun
    Print what would be copied without actually copying.

.EXAMPLE
    .\upload_to_share.ps1
    .\upload_to_share.ps1 -DryRun
    .\upload_to_share.ps1 -Destination '\\someserver\some\path'
#>
[CmdletBinding()]
param(
    [string]$Destination = '\\wine\APTAuto\ADAS\fengxuan\script_test\GENIE\latest',
    [switch]$Mirror,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Project root = directory containing this script (fall back to CWD).
$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }

# Items to upload (paths relative to project root).
$Items = @(
    'handq_linux.py',
    'handq_setup.sh',
    'handq_config.example.yaml',
    'packaging',
    'src',
    'electron/package.json',
    'Skill'
)

# Exclusions for directory copies.
$ExcludeDirs  = @('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
                  '.nuitka_cache', '__nuitka_build__', 'node_modules', '.git')
$ExcludeFiles = @('*.pyc', '*.pyo', '*.pyd', '.DS_Store', 'Thumbs.db')

Write-Host "HandQ upload" -ForegroundColor Cyan
Write-Host "  Source     : $ProjectRoot"
Write-Host "  Destination: $Destination"
Write-Host "  Mode       : $(if ($Mirror) { 'MIRROR (deletes dest-only files)' } else { 'OVERWRITE (keep dest extras)' })"
if ($DryRun) { Write-Host "  Dry run    : YES (no files will be written)" -ForegroundColor Yellow }
Write-Host ''

# Verify all source items exist before touching the network.
$missing = @()
foreach ($item in $Items) {
    $src = Join-Path $ProjectRoot $item
    if (-not (Test-Path -LiteralPath $src)) { $missing += $item }
}
if ($missing.Count -gt 0) {
    Write-Error "Missing source items: $($missing -join ', ')"
    exit 1
}

# Verify / create destination root.
if (-not (Test-Path -LiteralPath $Destination)) {
    if ($DryRun) {
        Write-Host "[dry-run] would create destination: $Destination" -ForegroundColor Yellow
    } else {
        Write-Host "Creating destination: $Destination" -ForegroundColor Yellow
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }
}

# robocopy flags:
#   /E   recurse including empty dirs
#   /IS  re-copy files even if size+timestamp match (force overwrite)
#   /IT  include attribute-only changes
#   /MT:8        multithreaded
#   /R:1 /W:1    retry once with 1s wait (default 1M retries hangs forever)
#   /NFL /NDL /NP /NJH /NJS  quieter output
$RoboArgs = @('/E', '/IS', '/IT', '/MT:8', '/R:1', '/W:1',
              '/NFL', '/NDL', '/NP', '/NJH', '/NJS')
if ($Mirror) { $RoboArgs += '/PURGE' }
if ($DryRun) { $RoboArgs += '/L' }
$RoboArgs += @('/XD') + $ExcludeDirs
$RoboArgs += @('/XF') + $ExcludeFiles

$copied = 0
$failed = 0

foreach ($item in $Items) {
    $src = Join-Path $ProjectRoot $item
    $dst = Join-Path $Destination $item

    if (Test-Path -LiteralPath $src -PathType Container) {
        Write-Host "[DIR ] $item" -ForegroundColor Green
        & robocopy $src $dst @RoboArgs | Out-Null
        $rc = $LASTEXITCODE
        # robocopy: 0-7 = success (various states); 8+ = real error.
        if ($rc -ge 8) {
            Write-Host "       robocopy failed (exit=$rc)" -ForegroundColor Red
            $failed++
        } else {
            $copied++
        }
    } else {
        Write-Host "[FILE] $item" -ForegroundColor Green
        if (-not $DryRun) {
            $dstParent = Split-Path -Parent $dst
            if (-not (Test-Path -LiteralPath $dstParent)) {
                New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
        $copied++
    }
}

Write-Host ''
if ($failed -eq 0) {
    Write-Host "Upload complete: $copied item(s)$(if ($DryRun) { ' (dry run)' })." -ForegroundColor Green
    exit 0
} else {
    Write-Host "Upload finished with $failed failure(s) ($copied succeeded)." -ForegroundColor Red
    exit 1
}
