# UIA query loop — stdin JSON requests, stdout JSON responses.
# Loaded by uia_worker.py via:
#   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <this>
#
# Protocol:
#   stdin  (one JSON object per line): {"req": "query", "hwnd": <int>, "depth_limit": <int>}
#   stdout (one JSON object per line): {"ax_text": "...", "parsed_json": {...},
#                                       "top_window_titles": [...]}
#
# On parse failure or any error, emits {"error": "<msg>"} but stays in the loop.
# Sends {"ready": true} once after init; uia_worker.py uses it as readiness signal.

Add-Type -AssemblyName UIAutomationClient -ErrorAction SilentlyContinue
Add-Type -AssemblyName UIAutomationTypes -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue

function Get-AxText {
    param([System.Windows.Automation.AutomationElement]$Root, [int]$DepthLimit)
    if ($null -eq $Root) { return "" }
    $parts = New-Object System.Collections.Generic.List[string]
    $walker = [System.Windows.Automation.TreeWalker]::ContentViewWalker
    function Recurse {
        param($Node, $Depth)
        if ($null -eq $Node -or $Depth -gt $DepthLimit) { return }
        try {
            $name = $Node.Current.Name
            if (-not [string]::IsNullOrWhiteSpace($name) -and $name.Length -lt 400) {
                $parts.Add($name)
            }
        } catch {}
        try {
            $child = $walker.GetFirstChild($Node)
            while ($null -ne $child) {
                Recurse -Node $child -Depth ($Depth + 1)
                $child = $walker.GetNextSibling($child)
            }
        } catch {}
    }
    Recurse -Node $Root -Depth 0
    return ($parts -join " | ").Substring(0, [Math]::Min(4000, ($parts -join " | ").Length))
}

function Get-BrowserUrl {
    param([System.Windows.Automation.AutomationElement]$Root)
    if ($null -eq $Root) { return $null }
    try {
        # Browser address bar pattern: AutomationId=address-bar or
        # ClassName=Chrome_OmniboxView etc. Try a generic ControlType.Edit search.
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Edit
        )
        $edits = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
        foreach ($e in $edits) {
            $name = $e.Current.Name
            if ($name -match '^https?://' -or $name -like 'about:*') {
                return $name
            }
        }
    } catch {}
    return $null
}

function Get-TopWindowTitles {
    try {
        $titles = @()
        foreach ($p in [System.Diagnostics.Process]::GetProcesses()) {
            try {
                $t = $p.MainWindowTitle
                if (-not [string]::IsNullOrWhiteSpace($t)) {
                    $titles += $t
                }
            } catch {}
        }
        # Cap at 12 unique titles.
        $unique = $titles | Select-Object -Unique | Select-Object -First 12
        return ,$unique
    } catch {
        return ,@()
    }
}

# Signal readiness.
'{"ready": true}' | Out-Host
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try {
        $req = $line | ConvertFrom-Json
    } catch {
        $resp = @{ error = "invalid_json" } | ConvertTo-Json -Compress
        $resp | Out-Host
        [Console]::Out.Flush()
        continue
    }
    if ($req.req -eq "ping") {
        '{"pong": true}' | Out-Host
        [Console]::Out.Flush()
        continue
    }
    if ($req.req -eq "query") {
        $hwnd = [IntPtr][long]$req.hwnd
        $depthLimit = if ($req.depth_limit) { [int]$req.depth_limit } else { 4 }
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
            $axText = Get-AxText -Root $root -DepthLimit $depthLimit
            $url = Get-BrowserUrl -Root $root
            $titles = Get-TopWindowTitles
            $parsed = @{}
            if ($url) { $parsed.url = $url }
            $resp = @{
                ax_text = $axText
                parsed_json = $parsed
                top_window_titles = $titles
            }
            $resp | ConvertTo-Json -Compress -Depth 4 | Out-Host
        } catch {
            $resp = @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
            $resp | Out-Host
        }
        [Console]::Out.Flush()
        continue
    }
    $resp = @{ error = "unknown_req"; req = $req.req } | ConvertTo-Json -Compress
    $resp | Out-Host
    [Console]::Out.Flush()
}
