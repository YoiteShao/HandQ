"""
Memory System - Agent memory (conversation history, context, workspace state).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from pathlib import Path

from ..models.plan import Step, StepStatus

from .llm_pool import call_with_fallback

if TYPE_CHECKING:
    from .llm_service import LLMService

@dataclass
class Message:
    """Conversation message."""
    role: str  # system, user, assistant
    content: str
    timestamp: datetime = field(default_factory=datetime.now)


class Memory:
    """Agent memory store (global, per-session)."""

    def __init__(self, working_directory: str = "."):
        self.conversation_history: List[Message] = []
        self.completed_steps: List[Step] = []
        self.working_directory = str(Path(working_directory).absolute())
        self.modified_files: List[str] = []

        # Cross-step knowledge accumulation — successful steps only.
        # Each entry is a dict extracted from a successfully completed step:
        #   {"description": str, "outcome": str, "key_findings": List[str],
        #    "artifacts": List[str]}
        # outcome is stored as a joined string ("; ".join(step.factual_outcome))
        # for compact display in context injection.
        # This is the primary mechanism for breaking agent isolation: subsequent
        # agents and the planner can consult this store to avoid redundant work
        # and make globally-informed decisions, without relying on truncated
        # to_planner_summary() snippets.
        self._step_context_entries: List[Dict[str, Any]] = []

        # Failed approach tracking — persists across the full task lifetime.
        # Each entry records a step that failed so the planner can avoid
        # repeating the same approach, even for steps that have scrolled out
        # of the detail window in completed_summary.
        #   {"description": str, "goal": str, "issues": List[str]}
        # Included in get_accumulated_findings_for_planner() as a dedicated
        # section so the planner always has visibility into what was tried
        # and failed, regardless of how many steps have elapsed since.
        self._failed_approaches: List[Dict[str, Any]] = []

        # Compressed findings cache — populated by compress_findings_async().
        # _compressed_findings_summary: LLM-generated summary of the oldest
        #   _compressed_findings_boundary entries in _step_context_entries.
        # When set, get_accumulated_findings_for_planner() uses this summary
        # instead of dropping entries on budget overflow.
        self._compressed_findings_summary: Optional[str] = None
        self._compressed_findings_boundary: int = 0  # how many entries are summarised

        # SSH context — set by SSHContextProvider when credentials are established.
        # Keyed by hostname so parallel steps targeting different hosts each get
        # their own credentials without overwriting each other.
        # Persists for the lifetime of the task so subsequent steps reuse the same
        # credentials without re-prompting.
        self._ssh_contexts: Dict[str, Dict[str, Any]] = {}

        # Browser context — set by BrowserContextProvider on first activation.
        # Browser is process-wide (one persistent profile, one Playwright
        # session), so this is keyed by a constant slot. Phase 5 may add
        # additional keys for attach-mode CDP probe results so the agent
        # avoids re-probing the user's debug port every step.
        # Stored value shape: {"prepared": bool, "channel": str|None, ...}
        self._browser_contexts: Dict[str, Dict[str, Any]] = {}

    def set_ssh_context(self, hostname: str, creds_file: str, hint: str) -> None:
        """
        Store SSH credential context for a specific host.

        Called by SSHContextProvider after credentials are successfully
        established.  Subsequent steps retrieve this via get_ssh_context()
        to avoid re-prompting the user.

        Parameters
        ----------
        hostname : str
            Target hostname (used as cache key).
        creds_file : str
            Absolute path to the auto-generated credentials YAML file.
        hint : str
            Context hint string to inject into effective_goal.
        """
        self._ssh_contexts[hostname] = {"creds_file": creds_file, "hint": hint}

    def get_ssh_context(self, hostname: str) -> Optional[Dict[str, Any]]:
        """
        Return the stored SSH context dict for *hostname*, or None if not yet established.

        Returns a dict with keys:
          - ``creds_file``: absolute path to the credentials YAML
          - ``hint``: context hint string for effective_goal injection
        """
        return self._ssh_contexts.get(hostname)

    def get_all_ssh_contexts(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of all per-hostname SSH contexts established so far."""
        return dict(self._ssh_contexts)

    # ── Browser context ──────────────────────────────────────────────────────

    def set_browser_context(self, key: str, value: Dict[str, Any]) -> None:
        """Store browser-related context (prepare flag, attach probe, etc.).

        BrowserContextProvider uses ``key="default"`` to record that the
        first-touch hint has already been delivered, so subsequent steps
        receive a brief reminder instead of the full workflow guide.

        Phase 5 may use additional keys for attach-mode probe results.
        """
        self._browser_contexts[key] = dict(value)

    def get_browser_context(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the stored browser context for *key*, or None."""
        return self._browser_contexts.get(key)


    def add_step(self, step: Step) -> None:
        """Record a completed step and accumulate its structured findings.

        COMPLETED steps with at least one piece of structured info
        (outcome, key_findings, or artifacts) contribute a context entry to
        _step_context_entries — the cross-step knowledge base.

        FAILED steps with issues contribute an entry to _failed_approaches —
        the failure history that prevents the planner from repeating dead ends,
        even for steps that have scrolled out of the detail window.

        This builds two growing stores:
          - _step_context_entries: what was found/produced (successful work)
          - _failed_approaches: what was tried and failed (dead ends to avoid)

        Both are surfaced to the planner via get_accumulated_findings_for_planner()
        so it can make globally-informed decisions across the full task lifetime.
        Subsequent agents receive _step_context_entries as "[Prior step findings]"
        injected into their goal, so they are not isolated islands.
        """
        self.completed_steps.append(step)

        if step.status == StepStatus.COMPLETED:
            if not (step.key_findings or step.factual_outcome or step.artifacts):
                return
            self._step_context_entries.append({
                "step_id": step.step_id,
                "description": step.description,
                "outcome": "; ".join(step.factual_outcome) if step.factual_outcome else "",
                "key_findings": list(step.key_findings or []),
                "artifacts": list(step.artifacts or []),
            })
        elif step.status == StepStatus.FAILED and step.issues:
            # Record the failure so the planner can avoid repeating this approach.
            # Truncate goal to 300 chars to keep accumulated findings compact;
            # the full goal is visible in completed_summary for recent steps.
            self._failed_approaches.append({
                "description": step.description,
                "goal": (step.goal[:300] + "…") if len(step.goal) > 300 else step.goal,
                "issues": list(step.issues[:3]),  # Cap at 3 issues per step
            })

    def get_prior_step_context(self, max_chars: int = 30000, filter_keys: Optional[List[str]] = None, max_steps: Optional[int] = None) -> str:
        """Return accumulated findings for injection into agent goals.

        Gives each agent awareness of what previous steps discovered,
        preventing isolated-island execution and redundant work.  The agent
        decides which findings are relevant to its current goal — no
        task-specific filtering is applied here.

        Context budget rationale:
          With a 200k-token (~800k-char) context window, the dominant consumer
          is the agent's own conversation history (tool observations).  The
          prior step context injection is a small fraction of the total.  30,000
          chars (~7,500 tokens, ~4% of the window) is generous enough to carry
          rich findings from 20+ steps while leaving ample room for everything
          else.  Artifact paths are always preserved first since they are the
          primary mechanism for passing rich content between steps.

        Format strategy:
          Artifacts are listed first and are never subject to truncation —
          file paths are short and are the primary mechanism for passing rich
          content between steps.  Outcome and key_findings follow and may be
          truncated if the total exceeds max_chars.

        Args:
            max_chars: Soft cap on the returned string length.  Artifact lines
                       are always preserved; the remainder is truncated with an
                       ellipsis if it exceeds the cap.
            filter_keys: If provided and non-empty, only entries whose
                         step_id (stored under the "step_id" key) appears
                         in filter_keys are included.  If None, all entries
                         are returned (backward-compatible behaviour).

        Returns:
            Formatted multi-line string, or "" if no findings yet.
        """
        if not self._step_context_entries:
            return ""

        if filter_keys is not None:
            entries = [e for e in self._step_context_entries if e.get("step_id", e.get("description", "")) in filter_keys]
        else:
            entries = self._step_context_entries

        if max_steps is not None:
            entries = entries[-max_steps:]

        if not entries:
            return ""

        # Build artifact lines first (always preserved, never truncated).
        artifact_lines: List[str] = []
        for entry in entries:
            if entry["artifacts"]:
                artifact_lines.append(
                    f"[{entry['description']}] → {', '.join(entry['artifacts'])}"
                )

        # Build detail lines (outcome + key_findings, subject to truncation).
        detail_lines: List[str] = []
        for entry in entries:
            desc = entry["description"]
            outcome = entry["outcome"]
            findings = entry["key_findings"]
            parts: List[str] = []
            if outcome:
                parts.append(outcome)
            parts.extend(f"• {f}" for f in findings)
            if parts:
                detail_lines.append(f"[{desc}]")
                detail_lines.extend(f"  {p}" for p in parts)

        # Build named blocks to avoid duplication in the truncation path.
        artifact_block = "\n".join(
            ["Artifacts from prior steps:"] + [f"  {line}" for line in artifact_lines]
        ) if artifact_lines else ""

        detail_block = "\n".join(
            ["Findings from prior steps:"] + detail_lines
        ) if detail_lines else ""

        # Combine: artifacts section first, then details.
        result = "\n".join(b for b in [artifact_block, detail_block] if b)

        if len(result) > max_chars:
            # Preserve artifact section; truncate only the detail section.
            remaining_budget = max_chars - len(artifact_block) - 1
            if remaining_budget > 0 and detail_block:
                result = artifact_block + "\n" + detail_block[:remaining_budget] + "\n  ..."
            else:
                result = artifact_block
        return result

    def get_accumulated_findings_for_planner(self, already_covered_count: int = 0, max_steps: Optional[int] = None) -> str:
        """Return accumulated findings for the planner's global view.

        Unlike _build_completed_summary() which truncates early steps to
        one-liners, this method returns the semantic content (outcomes +
        key findings + artifacts) from completed steps, PLUS a record of
        all failed approaches so the planner can avoid repeating dead ends.

        Two sections are returned:
          1. Successful findings: outcomes, key_findings, artifacts from
             COMPLETED steps — what was found and produced.
          2. Failed approaches: description + goal + issues from FAILED steps —
             what was tried and why it failed. Always shown in full (no budget
             truncation) since this is critical for loop avoidance.

        Context budget guard (180K tokens × 4 chars/token = 720K chars):
        When the total size of all successful entry_texts exceeds the budget,
        the oldest 1/3 are dropped in a single slice.  Dropping (not truncating)
        preserves the integrity of each remaining entry.  A notice is prepended
        so the planner knows earlier findings are missing.

        Args:
            already_covered_count: Number of the most-recent _step_context_entries
                to exclude from Section 1, because they are already present in full
                detail inside _build_completed_summary()'s detail window.  The same
                set of descriptions is also excluded from Section 2 (failed
                approaches) to avoid duplicating failure detail for recent steps.
                Pass 0 (default) to include all entries (original behaviour).

        Returns:
            Formatted multi-line string, or "" if no findings and no failures yet.
        """
        _FINDINGS_BUDGET_CHARS: int = 720_000  # 180K tokens × 4 chars/token

        # ── Section 1: Successful findings ───────────────────────────────────
        # Exclude the most-recent `already_covered_count` entries: they are
        # already present verbatim in _build_completed_summary()'s detail window,
        # so including them here would duplicate outcome/key_findings/artifacts
        # in the same LLM prompt.
        entries_for_section1 = (
            self._step_context_entries[:-already_covered_count]
            if already_covered_count > 0
            else self._step_context_entries
        )
        if max_steps is not None:
            entries_for_section1 = entries_for_section1[-max_steps:]
        # Collect the descriptions of the skipped (covered) entries so Section 2
        # can also omit failed-approach entries that are already in the window.
        covered_descriptions: set = (
            {e["description"] for e in self._step_context_entries[-already_covered_count:]}
            if already_covered_count > 0
            else set()
        )

        entry_texts: List[str] = []
        for entry in entries_for_section1:
            desc = entry["description"]
            outcome = entry["outcome"]
            findings = entry["key_findings"]
            artifacts = entry["artifacts"]
            if outcome or findings or artifacts:
                lines: List[str] = [f"[{desc}]"]
                if outcome:
                    lines.append(f"  Outcome: {outcome}")
                for f in findings:
                    lines.append(f"  Finding: {f}")
                for a in artifacts:
                    lines.append(f"  Artifact: {a}")
                entry_texts.append("\n".join(lines))

        # If total exceeds budget, use the pre-compressed summary if available,
        # otherwise fall back to dropping the oldest 1/3.
        dropped = 0
        if entry_texts:
            total_chars = sum(len(t) for t in entry_texts)
            if total_chars > _FINDINGS_BUDGET_CHARS and len(entry_texts) > 1:
                if self._compressed_findings_summary:
                    # Replace oldest entries with the pre-computed summary.
                    keep_from = self._compressed_findings_boundary
                    entry_texts = (
                        [f"[Compressed summary of {keep_from} earlier step(s)]\n"
                         + self._compressed_findings_summary]
                        + entry_texts[keep_from:]
                    )
                else:
                    dropped = max(1, len(entry_texts) // 3)
                    entry_texts = entry_texts[dropped:]

        # ── Section 2: Failed approaches ─────────────────────────────────────
        # Always included in full — failure history is critical for loop avoidance
        # and is compact by design (description + truncated goal + issues only).
        failed_lines: List[str] = []
        if self._failed_approaches:
            failed_lines.append("[Failed approaches — do not repeat these]")
            for fa in self._failed_approaches:
                if fa["description"] in covered_descriptions:
                    continue  # already shown in full in _build_completed_summary detail window
                issue_summary = "; ".join(fa["issues"][:2])
                goal_hint = fa["goal"]
                failed_lines.append(
                    f"  ✗ [{fa['description']}] goal: {goal_hint} → {issue_summary}"
                )

        # ── Combine sections ──────────────────────────────────────────────────
        if not entry_texts and not failed_lines:
            return ""

        parts: List[str] = []
        if entry_texts:
            success_block = "\n".join(entry_texts)
            if dropped > 0:
                success_block = (
                    f"[Context budget notice: oldest {dropped} step finding(s) were "
                    f"dropped to stay within the 180K-token context limit. "
                    f"Only the most recent {len(entry_texts)} step finding(s) are shown.]\n"
                    + success_block
                )
            parts.append(success_block)

        if failed_lines:
            parts.append("\n".join(failed_lines))

        return "\n\n".join(parts)

    def count_context_entries_in_last_n_steps(self, n: int) -> int:
        """Return the number of _step_context_entries that fall within the last n completed_steps.

        Used by FlowController to compute the correct already_covered_count for
        get_accumulated_findings_for_planner(): the detail window in
        _build_completed_summary covers the last n completed_steps, but
        _step_context_entries only contains COMPLETED steps with structured output.
        These two counts diverge whenever steps are FAILED or have no structured output.
        """
        if n <= 0 or not self.completed_steps or not self._step_context_entries:
            return 0
        window_ids = {s.step_id for s in self.completed_steps[-n:]}
        return sum(1 for e in self._step_context_entries if e.get("step_id") in window_ids)

    async def compress_findings_async(self, llm_services: "List[LLMService]") -> None:
        """Compress the oldest half of _step_context_entries into a summary.

        Called by FlowController before invoking get_accumulated_findings_for_planner()
        when the findings store is growing large.  On success, the summary is cached
        in _compressed_findings_summary and used in place of hard-dropping entries.
        On LLM failure the method is a no-op — the hard-drop fallback remains.

        The compression boundary advances each call: entries 0.._compressed_findings_boundary
        are always summarised together, so repeated calls accumulate into a single
        rolling summary rather than creating nested summaries.
        """
        _COMPRESS_THRESHOLD_CHARS: int = 480_000  

        entry_texts: List[str] = []
        for entry in self._step_context_entries:
            if entry["outcome"] or entry["key_findings"] or entry["artifacts"]:
                lines: List[str] = [f"[{entry['description']}]"]
                if entry["outcome"]:
                    lines.append(f"  Outcome: {entry['outcome']}")
                for f in entry["key_findings"]:
                    lines.append(f"  Finding: {f}")
                for a in entry["artifacts"]:
                    lines.append(f"  Artifact: {a}")
                entry_texts.append("\n".join(lines))

        if not entry_texts:
            return

        total_chars = sum(len(t) for t in entry_texts)
        if total_chars <= _COMPRESS_THRESHOLD_CHARS:
            return  # Not yet large enough to warrant compression

        # Compress the oldest half (entries not yet summarised).
        compress_up_to = max(len(entry_texts) // 2, self._compressed_findings_boundary + 1)
        if compress_up_to <= self._compressed_findings_boundary:
            return  # Nothing new to compress

        # Include existing summary as context for the new compression.
        old_texts = entry_texts[self._compressed_findings_boundary:compress_up_to]
        prefix = ""
        if self._compressed_findings_summary:
            prefix = (
                f"Previous summary (covers {self._compressed_findings_boundary} step(s)):\n"
                + self._compressed_findings_summary + "\n\nAdditional steps to incorporate:\n"
            )

        prompt = (
            "The following are findings from completed task steps. "
            "Produce a concise summary that preserves all file paths, key values, "
            "important discoveries, and completed actions. Max 1000 words:\n\n"
            + prefix + "\n\n".join(old_texts)
        )

        try:
            from typing import cast as _cast
            from .llm_service import LLMChatResult
            result = _cast(LLMChatResult, await call_with_fallback(
                llm_services,
                dict(
                    messages=[{"role": "user", "content": prompt}],
                    json_mode=False,
                    max_tokens=2000,
                ),
            ))
            if result.content:
                self._compressed_findings_summary = result.content
                self._compressed_findings_boundary = compress_up_to
        except Exception:
            pass  # Silently fall back to hard-drop in get_accumulated_findings_for_planner()

    def mark_last_step_failed(self, issues: Optional[List[str]] = None) -> None:
        """
        Retroactively mark the most recently recorded step as FAILED.

        Called by FlowController when the Planner's last_step_confidence is
        below the verification threshold, indicating the step did not truly
        achieve its stated goal despite the agent's success claim.

        Also adds the step to _failed_approaches so the planner has visibility
        into this failure even after it scrolls out of the detail window.
        """
        if not self.completed_steps:
            return
        last = self.completed_steps[-1]
        last.update_status(StepStatus.FAILED)
        if issues:
            last.issues = issues
        # Track as a failed approach so the planner can avoid repeating it.
        if last.issues:
            self._failed_approaches.append({
                "description": last.description,
                "goal": (last.goal[:300] + "…") if len(last.goal) > 300 else last.goal,
                "issues": list(last.issues[:3]),
            })

    def get_completed_steps(self) -> List[Step]:
        return self.completed_steps.copy()

    def add_modified_file(self, file_path: str) -> None:
        if file_path not in self.modified_files:
            self.modified_files.append(file_path)

    def get_recent_messages(self, n: int = 10) -> List[Message]:
        return self.conversation_history[-n:]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_history": [
                {"role": m.role, "content": m.content,
                 "timestamp": m.timestamp.isoformat()}
                for m in self.conversation_history
            ],
            "completed_steps": [s.to_dict() for s in self.completed_steps],
            "working_directory": self.working_directory,
            "modified_files": self.modified_files,
        }

    def clear(self) -> None:
        self.conversation_history.clear()
        self.completed_steps.clear()
        self.modified_files.clear()
        self._step_context_entries.clear()
        self._failed_approaches.clear()
        self._compressed_findings_summary = None
        self._compressed_findings_boundary = 0
        self._ssh_contexts.clear()
        self._browser_contexts.clear()

AgentMemory = Memory  # alias for backward-compatible imports
