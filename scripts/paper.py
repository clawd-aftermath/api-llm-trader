#!/usr/bin/env python3
"""Paper trading simulator for API LLM Trader.

Simulates taker fills against live Aftermath orderbook snapshots.
All state is local — no credentials, no on-chain transactions.
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import JsonArgumentParser, error, output
from _api import af_post, get_aftermath_host
from _paths import paper_state_path
from _symbols import (
    resolve_symbol,
    normalize_side,
    side_label,
)

STATE_VERSION = 1
DEFAULT_COLLATERAL = 10_000.0
TAKER_FEE_RATE = 0.0005  # 0.05%
MAINTENANCE_MARGIN_RATIO = 0.03

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path():
    return paper_state_path()


def _new_state(collateral):
    return {
        "version": STATE_VERSION,
        "collateral": collateral,
        "initial_collateral": collateral,
        "positions": {},
        "trades": [],
        "market_cache": {},
    }


def _save_state(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _load_state():
    path = _state_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        error(f"paper state corrupted: {e}; run `paper.py reset`")
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        error("paper state version mismatch; run `paper.py reset`")
    return data


def _require_state():
    state = _load_state()
    if state is None:
        error("no paper account; run `paper.py init` first")
    return state


# ---------------------------------------------------------------------------
# Orderbook fetching
# ---------------------------------------------------------------------------


def _fetch_orderbook(market_id):
    """Fetch live orderbook for one market. Returns (bids, asks, mid_price)."""
    data = af_post("/api/perpetuals/markets/orderbooks", {"marketIds": [market_id]})
    books = data.get("orderbooks", [])
    if not books:
        error(f"no orderbook for {market_id}")
    ob = books[0].get("orderbook", {})
    bids = ob.get("bids", [])
    asks = sorted(ob.get("asks", []), key=lambda x: float(x.get("price", 0)))
    mid = ob.get("midPrice")
    if mid is None and bids and asks:
        mid = (float(bids[0].get("price", 0)) + float(asks[0].get("price", 0))) / 2
    return bids, asks, float(mid) if mid else 0.0


def _get_mark_price(market_id):
    """Get current mark price (mid) for a market."""
    _, _, mid = _fetch_orderbook(market_id)
    return mid


# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------


def _simulate_taker_fill(bids, asks, side_int, size, limit_price=None):
    """Walk the book and simulate a taker fill.

    side_int: 0=buy/long (walk asks), 1=sell/short (walk bids).
    Returns (filled_size, avg_price, fills_list).
    """
    asks_book = sorted(asks, key=lambda x: float(x.get("price", 0)))
    bids_book = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
    book = asks_book if side_int == 0 else bids_book
    remaining = size
    total_cost = 0.0
    fills = []

    for level in book:
        level_price = float(level.get("price", 0))
        level_size = float(level.get("size", level.get("quantity", 0)))
        if level_size <= 0 or level_price <= 0:
            continue

        # Check limit price
        if limit_price is not None:
            if side_int == 0 and level_price > limit_price:
                break
            if side_int == 1 and level_price < limit_price:
                break

        fill_size = min(remaining, level_size)
        total_cost += fill_size * level_price
        fills.append({"price": level_price, "size": fill_size})
        remaining -= fill_size
        if remaining <= 1e-12:
            break

    filled = size - remaining
    avg_price = total_cost / filled if filled > 0 else 0.0
    return filled, avg_price, fills


# ---------------------------------------------------------------------------
# Position accounting
# ---------------------------------------------------------------------------


def _update_position(state, market_id, symbol, side_int, filled_size, avg_price):
    """Update position after a fill. Returns realized PnL from this fill."""
    pos = state["positions"].get(market_id, {"size": 0.0, "entry_price": 0.0, "realized_pnl": 0.0})
    old_size = pos["size"]  # positive=long, negative=short
    fill_signed = filled_size if side_int == 0 else -filled_size
    realized_pnl = 0.0

    if old_size == 0:
        # New position
        pos["size"] = fill_signed
        pos["entry_price"] = avg_price
    elif (old_size > 0) == (fill_signed > 0):
        # Adding to same direction — adjust avg entry
        total_cost = abs(old_size) * pos["entry_price"] + abs(fill_signed) * avg_price
        pos["size"] = old_size + fill_signed
        pos["entry_price"] = total_cost / abs(pos["size"]) if pos["size"] != 0 else 0.0
    else:
        # Reducing or flipping
        close_size = min(abs(old_size), abs(fill_signed))
        if old_size > 0:
            realized_pnl = close_size * (avg_price - pos["entry_price"])
        else:
            realized_pnl = close_size * (pos["entry_price"] - avg_price)
        new_size = old_size + fill_signed
        if abs(new_size) < 1e-12:
            new_size = 0.0
        if (new_size > 0) != (old_size > 0) and new_size != 0:
            # Flipped direction — new entry price is fill price
            pos["entry_price"] = avg_price
        pos["size"] = new_size

    pos["realized_pnl"] = pos.get("realized_pnl", 0.0) + realized_pnl

    if abs(pos["size"]) < 1e-12:
        state["positions"].pop(market_id, None)
    else:
        state["positions"][market_id] = pos

    # Cache market info
    state.setdefault("market_cache", {})[market_id] = {"symbol": symbol}

    return realized_pnl


def _position_pnl(pos, mark_price):
    """Unrealized PnL for a position at a given mark price."""
    size = pos.get("size", 0)
    entry = pos.get("entry_price", 0)
    if size > 0:
        return size * (mark_price - entry)
    elif size < 0:
        return abs(size) * (entry - mark_price)
    return 0.0


def _refresh_marks(state, skip=False):
    """Refresh mark prices for all open positions. Returns warnings dict."""
    if skip:
        return {}
    warnings = {}
    for mid in list(state.get("positions", {})):
        try:
            mark = _get_mark_price(mid)
            state["positions"][mid]["mark_price"] = mark
        except Exception as e:
            sym = state.get("market_cache", {}).get(mid, {}).get("symbol", mid)
            warnings[sym] = str(e)
    return warnings


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args):
    if _load_state() is not None:
        error("paper account already exists; use `paper.py reset`")
    state = _new_state(args.collateral)
    _save_state(state)
    output({"status": "ok", "collateral": args.collateral, "state_path": str(_state_path())})


def cmd_reset(args):
    state = _new_state(args.collateral)
    _save_state(state)
    output({"status": "ok", "collateral": args.collateral, "state_path": str(_state_path())})


def cmd_status(args):
    state = _require_state()
    warns = _refresh_marks(state, skip=getattr(args, "no_refresh", False))
    total_unrealized = sum(
        _position_pnl(p, p.get("mark_price", p.get("entry_price", 0)))
        for p in state["positions"].values()
    )
    total_realized = sum(float(t.get("realized_pnl", 0) or 0.0) for t in state.get("trades", []))
    _save_state(state)
    result = {
        "status": "ok",
        "collateral": state["collateral"],
        "initial_collateral": state["initial_collateral"],
        "unrealized_pnl": round(total_unrealized, 4),
        "realized_pnl": round(total_realized, 4),
        "total_pnl": round(state["collateral"] - state["initial_collateral"] + total_unrealized, 4),
        "positions_count": len(state["positions"]),
        "trades_count": len(state.get("trades", [])),
    }
    if warns:
        result["warnings"] = warns
    output(result)


def cmd_positions(args):
    state = _require_state()
    warns = _refresh_marks(state, skip=getattr(args, "no_refresh", False))
    _save_state(state)
    positions = []
    for mid, pos in state["positions"].items():
        sym = state.get("market_cache", {}).get(mid, {}).get("symbol", mid)
        if args.symbol and args.symbol.upper() != sym.upper():
            continue
        mark = pos.get("mark_price", pos.get("entry_price", 0))
        positions.append({
            "symbol": sym,
            "market_id": mid,
            "side": "long" if pos["size"] > 0 else "short",
            "size": abs(pos["size"]),
            "entry_price": pos["entry_price"],
            "mark_price": mark,
            "unrealized_pnl": round(_position_pnl(pos, mark), 4),
            "realized_pnl": round(pos.get("realized_pnl", 0), 4),
        })
    result = {"positions": positions}
    if warns:
        result["warnings"] = warns
    output(result)


def cmd_trades(args):
    state = _require_state()
    trades = list(reversed(state.get("trades", [])))
    if args.symbol:
        sym = args.symbol.upper()
        trades = [t for t in trades if t.get("symbol", "").upper() == sym]
    output({"trades": trades[: args.limit]})


def cmd_order_market(args):
    if args.size <= 0:
        error("--size must be positive")
    state = _require_state()
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    side_int = normalize_side(args.side)
    bids, asks, mid = _fetch_orderbook(market_id)
    filled, avg_price, fills = _simulate_taker_fill(bids, asks, side_int, args.size)
    if filled <= 0:
        error("no liquidity to fill order")
    fee = abs(filled * avg_price) * TAKER_FEE_RATE
    state["collateral"] -= fee
    rpnl = _update_position(state, market_id, sym, side_int, filled, avg_price)
    state["collateral"] += rpnl
    state.setdefault("trades", []).append({
        "market_id": market_id, "symbol": sym, "side": side_label(side_int),
        "size": filled, "price": avg_price, "fee": round(fee, 6),
        "realized_pnl": round(rpnl, 4),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state)
    output({
        "status": "ok", "symbol": sym, "side": side_label(side_int),
        "order_type": "market", "filled_size": filled, "avg_price": round(avg_price, 6),
        "fee": round(fee, 6), "realized_pnl": round(rpnl, 4),
        "fills_count": len(fills),
    })


def cmd_order_ioc(args):
    if args.size <= 0:
        error("--size must be positive")
    if args.price <= 0:
        error("--price must be positive")
    state = _require_state()
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    side_int = normalize_side(args.side)
    bids, asks, mid = _fetch_orderbook(market_id)
    filled, avg_price, fills = _simulate_taker_fill(bids, asks, side_int, args.size, limit_price=args.price)
    if filled <= 0:
        output({"status": "ok", "symbol": sym, "filled_size": 0, "unfilled": args.size, "note": "no fill at limit price"})
        return
    fee = abs(filled * avg_price) * TAKER_FEE_RATE
    state["collateral"] -= fee
    rpnl = _update_position(state, market_id, sym, side_int, filled, avg_price)
    state["collateral"] += rpnl
    state.setdefault("trades", []).append({
        "market_id": market_id, "symbol": sym, "side": side_label(side_int),
        "size": filled, "price": avg_price, "fee": round(fee, 6),
        "realized_pnl": round(rpnl, 4),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state)
    output({
        "status": "ok", "symbol": sym, "side": side_label(side_int),
        "order_type": "ioc", "limit_price": args.price,
        "filled_size": filled, "avg_price": round(avg_price, 6),
        "unfilled": round(args.size - filled, 8),
        "fee": round(fee, 6), "realized_pnl": round(rpnl, 4),
        "fills_count": len(fills),
    })


def cmd_health(args):
    state = _require_state()
    warns = _refresh_marks(state, skip=getattr(args, "no_refresh", False))
    _save_state(state)
    total_unrealized = 0.0
    total_notional = 0.0
    for pos in state["positions"].values():
        mark = pos.get("mark_price", pos.get("entry_price", 0))
        total_unrealized += _position_pnl(pos, mark)
        total_notional += abs(pos["size"]) * mark
    account_value = state["collateral"] + total_unrealized
    margin_usage = total_notional / account_value if account_value > 0 else 0
    leverage = total_notional / account_value if account_value > 0 else 0
    result = {
        "status": "healthy" if account_value > total_notional * MAINTENANCE_MARGIN_RATIO else "at_risk",
        "account_value": round(account_value, 4),
        "collateral": state["collateral"],
        "total_notional": round(total_notional, 4),
        "margin_usage": round(margin_usage, 4),
        "leverage": round(leverage, 4),
    }
    if warns:
        result["warnings"] = warns
    output(result)


def cmd_liquidation_price(args):
    state = _require_state()
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    warns = _refresh_marks(state, skip=getattr(args, "no_refresh", False))
    _save_state(state)
    pos = state["positions"].get(market_id)
    if not pos or abs(pos.get("size", 0)) < 1e-12:
        output({"symbol": sym, "liquidation_price": 0, "note": "no open position"})
        return
    # Simplified liq price: price where account_value = maintenance_margin
    # account_value = collateral + unrealized_pnl_all_positions
    # At liq: collateral + size*(liq-entry) = maintenance_ratio * abs(size)*liq  (for long)
    size = pos["size"]
    entry = pos["entry_price"]
    other_unrealized = sum(
        _position_pnl(p, p.get("mark_price", p.get("entry_price", 0)))
        for mid, p in state["positions"].items() if mid != market_id
    )
    equity_ex = state["collateral"] + other_unrealized
    if size > 0:
        # Long: equity + size*(liq-entry) = maint*size*liq
        # liq = (equity - size*entry) / (maint*size - size)  ... simplified
        denom = size * (MAINTENANCE_MARGIN_RATIO - 1)
        if abs(denom) < 1e-12:
            liq = 0
        else:
            liq = (equity_ex - size * entry) / denom
    else:
        # Short: equity + abs(size)*(entry-liq) = maint*abs(size)*liq
        absz = abs(size)
        denom = absz * (1 + MAINTENANCE_MARGIN_RATIO)
        if abs(denom) < 1e-12:
            liq = 0
        else:
            liq = (equity_ex + absz * entry) / denom
    result = {
        "symbol": sym, "market_id": market_id,
        "liquidation_price": round(max(0, liq), 4),
        "position_side": "long" if size > 0 else "short",
        "position_size": abs(size),
        "mark_price": pos.get("mark_price", entry),
    }
    if warns:
        result["warnings"] = warns
    output(result)


def cmd_refresh(args):
    state = _require_state()
    try:
        market_id, sym, _ = resolve_symbol(args.symbol)
    except ValueError as e:
        error(str(e))
    bids, asks, mid = _fetch_orderbook(market_id)
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    state.setdefault("market_cache", {})[market_id] = {"symbol": sym}
    _save_state(state)
    output({"status": "ok", "symbol": sym, "market_id": market_id,
            "mid_price": mid, "best_bid": best_bid, "best_ask": best_ask})


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

SIDE_CHOICES = ["buy", "sell", "long", "short"]


def build_parser():
    parser = JsonArgumentParser(prog="paper.py", description="Paper trading on Aftermath perpetuals")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create paper account")
    p.add_argument("--collateral", type=float, default=DEFAULT_COLLATERAL)

    p = sub.add_parser("reset", help="Reset paper account")
    p.add_argument("--collateral", type=float, default=DEFAULT_COLLATERAL)

    p = sub.add_parser("status", help="Account summary")
    p.add_argument("--no-refresh", action="store_true")

    p = sub.add_parser("positions", help="Open positions")
    p.add_argument("--symbol", help="Filter to one symbol")
    p.add_argument("--no-refresh", action="store_true")

    p = sub.add_parser("trades", help="Trade history")
    p.add_argument("--symbol", help="Filter")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("health", help="Account health")
    p.add_argument("--no-refresh", action="store_true")

    p = sub.add_parser("liquidation_price", help="Estimated liq price")
    p.add_argument("symbol")
    p.add_argument("--no-refresh", action="store_true")

    p = sub.add_parser("refresh", help="Refresh orderbook snapshot")
    p.add_argument("symbol")

    # Shared-subset: order <action>
    order = sub.add_parser("order", help="Paper orders")
    order_sub = order.add_subparsers(dest="action", required=True)

    p = order_sub.add_parser("market", help="Paper market order")
    p.add_argument("symbol")
    p.add_argument("--side", required=True, choices=SIDE_CHOICES)
    p.add_argument("--size", type=float, required=True)

    p = order_sub.add_parser("ioc", help="Paper IOC order")
    p.add_argument("symbol")
    p.add_argument("--side", required=True, choices=SIDE_CHOICES)
    p.add_argument("--size", type=float, required=True)
    p.add_argument("--price", type=float, required=True)

    return parser


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

FLAT = {
    "init": cmd_init, "reset": cmd_reset, "status": cmd_status,
    "positions": cmd_positions, "trades": cmd_trades, "health": cmd_health,
    "liquidation_price": cmd_liquidation_price, "refresh": cmd_refresh,
}
GROUPED = {("order", "market"): cmd_order_market, ("order", "ioc"): cmd_order_ioc}


def main():
    args = build_parser().parse_args()
    cmd = args.command
    action = getattr(args, "action", None)
    handler = FLAT.get(cmd) or GROUPED.get((cmd, action))
    if not handler:
        error(f"unknown command: {cmd} {action}" if action else f"unknown command: {cmd}")
    try:
        handler(args)
    except SystemExit:
        raise
    except Exception as e:
        error(str(e))


if __name__ == "__main__":
    main()
