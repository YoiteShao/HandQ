"""VisionClient — single-call image → text/JSON helper.

This is intentionally NOT an :class:`LLMService` subclass.  The vision use
cases (browser canvas reading, desktop find_element, periodic activity
OCR) all want one-shot ``image + question → answer`` semantics, not the
streaming / tool-call / fallback machinery that ``LLMService`` provides.
A focused 150-line helper is the right primitive; we can promote it to a
full ``LLMService`` later if and when fallback or tool-use is needed.

Configuration lives in ``handq_config.yaml`` under the ``vision:``
section::

    vision:
      endpoint: https://qgenie-api.qualcomm.com/v1
      api_key: ff6bb18f-...
      model: azure::gpt-5.4-mini
      timeout: 120
      verify_ssl: false
      max_image_dim: 1024

The client is a process-wide singleton fetched via
:func:`get_vision_client`.  First call lazily builds the underlying
``AsyncOpenAI`` + ``httpx.AsyncClient``; :func:`flush_vision_client`
closes both.  Modeled after :func:`browser_tool.flush_browser_pool`
so the bridge / flow controller has one consistent shutdown idiom.

Why ``verify_ssl=False`` by default: the QGenie gateway uses an internal
Qualcomm CA that is not in the public truststore.  This mirrors the
already-shipped pattern in ``scripts/vision_bench/test_vision_gpt.py``.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..logger import get_logger

# ── Optional deps ────────────────────────────────────────────────────────────
# We keep the imports lazy so ``import vision_client`` works on systems
# that have not yet installed openai / pillow / httpx — only the actual
# query path needs them.

_DEFAULT_SYSTEM_PROMPT = (
    "You are a visual UI grounding assistant. Given a screenshot and a "
    "question or instruction, answer concisely. When the request asks for "
    "coordinates or structured data, return ONLY a JSON object that matches "
    "the schema described in the user message. Do not wrap JSON in code "
    "fences. When the request is open-ended, answer in 1–3 sentences."
)


@dataclass
class VisionResult:
    """Output of a single :meth:`VisionClient.query`.

    ``answer`` is always populated with the raw model text.  When
    ``output_schema`` was provided and the response parsed as valid
    JSON, ``parsed_json`` carries the structured form; otherwise it
    is None and the caller can decide whether to retry / repair.
    """
    answer: str
    parsed_json: Optional[Dict[str, Any]] = None
    image_dims: Tuple[int, int] = (0, 0)
    elapsed_ms: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    model: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class VisionClient:
    """Single-shot multimodal client for the QGenie OpenAI-compatible gateway.

    Not thread-safe by itself — the underlying ``AsyncOpenAI`` is built
    on httpx and is safe across awaits, but the singleton state (set by
    :func:`get_vision_client` / :func:`flush_vision_client`) assumes
    single-process use.  HandQ runs one bridge process per session, so
    that's fine.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        verify_ssl: bool = False,
        max_image_dim: int = 1024,
    ) -> None:
        if not endpoint or not api_key or not model:
            raise ValueError(
                "VisionClient requires endpoint, api_key and model "
                "(see vision: section in handq_config.yaml)"
            )
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_image_dim = int(max_image_dim) if max_image_dim else 1024
        self.logger = get_logger()

        # Built lazily on first query so import-time has no network /
        # SDK cost.
        self._http: Any = None
        self._client: Any = None

    # ── Lazy init ────────────────────────────────────────────────────────────

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "vision_client requires the openai package. Run:\n"
                "  pip install openai\n"
                f"Underlying: {exc}"
            )
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(
                "vision_client requires httpx (already pulled in by anthropic). "
                f"Underlying: {exc}"
            )
        # verify=False routes around the QGenie self-signed cert chain;
        # see test_vision_gpt.py for the same pattern.
        self._http = httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout)
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.endpoint,
            http_client=self._http,
            timeout=self.timeout,
        )

    # ── Image preparation ────────────────────────────────────────────────────

    def _load_and_resize(self, image_path: str) -> Tuple[bytes, Tuple[int, int]]:
        """Load *image_path*, resize the long edge to ``max_image_dim``, return PNG bytes.

        Bypasses the resize when the image already fits — saves the PIL
        encode round-trip on the small browser viewport screenshots that
        dominate Phase 1's traffic.
        """
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"image not found: {image_path}")
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                "vision_client requires Pillow for image resize. Run:\n"
                "  pip install pillow\n"
                f"Underlying: {exc}"
            )
        with Image.open(image_path) as img:
            img.load()
            w, h = img.size
            long_edge = max(w, h)
            if long_edge <= self.max_image_dim:
                # Re-encode as PNG anyway to normalise the on-wire format
                # (some screenshots come in JPEG; the gateway is more
                # consistent with PNG).
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG", optimize=True)
                return buf.getvalue(), (w, h)
            scale = self.max_image_dim / float(long_edge)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            resized = img.convert("RGB").resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), new_size

    # ── Core API ─────────────────────────────────────────────────────────────

    async def query(
        self,
        image_path: str,
        instruction: str,
        *,
        output_schema: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        max_tokens: int = 300,
        timeout: Optional[float] = None,
    ) -> VisionResult:
        """One-shot multimodal query.

        Catches its own exceptions and returns them via
        :attr:`VisionResult.error` so call sites do not need to wrap
        every invocation in try/except.

        ``output_schema`` is included in the user prompt as a guidance
        block; the gateway's ``response_format=json_object`` mode is
        used to keep the model honest about returning JSON.  We do not
        strictly schema-validate the response here — the caller can do
        that with ``jsonschema`` if it cares.
        """
        t0 = time.time()
        try:
            self._ensure_client()
        except Exception as exc:
            return VisionResult(answer="", model=self.model,
                                error=f"client init failed: {exc}")

        try:
            png_bytes, dims = self._load_and_resize(image_path)
        except Exception as exc:
            return VisionResult(answer="", model=self.model,
                                error=f"image preparation failed: {exc}")

        b64 = base64.b64encode(png_bytes).decode("ascii")
        sys_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

        user_text = instruction.strip()
        if dims and dims[0] > 0:
            user_text += f"\n\nImage dimensions: {dims[0]}x{dims[1]} pixels."
        if output_schema is not None:
            try:
                schema_str = json.dumps(output_schema, ensure_ascii=False)
            except Exception:
                schema_str = str(output_schema)
            user_text += (
                "\n\nRespond ONLY with a JSON object matching this schema:\n"
                + schema_str
            )

        request_kwargs: Dict[str, Any] = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                },
            ],
            temperature=0.0,
            max_tokens=int(max_tokens),
        )
        if output_schema is not None:
            request_kwargs["response_format"] = {"type": "json_object"}

        eff_timeout = timeout if timeout is not None else self.timeout
        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(**request_kwargs),
                timeout=eff_timeout,
            )
        except asyncio.TimeoutError:
            return VisionResult(
                answer="", model=self.model, image_dims=dims,
                elapsed_ms=int((time.time() - t0) * 1000),
                error=f"vision request timed out after {eff_timeout:.0f}s",
            )
        except Exception as exc:
            return VisionResult(
                answer="", model=self.model, image_dims=dims,
                elapsed_ms=int((time.time() - t0) * 1000),
                error=f"vision request failed: {exc}",
            )

        try:
            text = (resp.choices[0].message.content or "").strip()
        except Exception:
            text = ""

        # Token usage — best-effort, the gateway sometimes omits it.
        usage = getattr(resp, "usage", None)
        in_toks = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        out_toks = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

        parsed: Optional[Dict[str, Any]] = None
        if output_schema is not None and text:
            parsed = _try_parse_json_object(text)

        return VisionResult(
            answer=text,
            parsed_json=parsed,
            image_dims=dims,
            elapsed_ms=int((time.time() - t0) * 1000),
            tokens_input=in_toks,
            tokens_output=out_toks,
            model=self.model,
        )

    async def close(self) -> None:
        """Release the underlying httpx connection pool. Idempotent."""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception as exc:
                self.logger.debug(
                    f"vision client.close: aclose failed: {exc}",
                    component="VisionClient",
                )
        self._http = None
        self._client = None


# ── JSON repair helper ───────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _try_parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON parse.  Strips code fences, finds the first {...} block."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    m = _JSON_OBJECT_RE.search(s)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


# ── Process-wide singleton ───────────────────────────────────────────────────

_client: Optional[VisionClient] = None


def get_vision_client(config_manager: Any) -> VisionClient:
    """Return the process-wide :class:`VisionClient` singleton.

    Builds it on first call from the ``vision:`` section of
    ``handq_config.yaml`` via the supplied :class:`ConfigManager`.
    Subsequent calls return the same instance even if a different
    ConfigManager is passed — this matches the browser-pool pattern
    where a single user-data-dir lock means one process-wide handle.
    """
    global _client
    if _client is not None:
        return _client
    try:
        section = config_manager.get_section("vision") or {}
    except Exception as exc:
        raise RuntimeError(
            f"vision_client: cannot read 'vision:' section from config: {exc}"
        )
    if not section:
        raise RuntimeError(
            "vision_client: handq_config.yaml is missing the 'vision:' section. "
            "Add it with endpoint / api_key / model fields. See plan §1.1."
        )
    _client = VisionClient(
        endpoint=str(section.get("endpoint", "")).strip(),
        api_key=str(section.get("api_key", "")).strip(),
        model=str(section.get("model", "")).strip(),
        timeout=float(section.get("timeout", 120.0)),
        verify_ssl=bool(section.get("verify_ssl", False)),
        max_image_dim=int(section.get("max_image_dim", 1024)),
    )
    return _client


async def flush_vision_client() -> int:
    """Close the singleton (if any). Returns 1 if a client was closed, 0 otherwise.

    Mirrors :func:`browser_tool.flush_browser_pool` so the bridge has
    a single shutdown idiom.  Idempotent and best-effort.
    """
    global _client
    c = _client
    _client = None
    if c is None:
        return 0
    try:
        await c.close()
    except Exception:
        pass
    return 1
