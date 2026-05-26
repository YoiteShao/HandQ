"""Dataclasses + enums for the long-term memory system."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class CandidateStatus(str, Enum):
    PENDING = "pending"
    TRIAGING = "triaging"
    ACCEPTED_MEMORY = "accepted_memory"
    ACCEPTED_KNOWLEDGE = "accepted_knowledge"
    ACCEPTED_BOTH = "accepted_both"
    REJECTED = "rejected"
    FAILED = "failed"


class MemoryDimension(str, Enum):
    AGENTIC = "agentic"   # how the user wants the agent to BEHAVE
    INSIGHT = "insight"   # factual context about the user / environment


class KnowledgeCategory(str, Enum):
    DOMAIN = "domain"
    PEOPLE = "people"
    PROCESS = "process"
    CODING = "coding"
    OTHER = "other"


class EntryKind(str, Enum):
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    PROCEDURE = "procedure"  # P6 — not implemented in v1


@dataclass
class Candidate:
    id: str
    source: str
    source_ref: Optional[str]
    raw_text: str
    hint: Optional[str]
    metadata: dict
    status: CandidateStatus
    retry_count: int
    created_at: int


@dataclass
class Chunk:
    id: str
    entry_id: str
    chunk_index: int
    text: str
    hash: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class Entry:
    id: str
    kind: EntryKind
    summary: str = ""
    content: str = ""                          # joined chunks
    chunks: List[Chunk] = field(default_factory=list)
    dimension: Optional[MemoryDimension] = None       # memory only
    category: Optional[KnowledgeCategory] = None      # knowledge only
    archived: bool = False
    archived_reason: Optional[str] = None
    version: int = 1
    source: str = ""
    source_ref: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0
    score: Optional[float] = None              # filled at recall time


@dataclass
class TriageVerdict:
    """One triage call may emit memory and/or knowledge verdicts."""
    worth_memory: bool = False
    worth_knowledge: bool = False

    memory_action: str = "skip"                # 'create' | 'update' | 'skip'
    memory_dimension: Optional[MemoryDimension] = None
    memory_summary: str = ""
    memory_content: str = ""
    memory_update_id: Optional[str] = None

    knowledge_action: str = "skip"
    knowledge_category: Optional[KnowledgeCategory] = None
    knowledge_summary: str = ""
    knowledge_content: str = ""
    knowledge_update_id: Optional[str] = None

    reason: str = ""
