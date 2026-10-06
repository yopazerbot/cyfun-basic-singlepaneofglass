"""Calls to the Anthropic API (official Python SDK): one review, batch submission and results, key test, cost.

One review is a single Messages API request with adaptive thinking, the effort from Settings
and a JSON schema as output format. Single requests opt into server-side refusal fallbacks
(`fallbacks: "default"`): when the model's safety classifier declines, the API re-runs the
request on the model Anthropic recommends for that category, and `served_model` records it.
The Batches API does not accept fallbacks; a declined batch request ends as "declined".
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass

import anthropic

from .prompt import SCHEMA

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 300.0

# USD per million tokens: input, output, cache write (5 minutes), cache read. List prices; batches cost half.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
}
OUTPUT_ESTIMATE = {"low": 1500, "medium": 3000, "high": 6000}  # thinking plus answer, for spend estimates


class ApiError(Exception):
    """A failed call, with a message for the administrator."""


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class Reply:
    data: dict | None
    raw_text: str
    stop_reason: str
    refusal: str
    served_model: str
    usage: Usage
    request_id: str


def client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=2)


def params(model: str, effort: str, system: str, content: list[dict]) -> dict:
    """Request body shared by single reviews and batch entries."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": content}],
    }


def friendly(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check it on the Settings page."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The Anthropic API key has no access to this model or feature."
    if isinstance(exc, anthropic.NotFoundError):
        return "The model was not found for this API key."
    if isinstance(exc, anthropic.RateLimitError):
        return "The Anthropic API rate limit was reached. Try again in a few minutes."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The Anthropic API refused the request: {exc.message}"[:500]
    if isinstance(exc, anthropic.APIStatusError):
        return f"The Anthropic API returned HTTP {exc.status_code}. Try again later."
    if isinstance(exc, anthropic.APITimeoutError):
        return "The request to the Anthropic API timed out."
    if isinstance(exc, anthropic.APIConnectionError):
        return "No connection to api.anthropic.com. Check outbound HTTPS from the server."
    return str(exc)[:300]


@contextlib.contextmanager
def _api() -> Iterator[None]:
    """Turn SDK errors into ApiError with a message for the administrator."""
    try:
        yield
    except anthropic.AnthropicError as exc:
        raise ApiError(friendly(exc)) from exc


def reply_from(message) -> Reply:
    stop = message.stop_reason or ""
    refusal = ""
    if stop == "refusal":
        refusal = getattr(getattr(message, "stop_details", None), "category", None) or "unspecified"
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    data = None
    if stop != "refusal" and text:
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(text)
    u = message.usage
    usage = Usage(
        getattr(u, "input_tokens", 0) or 0,
        getattr(u, "output_tokens", 0) or 0,
        getattr(u, "cache_read_input_tokens", 0) or 0,
        getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    return Reply(data if isinstance(data, dict) else None, text, stop, refusal, message.model or "", usage, getattr(message, "_request_id", "") or "")


def review(c: anthropic.Anthropic, body: dict) -> Reply:
    with _api():
        message = c.beta.messages.create(**body, betas=[FALLBACK_BETA], fallbacks="default")
    return reply_from(message)


def cost(requested_model: str, served_model: str, usage: Usage, batch: bool = False) -> float:
    price = PRICES.get(served_model) or PRICES.get(requested_model) or PRICES["claude-opus-5-5"]
    p_in, p_out, p_write, p_read = price
    usd = (usage.input * p_in + usage.output * p_out + usage.cache_write * p_write + usage.cache_read * p_read) / 1_000_000
    return usd / 2 if batch else usd


def estimate(model: str, effort: str, input_chars: int, binary_bytes: int = 0, images: int = 0, batch: bool = False) -> float:
    """Rough upper estimate in USD, used for the spend limit before anything is sent."""
    tokens_in = input_chars / 3 + binary_bytes / 1024 * 50 + images * 1600
    usage = Usage(input=int(tokens_in), output=OUTPUT_ESTIMATE.get(effort, 3000))
    return cost(model, model, usage, batch)


def test_key(api_key: str, model: str) -> str:
    with _api():
        info = client(api_key).models.retrieve(model)
    return f"The key works and {getattr(info, 'display_name', '') or model} is available."


def submit_batch(c: anthropic.Anthropic, entries: list[tuple[str, dict]]) -> str:
    with _api():
        return c.messages.batches.create(requests=[{"custom_id": cid, "params": body} for cid, body in entries]).id


def batch_status(c: anthropic.Anthropic, batch_id: str) -> str:
    with _api():
        return c.messages.batches.retrieve(batch_id).processing_status


def batch_results(c: anthropic.Anthropic, batch_id: str) -> Iterator:
    with _api():
        yield from c.messages.batches.results(batch_id)


def cancel_batch(c: anthropic.Anthropic, batch_id: str) -> None:
    with _api():
        c.messages.batches.cancel(batch_id)
