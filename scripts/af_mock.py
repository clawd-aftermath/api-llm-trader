"""Offline twin of :class:`af_adapter.AftermathAdapter`.

Every strategy and command in this repo must be runnable with **zero network
and zero keys** — for tests, for dry runs, and so a new user can see the tool
work before funding anything.

The twin is built by *subclassing the real adapter and replacing only the
transport*. That is deliberate: a hand-written mock reimplements sixty methods
and drifts from the original the first time someone edits one. Here, market-id
resolution, isolated-margin allocation, BigInt wire encoding, preview gating,
transaction inspection, and circuit breakers all run the **real** code paths.
Only the HTTP boundary is fake.

Consequences worth knowing:
  * interface parity is structural, not aspirational (see tests/test_mock_parity.py)
  * a bug in the adapter's request-building shows up in mock tests too, which
    is exactly what you want
  * nothing here can sign or submit; the mock refuses to produce signatures

Usage::

    from af_mock import AftermathMockAdapter
    adapter = AftermathMockAdapter()
    adapter.get_snapshot("BTC")
"""

from __future__ import annotations

import copy
import dataclasses

from af_adapter import AdapterConfig, AftermathAdapter

# A wallet that exists only here. Not a real address; never fund it.
MOCK_WALLET = "0x" + "11" * 32
MOCK_MARKET_ID = "0x" + "ab" * 32
MOCK_MARKET_ID_ETH = "0x" + "cd" * 32
MOCK_ACCOUNT_CAP = "0x" + "ef" * 32
MOCK_COLLATERAL = (
    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
)

# Base64 of "mock-transaction-bytes" / "mock-digest" — shape-valid, meaningless.
_MOCK_TX_BYTES = "bW9jay10cmFuc2FjdGlvbi1ieXRlcw=="
_MOCK_DIGEST = "bW9jay1kaWdlc3Q="


def _market(object_id, base_asset_symbol, index_price):
    """A market shaped exactly like the live API returns one.

    Field names match the production wire contract. Object IDs in this module
    are deliberately synthetic fixtures, not snapshots of live markets. The
    details that matter: markets are keyed by ``objectId`` (not ``marketId``), the ticker
    lives at ``marketParams.baseAssetSymbol`` in ``BTCUSD`` form (not
    ``BTC-PERP``), and margin ratios are 0.1 / 0.05. Getting any of these wrong
    in a fixture produces tests that pass against a mock the real API would
    reject — the exact failure a mock is supposed to prevent.
    """
    return {
        "packageId": "0x" + "99" * 32,
        "objectId": object_id,
        "collateralCoinType": MOCK_COLLATERAL,
        "marketParams": {
            "marginRatioInitial": 0.1,
            "marginRatioMaintenance": 0.05,
            "baseAssetSymbol": base_asset_symbol,
            "basePriceFeedId": 1,          # numeric u32 in v3.0.0, was an address
            "collateralPriceFeedId": 2,
            "fundingFrequencyMs": 3_600_000,
            "fundingPeriodMs": 28_800_000,
            "premiumTwapFrequencyMs": 60_000,
            "premiumTwapPeriodMs": 3_600_000,
            "spreadTwapFrequencyMs": 60_000,
            "spreadTwapPeriodMs": 3_600_000,
            "makerFee": 0.0002,
            "takerFee": 0.0006,
            "liquidationFee": 0.01,
            "insuranceFundFee": 0.002,
            "minOrderUsdValue": 10.0,
            "lotSize": 0.001,
            "tickSize": 0.1,
        },
        "marketState": {"openInterest": 100.0, "cumFundingRateLong": 0.0, "cumFundingRateShort": 0.0},
        "collateralPrice": 1.0,
        "indexPrice": index_price,
        "estimatedFundingRate": 0.0001,
        "nextFundingTimestampMs": 1_700_003_600_000,
    }


class MockTransport:
    """Canned responses keyed by API path.

    Shapes mirror the live V2 API, including the envelopes that are easy to get
    wrong: ``all-markets`` returns ``{"markets": [...]}`` and ``accounts/owned``
    returns ``{"accountCaps": [...]}`` — neither is a bare array.
    """

    def __init__(self):
        self.calls = []  # [(path, body)] — assertable in tests
        self.markets = [
            _market(MOCK_MARKET_ID, "BTCUSD", 60_000.0),
            _market(MOCK_MARKET_ID_ETH, "ETHUSD", 3_000.0),
        ]
        self.open_orders = []
        self.positions = []
        self.next_order_id = 1
        # Route -> error string, to rehearse failure paths in tests.
        self.fail_paths = {}

    # -- helpers used by tests ------------------------------------------

    def fail(self, path, message="mock failure"):
        self.fail_paths[path] = message

    def add_open_order(self, market_id=MOCK_MARKET_ID, side=0, price=59_000.0, size=0.01):
        order = {
            "orderId": str(self.next_order_id),
            "marketId": market_id,
            "side": side,
            "price": price,
            "size": size,
        }
        self.next_order_id += 1
        self.open_orders.append(order)
        return order

    # -- dispatch --------------------------------------------------------

    def handle(self, path, body):
        self.calls.append((path, copy.deepcopy(body) if body else {}))

        if path in self.fail_paths:
            raise RuntimeError(self.fail_paths[path])

        # Transaction builders: every one returns an inspectable envelope.
        if "/transactions/" in path:
            return self._tx_response(path, body)

        # Previews: success unless a test marks them failing.
        if "/previews/" in path:
            return {"estimatedFee": "10", "ok": True}

        handler = self._routes().get(path)
        if handler is None:
            raise RuntimeError(
                f"MockTransport has no fixture for {path!r}. "
                "Add one rather than letting a test silently pass."
            )
        return handler(body or {})

    def _tx_response(self, path, body):
        resp = {
            "transactionBytes": _MOCK_TX_BYTES,
            "signingDigest": _MOCK_DIGEST,
            "sender": (body or {}).get("walletAddress", MOCK_WALLET),
        }
        # create-account with deferShare returns deferred PTB argument
        # references *instead of* the simple shape — the trap in gotchas #12.
        if path.endswith("/create-account") and (body or {}).get("deferShare"):
            resp["txKind"] = "mock-tx-kind"
            resp["deferredArgs"] = {"accountCap": {"kind": "NestedResult", "index": 0}}
        if (body or {}).get("sponsor"):
            resp["sponsor"] = body["sponsor"]
        return resp

    def _routes(self):
        return {
            "/api/perpetuals/all-markets": lambda b: {"markets": self.markets},
            "/api/perpetuals/markets": lambda b: {"markets": self.markets},
            "/api/perpetuals/accounts/owned": lambda b: {
                "accountCaps": [
                    {
                        "accountCapId": MOCK_ACCOUNT_CAP,
                        "accountId": "1n",
                        "accountNumber": 1,
                        "collateralCoinType": MOCK_COLLATERAL,
                    }
                ]
            },
            "/api/perpetuals/accounts": lambda b: {
                "accounts": [{"accountId": "1n", "collateral": "1000", "freeCollateral": "800"}]
            },
            "/api/perpetuals/accounts/positions": lambda b: {"positions": self.positions},
            "/api/perpetuals/markets/orderbooks": lambda b: {
                "orderbooks": [
                    {
                        "objectId": MOCK_MARKET_ID,
                        # Asks arrive descending from the API; adapter must sort.
                        "asks": [[60_100.0, 0.5], [60_050.0, 1.0]],
                        "bids": [[59_950.0, 1.0], [59_900.0, 2.0]],
                    }
                ]
            },
            "/api/perpetuals/markets/prices": lambda b: {
                "prices": [{"objectId": m["objectId"], "indexPrice": m["indexPrice"]} for m in self.markets]
            },
            "/api/perpetuals/markets/24hr-stats": lambda b: {
                "stats": [{"objectId": MOCK_MARKET_ID, "volume": "1000000", "priceChange": "0.02"}]
            },
            "/api/perpetuals/market/candle-history": lambda b: {
                "candles": [
                    {"timestamp": 1_700_000_000_000, "open": 59_000, "high": 60_500,
                     "low": 58_900, "close": 60_000, "volume": 120.5}
                ]
            },
            "/api/perpetuals/market/funding-history": lambda b: {
                "fundingHistory": [{"timestamp": 1_700_000_000_000, "fundingRate": 0.0001}]
            },
            "/api/perpetuals/market/order-history": lambda b: {"orders": []},
            "/api/perpetuals/account/order-history": lambda b: {
                "orders": [], "nextBeforeTimestampCursor": None
            },
            "/api/perpetuals/account/max-order-size": lambda b: {"maxOrderSize": "5.0"},
            "/api/gas-pool/pool": lambda b: {
                "sponsorAddress": MOCK_WALLET, "balance": "1000000000"
            },
            "/api/wallet/all_coin_balances": lambda b: {
                "0x2::sui::SUI": "2000000000",
                MOCK_COLLATERAL: "1000000000",
            },
            "/api/rewards/points": lambda b: {"totalPoints": 12.5},
        }


class AftermathMockAdapter(AftermathAdapter):
    """Interface-identical, fully offline. Never signs, never submits."""

    def __init__(self, config=None, transport=None):
        cfg = config or self._default_config()
        super().__init__(config=cfg)
        self.transport = transport or MockTransport()

    @staticmethod
    def _default_config():
        """A config that works with no environment at all.

        AdapterConfig is frozen, so overrides go through ``dataclasses.replace``.
        The wallet/collateral/armed values are forced rather than inherited so a
        real wallet sitting in the shell environment can never leak into a mock
        run — the mock must be identical no matter who runs it.
        """
        cfg = AdapterConfig()
        return dataclasses.replace(
            cfg,
            wallet_address=MOCK_WALLET,
            collateral_coin_type=MOCK_COLLATERAL,
            armed=False,
        )

    # -- transport override: the ONLY thing that differs ----------------

    def request(self, path, body=None, method="POST", retries=None):
        return self.transport.handle(path, body)

    def set_signer(self, signer):
        raise RuntimeError(
            "the mock adapter cannot sign. It exists so strategies can be "
            "exercised with no keys and no network."
        )

    def sign_and_submit(self, inspected, reconcile=None):
        raise RuntimeError("the mock adapter cannot submit transactions.")

    # -- conveniences for tests -----------------------------------------

    @property
    def calls(self):
        """Every (path, body) the code under test attempted."""
        return self.transport.calls

    def paths_called(self):
        return [p for p, _ in self.transport.calls]

    def assert_called(self, path):
        if path not in self.paths_called():
            raise AssertionError(f"expected a call to {path}; saw {self.paths_called()}")


def get_mock_adapter(**kwargs):
    return AftermathMockAdapter(**kwargs)
