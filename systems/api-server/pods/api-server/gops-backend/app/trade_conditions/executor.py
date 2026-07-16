from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from app.routes.orders import (
    _find_idempotent_response,
    _record_buy_spend,
    _repository_from_app as order_repository_from_app,
    _risk_verdict,
)
from app.routes.paper_trading import paper_repository_from_app
from app.trade_conditions.repository import TradeConditionRepository
from app.trade_conditions.routes import _repository_from_app as trade_condition_repository_from_app
from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.domain.envelope import build_order_command_envelope, validate_order_envelope
from kis_trader.domain.status import OrderContractError
from kis_trader.paper.models import PaperOrderError
from kis_trader.persistence.repository import IdempotencyConflictError
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash


DEFAULT_INPUT_TOPIC = "alerts.triggered.v1"
DEFAULT_GROUP_ID = "gops-trade-condition-executor-v1"


class TradeConditionExecutionBlocked(RuntimeError):
    pass


def process_trigger_event(app: Any, payload: dict[str, Any], *, mode: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("type") != "alert.price_cross":
        return {"status": "ignored", "reason": "unsupported_event"}
    if _simulation_replay_active(app):
        return {"status": "ignored", "reason": "simulation_replay_active"}
    try:
        alert_id = int(payload.get("alertId"))
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "missing_alert_id"}
    event_id = str(payload.get("eventId") or "").strip()
    if not event_id:
        return {"status": "ignored", "reason": "missing_event_id"}

    repository: TradeConditionRepository = trade_condition_repository_from_app(app)
    condition = repository.claim_trigger(alert_id, event_id, str(payload.get("triggeredAt") or "") or None)
    if condition is None:
        return {"status": "duplicate_or_unowned", "eventId": event_id}
    if not condition.get("execution_enabled", True):
        return {"status": "triggered", "condition": condition, "orderSubmitted": False}

    execution_mode = (mode or os.getenv("TRADE_CONDITION_EXECUTION_MODE", "off")).strip().lower()
    if execution_mode == "off":
        updated = repository.finish_execution(int(condition["id"]), status="triggered")
        return {"status": "triggered", "condition": updated, "orderSubmitted": False, "executionMode": "off"}
    try:
        if execution_mode in {"sim", "paper"}:
            order = _submit_paper_order(app, condition)
        elif execution_mode == "demo":
            order = _submit_demo_order(app, condition)
        else:
            raise ValueError(f"unsupported trade condition execution mode: {execution_mode}")
    except TradeConditionExecutionBlocked as exc:
        updated = repository.finish_execution(int(condition["id"]), status="blocked", error_reason=str(exc))
        return {"status": "blocked", "condition": updated, "orderSubmitted": False, "reason": str(exc)}
    except (OrderContractError, PaperOrderError, IdempotencyConflictError, ValueError) as exc:
        updated = repository.finish_execution(int(condition["id"]), status="failed", error_reason=str(exc))
        return {"status": "failed", "condition": updated, "orderSubmitted": False, "reason": str(exc)}

    order_id = str(order.get("order_id") or "").strip() or None
    updated = repository.finish_execution(int(condition["id"]), status="completed", order_id=order_id)
    return {
        "status": "completed",
        "condition": updated,
        "order": order,
        "orderSubmitted": True,
        "executionMode": execution_mode,
    }


def _submit_paper_order(app: Any, condition: dict[str, Any]) -> dict[str, Any]:
    payload = _order_payload(condition)
    request = validate_order_request_payload(payload, default_account_alias="paper-account")
    repository = paper_repository_from_app(app)
    risk = repository.pretrade(str(condition["user_sub"]), request)
    if risk.get("verdict") != "allow":
        rules = risk.get("triggeredRules") if isinstance(risk.get("triggeredRules"), list) else []
        reason = str(rules[0].get("explanation")) if rules and isinstance(rules[0], dict) else "가상계좌 리스크 검사에서 차단되었습니다."
        raise TradeConditionExecutionBlocked(reason)
    key = _execution_key(condition)
    result = repository.create_order(
        user_id=str(condition["user_sub"]),
        idempotency_key_hash=hash_idempotency_key(f"paper:{condition['user_sub']}:{key}"),
        body_hash=stable_body_hash(payload),
        request=request,
    )
    return dict(result.order)


def _submit_demo_order(app: Any, condition: dict[str, Any]) -> dict[str, Any]:
    payload = _order_payload(condition)
    payload["actor_id"] = str(condition["user_sub"])
    payload["role"] = "trader"
    order_request = validate_order_request_payload(payload, default_account_alias=os.getenv("KAFKA_ACCOUNT_ALIAS", "demo-account"))
    verdict = _risk_verdict(app, str(condition["user_sub"]), order_request, force=True)
    risk_required = os.getenv("TRADE_CONDITION_RISK_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if verdict is None and risk_required:
        raise TradeConditionExecutionBlocked("자동 예약 주문의 사전 리스크 검사를 완료하지 못했습니다.")
    if verdict is not None and verdict.verdict != "allow":
        reason = verdict.results[0].explanation if verdict.results else "사전 리스크 검사에서 차단되었습니다."
        raise TradeConditionExecutionBlocked(reason)

    repository = order_repository_from_app(app)
    key_hash = hash_idempotency_key(_execution_key(condition))
    body_hash = stable_body_hash(payload)
    replay = _find_idempotent_response(repository, key_hash, body_hash)
    if replay is not None:
        return replay
    envelope = build_order_command_envelope(order_request, occurred_at=datetime.now(timezone.utc).isoformat())
    command = validate_order_envelope(envelope)
    result = repository.create_received_order(
        idempotency_key_hash=key_hash,
        body_hash=body_hash,
        command=command,
        user_sub=str(condition["user_sub"]),
    )
    if not result.idempotent_replay:
        _record_buy_spend(app, str(condition["user_sub"]), order_request)
    return dict(result.response)


def _order_payload(condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": "overseas",
        "symbol": str(condition["symbol"]).upper(),
        "side": str(condition["side"]).lower(),
        "qty": str(condition["quantity"]),
        "price": str(condition["limit_price"]),
        "exchange": str(condition.get("exchange") or "NASD").upper(),
        "order_division": "00",
    }


def _execution_key(condition: dict[str, Any]) -> str:
    return f"trade-condition:{condition['id']}:trigger:{condition.get('version', 1)}"


def _simulation_replay_active(app: Any) -> bool:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return False
    client = getattr(app.state, "simulation_replay_redis", None)
    try:
        if client is None:
            import redis

            client = redis.from_url(redis_url, decode_responses=True)
            app.state.simulation_replay_redis = client
        return bool(client.get("simulator:replay:active-run"))
    except Exception:
        return False


def run() -> None:
    from kafka import TopicPartition
    from kafka.structs import OffsetAndMetadata

    from alfaka.common.kafka_io import create_json_consumer
    from app.main import create_app
    from kis_trader.runtime_heartbeat import touch_heartbeat

    app = create_app()
    topic = os.getenv("TRADE_CONDITION_TRIGGER_TOPIC", DEFAULT_INPUT_TOPIC)
    consumer = create_json_consumer(
        [topic],
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        os.getenv("TRADE_CONDITION_EXECUTOR_GROUP_ID", DEFAULT_GROUP_ID),
        os.getenv("TRADE_CONDITION_EXECUTOR_CLIENT_ID", "gops-trade-condition-executor"),
        enable_auto_commit=False,
        max_poll_records=os.getenv("TRADE_CONDITION_EXECUTOR_MAX_POLL_RECORDS", "100"),
    )
    touch_heartbeat()
    try:
        while True:
            batches = consumer.poll(timeout_ms=1000, max_records=100)
            if not batches:
                touch_heartbeat()
                continue
            for partition, messages in batches.items():
                for message in messages:
                    try:
                        result = process_trigger_event(app, message.value)
                        print(f"trade condition event={message.value.get('eventId')} result={result.get('status')}", flush=True)
                        consumer.commit({
                            TopicPartition(message.topic, message.partition): OffsetAndMetadata(message.offset + 1, "")
                        })
                    except Exception:
                        traceback.print_exc()
                        consumer.seek(partition, message.offset)
                        time.sleep(1)
                        break
                touch_heartbeat()
    finally:
        consumer.close()


if __name__ == "__main__":
    run()
