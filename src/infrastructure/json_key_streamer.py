"""
JsonKeyStreamer — incremental JSON value extractor for streaming LLM output.

Tracks a specific top-level JSON key (default: "response_to_user") across
streamed text chunks and emits the *unescaped* string content of that value
as it arrives.

JsonNextStepsArrayStreamer — incremental array-of-objects extractor for
streaming planner output. Emits each step's raw JSON text as the closing
brace arrives, so the caller can dispatch the first batch before the full
response is available.
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


class JsonNextStepsArrayStreamer:
    """Incrementally yield each complete object inside a top-level JSON array.

    Watches for a target top-level key (default ``"next_steps"``) whose
    value is a JSON array of objects. As each object's matching closing
    brace is consumed, ``feed()`` returns its raw JSON text. The caller
    can then ``json.loads`` the text and construct the corresponding
    domain object.

    Usage::

        streamer = JsonNextStepsArrayStreamer("next_steps")
        for chunk in llm_stream:
            for obj_text in streamer.feed(chunk):
                step = parse_step(obj_text)
                ...
            if streamer.array_finished or streamer.aborted:
                break

    The streamer is best-effort. ``aborted`` is set when the stream's
    structure violates expectations (e.g. ``next_steps`` value is not an
    array, or a non-``{`` token appears between objects); callers should
    fall back to full-stream parsing in that case.
    """

    _SCANNING = 0       # looking for the target key at depth 1
    _AFTER_KEY = 1      # found key, expecting ':'
    _AFTER_COLON = 2    # found ':', expecting '['
    _IN_ARRAY = 3       # inside the array, between objects
    _IN_OBJECT = 4      # accumulating an object's text
    _DONE = 5           # array finished or aborted

    def __init__(self, target_key: str = "next_steps") -> None:
        self._target = f'"{target_key}"'
        self._state = self._SCANNING
        # Top-level depth tracking (used only in SCANNING).
        self._buffer = ""
        self._depth = 0
        self._scan_in_string = False
        self._scan_string_escape = False
        # Per-object tracking (used in IN_OBJECT).
        self._obj_buf: List[str] = []
        self._obj_depth = 0
        self._obj_in_string = False
        self._obj_string_escape = False
        # Outcome flags.
        self._array_finished = False
        self._aborted = False

    @property
    def array_finished(self) -> bool:
        """True once the closing ``]`` of the target array has been consumed."""
        return self._array_finished

    @property
    def aborted(self) -> bool:
        """True when the stream's structure violated expectations and the streamer gave up."""
        return self._aborted

    @property
    def done(self) -> bool:
        """True when no further objects can be produced (array finished or aborted)."""
        return self._state == self._DONE

    def feed(self, chunk: str) -> List[str]:
        """Feed a text chunk; return a list of newly-completed object JSON texts.

        Most chunks return an empty list. When a closing brace lands inside
        the chunk, the corresponding step's complete JSON text is appended
        to the result.
        """
        if self._state == self._DONE:
            return []
        result: List[str] = []
        for ch in chunk:
            if self._state == self._DONE:
                break
            if self._state == self._SCANNING:
                self._scan_track_depth(ch)
                self._buffer += ch
                # Match only at depth 1 and not while inside a string value;
                # this avoids false positives if a string content happens to
                # contain the literal substring "next_steps".
                if (self._depth == 1
                        and not self._scan_in_string
                        and self._buffer.endswith(self._target)):
                    self._state = self._AFTER_KEY
                    self._buffer = ""
            elif self._state == self._AFTER_KEY:
                if ch == ':':
                    self._state = self._AFTER_COLON
                elif not ch.isspace():
                    # Substring match was not actually a key — go back to scanning.
                    self._state = self._SCANNING
                    self._buffer = ch
            elif self._state == self._AFTER_COLON:
                if ch == '[':
                    self._state = self._IN_ARRAY
                elif ch.isspace():
                    continue
                else:
                    # Value is not an array.
                    self._aborted = True
                    self._state = self._DONE
            elif self._state == self._IN_ARRAY:
                if ch == '{':
                    self._obj_buf = ['{']
                    self._obj_depth = 1
                    self._obj_in_string = False
                    self._obj_string_escape = False
                    self._state = self._IN_OBJECT
                elif ch == ']':
                    self._array_finished = True
                    self._state = self._DONE
                elif ch == ',' or ch.isspace():
                    continue
                else:
                    # Unexpected character between objects.
                    self._aborted = True
                    self._state = self._DONE
            elif self._state == self._IN_OBJECT:
                self._obj_buf.append(ch)
                if self._obj_in_string:
                    if self._obj_string_escape:
                        self._obj_string_escape = False
                    elif ch == '\\':
                        self._obj_string_escape = True
                    elif ch == '"':
                        self._obj_in_string = False
                else:
                    if ch == '"':
                        self._obj_in_string = True
                    elif ch == '{':
                        self._obj_depth += 1
                    elif ch == '}':
                        self._obj_depth -= 1
                        if self._obj_depth == 0:
                            result.append("".join(self._obj_buf))
                            self._obj_buf = []
                            self._state = self._IN_ARRAY
        return result

    def _scan_track_depth(self, ch: str) -> None:
        """Top-level depth tracking, ignoring chars inside strings."""
        if self._scan_in_string:
            if self._scan_string_escape:
                self._scan_string_escape = False
            elif ch == '\\':
                self._scan_string_escape = True
            elif ch == '"':
                self._scan_in_string = False
        else:
            if ch == '"':
                self._scan_in_string = True
            elif ch == '{' or ch == '[':
                self._depth += 1
            elif ch == '}' or ch == ']':
                self._depth -= 1
