# Deltas between the vendored skills and this codebase

The skills under `skills/` are vendored **verbatim** at the commit recorded in
`PINNED.md`. They are the authority on *patterns and semantics*. They are not
authoritative on *hosts*. This file records every place we deliberately deviate,
so a future reader does not "fix" the code back to a broken state.

---

## 1. The vendored skills name a RETIRED host

The skills document V2-only features — `integratorId`, `integratorFee`,
`triggerPriceType`, TWAP orders — while pointing at the v1 host throughout.
Verified at the pinned commit: **22 references to `aftermath.finance`, zero to
`v2-preview`.**

| File | What it says |
|---|---|
| `skills/api/SKILL.md:22` | ``Production OpenAPI: `https://aftermath.finance/api/openapi/spec.json` `` |
| `skills/api/ccxt.md:7`, `auxiliary-endpoints.md:349`, `gotchas.md:92` | the same retired spec URL |
| `skills/api/monitoring-patterns.md:12` | `const BASE_URL = "https://aftermath.finance";` |
| `skills/api/monitoring-patterns.md:143,153`, `ccxt.md:60,64` | `wss://aftermath.finance/...` |
| `skills/api/safety-and-risk.md` | its `max-order-size` fetch example |
| `skills/api/.api-spec-state.json:2` | `"spec_url"` pointing at v1 |

**This codebase uses `https://v2-preview.aftermath.finance`** — production
mainnet, despite the hostname. Defined once, in `scripts/af_adapter.py`
(`AF_API_BASE_URL`), overridable via `AF_API_BASE_URL`. Nothing else in
`scripts/` may name a host; `tests/test_host_guard.py` fails the build if that
changes.

## 2. The OpenAPI spec carries the same trap

The spec's own `servers` block advertises the retired host:

```json
"servers": [
  { "url": "https://aftermath.finance",         "description": "Production server" },
  { "url": "https://testnet.aftermath.finance", "description": "Testnet server" },
  { "url": "http://localhost:8080",             "description": "Local development server" }
]
```

Any standard generator (`openapi-typescript`, `openapi-generator`, …) will bake
that in as the default base URL and the client will silently talk to a dead API.
**Strip or override `servers` before generating anything from the spec.**

## 3. Live API differs from the published spec

Verified against `v2-preview` on 2026-07-28:

| Route | Spec | Live | Handling |
|---|---|---|---|
| `/api/wallet/all_coin_balances` | present | **404** | `doctor` degrades to a warning; balances shown as "unknown" |
| `/api/wallet/coin_balances` | present | **404** | same |
| `/api/wallet/{address}` | present | **404** | same |

The whole `/api/wallet/*` family is unavailable. Do not build a required path on
it until it is confirmed live.

## 4. Method/shape corrections the skills' prose does not spell out

Taken from the spec, confirmed against the live API:

- `POST /api/gas-pool/pool` — POST, and `walletAddress` is **required**.
  (Not a parameterless GET health check.)
- `POST /api/dynamic-gas` — requires `{ serializedTx, walletAddress, gasCoinType }`.
  It **transforms an already-built transaction** to pay gas in another coin; it
  is not a status endpoint and cannot be pinged for liveness.
- `POST /api/perpetuals/all-markets` — requires `collateralCoinType`; the
  response is `{ markets: [...] }`, **not** a bare array.
- `POST /api/perpetuals/accounts/owned` — requires `walletAddress`; the response
  is `{ accountCaps: [...] }`, **not** a bare array.

## 5. Operational note: no markets are live yet

Aftermath perps have not launched. An empty market list is the **expected**
pre-launch state, so `doctor` reports zero markets as a warning, never a
failure. Revisit once markets are listed.

---

### Rule of thumb

Take the skills' **patterns** — isolated margin, circuit breakers, kill switch,
preview tagged-unions, ID discipline, BigInt wire format. Never take their
**URLs**. Verify hosts and shapes against a live spec fetch, not against prose.
