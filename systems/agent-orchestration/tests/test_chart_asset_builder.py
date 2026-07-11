from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "systems" / "agent-orchestration" / "shared", ROOT / "systems" / "market-data" / "shared"):
    if str(path) not in sys.path: sys.path.insert(0, str(path))

from gops_agents.chart_assets.builder import ChartAssetBuilder, _asset_content_digest  # noqa: E402
from gops_agents.chart_assets.envelope import ChartAssetBuildEnvelope  # noqa: E402
from gops_agents.chart_assets.progress import InMemoryChartAssetProgressStore, RedisChartAssetProgressStore  # noqa: E402
from gops_agents.chart_assets.curation import deterministic_curation  # noqa: E402
from alfaka.analytics.analysis_candles import CANDLE_CONTRACT_VERSION, analysis_input_digest  # noqa: E402


class FakeCandleLoader:
    def __init__(self): self.calls = []; self.bundle_calls = 0
    def load(self, symbol, interval):
        self.calls.append((symbol, interval))
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return [{
            "timestamp": (start + timedelta(days=index)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "open": 100 + index * 0.2, "high": 102 + index * 0.2 + math.sin(index),
            "low": 98 + index * 0.2 + math.sin(index), "close": 100 + index * 0.2 + math.sin(index),
            "volume": 1000 + index,
        } for index in range(72)]
    def load_symbol(self, symbol, intervals):
        self.bundle_calls += 1
        rows = {interval: self.load(symbol, interval) for interval in intervals}
        coverage = {interval: {
            "expectedBars": len(values), "actualBars": len(values), "missingBars": 0,
            "coverageRatio": 1, "recentContiguousBars": len(values), "largestGapBars": 0,
            "lastExpectedClosedAt": values[-1]["timestamp"], "lastActualClosedAt": values[-1]["timestamp"],
            "renderable": True, "qualityFlags": [],
        } for interval, values in rows.items()}
        return SimpleNamespace(rows=rows, coverage=coverage, digests={interval: analysis_input_digest(symbol, interval, values) for interval, values in rows.items()})


class PartiallyInsufficientCandleLoader(FakeCandleLoader):
    def load_symbol(self, symbol, intervals):
        bundle = super().load_symbol(symbol, intervals)
        if "1W" in bundle.rows:
            bundle.rows["1W"] = bundle.rows["1W"][:5]
            bundle.coverage["1W"] = {
                **bundle.coverage["1W"],
                "actualBars": 5,
                "missingBars": 67,
                "coverageRatio": 5 / 72,
                "renderable": False,
                "qualityFlags": ["recent_contiguous_below_threshold"],
            }
            bundle.digests["1W"] = analysis_input_digest(symbol, "1W", bundle.rows["1W"])
        return bundle


class FakeStorage:
    def __init__(self, initial=None, cancel_after=None, progress=None, job_id=None):
        self.assets = copy.deepcopy(initial or {})
        self.saved = []
        self.snapshot_calls = []
        self.cancel_after = cancel_after
        self.progress = progress
        self.job_id = job_id
    def get(self, symbol, interval): return copy.deepcopy(self.assets.get((symbol, interval)))
    def get_symbol_assets(self, symbol):
        self.snapshot_calls.append(symbol)
        return {interval: copy.deepcopy(self.assets.get((symbol, interval))) for interval in ("1D", "1W", "1M")}
    def save(self, asset):
        self.assets[(asset["symbol"], asset["interval"])] = copy.deepcopy(asset)
        self.saved.append(copy.deepcopy(asset))
        if self.cancel_after and len(self.saved) == self.cancel_after: self.progress.request_cancel(self.job_id)


class ShadowWarningStorage(FakeStorage):
    def __init__(self):
        super().__init__()
        self.warnings = []
    def save(self, asset):
        super().save(asset)
        self.warnings.append("chart_asset_shadow_write_failed:dual_clickhouse_read:TimeoutError")
    def pop_warnings(self):
        current = list(self.warnings)
        self.warnings.clear()
        return current


class FakeCurator:
    model = "mock-model"
    def __init__(self): self.calls = 0
    def curate_symbol(self, bundle):
        self.calls += 1
        return {"output": deterministic_curation(bundle), "degraded": False, "reason": None, "model": self.model, "usage": {}}


class SelectingCurator:
    model = "mock-model"
    def __init__(self): self.calls = 0
    def curate_symbol(self, bundle):
        self.calls += 1
        selections = []
        for palette in bundle["intervals"]:
            candidates = palette["visualCandidates"]
            if candidates:
                candidate = candidates[0]
                selections.append({
                    "interval": palette["interval"], "selectedCandidateIds": [candidate["candidateId"]],
                    "headlineFactIds": [candidate["factIds"][0]],
                    "focusNarratives": [{"refType": "visualCandidate", "refId": candidate["candidateId"], "factIds": [candidate["factIds"][0]], "watchConditionRef": candidate["confirmationConditionRef"], "priority": 1}],
                    "counterEvidenceRefs": [], "higherTimeframeRelationIds": [], "emphasisCode": "STRUCTURE_FIRST",
                })
            else:
                selections.append({
                    "interval": palette["interval"], "selectedCandidateIds": [], "headlineFactIds": [],
                    "focusNarratives": [], "counterEvidenceRefs": [], "higherTimeframeRelationIds": [], "emphasisCode": "STRUCTURE_FIRST",
                })
        return {"output": {"intervalSelections": selections}, "degraded": False, "reason": None, "model": self.model, "usage": {}}


class FailingCurator:
    model = "mock-model"
    def __init__(self): self.calls = 0
    def curate_symbol(self, _bundle):
        self.calls += 1
        raise TimeoutError("curator unavailable")


class FakeRepairResult:
    def __init__(self, *, unavailable=False, reason="coverage_complete"):
        self.unavailable = unavailable
        self.reason = reason

    def to_dict(self):
        return {
            "checked": True, "attempted": self.reason != "coverage_complete", "repaired": False,
            "unavailable": self.unavailable, "missing_before": 2 if self.unavailable else 0,
            "missing_after": 2 if self.unavailable else 0, "materialized_rows": 0, "reason": self.reason,
        }


class FakeRepairService:
    def __init__(self, result=None):
        self.result = result or FakeRepairResult()
        self.calls = []

    def ensure_ready(self, symbol, intervals, **kwargs):
        self.calls.append((symbol, tuple(intervals)))
        kwargs["on_event"]("audit", {"missingBars": 0, "actualBars": 500, "expectedBars": 500, "ranges": []})
        return self.result


class RecordingProgressStore(InMemoryChartAssetProgressStore):
    def __init__(self):
        super().__init__()
        self.emitted_logs = []

    def add_log(self, _job_id, message):
        self.emitted_logs.append(str(message))


class PublishOnlyRedis:
    def __init__(self):
        self.published = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))


def envelope(symbols=("NVDA", "AAPL"), intervals=("1D", "1W", "1M"), job_id="cab-12345678-test", llm_enabled=False, force=False, skip_fresh_hours=0):
    return ChartAssetBuildEnvelope.create(requested_by="test", symbols=symbols, intervals=intervals, llm_enabled=llm_enabled, force=force, skip_fresh_hours=skip_fresh_hours, job_id=job_id, submitted_at="2026-07-11T00:00:00.000Z")


class ChartAssetBuilderTest(unittest.TestCase):
    def test_content_digest_tracks_presentation_freshness_but_not_audit_time(self):
        base = {
            "asOf": "2026-07-09T04:00:00.000Z",
            "generatedAt": "2026-07-11T00:00:00.000Z",
            "window": {"displayFrom": "2026-01-01T00:00:00.000Z", "displayTo": "2026-07-09T04:00:00.000Z"},
            "input": {"digest": "sha256:old", "candleContractVersion": CANDLE_CONTRACT_VERSION},
            "status": "ready", "quality": {}, "features": {}, "layers": {},
            "chartSetup": {}, "commentary": {}, "buildContext": {},
            "build": {"assemblerVersion": "chart-asset-assembler-v3", "agentOutcome": "not_requested_empty"},
        }
        audit_only = {**copy.deepcopy(base), "generatedAt": "2026-07-11T01:00:00.000Z"}
        next_candle = copy.deepcopy(base)
        next_candle.update({"asOf": "2026-07-10T04:00:00.000Z"})
        next_candle["window"]["displayTo"] = next_candle["asOf"]
        next_candle["input"]["digest"] = "sha256:new"

        self.assertEqual(_asset_content_digest(base), _asset_content_digest(audit_only))
        self.assertNotEqual(_asset_content_digest(base), _asset_content_digest(next_candle))

    def test_requested_symbol_runs_inline_repair_before_candle_load(self):
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-repair")
        progress = RecordingProgressStore(); progress.initialize(request)
        repair = FakeRepairService()
        loader = FakeCandleLoader()
        state = ChartAssetBuilder(
            candle_loader=loader, storage=FakeStorage(), progress=progress,
            repair_service=repair, concurrency=1,
        ).run(request)
        self.assertEqual(repair.calls, [("NVDA", ("1D",))])
        self.assertEqual(loader.bundle_calls, 1)
        self.assertEqual(state["repair"]["checkedSymbols"], 1)
        self.assertTrue(any("repair audit" in line for line in progress.emitted_logs))

    def test_incomplete_repair_reason_is_compact_status_and_interval_warning(self):
        request = envelope(
            symbols=("NVDA",), intervals=("1D",),
            job_id="cab-12345678-repair-reason",
        )
        progress = RecordingProgressStore(); progress.initialize(request)
        repair = FakeRepairService(FakeRepairResult(
            unavailable=True,
            reason="alpaca_unavailable",
        ))

        storage = FakeStorage()
        state = ChartAssetBuilder(
            candle_loader=FakeCandleLoader(), storage=storage,
            progress=progress, repair_service=repair, concurrency=1,
        ).run(request)

        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(state["repair"]["reasonCodes"], {"alpaca_unavailable": 1})
        self.assertIn("alpaca_unavailable", state["recentItems"][0]["warning"])
        asset = storage.assets[("NVDA", "1D")]
        self.assertEqual(asset["status"], "degraded")
        self.assertEqual(asset["quality"]["state"], "insufficient_data")
        self.assertEqual(sum(len(layer["drawings"]) for layer in asset["layers"].values()), 0)

    def test_freshness_skip_does_not_run_repair_or_load_candles(self):
        existing = {("NVDA", "1D"): {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "input": {"candleContractVersion": CANDLE_CONTRACT_VERSION},
            "layers": {"structure": {"drawings": []}, "trend": {"drawings": []}, "agent": {"drawings": []}},
        }}
        request = envelope(
            symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-fresh",
            skip_fresh_hours=24,
        )
        progress = RecordingProgressStore(); progress.initialize(request)
        repair = FakeRepairService(); loader = FakeCandleLoader()
        state = ChartAssetBuilder(
            candle_loader=loader, storage=FakeStorage(existing), progress=progress,
            repair_service=repair, concurrency=1,
        ).run(request)
        self.assertEqual(repair.calls, [])
        self.assertEqual(loader.bundle_calls, 0)
        self.assertEqual(state["progress"]["skipped"], 1)

    def test_old_candle_contract_is_not_fresh(self):
        existing = {("NVDA", "1D"): {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "input": {"candleContractVersion": "analysis-candles-old"},
            "layers": {"structure": {"drawings": []}, "trend": {"drawings": []}, "agent": {"drawings": []}},
        }}
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-old-contract", skip_fresh_hours=24)
        progress = RecordingProgressStore(); progress.initialize(request)
        repair = FakeRepairService(); loader = FakeCandleLoader()

        ChartAssetBuilder(candle_loader=loader, storage=FakeStorage(existing), progress=progress, repair_service=repair, concurrency=1).run(request)

        self.assertEqual(repair.calls, [("NVDA", ("1D",))])
        self.assertEqual(loader.bundle_calls, 1)

    def test_redis_logs_are_pubsub_only_and_not_job_state(self):
        redis = PublishOnlyRedis()
        store = RedisChartAssetProgressStore(redis_client=redis)
        store.add_log("cab-12345678-log", "NVDA:1D saved; entities=2")
        self.assertEqual(redis.published[0][0], "chart-assets.build:cab-12345678-log")
        self.assertEqual(json.loads(redis.published[0][1]), {
            "type": "log", "jobId": "cab-12345678-log", "message": "NVDA:1D saved; entities=2",
        })

    def test_s3_metrics_are_emitted_only_in_ephemeral_log(self):
        progress = RecordingProgressStore()
        builder = ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=FakeStorage(), progress=progress, concurrency=1)

        builder._log_repair_event("cab-12345678-metrics", "NVDA", "s3", {
            "status": "completed", "materializedRows": 12,
            "metrics": {"listCalls": 2, "objectsListed": 5, "manifestObjectsRead": 1, "objectsSelected": 2, "objectGets": 2, "elapsedMs": 41, "manifestSource": "compact"},
        })

        self.assertIn("metrics(listCalls=2 objectsListed=5", progress.emitted_logs[0])
        self.assertIn("manifestSource=compact", progress.emitted_logs[0])

    def test_shadow_write_warning_is_nonfatal_and_visible(self):
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-shadow")
        progress = RecordingProgressStore(); progress.initialize(request)
        storage = ShadowWarningStorage()

        state = ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)

        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(state["progress"]["failed"], 0)
        self.assertTrue(any("shadow_write_failed" in line for line in progress.emitted_logs))

    def test_preflight_warning_is_counted_and_shadow_warning_is_drained(self):
        request = envelope(
            symbols=("NVDA",), intervals=("1D", "1W"),
            job_id="cab-12345678-preflight-shadow",
        )
        progress = RecordingProgressStore(); progress.initialize(request)
        storage = ShadowWarningStorage()

        state = ChartAssetBuilder(
            candle_loader=PartiallyInsufficientCandleLoader(), storage=storage,
            progress=progress, concurrency=1,
        ).run(request)

        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertGreaterEqual(state["progress"]["warnings"], 1)
        weekly = next(item for item in state["recentItems"] if item["interval"] == "1W")
        self.assertIn("shadow_write_failed", weekly["warning"])
        self.assertEqual(storage.warnings, [])
        self.assertTrue(any("1W warning=chart_asset_shadow" in line for line in progress.emitted_logs))

    def test_rule_only_job_builds_six_assets_and_completes(self):
        progress = RecordingProgressStore(); request = envelope(); progress.initialize(request)
        loader = FakeCandleLoader(); storage = FakeStorage()
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=progress, concurrency=2).run(request)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["progress"], {"total": 6, "done": 6, "failed": 0, "skipped": 0, "warnings": 0, "current": state["progress"]["current"]})
        self.assertEqual(len(storage.saved), 6)
        self.assertEqual(loader.bundle_calls, 2)
        self.assertCountEqual(storage.snapshot_calls, request.symbols)
        created_entities = sum(
            len(layer["drawings"])
            for asset in storage.saved
            for layer in asset["layers"].values()
        )
        self.assertTrue(any("entities=" in line and "(S=" in line for line in progress.emitted_logs))
        self.assertIn(f"created_entities={created_entities}", progress.emitted_logs[-1])
        self.assertIn("done=6/6", progress.emitted_logs[-1])
        self.assertNotIn("logs", state)
        self.assertEqual(state["createdEntities"], created_entities)
        for symbol in request.symbols:
            self.assertEqual([interval for current_symbol, interval in loader.calls if current_symbol == symbol], ["1M", "1W", "1D"])
        one_day = storage.assets[("NVDA", "1D")]
        self.assertEqual(one_day["assetVersion"], "v2")
        self.assertEqual(one_day["build"]["agentOutcome"], "not_requested_empty")
        self.assertLessEqual(len(json.dumps(one_day, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()), 12 * 1024)
        selected_ids = {item["candidateId"] for layer in one_day["layers"].values() for item in layer.get("selected", [])}
        self.assertTrue({item["id"] for item in one_day["features"]["levels"]}.issubset(selected_ids))
        self.assertTrue({item["id"] for item in one_day["features"]["trends"]}.issubset(selected_ids))

    def test_cancel_marks_remaining_items_skipped(self):
        request = envelope(symbols=("NVDA",), job_id="cab-12345678-cancel")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(cancel_after=1, progress=progress, job_id=request.job_id)
        state = ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        self.assertEqual(state["status"], "canceled")
        self.assertEqual(state["progress"]["done"], 3)
        self.assertEqual(state["progress"]["skipped"], 2)

    def test_rule_only_rebuild_does_not_copy_v1_agent_layer(self):
        existing_agent = {"drawings": [{"id": "kept"}], "intents": [], "rationale": "kept", "degraded": False, "model": "mock", "droppedIntents": []}
        existing = {("NVDA", "1D"): {
            "promptVersion": "prompt-v1", "status": "ready", "layers": {"agent": existing_agent},
            "chartSetup": {"recommended": [{"layer": "ema:20", "reason": "기존 LLM 제안", "source": "llm"}]},
            "commentary": {"text": "기존", "keyLevels": [], "invalidation": "기존", "confidence": 0.5, "enrichment": None},
        }}
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-preserve")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request); storage = FakeStorage(existing)
        ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        rebuilt = storage.assets[("NVDA", "1D")]
        self.assertEqual(rebuilt["layers"]["agent"]["drawings"], [])
        self.assertEqual(rebuilt["layers"]["agent"]["emptyReason"], "llm_not_requested")
        self.assertEqual(rebuilt["promptVersion"], "prompt-v2")
        self.assertEqual(rebuilt["build"]["agentOutcome"], "not_requested_empty")

    def test_standalone_1d_uses_stored_higher_timeframes(self):
        higher = {
            ("NVDA", "1M"): {"assetVersion":"v2","asOf": "2026-06-01T00:00:00.000Z", "quality":{"state":"eligible"},"build":{"ruleDigest":"m"},"features": {"regime": {}}},
            ("NVDA", "1W"): {"assetVersion":"v2","asOf": "2026-07-06T00:00:00.000Z", "quality":{"state":"eligible"},"build":{"ruleDigest":"w"},"features": {"regime": {}}},
        }
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-higher")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request); storage = FakeStorage(higher)
        ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)
        context = storage.assets[("NVDA", "1D")]["buildContext"]
        self.assertEqual(set(context["higherTf"]), {"1M", "1W"})
        self.assertEqual(context["flags"], [])

    def test_failure_list_is_not_truncated_with_recent_items(self):
        request = envelope(symbols=tuple(f"S{index}" for index in range(60)), intervals=("1D",), job_id="cab-12345678-failures")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        for index, symbol in enumerate(request.symbols):
            progress.record_item(request.job_id, {
                "symbol": symbol, "interval": "1D", "status": "failed",
                "stage": "kernel", "error": f"failure-{index}", "elapsedMs": 1,
            })
        state = progress.get(request.job_id)
        self.assertEqual(len(state["recentItems"]), 50)
        self.assertEqual(len(state["failedItems"]), 60)

    def test_second_identical_symbol_build_skips_kernel_llm_and_write(self):
        storage = FakeStorage(); service = FakeCurator(); loader = FakeCandleLoader()
        first = envelope(symbols=("NVDA",), job_id="cab-12345678-noop-a", llm_enabled=True)
        first_progress = InMemoryChartAssetProgressStore(); first_progress.initialize(first)
        ChartAssetBuilder(candle_loader=loader, storage=storage, progress=first_progress, llm_service=service, concurrency=1).run(first)
        saved_count = len(storage.saved)
        second = envelope(symbols=("NVDA",), job_id="cab-12345678-noop-b", llm_enabled=True)
        second_progress = RecordingProgressStore(); second_progress.initialize(second)
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=second_progress, llm_service=service, concurrency=1).run(second)
        self.assertEqual(service.calls, 1)
        self.assertEqual(len(storage.saved), saved_count)
        self.assertEqual({item["status"] for item in state["recentItems"]}, {"unchanged"})

        forced = envelope(symbols=("NVDA",), job_id="cab-12345678-noop-force", llm_enabled=True, force=True)
        forced_progress = InMemoryChartAssetProgressStore(); forced_progress.initialize(forced)
        forced_state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=forced_progress, llm_service=service, concurrency=1).run(forced)
        self.assertEqual(service.calls, 2, "force evaluates curator once")
        self.assertEqual(len(storage.saved), saved_count, "audit timestamps do not change content digest")
        self.assertEqual({item.get("reason") for item in forced_state["recentItems"]}, {"unchanged_after_force"})

    def test_empty_visual_palette_skips_llm_call(self):
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-empty-llm", llm_enabled=True)
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage(); curator = FakeCurator()
        empty_features = {
            "pivots": [], "levels": [], "trends": [], "events": [], "fibCandidates": [],
            "vp": {}, "regime": {"trend": "range", "atr14": 1}, "qualityFlags": [],
        }

        with patch("gops_agents.chart_assets.builder.compute_feature_pack", return_value=empty_features):
            ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, llm_service=curator, concurrency=1).run(request)

        self.assertEqual(curator.calls, 0)
        self.assertEqual(storage.saved[0]["build"]["llmSkippedReason"], "no_visual_candidates")
        self.assertIn("quality_empty", storage.saved[0]["quality"]["reasons"])

    def test_recent_data_outlier_saves_explicit_degraded_empty_asset(self):
        request = envelope(symbols=("NVDA",), intervals=("1D",), job_id="cab-12345678-data-blocked")
        progress = InMemoryChartAssetProgressStore(); progress.initialize(request)
        storage = FakeStorage()
        blocked_features = {
            "pivots": [], "levels": [], "trends": [], "events": [], "fibCandidates": [],
            "vp": {}, "regime": {"trend": "range", "atr14": 1},
            "qualityFlags": ["abnormal_true_range", "data_quality_blocked"],
        }

        with patch("gops_agents.chart_assets.builder.compute_feature_pack", return_value=blocked_features):
            state = ChartAssetBuilder(candle_loader=FakeCandleLoader(), storage=storage, progress=progress, concurrency=1).run(request)

        asset = storage.saved[0]
        self.assertEqual(state["status"], "completed_with_warnings")
        self.assertEqual(asset["status"], "degraded")
        self.assertEqual(asset["quality"]["state"], "insufficient_data")
        self.assertEqual(asset["commentary"]["emptyState"], "data_degraded")

    def test_llm_failure_preserves_valid_same_input_agent_layer(self):
        storage = FakeStorage(); selecting = SelectingCurator(); loader = FakeCandleLoader()
        first = envelope(symbols=("NVDA",), intervals=("1W",), job_id="cab-12345678-agent-first", llm_enabled=True)
        progress = InMemoryChartAssetProgressStore(); progress.initialize(first)
        ChartAssetBuilder(candle_loader=loader, storage=storage, progress=progress, llm_service=selecting, concurrency=1).run(first)
        original = copy.deepcopy(storage.assets[("NVDA", "1W")]["layers"]["agent"])
        self.assertTrue(original["drawings"])

        failing = FailingCurator()
        second = envelope(symbols=("NVDA",), intervals=("1W",), job_id="cab-12345678-agent-failure", llm_enabled=True, force=True)
        progress = InMemoryChartAssetProgressStore(); progress.initialize(second)
        ChartAssetBuilder(candle_loader=loader, storage=storage, progress=progress, llm_service=failing, concurrency=1).run(second)
        preserved = storage.assets[("NVDA", "1W")]["layers"]["agent"]

        self.assertEqual(failing.calls, 1)
        self.assertEqual(preserved["drawings"], original["drawings"])
        self.assertTrue(preserved["meta"]["preservedOnFailure"])
        self.assertEqual(preserved["meta"]["failureReason"], "llm_TimeoutError")

    def test_late_identical_intent_skips_llm_and_write_after_kernel(self):
        storage = FakeStorage(); service = FakeCurator(); loader = FakeCandleLoader()
        first = envelope(symbols=("NVDA",), job_id="cab-12345678-late-a", llm_enabled=True)
        first_progress = InMemoryChartAssetProgressStore(); first_progress.initialize(first)
        ChartAssetBuilder(candle_loader=loader, storage=storage, progress=first_progress, llm_service=service, concurrency=1).run(first)
        saved_count = len(storage.saved)
        for asset in storage.assets.values():
            asset["build"]["preKernelDigest"] = "sha256:" + "f" * 64

        second = envelope(symbols=("NVDA",), job_id="cab-12345678-late-b", llm_enabled=True)
        second_progress = RecordingProgressStore(); second_progress.initialize(second)
        state = ChartAssetBuilder(candle_loader=loader, storage=storage, progress=second_progress, llm_service=service, concurrency=1).run(second)

        self.assertEqual(loader.bundle_calls, 2)
        self.assertEqual(service.calls, 1, "late intent no-op skips the second curator call")
        self.assertEqual(len(storage.saved), saved_count)
        self.assertEqual({item.get("reason") for item in state["recentItems"]}, {"late_intent_unchanged"})
        self.assertTrue(any("unchanged after kernel" in line and "entities=" in line for line in second_progress.emitted_logs))


if __name__ == "__main__": unittest.main()
