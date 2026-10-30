"""Prefix-cache and speculative-decoding figures, the same arithmetic for every
engine (GitHub issue #2).

Engines report these differently. llama.cpp's `prompt_tokens_total` EXCLUDES
cached tokens, so its lookups are prompt + cached, while vLLM counts lookups
directly. TensorFold counts draft tokens but not draft steps. Each collector
maps its engine's counters onto `ServingStats`, and the ratios are derived
here, once.
"""

from __future__ import annotations

from spark_dash_common.models import ServingStats


def _count(value: float | None) -> int | None:
    return None if value is None else int(value)


def fill(
    stats: ServingStats,
    *,
    prefix_hits: float | None = None,
    prefix_queries: float | None = None,
    draft_tokens: float | None = None,
    accepted: float | None = None,
    drafts: float | None = None,
) -> None:
    """Record the counters on `stats` and derive its ratios.

    A ratio is left None when its denominator is absent or zero: an engine
    with speculative decoding switched off publishes the counters at 0, and
    "no drafts" must not read as "0% accepted".
    """
    stats.prefix_hit_tokens_total = _count(prefix_hits)
    stats.prefix_query_tokens_total = _count(prefix_queries)
    stats.spec_draft_tokens_total = _count(draft_tokens)
    stats.spec_accepted_tokens_total = _count(accepted)
    stats.spec_drafts_total = _count(drafts)

    if prefix_hits is not None and prefix_queries:
        stats.prefix_hit_pct = 100.0 * prefix_hits / prefix_queries
    if accepted is not None and draft_tokens:
        stats.spec_accept_pct = 100.0 * accepted / draft_tokens
    if accepted is not None and drafts:
        stats.spec_accept_len = 1.0 + accepted / drafts
