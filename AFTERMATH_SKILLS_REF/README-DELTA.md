# Deltas between the vendored skills and this codebase

The skills under `skills/` are vendored **verbatim** at the commit recorded in
`PINNED.md`. They are the authority on *patterns and semantics*. They are not
authoritative on *hosts*. This file records every place we deliberately deviate,
so a future reader does not "fix" the code back to a broken state.

---

## 1. Production host alignment

The pinned skills and this codebase now agree that `https://aftermath.finance`
is the launched production host. It is defined once in
`scripts/af_adapter.py` (`AF_API_BASE_URL`) and can be overridden with
`AFTERMATH_API_BASE_URL` (or the legacy `AFTERMATH_HOST` alias).
`tests/test_host_guard.py` pins that default and rejects the retired preview
deployment in live Python.

## 2. Historical API/spec differences

The following differences were verified against the now-retired preview
environment on 2026-07-28. They remain historical integration notes until each
route is rechecked against production:

| Route | Spec | Live | Handling |
|---|---|---|---|
| `/api/wallet/all_coin_balances` | present | **404** | `doctor` degrades to a warning; balances shown as "unknown" |
| `/api/wallet/coin_balances` | present | **404** | same |
| `/api/wallet/{address}` | present | **404** | same |

The whole `/api/wallet/*` family is unavailable. Do not build a required path on
it until it is confirmed live.

## 3. Method/shape corrections the skills' prose does not spell out

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

## 4. Production market identity

Production served 15 markets (BTCUSD through XAUTUSD) on 2026-08-19 under
native USDC
`0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC`.
Market object IDs are deployment-specific and must be discovered from
`/api/perpetuals/all-markets`; tests use visibly synthetic fixture IDs and must
not treat IDs captured from the retired preview deployment as live truth.

---

### Rule of thumb

Take the skills' **patterns** — isolated margin, circuit breakers, kill switch,
preview tagged-unions, ID discipline, and BigInt wire format. Verify dynamic
market IDs and response shapes against production rather than copied examples.
