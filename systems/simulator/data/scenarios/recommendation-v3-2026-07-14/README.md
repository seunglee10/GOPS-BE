# July 14 V3 real-data fixture

`scenario.json` defines the stable replay timeline. The extractor replaces this
directory with hash-verified real inputs and frozen V3 outputs only after every
SPY and candidate gate passes:

```bash
REPLAY_EXTRACTOR_TOKEN=... \
CLICKHOUSE_HTTP_URL=... CLICKHOUSE_USER=... CLICKHOUSE_PASSWORD=... \
.venv/bin/python systems/simulator/tools/recommendation_v3_fixture.py extract \
  --authorization-token ...

.venv/bin/python systems/simulator/tools/recommendation_v3_fixture.py verify
```

Until extraction succeeds, the offline fixture is intentionally incomplete and
cannot pass `verify`. It is not connected to the current tick-replay runtime;
that runtime continues to return `simulation_data_unavailable` for recommendations
and never substitutes synthetic candles or a live OpenAI narrative.
