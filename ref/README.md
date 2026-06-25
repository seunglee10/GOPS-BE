# GOPS Chart

GOPS Chart is a local React + FastAPI market chart prototype. It combines a custom Canvas 2D chart, dummy US stock market data, a chat panel, and an LLM chart-proposal flow.

The application runs with separate frontend and backend dev servers. The backend owns market simulation and OpenAI API calls. The frontend owns the workspace document, chart rendering, user tools, proposal display, and command application.

## Main Features

- React + TypeScript frontend built with Vite.
- FastAPI backend with REST and WebSocket endpoints.
- Dummy market data for `AAPL`, `MSFT`, `NVDA`, `TSLA`, and `SPY`.
- Real-time candle updates over WebSocket.
- Custom Canvas 2D chart rendering for candles, volume, comparison lines, indicators, drawings, axes, crosshair, and proposal previews.
- Chart panel tools for symbol, timeframe, indicators, comparison symbols, horizontal lines, layer visibility, pin mode, and crosshair visibility.
- Chat panel that sends chart context, visible market summary, layers, panes, and available commands to the backend.
- Backend OpenAI Responses API integration with structured output.
- LLM chart proposals that can be accepted or rejected from the frontend.
- Command Engine for all document mutations.
- Shared capability manifest for frontend/backend command exposure.

## Application Shape

The frontend keeps a `WorkspaceDocument` as the main durable state. It contains panels, charts, proposals, and the command journal. User interactions and accepted LLM proposals mutate this document only through the Command Engine.

The active chart panel is composed of:

- `Toolbar`: symbol and timeframe controls.
- `ChartCanvas`: pointer interaction and Canvas rendering surface.
- `ChartPanelTools`: chart tools, indicators, comparison symbols, drawings, layer controls, and panel pin mode.
- `ChatPanel`: user questions and backend responses.
- `ProposalPanel`: pending LLM proposals, validation errors, accept, and reject.

The chart renderer builds a derived render scene from `ChartDocument`, market candles, calculation outputs, and runtime pointer state. Canvas renderers draw from that scene instead of mutating documents directly.

## Pin Mode And Proposals

Chart panels use `pinMode` to decide how LLM chart edits are handled:

- `locked`: LLM chart edit commands are not exposed.
- `approval`: validated LLM commands are returned as pending proposals.
- `auto`: validated LLM proposals are applied through the Command Engine automatically.

User actions do not create proposals. A user tool creates a command, dispatches it, and the Command Engine applies it immediately when validation passes.

LLM proposals use the same chart command types as user-facing tools, but the backend and frontend both filter LLM-accessible commands through `shared/chartCapabilities.json`.

## Frontend Structure

```txt
src/
  App.tsx                         App shell, market connection, chat request assembly, command dispatch
  main.tsx                        React entrypoint
  styles.css                      Application styles
  capabilities/
    chartCapabilities.ts          Frontend helper for shared capability manifest
  calculations/
    indicators.ts                 Indicator calculations
    indicatorRegistry.ts          Indicator definitions and input validation
    marketSummary.ts              Visible market summary calculations
  components/
    ChartPanel.tsx                Main chart panel composition
    ChartPanelTools.tsx           Panel-local chart controls
    ChatPanel.tsx                 Chat UI
    ProposalPanel.tsx             LLM proposal list and actions
    Toolbar.tsx                   Symbol/timeframe controls
  market/
    candleStore.ts                Snapshot/live candle merge logic
    marketClient.ts               REST snapshot and WebSocket client
  renderer/
    ChartCanvas.tsx               Canvas surface and pointer interaction
    canvasRenderer.ts             Canvas layer orchestration
    sceneBuilder.ts               Document/data to render-scene builder
    layerRendererRegistry.ts      Renderer registry
    timeScale*.ts                 Time scale helpers
    valueScaleModel.ts            Value scale helpers
    hitTest.ts                    Render hit testing
  state/
    commandEngine.ts              Validation, command application, proposal accept/reject
    commandRegistry.ts            Command definitions
    createDefaultWorkspace.ts     Initial workspace document
    proposalStore.ts              Proposal selectors
  types/
    *.ts                          Market, document, command, calculation, and LLM contracts
  tests/
    *.test.ts                     Frontend unit tests
```

## Backend Structure

```txt
backend/
  main.py                         FastAPI app and API routes
  dummy_market.py                 Deterministic dummy OHLCV market data
  market_stream.py                WebSocket stream loop
  market_summary.py               Backend market summary helpers
  llm_client.py                   OpenAI Responses API call and LLM output normalization
  schemas.py                      Pydantic request/response schemas
  settings.py                     .env loading for OpenAI settings
  tests/                          Backend tests
```

Backend endpoints:

- `GET /api/health`
- `GET /api/market/snapshot`
- `WS /ws/market`
- `POST /api/chat`

`POST /api/chat` reads `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_TIMEOUT_SECONDS` from `.env`. When no API key is configured, it returns a clear `503` response.

## Shared Capability Manifest

`shared/chartCapabilities.json` describes the chart capabilities currently exposed by the app. Each entry includes:

- `id`
- `label`
- `enabled`
- `commandTypes`
- `userEnabled`
- `llmEnabled`
- `notes`

The frontend uses this manifest to compute available LLM command types and to guard user command dispatch. The backend reads the same manifest to build the LLM command allowlist and prompt guidance.

## References

The `references/` directory contains source snapshots of external chart and indicator projects used as reading material.

```txt
references/
  README.md
  vendor-src/
    klinecharts/
    lightweight-charts/
    technicalindicators/
    uplot/
```

Reference inventory:

- `klinecharts`: pane, indicator, overlay, and drawing API design.
- `lightweight-charts`: financial Canvas rendering, time scale behavior, series APIs, and plugin model.
- `technicalindicators`: technical indicator formulas and calculation patterns.
- `uplot`: high-performance time-series rendering and scale handling.

These reference libraries are not runtime dependencies of GOPS Chart. Their source is kept for comparison and study.

## Local Setup

Install frontend dependencies:

```bash
npm install
```

Create the backend virtual environment and install Python dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `.env` in the project root:

```bash
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.5
OPENAI_TIMEOUT_SECONDS=20
```

## Local Commands

Start the backend:

```bash
npm run backend
```

Start the frontend:

```bash
npm run dev
```

Build and test:

```bash
npm run build
npm run test
npm run backend:test
```
