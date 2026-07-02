# -*- coding: utf-8 -*-
"""
Secret redactor — strip known plaintext secrets from tool output before it
flows into the agent's conversation history (and from there to the LLM API
and on-disk session logs).

Threat model
------------
HandQ stores SSH passwords in the OS keyring. The agent has shell access to
a Python interpreter that can ``import keyring`` and read those passwords —
this is by design because legitimate SSH workflows need them. The leak path
is when the agent (or one of its scripts) prints the plaintext to stdout:

    >>> python -c "import keyring; print(keyring.get_password(...))"

stdout becomes a ToolResult, the ToolResult lands in conversation history,
the conversation history is uploaded to the LLM provider on every turn, and
also persisted to disk in ``handq-engine.log`` / session logs. The password
is now (a) on the LLM provider's servers and (b) in plaintext on disk.

This module does not stop the agent from reading keyring — process-internal
isolation is not a real boundary on a same-user OS. It enforces an EGRESS
filter: by the time a ToolResult exits the tool layer it is scrubbed of any
plaintext secret matching a known keyring entry.

Coverage
--------
- ``ToolResult.output`` (recursive: str / dict / list / tuple)
- ``ToolResult.error`` (str)

Limits
------
- Only plaintext matches are caught. Base64, hex, custom obfuscation pass
  through unchanged.
- Redactor pulls the secret list once at first use (lazy). New keyring
  entries added mid-session require ``SecretRedactor.get().refresh()``.
- Passwords shorter than ``_MIN_SECRET_LEN`` are skipped to avoid mangling
  unrelated short tokens (e.g. a 4-char common-word password colliding with
  a regular shell word).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .logger import get_logger

# Skip very short secrets. A password of 4-7 chars risks colliding with
# normal output tokens (echo / pass / test / etc.). HandQ's setup wizard
# encourages longer passwords; a short one was likely a placeholder.
_MIN_SECRET_LEN = 8


class SecretRedactor:
    """Process-wide registry of known plaintext secrets to scrub.

    Singleton (per process). First call to :py:meth:`get` triggers a one-shot
    scan of HandQ's known credential stores. Adding a new SSH credential
    mid-session is rare; callers that need to pick up new entries can call
    :py:meth:`refresh` explicitly.
    """

    _instance: Optional["SecretRedactor"] = None

    def __init__(self) -> None:
        # (secret_value, replacement_label). Sorted longest-first so a
        # secret that is a prefix of another secret is replaced correctly.
        self._pairs: List[Tuple[str, str]] = []
        self.logger = get_logger()

    @classmethod
    def get(cls) -> "SecretRedactor":
        if cls._instance is None:
            cls._instance = cls()
            cls._instance.refresh()
        return cls._instance

    @classmethod
    def reset_for_test(cls) -> None:
        """Drop the singleton — used by tests that pin a different state."""
        cls._instance = None

    def refresh(self) -> None:
        """Re-scan known credential stores. Idempotent.

        Currently only HandQ's SSH credential YAMLs (~/.ssh/handq_*.yaml).
        Each yields a (keyring_service, username) lookup against the OS
        keyring. Misses (no entry / read error) are skipped silently.
        """
        try:
            import keyring as _keyring  # type: ignore[import-untyped]
            import yaml as _yaml
        except Exception as exc:
            self.logger.debug(
                f"SecretRedactor: keyring/yaml import failed ({exc}); "
                f"redactor will be a no-op",
                component="SecretRedactor",
            )
            self._pairs = []
            return

        ssh_dir = Path(os.path.expanduser("~")) / ".ssh"
        if not ssh_dir.is_dir():
            self._pairs = []
            return

        pairs: List[Tuple[str, str]] = []
        for path in sorted(ssh_dir.glob("handq_*.yaml")):
            try:
                data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            service = (data.get("keyring_service") or "").strip()
            username = (data.get("username") or "").strip()
            if not service or not username:
                continue
            try:
                pw = _keyring.get_password(service, username)
            except Exception as exc:
                self.logger.debug(
                    f"SecretRedactor: keyring lookup failed for "
                    f"{service}/{username}: {exc}",
                    component="SecretRedactor",
                )
                continue
            if not pw or len(pw) < _MIN_SECRET_LEN:
                continue
            label = f"<redacted:{service}/{username}>"
            pairs.append((pw, label))

        # Longest-first prevents a shorter secret that is a substring of a
        # longer one from being replaced first and corrupting the longer match.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._pairs = pairs
        if pairs:
            self.logger.info(
                f"SecretRedactor: registered {len(pairs)} secret(s) for egress filtering",
                component="SecretRedactor",
            )

    # ── Redaction primitives ─────────────────────────────────────────────────

    def redact_str(self, text: str) -> str:
        if not text or not self._pairs:
            return text
        for secret, label in self._pairs:
            if secret in text:
                text = text.replace(secret, label)
        return text

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings inside a nested structure."""
        if not self._pairs:
            return value
        if isinstance(value, str):
            return self.redact_str(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(v) for v in value)
        return value

    def redact_tool_result(self, result: Any) -> Any:
        """Mutate a ToolResult in place, scrubbing output and error.

        Typed as ``Any`` to avoid a circular import on ``ToolResult``;
        duck-types via attribute access. Returns the same instance for
        chaining.
        """
        if not self._pairs:
            return result
        try:
            result.output = self.redact_value(getattr(result, "output", None))
            err = getattr(result, "error", None)
            if isinstance(err, str):
                result.error = self.redact_str(err)
        except Exception as exc:
            # Never let a redactor failure break tool dispatch.
            self.logger.warning(
                f"SecretRedactor.redact_tool_result failed: {exc}",
                component="SecretRedactor",
            )
        return result
