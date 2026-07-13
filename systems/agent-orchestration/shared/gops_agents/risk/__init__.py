"""Always-on risk monitoring (defensive rules only).

Buy-signal / opportunity alerts belong to the signal agent, not here — see
risk-manager-implementation-plan.md section 0.5 for the role boundary.
"""

from .monitor import RiskMonitor, RiskMonitorThresholds

__all__ = ["RiskMonitor", "RiskMonitorThresholds"]
