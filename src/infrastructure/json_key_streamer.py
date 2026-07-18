"""
JsonKeyStreamer — incremental JSON value extractor for streaming LLM output.

Tracks a specific top-level JSON key (default: "response_to_user") across
streamed text chunks and emits the *unescaped* string content of that value
as it arrives.
"""
from typing import List


class JsonKeyStreamer:
    """State machine that extracts a target JSON string value from streamed chunks.

    Usage::

        streamer = JsonKeyStreamer("response_to_user")
        for chunk in llm_stream:
            fragments = streamer.feed(chunk)
            for frag in fragments:
                emit_to_ui(frag)
    """

    # States
    _SCANNING = 0        # looking for the target key
    _AFTER_KEY = 1       # found key, expecting ':'
    _AFTER_COLON = 2     # found ':', expecting '"' to start value
    _IN_VALUE = 3        # inside the target string value
    _AT_POSSIBLE_END = 4 # saw '"' inside value; need next non-ws char to confirm end
    _DONE = 5            # target value fully consumed

    def __init__(self, target_key: str = "response_to_user") -> None:
        self._target = f'"{target_key}"'
        self._state = self._SCANNING
        self._buffer = ""       # accumulates chars for key matching
        self._escaped = False   # inside value: next char is escaped
        self._depth = 0         # JSON nesting depth (only match at top level)
        self._in_string = False # whether we're inside ANY string (for depth tracking)
        self._string_escape = False  # escape tracking for depth counting

    @property
    def done(self) -> bool:
        return self._state == self._DONE

    def feed(self, chunk: str) -> List[str]:
        """Feed a text chunk; return list of extracted value fragments (may be empty)."""
        if self._state == self._DONE:
            return []

        result: List[str] = []
        current_fragment: List[str] = []

        for ch in chunk:
            if self._state == self._SCANNING:
                self._track_depth(ch)
                self._buffer += ch
                # Only match target key at depth 1 (top-level object)
                if self._depth == 1 and self._buffer.endswith(self._target):
                    # Verify we're not inside a string value (the key itself is a string,
                    # but at this point we just exited that string)
                    self._state = self._AFTER_KEY
                    self._buffer = ""

            elif self._state == self._AFTER_KEY:
                # Expecting ':' (skip whitespace)
                if ch == ':':
                    self._state = self._AFTER_COLON
                elif not ch.isspace():
                    # Not a valid key:value pair, go back to scanning
                    self._state = self._SCANNING
                    self._buffer = ""

            elif self._state == self._AFTER_COLON:
                # Expecting '"' to start the string value (skip whitespace)
                if ch == '"':
                    self._state = self._IN_VALUE
                    self._escaped = False
                elif ch.isspace():
                    continue
                else:
                    # Value is not a string (number, null, etc.) — not our target
                    self._state = self._DONE

            elif self._state == self._IN_VALUE:
                if self._escaped:
                    # Handle escape sequences
                    self._escaped = False
                    if ch == 'n':
                        current_fragment.append('\n')
                    elif ch == 't':
                        current_fragment.append('\t')
                    elif ch == 'r':
                        current_fragment.append('\r')
                    elif ch == '"':
                        current_fragment.append('"')
                    elif ch == '\\':
                        current_fragment.append('\\')
                    elif ch == '/':
                        current_fragment.append('/')
                    elif ch == 'u':
                        # Unicode escape — simplified: emit \u literally,
                        # the next 4 chars will be appended as-is which is
                        # acceptable for UI display purposes
                        current_fragment.append('\\u')
                    else:
                        current_fragment.append(ch)
                elif ch == '\\':
                    self._escaped = True
                elif ch == '"':
                    # Defer the end-of-value decision: an unescaped `"` may
                    # also be a stray literal the LLM forgot to escape (e.g.
                    # `"UTC 是"零时区"时间"`). Confirm by looking at the next
                    # non-whitespace character — only `,` or `}` end the value.
                    self._state = self._AT_POSSIBLE_END
                else:
                    current_fragment.append(ch)

            elif self._state == self._AT_POSSIBLE_END:
                if ch.isspace():
                    continue
                if ch == ',' or ch == '}':
                    # Real end-of-value.
                    if current_fragment:
                        result.append("".join(current_fragment))
                    self._state = self._DONE
                    return result
                # Stray unescaped quote inside value — flush the deferred `"`
                # as a literal and reprocess this char in _IN_VALUE.
                current_fragment.append('"')
                self._state = self._IN_VALUE
                if ch == '\\':
                    self._escaped = True
                elif ch == '"':
                    # Two quotes back-to-back — defer the second one too.
                    self._state = self._AT_POSSIBLE_END
                else:
                    current_fragment.append(ch)

        # Flush any accumulated fragment from this chunk
        if current_fragment:
            result.append("".join(current_fragment))

        return result

    def _track_depth(self, ch: str) -> None:
        """Track JSON nesting depth, ignoring characters inside strings."""
        if self._in_string:
            if self._string_escape:
                self._string_escape = False
            elif ch == '\\':
                self._string_escape = True
            elif ch == '"':
                self._in_string = False
        else:
            if ch == '"':
                self._in_string = True
            elif ch == '{' or ch == '[':
                self._depth += 1
            elif ch == '}' or ch == ']':
                self._depth -= 1
