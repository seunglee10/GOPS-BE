"""Small, testable execution-page loop for the SIM paper matcher."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any


def process_execution_page(
    page: Mapping[str, Any],
    *,
    active_symbols: Collection[str],
    on_quote: Callable[[Mapping[str, Any]], None],
    save_checkpoint: Callable[[int], None],
    heartbeat: Callable[[], None],
    checkpoint_interval: int,
) -> dict[str, int]:
    """Process only useful quotes and persist bounded progress within a page."""
    normalized_symbols = {
        str(symbol).strip().upper()
        for symbol in active_symbols
        if str(symbol).strip()
    }
    interval = max(1, int(checkpoint_interval))
    quotes = page.get("quotes") or []
    seen = 0
    selected = 0
    last_checkpoint: int | None = None

    for quote in quotes:
        if not isinstance(quote, Mapping):
            continue
        seen += 1
        symbol = str(quote.get("symbol") or "").strip().upper()
        if symbol not in normalized_symbols:
            continue
        on_quote(quote)
        selected += 1
        if selected % interval == 0:
            last_checkpoint = max(0, int(quote.get("sequence") or 0))
            save_checkpoint(last_checkpoint)
            heartbeat()

    next_sequence = max(0, int(page.get("nextSequence") or 0))
    if next_sequence != last_checkpoint:
        save_checkpoint(next_sequence)
        heartbeat()

    return {"seen": seen, "selected": selected, "checkpoint": next_sequence}
