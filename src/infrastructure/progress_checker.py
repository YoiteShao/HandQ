from typing import List, Optional
from dataclasses import dataclass

@dataclass
class ProgressStatus:
    """Progress status — exposes only reminder-related information."""
    should_add_reminder: bool
    reminder_message: Optional[str] = None
    
class ProgressAnalyzerBase:
    """
    Shared base for success/failure pattern analysis.

    Encapsulates the common logic used by both the agent-level
    SuccessPatternAnalyzer and the planner-level PlannerProgressTracker:
      - success/failure history tracking
      - consecutive failure counting
      - sliding-window success rate
      - graduated reminder generation (warning → critical)
      - summary reporting

    Subclasses must implement:
      - _default_config()
      - analyze()
      - _generate_warning_reminder()
      - _generate_critical_reminder()
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config if config is not None else self._default_config()
        self.success_history: List[bool] = []
        # Short string set by add_result() when a failure has a known type
        # (e.g. "write_param_error").  Cleared automatically on success.
        # Subclasses can read this in analyze() to generate targeted reminders.
        self.last_error_hint: Optional[str] = None

    def _default_config(self) -> dict:
        raise NotImplementedError

    # ── History management ───────────────────────────────────────────────────

    def add_result(self, success: bool, error_hint: Optional[str] = None) -> None:
        """Record the outcome of one operation.

        Args:
            success:    Whether the operation succeeded.
            error_hint: Optional short tag describing the failure type
                        (e.g. ``"write_param_error"``).  Stored in
                        ``self.last_error_hint`` so subclasses can generate
                        more targeted reminders in ``analyze()``.
                        Automatically cleared when *success* is True.
        """
        self.success_history.append(success)
        self.last_error_hint = error_hint if not success else None

    # ── Core metrics ─────────────────────────────────────────────────────────

    def _count_consecutive_failures(self) -> int:
        """Count how many of the most recent operations failed in a row."""
        count = 0
        for success in reversed(self.success_history):
            if not success:
                count += 1
            else:
                break
        return count

    def _get_success_rate(self, window_size: Optional[int] = None) -> float:
        """Return the success rate over the most recent window of operations."""
        if not self.success_history:
            return 1.0
        window = window_size if window_size is not None else self.config["window_size"] * 2
        recent = self.success_history[-window:]
        return sum(recent) / len(recent) if recent else 1.0

    # ── Reminder logic ────────────────────────────────────────────────────────

    def _should_add_reminder(self, consecutive_failures: int) -> bool:
        """Return True when consecutive failures reach the moderate threshold."""
        if not self.config.get("enable_reminders", True):
            return False
        return consecutive_failures >= self.config["moderate_stagnation_threshold"]

    def _generate_reminder(self, consecutive_failures: int, success_rate: float) -> str:
        """Dispatch to warning or critical reminder based on severity."""
        if consecutive_failures >= self.config["severe_stagnation_threshold"]:
            return self._generate_critical_reminder(consecutive_failures, success_rate)
        return self._generate_warning_reminder(consecutive_failures, success_rate)

    def _generate_warning_reminder(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        raise NotImplementedError

    def _generate_critical_reminder(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        raise NotImplementedError

    # ── Summary ───────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Return a snapshot of the current tracker state (for logging)."""
        return {
            "total_operations": len(self.success_history),
            "consecutive_failures": self._count_consecutive_failures(),
            "success_rate": self._get_success_rate(),
            "recent_history": self.success_history[-10:] if self.success_history else [],
        }
