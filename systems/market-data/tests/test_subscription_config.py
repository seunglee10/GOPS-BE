import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "market-data" / "shared"))

from alfaka.alpaca.subscription import configured_collection_symbols, load_symbols_and_channels  # noqa: E402


class SubscriptionConfigTest(unittest.TestCase):
    def test_collection_symbols_follow_configured_universe(self) -> None:
        config = {
            "defaultUniverse": "sp500",
            "universeRegistryPath": "",
            "defaultSymbols": ["AAPL", "MSFT", "JPM"],
            "validChannels": ["bars", "updatedBars", "trades", "dailyBars", "statuses", "quotes"],
            "symbolPattern": r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$",
        }

        with mock.patch.dict(os.environ, {"ALPACA_UNIVERSE": "sp500"}, clear=True):
            self.assertEqual(configured_collection_symbols(config), ["AAPL", "MSFT", "JPM"])

    def test_explicit_preview_symbol_still_builds_single_symbol_request(self) -> None:
        config = {
            "defaultUniverse": "sp500",
            "universeRegistryPath": "",
            "defaultSymbols": ["AAPL", "MSFT", "JPM"],
            "defaultChannels": ["bars", "dailyBars", "statuses"],
            "activeChartChannels": ["trades"],
            "validChannels": ["bars", "updatedBars", "trades", "dailyBars", "statuses", "quotes"],
            "symbolPattern": r"^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$",
            "companyToSymbol": {},
            "symbolMetadata": {},
        }

        with mock.patch("alfaka.alpaca.subscription.load_request_config", return_value=config):
            with mock.patch.dict(os.environ, {"ALPACA_CHANNELS": "bars,dailyBars,statuses"}, clear=True):
                symbols, channels = load_symbols_and_channels("AAPL")

        self.assertEqual(symbols, ["AAPL"])
        self.assertEqual(channels, ["bars", "dailyBars", "statuses"])


if __name__ == "__main__":
    unittest.main()
