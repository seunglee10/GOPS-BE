from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.professional_v2 import (  # noqa: E402
    normalize_fundamental_batch,
    resolve_algorithm_version,
    stable_digest,
)


NOW = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)


def test_fundamental_batch_validates_provenance_cutoff_and_quality() -> None:
    payload = {
        "snapshotId": "snapshot-1",
        "schemaVersion": "fundamentals.v1",
        "featureVersion": "features.v1",
        "digest": "abc",
        "sourceAsOf": (NOW - timedelta(minutes=1)).isoformat(),
        "snapshots": {
            "FAST": {
                "value": 80,
                "quality": 70,
                "growth": 60,
                "earningsRevision": 50,
                "coverage": 1,
                "freshness": 0.8,
                "sourceQuality": 0.5,
            }
        },
    }

    rows, provenance = normalize_fundamental_batch(payload, ["FAST", "MISS"], NOW)
    future, future_provenance = normalize_fundamental_batch(
        {**payload, "sourceAsOf": (NOW + timedelta(minutes=1)).isoformat()}, ["FAST"], NOW
    )

    assert provenance["status"] == "ready"
    assert rows["FAST"]["score"] == 68.0
    assert rows["FAST"]["weight"] == 0.06
    assert "MISS" not in rows
    assert future == {}
    assert future_provenance["status"] == "future_data"


def test_continuous_v2_algorithm_selection_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be legacy, professional-v1, or deterministic-evidence-v3"):
        resolve_algorithm_version("continuous-v2", enabled=False, shadow=True)
    assert resolve_algorithm_version("deterministic-evidence-v3", enabled=False, shadow=True) == (
        "deterministic-evidence-v3",
        False,
    )
    assert resolve_algorithm_version(None, enabled=True, shadow=True) == ("professional-v1", True)


def test_stable_digest_is_reproducible_across_key_order() -> None:
    assert stable_digest({"b": 2, "a": 1}) == stable_digest({"a": 1, "b": 2})
