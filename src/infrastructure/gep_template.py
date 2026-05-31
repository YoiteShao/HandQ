"""
GEP Template — data model, JSON serialisation, and template directory I/O.

Templates are stored as JSON files in HandQ/gep_templates/<id>.json,
where HandQ/ is the directory containing handq.py (fixed, independent of
the working directory).
See gep_design.md Section B for the full field specification.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repo / install root resolution.
#
# Mirrors the logic in bridge_main.py so frozen (Nuitka standalone /
# PyInstaller) builds locate gep_templates/ next to the bridge executable,
# not via __file__ — which Nuitka may virtualise to a path inside the dist
# tree that is not predictable across builds.
#
#   * Frozen build  → parent directory of sys.executable
#   * Dev / source  → repo root (this file's grandparent)

_TEMPLATES_SUBDIR = "gep_templates"


def _install_dir() -> Path:
    """Return the directory next to the bridge entry point.

    Same algorithm as bridge_main._INSTALL_DIR. Used as the "shipped
    defaults" location for templates on every platform, and as the active
    templates directory on Linux/macOS.
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    # Dev: gep_template.py lives at src/infrastructure/gep_template.py,
    # so two levels up is the repo root.
    return Path(__file__).parent.parent.parent.resolve()


def _user_handq_root() -> Path:
    """Per-user HandQ root: %USERPROFILE%\\HandQ on Windows, ~/HandQ elsewhere.

    Matches bridge_main._user_handq_root() — single source of truth for user-
    owned data per ARCHITECTURE.md §1.5.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


@dataclass
class ParamSpec:
    type: str                        # "path" | "string" | "int" | "bool" | "list"
    description: str
    default: Any = None              # always provide a sensible default
    emphasis: bool = False           # True = highlight this param in confirmation UI


@dataclass
class StepSpec:
    """Lightweight serializable step spec for GEP templates.

    Intentionally separate from models.plan.Step which contains
    execution-phase fields (ssh_target, timestamps, record IDs,
    status, observations, agent_runtime_reasoning, factual_outcome,
    artifacts, key_findings) not needed in templates.
    StepSpec holds only the planning-phase fields that describe
    what a step should do, making it safe to serialize to JSON
    and reuse across sessions without any runtime state.
    """

    step_id: str
    description: str
    goal: str                        # may contain {{params.X}} placeholders
    step_supplement: str = ""
    parallel_group: str = ""
    is_aggregation: bool = False
    planner_reasoning: str = ""
    expected_outcomes: List[str] = field(default_factory=list)
    risk_assessment: str = ""
    required_context_keys: List[str] = field(default_factory=list)
    ssh_target: str = ""             # "user@hostname"; empty = local
    # Tools that the original successful run actually used for this step
    # (e.g. ["browser"], ["shell"], ["browser","desktop"]). Surfaced in
    # the agent prompt during GEP execution as a hard constraint so the
    # agent doesn't reinvent the wheel with a different tool that may
    # have different dependencies. Empty = no constraint (legacy templates
    # written before this field existed).
    tools_required: List[str] = field(default_factory=list)


@dataclass
class GEPTemplate:
    id: str
    name: str
    description: str
    created_at: str                  # ISO8601, e.g. "2026-04-15T23:05:23Z"
    version: int
    source_log_path: str
    params_schema: Dict[str, ParamSpec] = field(default_factory=dict)
    guide_steps: List[StepSpec] = field(default_factory=list)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "$schema": "HandQ-GEP-Template/v1",
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "version": self.version,
            "source_log_path": self.source_log_path,
            "params_schema": {
                k: {
                    "type": v.type,
                    "default": v.default,
                    "description": v.description,
                    **( {"emphasis": True} if v.emphasis else {} ),
                }
                for k, v in self.params_schema.items()
            },
            "guide_steps": [
                {
                    "step_id": s.step_id,
                    "description": s.description,
                    "goal": s.goal,
                    "step_supplement": s.step_supplement,
                    "parallel_group": s.parallel_group,
                    "is_aggregation": s.is_aggregation,
                    "planner_reasoning": s.planner_reasoning,
                    "expected_outcomes": s.expected_outcomes,
                    "risk_assessment": s.risk_assessment,
                    "required_context_keys": s.required_context_keys,
                    "ssh_target": s.ssh_target,
                    "tools_required": s.tools_required,
                }
                for s in self.guide_steps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GEPTemplate":
        params_schema: Dict[str, ParamSpec] = {}
        for k, v in data.get("params_schema", {}).items():
            params_schema[k] = ParamSpec(
                type=v.get("type", "string"),
                description=v.get("description", ""),
                default=v.get("default", None),
                emphasis=bool(v.get("emphasis", False)),
            )

        guide_steps: List[StepSpec] = []
        for s in data.get("guide_steps", []):
            _tr_raw = s.get("tools_required", [])
            if isinstance(_tr_raw, str):
                # Tolerate "browser" as well as ["browser"] from older / hand-written
                # templates.
                _tools_required = [_tr_raw] if _tr_raw else []
            elif isinstance(_tr_raw, list):
                _tools_required = [str(t) for t in _tr_raw if t]
            else:
                _tools_required = []
            guide_steps.append(StepSpec(
                step_id=s.get("step_id", ""),
                description=s.get("description", ""),
                goal=s.get("goal", ""),
                step_supplement=s.get("step_supplement", ""),
                parallel_group=s.get("parallel_group", ""),
                is_aggregation=s.get("is_aggregation", False),
                planner_reasoning=s.get("planner_reasoning", ""),
                expected_outcomes=s.get("expected_outcomes", []),
                risk_assessment=s.get("risk_assessment", ""),
                required_context_keys=s.get("required_context_keys", []),
                ssh_target=s.get("ssh_target", ""),
                tools_required=_tools_required if "ssh" in _tools_required or not s.get("ssh_target", "")
                    else _tools_required + ["ssh"],
            ))

        _id = data.get("id") or data.get("name") or str(uuid.uuid4())

        raw_ver = data.get("version", 1)
        try:
            parsed_version = int(str(raw_ver).split(".")[0])
        except (ValueError, TypeError):
            parsed_version = 1

        return cls(
            id=_id,
            name=data.get("name", ""),
            description=data.get("description", ""),
            created_at=data.get("created_at", _utcnow()),
            version=parsed_version,
            source_log_path=data.get("source_log_path", ""),
            params_schema=params_schema,
            guide_steps=guide_steps,
        )


# ── Template directory helpers ─────────────────────────────────────────────────

def _templates_dir() -> Path:
    """Return the templates directory.

    Platform split (per ARCHITECTURE.md §1.5 — no dev/prod variation):

    * **Windows** (any mode) — ``%USERPROFILE%\\HandQ\\gep_templates\\``.
      Templates always live next to the user's other HandQ artifacts
      (config, History, personality, scheduled_tasks.json). Dev mode no
      longer falls back to the repo tree; the architecture is the source
      of truth and applies uniformly. The directory is auto-seeded from
      the install copy on first launch (so a packaged build's shipped
      defaults reach the user dir, and a dev install with a populated
      repo gets the same starter content).

    * **Linux / macOS** (any mode) — ``<install_dir>/gep_templates/``.
      No equivalent "user root" convention; co-locating with the bridge
      install keeps everything self-contained. install_dir = parent of
      sys.executable for frozen, repo root in dev.

    First match wins:
      1. ``HANDQ_GEP_TEMPLATES_DIR`` env override (CI / portable mode).
      2. Per-platform default (above).
    """
    env_override = os.environ.get("HANDQ_GEP_TEMPLATES_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()

    install_dir = _install_dir() / _TEMPLATES_SUBDIR

    if sys.platform != "win32":
        # Linux / macOS: install dir always.
        return install_dir

    # ── Windows: always %USERPROFILE%\HandQ\gep_templates\ ───────────────
    user_dir = _user_handq_root() / _TEMPLATES_SUBDIR
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        # Seed from install copy on first run so the user dir starts
        # populated with whatever ships in the repo / installer.
        if install_dir.exists() and not any(user_dir.glob("*.json")):
            import shutil
            for src in install_dir.glob("*.json"):
                dst = user_dir / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
    except OSError:
        pass
    return user_dir


def _sanitize_template_id(template_id: str) -> str:
    """
    Ensure template_id is a safe bare filename component.

    Strips any directory separators and path-traversal sequences so that
    _template_path() always stays inside _templates_dir(), regardless of
    what value an LLM-generated template carries in its ``id`` field.

    Raises ValueError when the sanitised result is empty.
    """
    # Take only the final path component, which discards any leading
    # directory parts including ".." traversal sequences.
    safe = Path(template_id).name
    # Guard against an empty result (e.g. template_id was "/" or "..").
    if not safe:
        raise ValueError(f"Invalid template id (resolves to empty name): {template_id!r}")
    return safe


def _template_path(template_id: str) -> Path:
    return _templates_dir() / f"{_sanitize_template_id(template_id)}.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Template normalization ─────────────────────────────────────────────────────
# Deterministic, LLM-independent enforcement of the parameterization invariant:
#   ∀ session-specific path/identifier in guide_steps.goal
#   → ∃ entry in params_schema that covers it via {{params.X}}
#
# Called automatically by save_template() so the invariant holds for every
# template written to disk, regardless of whether the LLM followed the
# anti-overfitting rules in SAVE_SESSION_GOAL_TEMPLATE.

# Patterns that identify session-specific values in step goal text.
# Applied to text AFTER stripping existing {{params.X}} tokens so we only
# match literals that are not already parameterized.
_UNPARAMETERIZED_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Absolute paths with ≥2 components: /foo/bar, /a/b/c.json
    # Requires at least one internal slash so single tokens like /SIGKILL are excluded.
    (re.compile(r'/[\w.\-]+(?:/[\w.\-]+)+'), "path"),
    # Windows drive paths: C:\foo\bar, D:\project\src
    (re.compile(r'[A-Za-z]:\\[\w.\-\\]+'), "path"),
    # Absolute paths to files with a recognised extension: /foo.json, /config.yaml
    (re.compile(r'/[\w\-]{2,}\.(?:py|js|ts|json|yaml|yml|toml|cfg|ini|sh|md|txt|csv|sql|log|env)\b'), "path"),
    # Home-relative paths: ~/foo, ~/foo/bar
    (re.compile(r'~/[\w.\-][\w./\-]*'), "path"),
    # Relative paths with leading ./ or ../
    (re.compile(r'\.{1,2}/[\w.\-][\w./\-]+'), "path"),
    # Relative paths with leading .\ or ..\ (Windows)
    (re.compile(r'\.{1,2}\\[\w.\-][\w.\\-]+'), "path"),
    # Bare filenames with recognised extensions (e.g. config.yaml, run.sh)
    (re.compile(r'\b[\w\-]{2,}\.(?:py|js|ts|json|yaml|yml|toml|cfg|ini|sh|md|txt|csv|sql|env)\b'), "file"),
    # hostname:port patterns (e.g. localhost:5432, db.internal:3306)
    (re.compile(r'\b[\w.\-]{3,}:\d{2,5}\b'), "host"),
    # Connection / DSN strings (e.g. postgres://db.prod/mydb)
    (re.compile(r'(?:postgres|postgresql|mysql|mongodb|redis|amqp|kafka|jdbc)://[^\s\'"<>]+'), "connection_string"),
    # Fully-qualified hostnames with ≥3 components (e.g. db.prod.internal)
    # Requires lowercase letters only to avoid matching version strings like "v1.2.3".
    (re.compile(r'\b[a-z][a-z0-9\-]{1,}\.[a-z0-9\-]+\.[a-z]{2,}(?:\.[a-z]{2,})?\b'), "hostname"),
]

# Tokens that must never be auto-parameterized even when they superficially
# match a path pattern.
_NON_PATH_TOKENS = frozenset({
    # Python builtins / booleans
    "False", "True", "None",
    # Standard I/O streams
    "stderr", "stdout", "stdin",
    # Unix signals
    "SIGKILL", "SIGTERM", "SIGINT", "SIGHUP", "SIGSTOP", "SIGQUIT", "SIGUSR1", "SIGUSR2",
})


def _is_non_path(value: str) -> bool:
    """Return True when the matched value is a path-shaped but non-path token.

    Catches:
      • All-uppercase strings (signal names, verdict constants like PARTIAL/FAIL)
      • Known non-path tokens (Python booleans, stream names, signal names)
      • Single-segment absolute paths whose bare name is all-uppercase or in the blocklist
    """
    bare = value.lstrip('/~.').lstrip('\\')
    # Normalize both separators for cross-platform analysis
    parts = [p for p in re.split(r'[/\\]', bare) if p]
    if not parts:
        return True
    # Any component that is a known non-path token → reject the whole match
    if any(p.rstrip('.') in _NON_PATH_TOKENS for p in parts):
        return True
    # All-uppercase components (≥2 letters) → reject (e.g. SIGKILL, PARTIAL, FAIL)
    if all(re.sub(r'[^A-Za-z]', '', p) == re.sub(r'[^A-Za-z]', '', p).upper()
           and len(re.sub(r'[^A-Za-z]', '', p)) >= 2
           for p in parts):
        return True
    return False


def _canonical_literal(literal: str) -> str:
    """Normalise a path literal for deduplication purposes.

    Strips a leading slash/backslash and trailing punctuation so that
    '/compile_manifest.json' and 'compile_manifest.json' map to the same key.
    """
    return literal.lstrip('/\\').rstrip('.')

_SYSTEM_PATH_PREFIXES = (
    "/usr", "/bin", "/sbin", "/lib", "/etc/ssl", "/dev/", "/proc/",
    "/sys/", "/tmp", "/var/log", "/opt/",
)

# Windows system paths that should not be parameterized
_WIN_SYSTEM_PATH_PREFIXES = (
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
)

def _is_system_path(value: str) -> bool:
    if any(value.startswith(pfx) for pfx in _SYSTEM_PATH_PREFIXES):
        return True
    upper = value.upper().replace("/", "\\")
    return any(upper.startswith(pfx.upper()) for pfx in _WIN_SYSTEM_PATH_PREFIXES)


def _make_param_name(literal: str, existing: set) -> str:
    """Convert a literal path/identifier to a safe, unique params_schema key."""
    clean = re.sub(r'[^a-z0-9]', '_', literal.lstrip('/~.').lower())
    clean = re.sub(r'_+', '_', clean).strip('_')[:28] or "value"
    name, counter = clean, 2
    while name in existing:
        name = f"{clean}_{counter}"
        counter += 1
    return name


def normalize_template_params(template: GEPTemplate) -> Tuple[GEPTemplate, List[str]]:
    """
    Scan all step goal/step_supplement/risk_assessment text for session-specific
    values (paths, filenames, host:port) that are NOT yet covered by a
    {{params.X}} placeholder, and automatically:
      1. Create a new params_schema entry for each (type="path", default=<original>).
      2. Replace the literal with {{params.<name>}} in every step field.

    Using the original literal as the default means the template remains
    immediately usable for identical environments, while allowing callers
    (extract_gep_params / _adapt_gep_steps) to override it from user context.

    Returns (normalized_template, list_of_auto_added_param_names).
    An empty list means the template was already fully parameterized.
    """
    existing_params: set = set(template.params_schema.keys())
    literal_to_param: Dict[str, str] = {}

    # Fields scanned per step (must be str, may be empty)
    def _step_texts(s: StepSpec) -> List[str]:
        return [s.goal, s.step_supplement, s.risk_assessment]

    # ── Pass 1: collect unparameterized literals ───────────────────────────
    # canonical_to_param maps the normalised form of a literal to its param name
    # so that path variants like '/foo.json' and 'foo.json' resolve to the same param.
    canonical_to_param: Dict[str, str] = {}

    for step in template.guide_steps:
        for text in _step_texts(step):
            for pattern, _ in _UNPARAMETERIZED_PATTERNS:
                for m in pattern.finditer(text):
                    literal = m.group(0)
                    if literal in literal_to_param:
                        continue
                    if _is_system_path(literal) or _is_non_path(literal):
                        continue
                    # Deduplicate path variants: '/foo.json' and 'foo.json' → same param
                    canon = _canonical_literal(literal)
                    if canon in canonical_to_param:
                        literal_to_param[literal] = canonical_to_param[canon]
                        continue
                    # If already declared in params_schema by default value, reuse it
                    existing_pname = next(
                        (
                            k for k, v in template.params_schema.items()
                            if _canonical_literal(str(
                                v.default if not isinstance(v, dict) else v.get("default", "")
                            )) == canon
                        ),
                        None,
                    )
                    if existing_pname is not None:
                        literal_to_param[literal] = existing_pname
                        canonical_to_param[canon] = existing_pname
                        continue
                    pname = _make_param_name(
                        literal, existing_params | set(literal_to_param.values())
                    )
                    literal_to_param[literal] = pname
                    canonical_to_param[canon] = pname

    if not literal_to_param:
        return template, []

    # ── Pass 2: substitute literals → {{params.X}} ────────────────────────
    # Sort longest-first to avoid partial replacement of sub-strings.
    sorted_literals = sorted(literal_to_param.keys(), key=len, reverse=True)

    def _sub(text: str) -> str:
        for lit in sorted_literals:
            text = text.replace(lit, f"{{{{params.{literal_to_param[lit]}}}}}")
        return text

    new_steps: List[StepSpec] = []
    for s in template.guide_steps:
        new_steps.append(StepSpec(
            step_id=s.step_id,
            description=s.description,
            goal=_sub(s.goal),
            step_supplement=_sub(s.step_supplement),
            parallel_group=s.parallel_group,
            is_aggregation=s.is_aggregation,
            planner_reasoning=s.planner_reasoning,
            expected_outcomes=list(s.expected_outcomes),
            risk_assessment=_sub(s.risk_assessment),
            required_context_keys=list(s.required_context_keys),
            ssh_target=s.ssh_target,
            tools_required=list(s.tools_required),
        ))

    # ── Pass 3: extend params_schema with new entries ──────────────────────
    # Only create entries for literals that were NOT already in params_schema.
    # Literals mapped to an existing param key (via the already_covered branch
    # above) must NOT generate a duplicate entry — doing so would create two
    # params with the same default value but different keys (Problem E).
    new_schema: Dict[str, ParamSpec] = dict(template.params_schema)
    added: List[str] = []
    for literal, pname in literal_to_param.items():
        if pname not in existing_params:  # skip pre-existing params
            new_schema[pname] = ParamSpec(
                type="path",
                description=f'Auto-parameterized (original value: "{literal}")',
                default=literal,  # safe fallback for same-environment reuse
            )
            added.append(pname)

    normalized = GEPTemplate(
        id=template.id,
        name=template.name,
        description=template.description,
        created_at=template.created_at,
        version=template.version,
        source_log_path=template.source_log_path,
        params_schema=new_schema,
        guide_steps=new_steps,
    )
    return normalized, added


def validate_instantiated_steps(
    template_guide_steps: List,
    adapted_step_goals: List[str],
    user_goal: str,
    params_schema: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Post-instantiation invariant checker (deterministic, zero LLM cost).

    Checks two invariants after any instantiation path (LLM or mechanical):
      I1 — No unresolved {{params.X}} tokens remain in any adapted step goal.
      I2 — No param default values were copied verbatim into adapted steps
           without the user mentioning them (only checked when params_schema
           is supplied; requires default length > 5 and non-system path).

    I2 uses params_schema defaults (not a regex scan of raw template goals)
    to avoid false positives on generic filenames that appear in both the
    template and legitimately in the user's adapted goals.

    Returns a list of violation strings (empty = all invariants satisfied).
    """
    violations: List[str] = []

    # ── I1: unresolved placeholders ───────────────────────────────────────
    for i, goal in enumerate(adapted_step_goals):
        unresolved = re.findall(r'\{\{params\.(\w+)\}\}', goal)
        if unresolved:
            violations.append(
                f"step[{i}]: unresolved placeholder(s): {unresolved}"
            )

    # ── I2: param defaults used verbatim (not adapted to user context) ────
    # Only runs when the caller supplies params_schema (normalized templates).
    # Checks each param whose default is a non-trivial, non-system-path string
    # and flags it if the raw default appears in an adapted goal but NOT in
    # the user's goal text — i.e. the LLM kept the template default instead
    # of substituting the user's actual value.
    if params_schema:
        user_goal_lower = user_goal.lower()
        for i, adapted_goal in enumerate(adapted_step_goals):
            for pname, pspec in params_schema.items():
                default = (
                    pspec.get('default') if isinstance(pspec, dict)
                    else getattr(pspec, 'default', None)
                )
                if (
                    isinstance(default, str)
                    and len(default) > 5
                    and not _is_system_path(default)
                    and default in adapted_goal
                    and default.lower() not in user_goal_lower
                ):
                    violations.append(
                        f"step[{i}]: param '{pname}' default value '{default}' "
                        f"used verbatim — may not be adapted to user's context"
                    )

    return violations

def validate_template_shape(t: GEPTemplate) -> List[str]:
    """Return a list of human-readable problems with *t* (empty = OK).

    Catches malformed templates that load_template happily parses but that
    later break the GEP flow:
      * Missing ``name`` → the receptionist intro shows a blank "Template:"
        header and instantiation cannot identify the template by name.
      * Empty ``guide_steps`` → instantiate_gep_plan returns no steps and
        the planner falls through to normal planning, silently bypassing
        the template the user just confirmed.
      * Step missing ``goal`` → instantiation has nothing to substitute
        params into.

    Used by load_template (to fail fast on broken files) and by
    list_templates (to filter them out of the receptionist's match pool).
    """
    problems: List[str] = []
    if not (t.name or "").strip():
        problems.append("missing 'name' field")
    if not t.guide_steps:
        problems.append("empty 'guide_steps' — template would activate to a no-op")
    else:
        for i, s in enumerate(t.guide_steps):
            if not (s.description or s.goal or "").strip():
                problems.append(f"guide_steps[{i}] has neither description nor goal")
    return problems


def load_template(template_id: str) -> GEPTemplate:
    """
    Load a GEPTemplate from HandQ/gep_templates/<template_id>.json.

    If no exact match is found, falls back to a prefix match so that a short
    ID (e.g. "4bb46009") can locate a file whose name is the full UUID
    (e.g. "4bb46009-dd25-4597-8a79-255f64c481ee.json").

    Raises FileNotFoundError if no matching template is found.
    Raises ValueError if the short ID matches more than one file, OR if the
    matched file fails validate_template_shape (so a malformed template
    cannot silently activate to nothing).
    """
    def _load_from_path(p: Path) -> GEPTemplate:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not data.get("id"):
            data["id"] = p.stem
        t = GEPTemplate.from_dict(data)
        problems = validate_template_shape(t)
        if problems:
            raise ValueError(
                f"Template at {p} is malformed: {'; '.join(problems)}. "
                "Re-save it via the Save GEP flow or fix the JSON manually."
            )
        return t

    path = _template_path(template_id)
    if path.exists():
        return _load_from_path(path)

    # Exact file not found — try prefix match inside the templates directory.
    safe_id = _sanitize_template_id(template_id)
    matches = list(_templates_dir().glob(f"{safe_id}*.json"))
    if len(matches) == 1:
        return _load_from_path(matches[0])
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous template id {template_id!r}: matches {[m.name for m in matches]}"
        )
    raise FileNotFoundError(
        f"No template found for id {template_id!r} in {_templates_dir()}"
    )


def save_template(template: GEPTemplate) -> str:
    """
    Persist a GEPTemplate to HandQ/gep_templates/<template.id>.json.

    Creates the gep_templates directory if it does not exist.
    Returns the absolute path of the saved file.
    """
    tdir = _templates_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / f"{_sanitize_template_id(template.id)}.json"
    path.write_text(
        json.dumps(template.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path.resolve())


def list_templates(*, include_invalid: bool = False) -> List[GEPTemplate]:
    """
    Return all GEPTemplate objects found in HandQ/gep_templates/.

    Files that cannot be parsed are silently skipped.

    When ``include_invalid`` is False (the default), templates that fail
    validate_template_shape are also skipped — this keeps the receptionist's
    match pool free of templates that would activate to a no-op.

    When ``include_invalid`` is True, every parseable template is returned;
    callers (e.g. the Templates review panel) can then inspect each entry's
    ``_problems`` attribute to surface broken entries to the user.
    """
    tdir = _templates_dir()
    if not tdir.exists():
        return []
    templates: List[GEPTemplate] = []
    for json_file in sorted(tdir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if not data.get("id"):
                data["id"] = json_file.stem
            t = GEPTemplate.from_dict(data)
        except Exception:
            continue
        problems = validate_template_shape(t)
        if problems:
            if not include_invalid:
                continue
            # Stash the problems on the object so admin views can render them
            # without re-running the validator.
            setattr(t, "_problems", problems)
            setattr(t, "_source_path", str(json_file.resolve()))
        templates.append(t)
    return templates


def list_templates_summary() -> str:
    """
    Return a compact JSON-serialisable list of available templates suitable
    for injection into LLM prompts (receptionist, planner).

    Each entry contains only: id, name, description.
    Returns an empty list serialised as "[]" when no templates exist.
    """
    templates = list_templates()
    summary = [
        {
            "id":          t.id,
            "name":        t.name,
            "description": t.description,
            "version":     t.version,
            "created_at":  t.created_at,
            "params":      list(t.params_schema.keys()),
            "steps":       [s.description for s in t.guide_steps],
        }
        for t in templates
    ]
    return json.dumps(summary, indent=2, ensure_ascii=False)
