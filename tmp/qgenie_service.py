"""
QGenie LLM Service — Concrete Adapter
======================================
Implements :class:`src.infrastructure.llm_service.LLMService` using
``QGenieAsyncClient``.

This module is the *only* place in the codebase that imports from the
``qgenie`` SDK.  All other modules depend solely on the abstract
:class:`~src.infrastructure.llm_service.LLMService` interface, keeping the
rest of the application fully decoupled from the QGenie SDK.

This file is named ``qgenie_service.py`` (not ``qgenie.py``) so that it does
not shadow the installed ``qgenie-sdk`` package when the project root is on
``sys.path``.

Other provider adapters (OpenAI, Anthropic, Grok, Qwen) follow the same
pattern — subclass :class:`LLMService` and implement ``chat()`` + ``close()``.
"""
import asyncio
from typing import Any, AsyncGenerator, Literal, Optional, Union, cast

from qgenie import QGenieAsyncClient, QGenieAPIException  # pyright: ignore[reportMissingImports]
from qgenie.resources.chat_completions import (  # pyright: ignore[reportMissingImports]
    ChatCompletionResponse,
    ChatCompletionStreamResponse,
)
from qgenie.resources.common import ResponseFormat, StreamOptions  # pyright: ignore[reportMissingImports]

from src.infrastructure.llm_service import LLMChatResult, LLMService, ToolCallInfo
from src.infrastructure.anthropic_streaming_service import (
    StreamDoneEvent,
    StreamTextDeltaEvent,
    StreamToolCallEvent,
)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class LLMTokenLimitError(Exception):
    """Raised when the LLM truncates its response because the output hit the
    configured token limit (``finish_reason == 'length'``).

    This typically means ``max_tokens`` in the LLM config is too small for the
    requested task.  Increase ``config.llm.max_tokens`` in
    ``./handq_config.yaml`` and retry.
    """



class QGenieLLMService(LLMService):
    """Concrete LLM adapter that wraps ``QGenieAsyncClient``.

    Translates the provider-agnostic :class:`LLMService` interface into
    QGenie API calls, including:

    * ``json_mode``     → ``ResponseFormat(type="json_object")``
    * ``first_content`` → extracts ``response.first_content`` as ``str``
    * Exponential-backoff retry on transient failures
    * Immediate re-raise for prompt-too-long and streaming errors

    Parameters
    ----------
    endpoint:
        QGenie API endpoint URL.  Defaults to the SDK's built-in default.
    api_key:
        QGenie API key.
    model:
        Default model identifier, e.g. ``"anthropic::claude-4-5-sonnet"``
        or ``"Llama-3.1-70B-Instruct"``.
    max_retries:
        Maximum retry attempts for transient failures (default: 3).
    timeout:
        Per-request timeout in seconds (default: 120).
    max_tokens:
        Default maximum tokens to generate (default: 10 240).
    temperature:
        Default sampling temperature (default: 0.7).
    verify:
        SSL certificate verification — ``True``/``False`` or path to a CA
        bundle (default: ``False``).
    max_concurrent_requests:
        Concurrency limit for the underlying HTTP connection pool
        (default: 64).
    proxy:
        Optional HTTP/HTTPS proxy URL.
    debug:
        Enable verbose debug logging inside the QGenie SDK (default: ``False``).
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = "Llama-3.1-70B-Instruct",
        max_retries: int = 2,
        timeout: int = 600,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
        verify: Union[bool, str] = False,
        max_concurrent_requests: int = 5,
        proxy: Optional[str] = None,
        debug: bool = False,
    ) -> None:
        super().__init__(
            model=model,
            temperature=temperature,
            max_retries=max_retries,
        )
        self._client = QGenieAsyncClient(
            endpoint=endpoint,
            api_key=api_key,
            max_retries=max_retries,
            timeout=timeout,
            verify=verify,
            max_concurrent_requests=max_concurrent_requests,
            proxy=proxy,
            debug=debug,
        )
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # LLMService interface implementation
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[Any],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
        json_mode: bool = True,
        first_content: bool = True,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[Literal["low", "medium", "high"]] = None,
        thinking_budget_tokens: Optional[int] = None,
        tools: Optional[list[dict[str, str | dict[str, Any]]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[ResponseFormat] = None,
        stream_options: Optional[StreamOptions] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Open a streaming request and yield unified stream events.

        Yields ``StreamTextDeltaEvent``, ``StreamToolCallEvent``, and a
        final ``StreamDoneEvent`` carrying the full ``LLMChatResult``.

        Retries transient errors up to ``max_retries`` times.  PTL and 4xx
        client errors are raised immediately without retrying.
        """
        model_name = model       if model       is not None else self.model
        temp       = temperature if temperature is not None else self.temperature
        max_tok    = max_tokens  if max_tokens  is not None else self.max_tokens

        effective_response_format = response_format
        if json_mode and effective_response_format is None:
            effective_response_format = ResponseFormat(type="json_object")

        self.logger.debug(
            f"LLM request: model={model_name}, messages={len(messages)}, "
            f"json_mode={json_mode}",
            component="LLMService",
        )

        # Sanitize messages: replace None content with "" to avoid SDK validation errors.
        sanitized_messages = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("content") is None:
                msg = {**msg, "content": ""}
            sanitized_messages.append(msg)

        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = await self._client.chat(
                    messages=sanitized_messages,
                    model=model_name,
                    temperature=temp,
                    max_tokens=max_tok,
                    stop=stop,
                    frequency_penalty=frequency_penalty,
                    presence_penalty=presence_penalty,
                    repetition_penalty=repetition_penalty,
                    top_k=top_k,
                    top_p=top_p,
                    reasoning_effort=reasoning_effort,
                    tools=tools,
                    response_format=effective_response_format,
                    stream=False,
                    stream_options=stream_options,
                    **kwargs,
                )

                chat_response = cast(ChatCompletionResponse, response)
                self.logger.debug(f"LLM return response{str(response)}")

                choice = chat_response.choices[0] if chat_response.choices else None

                if choice is not None and getattr(choice, "finish_reason", None) == "length":
                    raise LLMTokenLimitError(
                        "LLM server response was truncated due to token limit "
                    )

                message = choice.message if choice else None
                content_val = message.content if message else None
                reasoning_val = (
                    getattr(message, "reasoning_content", None) if message else None
                )
                tool_name_val = tool_args_val = None
                all_tool_calls: list[ToolCallInfo] = []
                if message and message.tool_calls:
                    for i, tc in enumerate(message.tool_calls):
                        call_id = getattr(tc, "id", None) or f"call_{i}"
                        all_tool_calls.append(ToolCallInfo(
                            call_id=call_id,
                            tool_name=tc.function.name,
                            tool_arguments=tc.function.arguments,
                        ))
                    tool_name_val = all_tool_calls[0].tool_name
                    tool_args_val = all_tool_calls[0].tool_arguments

                total_tokens_val = (
                    getattr(chat_response.usage, "total_tokens", 0)
                    if chat_response.usage
                    else 0
                )
                self.logger.debug(
                    f"LLM response: reasoning_val={reasoning_val}, tool={tool_name_val}, total_tokens={total_tokens_val}",
                    component="LLMService",
                )

                # Yield tool call events before the done event
                for tc in all_tool_calls:
                    import json as _json
                    try:
                        args = _json.loads(tc.tool_arguments or "{}")
                    except Exception:
                        args = {}
                    yield StreamToolCallEvent(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        args=args,
                        block_index=0,
                    )

                # Yield text delta if there is text content
                if content_val:
                    yield StreamTextDeltaEvent(text=content_val)

                yield StreamDoneEvent(result=LLMChatResult(
                    content=content_val,
                    reasoning_content=reasoning_val,
                    tool_name=tool_name_val,
                    tool_arguments=tool_args_val,
                    tool_calls=all_tool_calls,
                    total_tokens=total_tokens_val,
                ))
                return

            except Exception as e:
                last_error = e
                if self._is_prompt_too_long_error(e):
                    self.logger.error(
                        f"LLM request failed (prompt too long): {e}",
                        component="LLMService",
                    )
                    raise

                if (
                    isinstance(e, QGenieAPIException)
                    and e.http_status is not None
                    and 400 <= e.http_status < 500
                ):
                    self.logger.error(
                        f"LLM request failed with 4xx client error "
                        f"(http_status={e.http_status}), not retrying: {e}",
                        component="LLMService",
                    )
                    raise

                if attempt < self.max_retries - 1:
                    wait_time = min(600, 2 ** (attempt + 1))
                    self.logger.warning(
                        f"LLM request failed (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {wait_time}s: {e}",
                        component="LLMService",
                    )
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"LLM request failed after {self.max_retries} retries: {e}",
                        component="LLMService",
                    )

        assert last_error is not None
        if "Unexpected exception (ReadTimeout)" in str(last_error):
            self.logger.error(
                f"LLM request failed (ReadTimeout): {last_error}",
                component="LLMService",
            )
            raise Exception("Client error: Too many tokens in one minute")
        raise last_error

    async def close(self) -> None:
        """Close the underlying QGenie client connection."""
        await self._client.close()


async def main() -> None:
    """Direct smoke-test: call ``QGenieAsyncClient.chat()`` with no retry layer.

    Config is loaded from ``myconfig.yaml`` (same directory as this file).
    Any exception from the client propagates unhandled — full traceback shown.
    """
    import pathlib
    import yaml  # pip install pyyaml

    cfg_path = pathlib.Path(__file__).with_name("myconfig.yaml")
    cfg      = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    llm_cfg  = cfg.get("llm", {})

    api_key = llm_cfg.get("API_KEY")
    model   = "anthropic::claude-4-6-sonnet"

    client = QGenieAsyncClient(api_key=api_key, verify=False, debug=True,timeout=600)

    with open("test.txt", "r", encoding="utf-8") as f:
        user_input = f.read()
    messages   = [{"role": "user", "content": user_input}]
    response = await client.chat(messages=messages, model=model,)
    print(response)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
