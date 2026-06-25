from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any

from .auth import KisAuthError
from .client import KisApiError, KisOverseasClient
from .config import ConfigError, load_config
from .market import default_currency
from .models import DomesticOrderRequest, OverseasOrderRequest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(env=args.env, env_file=args.env_file)
        client = KisOverseasClient(config)
        payload = args.handler(args, client)
    except (ConfigError, KisApiError, KisAuthError, ValueError) as exc:
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

    return parser


def add_env_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", choices=["demo", "real"], default=argparse.SUPPRESS, help="Trading environment")
    parser.add_argument("--env-file", default=argparse.SUPPRESS, help="Path to .env file")


def handle_balance(args: argparse.Namespace, client: KisOverseasClient) -> dict[str, Any]:
    exchange = args.exchange or client.config.default_exchange
    currency = args.currency or default_currency(exchange)
    return client.balance(exchange=exchange, currency=currency)


def handle_domestic_balance(args: argparse.Namespace, client: KisOverseasClient) -> dict[str, Any]:
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


def handle_domestic_order(args: argparse.Namespace, client: KisOverseasClient) -> dict[str, Any]:
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

    return {"order": client.domestic_order(order_request)}


def handle_order(args: argparse.Namespace, client: KisOverseasClient) -> dict[str, Any]:
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

    result: dict[str, Any] = {"order": client.order(order_request)}
    if args.poll_ccnl:
        today = date.today()
        result["ccnl"] = client.order_history(
            start_date=today,
            end_date=today,
            symbol=order_request.symbol,
            side=order_request.side,
            exchange=order_request.exchange,
        )
    return result


def handle_ccnl(args: argparse.Namespace, client: KisOverseasClient) -> dict[str, Any]:
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


def parse_yyyymmdd(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}. Use YYYYMMDD.") from exc


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
