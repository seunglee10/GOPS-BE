from decimal import Decimal

import pytest

from kis_trader.risk import (
    PositionSnapshot,
    RiskConfig,
    RiskContext,
    SymbolMetrics,
    evaluate_pretrade,
    load_risk_config,
    portfolio_weights,
    sector_exposures,
)
from kis_trader.risk.context import risk_context_from_dict


def build_context(**overrides):
    defaults = dict(
        account_equity=Decimal("10000"),
        positions=(),
        metrics=SymbolMetrics(
            last_price=Decimal("100"),
            atr=Decimal("2"),
            average_daily_volume=Decimal("1000000"),
        ),
        daily_pnl=Decimal("0"),
        stop_price=Decimal("96"),
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def evaluate(qty="10", price="100", side="buy", symbol="NVDA", context=None, config=None):
    return evaluate_pretrade(
        side=side,
        symbol=symbol,
        qty=Decimal(qty),
        price=Decimal(price),
        context=context or build_context(),
        config=config or RiskConfig(),
    )


def triggered(verdict, rule_id):
    return [result for result in verdict.results if result.rule_id == rule_id]


def skipped_ids(verdict):
    return {item.rule_id for item in verdict.skipped}


# --- position_sizing_2pct_atr ------------------------------------------------


WIDE_NAME_CAP = {"single_name_max_weight": "1.0"}  # isolate position sizing from the 20% cap


def test_position_sizing_allows_qty_at_exact_limit():
    # equity 10000 * 2% = 200 max loss; stop distance = ATR 2 * 2 = 4 -> 50 shares
    verdict = evaluate(qty="50", config=load_risk_config(overrides=WIDE_NAME_CAP))

    assert verdict.verdict == "allow"
    assert not triggered(verdict, "position_sizing_2pct_atr")


def test_position_sizing_resizes_one_share_over_limit():
    verdict = evaluate(qty="51", config=load_risk_config(overrides=WIDE_NAME_CAP))

    assert verdict.verdict == "resize"
    assert verdict.adjusted_qty == Decimal("50")
    result = triggered(verdict, "position_sizing_2pct_atr")[0]
    assert result.suggested_qty == Decimal("50")
    assert result.numbers["allowedQty"] == "50"


def test_position_sizing_skipped_without_atr():
    context = build_context(metrics=SymbolMetrics(last_price=Decimal("100")))
    verdict = evaluate(context=context)

    assert "position_sizing_2pct_atr" in skipped_ids(verdict)


def test_position_sizing_not_applied_to_sells():
    verdict = evaluate(qty="500", side="sell")

    assert not triggered(verdict, "position_sizing_2pct_atr")


# --- single_name_limit -------------------------------------------------------


def test_single_name_limit_resizes_to_remaining_headroom():
    # equity 10000, cap 20% = 2000; existing NVDA 1500 -> headroom 500 -> 5 shares at 100
    context = build_context(
        positions=(PositionSnapshot("NVDA", Decimal("15"), Decimal("1500")),),
    )
    verdict = evaluate(qty="10", context=context)

    result = triggered(verdict, "single_name_limit")[0]
    assert result.suggested_qty == Decimal("5")
    assert verdict.verdict == "resize"
    assert verdict.adjusted_qty == Decimal("5")


def test_single_name_limit_blocks_when_already_at_cap():
    context = build_context(
        positions=(PositionSnapshot("NVDA", Decimal("20"), Decimal("2000")),),
    )
    verdict = evaluate(qty="1", context=context)

    assert verdict.verdict == "block"
    assert triggered(verdict, "single_name_limit")[0].suggested_qty == Decimal("0")


def test_single_name_limit_allows_exact_cap():
    verdict = evaluate(qty="20", price="100")  # 2000 == 20% of 10000

    assert not triggered(verdict, "single_name_limit")


# --- sector_limit -------------------------------------------------------------


def test_sector_limit_warns_using_sector_map_fallback():
    # existing semis 3500 + order 10*100 = 4500 -> 45% > 40%
    context = build_context(
        positions=(
            PositionSnapshot("MU", Decimal("10"), Decimal("2000"), sector="semiconductor"),
            PositionSnapshot("AMD", Decimal("10"), Decimal("1500"), sector="semiconductor"),
        ),
    )
    config = load_risk_config(overrides={"sector_map": {"NVDA": "semiconductor"}})
    verdict = evaluate(qty="10", context=context, config=config)

    result = triggered(verdict, "sector_limit")[0]
    assert result.action == "warn"
    assert result.numbers["sector"] == "semiconductor"
    # warn does not change the verdict
    assert verdict.verdict == "allow"


def test_sector_limit_skipped_when_sector_unknown():
    verdict = evaluate(symbol="ZZZZ")

    assert "sector_limit" in skipped_ids(verdict)


# --- fat_finger ----------------------------------------------------------------


def test_fat_finger_blocks_notional_above_cap():
    config = load_risk_config(overrides={"max_order_notional": "1000"})
    verdict = evaluate(qty="11", price="100", config=config)

    assert verdict.verdict == "block"
    assert triggered(verdict, "fat_finger")[0].numbers["notional"] == "1100"


def test_fat_finger_blocks_excessive_adv_participation():
    context = build_context(
        metrics=SymbolMetrics(
            last_price=Decimal("100"),
            atr=Decimal("2"),
            average_daily_volume=Decimal("100"),
        ),
    )
    verdict = evaluate(qty="6", context=context)  # 6% of ADV > 5%

    assert verdict.verdict == "block"
    assert triggered(verdict, "fat_finger")[0].numbers["participation"] == "0.06"


def test_fat_finger_blocks_price_far_from_last_price():
    verdict = evaluate(qty="1", price="106")  # 6% above last price 100

    assert verdict.verdict == "block"
    assert "lastPrice" in triggered(verdict, "fat_finger")[0].numbers


def test_fat_finger_applies_to_sells_too():
    config = load_risk_config(overrides={"max_order_notional": "1000"})
    verdict = evaluate(qty="11", price="100", side="sell", config=config)

    assert verdict.verdict == "block"


# --- daily_loss_cooldown --------------------------------------------------------


def test_daily_loss_cooldown_blocks_buy_at_limit():
    context = build_context(daily_pnl=Decimal("-300"))  # 3% of 10000
    verdict = evaluate(context=context)

    assert verdict.verdict == "block"
    assert triggered(verdict, "daily_loss_cooldown")


def test_daily_loss_cooldown_allows_buy_just_inside_limit():
    context = build_context(daily_pnl=Decimal("-299.99"))
    verdict = evaluate(context=context)

    assert not triggered(verdict, "daily_loss_cooldown")


def test_daily_loss_cooldown_does_not_block_sells():
    context = build_context(daily_pnl=Decimal("-999"))
    verdict = evaluate(side="sell", context=context)

    assert not triggered(verdict, "daily_loss_cooldown")


# --- stop_loss_required ----------------------------------------------------------


def test_stop_loss_warning_suggests_atr_stop():
    context = build_context(stop_price=None)
    verdict = evaluate(qty="10", price="100", context=context)

    result = triggered(verdict, "stop_loss_required")[0]
    assert result.action == "warn"
    assert Decimal(result.numbers["suggestedStop"]) == Decimal("96")  # 100 - 2*2
    assert verdict.verdict == "allow"


def test_no_stop_loss_warning_when_stop_attached():
    verdict = evaluate()

    assert not triggered(verdict, "stop_loss_required")


# --- aggregation ------------------------------------------------------------------


def test_block_wins_over_resize():
    context = build_context(daily_pnl=Decimal("-300"))
    verdict = evaluate(qty="51", context=context)

    assert verdict.verdict == "block"
    assert verdict.adjusted_qty is None


def test_smallest_resize_suggestion_wins():
    # position sizing allows 50; single name cap allows 20 -> adjusted 20
    verdict = evaluate(qty="60")

    assert verdict.verdict == "resize"
    assert verdict.adjusted_qty == Decimal("20")


def test_verdict_serializes_for_api_and_agent():
    verdict = evaluate(qty="51", config=load_risk_config(overrides=WIDE_NAME_CAP))
    payload = verdict.to_dict()

    assert payload["verdict"] == "resize"
    assert payload["adjustedQty"] == "50"
    assert payload["triggeredRules"][0]["ruleId"] == "position_sizing_2pct_atr"
    assert isinstance(payload["skippedRules"], list)


def test_empty_context_only_runs_data_free_rules():
    verdict = evaluate(context=RiskContext())

    assert verdict.verdict == "allow"
    assert {"position_sizing_2pct_atr", "single_name_limit", "sector_limit", "daily_loss_cooldown"} <= skipped_ids(verdict)
    # stop-loss warning still fires without any market data
    assert triggered(verdict, "stop_loss_required")


# --- portfolio helpers --------------------------------------------------------------


def test_portfolio_weights_and_sector_exposures():
    positions = (
        PositionSnapshot("NVDA", Decimal("10"), Decimal("2000")),
        PositionSnapshot("MU", Decimal("10"), Decimal("1000"), sector="semiconductor"),
    )
    weights = portfolio_weights(positions, Decimal("10000"))
    exposures = sector_exposures(positions, Decimal("10000"), {"NVDA": "semiconductor"})

    assert weights["NVDA"] == Decimal("0.2")
    assert exposures["semiconductor"] == Decimal("0.3")


def test_risk_context_from_dict_parses_json_payload():
    context = risk_context_from_dict(
        {
            "accountEquity": "10000",
            "dailyPnl": "-50",
            "positions": [
                {"symbol": "nvda", "quantity": "10", "marketValue": "2000", "sector": "semiconductor"},
                {"symbol": "", "quantity": "1", "marketValue": "1"},
            ],
            "metrics": {"lastPrice": "100", "atr": "2", "averageDailyVolume": "5000"},
        }
    )

    assert context.account_equity == Decimal("10000")
    assert len(context.positions) == 1
    assert context.positions[0].symbol == "NVDA"
    assert context.metrics.atr == Decimal("2")


def test_load_risk_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        load_risk_config(overrides={"nope": 1})
