"""
UserConfirmation — confirmation dataclass.

The contract between the InteractionManager and the PersistentAgent for
user confirmation responses.

Four kinds of confirmation are distinguished so the agent loop can react
correctly:

  YES            — user approves; continue execution.
  NO             — user rejects; do not execute.
  MESSAGE        — user provides a new instruction (tool-confirmation path);
                   propagated up to FlowControllerV2 so the planner can replan.
  RISK_GUIDANCE  — user provides guidance for a risk dialog; handled within
                   the current agent step rather than escalating to the planner.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ConfirmationType(Enum):
    """Type of user confirmation."""
    YES = "yes"
    NO = "no"
    MESSAGE = "message"
    RISK_GUIDANCE = "risk_guidance"


@dataclass
class UserConfirmation:
    """User confirmation response."""
    confirmation_type: ConfirmationType
    message: Optional[str] = None  # populated for MESSAGE / RISK_GUIDANCE

    @classmethod
    def yes(cls) -> "UserConfirmation":
        return cls(confirmation_type=ConfirmationType.YES)

    @classmethod
    def no(cls) -> "UserConfirmation":
        return cls(confirmation_type=ConfirmationType.NO)

    @classmethod
    def with_message(cls, user_message: str) -> "UserConfirmation":
        """Tool-confirmation path: user typed a free-form instruction.

        The message propagates up to FlowControllerV2 as a new user instruction
        so the planner can replan the entire task.
        """
        return cls(confirmation_type=ConfirmationType.MESSAGE, message=user_message)

    @classmethod
    def risk_guidance(cls, user_message: str) -> "UserConfirmation":
        """Risk-confirmation path: user typed guidance for the current risky op.

        Unlike ``with_message``, this is handled entirely inside the current
        PersistentAgent iteration — the guidance is injected as an observation
        so the agent can re-think without escalating to the planner.
        """
        return cls(confirmation_type=ConfirmationType.RISK_GUIDANCE, message=user_message)

    def is_approved(self) -> bool:
        return self.confirmation_type == ConfirmationType.YES

    def is_rejected(self) -> bool:
        return self.confirmation_type == ConfirmationType.NO

    def has_new_message(self) -> bool:
        """True iff this is a tool-confirmation path with a new instruction."""
        return self.confirmation_type == ConfirmationType.MESSAGE

    def is_risk_guidance(self) -> bool:
        """True iff this is a risk-confirmation path with in-step guidance."""
        return self.confirmation_type == ConfirmationType.RISK_GUIDANCE

    def __str__(self) -> str:
        if self.confirmation_type in (ConfirmationType.MESSAGE, ConfirmationType.RISK_GUIDANCE):
            return f"UserConfirmation({self.confirmation_type.value.upper()}: '{self.message}')"
        return f"UserConfirmation({self.confirmation_type.value.upper()})"
