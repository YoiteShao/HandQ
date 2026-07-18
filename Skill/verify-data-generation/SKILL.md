---
name: verify-data-generation
description: When an item generates data (extraction, transformation, scraping, a CSV/report) and you need to confirm it is correct, not just present
origin: bundled
---
# Verify generated data orthogonally

When you produce data, verify it from a DIFFERENT ANGLE than the one that
produced it. Re-running the same method only re-confirms the same bug.

## Orthogonal checks (pick what fits)
- **Completeness**: "expected N rows from M source files; got N — do they match?"
  e.g. output line count vs `grep -c` across the sources.
- **Plausibility**: catch systematic false-negatives — "if the source clearly
  contains category X, does the output have at least one X?"
- **Boundary**: does the largest / smallest / first / last entry make sense for
  the domain?
- **Cross-count**: `wc -l` on source vs row count in output.

## When to skip
- The task already names its own verification command → just run that.
- The action is trivially correct (one small file with known content).

One orthogonal check beats ten redundant re-reads: it catches the wrong regex,
the missed edge case, the off-by-one that a re-run cannot.
