---
name: api-llm-trader
description: >-
  Trade on Aftermath Finance perpetuals — query orderbooks, funding rates,
  candles, positions, place limit/market orders, cancel-and-place atomically,
  set leverage, deposit/withdraw USDC collateral. Uses native Aftermath API.
allowed-tools:
  - Bash
compatibility: >-
  Requires Python 3.9+ with requests and PyNaCl. All platforms.
---

# API LLM Trader

Trade on Aftermath Finance — a perpetual futures exchange on Sui.

Scripts live in `scripts/`. Reads use `query.py`, writes use `trade.py`, paper trading uses `paper.py`. Every command prints JSON to stdout. Errors are always `{"error": "..."}`.

## Install

```bash
pip install -r requirements.txt
```

Copy this folder to a skill location:
- **Personal:** `~/.claude/skills/api-llm-trader/`
- **Project:** `.claude/skills/api-llm-trader/`

## Symbol Convention

Perps only. Bare tickers: `BTC`, `ETH`, `SOL` or full symbols like `BTCUSD`. No spot pairs.

Market IDs (`0x...`) are also accepted as escape hatches.

## How to Handle User Requests

1. **Symbol resolution is automatic.** If unsure, run `query.py market list --search <term>`.
2. **Side accepts both forms:** `--side buy|sell|long|short` — both accepted, mapped to 0/1 internally.
3. Run the matching script and parse the JSON response.
4. Run `query.py auth status` to check credentials before authenticated commands.

---

## Read Commands (`query.py`)

### Public — no credentials

| Command | Purpose |
|---|---|
| `system status` | API health check |
| `market list [--search X]` | All perpetual markets |
| `market stats [--symbol X]` | Prices, funding, open interest |
| `market info [--symbol X]` | Market parameters (fees, lot/tick size, margins) |
| `market book <symbol> [--limit 20]` | Orderbook depth |
| `market trades <symbol> [--limit 20]` | Recent fills |
| `market candles <symbol> --resolution 1h [--count_back 24]` | OHLCV candles (1m,5m,15m,30m,1h,4h,1d) |
| `market funding [--symbol X]` | Funding rates and TWAP data |

### Account reads — require `AFTERMATH_WALLET_ADDRESS` / `AFTERMATH_ACCOUNT_ID`

| Command | Purpose |
|---|---|
| `account info [--address 0x...]` | Account list for a wallet |
| `account positions [--symbol X]` | Open positions |
| `orders open [--symbol X]` | Pending orders |
| `orders history [--limit 20]` | Filled/cancelled orders |
| `auth status` | Local credential check (no network) |

---

## Write Commands (`trade.py`)

All require `AFTERMATH_PRIVATE_KEY`, `AFTERMATH_WALLET_ADDRESS`, `AFTERMATH_ACCOUNT_ID`.

| Command | Purpose |
|---|---|
| `order market <symbol> --side S --size N [--slippage 0.01] [--reduce_only]` | Market order |
| `order limit <symbol> --side S --size N --price N [--order_type 0] [--post_only] [--reduce_only]` | Limit order (0=GTC,2=PostOnly) |
| `order cancel <symbol> --order_ids ID1,ID2` | Cancel specific orders |
| `order cancel-and-place <symbol> --cancel_ids ID1,ID2 --side S --size N --price N [--order_type 2]` | Atomic cancel+place (key MM optimization) |
| `position leverage <symbol> --leverage N` | Set leverage |
| `funds deposit --amount N` | Deposit USDC |
| `funds withdraw --amount N` | Withdraw USDC |

---

## Paper Trading (`paper.py`)

No credentials required. Simulates against live orderbooks.

| Command | Purpose |
|---|---|
| `init [--collateral 10000]` | Create paper account |
| `reset [--collateral 10000]` | Wipe and recreate |
| `status [--no-refresh]` | Account summary |
| `positions [--symbol X] [--no-refresh]` | Open positions |
| `trades [--symbol X] [--limit 50]` | Trade history |
| `order market <symbol> --side S --size N` | Paper market order |
| `order ioc <symbol> --side S --size N --price N` | Paper IOC |
| `health [--no-refresh]` | Margin and leverage |
| `liquidation_price <symbol> [--no-refresh]` | Estimated liq price |
| `refresh <symbol>` | Force-refresh orderbook |

### Paper vs Live Caveats

1. Taker-only fills (no maker). 2. No order-impact model. 3. No funding accrual.
4. Simplified margin. 5. No stop/TP/SL orders.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AFTERMATH_PRIVATE_KEY` | For writes | Sui private key (`suiprivkey1...`) |
| `AFTERMATH_WALLET_ADDRESS` | For writes | Sui wallet address (`0x...`) |
| `AFTERMATH_ACCOUNT_ID` | For writes | Numeric perpetuals account ID |
| `AFTERMATH_ACCOUNT_CAP_ID` | Optional | Account capability object ID |
| `AFTERMATH_HOST` | No | API host (default: `https://aftermath.finance`) |
| `SUI_RPC_URL` | No | Sui fullnode (default: auto from host) |
| `AFTERMATH_PAPER_STATE_PATH` | No | Paper state file path |

Set via env vars or `~/.aftermath/api-llm-trader/credentials` file.

## Safety Notes

- Write commands sign and broadcast irreversible Sui transactions.
- `order cancel-and-place` is atomic — prefer it over separate cancel+place.
- Aftermath perps are 24/7 — markets never close.
- orderType enum: `0`=GTC, `1`=FOK, `2`=PostOnly, `3`=IOC.

### Credential Security — MANDATORY

**Never read, cat, print, or echo the credentials file or private key.** This includes:
- `cat ~/.aftermath/api-llm-trader/credentials`
- `echo $AFTERMATH_PRIVATE_KEY`
- `env | grep AFTERMATH`
- Reading `.env` files containing keys

Private keys are wrapped in `SecretValue` that prints `[REDACTED]`.
