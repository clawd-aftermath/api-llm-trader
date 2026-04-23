#!/usr/bin/env python3
"""Read-only query script for API LLM Trader.

Commands follow <group> <action> pattern.
All output is structured JSON via stdout.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import JsonArgumentParser, error, output
from _api import (
    USDC_COIN_TYPE,
    af_post,
    get_config_value,
    resolve_with_source,
    get_aftermath_host,
)
from _paths import credentials_path
from _symbols import (
    get_markets,
    resolve_symbol,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESOLUTION_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _get_wallet_address(args=None):
    addr = getattr(args, "address", None) if args else None
    if addr:
        return addr
    val = get_config_value("AFTERMATH_WALLET_ADDRESS")
    if val:
        return str(val)
    error("missing wallet address; pass --address or set AFTERMATH_WALLET_ADDRESS")


def _get_account_id():
    val = get_config_value("AFTERMATH_ACCOUNT_ID")
    if val is None:
        error("missing AFTERMATH_ACCOUNT_ID; set it or run aftermath-config")
    try:
        return int(val)
    except (TypeError, ValueError):
        error("AFTERMATH_ACCOUNT_ID must be an integer")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = JsonArgumentParser(
        prog="query.py",
        description="Query Aftermath Perpetuals — <group> <action>",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # -- system --
    sys_p = sub.add_parser("system", help="System-level queries")
    sys_sub = sys_p.add_subparsers(dest="action", required=True)
    sys_sub.add_parser("status", help="API health check")

    # -- market --
    mkt = sub.add_parser("market", help="Market data queries")
    mkt_sub = mkt.add_subparsers(dest="action", required=True)

    p = mkt_sub.add_parser("list", help="List all perpetual markets")
    p.add_argument("--search", help="Filter by symbol substring")

    p = mkt_sub.add_parser("stats", help="Market statistics")
    p.add_argument("--symbol", help="Filter to one symbol")

    p = mkt_sub.add_parser("info", help="Market parameters and metadata")
    p.add_argument("--symbol", help="Filter to one symbol")

    p = mkt_sub.add_parser("book", help="Order book depth")
    p.add_argument("symbol", help="Market symbol (e.g. BTC) or market_id (0x...)")
    p.add_argument("--limit", type=int, default=20, help="Levels per side (default: 20)")

    p = mkt_sub.add_parser("trades", help="Recent trades")
    p.add_argument("symbol", help="Market symbol or market_id")
    p.add_argument("--limit", type=int, default=20, help="Max trades (default: 20)")

    p = mkt_sub.add_parser("candles", help="OHLCV candles")
    p.add_argument("symbol", help="Market symbol or market_id")
    p.add_argument("--resolution", default="1h", help="1m,5m,15m,30m,1h,4h,1d")
    p.add_argument("--count_back", type=int, default=24, help="Number of candles")

    p = mkt_sub.add_parser("funding", help="Funding rate data")
    p.add_argument("--symbol", help="Filter to one symbol")

    # -- account --
    acct = sub.add_parser("account", help="Account queries")
    acct_sub = acct.add_subparsers(dest="action", required=True)

    p = acct_sub.add_parser("info", help="Account details")
    p.add_argument("--address", help="Wallet address (default: AFTERMATH_WALLET_ADDRESS)")

    p = acct_sub.add_parser("positions", help="Open positions")
    p.add_argument("--symbol", help="Filter to one symbol")

    # -- orders --
    orders = sub.add_parser("orders", help="Order queries")
    orders_sub = orders.add_subparsers(dest="action", required=True)

    p = orders_sub.add_parser("open", help="Pending orders")
    p.add_argument("--symbol", help="Filter to one market")

    p = orders_sub.add_parser("history", help="Order history")
    p.add_argument("--limit", type=int, default=20, help="Max orders (default: 20)")

    # -- auth --
    auth = sub.add_parser("auth", help="Credential introspection")
    auth_sub = auth.add_subparsers(dest="action", required=True)
    auth_sub.add_parser("status", help="Check configured credentials")

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_system_status():
    try:
        data = af_post("/api/perpetuals/all-markets", {"collateralCoinType": USDC_COIN_TYPE})
        markets = data.get("markets", []) if isinstance(data, dict) else []
        output({
            "status": "ok",
            "markets_count": len(markets),
            "timestamp": int(time.time()),
            "host": get_aftermath_host(),
        })
    except Exception as e:
        error(f"API unreachable: {e}")


def cmd_market_list(args):
    try:
        markets = get_markets()
    except Exception as e:
        error(f"failed to fetch markets: {e}")
    result = []
    for m in markets:
        params = m.get("marketParams", {})
        sym = params.get("baseAssetSymbol", "")
        if args.search and args.search.upper() not in sym.upper():
            continue
        result.append({
            "symbol": sym,
            "market_id": m.get("objectId"),
            "index_price": m.get("indexPrice"),
        })
    output({"markets": result})


def cmd_market_stats(args):
    try:
        markets = get_markets()
    except Exception as e:
        error(f"failed to fetch markets: {e}")
    result = []
    for m in markets:
        params = m.get("marketParams", {})
        state = m.get("marketState", {})
        sym = params.get("baseAssetSymbol", "")
        if args.symbol and args.symbol.upper() not in (sym.upper(), sym.upper().replace("USD", "")):
            continue
        result.append({
            "symbol": sym,
            "market_id": m.get("objectId"),
            "index_price": m.get("indexPrice"),
            "collateral_price": m.get("collateralPrice"),
            "estimated_funding_rate": m.get("estimatedFundingRate"),
            "open_interest": state.get("openInterest"),
            "premium_twap": state.get("premiumTwap"),
            "next_funding_ms": m.get("nextFundingTimestampMs"),
            "taker_fee": params.get("takerFee"),
            "maker_fee": params.get("makerFee"),
        })
    output({"stats": result})


def cmd_market_info(args):
    try:
        markets = get_markets()
    except Exception as e:
        error(f"failed to fetch markets: {e}")
    result = []
    for m in markets:
        params = m.get("marketParams", {})
        sym = params.get("baseAssetSymbol", "")
        if args.symbol and args.symbol.upper() not in (sym.upper(), sym.upper().replace("USD", "")):
            continue
        result.append({
            "symbol": sym,
            "market_id": m.get("objectId"),
            "lot_size": params.get("lotSize"),
            "tick_size": params.get("tickSize"),
            "taker_fee": params.get("takerFee"),
            "maker_fee": params.get("makerFee"),
            "margin_ratio_initial": params.get("marginRatioInitial"),
            "margin_ratio_maintenance": params.get("marginRatioMaintenance"),
            "min_order_usd_value": params.get("minOrderUsdValue"),
            "max_pending_orders": params.get("maxPendingOrders"),
            "max_open_interest": params.get("maxOpenInterest"),
            "funding_frequency_ms": params.get("fundingFrequencyMs"),
            "funding_period_ms": params.get("fundingPeriodMs"),
        })
    output({"markets": result})


def cmd_market_book(args):
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    try:
        data = af_post("/api/perpetuals/markets/orderbooks", {"marketIds": [market_id]})
    except Exception as e:
        error(f"failed to fetch orderbook: {e}")
    books = data.get("orderbooks", [])
    if not books:
        error(f"no orderbook returned for {sym} ({market_id})")
    ob = books[0].get("orderbook", {})
    bids = ob.get("bids", [])
    asks = sorted(ob.get("asks", []), key=lambda x: float(x.get("price", 0)))
    limit = args.limit
    output({
        "symbol": sym,
        "market_id": market_id,
        "mid_price": ob.get("midPrice"),
        "best_bid": ob.get("bestBidPrice"),
        "best_ask": ob.get("bestAskPrice"),
        "bids": bids[:limit],
        "asks": asks[:limit],
        "bids_total_size": ob.get("bidsTotalSize"),
        "asks_total_size": ob.get("asksTotalSize"),
    })


def cmd_market_trades(args):
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    try:
        data = af_post("/api/perpetuals/market/order-history", {
            "marketId": market_id,
            "limit": args.limit,
            "beforeTimestampCursor": None,
        })
    except Exception as e:
        error(f"failed to fetch trades: {e}")
    output({"symbol": sym, "market_id": market_id, "trades": data.get("orders", data)})


def cmd_market_candles(args):
    if args.resolution not in RESOLUTION_MS:
        error(f"unsupported resolution '{args.resolution}'; use: {', '.join(RESOLUTION_MS)}")
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    interval_ms = RESOLUTION_MS[args.resolution]
    now_ms = int(time.time() * 1000)
    from_ts = now_ms - interval_ms * args.count_back
    try:
        data = af_post("/api/perpetuals/market/candle-history", {
            "marketId": market_id,
            "intervalMs": interval_ms,
            "fromTimestamp": from_ts,
            "toTimestamp": now_ms,
        })
    except Exception as e:
        error(f"failed to fetch candles: {e}")
    output({"symbol": sym, "market_id": market_id, "resolution": args.resolution, "candles": data})


def cmd_market_funding(args):
    try:
        markets = get_markets()
    except Exception as e:
        error(f"failed to fetch markets: {e}")
    result = []
    for m in markets:
        params = m.get("marketParams", {})
        state = m.get("marketState", {})
        sym = params.get("baseAssetSymbol", "")
        if args.symbol and args.symbol.upper() not in (sym.upper(), sym.upper().replace("USD", "")):
            continue
        result.append({
            "symbol": sym,
            "market_id": m.get("objectId"),
            "estimated_funding_rate": m.get("estimatedFundingRate"),
            "premium_twap": state.get("premiumTwap"),
            "index_price": m.get("indexPrice"),
            "next_funding_ms": m.get("nextFundingTimestampMs"),
            "funding_frequency_ms": params.get("fundingFrequencyMs"),
            "funding_period_ms": params.get("fundingPeriodMs"),
        })
    output({"funding": result})


def cmd_account_info(args):
    addr = _get_wallet_address(args)
    try:
        data = af_post("/api/perpetuals/accounts/owned", {
            "walletAddress": addr,
            "collateralCoinTypes": [USDC_COIN_TYPE],
        })
    except Exception as e:
        error(f"failed to fetch accounts: {e}")
    output({"address": addr, "account_caps": data.get("accountCaps", data)})


def cmd_account_positions(args):
    account_id = _get_account_id()
    body = {"accountIds": [account_id]}
    if args.symbol:
        try:
            market_id, _, _ = resolve_symbol(args.symbol)
            body["marketIds"] = [market_id]
        except ValueError as e:
            error(str(e))
    try:
        data = af_post("/api/perpetuals/accounts/positions", body)
    except Exception as e:
        error(f"failed to fetch positions: {e}")
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    positions = accounts[0].get("positions", []) if accounts else []
    output({"account_id": account_id, "positions": positions, "account": accounts[0] if accounts else None})


def cmd_orders_open(args):
    account_id = _get_account_id()
    body = {"accountIds": [account_id]}
    if args.symbol:
        try:
            market_id, _, _ = resolve_symbol(args.symbol)
            body["marketIds"] = [market_id]
        except ValueError as e:
            error(str(e))
    try:
        data = af_post("/api/perpetuals/accounts/positions", body)
    except Exception as e:
        error(f"failed to fetch open orders: {e}")
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    orders = []
    if accounts:
        for pos in accounts[0].get("positions", []):
            market_id = pos.get("marketId")
            for order in pos.get("pendingOrders", []) or []:
                if isinstance(order, dict):
                    orders.append({"marketId": market_id, **order})
                else:
                    orders.append({"marketId": market_id, "order": order})
    output({"account_id": account_id, "orders": orders})


def cmd_orders_history(args):
    account_id = _get_account_id()
    try:
        data = af_post("/api/perpetuals/account/order-history", {
            "accountId": account_id,
            "limit": args.limit,
            "beforeTimestampCursor": None,
        })
    except Exception as e:
        error(f"failed to fetch order history: {e}")
    output({"account_id": account_id, **data})


def cmd_auth_status():
    keys = [
        "AFTERMATH_PRIVATE_KEY",
        "AFTERMATH_WALLET_ADDRESS",
        "AFTERMATH_ACCOUNT_ID",
        "AFTERMATH_ACCOUNT_CAP_ID",
        "AFTERMATH_HOST",
    ]
    sources = {}
    missing = []
    for name in keys:
        _, source = resolve_with_source(name)
        sources[name] = source or "not set"
        if source is None and name != "AFTERMATH_HOST" and name != "AFTERMATH_ACCOUNT_CAP_ID":
            missing.append(name)

    creds_path = credentials_path()
    creds_present = creds_path.is_file()
    mode_secure = None
    if creds_present and os.name != "nt":
        try:
            mode_secure = (creds_path.stat().st_mode & 0o077) == 0
        except OSError:
            pass

    output({
        "status": "ok",
        "auth_capable": len(missing) == 0,
        "host": get_aftermath_host(),
        "sources": sources,
        "credentials_file": {
            "path": str(creds_path),
            "present": creds_present,
            "mode_secure": mode_secure,
        },
        "missing": missing,
    })


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

DISPATCH = {
    ("system", "status"): lambda args: cmd_system_status(),
    ("market", "list"): cmd_market_list,
    ("market", "stats"): cmd_market_stats,
    ("market", "info"): cmd_market_info,
    ("market", "book"): cmd_market_book,
    ("market", "trades"): cmd_market_trades,
    ("market", "candles"): cmd_market_candles,
    ("market", "funding"): cmd_market_funding,
    ("account", "info"): cmd_account_info,
    ("account", "positions"): cmd_account_positions,
    ("orders", "open"): cmd_orders_open,
    ("orders", "history"): cmd_orders_history,
    ("auth", "status"): lambda args: cmd_auth_status(),
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    key = (args.group, args.action)
    handler = DISPATCH.get(key)
    if handler is None:
        error(f"unknown command: {args.group} {args.action}")
    try:
        handler(args)
    except SystemExit:
        raise
    except Exception as e:
        error(str(e))


if __name__ == "__main__":
    main()
