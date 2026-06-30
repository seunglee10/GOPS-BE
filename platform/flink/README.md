# Flink / Stream Processing Platform Contract

Current repository stage:

```text
python -u systems/market-data/pods/market-processor/local_main.py
infra/k8s/base/deployment-market-processor.yaml
```

Staged path:

```text
local Python processor -> explicit Python processor pod -> Flink or managed Flink candidate
```

Do not assume managed Flink is the immediate next step.
