"""KIS demo API integration."""

from .config import KisConfig, KisConfigError, load_kis_config
from .fake import FakeKisClient, KisConnectionReset, KisExplicitReject, KisHttpError, KisTimeout, KisTokenExpired
from .payload import build_kis_order_payload

__all__ = [
    "DemoKisHttpClient",
    "FakeKisClient",
    "KisConfig",
    "KisConfigError",
    "KisConnectionReset",
    "KisExplicitReject",
    "KisHttpError",
    "KisTimeout",
    "KisTokenExpired",
    "build_kis_order_payload",
    "load_kis_config",
]


def __getattr__(name: str):
    if name == "DemoKisHttpClient":
        from .client import DemoKisHttpClient

        return DemoKisHttpClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
