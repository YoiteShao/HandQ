"""Markdown chunking — split entry body into searchable chunks.

Two paths:
- Body fits in CHUNK_MAX_CHARS → single chunk (zero-overhead common case).
- Larger body → split on H2 boundaries first, then by paragraph if any
  resulting section still exceeds the cap.

Each chunk records (start_line, end_line, sha256(text)) so embeddings can be
content-addressed via embedding_cache.hash.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List

from . import _constants as C


_H2_RE = re.compile(r"^##\s", re.MULTILINE)


@dataclass
class ChunkSpec:
    text: str
    start_line: int
    end_line: int
    hash: str


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _line_of(content: str, offset: int) -> int:
    """0-indexed line that contains *offset* in *content*."""
    return content.count("\n", 0, offset)


def chunk_markdown(
    content: str, *, max_chars: int = C.CHUNK_MAX_CHARS,
) -> List[ChunkSpec]:
    content = content.strip("\n")
    if not content:
        return [ChunkSpec(text="", start_line=0, end_line=0, hash=_sha256(""))]

    # Fast path: small body — one chunk, no analysis needed.
    if len(content) <= max_chars:
        return [ChunkSpec(
            text=content,
            start_line=0,
            end_line=content.count("\n"),
            hash=_sha256(content),
        )]

    # Split on H2 boundaries — those are the section markers our triage
    # prompt requires (## Memory Points / ## Key Insights / ## Description).
    sections = _split_on_h2(content)

    chunks: List[ChunkSpec] = []
    for section_text, section_start_line in sections:
        if len(section_text) <= max_chars:
            chunks.append(ChunkSpec(
                text=section_text,
                start_line=section_start_line,
                end_line=section_start_line + section_text.count("\n"),
                hash=_sha256(section_text),
            ))
            continue
        # Still too big — split this section on blank lines.
        chunks.extend(_split_paragraphs(section_text, section_start_line, max_chars))

    return chunks


def _split_on_h2(content: str) -> List[tuple]:
    """Return [(section_text, start_line), ...] for each H2-delimited section.

    The text BEFORE the first H2 is its own implicit section.
    """
    matches = list(_H2_RE.finditer(content))
    if not matches:
        return [(content, 0)]

    sections: List[tuple] = []
    if matches[0].start() > 0:
        prefix = content[:matches[0].start()].rstrip("\n")
        if prefix:
            sections.append((prefix, 0))

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section = content[start:end].rstrip("\n")
        sections.append((section, _line_of(content, start)))

    return sections


def _split_paragraphs(text: str, base_line: int, max_chars: int) -> List[ChunkSpec]:
    """Greedy pack paragraphs into chunks of <= max_chars. Always make
    forward progress: a single paragraph larger than max_chars is emitted
    on its own (rather than infinite-looped or hard-truncated)."""
    paragraphs = text.split("\n\n")
    chunks: List[ChunkSpec] = []
    cur: List[str] = []
    cur_chars = 0
    cur_start_line = base_line
    line_cursor = base_line

    for p in paragraphs:
        p_lines = p.count("\n") + 1
        # +2 for the "\n\n" separator we'll re-insert (only once per pack)
        increment = len(p) + (2 if cur else 0)
        if cur and cur_chars + increment > max_chars:
            packed = "\n\n".join(cur)
            chunks.append(ChunkSpec(
                text=packed,
                start_line=cur_start_line,
                end_line=cur_start_line + packed.count("\n"),
                hash=_sha256(packed),
            ))
            cur = []
            cur_chars = 0
            cur_start_line = line_cursor
        cur.append(p)
        cur_chars += increment if cur_chars else len(p)
        line_cursor += p_lines + 1  # +1 for blank-line separator

    if cur:
        packed = "\n\n".join(cur)
        chunks.append(ChunkSpec(
            text=packed,
            start_line=cur_start_line,
            end_line=cur_start_line + packed.count("\n"),
            hash=_sha256(packed),
        ))

    return chunks
