"""
System State - State machine and user interaction definitions
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SystemState(Enum):
    """System state enumeration"""
    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_PLAN_CONFIRMATION = "awaiting_plan_confirmation"
    EXECUTING = "executing"
    PAUSED = "paused"
    AWAITING_RISK_CONFIRMATION = "awaiting_risk_confirmation"
    REPLANNING = "replanning"
    ERROR = "error"
    COMPLETED = "completed"


class ConfirmationType(Enum):
    """Type of user confirmation"""
    YES = "yes"  # User approves, continue execution
    NO = "no"  # User rejects, do not execute
    MESSAGE = "message"  # User provides new instruction (tool confirmation → propagates to controller)
    RISK_GUIDANCE = "risk_guidance"  # User provides guidance for a risk dialog → handled within the step


@dataclass
class UserConfirmation:
    """
    User confirmation response
    
    Represents user's response to a confirmation request
    """
    confirmation_type: ConfirmationType
    message: Optional[str] = None  # Only used when type is MESSAGE
    
    @classmethod
    def yes(cls) -> 'UserConfirmation':
        """Create a YES confirmation"""
        return cls(confirmation_type=ConfirmationType.YES)
    
    @classmethod
    def no(cls) -> 'UserConfirmation':
        """Create a NO confirmation"""
        return cls(confirmation_type=ConfirmationType.NO)
    
    @classmethod
    def with_message(cls, user_message: str) -> 'UserConfirmation':
        """
        Create a MESSAGE confirmation (tool confirmation path).

        The message is propagated to the FlowController as USER_NEW_INSTRUCTION
        so the Planner can replan the entire task.

        Args:
            user_message: User's new instruction
        """
        return cls(confirmation_type=ConfirmationType.MESSAGE, message=user_message)

    @classmethod
    def risk_guidance(cls, user_message: str) -> 'UserConfirmation':
        """
        Create a RISK_GUIDANCE confirmation (risk confirmation path).

        Unlike MESSAGE, this is handled entirely within the RuntimeAgent step:
        the guidance is injected as an observation so the agent can re-think
        without the message ever escaping to the FlowController / Planner.

        Args:
            user_message: User's guidance for the current risky operation
        """
        return cls(confirmation_type=ConfirmationType.RISK_GUIDANCE, message=user_message)

    def is_approved(self) -> bool:
        """Check if user approved"""
        return self.confirmation_type == ConfirmationType.YES

    def is_rejected(self) -> bool:
        """Check if user rejected"""
        return self.confirmation_type == ConfirmationType.NO

    def has_new_message(self) -> bool:
        """Check if user provided new instruction (tool confirmation path)"""
        return self.confirmation_type == ConfirmationType.MESSAGE

    def is_risk_guidance(self) -> bool:
        """
        Check if user provided guidance for a risk confirmation.

        When True the RuntimeAgent handles the response internally (adds it as
        an observation and continues the loop) instead of propagating it to the
        FlowController as USER_NEW_INSTRUCTION.
        """
        return self.confirmation_type == ConfirmationType.RISK_GUIDANCE

    def __str__(self) -> str:
        """String representation"""
        if self.confirmation_type in (ConfirmationType.MESSAGE, ConfirmationType.RISK_GUIDANCE):
            return f"UserConfirmation({self.confirmation_type.value.upper()}: '{self.message}')"
        else:
            return f"UserConfirmation({self.confirmation_type.value.upper()})"
