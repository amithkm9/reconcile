"""The single door to a language model.

Rules this module enforces for every call:
  - structured output only (`messages.parse` with a pydantic schema), never regex over prose;
  - reproducibility from a record/replay cache keyed by (model, prompts, schema) in llm_cache/:
    once a run with a key has filled it, evals replay for free and without a key (no `temperature`
    is set; the cache, not sampling settings, is what makes reruns identical);
  - the caller sends profiles and masked samples, never raw exports (see mapping/profile.py);
  - every call is logged (tokens, latency, cost, cache hit) to reports/llm_calls.jsonl;
  - any failure (no key, refusal, truncation, API error) returns None and the caller falls back
    to its deterministic path. The pipeline never depends on a model being reachable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache
from typing import TypeVar

import yaml
from pydantic import BaseModel

from reconcile.paths import REPORTS, ROOT

CACHE = ROOT / "llm_cache"
LOG = REPORTS / "llm_calls.jsonl"
T = TypeVar("T", bound=BaseModel)


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "llm.yaml").read_text(encoding="utf-8"))


def configured_models() -> tuple[str, ...]:
    """The models with a price in config/llm.yaml: the ones the mapping eval compares."""
    return tuple(_cfg()["prices"])


@lru_cache(maxsize=1)
def _warn_no_key() -> None:
    print(
        "[llm] ANTHROPIC_API_KEY not set and no cached response: using the deterministic fallback"
    )


def _log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def cost_usd(model: str, usage: dict) -> float:
    p = _cfg()["prices"].get(model)
    if not p:
        return 0.0
    return (
        usage.get("input_tokens", 0) / 1e6 * p["input"]
        + usage.get("output_tokens", 0) / 1e6 * p["output"]
    )


def structured_call(
    system: str, user: str, schema: type[T], model: str | None = None, purpose: str = ""
) -> tuple[T | None, dict]:
    """Returns (parsed object or None, call record)."""
    model = model or os.environ.get("LLM_MODEL") or _cfg()["default_model"]
    key = hashlib.sha256(
        json.dumps([model, system, user, schema.model_json_schema()], sort_keys=True).encode()
    ).hexdigest()[:24]
    path = CACHE / f"{key}.json"
    if path.exists():
        rec = json.loads(path.read_text())
        _log({"purpose": purpose, "model": model, "cache": "hit", **rec["usage"], "latency_s": 0.0})
        return schema.model_validate(rec["output"]), rec | {"cache": "hit"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _warn_no_key()
        return None, {"cache": "miss", "skipped": "no_api_key", "model": model}
    import anthropic

    client = anthropic.Anthropic()
    t0 = time.time()
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=_cfg()["max_tokens"],
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except (
        anthropic.APIError
    ) as e:  # rate limit, overload, bad request: fall back, don't crash onboarding
        _log(
            {"purpose": purpose, "model": model, "error": type(e).__name__, "message": str(e)[:300]}
        )
        return None, {"error": type(e).__name__, "model": model}
    latency = time.time() - t0
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    rec = {
        "model": model,
        "stop_reason": resp.stop_reason,
        "usage": usage,
        "latency_s": round(latency, 2),
        "cost_usd": round(cost_usd(model, usage), 5),
    }
    _log({"purpose": purpose, "cache": "miss", **rec})
    if (
        resp.stop_reason != "end_turn" or resp.parsed_output is None
    ):  # refusal / max_tokens: don't trust it
        return None, rec
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(rec | {"output": resp.parsed_output.model_dump()}, indent=1))
    return resp.parsed_output, rec
