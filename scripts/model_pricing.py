"""Price a captured call with the shared ``pydantic/genai-prices`` dataset.

This module is the only place that knows what a token costs. It used to be a
hand-maintained JSON table in this repository; every rate now comes from
``genai-prices``, which tracks published provider pricing (including historic
rates, dated price changes, off-peak windows and context tiers) for far more
models than a table kept by hand ever covered.

Three things the caller has to get right, and this module handles:

*   **Which provider.** The cache labels a call by the host it went to
    (``anthropic``, ``openai-codex``, ``github-copilot``, ...), which is a
    product, not a billing provider. ``PROVIDER_IDS`` maps the labels that map
    cleanly; anything else falls back to identifying the provider from the
    model string alone, which is a guess and is reported as one.
*   **What "input tokens" means.** Anthropic reports ``input_tokens``
    *excluding* cache reads and writes, OpenAI and Google count cached tokens
    *inside* their input total. ``genai-prices`` wants the total, with the
    cache counts as a breakdown of it, so the extractor records which
    convention a body used (``input_is_total``) and ``price_call`` adds the
    cache counts back only when they are not already included.
*   **When the call happened.** Rates are resolved against the call's own
    timestamp, never today's, so historical totals stay correct after a
    provider changes a price.

Reasoning tokens are deliberately not passed on. Providers disagree about
whether they are already inside the output count, and only a handful of models
price them separately, so feeding an ambiguous number in would risk moving a
cost figure on a guess.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    import genai_prices
except ImportError:  # pragma: no cover - depends on the install
    genai_prices = None


# Cache provider label -> genai-prices provider id, for the labels where the
# billing provider is known rather than inferred. A product that resells
# another vendor's models bills at that vendor's published rates, which is why
# Codex maps to OpenAI. Labels absent here (github-copilot, huggingface,
# augment, a bare hostname, unknown) have no single billing provider, so their
# calls are priced from the model string alone and flagged estimated.
PROVIDER_IDS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openai-codex": "openai",
    "google": "google",
    "groq": "groq",
    "mistral": "mistral",
    "deepseek": "deepseek",
    "xai": "x-ai",
    "openrouter": "openrouter",
}


@dataclass(frozen=True)
class PricedCall:
    """What pricing one call produced, as stored on the cache row.

    ``has_price = 0`` is the "nothing matched" outcome: cost is None and the
    call still appears in every usage total, only without a cost. That is the
    whole point of keeping it a row rather than dropping it.
    """

    has_price: int = 0
    cost_usd: float | None = None
    is_estimated: int = 0
    priced_provider: str = ""
    priced_model: str = ""
    price_effective_from: str = ""
    input_price_per_1m: float = 0.0
    output_price_per_1m: float = 0.0
    cached_price_per_1m: float = 0.0
    cache_write_price_per_1m: float = 0.0
    price_source: str = ""


UNPRICED = PricedCall()


def available() -> bool:
    """Whether pricing can run at all; False leaves every call unpriced."""
    return genai_prices is not None


def dataset_version() -> str:
    """Version token stored per row, so a package upgrade re-prices the cache.

    Rates live in the installed ``genai-prices`` release, so its version is
    exactly what decides whether a stored cost is still current.
    """
    if genai_prices is None:
        return ""
    return f"genai-prices/{genai_prices.__version__}"


def _decimal_to_float(value) -> float:
    """A price as a plain float, taking the base rate of a tiered price.

    Tiered rates (a higher price above a context threshold) are applied in full
    by the cost calculation; the base is what the rate columns report, because
    a single row cannot show a whole tier ladder.
    """
    if value is None:
        return 0.0
    base = getattr(value, "base", value)
    return float(base)


def _rate(model_price, price_key: str) -> float:
    return _decimal_to_float(getattr(model_price, price_key, None))


def _effective_from(model, when: datetime) -> str:
    """The start date of the dated price that applied, or '' when undated.

    ``genai-prices`` keeps a model's price history as a list of conditional
    prices; the last one whose constraint is satisfied wins. Only a start-date
    constraint names a date worth reporting -- an off-peak window repeats
    daily and is not a rate change.
    """
    prices = getattr(model, "prices", None)
    if not isinstance(prices, list):
        return ""
    for conditional in reversed(prices):
        constraint = conditional.constraint
        if constraint is None or constraint.active(when):
            start = getattr(constraint, "start_date", None)
            return start.isoformat() if start is not None else ""
    return ""


def _price_source(provider) -> str:
    urls = getattr(provider, "pricing_urls", None) or []
    reference = urls[0] if urls else provider.id
    return f"{dataset_version()} ({provider.id}): {reference}"


def billable_input_tokens(
    input_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    input_is_total: int,
) -> int:
    """Total input tokens, with cached and cache-write counts inside it.

    ``genai-prices`` treats cache reads and writes as partitions of the input
    total and prices the remainder at the plain input rate, so a provider that
    reports them alongside an input count that excludes them (Anthropic) needs
    them added back. The final ``max`` is a guard, not arithmetic: a row whose
    convention was recorded wrong would otherwise claim more cached tokens
    than input tokens, which the library rejects outright.
    """
    total = input_tokens if input_is_total else input_tokens + cached_tokens + cache_write_tokens
    return max(total, cached_tokens + cache_write_tokens)


def price_call(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    input_is_total: int,
    timestamp: datetime | None,
) -> PricedCall:
    """Price one call, or return UNPRICED when nothing matched it.

    Never raises: an unmatched model, an unknown provider or usage the library
    rejects all mean "no price for this call", which the dashboards count and
    report rather than failing a whole refresh over.
    """
    if genai_prices is None or not model:
        return UNPRICED

    provider_id = PROVIDER_IDS.get(provider)
    # No mapped provider means the model string is the only evidence of who
    # billed the call, so whatever it resolves to is a guess: priced, but
    # never counted as confirmed spend.
    is_estimated = 0 if provider_id else 1

    usage_kwargs: dict[str, Any] = {
        "input_tokens": billable_input_tokens(
            input_tokens,
            cached_tokens,
            cache_write_tokens,
            input_is_total,
        ),
        "output_tokens": max(int(output_tokens), 0),
    }
    if cached_tokens:
        usage_kwargs["cache_read_tokens"] = int(cached_tokens)
    if cache_write_tokens:
        usage_kwargs["cache_write_tokens"] = int(cache_write_tokens)

    try:
        calculation = genai_prices.calc_price(
            genai_prices.Usage(**usage_kwargs),
            model,
            provider_id=provider_id,
            genai_request_timestamp=timestamp,
        )
    except Exception:  # noqa: BLE001 - any failure means "cannot price this call"
        return UNPRICED

    model_price = calculation.model_price
    when = timestamp or datetime.now(UTC)
    return PricedCall(
        has_price=1,
        cost_usd=float(calculation.total_price),
        is_estimated=is_estimated,
        priced_provider=calculation.provider.id,
        priced_model=calculation.model.id,
        price_effective_from=_effective_from(calculation.model, when),
        input_price_per_1m=_rate(model_price, "input_mtok"),
        output_price_per_1m=_rate(model_price, "output_mtok"),
        cached_price_per_1m=_rate(model_price, "cache_read_mtok"),
        cache_write_price_per_1m=_rate(model_price, "cache_write_mtok"),
        price_source=_price_source(calculation.provider),
    )


def warn_if_unavailable() -> None:
    """Say once, loudly, that costs will be missing rather than wrong."""
    if not available():  # pragma: no cover - depends on the install
        print(
            "pricing: genai-prices is not installed, calls will be left unpriced",
            file=sys.stderr,
            flush=True,
        )
