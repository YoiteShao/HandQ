"""
Probe: send a minimal prompt to each configured Anthropic model with
max_tokens set to the per-model resolved ceiling. Goal is to verify that
the API does NOT reject the request as "max_tokens too high" — we don't
need the model to actually generate that many tokens.

Run: py probe_max_tokens.py
"""
import asyncio
import sys
import time
import yaml

sys.path.insert(0, ".")

from src.infrastructure.anthropic_streaming_service import (
    AnthropicStreamingService,
    StreamDoneEvent,
    StreamTextDeltaEvent,
    StreamToolCallEvent,
    _resolve_max_tokens,
)


async def probe_one(api_key: str, model: str) -> tuple[str, str]:
    """Return (verdict, detail). verdict in {OK, FAIL, ERR}."""
    ceiling = _resolve_max_tokens(model, None)
    svc = AnthropicStreamingService(
        api_key=api_key,
        model=model,
        max_tokens=ceiling,
        max_retries=1,
        timeout=60,
    )
    try:
        # Minimal prompt that will generate ~3-5 tokens; max_tokens is set to
        # the ceiling we want to validate. If the API rejects max_tokens, the
        # exception fires before any stream event arrives.
        gen = svc.chat_stream(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=ceiling,
            temperature=0.0,
        )
        first_event_seen = False
        out_tokens = 0
        async for event in gen:
            first_event_seen = True
            if isinstance(event, StreamDoneEvent):
                out_tokens = event.result.output_tokens
                return ("OK", f"ceiling={ceiling} accepted; generated {out_tokens} tokens")
        # No StreamDoneEvent: shouldn't happen, but treat as fail for safety
        if first_event_seen:
            return ("OK", f"ceiling={ceiling} accepted (no done event)")
        return ("FAIL", f"ceiling={ceiling} produced no events")
    except Exception as e:
        # Distinguish "max_tokens too high" rejections from generic errors
        msg = str(e)
        msg_low = msg.lower()
        if "max_tokens" in msg_low or "max tokens" in msg_low:
            return ("FAIL", f"ceiling={ceiling} REJECTED: {type(e).__name__}: {msg[:200]}")
        return ("ERR", f"ceiling={ceiling} other error: {type(e).__name__}: {msg[:200]}")
    finally:
        await svc.close()


async def main():
    with open("handq_config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    api_key = cfg["llm"]["API_KEY"]
    models = cfg["llm"]["models"]

    print(f"Probing {len(models)} models with API key (len={len(api_key)})\n")
    print(f"{'model':<42} {'ceiling':>8}  {'verdict':<6}  detail")
    print("-" * 130)

    results = []
    for m in models:
        ceiling = _resolve_max_tokens(m, None)
        t0 = time.time()
        try:
            verdict, detail = await probe_one(api_key, m)
        except Exception as e:
            verdict, detail = "ERR", f"probe crashed: {type(e).__name__}: {e}"
        elapsed = time.time() - t0
        print(f"{m:<42} {ceiling:>8}  {verdict:<6}  ({elapsed:.1f}s) {detail}")
        results.append((m, ceiling, verdict, detail))

    print()
    n_ok = sum(1 for _, _, v, _ in results if v == "OK")
    n_fail = sum(1 for _, _, v, _ in results if v == "FAIL")
    n_err = sum(1 for _, _, v, _ in results if v == "ERR")
    print(f"Summary: {n_ok} OK, {n_fail} FAIL (max_tokens rejected), {n_err} other errors")


if __name__ == "__main__":
    asyncio.run(main())
