# mention_search.ps1 — resident Windows SystemIndex query worker.
#
# Speaks single-line JSON on stdin/stdout with electron/main.js:
#
#   in  : {"id":"q1","query":"foo"}
#   out : {"id":"q1","results":[{path,name,parent,isDir}, ...]}
#
# On startup emits a handshake line: {"ready":true} once the ADODB.Connection
# to Provider=Search.CollatorDSO is open, or {"ready":false,"error":"..."}
# followed by exit 1 when Windows Search is unavailable. main.js flips the
# feature off after that.

# Force UTF-8 both directions so Chinese / Unicode paths survive the pipe.
# PowerShell 5.1 defaults to the console code page (often GBK on zh-CN),
# which mojibake's non-ASCII on the way to Node.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding           = [System.Text.UTF8Encoding]::new($false)

$conn = $null
try {
    $conn = New-Object -ComObject ADODB.Connection
    $conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
} catch {
    [Console]::Out.WriteLine((@{ ready = $false; error = "$_" } | ConvertTo-Json -Compress))
    exit 1
}
[Console]::Out.WriteLine((@{ ready = $true } | ConvertTo-Json -Compress))

# One-line JSON echo helper — always -Compress so it stays a single line, which
# is what main.js's readline expects.
function Emit($obj) {
    [Console]::Out.WriteLine(($obj | ConvertTo-Json -Depth 4 -Compress))
}

while ($null -ne ($line = [Console]::In.ReadLine())) {
    $line = $line.Trim()
    if (-not $line) { continue }

    try {
        $req = $line | ConvertFrom-Json
    } catch {
        Emit @{ error = "malformed JSON: $_" }
        continue
    }

    $id    = $req.id
    $query = "$($req.query)"
    if (-not $query) { Emit @{ id = $id; results = @() }; continue }

    # Build a fuzzy sub-sequence LIKE pattern: each user char becomes its own
    # SQL-safe token, then all tokens are joined by '%'. So "rme" becomes
    # "%r%m%e%" and matches "README.md", "resume.md", etc. Each char is
    # bracket-escaped so LIKE meta-chars in the raw input (', [, %, _) stay
    # literal.
    $parts = New-Object System.Collections.ArrayList
    foreach ($ch in $query.ToCharArray()) {
        switch ($ch) {
            "'" { [void]$parts.Add("''") }
            '[' { [void]$parts.Add("[[]") }
            '%' { [void]$parts.Add("[%]") }
            '_' { [void]$parts.Add("[_]") }
            default { [void]$parts.Add([string]$ch) }
        }
    }
    $fuzzy = $parts -join '%'

    $sql = "SELECT TOP 20 System.ItemPathDisplay, System.ItemType, " +
           "System.ItemFolderPathDisplay, System.ItemNameDisplay " +
           "FROM SystemIndex WHERE System.FileName LIKE '%$fuzzy%' " +
           "ORDER BY System.DateModified DESC"

    $results = New-Object System.Collections.ArrayList
    try {
        $rs = $conn.Execute($sql)
        while (-not $rs.EOF) {
            $itemType = [string]$rs.Fields.Item("System.ItemType").Value
            [void]$results.Add(@{
                path   = [string]$rs.Fields.Item("System.ItemPathDisplay").Value
                name   = [string]$rs.Fields.Item("System.ItemNameDisplay").Value
                parent = [string]$rs.Fields.Item("System.ItemFolderPathDisplay").Value
                isDir  = ($itemType -eq "Directory")
            })
            $rs.MoveNext()
        }
        $rs.Close()
        Emit @{ id = $id; results = $results }
    } catch {
        Emit @{ id = $id; error = "$_"; results = @() }
    }
}
