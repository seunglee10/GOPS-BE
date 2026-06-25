from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any

from .auth import KisAuthError
from .client import KisApiError, KisOverseasClient
from .config import ConfigError, KisConfig, load_config
from .fake_kis import FakeKisClient
from .kafka_consumer import run_broker_adapter_consumer
from .kafka_producer import (
    KafkaPublishError,
    publish_domestic_order_command,
    publish_order_command_payload,
    publish_overseas_order_command,
)
from .market import default_currency
from .metrics import collect_operational_metrics
from .models import DomesticOrderRequest, OverseasOrderRequest
from .outbox import publish_pending_outbox
from .reconciler import poll_and_reconcile_order_history
from .repository import PostgresOrderRepository


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        needs_kis_client = getattr(args, "needs_kis_client", True)
        config = load_config(
            env=args.env,
            env_file=args.env_file,
            require_kis_credentials=needs_kis_client,
        )
        client = KisOverseasClient(config) if needs_kis_client else None
        payload = args.handler(args, config, client)
    except (ConfigError, KisApiError, KisAuthError, KafkaPublishError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")

    if payload is not None:
        print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KIS stock trading skeleton")
    parser.add_argument("--env", choices=["demo", "real"], default=None, help="Trading environment")
    parser.add_argument("--env-file", default=None, help="Path to .env file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    balance = subparsers.add_parser("balance", help="Query overseas stock balance")
    add_env_args(balance)
    balance.add_argument("--exchange", default=None, help="Exchange code, e.g. NASD")
    balance.add_argument("--currency", default=None, help="Currency code, e.g. USD")
    balance.set_defaults(handler=handle_balance)

    domestic_balance = subparsers.add_parser("domestic-balance", help="Query domestic stock balance")
    add_env_args(domestic_balance)
    domestic_balance.add_argument(
        "--afhr-flpr-yn",
        default="N",
        choices=["N", "Y", "X"],
        help="N: regular, Y: after-hours single price, X: NXT",
    )
    domestic_balance.add_argument(
        "--inqr-dvsn",
        default="02",
        choices=["01", "02"],
        help="01: by loan date, 02: by stock",
    )
    domestic_balance.add_argument("--unpr-dvsn", default="01", help="Unit price division")
    domestic_balance.add_argument(
        "--fund-sttl-icld-yn",
        default="N",
        choices=["N", "Y"],
        help="Include fund settlement amount",
    )
    domestic_balance.add_argument(
        "--fncg-amt-auto-rdpt-yn",
        default="N",
        choices=["N", "Y"],
        help="Auto-redeem financing amount",
    )
    domestic_balance.add_argument(
        "--prcs-dvsn",
        default="00",
        choices=["00", "01"],
        help="00: include previous day trades, 01: exclude previous day trades",
    )
    domestic_balance.add_argument("--ctx-area-fk100", default="", help="Continuation search condition")
    domestic_balance.add_argument("--ctx-area-nk100", default="", help="Continuation key")
    domestic_balance.add_argument("--tr-cont", default="", help="Continuation header value")
    domestic_balance.set_defaults(handler=handle_domestic_balance)

    domestic_order = subparsers.add_parser("domestic-order", help="Preview or submit a domestic stock order")
    add_env_args(domestic_order)
    domestic_order.add_argument("--symbol", required=True, help="Domestic stock code, e.g. 005930")
    domestic_order.add_argument("--side", choices=["buy", "sell"], default="buy")
    domestic_order.add_argument("--qty", required=True, help="Order quantity")
    domestic_order.add_argument("--price", required=True, help="Limit price. Use 0 only for supported market orders.")
    domestic_order.add_argument("--exchange", default="KRX", help="Exchange ID division code. Defaults to KRX.")
    domestic_order.add_argument("--ord-dvsn", default="00", help="Order division. 00 means limit order.")
    domestic_order.add_argument("--sll-type", default="", help="Sell type for sell orders, e.g. 01 for normal sell.")
    domestic_order.add_argument("--cndt-pric", default="", help="Condition price for supported conditional orders.")
    domestic_order.add_argument("--submit", action="store_true", help="Actually send the order API request")
    domestic_order.add_argument(
        "--confirm-real-order",
        default="",
        help="Required literal REAL_ORDER when --env real and --submit are used",
    )
    domestic_order.set_defaults(handler=handle_domestic_order)

    order = subparsers.add_parser("order", help="Preview or submit an overseas stock order")
    add_env_args(order)
    order.add_argument("--symbol", required=True, help="Ticker/product number, e.g. AAPL")
    order.add_argument("--side", choices=["buy", "sell"], default="buy")
    order.add_argument("--qty", required=True, help="Order quantity")
    order.add_argument("--price", required=True, help="Limit price. Use 0 only for supported market orders.")
    order.add_argument("--exchange", default=None, help="Exchange code. Defaults to KIS_DEFAULT_EXCHANGE.")
    order.add_argument("--ord-dvsn", default="00", help="Order division. 00 means limit order.")
    order.add_argument("--submit", action="store_true", help="Actually send the order API request")
    order.add_argument(
        "--confirm-real-order",
        default="",
        help="Required literal REAL_ORDER when --env real and --submit are used",
    )
    order.add_argument("--poll-ccnl", action="store_true", help="Query today's order history after submit")
    order.set_defaults(handler=handle_order)

    ccnl = subparsers.add_parser("ccnl", help="Query overseas stock order/fill history")
    add_env_args(ccnl)
    ccnl.add_argument("--start-date", default=None, help="YYYYMMDD. Defaults to today.")
    ccnl.add_argument("--end-date", default=None, help="YYYYMMDD. Defaults to start-date.")
    ccnl.add_argument("--symbol", default=None, help="Real env only. Defaults to all.")
    ccnl.add_argument("--side", choices=["buy", "sell"], default=None, help="Real env only.")
    ccnl.add_argument("--exchange", default=None, help="Real env only. Defaults to KIS_DEFAULT_EXCHANGE.")
    ccnl.add_argument("--fill-status", choices=["00", "01", "02"], default="00")
    ccnl.set_defaults(handler=handle_ccnl)

    sample_command = subparsers.add_parser(
        "emit-sample-command",
        help="Publish a sample order command to Kafka without calling KIS",
    )
    add_env_args(sample_command)
    sample_command.add_argument("--market", choices=["domestic", "overseas"], default="overseas")
    sample_command.add_argument("--symbol", default=None, help="Defaults to AAPL or 005930 by market")
    sample_command.add_argument("--side", choices=["buy", "sell"], default="buy")
    sample_command.add_argument("--qty", default="1")
    sample_command.add_argument("--price", default=None, help="Defaults to 145.00 or 70000 by market")
    sample_command.add_argument("--exchange", default=None, help="Defaults to NASD or KRX by market")
    sample_command.set_defaults(handler=handle_emit_sample_command, needs_kis_client=False)

    db_init = subparsers.add_parser("db-init", help="Create or update PostgreSQL tables")
    add_env_args(db_init)
    db_init.set_defaults(handler=handle_db_init, needs_kis_client=False)

    broker_adapter = subparsers.add_parser("broker-adapter", help="Run the KIS Broker Adapter Kafka consumer")
    add_env_args(broker_adapter)
    broker_adapter.add_argument("--max-messages", type=int, default=None, help="Stop after N messages")
    broker_adapter.add_argument("--poll-timeout", type=float, default=1.0, help="Kafka poll timeout seconds")
    broker_adapter.add_argument(
        "--crash-before-process",
        action="store_true",
        help="Smoke test only: exit after polling before DB/KIS processing",
    )
    broker_adapter.add_argument(
        "--crash-after-process",
        action="store_true",
        help="Smoke test only: exit after DB/outbox commit before Kafka offset commit",
    )
    broker_adapter.add_argument(
        "--fake-kis",
        choices=["success", "reject", "timeout"],
        default=None,
        help="Use a fake KIS client for smoke tests without external order submission",
    )
    broker_adapter.set_defaults(handler=handle_broker_adapter, needs_kis_client=False)

    outbox = subparsers.add_parser("outbox-publish", help="Publish pending outbox events to Kafka")
    add_env_args(outbox)
    outbox.add_argument("--limit", type=int, default=100, help="Maximum events to publish")
    outbox.set_defaults(handler=handle_outbox_publish, needs_kis_client=False)

    dlq_reprocess = subparsers.add_parser("dlq-reprocess", help="Re-publish a corrected DLQ order command")
    add_env_args(dlq_reprocess)
    dlq_reprocess.add_argument("--event-id", default=None, help="DLQ outbox event id. Use --latest for newest.")
    dlq_reprocess.add_argument("--latest", action="store_true", help="Reprocess the newest DLQ event for the configured DLQ topic")
    dlq_reprocess.add_argument("--set-market", choices=["domestic", "overseas"], default=None)
    dlq_reprocess.add_argument("--set-symbol", default=None)
    dlq_reprocess.add_argument("--set-side", choices=["buy", "sell"], default=None)
    dlq_reprocess.add_argument("--set-qty", default=None)
    dlq_reprocess.add_argument("--set-price", default=None)
    dlq_reprocess.add_argument("--set-exchange", default=None)
    dlq_reprocess.set_defaults(handler=handle_dlq_reprocess, needs_kis_client=False)

    poll_events = subparsers.add_parser("poll-order-events", help="Poll KIS order/fill history and reconcile unknown orders")
    add_env_args(poll_events)
    poll_events.add_argument("--market", choices=["domestic", "overseas"], required=True)
    poll_events.add_argument("--start-date", default=None, help="YYYYMMDD. Defaults to today.")
    poll_events.add_argument("--end-date", default=None, help="YYYYMMDD. Defaults to start-date.")
    poll_events.add_argument(
        "--fake-kis",
        action="store_true",
        help="Use a fake KIS order history response for smoke tests",
    )
    poll_events.set_defaults(handler=handle_poll_order_events, needs_kis_client=False)

    metrics = subparsers.add_parser("ops-metrics", help="Show order pipeline reliability metrics")
    add_env_args(metrics)
    metrics.add_argument("--skip-kafka", action="store_true", help="Only read PostgreSQL metrics")
    metrics.set_defaults(handler=handle_ops_metrics, needs_kis_client=False)

    return parser


def add_env_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", choices=["demo", "real"], default=argparse.SUPPRESS, help="Trading environment")
    parser.add_argument("--env-file", default=argparse.SUPPRESS, help="Path to .env file")


def handle_balance(args: argparse.Namespace, config: KisConfig, client: KisOverseasClient | None) -> dict[str, Any]:
    assert client is not None
    exchange = args.exchange or client.config.default_exchange
    currency = args.currency or default_currency(exchange)
    return client.balance(exchange=exchange, currency=currency)


def handle_domestic_balance(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    assert client is not None
    return client.domestic_balance(
        afhr_flpr_yn=args.afhr_flpr_yn,
        inqr_dvsn=args.inqr_dvsn,
        unpr_dvsn=args.unpr_dvsn,
        fund_sttl_icld_yn=args.fund_sttl_icld_yn,
        fncg_amt_auto_rdpt_yn=args.fncg_amt_auto_rdpt_yn,
        prcs_dvsn=args.prcs_dvsn,
        ctx_area_fk100=args.ctx_area_fk100,
        ctx_area_nk100=args.ctx_area_nk100,
        tr_cont=args.tr_cont,
    )


def handle_domestic_order(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    assert client is not None
    order_request = DomesticOrderRequest.from_strings(
        symbol=args.symbol,
        side=args.side,
        qty=args.qty,
        price=args.price,
        exchange=args.exchange,
        order_division=args.ord_dvsn,
        sell_type=args.sll_type,
        condition_price=args.cndt_pric,
    )

    if not args.submit:
        return {
            "dry_run": True,
            "message": "Order was not submitted. Add --submit to send it.",
            "preview": client.preview_domestic_order(order_request),
        }

    if client.config.env == "real" and args.confirm_real_order != "REAL_ORDER":
        raise ValueError("--env real --submit requires --confirm-real-order REAL_ORDER.")

    return publish_domestic_order_command(client.config, order_request).to_dict()


def handle_order(args: argparse.Namespace, config: KisConfig, client: KisOverseasClient | None) -> dict[str, Any]:
    assert client is not None
    exchange = args.exchange or client.config.default_exchange
    order_request = OverseasOrderRequest.from_strings(
        symbol=args.symbol,
        side=args.side,
        qty=args.qty,
        price=args.price,
        exchange=exchange,
        order_division=args.ord_dvsn,
    )

    if not args.submit:
        return {
            "dry_run": True,
            "message": "Order was not submitted. Add --submit to send it.",
            "preview": client.preview_order(order_request),
        }

    if client.config.env == "real" and args.confirm_real_order != "REAL_ORDER":
        raise ValueError("--env real --submit requires --confirm-real-order REAL_ORDER.")

    if args.poll_ccnl:
        raise ValueError("--poll-ccnl cannot be used because --submit now publishes to Kafka.")

    return publish_overseas_order_command(client.config, order_request).to_dict()


def handle_ccnl(args: argparse.Namespace, config: KisConfig, client: KisOverseasClient | None) -> dict[str, Any]:
    assert client is not None
    start_date = parse_yyyymmdd(args.start_date) if args.start_date else date.today()
    end_date = parse_yyyymmdd(args.end_date) if args.end_date else start_date
    return client.order_history(
        start_date=start_date,
        end_date=end_date,
        symbol=args.symbol,
        side=args.side,
        exchange=args.exchange,
        fill_status=args.fill_status,
    )


def handle_emit_sample_command(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    if args.market == "domestic":
        symbol = args.symbol or "005930"
        payload = {
            "market": "domestic",
            "symbol": symbol,
            "side": args.side,
            "qty": args.qty,
            "price": args.price or "70000",
            "exchange": args.exchange or "KRX",
            "order_division": "00",
            "sell_type": "",
            "condition_price": "",
        }
    else:
        symbol = args.symbol or "AAPL"
        payload = {
            "market": "overseas",
            "symbol": symbol,
            "side": args.side,
            "qty": args.qty,
            "price": args.price or "145.00",
            "exchange": args.exchange or config.default_exchange,
            "order_division": "00",
        }
    return publish_order_command_payload(
        config,
        payload=payload,
        symbol=symbol,
        producer_name="kis-trader-smoke",
    ).to_dict()


def handle_db_init(args: argparse.Namespace, config: KisConfig, client: KisOverseasClient | None) -> dict[str, Any]:
    repository = PostgresOrderRepository(config.database_url)
    repository.ensure_schema()
    return {"database_initialized": True, "database_url": _redact_database_url(config.database_url)}


def handle_broker_adapter(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    selected_client = _client_for_optional_fake(config, client, fake_mode=args.fake_kis)
    summary = run_broker_adapter_consumer(
        config=config,
        client=selected_client,
        max_messages=args.max_messages,
        poll_timeout_seconds=args.poll_timeout,
        crash_before_process=args.crash_before_process,
        crash_after_process=args.crash_after_process,
    )
    return summary.__dict__


def handle_outbox_publish(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    repository = PostgresOrderRepository(config.database_url)
    repository.ensure_schema()
    summary = publish_pending_outbox(config=config, repository=repository, limit=args.limit)
    return summary.__dict__


def handle_dlq_reprocess(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    repository = PostgresOrderRepository(config.database_url)
    repository.ensure_schema()
    dlq_event = repository.fetch_dlq_event(
        topic=config.kafka_dlq_topic,
        event_id=args.event_id,
        latest=args.latest,
    )
    dlq_payload = dlq_event["payload"]
    original = dlq_payload.get("payload")
    if not isinstance(original, dict):
        raise ValueError("DLQ event payload does not contain an original command object.")
    command_payload = original.get("payload")
    if not isinstance(command_payload, dict):
        raise ValueError("Original command does not contain a payload object.")

    patched_command = dict(command_payload)
    for option_name, field_name in (
        ("set_market", "market"),
        ("set_symbol", "symbol"),
        ("set_side", "side"),
        ("set_qty", "qty"),
        ("set_price", "price"),
        ("set_exchange", "exchange"),
    ):
        option_value = getattr(args, option_name)
        if option_value is not None:
            patched_command[field_name] = option_value

    symbol = str(patched_command.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("Corrected DLQ command requires symbol.")

    corrected = dict(original)
    corrected["payload"] = patched_command
    corrected["producer"] = "kis-trader-dlq-reprocessor"
    corrected["account_alias"] = config.kafka_account_alias

    result = publish_order_command_payload(
        config,
        payload=patched_command,
        symbol=symbol,
        producer_name="kis-trader-dlq-reprocessor",
    )
    return {
        "reprocessed": True,
        "source_dlq_event_id": dlq_event["event_id"],
        "published": result.to_dict(),
    }


def handle_poll_order_events(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    selected_client = _client_for_optional_fake(config, client, fake_mode="success" if args.fake_kis else None)
    start_date = parse_yyyymmdd(args.start_date) if args.start_date else date.today()
    end_date = parse_yyyymmdd(args.end_date) if args.end_date else start_date
    summary = poll_and_reconcile_order_history(
        config=config,
        client=selected_client,
        market=args.market,
        start_date=start_date,
        end_date=end_date,
    )
    return summary.__dict__


def handle_ops_metrics(
    args: argparse.Namespace,
    config: KisConfig,
    client: KisOverseasClient | None,
) -> dict[str, Any]:
    repository = PostgresOrderRepository(config.database_url)
    repository.ensure_schema()
    metrics = collect_operational_metrics(config, include_kafka=not args.skip_kafka)
    return metrics.to_dict()


def parse_yyyymmdd(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}. Use YYYYMMDD.") from exc


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _redact_database_url(value: str) -> str:
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    return f"{scheme}://[REDACTED]@{rest.split('@', 1)[1]}"


def _client_for_optional_fake(
    config: KisConfig,
    client: KisOverseasClient | None,
    *,
    fake_mode: str | None,
) -> Any:
    if fake_mode:
        return FakeKisClient(config, mode=fake_mode)
    if not config.app_key or not config.app_secret or not config.account_no:
        raise ConfigError("KIS credentials are required unless --fake-kis is used.")
    return client or KisOverseasClient(config)
