# GOPS Backend Scaffold

Minimal FastAPI scaffold for the GOPS proof of concept.

## Run

```bash
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload
```

## Endpoint

- `GET /health`: returns a JSON health response.

OpenAI, WebSocket, market data, order, and server-side layout persistence APIs are intentionally excluded from this scaffold.
