"""User-facing pre-trade risk engine.

Deterministic rule evaluation only — no LLM involvement. The engine consumes
pre-computed market metrics (ATR, ADV, last price) and portfolio state, and
returns a structured verdict that the Risk Agent and the order ticket UI can
render. Operational guardrails (kill switches, broker circuit breakers) stay
in `kis_trader.operations.guardrails` and are not replaced by this package.
"""

from .config import RiskConfig, load_risk_config
from .context import PositionSnapshot, RiskContext, SymbolMetrics
from .engine import PretradeVerdict, RuleResult, evaluate_pretrade
from .portfolio import portfolio_weights, position_market_value, sector_exposures

__all__ = [
    "PositionSnapshot",
    "PretradeVerdict",
    "RiskConfig",
    "RiskContext",
    "RuleResult",
    "SymbolMetrics",
    "evaluate_pretrade",
    "load_risk_config",
    "portfolio_weights",
    "position_market_value",
    "sector_exposures",
]
