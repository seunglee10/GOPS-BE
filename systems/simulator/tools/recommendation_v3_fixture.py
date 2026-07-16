#!/usr/bin/env python3
"""Extract and verify the cutoff-safe July 14 V3 replay fixture.

Extraction is read-only: every ClickHouse statement is a SELECT.  The command
requires a short-lived operator token supplied through REPLAY_EXTRACTOR_TOKEN.
Verification uses only repository files and never contacts AWS or OpenAI.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems/api-server/pods/api-server/gops-backend"
sys.path.insert(0, str(BACKEND))

from app.recommendations.explanations import deterministic_explanation  # noqa: E402
from app.recommendations.professional_v3 import (  # noqa: E402
    ALGORITHM_VERSION,
    RULE_SET_VERSION,
    EvidenceContext,
    build_evidence_snapshot,
    process_evidence_preference_events,
    rank_evidence_candidates,
)


SCENARIO_ID = "recommendation-v3-2026-07-14"
SCENARIO_ROOT = ROOT / "systems/simulator/data/scenarios" / SCENARIO_ID
PREVIOUS_START = "2026-07-13 13:30:00"
CURRENT_START = "2026-07-14 13:30:00"
CURRENT_END = "2026-07-14 20:00:00"
SLOTS = [f"{hour:02d}:{minute:02d}" for hour in range(10, 16) for minute in (0, 30)]


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--authorization-token", required=True)
    extract.add_argument("--output", type=Path, default=SCENARIO_ROOT)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--fixture", type=Path, default=SCENARIO_ROOT)
    args = parser.parse_args()
    if args.command == "extract":
        authenticate(args.authorization_token)
        extract_fixture(args.output)
    else:
        verify_fixture(args.fixture)
    return 0


def authenticate(presented: str) -> None:
    expected = os.getenv("REPLAY_EXTRACTOR_TOKEN", "")
    if not expected or not hashlib.sha256(presented.encode()).digest() == hashlib.sha256(expected.encode()).digest():
        raise SystemExit("authenticated extractor token is missing or invalid")


class ClickHouseReadOnly:
    def __init__(self) -> None:
        self.url = os.environ["CLICKHOUSE_HTTP_URL"].rstrip("/")
        self.database = os.getenv("CLICKHOUSE_DATABASE", "market_data")
        self.user = os.getenv("CLICKHOUSE_USER", "default")
        self.password = os.getenv("CLICKHOUSE_PASSWORD", "")

    def rows(self, select_sql: str) -> list[dict[str, Any]]:
        normalized = select_sql.strip().rstrip(";")
        if not normalized.upper().startswith(("SELECT", "WITH")):
            raise ValueError("fixture extractor accepts SELECT statements only")
        request = urllib.request.Request(
            f"{self.url}/?{urllib.parse.urlencode({'database': self.database})}",
            data=f"{normalized} FORMAT JSONEachRow".encode(),
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return [json.loads(line) for line in response if line.strip()]


def extract_fixture(output: Path) -> None:
    client = ClickHouseReadOnly()
    spy_daily = client.rows(
        "SELECT event_time AS timestamp, open, high, low, close, volume, source, inserted_at "
        "FROM chart_candles FINAL WHERE symbol='SPY' AND interval='1D' "
        "AND event_time < toDateTime64('2026-07-14 04:00:00',3,'UTC') ORDER BY event_time"
    )
    spy_previous = client.rows(
        f"SELECT event_time AS timestamp, open, high, low, close, volume, source, inserted_at "
        f"FROM chart_candles FINAL WHERE symbol='SPY' AND interval='1m' "
        f"AND event_time >= toDateTime64('{PREVIOUS_START}',3,'UTC') "
        f"AND event_time < toDateTime64('{CURRENT_START}',3,'UTC') ORDER BY event_time"
    )
    spy_current = client.rows(
        f"SELECT event_time AS timestamp, open, high, low, close, volume, source, inserted_at "
        f"FROM chart_candles FINAL WHERE symbol='SPY' AND interval='1m' "
        f"AND event_time >= toDateTime64('{CURRENT_START}',3,'UTC') "
        f"AND event_time < toDateTime64('{CURRENT_END}',3,'UTC') ORDER BY event_time"
    )
    failures = []
    if len(spy_daily) < 252:
        failures.append(f"SPY daily history through July 13 is {len(spy_daily)}, expected at least 252")
    if len(spy_previous) < 380:
        failures.append(f"SPY previous session has {len(spy_previous)} candles, expected at least 380")
    if len(spy_current) < 380:
        failures.append(f"SPY July 14 session has {len(spy_current)} candles, expected at least 380")
    if failures:
        raise SystemExit("fixture extraction rejected:\n- " + "\n- ".join(failures))

    candidates = [row["symbol"] for row in client.rows(
        f"WITH p AS (SELECT symbol, count() c FROM chart_candles FINAL WHERE interval='1m' "
        f"AND event_time >= toDateTime64('{PREVIOUS_START}',3,'UTC') AND event_time < toDateTime64('{CURRENT_START}',3,'UTC') GROUP BY symbol), "
        f"c AS (SELECT symbol, count() c FROM chart_candles FINAL WHERE interval='1m' "
        f"AND event_time >= toDateTime64('{CURRENT_START}',3,'UTC') AND event_time < toDateTime64('{CURRENT_END}',3,'UTC') GROUP BY symbol), "
        "d AS (SELECT symbol, count() c FROM chart_candles FINAL WHERE interval='1D' "
        "AND event_time < toDateTime64('2026-07-14 04:00:00',3,'UTC') GROUP BY symbol) "
        "SELECT c.symbol FROM c INNER JOIN p USING symbol INNER JOIN d USING symbol "
        "WHERE c.symbol != 'SPY' AND c.c >= 60 AND p.c >= 380 AND d.c >= 252 ORDER BY c.symbol"
    )]
    if len(candidates) < 15:
        raise SystemExit(f"fixture extraction rejected: only {len(candidates)} complete candidate symbols")
    symbols = [*candidates, "SPY"]
    quoted = ",".join("'" + symbol + "'" for symbol in symbols)
    minute = client.rows(
        f"SELECT event_time AS timestamp, symbol, open, high, low, close, volume, source, inserted_at "
        f"FROM chart_candles WHERE interval='1m' AND symbol IN ({quoted}) "
        f"AND event_time >= toDateTime64('{PREVIOUS_START}',3,'UTC') "
        f"AND event_time < toDateTime64('{CURRENT_END}',3,'UTC') ORDER BY symbol,event_time"
    )
    daily = client.rows(
        f"SELECT event_time AS timestamp, symbol, open, high, low, close, volume, source, inserted_at, is_closed "
        f"FROM chart_candles WHERE interval='1D' AND symbol IN ({quoted}) "
        "AND event_time < toDateTime64('2026-07-14 04:00:00',3,'UTC') ORDER BY symbol,event_time"
    )
    quote_snapshots = {}
    for slot in SLOTS:
        cutoff = f"2026-07-14 {int(slot[:2]) + 4:02d}:{slot[3:]}:00"
        rows = client.rows(
            f"SELECT symbol, argMax(bid_price,event_time) bid, argMax(ask_price,event_time) ask, "
            f"max(event_time) availableAt FROM quote_ticks WHERE symbol IN ({quoted}) "
            f"AND event_time >= toDateTime64('{CURRENT_START}',3,'UTC') "
            f"AND event_time <= toDateTime64('{cutoff}',3,'UTC') GROUP BY symbol"
        )
        quote_snapshots[slot] = rows
    news = client.rows(
        f"SELECT published_at AS publishedAt, received_at AS availableAt, symbol, article_id AS articleId, "
        f"headline, summary, source FROM news_articles WHERE symbol IN ({quoted}) "
        "AND coalesce(received_at,published_at) <= toDateTime64('2026-07-14 20:00:00',3,'UTC') "
        "AND published_at >= toDateTime64('2026-07-01 00:00:00',3,'UTC') ORDER BY symbol,published_at"
    )
    dataset = {
        "schemaVersion": "recommendation-v3-replay-input.v1",
        "scenarioId": SCENARIO_ID,
        "timeZone": "America/New_York",
        "source": {"system": "AWS ClickHouse", "database": client.database, "readOnly": True},
        "universe": candidates,
        "minuteCandles": minute,
        "dailyCandles": daily,
        "quoteSnapshots": quote_snapshots,
        "news": news,
        "historicallyUnavailable": ["fundamentals", "tradability_history", "company_metadata_history"],
    }
    payloads = build_payloads(dataset)
    write_fixture(output, dataset, payloads)
    verify_fixture(output)


def build_payloads(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    minute_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    daily_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    news_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in dataset["minuteCandles"]:
        minute_by_symbol[row["symbol"]].append(normalize_candle(row))
    for row in dataset["dailyCandles"]:
        daily_by_symbol[row["symbol"]].append(normalize_candle(row))
    for row in dataset.get("news") or []:
        news_by_symbol[row["symbol"]].append(row)
    profile = SimpleNamespace(
        risk_level="balanced", recommendation_style="balanced", excluded_symbols=(), excluded_sectors=()
    )
    payloads = [{"phase": "open-0930", "payload": unavailable_payload("2026-07-14T09:30:00-04:00", "opening_data_accumulating")}]
    for index, slot in enumerate(SLOTS, start=1):
        cutoff = datetime.fromisoformat(f"2026-07-14T{slot}:00-04:00").astimezone(timezone.utc)
        quote_by_symbol = {row["symbol"]: row for row in dataset["quoteSnapshots"].get(slot, [])}
        market_items = []
        current_by_symbol = {}
        previous_by_symbol = {}
        for symbol in [*dataset["universe"], "SPY"]:
            rows = as_of_candles(minute_by_symbol[symbol], cutoff)
            current = [row for row in rows if CURRENT_START <= timestamp_sql(row) < CURRENT_END and parse_timestamp(row) <= cutoff]
            previous = [row for row in rows if PREVIOUS_START <= timestamp_sql(row) < CURRENT_START]
            current_by_symbol[symbol] = current
            previous_by_symbol[symbol] = previous
            if symbol == "SPY" or not current:
                continue
            quote = quote_by_symbol.get(symbol) or {}
            bid, ask = number(quote.get("bid")), number(quote.get("ask"))
            spread = ((ask - bid) / ((ask + bid) / 2) * 10_000) if bid and ask and ask >= bid else None
            market_items.append({
                "symbol": symbol,
                "sector": "Unclassified",
                "industry": "Unclassified",
                "changePercent": (number(current[-1]["close"]) / number(current[0]["open"]) - 1) * 100,
                "quotedSpreadBps": spread,
                "availableAt": current[-1]["timestamp"],
                "priceSource": "canonical",
            })
        daily_as_of = {symbol: as_of_candles(rows, cutoff) for symbol, rows in daily_by_symbol.items()}
        context = EvidenceContext(
            session_mode="regular", now=cutoff, market_items=market_items,
            candles_by_symbol=current_by_symbol, daily_candles_by_symbol=daily_as_of,
            previous_session_candles_by_symbol=previous_by_symbol,
            news_by_symbol={symbol: [row for row in news_by_symbol[symbol] if available_at(row) <= cutoff] for symbol in dataset["universe"]},
            fundamentals_by_symbol={}, fundamental_provenance={"status": "historically_unavailable"},
        )
        built = build_evidence_snapshot(context)
        preference, _events = process_evidence_preference_events(None, [], style="balanced", cutoff=cutoff)
        ranking = rank_evidence_candidates(
            built.candidates, profile=profile, preference_state=preference, risk_state={},
            watchlist_symbols=[], portfolio_positions=[], portfolio_snapshot=None,
            position_daily_candles={}, active_symbol=None, now=cutoff, snapshot_id=index,
        )
        if ranking.qualified_count < 15 or len(ranking.items) < 15:
            payload = unavailable_payload(cutoff.isoformat(), "candidate_data_not_ready", ranking.qualified_count)
        else:
            items = [dict(item, explanation=deterministic_explanation(item)) for item in ranking.items]
            for item in items:
                item["explanation"]["primary"]["generatedAt"] = cutoff.isoformat()
            payload = {
                "status": "completed", "slotStart": cutoff.isoformat(), "marketDate": "2026-07-14",
                "generatedAt": cutoff.isoformat(), "items": items,
                "summary": {"source": "repository-real-data-fixture", "sessionMode": "regular", "qualifiedCount": ranking.qualified_count,
                            "algorithmVersion": ALGORITHM_VERSION, "ruleSetVersion": RULE_SET_VERSION},
            }
        payloads.append({"phase": f"slot-{slot.replace(':', '')}", "payload": payload})
    payloads.append({"phase": "closed-1600", "payload": {
        "status": "market_closed", "slotStart": "2026-07-14T16:00:00-04:00", "marketDate": "2026-07-14",
        "items": [], "summary": {"emptyReason": "regular_not_active", "algorithmVersion": ALGORITHM_VERSION,
                                  "ruleSetVersion": RULE_SET_VERSION, "source": "repository-real-data-fixture"},
    }})
    return payloads


def write_fixture(output: Path, dataset: dict[str, Any], payloads: list[dict[str, Any]]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    phases = [{"id": "open-0930", "label": "09:30 · 데이터 누적", "atSeconds": 0, "marketCutoff": "2026-07-14T09:30:00-04:00"}]
    phases += [{"id": f"slot-{slot.replace(':', '')}", "label": f"{slot} · V3 추천", "atSeconds": index * 30,
                "marketCutoff": f"2026-07-14T{slot}:00-04:00"} for index, slot in enumerate(SLOTS, 1)]
    phases += [{"id": "closed-1600", "label": "16:00 · 장 마감", "atSeconds": 390, "marketCutoff": "2026-07-14T16:00:00-04:00"}]
    scenario = {
        "scenarioId": SCENARIO_ID, "title": "2026-07-14 실데이터 리플레이", "durationSeconds": 390,
        "timeZone": "America/New_York", "sourceLabel": "AWS canonical real market data",
        "seedPrices": {}, "phases": phases,
    }
    inputs_bytes = gzip.compress(canonical_json(dataset), mtime=0)
    recommendations_bytes = b"".join(canonical_json(row) + b"\n" for row in payloads)
    scenario_bytes = json.dumps(scenario, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    files = {"inputs.json.gz": inputs_bytes, "recommendations.jsonl": recommendations_bytes, "scenario.json": scenario_bytes}
    for name, content in files.items():
        (output / name).write_bytes(content)
    manifest = {
        "schemaVersion": "recommendation-v3-replay-manifest.v1", "scenarioId": SCENARIO_ID,
        "provenance": dataset["source"], "cutoffPolicy": "availableAt <= active cutoff; no later candles",
        "algorithmVersion": ALGORITHM_VERSION, "ruleSetVersion": RULE_SET_VERSION,
        "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)} for name, content in files.items()},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_fixture(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, metadata in manifest["files"].items():
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if digest != metadata["sha256"]:
            raise SystemExit(f"hash mismatch: {name}")
    dataset = json.loads(gzip.decompress((root / "inputs.json.gz").read_bytes()))
    expected = [json.loads(line) for line in (root / "recommendations.jsonl").read_text(encoding="utf-8").splitlines() if line]
    actual = build_payloads(dataset)
    if canonical_json(actual) != canonical_json(expected):
        raise SystemExit("offline replay output differs from frozen rankings/explanations")
    print(f"verified {len(expected)} phases; hashes and V3 outputs are identical")


def unavailable_payload(cutoff: str, reason: str, qualified: int = 0) -> dict[str, Any]:
    return {"status": "data_not_ready", "slotStart": cutoff, "marketDate": "2026-07-14", "items": [],
            "summary": {"emptyReason": reason, "qualifiedCount": qualified, "algorithmVersion": ALGORITHM_VERSION,
                        "ruleSetVersion": RULE_SET_VERSION, "source": "repository-real-data-fixture"}}


def normalize_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": iso_timestamp(row["timestamp"]), "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"], "sourceClass": "canonical", "availableAt": iso_timestamp(row.get("inserted_at") or row["timestamp"])}


def parse_timestamp(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))


def timestamp_sql(row: dict[str, Any]) -> str:
    return parse_timestamp(row).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso_timestamp(value: Any) -> str:
    text = str(value).replace(" ", "T")
    return text if text.endswith("Z") or "+" in text[10:] else text + "Z"


def available_at(row: dict[str, Any]) -> datetime:
    value = row.get("availableAt") or row.get("publishedAt")
    return datetime.fromisoformat(iso_timestamp(value).replace("Z", "+00:00"))


def as_of_candles(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    selected: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for row in rows:
        observed = available_at(row)
        if observed > cutoff:
            continue
        key = str(row["timestamp"])
        if key not in selected or observed > selected[key][0]:
            selected[key] = (observed, row)
    return [selected[key][1] for key in sorted(selected)]


def number(value: Any) -> float:
    return float(value or 0)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


if __name__ == "__main__":
    raise SystemExit(main())
