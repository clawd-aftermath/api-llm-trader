"""Market symbol resolution for API LLM Trader.

Resolves bare ticker symbols (BTC, ETH, SOL) to Aftermath perpetuals
market object IDs using the native all-markets endpoint.

All caching is disk-backed with a 5-minute TTL, shared across scripts.
"""

import json
import os
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _api import USDC_COIN_TYPE, af_post, get_aftermath_host
from _paths import symbol_cache_path

_CACHE_TTL_SECONDS = 300
_LIVE_CACHE = {}  # host -> cache entry
_SIZE_SCALE = Decimal("1000000000")
_PRICE_SCALE = Decimal("1000000")


def _normalize_host(host):
    return host.strip().rstrip("/")


# ---------------------------------------------------------------------------
# Value parsing / formatting for native API BigInt fields
# ---------------------------------------------------------------------------


def parse_n_value(s):
    """Parse a BigInt string like '1000000n' or '1000000' to int."""
    if isinstance(s, int):
        return s
    s = str(s).strip()
    if s.endswith("n"):
        s = s[:-1]
    return int(s)


def format_n_value(n):
    """Format an int to Aftermath BigInt string like '1000000n'."""
    return f"{int(n)}n"


def _to_decimal(value, field_name):
    try:
        val = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if val <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return val


def scale_size(human_size, lot_size_str):
    """Scale a human-readable base size to 9-decimal, lot-aligned units."""
    lot_size = parse_n_value(lot_size_str)
    raw = int((_to_decimal(human_size, "size") * _SIZE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    scaled = (raw // lot_size) * lot_size
    if scaled <= 0:
        raise ValueError(f"size is below minimum lot size ({lot_size} base units)")
    return format_n_value(scaled)


def scale_price(human_price, tick_size_str, side=None):
    """Scale a human-readable USD price to 6-decimal, tick-aligned units."""
    tick_size = parse_n_value(tick_size_str)
    raw = int((_to_decimal(human_price, "price") * _PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    if side in (0, "0", "buy", "long", "bid"):
        scaled = (raw // tick_size) * tick_size
    elif side in (1, "1", "sell", "short", "ask"):
        scaled = int((Decimal(raw) / Decimal(tick_size)).to_integral_value(rounding=ROUND_CEILING)) * tick_size
    else:
        scaled = int((Decimal(raw) / Decimal(tick_size)).to_integral_value(rounding=ROUND_HALF_UP)) * tick_size
    if scaled <= 0:
        raise ValueError(f"price is below minimum tick size ({tick_size} quote units)")
    return format_n_value(scaled)


def unscale_size(scaled_str, lot_size_str):
    """Convert a scaled size back to human-readable float."""
    scaled = parse_n_value(scaled_str)
    return float(Decimal(scaled) / _SIZE_SCALE)


def unscale_price(scaled_str, tick_size_str):
    """Convert a scaled price back to human-readable float."""
    scaled = parse_n_value(scaled_str)
    return float(Decimal(scaled) / _PRICE_SCALE)


# ---------------------------------------------------------------------------
# Side normalization
# ---------------------------------------------------------------------------


def normalize_side(side_str):
    """Normalize side string to native API int: 0=bid/long, 1=ask/short."""
    s = str(side_str).lower().strip()
    if s in ("buy", "long", "bid", "0"):
        return 0
    if s in ("sell", "short", "ask", "1"):
        return 1
    raise ValueError(f"invalid side '{side_str}'; use buy|sell|long|short")


def side_label(side_int):
    """Human-readable label for a native side int."""
    return "long" if side_int == 0 else "short"


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _is_fresh(entry, now=None):
    if not isinstance(entry, dict):
        return False
    if now is None:
        now = int(time.time())
    return now < entry.get("expires_at", 0)


def _read_disk_cache(host):
    path = symbol_cache_path(host)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("host") != host:
        return None
    return data


def _write_disk_cache(host, markets):
    path = symbol_cache_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    entry = {
        "host": host,
        "fetched_at": now,
        "expires_at": now + _CACHE_TTL_SECONDS,
        "markets": markets,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return entry


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_all_markets(host=None):
    """Fetch all perpetual markets from the Aftermath native API."""
    host = _normalize_host(host or get_aftermath_host())
    data = af_post(
        "/api/perpetuals/all-markets",
        {"collateralCoinType": USDC_COIN_TYPE},
        host=host,
    )
    return data.get("markets", [])


def get_markets(host=None):
    """Return cached markets list (memory → disk → fetch)."""
    host = _normalize_host(host or get_aftermath_host())

    cached = _LIVE_CACHE.get(host)
    if _is_fresh(cached):
        return cached["markets"]

    cached = _read_disk_cache(host)
    if _is_fresh(cached):
        _LIVE_CACHE[host] = cached
        return cached["markets"]

    markets = fetch_all_markets(host)
    entry = _write_disk_cache(host, markets)
    _LIVE_CACHE[host] = entry
    return entry["markets"]


def refresh_markets(host=None):
    """Force-refresh the market cache."""
    host = _normalize_host(host or get_aftermath_host())
    markets = fetch_all_markets(host)
    entry = _write_disk_cache(host, markets)
    _LIVE_CACHE[host] = entry
    return markets


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _find_by_symbol(markets, symbol):
    sym = symbol.upper()
    for m in markets:
        market_sym = m.get("marketParams", {}).get("baseAssetSymbol", "").upper()
        if market_sym == sym or market_sym == f"{sym}USD":
            return m
    return None


def _find_by_id(markets, market_id):
    for m in markets:
        if m.get("objectId") == market_id:
            return m
    return None


def resolve_symbol(symbol_or_market_id, host=None):
    """Resolve a symbol or market ID to (market_id, symbol, market_data).

    - Input starting with '0x' is treated as a market object ID.
    - Otherwise matched against baseAssetSymbol (case-insensitive).

    Returns (market_id_str, symbol_str, full_market_dict).
    Raises ValueError on failure.
    """
    host = _normalize_host(host or get_aftermath_host())
    markets = get_markets(host)

    if str(symbol_or_market_id).startswith("0x"):
        m = _find_by_id(markets, symbol_or_market_id)
        if m is None:
            # Retry with fresh data
            markets = refresh_markets(host)
            m = _find_by_id(markets, symbol_or_market_id)
        if m is None:
            raise ValueError(f"unknown market_id '{symbol_or_market_id}'")
        return m["objectId"], m["marketParams"]["baseAssetSymbol"], m

    m = _find_by_symbol(markets, symbol_or_market_id)
    if m is None:
        markets = refresh_markets(host)
        m = _find_by_symbol(markets, symbol_or_market_id)
    if m is None:
        raise ValueError(
            f"unknown symbol '{symbol_or_market_id}'; "
            f"use `query.py market list --search {symbol_or_market_id}` to find it"
        )
    return m["objectId"], m["marketParams"]["baseAssetSymbol"], m


def get_market_params(market_id, host=None):
    """Return the marketParams dict for a given market_id."""
    _, _, m = resolve_symbol(market_id, host=host)
    return m.get("marketParams", {})
