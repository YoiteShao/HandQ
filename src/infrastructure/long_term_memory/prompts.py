"""Triage prompts and JSON parsing.

The system prompt is verbatim from docs/long_term_memory/05_triage_prompts.md
— do not edit casually, every rule has a worked example.

Parsing strategy:
1. ``try_parse_json`` (sync, handles fences, truncation, trailing commas via json_repair)
2. Final fallback to an all-skip verdict so a parse failure never
   crashes the triage loop.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Union

from ..utils import try_parse_json
from .models import (
    Candidate,
    Entry,
    KnowledgeCategory,
    MemoryDimension,
    TriageVerdict,
    VerdictAction,
)

_logger = logging.getLogger("handq.ltm.prompts")


TRIAGE_SYSTEM = """\
You triage a candidate piece of activity into long-term memory and/or knowledge.
Output a SINGLE JSON object with the schema below. No prose, no markdown fences.

## Schema

{
  "worth_memory":     <bool>,
  "worth_knowledge":  <bool>,

  "memory_action":      "create" | "update" | "archive" | "skip",
  "memory_dimension":   "agentic" | "insight" | "identity" | null,
  "memory_summary":     "<single line, max 120 chars>",
  "memory_content":     "<markdown body, <=600 chars>",
  "memory_update_id":   "<existing entry id when action=update>" | null,
  "memory_archive_id":  "<existing entry id when action=archive>" | null,

  "knowledge_action":     "create" | "update" | "archive" | "skip",
  "knowledge_category":   "domain" | "people" | "process" | "coding" | "other" | null,
  "knowledge_summary":    "<single line, max 120 chars>",
  "knowledge_content":    "<markdown body, <=800 chars>",
  "knowledge_update_id":  "<existing entry id when action=update>" | null,
  "knowledge_archive_id": "<existing entry id when action=archive>" | null,

  "reason": "<short reason for both verdicts, max 100 chars>"
}

## Two parallel tracks

### MEMORY (worth_memory)
Memory is about THE BOUND USER specifically — their preferences, habits,
workflows, tools, recurring choices.

- "agentic"  : how the user wants the agent to BEHAVE
               ("always lint before commit", "prefer ruff over flake8")
- "insight"  : factual context about the user / their environment
               ("main project at C:\\\\HandQ", "uses Windows 11", "prefers terse responses")
- "identity" : CROSS-SESSION UNCONDITIONAL BEHAVIOURAL DIRECTIVES that must be
               active on EVERY future interaction regardless of task.
               ("respond in Chinese", "be concise", "always confirm before delete")

### When to use "identity" vs "agentic"

Use "identity" ONLY when ALL three conditions hold:
  1. UNCONDITIONAL — applies regardless of task or context
  2. CROSS-SESSION — "from now on" / "always" / "never", not "for this task"
  3. AGENT BEHAVIOUR — about how the agent responds or acts, not about a tool/project

If the directive is conditional ("when working on Python, use ruff") → "agentic".
If it is a fact ("I use Windows 11") → "insight".
If it is about a project ("this repo uses pytest") → KNOWLEDGE.

IDENTITY is the RAREST dimension. In a typical user's history, expect 5-15
identity entries total vs hundreds of agentic/insight entries.

To revise an existing identity entry, use action="update" with the target id
and provide the complete new content — the system converts this to a clean
archive-old + create-new internally. To purely retire an identity entry
without replacement, use action="archive".

### KNOWLEDGE (worth_knowledge)
Knowledge is REUSABLE TECHNICAL INSIGHT decoupled from this specific user.
The insight can be applied by ANYONE, at ANY TIME, in a SIMILAR situation.

- "domain"   : business / domain rules
- "people"   : people and org context ("Bob is on-call Mon-Wed")
- "process"  : workflow / procedure facts ("deploy window is Thursday")
- "coding"   : coding patterns / conventions ("this repo uses pydantic v2")

A piece of activity may produce BOTH a memory point AND a knowledge entry
(they are not mutually exclusive). Or neither. Or just one.

## What to KEEP

For MEMORY:
- Things the user themselves said, did, configured, preferred, or chose.
- Concrete preferences they expressed in first person.
- Lessons learned from their actions, expressed in second-person/imperative.

For KNOWLEDGE:
- Facts that would help anyone in a similar situation.
- Team/org conventions, deploy windows, library choices.
- Code patterns / project structure conventions.

## What to REJECT

For both tracks:
- The user's persona, vibe, tone, humour, communication style, identity, values,
  or "what kind of person they are". NEVER memorise these.
- Things OTHER people said ABOUT the user (compliments, opinions, nicknames,
  third-party descriptions like "X is the goat", "everyone loves your demo").
- Information already FULLY covered by <existing_memory> / <existing_knowledge>.
  If there is nothing new to add or update, set the corresponding action to "skip".
- Sensitive data: passwords, API keys, tokens, credit-card-shaped numbers,
  raw PII like national-ID numbers. If raw_text contains a secret, reject the
  whole candidate by setting BOTH worth flags to false and reason="sensitive".

For MEMORY only:
- One-off observations without sustained engagement evidence.
  ("Brief visual exposure to a topic" is NOT a habit / ongoing project.)
- Anything tagged [OTHER] in the input — those are context, not personal memory.

For KNOWLEDGE only:
- Facts that only make sense in the context of "what user X did on date Y" —
  those are diary entries, not knowledge.
- Project state that changes daily — not stable enough.

## SELF / OTHER tagging

The input raw_text may contain lines tagged with [SELF] or [OTHER]:
- [SELF]   : the bound user themselves
- [OTHER]  : third parties (collaborators, app UIs, screenshots)

- Only [SELF] lines count as primary evidence for personal MEMORY.
- [OTHER] lines may support project/team KNOWLEDGE, but do NOT justify personal
  memory points unless the bound user has explicitly confirmed the fact.

If the input has no SELF/OTHER tags, treat all lines as [SELF] (single-source).

## Source-specific rules

The candidate's ``Source:`` line tells you how the data was captured. Apply
these per-source rules BEFORE the generic KEEP/REJECT logic:

- ``session_failed``: the task ENDED IN FAILURE. Failure context is NOT
  consent for AGENTIC memory — what looks like a behaviour preference may
  just be evidence of what didn't work this time. Set
  ``worth_memory=false`` whenever the only evidence is an agentic
  preference. **Insight** memory IS allowed (stable factual context about
  the user / their environment doesn't change just because a task failed).
  Knowledge is allowed; bias toward extracting environmental constraints
  ("library X requires Y", "service Z rejects requests over 4MB") that
  caused the failure. When in doubt, reject.

- ``session_complete``: a task ended successfully. The raw_text contains
  the user's goal, the final summary, and recent steps. **Lean toward
  capturing reusable project / team knowledge** (commands, conventions,
  service endpoints, file layouts, library versions, deploy windows),
  not "default skip". Memory is still rare here — user preferences mostly
  surface in receptionist_turn, not session_complete. KEEP if you find:
    * a stable command-line convention used in this project
    * a library/version/config pattern that future sessions will need
    * a fact about the team's process (deploy, on-call, code review)
    * a project-structural fact (where files live, how services connect)
  REJECT if the only signal is "this specific task succeeded" — task
  outcomes are not reusable knowledge.

- ``manual_remember``: the user explicitly typed ``/remember <text>``.
  Trust the user's framing and apply normal KEEP/REJECT rules with a
  slightly lower bar (default action='create' rather than 'skip' when
  the content is concrete).

- ``receptionist_turn``:
  Receptionist captures EVERY user message during a conversation, so the
  vast majority of these candidates are noise (small talk, clarifying
  questions, error pastes, intermediate prompts, "ok thanks", etc.).
  **Default action is SKIP.** Only KEEP when the message contains an
  explicit, durable signal:

    Strong-accept patterns (memory candidates):
      * Imperatives: "always X", "never Y", "from now on", "stop doing Z",
        "use X instead of Y", "prefer X"
      * Environmental facts: "my project is at X", "I use X for Y",
        "we run X with these flags"
      * Corrections of existing entries: "wrong, X is right" → action=update

    Strong-accept patterns (knowledge candidates):
      * Team / project conventions: "we deploy on Mondays", "team uses
        pytest -xvs"
      * Stable infra facts: "DB is at 10.0.0.5", "release tag pattern is vN.M"

    Strong-REJECT patterns (always skip):
      * Questions to the agent ("can you help me with X")
      * Single-word acknowledgements ("ok", "thanks", "yes")
      * Code or error pastes without commentary
      * In-task clarifications ("actually try with --verbose")
      * Persona / tone requests (R2 already covers this — REJECT)
      * Anything that depends on this specific task's state to make sense

  When the LLM is on the fence: REJECT. A missed signal will resurface in
  later turns; a wrong accept pollutes every future planner prompt.

- ``post_commit``:
  apply normal KEEP/REJECT logic. Default is reject; promote only when
  there is clear, sustained, or explicit signal.

## Update vs create vs archive vs skip

Four states for each action:
- "create":  Genuinely new content not covered by existing entries.
- "update":  Relates to an existing entry but adds or refines details.
             Set the corresponding *_update_id to the existing entry's id
             (visible as [id=xxx] in the existing_* blocks).
             *_content must be the COMPLETE merged content, not a delta.
             This bumps the entry's version — repeated updates mark it
             as a strongly-held user preference.
- "archive": The user EXPLICITLY contradicted an existing entry —
             they said the prior fact is now wrong, no longer applies,
             or should be removed. Set *_archive_id to the existing
             entry's id. *_content / *_summary should be empty. This
             soft-deletes the entry.
- "skip":    Pure duplicate, no new information.

The corresponding worth_* must be true when action is "create",
"update", or "archive"; false when action is "skip".

### When to use ARCHIVE (strict)

ARCHIVE is reserved for EXPLICIT, UNAMBIGUOUS contradiction with strong
referential intent. The user must be saying "the existing memory is
wrong" — not just complaining, not just preferring something else once.

Strong-archive patterns (KEEP archive):
- "I don't use X anymore" / "stop saying I use X"
- "我不再用 X 了" / "X 错了，应该是 Y" / "之前那条 X 偏好删了吧"
- "from now on it's Y, not X" with reference to existing X

Reject-archive patterns (do NOT archive — at most action=skip):
- "X 这次跑挂了" / "X is broken right now"  — single-incident complaint
- "X 不太好用" / "X is annoying"            — vague dissatisfaction
- "考虑下 Y" / "thinking about Y"           — exploration, not commitment
- "actually let's try Y this time"          — one-off task choice

If the existing_memory / existing_knowledge block doesn't contain a
candidate for *_archive_id, NEVER fabricate one. ARCHIVE without a
matching existing id is a parse error → fall back to skip.

## Style rules for content

- Use second-person ("you ...") or imperative ("Always ...").
- NEVER use the user's name. NEVER write in third person ("The user prefers ...").
- Markdown body. If multiple bullets, use a single section heading EXACTLY:
    "## Memory Points"  (for memory)
    "## Key Insights"   (for knowledge)
  You may also include "## Description" before the points.
- Each bullet is one sentence with a concrete subject.
- Cite a concrete signal when possible (filename / command / config key).

## Conservatism

The DEFAULT is to reject. Most candidates should produce
{"worth_memory": false, "worth_knowledge": false, "memory_action": "skip",
 "knowledge_action": "skip", "reason": "no new signal"}.

Only promote to memory or knowledge when there is clear, sustained, or explicit
evidence. When in doubt, reject.

## Worked examples

### Example 1 — explicit user preference (memory only)

Input raw_text:
  [SELF] I always run `ruff check .` before git commit

Output:
{"worth_memory": true, "worth_knowledge": false,
 "memory_action": "create", "memory_dimension": "agentic",
 "memory_summary": "always run `ruff check .` before commits",
 "memory_content": "## Memory Points\\n- Always run `ruff check .` before any `git commit`.",
 "memory_update_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "explicit user behaviour preference"}

### Example 2 — team fact (knowledge only)

Input raw_text:
  [SELF] reminded the team that deploy window is Thursday afternoons.

Output:
{"worth_memory": false, "worth_knowledge": true,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "create", "knowledge_category": "process",
 "knowledge_summary": "deploy window is Thursday afternoons",
 "knowledge_content": "## Description\\nProject deploy schedule.\\n\\n## Key Insights\\n- Deploys happen Thursday afternoons.",
 "knowledge_update_id": null,
 "reason": "stable team process, applies regardless of user"}

### Example 3 — both apply

Input raw_text:
  [SELF] We use pytest with -xvs for debugging in this project.

Output:
{"worth_memory": true, "worth_knowledge": true,
 "memory_action": "create", "memory_dimension": "agentic",
 "memory_summary": "use pytest -xvs for debugging",
 "memory_content": "## Memory Points\\n- When debugging tests, use `pytest -xvs`.",
 "memory_update_id": null,
 "knowledge_action": "create", "knowledge_category": "coding",
 "knowledge_summary": "project debug convention: pytest -xvs",
 "knowledge_content": "## Description\\nProject test debugging convention.\\n\\n## Key Insights\\n- Use `pytest -xvs` for fast feedback on failing tests.",
 "knowledge_update_id": null,
 "reason": "explicit user behaviour AND project convention"}

### Example 4 — persona, REJECT

Input raw_text:
  [OTHER] You're hilarious and witty.

Output:
{"worth_memory": false, "worth_knowledge": false,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "persona/vibe; never memorise"}

### Example 5 — covered, REJECT

Input raw_text:
  [SELF] I prefer ruff over flake8.

<existing_memory>
- [id=abc123] (agentic) prefer ruff over flake8 for linting
</existing_memory>

Output:
{"worth_memory": false, "worth_knowledge": false,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "covered by existing memory abc123"}

### Example 6 — update existing

Input raw_text:
  [SELF] now using ruff with the --select=E9 flag

<existing_memory>
- [id=abc123] (agentic) prefer ruff over flake8 for linting
</existing_memory>

Output:
{"worth_memory": true, "worth_knowledge": false,
 "memory_action": "update", "memory_dimension": "agentic",
 "memory_summary": "use ruff with --select=E9 flag",
 "memory_content": "## Memory Points\\n- Prefer ruff over flake8 for linting.\\n- Run with `--select=E9` to focus on syntax errors.",
 "memory_update_id": "abc123",
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "extends existing entry with new flag preference"}

### Example 7 — sensitive, REJECT

Input raw_text:
  [SELF] my anthropic key is sk-ant-abc123def456...

Output:
{"worth_memory": false, "worth_knowledge": false,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "sensitive — contains API key"}

### Example 8 — failed session, knowledge only (no agentic memory)

Source: session_failed
Input raw_text:
  # Goal
  [SELF] deploy the build to staging
  # Final summary (success=False)
  Aborted after 5 retries — staging cluster rejected requests larger than 4MB
  # Recent steps
  - [FAILED] upload artifact bundle.zip (8.2MB)
    outcome: HTTP 413 Payload Too Large

Output:
{"worth_memory": false, "worth_knowledge": true,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "create", "knowledge_category": "process",
 "knowledge_summary": "staging rejects payloads larger than 4MB",
 "knowledge_content": "## Description\\nStaging deploy upload limit.\\n\\n## Key Insights\\n- Staging cluster returns HTTP 413 for artifact uploads above 4MB; split or compress before upload.",
 "knowledge_update_id": null,
 "reason": "stable env constraint from failed deploy; no agentic-preference signal"}

### Example 8b — failed session, insight memory still OK

Source: session_failed
Input raw_text:
  # Goal
  [SELF] run the desktop integration tests
  # Final summary (success=False)
  Aborted: Display 0 has no GUI session — tests need RDP login.
  Confirmed primary workstation is C:\\Users\\fengxuan, Windows 11 Enterprise.
  # Recent steps
  - [FAILED] launch test runner
    outcome: GetForegroundWindow returned 0; no interactive session

Output:
{"worth_memory": true, "worth_knowledge": true,
 "memory_action": "create", "memory_dimension": "insight",
 "memory_summary": "primary workstation is Windows 11 Enterprise (C:\\\\Users\\\\fengxuan)",
 "memory_content": "## Memory Points\\n- Primary workstation runs Windows 11 Enterprise; user profile lives at C:\\\\Users\\\\fengxuan.",
 "memory_update_id": null,
 "knowledge_action": "create", "knowledge_category": "process",
 "knowledge_summary": "desktop tests need an interactive RDP session",
 "knowledge_content": "## Description\\nDesktop integration test prerequisites.\\n\\n## Key Insights\\n- Tests cannot run on a logged-out machine; require RDP-attached interactive session.",
 "knowledge_update_id": null,
 "reason": "failed session but stable env facts (insight + process knowledge) are valid"}

### Example 9 — successful session, project knowledge

Source: session_complete
Input raw_text:
  # Goal
  [SELF] add CI badges to the README
  # Final summary (success=True)
  Added GitHub Actions CI + coverage badges. CI runs on push to `main`
  via `.github/workflows/ci.yml`. Coverage uploaded to Codecov; the
  CODECOV_TOKEN is in the org-level secrets store.
  # Recent steps
  - [COMPLETED] discover existing CI config
    outcome: repo uses pnpm; lockfile is pnpm-lock.yaml; node 20.x
  - [COMPLETED] add badges to README
    artifacts: README.md

Output:
{"worth_memory": false, "worth_knowledge": true,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "create", "knowledge_category": "coding",
 "knowledge_summary": "CI uses GitHub Actions on push to main; pnpm + node 20",
 "knowledge_content": "## Description\\nCI / build conventions for this repo.\\n\\n## Key Insights\\n- CI runs via `.github/workflows/ci.yml` on push to `main`.\\n- Package manager is pnpm (lockfile `pnpm-lock.yaml`); Node 20.x.\\n- Coverage uploaded to Codecov; secret name is `CODECOV_TOKEN` (org-level).",
 "knowledge_update_id": null,
 "reason": "concrete project conventions; reusable across future sessions"}

### Example 10 — successful session, nothing reusable

Source: session_complete
Input raw_text:
  # Goal
  [SELF] fix the typo in line 42 of utils.py
  # Final summary (success=True)
  Replaced "recieve" with "receive". Single-character commit.
  # Recent steps
  - [COMPLETED] grep + edit
    artifacts: utils.py

Output:
{"worth_memory": false, "worth_knowledge": false,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "one-off task; no reusable convention or fact"}

### Example 11 — activity_observer, observed path is NOT durable signal, REJECT

Source: activity_observer
Input raw_text:
  # Activity observation
  Monitor 1 (Display 1 (1920x1200, primary))
  Samples: 4
  ## 14:32 [hot] Code.exe
  window: HandQ - presentation_v2.html - Visual Studio Code
  text excerpt:
  C:\\CodeProject\\HandQ\\presentation\\handq_presentation_v2.html
  ...HTML markup...

Output:
{"worth_memory": false, "worth_knowledge": false,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "passive path observation; not a durable preference or reusable fact"}

Reasoning: Seeing a path on screen is NOT evidence of a stable environment
fact. The user happens to have a file open. Repeated observations of the
same path across many activity_observer candidates would compound into
N near-duplicate INSIGHT entries. Reject path-inventory summaries even
when they look factual — only KEEP when there is a behaviour verb
("uses pytest -xvs", "runs build via Nuitka") or an explicit preference.

### Example 12 — explicit user contradiction, ARCHIVE existing memory

Input raw_text:
  [SELF] 我不再用 ruff 了，改用 pyflakes

<existing_memory>
- [id=abc123] (agentic) prefer ruff over flake8 for linting
</existing_memory>

Output:
{"worth_memory": true, "worth_knowledge": false,
 "memory_action": "archive", "memory_dimension": null,
 "memory_summary": "", "memory_content": "",
 "memory_update_id": null, "memory_archive_id": "abc123",
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "explicit contradiction of existing entry; user switched away from ruff"}

Reasoning: The user explicitly stated they no longer use the tool the
existing entry captures. ARCHIVE removes the now-stale memory; if the
user later mentions pyflakes as a preference, a fresh CREATE will
emit on that turn. Don't UPDATE the entry to "prefers pyflakes" — the
memory's identity is about ruff; the right move is to retire it and
let a clean new entry form.

### Example 13 — vague dissatisfaction, NOT archive (skip)

Input raw_text:
  [SELF] ruff 这次跑挂了，烦死了

<existing_memory>
- [id=abc123] (agentic) prefer ruff over flake8 for linting
</existing_memory>

Output:
{"worth_memory": false, "worth_knowledge": false,
 "memory_action": "skip", "memory_dimension": null,
 "memory_summary": "", "memory_content": "", "memory_update_id": null,
 "memory_archive_id": null,
 "knowledge_action": "skip", "knowledge_category": null,
 "knowledge_summary": "", "knowledge_content": "", "knowledge_update_id": null,
 "reason": "single incident complaint, not a preference change"}

Reasoning: Frustration about a single failure is NOT a preference
contradiction. The user might still prefer ruff overall — they're
just venting about today's run. ARCHIVE only when the user explicitly
states the prior preference no longer applies.
"""


MERGE_ARBITER_SYSTEM = """\
You decide whether two long-term memory entries are duplicates.

They already scored high embedding similarity (the 0.85-0.90 band), so a
keyword/vector pass thinks they might be the same. Your job is the finer
judgement a similarity score can't make.

Answer with a SINGLE JSON object, no prose, no markdown fences:

{"same": <bool>, "reason": "<short, max 100 chars>"}

Set "same": true ONLY when one entry is fully redundant given the other —
merging loses NO information the agent would want later. If either entry
holds a detail, qualifier, scope, or value the other lacks, they are
DISTINCT: answer false. When unsure, answer false (keeping both is cheap;
silently dropping a real difference is not).
"""


TRIAGE_USER_TEMPLATE = """\
## Source: {source}{ref_part}

## Raw text
{raw_text}
{hint_part}{existing_memory_part}{existing_knowledge_part}

Output the JSON object only. No prose, no markdown fences."""


def render_user(
    candidate: Candidate,
    existing_memory: List[Entry],
    existing_knowledge: List[Entry],
) -> str:
    ref_part = f" (ref: {candidate.source_ref})" if candidate.source_ref else ""

    hint_part = ""
    if candidate.hint:
        hint_part = f"\n\n## Hint\n{candidate.hint}"

    existing_memory_part = ""
    if existing_memory:
        items = "\n".join(
            f"- [id={e.id}] ({e.dimension.value if e.dimension else 'memory'}) {e.summary}"
            for e in existing_memory
        )
        existing_memory_part = f"\n\n<existing_memory>\n{items}\n</existing_memory>"

    existing_knowledge_part = ""
    if existing_knowledge:
        items = "\n".join(
            f"- [id={e.id}] ({e.category.value if e.category else 'knowledge'}) {e.summary}"
            for e in existing_knowledge
        )
        existing_knowledge_part = (
            f"\n\n<existing_knowledge>\n{items}\n</existing_knowledge>"
        )

    raw = (candidate.raw_text or "")[:4000]
    return TRIAGE_USER_TEMPLATE.format(
        source=candidate.source,
        ref_part=ref_part,
        raw_text=raw,
        hint_part=hint_part,
        existing_memory_part=existing_memory_part,
        existing_knowledge_part=existing_knowledge_part,
    )


# ── Parse: expected keys, dict → TriageVerdict ──────────────────────────────

# Keys the parser MUST see in the dict for a successful parse.
_VERDICT_EXPECTED_KEYS: List[str] = [
    "worth_memory",
    "worth_knowledge",
    "memory_action",
    "knowledge_action",
]


def _coerce_dict_to_verdict(d: dict) -> TriageVerdict:
    """Apply business validation to a parsed dict and return a TriageVerdict.

    Pure dict → object mapper, with the same defensive logic the previous
    inline parser had: enum validation, length caps, action↔worth
    consistency. Bad inputs degrade to a SKIP verdict, never raise.
    """
    worth_m = bool(d.get("worth_memory", False))
    worth_k = bool(d.get("worth_knowledge", False))

    valid_actions = {a.value for a in VerdictAction}
    m_action = (d.get("memory_action") or VerdictAction.SKIP.value).lower()
    k_action = (d.get("knowledge_action") or VerdictAction.SKIP.value).lower()
    if m_action not in valid_actions:
        m_action = VerdictAction.SKIP.value
    if k_action not in valid_actions:
        k_action = VerdictAction.SKIP.value
    if m_action == VerdictAction.SKIP.value:
        worth_m = False
    if k_action == VerdictAction.SKIP.value:
        worth_k = False

    m_dim: Optional[MemoryDimension] = None
    if worth_m and d.get("memory_dimension"):
        try:
            m_dim = MemoryDimension(str(d["memory_dimension"]).lower())
        except ValueError:
            worth_m = False
            m_action = VerdictAction.SKIP.value

    k_cat: Optional[KnowledgeCategory] = None
    if worth_k and d.get("knowledge_category"):
        try:
            k_cat = KnowledgeCategory(str(d["knowledge_category"]).lower())
        except ValueError:
            worth_k = False
            k_action = VerdictAction.SKIP.value

    m_summary = (d.get("memory_summary") or "").strip()
    k_summary = (d.get("knowledge_summary") or "").strip()
    m_content = (d.get("memory_content") or "").strip()
    k_content = (d.get("knowledge_content") or "").strip()
    m_archive_id = (d.get("memory_archive_id") or None)
    k_archive_id = (d.get("knowledge_archive_id") or None)

    # ARCHIVE has different validity rules than CREATE/UPDATE:
    # - summary/content are intentionally empty (we are removing, not writing)
    # - archive_id MUST be present (we need a target to archive)
    # - dimension/category may be absent (we don't need to classify a
    #   removal — we're operating on an existing classified entry)
    if m_action == VerdictAction.ARCHIVE.value:
        if not m_archive_id:
            # LLM emitted archive without a target — fall back to skip
            # rather than silently corrupt state.
            m_action = VerdictAction.SKIP.value
            worth_m = False
    elif m_action == VerdictAction.UPDATE.value:
        if not (d.get("memory_update_id") or None):
            # UPDATE without a target id — demote to CREATE so the signal
            # isn't lost but we don't silently fall through to insert.
            m_action = VerdictAction.CREATE.value
        if worth_m and not (m_summary and m_content):
            worth_m = False
            m_action = VerdictAction.SKIP.value
    elif worth_m and not (m_summary and m_content):
        worth_m = False
        m_action = VerdictAction.SKIP.value

    if k_action == VerdictAction.ARCHIVE.value:
        if not k_archive_id:
            k_action = VerdictAction.SKIP.value
            worth_k = False
    elif k_action == VerdictAction.UPDATE.value:
        if not (d.get("knowledge_update_id") or None):
            k_action = VerdictAction.CREATE.value
        if worth_k and not (k_summary and k_content):
            worth_k = False
            k_action = VerdictAction.SKIP.value
    elif worth_k and not (k_summary and k_content):
        worth_k = False
        k_action = VerdictAction.SKIP.value

    return TriageVerdict(
        worth_memory=worth_m,
        worth_knowledge=worth_k,
        memory_action=m_action,
        memory_dimension=m_dim,
        memory_summary=m_summary[:120],
        memory_content=m_content[:1200],
        memory_update_id=(d.get("memory_update_id") or None),
        memory_archive_id=m_archive_id,
        knowledge_action=k_action,
        knowledge_category=k_cat,
        knowledge_summary=k_summary[:120],
        knowledge_content=k_content[:2000],
        knowledge_update_id=(d.get("knowledge_update_id") or None),
        knowledge_archive_id=k_archive_id,
        reason=(d.get("reason") or "")[:200],
    )


def _all_skip_verdict(reason: str) -> TriageVerdict:
    """Final fallback when both sync and LLM-repair parses fail."""
    return TriageVerdict(
        worth_memory=False,
        worth_knowledge=False,
        memory_action=VerdictAction.SKIP.value,
        knowledge_action=VerdictAction.SKIP.value,
        reason=reason[:200],
    )


async def parse_verdict(
    json_str: str,
    *,
    llm_services: Optional[List[Any]] = None,
) -> TriageVerdict:
    """Parse the LLM's JSON output into a TriageVerdict.

    Strategy:
      1. ``try_parse_json`` — handles fences, truncation, json_repair.
      2. ``_all_skip_verdict`` — final fallback so a malformed response
         degrades to "skip both tracks", never raises.

    Args:
        llm_services: Deprecated, ignored. Kept for backward compatibility.
    """
    raw = json_str or ""

    parsed: Union[dict, str] = try_parse_json(raw)
    if isinstance(parsed, dict) and all(
        k in parsed for k in _VERDICT_EXPECTED_KEYS
    ):
        return _coerce_dict_to_verdict(parsed)

    # Irrecoverable — log a head-of-content snippet for post-mortem.
    head = (raw[:120] + "…") if len(raw) > 120 else raw
    _logger.warning(
        "triage verdict parse failed; head=%r — falling back to all-skip", head,
    )
    return _all_skip_verdict("parse_failed")


# ── Skill extraction (completed-task → reusable skill) ──────────────────────
#
# A SEPARATE pass from TRIAGE_SYSTEM. Runs only on successful SESSION_COMPLETE
# candidates (a finished HandQ task trajectory: goal + summary + steps). Its
# job is NOT "did anything happen" but "is this a reusable, generalizable
# multi-step procedure worth turning into a skill". The caller
# (_apply_session_skill) clusters the result by a fingerprint computed over the
# normalized ``name`` tokens, so the single most important instruction below is:
# emit a CANONICAL, phrasing-stable name. Two runs of the same underlying task
# must collapse to the same name → same fingerprint → recurrence counter climbs.

SKILL_EXTRACTION_SYSTEM = """\
You analyze a COMPLETED, SUCCESSFUL automation task and decide whether it is a
reusable skill worth saving — a repeatable, generalizable procedure the agent
could run again on a future request of the same kind.

Answer with a SINGLE JSON object, no prose, no markdown fences:

{"worth_skill": <bool>,
 "name": "<canonical action name>",
 "description": "<one line: what the skill does>",
 "when_to_use": "<one line: the trigger/situation>",
 "steps_md": "<markdown: the generalized, parameterized steps>",
 "reason": "<short, max 100 chars>"}

Set "worth_skill": true ONLY when ALL hold:
- The task is a MULTI-STEP procedure (not a single trivial action).
- It GENERALIZES: the same procedure would apply to other inputs, not just
  this one specific file/value. Parameterize specifics (paths, names, hosts,
  IDs) into placeholders in steps_md rather than hard-coding them.
- It is genuinely REUSABLE: the agent (or user) would plausibly want to repeat
  this workflow on future, similar requests.

Set "worth_skill": false (the conservative default) for: one-off or trivial
tasks, pure question-answering, anything whose only content is specific values
that cannot be generalized, or anything that cannot be described without
embedding a secret/credential.

CRITICAL — the "name" must be CANONICAL and PHRASING-STABLE: a short, generic
action label describing the KIND of task, NOT the user's literal wording. The
same underlying task phrased two different ways MUST get the same name, because
identical names are what let us detect that a task is recurring.
  good:  "Restart the staging server"
  bad:   "can you restart staging again pls"   (echoes the user's phrasing)
  good:  "Bump the package version and tag a release"
  bad:   "do the 1.2.4 release thing"          (embeds a specific value)
Lowercase the conceptual action; drop pleasantries, filler, and specific
values from the name (they belong in steps_md as placeholders).
"""


def parse_skill_extraction(json_str: str) -> dict:
    """Parse the skill-extraction LLM output into a normalized dict.

    Mirrors ``parse_verdict``'s contract: never raises. On any parse failure,
    missing/false ``worth_skill``, or a blank ``name`` it returns the
    conservative all-skip ``{"worth_skill": False}`` so the caller writes no
    proposal. Only an explicit ``worth_skill: true`` WITH a usable name passes.
    """
    parsed = try_parse_json(json_str or "")
    if not isinstance(parsed, dict):
        return {"worth_skill": False}
    if parsed.get("worth_skill") is not True:
        return {"worth_skill": False}
    name = str(parsed.get("name") or "").strip()
    if not name:
        return {"worth_skill": False}
    return {
        "worth_skill": True,
        "name": name[:120],
        "description": str(parsed.get("description") or "").strip()[:300],
        "when_to_use": str(parsed.get("when_to_use") or "").strip()[:300],
        "steps_md": str(parsed.get("steps_md") or "").strip()[:4000],
        "reason": str(parsed.get("reason") or "").strip()[:200],
    }
