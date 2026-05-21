import hashlib
import os
import threading
from typing import Dict, Optional, Tuple


class FileState:
    """Singleton tracking file reads to enforce read-before-write safety."""

    _instance: Optional["FileState"] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        # Map from path -> (content_hash, read_count)
        self._reads: Dict[str, Tuple[str, int]] = {}
        self._rlock = threading.Lock()

    # ------------------------------------------------------------------
    # Singleton lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "FileState":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    @classmethod
    def reset_for_session(cls) -> None:
        """Clear all recorded reads for the current session.

        Call this at the start of each agent session to prevent read records
        from a previous session leaking into the new one (process-level singleton).
        """
        instance = cls.get_instance()
        instance.clear()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def record_read(self, path: str, content_hash: str) -> None:
        """Record that *path* was read with the given *content_hash* (sha256 hex digest)."""
        path = os.path.realpath(path)
        with self._rlock:
            existing = self._reads.get(path)
            count = (existing[1] + 1) if existing else 1
            self._reads[path] = (content_hash, count)

    def check_stale(self, path: str) -> Tuple[bool, str]:
        """Return (is_stale, reason).

        A path is considered stale / unsafe to write when:
        - It has never been read in this session, OR
        - The file on disk has changed since it was last read.
        """
        path = os.path.realpath(path)
        with self._rlock:
            entry = self._reads.get(path)

        if entry is None:
            return True, f"'{path}' has not been read in this session"

        if not os.path.exists(path):
            # File was read (possibly created in memory) but no longer on disk —
            # treat as not stale so a write can (re)create it.
            return False, ""

        try:
            with open(path, "rb") as fh:
                current_bytes = fh.read()
        except OSError as exc:
            return True, f"Cannot read '{path}' for staleness check: {exc}"

        recorded_hash = entry[0]
        current_hash = hashlib.sha256(current_bytes).hexdigest()

        if recorded_hash != current_hash:
            return True, (
                f"'{path}' has changed on disk since it was last read "
                "(re-read the file before writing)"
            )

        return False, ""

    def check_stale_and_read(
        self, path: str, encoding: str = "utf-8"
    ) -> Tuple[bool, str, "Optional[str]"]:
        """Like check_stale() but also returns the file content on success.

        Returns:
            (is_stale, reason, content)
            - If is_stale is True: content is None.
            - If is_stale is False: content is the file text read during the
              check (same open() call used for the hash comparison — no TOCTOU
              window).
        """
        path = os.path.realpath(path)
        with self._rlock:
            entry = self._reads.get(path)

        if entry is None:
            return True, f"'{path}' has not been read in this session", None

        if not os.path.exists(path):
            return False, "", None

        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            return True, f"Cannot read '{path}' for staleness check: {exc}", None

        recorded_hash = entry[0]
        current_hash = hashlib.sha256(raw).hexdigest()

        if recorded_hash != current_hash:
            return True, (
                f"'{path}' has changed on disk since it was last read "
                "(re-read the file before writing)"
            ), None

        try:
            content = raw.decode(encoding)
        except UnicodeDecodeError:
            content = raw.decode("latin-1")

        return False, "", content

    def was_read(self, path: str) -> bool:
        """Return True if *path* has been read at least once this session."""
        path = os.path.realpath(path)
        with self._rlock:
            return path in self._reads

    def clear(self) -> None:
        """Clear all recorded reads (e.g. at session start)."""
        with self._rlock:
            self._reads.clear()

    def get_distinct_path_count(self) -> int:
        """Return the total number of distinct paths that have been read this session."""
        with self._rlock:
            return len(self._reads)
