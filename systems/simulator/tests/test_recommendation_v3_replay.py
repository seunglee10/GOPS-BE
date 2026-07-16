from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SIMULATOR = ROOT / "systems/simulator"
SCENARIO = SIMULATOR / "data/scenarios/recommendation-v3-2026-07-14"


def test_july_14_has_open_all_half_hour_slots_and_close() -> None:
    scenario = json.loads((SCENARIO / "scenario.json").read_text(encoding="utf-8"))
    assert scenario["scenarioId"] == "recommendation-v3-2026-07-14"
    assert scenario["timeZone"] == "America/New_York"
    assert [phase["id"] for phase in scenario["phases"]] == [
        "open-0930", "slot-1000", "slot-1030", "slot-1100", "slot-1130",
        "slot-1200", "slot-1230", "slot-1300", "slot-1330", "slot-1400",
        "slot-1430", "slot-1500", "slot-1530", "closed-1600",
    ]


def test_unextracted_real_fixture_is_explicitly_unavailable() -> None:
    assert not (SCENARIO / "inputs.json.gz").exists()
    assert not (SCENARIO / "recommendations.jsonl").exists()
    assert not (SCENARIO / "manifest.json").exists()
