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
            average_daily_volume=Decimal("1000000"),
        ),
        daily_pnl=Decimal("0"),
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

    assert verdict.verdict == "allow"
    assert not triggered(verdict, "single_name_limit")


def test_single_name_limit_not_applied_to_sells():
    context = build_context(
        positions=(PositionSnapshot("NVDA", Decimal("30"), Decimal("3000")),),
    )
    verdict = evaluate(qty="30", side="sell", context=context)

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


# --- aggregation ------------------------------------------------------------------


def test_block_wins_over_resize():
    # daily loss block + single-name resize on the same order -> block
    context = build_context(
        daily_pnl=Decimal("-300"),
        positions=(PositionSnapshot("NVDA", Decimal("15"), Decimal("1500")),),
    )
    verdict = evaluate(qty="10", context=context)

    assert verdict.verdict == "block"
    assert verdict.adjusted_qty is None


def test_verdict_serializes_for_api_and_agent():
    context = build_context(
        positions=(PositionSnapshot("NVDA", Decimal("15"), Decimal("1500")),),
    )
    verdict = evaluate(qty="10", context=context)
    payload = verdict.to_dict()

    assert payload["verdict"] == "resize"
    assert payload["adjustedQty"] == "5"
    assert payload["triggeredRules"][0]["ruleId"] == "single_name_limit"
    assert isinstance(payload["skippedRules"], list)


def test_empty_context_only_runs_data_free_rules():
    verdict = evaluate(context=RiskContext())

    assert verdict.verdict == "allow"
    assert verdict.results == ()
    assert {"single_name_limit", "sector_limit", "daily_loss_cooldown"} <= skipped_ids(verdict)


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
            "metrics": {"lastPrice": "100", "averageDailyVolume": "5000"},
        }
    )

    assert context.account_equity == Decimal("10000")
    assert len(context.positions) == 1
    assert context.positions[0].symbol == "NVDA"
    assert context.metrics.average_daily_volume == Decimal("5000")


# --- edge cases ---------------------------------------------------------------


def test_boundary_values_pass_at_exact_limits():
    """팻핑거 3종은 전부 초과(>)일 때만 발동 — 정확히 한도값이면 통과."""
    config = load_risk_config(overrides={"max_order_notional": "1000"})
    at_notional_cap = evaluate(qty="10", price="100", config=config)  # 1000 == cap
    assert not triggered(at_notional_cap, "fat_finger")

    thin_adv = build_context(metrics=SymbolMetrics(last_price=Decimal("100"), average_daily_volume=Decimal("100")))
    at_adv_cap = evaluate(qty="5", context=thin_adv)  # 5% == cap
    assert not triggered(at_adv_cap, "fat_finger")

    at_band_cap = evaluate(qty="1", price="105")  # 5% == price band
    assert not triggered(at_band_cap, "fat_finger")


def test_side_and_symbol_are_normalized():
    context = build_context(
        positions=(PositionSnapshot("NVDA", Decimal("15"), Decimal("1500")),),
    )
    verdict = evaluate_pretrade(
        side="  BUY ",
        symbol="nvda",
        qty=Decimal("10"),
        price=Decimal("100"),
        context=context,
        config=RiskConfig(),
    )

    # 소문자 심볼도 기존 NVDA 포지션과 합산되어 비중 한도에 걸린다
    assert verdict.verdict == "resize"
    assert verdict.adjusted_qty == Decimal("5")


def test_duplicate_position_rows_are_aggregated():
    context = build_context(
        positions=(
            PositionSnapshot("NVDA", Decimal("10"), Decimal("1000")),
            PositionSnapshot("NVDA", Decimal("10"), Decimal("1000")),
        ),
    )
    verdict = evaluate(qty="1", context=context)  # 이미 2000 == 20% cap

    assert verdict.verdict == "block"


def test_sector_from_metrics_overrides_config_map():
    context = build_context(
        positions=(PositionSnapshot("XOM", Decimal("38"), Decimal("3800"), sector="energy"),),
        metrics=SymbolMetrics(last_price=Decimal("100"), average_daily_volume=Decimal("1000000"), sector="energy"),
    )
    config = load_risk_config(overrides={"sector_map": {"NVDA": "semiconductor"}})
    verdict = evaluate(qty="7", context=context, config=config)  # energy 45% > 40%

    result = triggered(verdict, "sector_limit")[0]
    assert result.numbers["sector"] == "energy"


def test_zero_equity_skips_equity_rules_but_fat_finger_still_blocks():
    context = build_context(account_equity=Decimal("0"))
    verdict = evaluate(qty="1", price="200", context=context)  # 직전가 100 대비 +100%

    assert verdict.verdict == "block"
    assert triggered(verdict, "fat_finger")
    assert {"single_name_limit", "sector_limit", "daily_loss_cooldown"} <= skipped_ids(verdict)


def test_yaml_file_overrides_thresholds(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("single_name_max_weight: 0.5\n", encoding="utf-8")
    config = load_risk_config(path=rules)

    verdict = evaluate(qty="30", price="100", config=config)  # 30% < 50% cap

    assert verdict.verdict == "allow"
    assert not triggered(verdict, "single_name_limit")


def test_load_risk_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        load_risk_config(overrides={"nope": 1})


def test_removed_rule_keys_are_rejected_by_config():
    # 손절·사이징 제거 후 이 키들은 더 이상 유효하지 않다 (risk-stoploss-removal-plan.md)
    for key in ("risk_per_trade", "atr_stop_multiple"):
        with pytest.raises(ValueError):
            load_risk_config(overrides={key: "0.02"})
