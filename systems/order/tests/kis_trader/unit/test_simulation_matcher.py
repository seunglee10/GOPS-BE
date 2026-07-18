from __future__ import annotations

from kis_trader.paper.simulation_matcher import process_execution_page


def test_empty_active_symbol_set_advances_the_whole_page_without_matching():
    matched = []
    checkpoints = []
    heartbeats = []

    result = process_execution_page(
        {
            "nextSequence": 50_411,
            "quotes": [
                {"sequence": 412, "symbol": "AAPL"},
                {"sequence": 413, "symbol": "MSFT"},
            ],
        },
        active_symbols=set(),
        on_quote=lambda quote: matched.append(quote),
        save_checkpoint=checkpoints.append,
        heartbeat=lambda: heartbeats.append(True),
        checkpoint_interval=100,
    )

    assert matched == []
    assert checkpoints == [50_411]
    assert len(heartbeats) == 1
    assert result == {"seen": 2, "selected": 0, "checkpoint": 50_411}


def test_only_active_symbols_are_matched_and_progress_is_checkpointed_in_chunks():
    matched = []
    checkpoints = []
    heartbeats = []
    quotes = [
        {"sequence": sequence, "symbol": "AAPL" if sequence % 2 == 0 else "MSFT"}
        for sequence in range(1, 7)
    ]

    result = process_execution_page(
        {"nextSequence": 10, "quotes": quotes},
        active_symbols={"AAPL"},
        on_quote=lambda quote: matched.append(quote["sequence"]),
        save_checkpoint=checkpoints.append,
        heartbeat=lambda: heartbeats.append(True),
        checkpoint_interval=2,
    )

    assert matched == [2, 4, 6]
    assert checkpoints == [4, 10]
    assert len(heartbeats) == 2
    assert result == {"seen": 6, "selected": 3, "checkpoint": 10}
