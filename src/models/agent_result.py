"""
Agent Result Data Models

This module defines structured data models for agent execution results,
ensuring consistency across the system.
"""

from dataclasses import dataclass, field
from typing import Optional, List
from .token_usage import TokenUsage


@dataclass
class AgentResult:
    """
    Structured result from agent execution.

    Attributes:
        success: Whether the execution was successful
        iterations: Number of iterations performed
        reasoning: Agent's internal reasoning / thought process (for debugging)
        factual_outcome: List of factual statements describing what the agent actually did
                         and what results it obtained — the primary report for the planner.
                         Populated from Decision.factual_outcome when tool_name is None (goal achieved).
        artifacts: Files or resources created/modified (from Decision.artifacts).
        key_findings: Important discoveries made during execution (from Decision.key_findings).
        error: Error message if execution failed
    """
    success: bool
    reasoning: str
    iterations: int = 0
    error: Optional[str] = None
    # Factual report fields — populated from Decision when tool_name is None (goal achieved).
    # These are the primary information channel to the planner.
    factual_outcome: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    key_findings: List[str] = field(default_factory=list)
    # Ordered list of tool names attempted in this step (one entry per Think/Act iteration).
    # Populated by RuntimeAgent.run() so MetricsCollector can track failed-approach reuse.
    tools_used: List[str] = field(default_factory=list)
    # Cumulative token usage across all LLM calls in this step.
    token_usage: TokenUsage = field(default_factory=TokenUsage)

    # ── Backward-compat property accessors ───────────────────────────────────

    @property
    def total_input_tokens(self) -> int:
        return self.token_usage.input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.token_usage.output_tokens

    @property
    def total_cache_creation_tokens(self) -> int:
        return self.token_usage.cache_creation_tokens

    @property
    def total_cache_read_tokens(self) -> int:
        return self.token_usage.cache_read_tokens
    
    def to_dict(self) -> dict:
        """Convert to dictionary format for backward compatibility"""
        result_dict = {
            "success": self.success,
            "reasoning": self.reasoning,
            "iterations": self.iterations,
        }
        
        # Add optional fields only if they have values
        if self.reasoning is not None:
            result_dict["reasoning"] = self.reasoning
        if self.error is not None:
            result_dict["error"] = self.error
        
        return result_dict
    
    @classmethod
    def from_dict(cls, data: dict) -> 'AgentResult':
        """Create AgentResult from dictionary"""
        return cls(
            success=data.get("success", False),
            reasoning=data.get("reasoning", ""),
            iterations=data.get("iterations", 0),
            error=data.get("error")
        )
    
    @classmethod
    def create_result(
        cls,
        success: bool,
        reasoning: str,
        error: Optional[str] = None,
        iterations: int = 0,
        factual_outcome: Optional[List[str]] = None,
        artifacts: Optional[List[str]] = None,
        key_findings: Optional[List[str]] = None,
        tools_used: Optional[List[str]] = None,
        token_usage: Optional[TokenUsage] = None,
        # Backward-compat individual kwargs — used when token_usage is not provided
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_cache_creation_tokens: int = 0,
        total_cache_read_tokens: int = 0,
    ) -> 'AgentResult':
        """
        Create an AgentResult (success or failure).

        Args:
            success: Whether the execution was successful
            reasoning: Agent's internal thought process (for debugging)
            factual_outcome: List of factual statements describing what was done
                             and what results were obtained (for the planner).
                             Pass Decision.factual_outcome here when tool_name is None.
            artifacts: Files/resources created or modified (pass Decision.artifacts).
            key_findings: Important discoveries (pass Decision.key_findings).
            error: Detailed error message (for failures)
            iterations: Number of iterations
            tools_used: Ordered list of tool names attempted (one per Think/Act iteration).
            total_input_tokens: Cumulative input tokens across all LLM calls in this step.
            total_output_tokens: Cumulative output tokens across all LLM calls in this step.
        """
        return cls(
            success=success,
            error=error,
            reasoning=reasoning,
            factual_outcome=factual_outcome or [],
            artifacts=artifacts or [],
            key_findings=key_findings or [],
            iterations=iterations,
            tools_used=tools_used or [],
            token_usage=token_usage or TokenUsage(
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_creation_tokens=total_cache_creation_tokens,
                cache_read_tokens=total_cache_read_tokens,
            ),
        )
