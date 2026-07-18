---
name: cache-large-discovery
description: When a discovery command (find/grep/ls) returns a large result set (>20 entries) you will need across multiple later steps
origin: bundled
---
# Cache a large discovery result

When a discovery command returns many entries (>20) that later steps will reuse,
save it once to a temp file instead of re-running the scan every turn.

## Windows (PowerShell)
```
Get-ChildItem -Recurse -Filter *.bat "C:\some\large\dir" |
  Select-Object -ExpandProperty FullName |
  Set-Content "$env:TEMP\handq_filelist_<short>.txt"
# later: Get-Content "$env:TEMP\handq_filelist_<short>.txt"
```

## Linux / macOS
```
find /some/large/dir -name '*.sh' > /tmp/handq_filelist_<short>.txt
# later: xargs ... < /tmp/handq_filelist_<short>.txt
```

Naming convention: `handq_<type>_<short_description>.txt` under the temp dir.

Re-running the same expensive scan every turn bloats context and wastes time —
scan once, read the cache thereafter.
