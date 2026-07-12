import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
if str(MARKET_SHARED) not in sys.path:
    sys.path.insert(0, str(MARKET_SHARED))


from alfaka.serving.indicators import compute_indicator_payload, indicator_specs_from_csv  # noqa: E402


def candles(count):
    return [
        {
            "timestamp": f"2026-06-25T13:{index:02d}:00.000Z",
            "open": index + 1,
            "high": index + 2,
            "low": index,
            "close": index + 1,
            "volume": 100 + index,
        }
        for index in range(count)
    ]


class IndicatorCalculationTest(unittest.TestCase):
    def test_parses_legacy_ma_aliases_as_sma_layers(self):
        specs = indicator_specs_from_csv("ma5,sma:20,ema:12,wma:10,bollinger:20:2,rsi:14,stochastic:14:3:3,macd:12:26:9")

        self.assertEqual([spec.id for spec in specs], [
            "sma:5",
            "sma:20",
            "ema:12",
            "wma:10",
            "bollinger:20:2",
            "rsi:14",
            "stochastic:14:3:3",
            "macd:12:26:9",
        ])

    def test_computes_core_indicator_series_with_warmup_gaps(self):
        specs = indicator_specs_from_csv("sma:5,ema:5,wma:5,bollinger:5:2,rsi:14,stochastic:14:3:3,macd:12:26:9")
        payload = compute_indicator_payload(candles(40), specs)

        self.assertEqual(payload["calculationVersion"], "indicator-v1")
        self.assertIsNone(payload["series"]["sma:5"][3]["value"])
        self.assertEqual(payload["series"]["sma:5"][4]["value"], 3)
        self.assertGreater(payload["series"]["ema:5"][-1]["value"], payload["series"]["ema:5"][4]["value"])
        self.assertEqual(payload["series"]["wma:5"][4]["value"], 11 / 3)
        self.assertGreater(payload["series"]["bollinger:5:2"][4]["upper"], payload["series"]["bollinger:5:2"][4]["middle"])
        self.assertEqual(payload["series"]["rsi:14"][-1]["value"], 100.0)
        self.assertGreater(payload["series"]["stochastic:14:3:3"][-1]["k"], 0)
        self.assertIsNotNone(payload["series"]["macd:12:26:9"][-1]["histogram"])

    def test_filters_computed_points_to_requested_visible_range(self):
        specs = indicator_specs_from_csv("sma:3")
        payload = compute_indicator_payload(
            candles(8),
            specs,
            from_time="2026-06-25T13:04:00.000Z",
            to_time="2026-06-25T13:06:00.000Z",
        )

        self.assertEqual(
            [point["timestamp"] for point in payload["series"]["sma:3"]],
            [
                "2026-06-25T13:04:00.000Z",
                "2026-06-25T13:05:00.000Z",
                "2026-06-25T13:06:00.000Z",
            ],
        )

    def test_computes_sma120_without_a_persisted_candle_column(self):
        source = [
            {
                "timestamp": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}T00:00:00.000Z",
                "open": index + 1,
                "high": index + 2,
                "low": index,
                "close": index + 1,
                "volume": 100 + index,
            }
            for index in range(121)
        ]
        payload = compute_indicator_payload(source, indicator_specs_from_csv("sma:120"))

        self.assertIsNone(payload["series"]["sma:120"][118]["value"])
        self.assertEqual(payload["series"]["sma:120"][119]["value"], 60.5)
        self.assertEqual(payload["series"]["sma:120"][120]["value"], 61.5)


if __name__ == "__main__":
    unittest.main()
