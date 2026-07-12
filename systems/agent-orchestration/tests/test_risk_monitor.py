import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
if str(AGENT_SHARED) not in sys.path:
    sys.path.insert(0, str(AGENT_SHARED))

from gops_agents.risk import RiskMonitor, RiskMonitorThresholds  # noqa: E402

FILLS_TOPIC = "orders.fills.v1"
CANDLE_TOPIC = "market.layer.candles.1m.closed.v1"


def fill(symbol="NVDA", side="buy", qty="10", price="100", account="demo-account"):
    return {
        "schema_version": 1,
        "event_type": "order.filled",
        "account_alias": account,
        "payload": {"symbol": symbol, "side": side, "qty": qty, "price": price, "status": "FILLED"},
    }


def candle(symbol="NVDA", close=100.0, volume=1_000_000, timestamp="2026-07-11T14:00:00Z", open_price=None):
    return {
        "symbol": symbol,
        "open": open_price if open_price is not None else close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": volume,
        "timestamp": timestamp,
    }


def build_monitor(**kwargs):
    kwargs.setdefault("account_equity", 10_000.0)
    return RiskMonitor(**kwargs)


def warm_up_prices(monitor, symbol="NVDA", price=100.0, bars=6):
    """Feed a few candles so the day anchor and volume history exist."""
    for index in range(bars):
        monitor.handle(candle(symbol=symbol, close=price, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)


def types_of(events):
    return [event["eventType"] for event in events]


class RiskMonitorFillTest(unittest.TestCase):
    def test_buy_fill_builds_position_without_events(self):
        monitor = build_monitor()

        events = monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)

        self.assertEqual(events, [])
        position = monitor.positions["demo-account"]["NVDA"]
        self.assertEqual(position.qty, 10)
        self.assertEqual(position.avg_price, 100)

    def test_sell_realizes_pnl_and_clears_position(self):
        monitor = build_monitor()
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)

        monitor.handle(fill(side="sell", qty="10", price="90"), FILLS_TOPIC)

        self.assertAlmostEqual(monitor.realized_pnl_today["demo-account"], -100.0)
        self.assertNotIn("NVDA", monitor.positions["demo-account"])

    def test_partial_sell_keeps_remaining_position(self):
        monitor = build_monitor()
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)

        monitor.handle(fill(side="sell", qty="4", price="110"), FILLS_TOPIC)

        position = monitor.positions["demo-account"]["NVDA"]
        self.assertEqual(position.qty, 6)
        self.assertEqual(position.avg_price, 100)
        self.assertAlmostEqual(monitor.realized_pnl_today["demo-account"], 40.0)

    def test_oversell_clears_position_without_crash(self):
        monitor = build_monitor()
        monitor.handle(fill(qty="5", price="100"), FILLS_TOPIC)

        monitor.handle(fill(side="sell", qty="10", price="90"), FILLS_TOPIC)

        # 실현손익은 실제 보유분(5주)까지만 계산, 포지션은 정리
        self.assertAlmostEqual(monitor.realized_pnl_today["demo-account"], -50.0)
        self.assertNotIn("NVDA", monitor.positions["demo-account"])

    def test_averaging_up_recomputes_avg_price(self):
        monitor = build_monitor()
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)

        monitor.handle(fill(qty="10", price="120"), FILLS_TOPIC)

        position = monitor.positions["demo-account"]["NVDA"]
        self.assertEqual(position.qty, 20)
        self.assertAlmostEqual(position.avg_price, 110.0)

    def test_malformed_fill_payloads_are_ignored(self):
        monitor = build_monitor()

        for bad in (
            {"payload": {"side": "buy", "qty": "10", "price": "100"}, "account_alias": "a"},  # symbol 없음
            {"payload": {"symbol": "NVDA", "side": "hold", "qty": "10", "price": "100"}, "account_alias": "a"},
            {"payload": {"symbol": "NVDA", "side": "buy", "qty": "-3", "price": "100"}, "account_alias": "a"},
            {"payload": {"symbol": "NVDA", "side": "buy", "qty": "10", "price": "abc"}, "account_alias": "a"},
            {"payload": {"symbol": "NVDA", "side": "buy", "qty": "10", "price": "100"}},  # 계좌 없음
        ):
            self.assertEqual(monitor.handle(bad, FILLS_TOPIC), [])
        self.assertEqual(monitor.positions, {})

    def test_malformed_candles_are_ignored(self):
        monitor = build_monitor()
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)

        no_symbol = monitor.handle({"close": 100.0, "volume": 1000}, CANDLE_TOPIC)
        no_close = monitor.handle({"symbol": "NVDA", "volume": 1000}, CANDLE_TOPIC)
        zero_close = monitor.handle({"symbol": "NVDA", "close": 0, "volume": 1000}, CANDLE_TOPIC)

        self.assertEqual((no_symbol, no_close, zero_close), ([], [], []))


class RiskMonitorRuleTest(unittest.TestCase):
    def test_concentration_drift_fires_when_price_rally_breaks_cap(self):
        monitor = build_monitor()
        warm_up_prices(monitor)
        monitor.handle(fill(qty="15", price="100"), FILLS_TOPIC)  # 15% weight

        events = monitor.handle(candle(close=140.0), CANDLE_TOPIC)  # 21% weight

        self.assertIn("risk_concentration_drift", types_of(events))
        repeat = monitor.handle(candle(close=141.0), CANDLE_TOPIC)
        self.assertNotIn("risk_concentration_drift", types_of(repeat))

    def test_daily_loss_limit_fires_once_per_day_and_resets_next_day(self):
        monitor = build_monitor()
        warm_up_prices(monitor)
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)
        # Day anchor was set at 100 during warm-up; drop to 66 -> -340 unrealized (limit -300).
        first = monitor.handle(candle(close=66.0), CANDLE_TOPIC)
        second = monitor.handle(candle(close=65.0), CANDLE_TOPIC)
        next_day = monitor.handle(candle(close=65.0, timestamp="2026-07-12T09:00:00Z"), CANDLE_TOPIC)

        self.assertIn("risk_daily_loss_limit", types_of(first))
        self.assertNotIn("risk_daily_loss_limit", types_of(second))
        # New day: anchor resets to the first observed price, pnl starts fresh.
        self.assertNotIn("risk_daily_loss_limit", types_of(next_day))

    def test_anomaly_surge_fires_on_price_and_volume_spike(self):
        monitor = build_monitor()
        # Warm-up: normal volume 1000, price around 100 (sets day anchor at 100).
        for index in range(6):
            monitor.handle(candle(symbol="PUMP", close=100.0 + index * 0.1, volume=1000, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)

        events = monitor.handle(candle(symbol="PUMP", close=112.0, volume=8000), CANDLE_TOPIC)

        self.assertIn("risk_anomaly_surge", types_of(events))
        surge = next(event for event in events if event["eventType"] == "risk_anomaly_surge")
        self.assertGreaterEqual(surge["metrics"]["volumeMultiple"], 5.0)
        # 조작 단정 금지 — 요약문에 '조작' 표현이 없어야 함
        self.assertNotIn("조작", surge["summary"])
        repeat = monitor.handle(candle(symbol="PUMP", close=113.0, volume=9000), CANDLE_TOPIC)
        self.assertNotIn("risk_anomaly_surge", types_of(repeat))

    def test_anomaly_surge_needs_both_price_and_volume(self):
        monitor = build_monitor()
        for index in range(6):
            monitor.handle(candle(symbol="PUMP", close=100.0 + index * 0.1, volume=1000, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)

        price_only = monitor.handle(candle(symbol="PUMP", close=112.0, volume=1000), CANDLE_TOPIC)
        volume_only = monitor.handle(candle(symbol="PUMP", close=101.0, volume=9000), CANDLE_TOPIC)

        self.assertNotIn("risk_anomaly_surge", types_of(price_only))
        self.assertNotIn("risk_anomaly_surge", types_of(volume_only))

    def test_correlation_cluster_flags_lockstep_holdings(self):
        monitor = build_monitor()
        monitor.handle(fill(symbol="NVDA", qty="10", price="100"), FILLS_TOPIC)
        monitor.handle(fill(symbol="MU", qty="30", price="100"), FILLS_TOPIC)
        # 12 candles moving in lockstep (identical direction/magnitude pattern).
        moves = [1, -1, 2, -2, 1, 1, -1, 2, -1, 1, 2, -2]
        price_a = price_b = 100.0
        collected = []
        for index, move in enumerate(moves):
            price_a += move
            price_b += move * 0.8
            collected += monitor.handle(candle(symbol="NVDA", close=price_a, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)
            collected += monitor.handle(candle(symbol="MU", close=price_b, timestamp=f"2026-07-11T13:{index:02d}:30Z"), CANDLE_TOPIC)

        # NVDA + MU together exceed 40% of the 10k equity and move in lockstep -> cluster alert
        cluster_events = [event for event in collected if event["eventType"] == "risk_correlation_cluster"]
        self.assertEqual(len(cluster_events), 1)  # one-shot
        self.assertEqual(sorted(cluster_events[0]["metrics"]["cluster"]), ["MU", "NVDA"])

    def test_correlation_cluster_quiet_for_uncorrelated_holdings(self):
        monitor = build_monitor()
        monitor.handle(fill(symbol="NVDA", qty="30", price="100"), FILLS_TOPIC)
        monitor.handle(fill(symbol="KO", qty="30", price="100"), FILLS_TOPIC)
        moves = [1, -1, 2, -2, 1, 1, -1, 2, -1, 1, 2, -2]
        price_a = price_b = 100.0
        collected = []
        for index, move in enumerate(moves):
            price_a += move
            price_b -= move  # opposite direction -> negative correlation
            collected += monitor.handle(candle(symbol="NVDA", close=price_a, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)
            collected += monitor.handle(candle(symbol="KO", close=price_b, timestamp=f"2026-07-11T13:{index:02d}:30Z"), CANDLE_TOPIC)

        self.assertNotIn("risk_correlation_cluster", types_of(collected))

    def test_two_accounts_fire_concentration_independently(self):
        monitor = build_monitor()
        warm_up_prices(monitor)
        monitor.handle(fill(qty="15", price="100", account="acct-a"), FILLS_TOPIC)
        monitor.handle(fill(qty="16", price="100", account="acct-b"), FILLS_TOPIC)

        events = monitor.handle(candle(close=140.0), CANDLE_TOPIC)

        drift = [event for event in events if event["eventType"] == "risk_concentration_drift"]
        self.assertEqual(len(drift), 2)
        self.assertEqual({event["metrics"]["account"] for event in drift}, {"acct-a", "acct-b"})

    def test_concentration_rearms_after_weight_falls_below_reset(self):
        monitor = build_monitor()
        warm_up_prices(monitor)
        monitor.handle(fill(qty="15", price="100"), FILLS_TOPIC)

        first = monitor.handle(candle(close=140.0), CANDLE_TOPIC)   # 21% -> fire
        cooled = monitor.handle(candle(close=120.0), CANDLE_TOPIC)  # 18% < 19%(reset) -> rearm
        second = monitor.handle(candle(close=140.0), CANDLE_TOPIC)  # 21% -> fire again

        self.assertIn("risk_concentration_drift", types_of(first))
        self.assertNotIn("risk_concentration_drift", types_of(cooled))
        self.assertIn("risk_concentration_drift", types_of(second))

    def test_daily_loss_combines_realized_and_unrealized(self):
        monitor = build_monitor()
        warm_up_prices(monitor)  # anchor 100
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)
        monitor.handle(fill(side="sell", qty="5", price="60"), FILLS_TOPIC)  # 실현 -200

        # 남은 5주가 70까지 하락 -> 당일 평가 -150, 합산 -350 <= 한도 -300
        events = monitor.handle(candle(close=70.0), CANDLE_TOPIC)

        self.assertIn("risk_daily_loss_limit", types_of(events))
        loss = next(event for event in events if event["eventType"] == "risk_daily_loss_limit")
        self.assertAlmostEqual(loss["metrics"]["dailyPnl"], -350.0)

    def test_anomaly_surge_can_fire_again_next_day(self):
        monitor = build_monitor()
        for index in range(6):
            monitor.handle(candle(symbol="PUMP", close=100.0, volume=1000, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)
        day1 = monitor.handle(candle(symbol="PUMP", close=112.0, volume=8000), CANDLE_TOPIC)

        # 다음날: 새 기준가(100)에서 다시 급등 + 거래량 폭증
        monitor.handle(candle(symbol="PUMP", close=100.0, open_price=100.0, volume=1000, timestamp="2026-07-12T09:00:00Z"), CANDLE_TOPIC)
        day2 = monitor.handle(candle(symbol="PUMP", close=112.0, volume=20000, timestamp="2026-07-12T09:05:00Z"), CANDLE_TOPIC)

        self.assertIn("risk_anomaly_surge", types_of(day1))
        self.assertIn("risk_anomaly_surge", types_of(day2))

    def test_correlation_cluster_quiet_below_weight_cap(self):
        monitor = build_monitor()
        monitor.handle(fill(symbol="NVDA", qty="10", price="100"), FILLS_TOPIC)
        monitor.handle(fill(symbol="MU", qty="10", price="100"), FILLS_TOPIC)  # 합산 ~20% < 40%
        moves = [1, -1, 2, -2, 1, 1, -1, 2, -1, 1, 2, -2]
        price_a = price_b = 100.0
        collected = []
        for index, move in enumerate(moves):
            price_a += move
            price_b += move * 0.8
            collected += monitor.handle(candle(symbol="NVDA", close=price_a, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)
            collected += monitor.handle(candle(symbol="MU", close=price_b, timestamp=f"2026-07-11T13:{index:02d}:30Z"), CANDLE_TOPIC)

        self.assertNotIn("risk_correlation_cluster", types_of(collected))

    def test_missing_equity_silences_equity_rules_but_not_surge(self):
        monitor = RiskMonitor(account_equity=None)
        for index in range(6):
            monitor.handle(candle(symbol="PUMP", close=100.0, volume=1000, timestamp=f"2026-07-11T13:{index:02d}:00Z"), CANDLE_TOPIC)
        monitor.handle(fill(symbol="PUMP", qty="50", price="100"), FILLS_TOPIC)

        events = monitor.handle(candle(symbol="PUMP", close=112.0, volume=8000), CANDLE_TOPIC)

        self.assertIn("risk_anomaly_surge", types_of(events))
        for silenced in ("risk_concentration_drift", "risk_daily_loss_limit", "risk_correlation_cluster"):
            self.assertNotIn(silenced, types_of(events))

    def test_non_risk_topics_are_ignored(self):
        monitor = build_monitor()

        self.assertEqual(monitor.handle({"anything": 1}, "agents.market-events.v1"), [])

    def test_removed_rules_never_fire(self):
        """손절·유동성·사용자 조건·캘린더 룰 제거 확인 (risk-stoploss-removal-plan.md)."""
        monitor = build_monitor()
        warm_up_prices(monitor)
        monitor.handle(fill(qty="10", price="100"), FILLS_TOPIC)
        # 제거된 제어 토픽 메시지는 무시돼야 함
        ignored = monitor.handle(
            {"action": "register", "account": "demo-account", "symbol": "NVDA", "kind": "price_below", "threshold": 95},
            "risk.control.v1",
        )
        self.assertEqual(ignored, [])

        collected = []
        for index, close in enumerate((97.0, 90.0, 80.0)):
            collected += monitor.handle(candle(close=close, volume=100, timestamp=f"2026-07-11T14:{index:02d}:00Z"), CANDLE_TOPIC)

        removed_rules = (
            "risk_stop_loss_hit",
            "risk_stop_registered",
            "risk_liquidity_warning",
            "risk_user_condition",
            "risk_event_calendar",
        )
        for removed in removed_rules:
            self.assertNotIn(removed, types_of(collected))
        for removed_attr in ("stops", "user_conditions", "calendar"):
            self.assertFalse(hasattr(monitor, removed_attr))

    def test_risk_events_are_compatible_with_notification_publisher(self):
        from gops_agents.events.publisher import notification_payload

        monitor = build_monitor()
        warm_up_prices(monitor)
        monitor.handle(fill(qty="15", price="100"), FILLS_TOPIC)
        drift_event = monitor.handle(candle(close=140.0), CANDLE_TOPIC)[0]

        payload = notification_payload(drift_event)

        self.assertEqual(payload["type"], "AGENT_ALERT")
        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["level"], "alert")
        self.assertTrue(payload["showToast"])
        self.assertEqual(payload["decision"]["eventType"], "risk_concentration_drift")


if __name__ == "__main__":
    unittest.main()
