# API LLM Trader

A skill that lets LLM agents trade on [Aftermath V2](https://aftermath.finance) perpetuals through the native API. Install it into Claude Code, Cursor, Codex, or any agent that supports skills, then interact in natural language.

> *"What's the funding rate on BTC right now?"*
>
> *"Place a limit buy for 0.01 BTC at $50 under the current mid."*
>
> *"Show me all open positions and their PnL."*

## Install

```bash
git clone https://github.com/clawd-aftermath/api-llm-trader.git
cd api-llm-trader
pip install -r requirements.txt
```

Then copy/symlink to your agent's skill directory:
```bash
ln -s "$(pwd)" ~/.claude/skills/api-llm-trader
```

## Configure

```bash
./aftermath-config
```

Or set environment variables:
```bash
export AFTERMATH_PRIVATE_KEY=suiprivkey1...
export AFTERMATH_WALLET_ADDRESS=0x...
export AFTERMATH_ACCOUNT_ID=123
```

## Scripts

| Script | Role |
|---|---|
| `scripts/query.py` | Market data, account reads |
| `scripts/trade.py` | Signed writes (orders, leverage, deposits) |
| `scripts/paper.py` | Local paper trading simulation |

All emit JSON. Errors are `{"error": "..."}`.

## Capabilities

- Orderbooks, candles, funding rates, market metadata
- Paper trading against live orderbooks
- Account positions, open orders, order history
- Limit/market orders, cancel, cancel-and-place (atomic)
- Leverage, deposit/withdraw USDC collateral

## Command Index

See [SKILL.md](SKILL.md) for the full command reference.

## Architecture

- **Reads**: Native Aftermath API (`/api/perpetuals/*`), with no CCXT dependency for core market/account reads
- **Writes**: Native transaction builders → Sui Ed25519 signing → Sui JSON-RPC submission
- **No SDK vendoring**: Pure HTTP via `requests` + `PyNaCl` for signing

## Integration References

- Site: https://aftermath.finance
- Swagger: https://aftermath.finance/docs
- OpenAPI: https://aftermath.finance/api/openapi/spec.json
- Canonical skills: https://github.com/AftermathFinance/skills

## License

MIT — see [LICENSE](LICENSE). Review [DISCLAIMER.md](DISCLAIMER.md) before using a funded account.

## Attribution

Forked from [elliottech/lighter-agent-kit](https://github.com/elliottech/lighter-agent-kit). See [ATTRIBUTION.md](ATTRIBUTION.md).
