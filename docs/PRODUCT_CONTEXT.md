# GOPS Product Context

This file gives product direction for future Codex sessions and team members.
It is not a claim that every feature is already implemented.

## Product Sentence

**종목을 찾는 사람에게 기준을, 시장을 읽는 사람에게 방향을.**

GOPS combines real-time market data, company relationship context, and snapshot-based AI agents into a trading environment that can reorganize charts, news, comparisons, and order flow around user intent.

GOPS should not merely show more information. It should help users read market relationships and surface the screen they need.

## Implemented Foundation

The current repository contains:

- React frontend and chart engine.
- FastAPI REST/WebSocket API server.
- Alpaca market-data ingestion and historical backfill.
- Kafka-compatible stream processing.
- Redis, ClickHouse, and S3 market-data storage/serving.
- KIS demo order flow with API, outbox, adapter, migrations, and reconciliation pieces.
- Local Docker Compose and early AWS/EKS deployment assets.

## Product Direction

Expected future areas:

- Real-time charts and natural-language visualization.
- Snapshot-based multi-agent analysis with role-compatible UI labels where needed.
- Ontology-based company, sector, competitor, and supply-chain exploration.
- Dynamic UI composition based on user intent and market context.
- Watchlist, issue detection, and comparison analysis.
- Controlled flow from analysis to order placement and portfolio state.

## Technical Themes

- React, FastAPI, and WebSocket for real-time web interaction.
- Kafka/Flink-compatible streaming for market data.
- ClickHouse and Redis for time-series serving and low-latency cache.
- S3 for durable source data, replay, and evidence.
- GraphDB-backed relationship snapshots, with GraphRAG as a possible future extension.
- Idempotent and ordered order/execution handling.

## Codex Guidance

- Use this file for intent and naming.
- Do not implement future-facing features unless explicitly asked.
- Use `STRUCTURE_GUIDE.md` for code placement.
- Use `ARCHITECTURE.md`, `IMAGE_STRATEGY.md`, and `ENVIRONMENT.md` for runtime and deployment boundaries.
