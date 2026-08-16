"""Permanent backfill: materialize scratch_resume/digests.json's 51 real
sessions as REAL digest.json files via the production SessionDigest class.

Unlike the earlier throwaway smoke-test version of this script (deleted
after Phase 2 verification), this run is meant to PERSIST — these 51
historical sessions get a permanent digest.json so they enter resume's
search corpus. Backfilled digests are honest about their provenance:
`agent_summary` carries a marker noting they were reconstructed from the
JSONL trace, not produced by a real destroy()/checkpoint cycle, so anyone
inspecting the file later isn't misled into thinking it's a live-path
artifact.

Field mapping notes (digests.json's shape predates the final SessionDigest
schema, so this is NOT a blind dict-splat):
  - digests.json's `completed[].goal` has no home in TaskResult (which has
    no `goal` field) — it's dropped; `final_answer`/`artifacts`/
    `key_findings` map directly.
  - digests.json's `_bm25_corpus` was scratch-only derived data (the
    original exploration script's own BM25 corpus string) — not part of
    SessionDigest at all, dropped.
  - TaskResult's other fields (`verification`, `issues`, `plan_feedback`,
    `iterations`, `token_usage`, `completed_at`) don't exist in the
    trace-derived source data — left at TaskResult's own defaults.

Idempotent: skips any session_dir that already has a digest.json (never
clobbers a real destroy()-produced digest), so re-running after this is
a safe no-op — already verified live 2026-07-31: all 51 written, all 51
reload correctly via SessionDigest.load(), real ResumeIndex.search()
against the resulting 52-entry corpus returns correct high-confidence
hits and stays silent on unrelated queries.

Run once: python scripts\\backfill_historical_digests.py
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.controller_v2.session_digest import SessionDigest  # noqa: E402
from src.controller_v2.task_channel import TaskResult  # noqa: E402


def _task_result_from_legacy_completed(c: dict) -> dict:
    """digests.json's completed[] entries predate TaskResult's real shape.
    Round-trip through the actual dataclass (not a hand-built dict) so the
    written digest.json is byte-for-byte what SessionDigest.save() would
    produce from a real TaskResult, and any future TaskResult field
    addition surfaces here as a constructor error instead of silently
    producing a malformed digest."""
    result = TaskResult(
        item_id=c.get("item_id", ""),
        success=bool(c.get("success", False)),
        final_answer=c.get("final_answer", "") or "",
        artifacts=list(c.get("artifacts") or []),
        key_findings=list(c.get("key_findings") or []),
    )
    return asdict(result)


def main() -> None:
    source = REPO_ROOT / "scratch_resume" / "digests.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    written = 0
    skipped_existing = 0
    skipped_missing_dir = 0

    for entry in raw:
        session_dir = Path(entry["session_dir"])
        if not session_dir.exists():
            skipped_missing_dir += 1
            continue
        target = session_dir / SessionDigest.DIGEST_FILENAME
        if target.exists():
            skipped_existing += 1
            continue  # never clobber a real destroy()-produced digest

        digest = SessionDigest(
            session_id=entry.get("session_id", ""),
            title=entry.get("title", ""),
            created_at=entry.get("created_at", ""),
            updated_at=entry.get("updated_at", ""),
            workspace_dir=str(session_dir / ".workspace"),
            workspace_files=entry.get("workspace_files", []),
            status=entry.get("status", "destroyed"),
            conversation=entry.get("conversation", []),
            completed=[
                _task_result_from_legacy_completed(c)
                for c in entry.get("completed", [])
            ],
            current=None,
            pending=[],
            active_tools=[],
            active_goal=None,
            agent_summary=(
                "[Backfilled 2026-07-31] Reconstructed from the JSONL "
                "execution trace, not a real destroy()/checkpoint cycle — "
                "queue/active_tools/active_goal are empty by construction "
                "(the trace doesn't carry them)."
            ),
        )
        digest.save(session_dir)
        written += 1

    print(
        f"wrote {written} digest.json files "
        f"(skipped_existing={skipped_existing}, "
        f"skipped_missing_dir={skipped_missing_dir})"
    )


if __name__ == "__main__":
    main()
