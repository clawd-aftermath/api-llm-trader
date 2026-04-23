#!/usr/bin/env python3
"""Write operations for Aftermath Perpetuals.

Pattern: python3 scripts/trade.py <group> <action> [args]
"""

import json
import os
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _api import USDC_COIN_TYPE, af_post, get_config_value
from _cli import JsonArgumentParser, error, output
from _signing import build_sign_submit
from _symbols import normalize_side, resolve_symbol, scale_price, scale_size


VALID_ORDER_TYPES = {0, 1, 2, 3}
SIDE_CHOICES = ["buy", "sell", "long", "short"]


def require_config(name):
    value = get_config_value(name)
    if not value:
        error(f"missing {name}; set it as an env var or in credentials")
    return value


def get_account_id():
    raw = require_config("AFTERMATH_ACCOUNT_ID")
    try:
        return int(raw)
    except (TypeError, ValueError):
        error("AFTERMATH_ACCOUNT_ID must be an integer")


def get_wallet():
    return str(require_config("AFTERMATH_WALLET_ADDRESS"))


def account_identifier():
    fields = {"accountId": get_account_id()}
    account_cap_id = get_config_value("AFTERMATH_ACCOUNT_CAP_ID")
    if account_cap_id:
        fields["accountCapId"] = str(account_cap_id)
    return fields


def native_tx(path, body):
    try:
        resp = af_post(path, body)
    except Exception as exc:
        error(str(exc))

    if not isinstance(resp, dict):
        error(f"unexpected response from {path}: expected object")

    if resp.get("error"):
        error(f"native endpoint error for {path}: {resp['error']}")

    if not resp.get("txKind"):
        error(f"missing txKind in response from {path}")

    return resp


def execute_native_tx(path, body):
    # Ensure key exists before attempting signing flow.
    require_config("AFTERMATH_PRIVATE_KEY")
    wallet = get_wallet()

    preview = native_tx(path, body)
    try:
        execution = build_sign_submit(
            preview["txKind"],
            wallet_address=wallet,
            sponsor_signature=preview.get("sponsorSignature"),
        )
    except Exception as exc:
        error(str(exc))

    return {
        "ok": True,
        "endpoint": path,
        "request": body,
        "preview": preview,
        "execution": execution,
    }


def _parse_csv_ints(raw, field_name):
    items = [x.strip() for x in str(raw).split(",") if x.strip()]
    if not items:
        error(f"{field_name} must contain at least one ID")
    try:
        return [f"{int(x)}n" for x in items]
    except ValueError:
        error(f"invalid {field_name}; expected comma-separated integers")


def _scaled_usdc_amount(amount):
    try:
        val = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        error("amount must be numeric")
    if val <= 0:
        error("amount must be > 0")
    return int((val * Decimal("1000000")).to_integral_value())


def _market_context(symbol):
    try:
        market_id, resolved_symbol, market = resolve_symbol(symbol)
    except ValueError as exc:
        error(str(exc))

    params = market.get("marketParams", {})
    lot_size = params.get("lotSize")
    tick_size = params.get("tickSize")
    if not lot_size or not tick_size:
        error(f"missing market params for {resolved_symbol} ({market_id})")

    return market_id, resolved_symbol, lot_size, tick_size


def _resolve_orders_to_place(args, lot_size, tick_size):
    if args.orders:
        try:
            rows = json.loads(args.orders)
        except json.JSONDecodeError as exc:
            error(f"invalid --orders JSON: {exc}")
        if not isinstance(rows, list) or not rows:
            error("--orders must be a non-empty JSON array")

        normalized = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                error(f"--orders[{idx}] must be an object")

            raw_side = row.get("side")
            if raw_side in (0, 1):
                side = int(raw_side)
            else:
                try:
                    side = normalize_side(raw_side)
                except Exception as exc:
                    error(f"--orders[{idx}].side invalid: {exc}")

            raw_size = row.get("size")
            raw_price = row.get("price")
            if raw_size is None or raw_price is None:
                error(f"--orders[{idx}] requires size and price")

            if isinstance(raw_size, str) and raw_size.endswith("n"):
                size = raw_size
            else:
                size = scale_size(float(raw_size), lot_size)

            if isinstance(raw_price, str) and raw_price.endswith("n"):
                price = raw_price
            else:
                price = scale_price(float(raw_price), tick_size, side=side)

            normalized.append({"side": side, "price": price, "size": size})
        return normalized

    if args.side is None or args.size is None or args.price is None:
        error("provide either --orders JSON or all of --side --size --price")

    return [{
        "side": normalize_side(args.side),
        "price": scale_price(args.price, tick_size, side=normalize_side(args.side)),
        "size": scale_size(args.size, lot_size),
    }]


def cmd_order_market(args):
    market_id, symbol, lot_size, _ = _market_context(args.symbol)
    body = {
        **account_identifier(),
        "walletAddress": get_wallet(),
        "marketId": market_id,
        "side": normalize_side(args.side),
        "size": scale_size(args.size, lot_size),
        "slippage": args.slippage,
        "reduceOnly": args.reduce_only,
        "hasPosition": True,
        "collateralChange": 0,
        "cancelSlTp": False,
        "sponsor": None,
        "txKind": None,
        "builderCode": None,
        "slTp": None,
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/place-market-order",
        body,
    )
    output({"symbol": symbol, "marketId": market_id, **result})


def cmd_order_limit(args):
    market_id, symbol, lot_size, tick_size = _market_context(args.symbol)
    order_type = 2 if args.post_only else args.order_type
    if order_type not in VALID_ORDER_TYPES:
        error("order_type must be one of: 0 (GTC), 1 (FOK), 2 (PostOnly), 3 (IOC)")

    body = {
        **account_identifier(),
        "walletAddress": get_wallet(),
        "marketId": market_id,
        "side": normalize_side(args.side),
        "size": scale_size(args.size, lot_size),
        "price": scale_price(args.price, tick_size, side=normalize_side(args.side)),
        "orderType": order_type,
        "reduceOnly": args.reduce_only,
        "hasPosition": True,
        "collateralChange": 0,
        "cancelSlTp": False,
        "sponsor": None,
        "txKind": None,
        "builderCode": None,
        "slTp": None,
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/place-limit-order",
        body,
    )
    output({"symbol": symbol, "marketId": market_id, **result})


def cmd_order_cancel(args):
    market_id, symbol, _, _ = _market_context(args.symbol)
    body = {
        **account_identifier(),
        "walletAddress": get_wallet(),
        "marketIdsToData": {
            market_id: {
                "orderIds": _parse_csv_ints(args.order_ids, "order_ids"),
                "collateralChange": 0,
            }
        },
        "sponsor": None,
        "txKind": None,
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/cancel-orders",
        body,
    )
    output({"symbol": symbol, "marketId": market_id, **result})


def cmd_order_cancel_and_place(args):
    market_id, symbol, lot_size, tick_size = _market_context(args.symbol)
    if args.order_type not in VALID_ORDER_TYPES:
        error("order_type must be one of: 0 (GTC), 1 (FOK), 2 (PostOnly), 3 (IOC)")

    body = {
        **account_identifier(),
        "walletAddress": get_wallet(),
        "marketId": market_id,
        "orderIdsToCancel": _parse_csv_ints(args.cancel_ids, "cancel_ids"),
        "ordersToPlace": _resolve_orders_to_place(args, lot_size, tick_size),
        "orderType": args.order_type,
        "reduceOnly": args.reduce_only,
        "hasPosition": True,
        "sponsor": None,
        "txKind": None,
        "builderCode": None,
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/cancel-and-place-orders",
        body,
    )
    output({"symbol": symbol, "marketId": market_id, **result})


def cmd_position_leverage(args):
    market_id, symbol, _, _ = _market_context(args.symbol)
    body = {
        **account_identifier(),
        "walletAddress": get_wallet(),
        "marketId": market_id,
        "leverage": float(args.leverage),
        "collateralChange": 0,
        "sponsor": None,
        "txKind": None,
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/set-leverage",
        body,
    )
    output({"symbol": symbol, "marketId": market_id, **result})


def cmd_funds_deposit(args):
    body = {
        **account_identifier(),
        "walletAddress": get_wallet(),
        "collateralCoinType": USDC_COIN_TYPE,
        "depositAmount": _scaled_usdc_amount(args.amount),
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/deposit-collateral",
        body,
    )
    output(result)


def cmd_funds_withdraw(args):
    body = {
        "accountId": get_account_id(),
        "withdrawAmount": _scaled_usdc_amount(args.amount),
        "recipientAddress": get_wallet(),
        "sponsor": None,
        "txKind": None,
    }
    result = execute_native_tx(
        "/api/perpetuals/account/transactions/withdraw-collateral",
        body,
    )
    output(result)


def build_parser():
    parser = JsonArgumentParser(
        prog="trade.py",
        description="Aftermath write operations — <group> <action>",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # order
    order = sub.add_parser("order", help="Order entry/cancel actions")
    order_sub = order.add_subparsers(dest="action", required=True)

    p = order_sub.add_parser("market", help="Place market order")
    p.add_argument("symbol", help="Market symbol (e.g. BTC) or market_id (0x...)")
    p.add_argument("--side", required=True, choices=SIDE_CHOICES)
    p.add_argument("--size", required=True, type=float)
    p.add_argument("--slippage", type=float, default=0.01)
    p.add_argument("--reduce_only", action="store_true")

    p = order_sub.add_parser("limit", help="Place limit order")
    p.add_argument("symbol", help="Market symbol (e.g. BTC) or market_id (0x...)")
    p.add_argument("--side", required=True, choices=SIDE_CHOICES)
    p.add_argument("--size", required=True, type=float)
    p.add_argument("--price", required=True, type=float)
    p.add_argument("--order_type", type=int, default=0)
    p.add_argument("--reduce_only", action="store_true")
    p.add_argument("--post_only", action="store_true")

    p = order_sub.add_parser("cancel", help="Cancel one or more orders")
    p.add_argument("symbol", help="Market symbol (e.g. BTC) or market_id (0x...)")
    p.add_argument("--order_ids", required=True, help="Comma-separated order IDs")

    p = order_sub.add_parser("cancel-and-place", help="Cancel then place orders")
    p.add_argument("symbol", help="Market symbol (e.g. BTC) or market_id (0x...)")
    p.add_argument("--cancel_ids", required=True, help="Comma-separated order IDs")
    p.add_argument("--side", choices=SIDE_CHOICES)
    p.add_argument("--size", type=float)
    p.add_argument("--price", type=float)
    p.add_argument("--orders", help="JSON array of orders, e.g. '[{\"side\":0,\"price\":\"...n\",\"size\":\"...n\"}]'")
    p.add_argument("--order_type", type=int, default=2)
    p.add_argument("--reduce_only", action="store_true")

    # position
    position = sub.add_parser("position", help="Position management")
    position_sub = position.add_subparsers(dest="action", required=True)
    p = position_sub.add_parser("leverage", help="Set position leverage")
    p.add_argument("symbol", help="Market symbol (e.g. BTC) or market_id (0x...)")
    p.add_argument("--leverage", required=True, type=float)

    # funds
    funds = sub.add_parser("funds", help="Collateral operations")
    funds_sub = funds.add_subparsers(dest="action", required=True)
    p = funds_sub.add_parser("deposit", help="Deposit USDC collateral")
    p.add_argument("--amount", required=True, type=str)
    p = funds_sub.add_parser("withdraw", help="Withdraw USDC collateral")
    p.add_argument("--amount", required=True, type=str)

    return parser


DISPATCH = {
    ("order", "market"): cmd_order_market,
    ("order", "limit"): cmd_order_limit,
    ("order", "cancel"): cmd_order_cancel,
    ("order", "cancel-and-place"): cmd_order_cancel_and_place,
    ("position", "leverage"): cmd_position_leverage,
    ("funds", "deposit"): cmd_funds_deposit,
    ("funds", "withdraw"): cmd_funds_withdraw,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    key = (args.group, args.action)
    handler = DISPATCH.get(key)
    if handler is None:
        error(f"unknown command: {args.group} {args.action}")

    try:
        # Ensure core required settings are present up-front.
        require_config("AFTERMATH_PRIVATE_KEY")
        require_config("AFTERMATH_WALLET_ADDRESS")
        require_config("AFTERMATH_ACCOUNT_ID")
        # Optional account cap ID (required by some custody/account setups).
        get_config_value("AFTERMATH_ACCOUNT_CAP_ID")

        handler(args)
    except SystemExit:
        raise
    except Exception as exc:
        error(str(exc))


if __name__ == "__main__":
    main()
