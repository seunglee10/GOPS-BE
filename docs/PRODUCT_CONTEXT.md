# GOPS Product Context

This file gives product direction for future contributors and AI agent sessions.
It is not a claim that every feature is already implemented.

## Product Sentence

**종목을 찾는 사람에게 기준을, 시장을 읽는 사람에게 방향을.**

GOPS combines real-time market data, company relationship context, and role-based AI agents into a trading environment that can reorganize charts, news, comparisons, and order flow around user intent.

GOPS should not merely show more information. It should help users read market relationships and surface the screen they need.

## Implemented Foundation

The current repository contains:

- React frontend and chart engine.
- FastAPI REST/WebSocket API server.
- Alpaca market-data ingestion and historical backfill.
- Kafka-compatible stream processing.
- Redis and ClickHouse market-data cache/storage/serving, with S3 as optional archive evidence.
- KIS demo order flow with API, outbox, adapter, migrations, and reconciliation pieces.
- Local Docker Compose and early AWS/EKS deployment assets.

## Product Direction

Expected future areas:

- Real-time charts and natural-language visualization.
- Role-based multi-agent analysis.
- Ontology-based company, sector, competitor, and supply-chain exploration.
- Dynamic UI composition based on user intent and market context.
- Watchlist, issue detection, and comparison analysis.
- Controlled flow from analysis to order placement and portfolio state.

## Technical Themes

- React, FastAPI, and WebSocket for real-time web interaction.
- Kafka/Flink-compatible streaming for market data.
- ClickHouse and Redis for time-series serving and low-latency cache.
- S3 for optional archive, recovery evidence, and teammate-isolated experiments.
- GraphDB/GraphRAG for future relationship reasoning.
- Idempotent and ordered order/execution handling.

## Contributor Guidance

- Use this file for intent and naming.
- Do not implement future-facing features unless explicitly asked.
- Use `STRUCTURE_GUIDE.md` for code placement.
- Use `ARCHITECTURE.md`, `IMAGE_STRATEGY.md`, and `ENVIRONMENT.md` for runtime and deployment boundaries.
