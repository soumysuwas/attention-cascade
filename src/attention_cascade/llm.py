"""The single metered gateway to Vertex AI. Every model call in this project goes through here.

Does: call Gemini on Vertex, record real token usage (including thinking tokens) into the
blackboard, cache every response to disk, and replay from cache with no network.
Does not: decide anything. It has no opinion about tiers, escalation, or importance.
Exists because: the deliverable is a measured cost table, and a cost table built on estimated
token counts is worthless. Rule 3 in CLAUDE.md lives here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import config as C
from .blackboard import Blackboard
from .models import LLMCall

_client: Any = None


def client() -> Any:
    """Lazily build the Vertex client. Import is local so replay mode works without the SDK."""
    global _client
    if _client is None:
        from google import genai  # noqa: PLC0415

        if not C.GCP_PROJECT:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in, "
                "or run with AC_LLM_MODE=replay to use the committed cache."
            )
        _client = genai.Client(vertexai=True, project=C.GCP_PROJECT, location=C.GCP_LOCATION)
    return _client


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0
    cached_input_tokens: int = 0
    from_cache: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0
    long_context: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the response as JSON, tolerating fenced or prose-wrapped output."""
        return parse_json(self.text)


def parse_json(text: str) -> Any:
    """Salvage JSON from a model response. Raises ValueError if nothing parses."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = t.find(opener), t.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"could not parse JSON from response of length {len(text)}")


def _cache_key(model: str, system: str, contents: Any, cfg: dict[str, Any]) -> str:
    blob = json.dumps(
        {"model": model, "system": system, "contents": contents, "cfg": cfg},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _read_cache(key: str) -> dict[str, Any] | None:
    path = C.CACHE_DIR / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def _write_cache(key: str, payload: dict[str, Any]) -> None:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (C.CACHE_DIR / f"{key}.json").write_text(json.dumps(payload, indent=2, default=str))


def _usage(resp: Any) -> tuple[int, int, int, int]:
    """Pull real token counts off the response. Missing fields default to 0, never estimated."""
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return 0, 0, 0, 0
    return (
        int(getattr(u, "prompt_token_count", 0) or 0),
        int(getattr(u, "candidates_token_count", 0) or 0),
        int(getattr(u, "thoughts_token_count", 0) or 0),
        int(getattr(u, "cached_content_token_count", 0) or 0),
    )


async def call(
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    tier: str,
    run_id: str,
    bb: Blackboard,
    thinking_budget: int = 0,
    json_output: bool = True,
    temperature: float = 0.0,
) -> LLMResult:
    """One metered, cached Vertex call.

    AC_LLM_MODE:
      auto   - serve from cache when present, otherwise call and populate the cache
      live   - always call, still writes cache
      replay - cache only; a miss raises, so a demo never silently goes to the network
    """
    cfg = {
        "max_output_tokens": max_tokens,
        "temperature": temperature,
        "thinking_budget": thinking_budget,
        "json": json_output,
    }
    key = _cache_key(model, system, prompt, cfg)
    mode = C.LLM_MODE

    if mode in ("auto", "replay"):
        hit = _read_cache(key)
        if hit:
            res = LLMResult(
                text=hit["text"], model=model,
                input_tokens=hit["input_tokens"], output_tokens=hit["output_tokens"],
                thoughts_tokens=hit.get("thoughts_tokens", 0),
                cached_input_tokens=hit.get("cached_input_tokens", 0),
                from_cache=True, latency_ms=hit.get("latency_ms", 0),
                cost_usd=hit["cost_usd"], long_context=hit.get("long_context", False),
            )
            _record(bb, res, tier, run_id)
            return res
        if mode == "replay":
            raise RuntimeError(
                f"cache miss in replay mode (tier={tier}, key={key}). "
                "Rebuild the cache with AC_LLM_MODE=live before demoing offline."
            )

    from google.genai import types  # noqa: PLC0415

    gen_cfg: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_output:
        gen_cfg["response_mime_type"] = "application/json"
    if thinking_budget is not None:
        gen_cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

    last_err: Exception | None = None
    for attempt in range(C.LLM_MAX_RETRIES):
        started = time.perf_counter()
        try:
            resp = await client().aio.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(**gen_cfg),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            in_tok, out_tok, think_tok, cached_tok = _usage(resp)
            cost = C.price_call(model, in_tok, out_tok, think_tok, cached_tok)
            res = LLMResult(
                text=resp.text or "", model=model,
                input_tokens=in_tok, output_tokens=out_tok, thoughts_tokens=think_tok,
                cached_input_tokens=cached_tok, from_cache=False, latency_ms=latency_ms,
                cost_usd=cost, long_context=C.crossed_long_context(model, in_tok),
            )
            _write_cache(key, {
                "text": res.text, "model": model, "tier": tier,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "thoughts_tokens": think_tok, "cached_input_tokens": cached_tok,
                "latency_ms": latency_ms, "cost_usd": cost, "long_context": res.long_context,
            })
            if res.long_context:
                bb.audit(f"llm:{tier}", "LONG_CONTEXT_CLIFF",
                         {"model": model, "input_tokens": in_tok})
            _record(bb, res, tier, run_id)
            return res
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced and audited
            last_err = exc
            bb.audit(f"llm:{tier}", "CALL_FAILED",
                     {"attempt": attempt + 1, "model": model, "error": str(exc)[:400]})
            if attempt < C.LLM_MAX_RETRIES - 1:
                await asyncio.sleep(C.LLM_BACKOFF_BASE_S * (2**attempt) + random.random() * 0.3)

    raise RuntimeError(f"{tier} call failed after {C.LLM_MAX_RETRIES} attempts: {last_err}")


def _record(bb: Blackboard, res: LLMResult, tier: str, run_id: str) -> None:
    """Every call - cached or live - lands in llm_calls. Cached calls carry from_cache=True
    so the report can show both 'cost as measured live' and 'this run was a replay'."""
    bb.write_llm_call(LLMCall(
        run_id=run_id, tier=tier, model=res.model,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens, thinking_tokens=res.thoughts_tokens,
        cached_input_tokens=res.cached_input_tokens,
        from_cache=res.from_cache, latency_ms=res.latency_ms,
        cost_usd=res.cost_usd, ts=datetime.now(UTC),
    ))
    bb.audit(f"llm:{tier}", "CALL", {
        "model": res.model, "in": res.input_tokens, "out": res.output_tokens,
        "thinking": res.thoughts_tokens, "cached_in": res.cached_input_tokens,
        "cost_usd": round(res.cost_usd, 6), "from_cache": res.from_cache,
        "latency_ms": res.latency_ms,
    })


def list_available_models() -> list[str]:
    """Used by `ac verify --list-models`. Confirms the ids in config.py exist in your region."""
    return sorted(m.name for m in client().models.list())
