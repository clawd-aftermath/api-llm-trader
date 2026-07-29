"""THE Aftermath V2 adapter — the single venue boundary for this repo.

Every call to the Aftermath API in this repository goes through this module.
No other file constructs an API path, a request body, or an HTTP request. That
is a hard rule, enforced by ``tests/test_adapter_boundary.py``: putting the
best practices *here* is what makes them apply to every command at once and
impossible for a caller to forget.

What lives here
---------------
* the ONE host constant (``AF_API_BASE_URL``) every request resolves from
* transport: retry/backoff, preview tagged-union parsing, error extraction
* strict ID typing (native ``"123n"`` vs CCXT cap object id vs account number)
* the native BigInt wire format
* gas modes (``sponsored`` | ``self`` | ``dynamic``) — the user's choice
* the transaction pipeline: build -> preview gate -> INSPECT -> (sign) -> reconcile
* safety: margin zones, 2% sizing, two-tier circuit breakers, kill switch,
  SIGINT/SIGTERM cancel-all, state refresh after mutation, serialized deposits
* atomic primitives: ``cancel-and-place-orders`` for every requote,
  ``place-scale-order`` for every ladder, composed PTBs for onboarding

Safety posture
--------------
This module NEVER signs, submits, or broadcasts unless the operator has
explicitly armed it (``AFTERMATH_ARMED=1``) *and* supplied a signer. Unarmed —
the shipped default — every write path stops immediately after inspection and
returns the inspection report.

Contract source: the live V2 OpenAPI spec (251 paths / 342 schemas, fetched
2026-07-28). Shapes below were read from that spec, not from memory.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _paths import credentials_path

# ===========================================================================
# 1. Host — exactly ONE definition
# ===========================================================================

# https://v2-preview.aftermath.finance IS production mainnet. Despite the
# hostname it is not a preview or a testbed: it is the live relaunch API. The
# legacy bare host is retired and no longer serves these routes at all.
#
# This is the only hostname literal in the tree outside tests, docs and the
# vendored skills. The relaunch domain will change; a repo with the host smeared
# across 40 files is a repo that breaks that day.
AF_API_BASE_URL = "https://v2-preview.aftermath.finance"

#: Env var that overrides the host without touching source.
HOST_ENV_VAR = "AFTERMATH_API_BASE_URL"
#: Back-compatible alias kept so existing credentials files keep working.
HOST_ENV_VAR_LEGACY = "AFTERMATH_HOST"

#: USDC on Sui mainnet — the default collateral. Overridable, never pasted by hand.
DEFAULT_COLLATERAL_COIN_TYPE = (
    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"
    "::usdc::USDC"
)

#: Sui's native gas coin.
SUI_COIN_TYPE = "0x2::sui::SUI"

#: Gas budget is always set explicitly. Auto-estimation under-counts the storage
#: cost of created objects and surfaces later as InsufficientGas on a
#: transaction that simulated fine.
DEFAULT_GAS_BUDGET_MIST = 50_000_000  # 0.05 SUI

MIST_PER_SUI = 1_000_000_000

#: Candle resolutions. v3.0.0: CCXT-style timeframe STRINGS everywhere. The old
#: `intervalMs` / `interval_ms` integers are gone from both the native
#: `candle-history` route (now `resolution`) and CCXT OHLCV (now `timeframe`).
CANDLE_RESOLUTIONS = (
    "1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "3d", "1w", "1mo",
)

#: Approximate millisecond span of each resolution. Used ONLY to pick a
#: from/to window for a "give me the last N candles" request — never sent.
_RESOLUTION_APPROX_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
    "1mo": 2_592_000_000,
}

#: Order execution types, native enum.
ORDER_TYPE_GTC = 0
ORDER_TYPE_FOK = 1
ORDER_TYPE_POST_ONLY = 2
ORDER_TYPE_IOC = 3
VALID_ORDER_TYPES = (ORDER_TYPE_GTC, ORDER_TYPE_FOK, ORDER_TYPE_POST_ONLY, ORDER_TYPE_IOC)

#: `triggerPriceType`: which on-chain price a stop/SL/TP trigger reads.
TRIGGER_PRICE_INDEX = 0
TRIGGER_PRICE_BOOK_MID = 1
TRIGGER_PRICE_MARK = 2


def api_base_url() -> str:
    """Resolve the API host. One definition, env-overridable, used everywhere."""
    raw = get_config_value(HOST_ENV_VAR) or get_config_value(HOST_ENV_VAR_LEGACY)
    return str(raw or AF_API_BASE_URL).strip().rstrip("/")


# ===========================================================================
# 2. Config — one secret, sensible defaults for everything else
# ===========================================================================

_SECRET_NAMES = frozenset({"AFTERMATH_PRIVATE_KEY"})
_CREDENTIALS = None


class SecretValue:
    """Opaque wrapper keeping secrets out of repr/str, logs and tracebacks."""

    __slots__ = ("_val",)

    def __init__(self, val):
        self._val = val

    def expose(self):
        """Return the plaintext secret. Callers must not log the result."""
        return self._val

    def __repr__(self):
        return "SecretValue([REDACTED])"

    def __str__(self):
        return "[REDACTED]"

    def __bool__(self):
        return bool(self._val)


def _strip_optional_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_credentials():
    global _CREDENTIALS
    if _CREDENTIALS is not None:
        return _CREDENTIALS

    path = credentials_path()
    if not path.is_file():
        _CREDENTIALS = {}
        return _CREDENTIALS

    if os.name != "nt":
        try:
            mode = path.stat().st_mode
            if mode & 0o077:
                import stat
                print(
                    f"WARNING: {path} is accessible by other users "
                    f"(mode {stat.filemode(mode)}). Run: chmod 600 {path}",
                    file=sys.stderr,
                )
        except OSError:
            pass

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        _CREDENTIALS = {}
        return _CREDENTIALS

    creds = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = _strip_optional_quotes(value.strip())
        if key and value:
            creds[key] = SecretValue(value) if key in _SECRET_NAMES else value
    _CREDENTIALS = creds
    return _CREDENTIALS


def get_config_value(name, default=None):
    """Resolve a setting from env first, then the per-user credentials file."""
    value = os.environ.get(name)
    if value:
        return SecretValue(value) if name in _SECRET_NAMES else value
    value = _load_credentials().get(name)
    if value:
        return value
    return default


def resolve_with_source(name):
    """Like :func:`get_config_value` but reports where the value came from."""
    value = os.environ.get(name)
    if value:
        return (SecretValue(value) if name in _SECRET_NAMES else value, "env")
    value = _load_credentials().get(name)
    if value:
        return value, "credentials_file"
    return None, None


def _truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on", "armed"}


class ConfigError(RuntimeError):
    """Configuration is missing or self-inconsistent. Always actionable."""


@dataclass(frozen=True)
class AdapterConfig:
    """Everything the adapter needs. One mandatory value: the wallet address."""

    base_url: str = AF_API_BASE_URL
    wallet_address: str = ""
    collateral_coin_type: str = DEFAULT_COLLATERAL_COIN_TYPE
    account_id: object = None          # NativeAccountId | None (auto-discovered)
    account_cap_id: object = None      # AccountCapId | None
    gas_mode: str = "sponsored"
    gas_coin_type: str = ""
    gas_budget_mist: int = DEFAULT_GAS_BUDGET_MIST
    armed: bool = False
    integrator_id: object = None       # u32, v3.0.0 (was integratorAddress)
    integrator_fee: object = None      # decimal fraction, v3.0.0 (was takerFee)
    max_integrator_fee: object = None  # v3.0.0 (was maxTakerFee)
    timeout_s: float = 30.0
    max_retries: int = 3

    @classmethod
    def from_env(cls):
        wallet = get_config_value("AFTERMATH_WALLET_ADDRESS")
        wallet = str(wallet).strip() if wallet else ""

        account_id = get_config_value("AFTERMATH_ACCOUNT_ID")
        account_cap = get_config_value("AFTERMATH_ACCOUNT_CAP_ID")
        integrator_id = get_config_value("AFTERMATH_INTEGRATOR_ID")
        integrator_fee = get_config_value("AFTERMATH_INTEGRATOR_FEE")
        max_integrator_fee = get_config_value("AFTERMATH_MAX_INTEGRATOR_FEE")
        budget = get_config_value("AFTERMATH_GAS_BUDGET_MIST")

        return cls(
            base_url=api_base_url(),
            wallet_address=wallet,
            collateral_coin_type=str(
                get_config_value("AFTERMATH_COLLATERAL_COIN_TYPE")
                or DEFAULT_COLLATERAL_COIN_TYPE
            ).strip(),
            account_id=native_account_id(account_id) if account_id else None,
            account_cap_id=account_cap_id(str(account_cap)) if account_cap else None,
            gas_mode=parse_gas_mode(get_config_value("AFTERMATH_GAS_MODE")),
            gas_coin_type=str(get_config_value("AFTERMATH_GAS_COIN_TYPE") or "").strip(),
            gas_budget_mist=int(budget) if budget else DEFAULT_GAS_BUDGET_MIST,
            armed=_truthy(get_config_value("AFTERMATH_ARMED", "")),
            integrator_id=int(integrator_id) if integrator_id else None,
            integrator_fee=float(integrator_fee) if integrator_fee else None,
            max_integrator_fee=float(max_integrator_fee) if max_integrator_fee else None,
        )


# ===========================================================================
# 3. Strict ID typing (skills v3.0.0, gotchas.md §1)
# ===========================================================================
#
# Three different things are all loosely called "the account", and mixing them
# is the single most common integration failure against this API:
#
#   native  accountId    numeric identity, transported as the string "123n"
#   CCXT write accountId an account-capability OBJECT id, "0x..."
#   CCXT read  accountNumber  a plain JSON number
#
# A bare `str` parameter accepts all three silently. Distinct classes make the
# mistake a TypeError at the boundary instead of a wrong-account transaction.

_OBJECT_ID_RE = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
_NATIVE_BIGINT_RE = re.compile(r"^(-?\d+)n$")


class _Id:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return type(other) is type(self) and other.value == self.value

    def __hash__(self):
        return hash((type(self).__name__, self.value))

    def __repr__(self):
        return f"{type(self).__name__}({self.value!r})"

    def __str__(self):
        return str(self.value)


class NativeAccountId(_Id):
    """Native perpetuals account id. Numeric identity; wire format ``"123n"``."""

    def wire(self):
        """The mandatory native BigInt wire form."""
        return to_native_bigint(self.value)


class AccountCapId(_Id):
    """CCXT *write* account id — an account-capability object id (``0x...``)."""


class AccountNumber(_Id):
    """CCXT read/stream account number — a plain integer, never ``"123n"``."""


class MarketId(_Id):
    """A market identifier. NOT a ticker — the API validates these strictly."""


class SuiAddress(_Id):
    """A Sui address (``0x...``)."""


def native_account_id(value):
    """Validate and brand a native account id. Accepts 123, "123" or "123n"."""
    if isinstance(value, NativeAccountId):
        return value
    if isinstance(value, (AccountCapId, AccountNumber)):
        raise TypeError(
            f"{type(value).__name__} is not a native accountId. Native ids are "
            f"numeric and go on the wire as \"123n\"."
        )
    if isinstance(value, bool):
        raise TypeError("native accountId must be an integer, got a bool")
    if isinstance(value, int):
        n = value
    else:
        m = re.match(r"^(\d+)n?$", str(value).strip())
        if not m:
            raise TypeError(
                f"native accountId must be digits (optionally \"n\"-suffixed), "
                f"got {value!r}"
            )
        n = int(m.group(1))
    if n < 0:
        raise TypeError(f"native accountId must be non-negative, got {n}")
    return NativeAccountId(n)


def account_cap_id(value):
    """Validate and brand a CCXT-write account capability object id."""
    if isinstance(value, AccountCapId):
        return value
    v = str(value).strip()
    if not _OBJECT_ID_RE.match(v):
        raise TypeError(
            f"CCXT accountId must be an object id (\"0x...\"), got {value!r}. "
            f"If you meant the numeric native account id, use native_account_id()."
        )
    return AccountCapId(v)


def account_number(value):
    """Validate and brand a CCXT read/stream account number."""
    if isinstance(value, AccountNumber):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"accountNumber must be a plain int, got {value!r}")
    if value < 0:
        raise TypeError(f"accountNumber must be non-negative, got {value}")
    return AccountNumber(value)


def market_id(value):
    """Brand a market id. Obtain these from the API — never construct them."""
    if isinstance(value, MarketId):
        return value
    v = str(value).strip()
    if not v:
        raise TypeError("marketId must not be empty")
    return MarketId(v)


def sui_address(value):
    """Validate and brand a Sui address."""
    if isinstance(value, SuiAddress):
        return value
    v = str(value).strip()
    if not _OBJECT_ID_RE.match(v):
        raise TypeError(f"invalid Sui address: {value!r}")
    return SuiAddress(v)


def looks_like_object_id(value):
    return bool(_OBJECT_ID_RE.match(str(value).strip()))


# --- Native BigInt wire format (gotchas.md §11) -----------------------------
# Native BigInt fields use the exact "...n" string on request AND response.
# Plain numbers are rejected. Other timestamps/counters stay JSON numbers —
# follow the per-endpoint type, never blanket-convert.


def to_native_bigint(value):
    """Encode an integer for a native BigInt request field: 123 -> ``"123n"``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"native BigInt fields take an int, got {value!r}")
    return f"{value}n"


def from_native_bigint(value):
    """Decode a native BigInt response field: ``"123n"`` -> 123."""
    if isinstance(value, bool):
        raise TypeError(f"expected native BigInt wire format, got {value!r}")
    if isinstance(value, int):
        # Some fields legitimately arrive as plain JSON numbers; accept them
        # rather than pretending the whole API is BigInt-encoded.
        return value
    m = _NATIVE_BIGINT_RE.match(str(value).strip())
    if not m:
        # Bare digit strings appear in a few responses; tolerate on the way in,
        # never emit them on the way out.
        s = str(value).strip()
        if re.match(r"^-?\d+$", s):
            return int(s)
        raise TypeError(
            f"expected native BigInt wire format (\"123n\"), got {value!r}"
        )
    return int(m.group(1))


def is_native_bigint(value):
    return isinstance(value, str) and bool(_NATIVE_BIGINT_RE.match(value.strip()))


# ===========================================================================
# 4. Gas — the USER'S choice, never hardcoded
# ===========================================================================
#
#   sponsored  gas pool pays        — the user needs no SUI at all (default,
#                                     so a fresh wallet can trade immediately)
#   self       user pays their SUI  — ordinary Sui gas coin path
#   dynamic    pay gas in a chosen coin via /api/dynamic-gas
#
# The user also picks WHICH COIN pays gas in dynamic mode
# (``AFTERMATH_GAS_COIN_TYPE``). Sponsor and sender MAY be the same address on
# Sui — nothing here asserts otherwise.

GAS_MODES = ("sponsored", "self", "dynamic")


def parse_gas_mode(value, fallback="sponsored"):
    if value is None or str(value).strip() == "":
        return fallback
    v = str(value).strip().lower()
    if v not in GAS_MODES:
        raise ConfigError(
            f"invalid AFTERMATH_GAS_MODE {value!r}. Expected one of: "
            f"{', '.join(GAS_MODES)}"
        )
    return v


@dataclass(frozen=True)
class GasConfig:
    mode: str = "sponsored"
    budget_mist: int = DEFAULT_GAS_BUDGET_MIST
    gas_coin_type: str = ""
    sponsor_wallet: object = None  # SuiAddress | None


def format_sui(mist):
    whole, frac = divmod(int(mist), MIST_PER_SUI)
    frac_s = str(frac).rjust(9, "0").rstrip("0")
    return f"{whole}.{frac_s} SUI" if frac_s else f"{whole} SUI"


def short_coin(coin_type):
    return str(coin_type).split("::")[-1] or str(coin_type)


# ===========================================================================
# 5. Transport — retry, backoff, preview tagged unions
# ===========================================================================

try:  # pragma: no cover - import guard
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None


class AftermathApiError(RuntimeError):
    """A request failed, or a route returned an error payload."""

    def __init__(self, message, endpoint=None, status=None, retryable=False):
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.retryable = retryable


class PreviewRejected(AftermathApiError):
    """A preview gate rejected the operation. The transaction is not built."""


class TxInspectionError(RuntimeError):
    """A built transaction did not match the caller's intent. Never signed."""

    def __init__(self, message, intent=""):
        super().__init__(f"transaction inspection failed ({intent}): {message}")
        self.intent = intent


class NotArmedError(RuntimeError):
    """Signing was attempted while the adapter is in its shipped dry-run state."""


_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _extract_error_message(payload):
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("error", "message", "msg", "reason", "detail", "details"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        return json.dumps(payload, default=str)
    return str(payload)


def _is_retryable_exc(exc):
    name = type(exc).__name__
    return name in {"ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout",
                    "ChunkedEncodingError", "RemoteDisconnected"}


class PreviewResult:
    """A preview outcome as a tagged union. Fails closed.

    Preview endpoints can return **HTTP 200 with an error body**
    (``{"error": ...}`` plus an ``X-Error-Message: true`` header). Treating a
    200 as success there is how a rejected order becomes a live transaction.
    """

    __slots__ = ("ok", "value", "error")

    def __init__(self, ok, value=None, error=None):
        self.ok = ok
        self.value = value
        self.error = error

    @classmethod
    def success(cls, value):
        return cls(True, value=value)

    @classmethod
    def failure(cls, error):
        return cls(False, error=str(error))

    def unwrap(self, intent=""):
        if not self.ok:
            raise PreviewRejected(f"preview rejected ({intent}): {self.error}")
        return self.value

    def __repr__(self):
        return f"PreviewResult(ok={self.ok}, error={self.error!r})"


def classify_payload(payload, headers=None):
    """Classify a 200 body as success or error. Shared by previews and builds.

    Fails closed: an ``error`` key, or the ``X-Error-Message`` header, means
    error regardless of the status code.
    """
    hdr = {str(k).lower(): v for k, v in (headers or {}).items()}
    if "x-error-message" in hdr and str(hdr["x-error-message"]).lower() not in {"", "false", "0"}:
        return PreviewResult.failure(_extract_error_message(payload))
    if isinstance(payload, dict):
        err = payload.get("error")
        if err not in (None, False, ""):
            return PreviewResult.failure(_extract_error_message(payload))
        if payload.get("success") is False or payload.get("ok") is False:
            return PreviewResult.failure(_extract_error_message(payload))
    return PreviewResult.success(payload)


# ===========================================================================
# 6. Transaction pipeline: build -> preview gate -> INSPECT -> sign -> reconcile
# ===========================================================================

#: Module-private token. Only :func:`inspect_tx` holds it, so an ``InspectedTx``
#: cannot be constructed anywhere else — the gate has no bypass.
_INSPECTION_TOKEN = object()


@dataclass(frozen=True)
class TxExpectation:
    """What the caller believes it asked the builder to produce."""

    sender: object                 # SuiAddress
    gas: GasConfig
    intent: str
    expected_package: object = None
    expects_sponsor: object = None  # True/False/None (None = don't care)


class InspectedTx:
    """A transaction that has passed inspection.

    Possession of one of these is proof the gate ran: the constructor rejects
    every caller that does not hold the module-private token, and only
    :func:`inspect_tx` does.
    """

    __slots__ = ("tx_kind", "sponsor_signature", "signing_digest",
                 "transaction_bytes", "deferred", "expectation", "raw")

    def __init__(self, token, *, tx_kind, sponsor_signature, signing_digest,
                 transaction_bytes, deferred, expectation, raw):
        if token is not _INSPECTION_TOKEN:
            raise TypeError(
                "InspectedTx cannot be constructed directly — it is produced "
                "only by inspect_tx(). This gate is not bypassable."
            )
        self.tx_kind = tx_kind
        self.sponsor_signature = sponsor_signature
        self.signing_digest = signing_digest
        self.transaction_bytes = transaction_bytes
        self.deferred = deferred
        self.expectation = expectation
        self.raw = raw

    @property
    def is_sponsored(self):
        """True when the builder attached a sponsor signature.

        When sponsored, ``txKind`` carries full BCS ``Transaction`` bytes with
        gas payment already attached — NOT a bare ``TransactionKind``. Wrapping
        it again would corrupt the transaction, so the distinction matters.
        """
        return self.sponsor_signature is not None

    def report(self):
        """A signable-but-unsigned summary. This is what dry-run returns."""
        return {
            "inspected": True,
            "intent": self.expectation.intent,
            "sender": str(self.expectation.sender),
            "gasMode": self.expectation.gas.mode,
            "gasBudgetMist": self.expectation.gas.budget_mist,
            "sponsored": self.is_sponsored,
            "hasTxKind": self.tx_kind is not None,
            "hasSigningDigest": self.signing_digest is not None,
            "hasDeferredArgs": self.deferred is not None,
            "deferredArgKeys": sorted(self.deferred) if isinstance(self.deferred, dict) else None,
            "txKindBytes": len(self.tx_kind) if self.tx_kind else 0,
        }


_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _looks_base64(s):
    return isinstance(s, str) and bool(_BASE64_RE.match(s)) and len(s) % 4 == 0


def _pick_string(obj, keys):
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def inspect_tx(built, expectation):
    """Verify a built transaction matches intent, or raise.

    A builder response is untrusted input: it arrives as opaque bytes and
    signing it is irreversible. This is the step between "the server gave me a
    transaction" and "I signed it".

    Handles both builder response shapes seen in the V2 spec:

    * native ``/api/perpetuals/**/transactions/*`` -> ``{txKind, sponsorSignature?}``
      (plus ``deferred`` from ``create-account`` when ``deferShare = true``)
    * CCXT ``/api/ccxt/build/*`` -> ``{signingDigest, transactionBytes}``

    Raises rather than returning a falsy value: an inspection failure must not
    be ignorable by a caller that forgets to check a boolean.
    """
    intent = expectation.intent

    if not isinstance(built, dict):
        raise TxInspectionError("builder returned no transaction object", intent)

    err = built.get("error")
    if err not in (None, False, ""):
        raise TxInspectionError(f"builder returned an error: {_extract_error_message(built)}", intent)

    tx_kind = built.get("txKind")
    signing_digest = built.get("signingDigest")
    transaction_bytes = built.get("transactionBytes")
    sponsor_signature = built.get("sponsorSignature")
    # create-account with deferShare=true answers with deferred PTB ARGUMENT
    # REFERENCES ({accountArg, adminCapArg, sharePolicyArg, collateralCoinType})
    # alongside txKind. Never hardcode the simple {txKind} shape. (gotchas §12)
    deferred = built.get("deferred")

    if tx_kind is None and signing_digest is None:
        raise TxInspectionError(
            "response carries neither txKind nor signingDigest — nothing to sign",
            intent,
        )

    if tx_kind is not None:
        if not isinstance(tx_kind, str) or not tx_kind:
            raise TxInspectionError("txKind is not a non-empty string", intent)
        if not _looks_base64(tx_kind):
            raise TxInspectionError("txKind is not valid base64", intent)

    if signing_digest is not None:
        # CCXT build path. Sign the DIGEST, never transactionBytes. (gotchas §2)
        if not _looks_base64(signing_digest):
            raise TxInspectionError("signingDigest is not valid base64", intent)
        if not isinstance(transaction_bytes, str) or not transaction_bytes:
            raise TxInspectionError(
                "signingDigest present without transactionBytes", intent
            )
        if not _looks_base64(transaction_bytes):
            raise TxInspectionError("transactionBytes is not valid base64", intent)

    if sponsor_signature is not None and not isinstance(sponsor_signature, str):
        raise TxInspectionError("sponsorSignature is not a string", intent)

    if deferred is not None and not isinstance(deferred, dict):
        raise TxInspectionError("deferred PTB arguments are not an object", intent)

    # Sender echo, when the builder provides one.
    echoed_sender = _pick_string(built, ("sender", "walletAddress", "from"))
    if echoed_sender and echoed_sender.lower() != str(expectation.sender).lower():
        raise TxInspectionError(
            f"sender mismatch: expected {expectation.sender}, transaction is for {echoed_sender}",
            intent,
        )

    echoed_package = _pick_string(built, ("packageId", "package", "target"))
    if expectation.expected_package and echoed_package and echoed_package != expectation.expected_package:
        raise TxInspectionError(
            f"package mismatch: expected {expectation.expected_package}, "
            f"transaction targets {echoed_package}",
            intent,
        )

    # Gas sanity. Sponsor and sender may legitimately be equal on Sui, so only
    # the PRESENCE of sponsorship is checked, never inequality of addresses.
    if expectation.gas.mode == "self" and sponsor_signature is not None:
        raise TxInspectionError(
            "gas mode is 'self' but the builder attached a sponsor signature",
            intent,
        )
    if expectation.expects_sponsor is True and sponsor_signature is None:
        raise TxInspectionError(
            "sponsored gas was requested but the builder returned no sponsor "
            "signature — the gas pool did not sponsor this transaction",
            intent,
        )

    return InspectedTx(
        _INSPECTION_TOKEN,
        tx_kind=tx_kind,
        sponsor_signature=sponsor_signature,
        signing_digest=signing_digest,
        transaction_bytes=transaction_bytes,
        deferred=deferred,
        expectation=expectation,
        raw=built,
    )


# ===========================================================================
# 7. Safety — margin, sizing, circuit breakers, kill switch
# ===========================================================================
#
# Aftermath uses ISOLATED margin: wallet USDC -> deposit -> account
# *unallocated* collateral -> explicit allocate -> per-position isolated margin.
# Unallocated collateral protects nothing. Any code assuming cross-margin is
# wrong about this exchange.

MARGIN_ZONES = ("SAFE", "WARNING", "DANGER", "LIQUIDATION", "NO_POSITION")


@dataclass(frozen=True)
class MarginHealth:
    zone: str
    margin_ratio: float
    maintenance_ratio: float
    buffer_multiple: float


def assess_margin_health(margin_ratio, maintenance_ratio):
    """Zones off the position's API-reported ratio vs maintenance:
    ``>2x`` safe, ``1.5-2x`` warning, ``1-1.5x`` danger, ``<1x`` liquidation."""
    try:
        mr = float(margin_ratio)
        mm = float(maintenance_ratio)
    except (TypeError, ValueError):
        raise ValueError("margin data missing — refusing to guess position health")
    if mr != mr or mm != mm or mm in (float("inf"), float("-inf")):
        raise ValueError("margin data non-finite — refusing to guess position health")
    if mm <= 0:
        raise ValueError("maintenance margin ratio must be positive")

    buffer = mr / mm
    if buffer < 1:
        zone = "LIQUIDATION"
    elif buffer < 1.5:
        zone = "DANGER"
    elif buffer < 2:
        zone = "WARNING"
    else:
        zone = "SAFE"
    return MarginHealth(zone, mr, mm, buffer)


def max_size_for_risk(account_collateral, entry_price, stop_loss_price, risk_percent=2.0):
    """The 2% rule: never risk more than ``risk_percent`` of account collateral
    on one trade, given the distance to the stop."""
    if account_collateral <= 0:
        raise ValueError("account collateral must be positive")
    if risk_percent <= 0 or risk_percent > 100:
        raise ValueError("risk_percent must be in (0, 100]")
    distance = abs(float(entry_price) - float(stop_loss_price))
    if distance <= 0:
        raise ValueError("entry and stop-loss prices must differ")
    return (float(account_collateral) * (risk_percent / 100.0)) / distance


@dataclass
class SoftLimits:
    """Tier 1 — advisory. Warnings only; trading continues."""

    max_drawdown_pct: float = 0.05
    max_position_notional: float = 50_000.0
    max_leverage: float = 5.0
    min_margin_buffer: float = 2.0


@dataclass
class HardLimits:
    """Tier 2 — binding. Any breach halts trading."""

    max_drawdown_pct: float = 0.15
    max_daily_loss: float = 5_000.0
    max_daily_trades: int = 200


@dataclass
class BotState:
    drawdown_pct: float = 0.0
    position_notional: float = 0.0
    effective_leverage: float = 0.0
    margin_buffer: float = float("inf")
    daily_loss: float = 0.0
    daily_trade_count: int = 0


def check_soft_limits(limits, state):
    """Returns human-readable warnings. Does not stop trading."""
    warnings = []
    if state.drawdown_pct > limits.max_drawdown_pct:
        warnings.append(
            f"drawdown {state.drawdown_pct * 100:.1f}% exceeds soft limit "
            f"{limits.max_drawdown_pct * 100:.1f}%"
        )
    if state.position_notional > limits.max_position_notional:
        warnings.append(
            f"position notional {state.position_notional:.2f} exceeds "
            f"{limits.max_position_notional}"
        )
    if state.effective_leverage > limits.max_leverage:
        warnings.append(
            f"leverage {state.effective_leverage:.1f}x exceeds {limits.max_leverage}x"
        )
    if state.margin_buffer < limits.min_margin_buffer:
        warnings.append(
            f"margin buffer {state.margin_buffer:.2f}x below {limits.min_margin_buffer}x"
        )
    return warnings


def enforce_hard_limits(limits, state):
    """Non-None return means STOP TRADING NOW."""
    if state.drawdown_pct > limits.max_drawdown_pct:
        return "HALT: maximum drawdown exceeded"
    if state.daily_loss > limits.max_daily_loss:
        return "HALT: daily loss limit reached"
    if state.daily_trade_count > limits.max_daily_trades:
        return "HALT: daily trade limit reached"
    return None


class KillSwitch:
    """Heartbeat-driven dead-man switch.

    The API deliberately provides no server-side dead-man switch (skills
    gotchas.md §13), so the bot owns it. If the strategy loop stalls past
    ``max_silence_s``, all open orders are cancelled.

    Cancellation is **verified**, not assumed: ``cancel_all`` is expected to
    re-read pending orders and raise if any survive. A kill switch that reports
    success without confirming is worse than none, because it hides exposure.
    """

    def __init__(self, max_silence_s, cancel_all, log=None):
        self.max_silence_s = float(max_silence_s)
        self._cancel_all = cancel_all
        self._log = log or (lambda m: print(m, file=sys.stderr))
        self._last_heartbeat = time.monotonic()
        self._armed = True
        self._lock = threading.Lock()
        self.fired = False

    def heartbeat(self):
        self._last_heartbeat = time.monotonic()

    @property
    def is_armed(self):
        return self._armed

    def silence_s(self):
        return time.monotonic() - self._last_heartbeat

    def check(self):
        """Returns True if the switch fired."""
        if not self._armed:
            return False
        silence = self.silence_s()
        if silence <= self.max_silence_s:
            return False
        self.trigger(f"heartbeat timeout — {silence:.1f}s since last beat")
        return True

    def trigger(self, reason):
        with self._lock:
            if not self._armed:
                return
            self._armed = False
        self._log(f"KILL SWITCH: {reason}")
        try:
            self._cancel_all()
        except Exception as exc:
            # Re-arm so a later attempt can still fire; the exposure is real.
            self._armed = True
            self._log(f"kill switch: cancellation FAILED — {exc}")
            raise
        self.fired = True
        self._log("kill switch: all orders cancelled and VERIFIED")

    def disarm(self):
        self._armed = False

    def rearm(self):
        self._armed = True
        self._last_heartbeat = time.monotonic()


def install_shutdown_handlers(kill_switch, timeout_s=20.0, exit_fn=None):
    """SIGINT/SIGTERM cancel all orders before exiting.

    Exits non-zero if cancellation failed, so a supervisor can distinguish a
    clean shutdown from one that left orders resting on the book.
    """
    state = {"shutting_down": False}
    _exit = exit_fn or sys.exit

    def _shutdown(signum, _frame):
        if state["shutting_down"]:
            return
        state["shutting_down"] = True
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        code = 0
        done = threading.Event()
        result = {}

        def _run():
            try:
                kill_switch.trigger(f"{name} — shutting down")
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        if not done.wait(timeout_s):
            print(f"shutdown: cancellation timed out after {timeout_s}s", file=sys.stderr)
            code = 1
        elif result.get("error"):
            print(f"shutdown: {result['error']}", file=sys.stderr)
            code = 1
        _exit(code)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):  # not on the main thread
            pass
    return _shutdown


# ===========================================================================
# 8. Value scaling
# ===========================================================================

_SIZE_SCALE = Decimal("1000000000")   # B9 base units
_PRICE_SCALE = Decimal("1000000000")  # B9 price units
_COLLATERAL_SCALE = Decimal("1000000")  # USDC, 6 decimals


def _to_decimal(value, field_name):
    try:
        val = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if val <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return val


def scale_size(human_size, lot_size):
    """Human base size -> lot-aligned native BigInt wire string."""
    from decimal import ROUND_HALF_UP

    lot = from_native_bigint(lot_size)
    raw = int((_to_decimal(human_size, "size") * _SIZE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    scaled = (raw // lot) * lot
    if scaled <= 0:
        raise ValueError(f"size is below minimum lot size ({lot} base units)")
    return to_native_bigint(scaled)


def scale_price(human_price, tick_size, side=None):
    """Human USD price -> tick-aligned native BigInt wire string.

    Bids round DOWN and asks round UP, so rounding never crosses the price the
    caller asked for.
    """
    from decimal import ROUND_CEILING, ROUND_HALF_UP

    tick = from_native_bigint(tick_size)
    raw = int((_to_decimal(human_price, "price") * _PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    if side in (0, "0", "buy", "long", "bid"):
        scaled = (raw // tick) * tick
    elif side in (1, "1", "sell", "short", "ask"):
        scaled = int((Decimal(raw) / Decimal(tick)).to_integral_value(rounding=ROUND_CEILING)) * tick
    else:
        scaled = int((Decimal(raw) / Decimal(tick)).to_integral_value(rounding=ROUND_HALF_UP)) * tick
    if scaled <= 0:
        raise ValueError(f"price is below minimum tick size ({tick} quote units)")
    return to_native_bigint(scaled)


def unscale_size(scaled):
    return float(Decimal(from_native_bigint(scaled)) / _SIZE_SCALE)


def unscale_price(scaled):
    return float(Decimal(from_native_bigint(scaled)) / _PRICE_SCALE)


def scale_collateral(amount):
    """Human USDC -> integer collateral units (6 decimals)."""
    return int((_to_decimal(amount, "amount") * _COLLATERAL_SCALE).to_integral_value())


def normalize_side(side):
    """Normalize a side to the native int enum: 0 = bid/long, 1 = ask/short."""
    if isinstance(side, bool):
        raise ValueError(f"invalid side {side!r}")
    if isinstance(side, int) and side in (0, 1):
        return side
    s = str(side).lower().strip()
    if s in ("buy", "long", "bid", "0"):
        return 0
    if s in ("sell", "short", "ask", "1"):
        return 1
    raise ValueError(f"invalid side {side!r}; use buy|sell|long|short")


def side_label(side_int):
    return "long" if int(side_int) == 0 else "short"


# ===========================================================================
# 9. The adapter
# ===========================================================================

#: Coin- and gas-object-sensitive operations must be serialized. Parallel
#: deposits race on the same Sui coin objects and fail with version /
#: equivocation errors that look like API flakiness but are not.
_COIN_LOCK = threading.RLock()

_MARKET_CACHE_TTL_S = 300


class AftermathAdapter:
    """The venue boundary. Everything the repo does to Aftermath happens here.

    The public method surface is deliberately the shape strategies already
    call, so a strategy written against it does not have to know about market
    ids, isolated-margin allocation, BigInt wire formats, or gas modes.
    """

    #: Kept in lockstep with :class:`af_mock.AftermathMockAdapter`; parity is
    #: enforced by ``tests/test_mock_parity.py``, not by discipline.
    def __init__(self, config=None, session=None):
        self.config = config or AdapterConfig.from_env()
        self._session = session
        self._markets_cache = None
        self._markets_fetched_at = 0.0
        self._state_cache = None
        self._signer = None
        self.soft_limits = SoftLimits()
        self.hard_limits = HardLimits()
        self.bot_state = BotState()

    # -- introspection ----------------------------------------------------

    @property
    def base_url(self):
        return self.config.base_url

    @property
    def is_armed(self):
        return bool(self.config.armed)

    def gas_config(self):
        sponsor = None
        if self.config.gas_mode == "sponsored" and self.config.wallet_address:
            # Sponsor and sender may be the same address on Sui.
            sponsor = sui_address(self.config.wallet_address)
        return GasConfig(
            mode=self.config.gas_mode,
            budget_mist=self.config.gas_budget_mist,
            gas_coin_type=self.config.gas_coin_type,
            sponsor_wallet=sponsor,
        )

    def wallet(self):
        if not self.config.wallet_address:
            raise ConfigError(
                "AFTERMATH_WALLET_ADDRESS is not set. It is the only mandatory "
                "setting — see .env.example."
            )
        return sui_address(self.config.wallet_address)

    # -- transport --------------------------------------------------------

    def _http(self):
        if _requests is None:
            raise AftermathApiError(
                "requests is not installed; run: pip install -r requirements.txt"
            )
        return self._session or _requests

    def request(self, path, body=None, method="POST", retries=None):
        """The ONLY place an HTTP request to Aftermath is made.

        Nearly every route in this API is POST, including reads. Retries use
        exponential backoff and only fire for genuinely retryable failures —
        retrying a 400 just multiplies the same rejection.
        """
        url = self.base_url + path
        attempts = self.config.max_retries if retries is None else retries
        http = self._http()
        last_exc = None

        for attempt in range(max(1, attempts)):
            try:
                if method == "GET":
                    resp = http.get(url, timeout=self.config.timeout_s)
                else:
                    resp = http.post(url, json=body if body is not None else {},
                                     timeout=self.config.timeout_s)
            except Exception as exc:  # transport-level
                last_exc = exc
                if not _is_retryable_exc(exc) or attempt == attempts - 1:
                    raise AftermathApiError(
                        f"request failed for {path}: {exc}", endpoint=path, retryable=True
                    ) from exc
                time.sleep(0.25 * (2 ** attempt))
                continue

            status = getattr(resp, "status_code", 200)
            headers = dict(getattr(resp, "headers", {}) or {})

            if status >= 400:
                detail = ""
                try:
                    detail = _extract_error_message(resp.json())
                except Exception:
                    detail = (getattr(resp, "text", "") or "").strip()
                if status in _RETRYABLE_STATUS and attempt < attempts - 1:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise AftermathApiError(
                    f"{path}: HTTP {status}: {detail or 'no detail'}",
                    endpoint=path, status=status,
                    retryable=status in _RETRYABLE_STATUS,
                )

            try:
                payload = resp.json()
            except Exception as exc:
                raise AftermathApiError(
                    f"{path}: response was not JSON", endpoint=path, status=status
                ) from exc

            result = classify_payload(payload, headers)
            if not result.ok:
                raise AftermathApiError(
                    f"{path}: {result.error}", endpoint=path, status=status
                )
            return result.value

        raise AftermathApiError(f"request failed for {path}: {last_exc}", endpoint=path)

    def preview(self, path, body):
        """Run a preview and return a tagged union. Never raises on rejection."""
        try:
            payload = self.request(path, body)
        except AftermathApiError as exc:
            return PreviewResult.failure(str(exc))
        return classify_payload(payload)

    # -- account identity -------------------------------------------------

    def discover_accounts(self, wallet=None):
        """POST /api/perpetuals/accounts/owned -> ``{accountCaps: [...]}``.

        Requires ``walletAddress``; the response is an object, not a bare array.
        This is how the repo is turnkey: no account id is ever pasted by hand.
        """
        addr = sui_address(wallet) if wallet else self.wallet()
        data = self.request("/api/perpetuals/accounts/owned", {
            "walletAddress": str(addr),
            "collateralCoinTypes": [self.config.collateral_coin_type],
        })
        caps = data.get("accountCaps", []) if isinstance(data, dict) else []
        out = []
        for cap in caps:
            out.append({
                "accountId": native_account_id(cap.get("accountId")),
                "accountCapId": account_cap_id(cap.get("objectId")),
                "accountObjectId": cap.get("accountObjectId"),
                "collateral": from_native_bigint(cap.get("collateral", 0)),
                "collateralCoinType": cap.get("collateralCoinType"),
                "isAgent": cap.get("isAgent"),
                "walletAddress": cap.get("walletAddress"),
            })
        return out

    def account_id(self):
        """The active native account id, discovering it when not configured."""
        if self.config.account_id is not None:
            return self.config.account_id
        accounts = self.discover_accounts()
        if not accounts:
            raise ConfigError(
                "no perpetuals account found for this wallet. Run "
                "`python3 scripts/doctor.py` for guidance, or "
                "`python3 scripts/trade.py account onboard --dry-run` to see the "
                "onboarding transaction."
            )
        object.__setattr__(self.config, "account_id", accounts[0]["accountId"])
        if self.config.account_cap_id is None:
            object.__setattr__(self.config, "account_cap_id", accounts[0]["accountCapId"])
        return self.config.account_id

    def _account_fields(self):
        """The account half of every native transaction body.

        Native account ids go on the wire as ``"123n"`` — never a plain number.
        """
        fields = {"accountId": self.account_id().wire()}
        if self.config.account_cap_id is not None:
            fields["accountCapId"] = str(self.config.account_cap_id)
        return fields

    def _builder_code(self):
        """v3.0.0 builder code: ``{integratorId (u32), integratorFee}``.

        Pre-v3 ``integratorAddress`` / ``builderCode.takerFee`` are gone.
        Returns None when no integrator is configured — the common case.
        """
        if self.config.integrator_id is None or self.config.integrator_fee is None:
            return None
        if self.config.max_integrator_fee is not None and \
                float(self.config.integrator_fee) > float(self.config.max_integrator_fee):
            raise ConfigError(
                f"AFTERMATH_INTEGRATOR_FEE {self.config.integrator_fee} exceeds "
                f"AFTERMATH_MAX_INTEGRATOR_FEE {self.config.max_integrator_fee}"
            )
        return {
            "integratorId": int(self.config.integrator_id),
            "integratorFee": float(self.config.integrator_fee),
        }

    def _sponsor_field(self):
        """``sponsor`` is ``{walletAddress}`` when the gas pool should pay."""
        gas = self.gas_config()
        if gas.mode == "sponsored" and gas.sponsor_wallet is not None:
            return {"walletAddress": str(gas.sponsor_wallet)}
        return None

    # -- market data ------------------------------------------------------

    def get_all_markets(self, force=False):
        """POST /api/perpetuals/all-markets -> ``{markets: [...]}``.

        Requires ``collateralCoinType``. The response is an object, NOT a bare
        array. Zero markets is expected pre-relaunch and is never an error.

        Ordering is deterministic (markets sort by symbol) — do not re-sort.
        """
        now = time.monotonic()
        if (not force and self._markets_cache is not None
                and now - self._markets_fetched_at < _MARKET_CACHE_TTL_S):
            return self._markets_cache
        data = self.request("/api/perpetuals/all-markets", {
            "collateralCoinType": self.config.collateral_coin_type,
        })
        markets = data.get("markets", []) if isinstance(data, dict) else []
        self._markets_cache = markets
        self._markets_fetched_at = now
        return markets

    def resolve_market(self, symbol_or_id, force=False):
        """Resolve a ticker or object id to ``(MarketId, symbol, market)``.

        Market ids are NOT tickers: the API validates them strictly, so they are
        always resolved from the API and never constructed.
        """
        markets = self.get_all_markets(force=force)
        target = str(symbol_or_id).strip()

        def _find(ms):
            if looks_like_object_id(target):
                for m in ms:
                    if m.get("objectId") == target:
                        return m
                return None
            up = target.upper()
            for m in ms:
                sym = str(m.get("marketParams", {}).get("baseAssetSymbol", "")).upper()
                if sym == up or sym == f"{up}USD" or sym.replace("USD", "") == up:
                    return m
            return None

        m = _find(markets)
        if m is None and not force:
            m = _find(self.get_all_markets(force=True))
        if m is None:
            if not markets:
                raise ConfigError(
                    f"cannot resolve {target!r}: no markets are live yet for "
                    f"{short_coin(self.config.collateral_coin_type)}. This is "
                    f"expected before relaunch."
                )
            raise ConfigError(
                f"unknown market {target!r}; run `query.py market list` to see "
                f"what is available"
            )
        return (market_id(m["objectId"]),
                m["marketParams"]["baseAssetSymbol"],
                m)

    def market_params(self, symbol_or_id):
        return self.resolve_market(symbol_or_id)[2].get("marketParams", {})

    def get_all_mids(self):
        """Index price per symbol, in one call."""
        return {
            m.get("marketParams", {}).get("baseAssetSymbol"): m.get("indexPrice")
            for m in self.get_all_markets()
        }

    def get_orderbook(self, symbol_or_id, limit=20):
        """POST /api/perpetuals/markets/orderbooks -> ``{orderbooks: [...]}``."""
        mid, sym, _ = self.resolve_market(symbol_or_id)
        data = self.request("/api/perpetuals/markets/orderbooks", {
            "marketIds": [str(mid)],
        })
        books = data.get("orderbooks", []) if isinstance(data, dict) else []
        if not books:
            raise AftermathApiError(f"no orderbook returned for {sym} ({mid})")
        ob = books[0].get("orderbook", {})
        return {
            "symbol": sym,
            "marketId": str(mid),
            "midPrice": ob.get("midPrice"),
            "bestBid": ob.get("bestBidPrice"),
            "bestAsk": ob.get("bestAskPrice"),
            # bids are returned descending and asks ascending; ordering is part
            # of the contract, so it is sliced, never re-sorted.
            "bids": (ob.get("bids") or [])[:limit],
            "asks": (ob.get("asks") or [])[:limit],
            "bidsTotalSize": ob.get("bidsTotalSize"),
            "asksTotalSize": ob.get("asksTotalSize"),
            "nonce": ob.get("nonce"),
        }

    #: v1-shaped alias so existing strategies keep working.
    def get_snapshot(self, symbol_or_id, limit=20):
        return self.get_orderbook(symbol_or_id, limit=limit)

    def get_24hr_stats(self, symbols=None):
        """POST /api/perpetuals/markets/24hr-stats — aligned with request order."""
        markets = self.get_all_markets()
        if symbols:
            wanted = {str(s).upper() for s in symbols}
            markets = [
                m for m in markets
                if str(m.get("marketParams", {}).get("baseAssetSymbol", "")).upper() in wanted
            ]
        if not markets:
            return []
        ids = [m["objectId"] for m in markets]
        data = self.request("/api/perpetuals/markets/24hr-stats", {"marketIds": ids})
        stats = data.get("marketsStats", []) if isinstance(data, dict) else []
        out = []
        for m, s in zip(markets, stats):
            row = {"symbol": m.get("marketParams", {}).get("baseAssetSymbol"),
                   "marketId": m.get("objectId")}
            row.update(s or {})
            out.append(row)
        return out

    def get_candles(self, symbol_or_id, resolution="1h", count_back=24,
                    from_timestamp=None, to_timestamp=None):
        """POST /api/perpetuals/market/candle-history.

        v3.0.0: the field is ``resolution`` and it takes a CCXT-style timeframe
        STRING (``"1m"``, ``"1h"``, ``"1d"``…). The pre-v3 ``intervalMs``
        integer no longer exists.
        """
        if resolution not in CANDLE_RESOLUTIONS:
            raise ValueError(
                f"unsupported resolution {resolution!r}; use one of: "
                f"{', '.join(CANDLE_RESOLUTIONS)}"
            )
        mid, sym, _ = self.resolve_market(symbol_or_id)
        now_ms = int(time.time() * 1000)
        to_ts = int(to_timestamp) if to_timestamp else now_ms
        from_ts = (int(from_timestamp) if from_timestamp
                   else to_ts - _RESOLUTION_APPROX_MS[resolution] * max(1, int(count_back)))
        data = self.request("/api/perpetuals/market/candle-history", {
            "marketId": str(mid),
            "resolution": resolution,
            "fromTimestamp": from_ts,
            "toTimestamp": to_ts,
        })
        return {
            "symbol": sym,
            "marketId": str(mid),
            "resolution": resolution,
            "candles": data.get("candles", []) if isinstance(data, dict) else [],
        }

    def candle_subscription(self, symbol_or_id, resolution="1h"):
        """The WS subscription frame for live candles.

        v3.0.0 removed ``GET /api/perpetuals/ws/market-candles/{id}/{ms}``.
        Candles now stream over the general updates socket
        ``/api/perpetuals/ws/updates`` via a ``marketCandles`` subscription.
        Returns ``(ws_url, frame)``; this adapter does not open the socket.
        """
        if resolution not in CANDLE_RESOLUTIONS:
            raise ValueError(f"unsupported resolution {resolution!r}")
        mid, _, _ = self.resolve_market(symbol_or_id)
        ws_url = self.base_url.replace("https://", "wss://").replace("http://", "ws://") \
            + "/api/perpetuals/ws/updates"
        frame = {
            "action": "subscribe",
            "subscriptionType": {
                "marketCandles": {"marketId": str(mid), "interval": resolution},
            },
        }
        return ws_url, frame

    def get_market_trades(self, symbol_or_id, limit=20, before_timestamp_cursor=None):
        """POST /api/perpetuals/market/order-history. Paginates by cursor."""
        mid, sym, _ = self.resolve_market(symbol_or_id)
        body = {"marketId": str(mid), "limit": int(limit)}
        if before_timestamp_cursor is not None:
            body["beforeTimestampCursor"] = int(before_timestamp_cursor)
        data = self.request("/api/perpetuals/market/order-history", body)
        return {
            "symbol": sym,
            "marketId": str(mid),
            "trades": data.get("orders", []) if isinstance(data, dict) else [],
            "nextBeforeTimestampCursor": (data or {}).get("nextBeforeTimestampCursor"),
        }

    def get_funding(self, symbol=None):
        """Funding view assembled from market params + live state.

        Reads no removed fields: ``gasPriceTwapPeriodMs``, ``forceCancelFee``,
        ``gasPriceTakerFee`` and ``zScoreThreshold`` were deleted in v3.0.0
        (``priorityTakerFee`` replaces the last of them).
        """
        out = []
        for m in self.get_all_markets():
            params = m.get("marketParams", {})
            state = m.get("marketState", {})
            sym = params.get("baseAssetSymbol", "")
            if symbol and str(symbol).upper() not in (sym.upper(), sym.upper().replace("USD", "")):
                continue
            out.append({
                "symbol": sym,
                "marketId": m.get("objectId"),
                "estimatedFundingRate": m.get("estimatedFundingRate"),
                "premiumTwap": state.get("premiumTwap"),
                "spreadTwap": state.get("spreadTwap"),
                "cumFundingRateLong": state.get("cumFundingRateLong"),
                "cumFundingRateShort": state.get("cumFundingRateShort"),
                "indexPrice": m.get("indexPrice"),
                "nextFundingTimestampMs": m.get("nextFundingTimestampMs"),
                "fundingFrequencyMs": params.get("fundingFrequencyMs"),
                "fundingPeriodMs": params.get("fundingPeriodMs"),
            })
        return out

    def get_funding_history(self, symbol_or_id, from_timestamp=None,
                            to_timestamp=None, limit=100):
        """POST /api/perpetuals/market/funding-history (new in v3.0.0)."""
        mid, sym, _ = self.resolve_market(symbol_or_id)
        now_ms = int(time.time() * 1000)
        to_ts = int(to_timestamp) if to_timestamp else now_ms
        from_ts = int(from_timestamp) if from_timestamp else to_ts - 7 * 86_400_000
        data = self.request("/api/perpetuals/market/funding-history", {
            "marketId": str(mid),
            "fromTimestamp": from_ts,
            "toTimestamp": to_ts,
            "limit": int(limit),
        })
        return {"symbol": sym, "marketId": str(mid),
                "history": data.get("history", []) if isinstance(data, dict) else []}

    # -- account state ----------------------------------------------------

    def get_account_state(self, force=False):
        """POST /api/perpetuals/accounts/positions -> ``{accounts: [...]}``.

        Cached, and invalidated by every mutation — state goes stale the instant
        a fill, cancel, deposit, withdraw or leverage change lands.
        """
        if self._state_cache is not None and not force:
            return self._state_cache
        acct = self.account_id()
        data = self.request("/api/perpetuals/accounts/positions", {
            "accountIds": [acct.wire()],
        })
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        state = accounts[0] if accounts else {
            "accountId": acct.wire(), "positions": [],
            "availableCollateral": 0, "availableCollateralUsd": 0,
            "totalEquityUsd": 0, "totalUnrealizedPnlUsd": 0,
            "totalUnrealizedFundingsUsd": 0,
        }
        self._state_cache = state
        return state

    def _invalidate_state(self):
        """Called after EVERY mutation. Never compute risk off stale state."""
        self._state_cache = None

    def get_positions(self, symbol=None, force=False):
        """Open positions. Ordered by market id — deterministic, do not re-sort.

        Position ``makerFee`` / ``takerFee`` were REMOVED in v3.0.0; nothing
        here reads them. Fee rates live on ``marketParams``.
        """
        state = self.get_account_state(force=force)
        positions = state.get("positions", []) or []
        if symbol:
            mid, _, _ = self.resolve_market(symbol)
            positions = [p for p in positions if p.get("marketId") == str(mid)]
        return positions

    def get_open_orders(self, symbol=None, force=False):
        """Pending orders, flattened out of positions.

        Pending bids and asks are each ordered by order id — part of the
        contract, so they are preserved as returned.
        """
        orders = []
        for pos in self.get_positions(symbol=symbol, force=force):
            mkt = pos.get("marketId")
            for order in pos.get("pendingOrders", []) or []:
                if isinstance(order, dict):
                    orders.append({"marketId": mkt, **order})
                else:
                    orders.append({"marketId": mkt, "order": order})
        return orders

    def get_order_history(self, limit=20, before_timestamp_cursor=None, event_types=None):
        """POST /api/perpetuals/account/order-history. Cursor-paginated."""
        body = {"accountId": self.account_id().wire(), "limit": int(limit)}
        if before_timestamp_cursor is not None:
            body["beforeTimestampCursor"] = int(before_timestamp_cursor)
        if event_types:
            body["eventTypes"] = list(event_types)
        data = self.request("/api/perpetuals/account/order-history", body)
        return {
            "orders": data.get("orders", []) if isinstance(data, dict) else [],
            "nextBeforeTimestampCursor": (data or {}).get("nextBeforeTimestampCursor"),
        }

    def margin_health(self, symbol=None):
        """Margin zone per position, off the API-reported ratios."""
        out = []
        markets = {m["objectId"]: m for m in self.get_all_markets()}
        for pos in self.get_positions(symbol=symbol):
            mkt = markets.get(pos.get("marketId")) or {}
            maintenance = mkt.get("marketParams", {}).get("marginRatioMaintenance")
            try:
                health = assess_margin_health(pos.get("marginRatio"), maintenance)
                row = {"zone": health.zone, "marginRatio": health.margin_ratio,
                       "maintenanceRatio": health.maintenance_ratio,
                       "bufferMultiple": health.buffer_multiple}
            except ValueError as exc:
                row = {"zone": "UNKNOWN", "error": str(exc)}
            row.update({
                "marketId": pos.get("marketId"),
                "symbol": mkt.get("marketParams", {}).get("baseAssetSymbol"),
                "liquidationPrice": pos.get("liquidationPrice"),
                "leverage": pos.get("leverage"),
            })
            out.append(row)
        return out

    def max_order_size(self, symbol_or_id, side, price=None, leverage=None):
        """POST /api/perpetuals/account/max-order-size — guard before sizing up."""
        mid, _, _ = self.resolve_market(symbol_or_id)
        body = {
            "accountId": self.account_id().wire(),
            "marketId": str(mid),
            "side": normalize_side(side),
        }
        if price is not None:
            body["price"] = float(price)
        if leverage is not None:
            body["leverage"] = float(leverage)
        bc = self._builder_code()
        if bc:
            body["builderCode"] = bc
        return self.request("/api/perpetuals/account/max-order-size", body)

    def size_for_risk(self, symbol_or_id, entry_price, stop_loss_price, risk_percent=2.0):
        """The 2% rule against real account collateral, guarded by max-order-size."""
        state = self.get_account_state()
        collateral = float(state.get("availableCollateralUsd") or state.get("availableCollateral") or 0)
        if collateral <= 0:
            raise ConfigError(
                "account has no available collateral — deposit and allocate before sizing"
            )
        size = max_size_for_risk(collateral, entry_price, stop_loss_price, risk_percent)
        side = 0 if float(stop_loss_price) < float(entry_price) else 1
        cap = None
        try:
            resp = self.max_order_size(symbol_or_id, side, price=entry_price)
            raw = (resp or {}).get("maxOrderSize") or (resp or {}).get("size")
            if raw is not None:
                cap = unscale_size(raw) if is_native_bigint(raw) else float(raw)
        except (AftermathApiError, ConfigError, TypeError):
            cap = None  # advisory only; never block sizing on a read failure
        return {
            "riskPercent": risk_percent,
            "accountCollateralUsd": collateral,
            "size": min(size, cap) if cap else size,
            "unclampedSize": size,
            "maxOrderSize": cap,
            "clamped": bool(cap and cap < size),
        }

    # -- circuit breakers -------------------------------------------------

    def check_circuit_breakers(self, state=None):
        """Two tiers: soft warns, hard halts. Both always run before a write."""
        st = state or self.bot_state
        warnings = check_soft_limits(self.soft_limits, st)
        halt = enforce_hard_limits(self.hard_limits, st)
        return {"warnings": warnings, "halt": halt, "tradingAllowed": halt is None}

    def _guard_write(self, intent):
        result = self.check_circuit_breakers()
        if result["halt"]:
            raise RuntimeError(f"{result['halt']} — refusing to {intent}")
        return result["warnings"]

    # -- transaction pipeline ---------------------------------------------

    def build_gated(self, build_path, body, intent, preview_path=None,
                    preview_body=None, tx_kind=None):
        """build -> preview gate -> INSPECT. Returns an :class:`InspectedTx`.

        The preview runs FIRST when a counterpart exists, and a preview error
        blocks the build entirely — no transaction is constructed for something
        already known to fail. Preview bodies are built separately because the
        preview routes accept a strict subset of fields (no ``walletAddress``,
        no ``sponsor``, no ``txKind``).
        """
        self._guard_write(intent)

        if preview_path and preview_body is not None:
            result = self.preview(preview_path, preview_body)
            if not result.ok:
                raise PreviewRejected(f"preview rejected ({intent}): {result.error}")

        payload = dict(body)
        sponsor = self._sponsor_field()
        if sponsor is not None:
            payload["sponsor"] = sponsor
        if tx_kind is not None:
            # Composing into an existing PTB rather than starting a new one.
            payload["txKind"] = tx_kind

        built = self.request(build_path, payload)
        expectation = TxExpectation(
            sender=self.wallet(),
            gas=self.gas_config(),
            intent=intent,
            # A sponsored build is only *expected* to come back sponsored when
            # we actually asked the pool to pay.
            expects_sponsor=None,
        )
        return inspect_tx(built, expectation)

    def set_signer(self, signer):
        """Install a signer. Signing still requires ``AFTERMATH_ARMED=1``.

        ``signer(inspected_tx) -> dict`` performs wrap/sign/submit. The shipped
        default is no signer at all, so nothing can be broadcast.
        """
        self._signer = signer

    def sign_and_submit(self, inspected, reconcile=None):
        """Sign and submit an INSPECTED transaction.

        The parameter type is the gate: an ``InspectedTx`` can only be produced
        by :func:`inspect_tx`, so there is no path from a raw builder response
        to a signature.
        """
        if not isinstance(inspected, InspectedTx):
            raise TypeError(
                "sign_and_submit() accepts only an InspectedTx produced by "
                "inspect_tx(). Inspection is not bypassable."
            )
        if not self.is_armed:
            raise NotArmedError(
                "adapter is not armed — nothing was signed or submitted. "
                "Set AFTERMATH_ARMED=1 to enable live trading (see README)."
            )
        if self._signer is None:
            raise NotArmedError(
                "no signer installed — nothing was signed or submitted. "
                "See scripts/_signing.py and README 'Arming'."
            )
        with _COIN_LOCK:
            result = self._signer(inspected)
        self._invalidate_state()
        if reconcile is not None:
            self.reconcile(reconcile, intent=inspected.expectation.intent)
        return result

    def submit_or_report(self, inspected, reconcile=None):
        """Submit when armed; otherwise return the inspection report.

        This is what every write command calls, so the dry-run default is
        structural rather than a flag each command remembers to check.
        """
        if not self.is_armed or self._signer is None:
            return {
                "submitted": False,
                "dryRun": True,
                "reason": ("adapter is not armed (AFTERMATH_ARMED unset)"
                           if not self.is_armed else "no signer installed"),
                "inspection": inspected.report(),
            }
        return {"submitted": True, "dryRun": False,
                "inspection": inspected.report(),
                "execution": self.sign_and_submit(inspected, reconcile=reconcile)}

    def reconcile(self, verify, attempts=5, delay_s=0.4, intent=""):
        """Re-read authoritative state until intent is observed.

        A 200 from submit means "accepted", not "applied". Never treat a submit
        response as final truth.
        """
        for i in range(attempts):
            self._invalidate_state()
            if verify():
                return True
            if i < attempts - 1:
                time.sleep(delay_s * (i + 1))
        raise RuntimeError(
            f"reconciliation failed ({intent}): expected state not observed "
            f"after {attempts} attempts"
        )

    # -- orders: atomic primitives ----------------------------------------

    def place_market_order(self, symbol_or_id, side, size, slippage=0.01,
                           reduce_only=False, leverage=None, stop_loss_price=None,
                           take_profit_price=None, trigger_price_type=TRIGGER_PRICE_INDEX):
        """ONE tx: market order, optionally carrying its SL/TP.

        v3.0.0 field names: ``stopLossPrice`` / ``takeProfitPrice``
        (previously ``stopLossIndexPrice`` / ``takeProfitIndexPrice``).
        """
        mid, sym, market = self.resolve_market(symbol_or_id)
        params = market.get("marketParams", {})
        scaled_size = scale_size(size, params["lotSize"])
        side_int = normalize_side(side)
        has_position = self._has_position(str(mid))

        preview_body = {
            "accountId": self.account_id().wire(),
            "marketId": str(mid),
            "side": side_int,
            "size": scaled_size,
            "reduceOnly": bool(reduce_only),
        }
        if leverage is not None:
            preview_body["leverage"] = float(leverage)

        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "side": side_int,
            "size": scaled_size,
            "slippage": float(slippage),
            "reduceOnly": bool(reduce_only),
            "hasPosition": has_position,
            "collateralChange": 0.0,
            "cancelSlTp": False,
        }
        if leverage is not None:
            body["leverage"] = float(leverage)
        bc = self._builder_code()
        if bc:
            body["builderCode"] = bc
        sl_tp = self._sl_tp_field(stop_loss_price, take_profit_price, trigger_price_type)
        if sl_tp:
            body["slTp"] = sl_tp

        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/place-market-order",
            body,
            intent=f"market {side_label(side_int)} {size} {sym}",
            preview_path="/api/perpetuals/account/previews/place-market-order",
            preview_body=preview_body,
        )
        return {"symbol": sym, "marketId": str(mid),
                **self.submit_or_report(inspected)}

    def place_limit_order(self, symbol_or_id, side, size, price,
                          order_type=ORDER_TYPE_GTC, reduce_only=False,
                          post_only=False, client_order_id=None, leverage=None,
                          expiry_timestamp=None, stop_loss_price=None,
                          take_profit_price=None,
                          trigger_price_type=TRIGGER_PRICE_INDEX):
        """ONE tx: limit order, optionally carrying its SL/TP."""
        otype = ORDER_TYPE_POST_ONLY if post_only else int(order_type)
        if otype not in VALID_ORDER_TYPES:
            raise ValueError(
                "order_type must be 0 (GTC), 1 (FOK), 2 (PostOnly) or 3 (IOC)"
            )
        mid, sym, market = self.resolve_market(symbol_or_id)
        params = market.get("marketParams", {})
        side_int = normalize_side(side)
        scaled_size = scale_size(size, params["lotSize"])
        scaled_price = scale_price(price, params["tickSize"], side=side_int)

        preview_body = {
            "accountId": self.account_id().wire(),
            "marketId": str(mid),
            "side": side_int,
            "size": scaled_size,
            "price": scaled_price,
            "orderType": otype,
            "reduceOnly": bool(reduce_only),
        }
        if leverage is not None:
            preview_body["leverage"] = float(leverage)

        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "side": side_int,
            "size": scaled_size,
            "price": scaled_price,
            "orderType": otype,
            "reduceOnly": bool(reduce_only),
            "hasPosition": self._has_position(str(mid)),
            "collateralChange": 0.0,
            "cancelSlTp": False,
        }
        if client_order_id is not None:
            body["clientOrderId"] = to_native_bigint(int(client_order_id))
        if leverage is not None:
            body["leverage"] = float(leverage)
        if expiry_timestamp is not None:
            body["expiryTimestamp"] = to_native_bigint(int(expiry_timestamp))
        bc = self._builder_code()
        if bc:
            body["builderCode"] = bc
        sl_tp = self._sl_tp_field(stop_loss_price, take_profit_price, trigger_price_type)
        if sl_tp:
            body["slTp"] = sl_tp

        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/place-limit-order",
            body,
            intent=f"limit {side_label(side_int)} {size} {sym} @ {price}",
            preview_path="/api/perpetuals/account/previews/place-limit-order",
            preview_body=preview_body,
        )
        return {"symbol": sym, "marketId": str(mid),
                **self.submit_or_report(inspected)}

    def place_scale_order(self, symbol_or_id, side, total_size, start_price,
                          end_price, number_of_orders, order_type=ORDER_TYPE_GTC,
                          size_skew=None, reduce_only=False, leverage=None,
                          client_order_ids=None, expiry_timestamp=None):
        """"Ladder me in" — a whole ladder in ONE transaction.

        This is ``place-scale-order``, not N ``place-limit-order`` calls. N
        separate transactions can partially fail and leave a half-built ladder,
        and each one pays its own gas.
        """
        mid, sym, market = self.resolve_market(symbol_or_id)
        params = market.get("marketParams", {})
        side_int = normalize_side(side)
        if int(number_of_orders) < 1:
            raise ValueError("number_of_orders must be >= 1")

        scaled_total = scale_size(total_size, params["lotSize"])
        scaled_start = scale_price(start_price, params["tickSize"], side=side_int)
        scaled_end = scale_price(end_price, params["tickSize"], side=side_int)

        preview_body = {
            "accountId": self.account_id().wire(),
            "marketId": str(mid),
            "side": side_int,
            "totalSize": scaled_total,
            "startPrice": scaled_start,
            "endPrice": scaled_end,
            "numberOfOrders": int(number_of_orders),
            "orderType": int(order_type),
            "reduceOnly": bool(reduce_only),
        }
        if size_skew is not None:
            preview_body["sizeSkew"] = float(size_skew)
        if leverage is not None:
            preview_body["leverage"] = float(leverage)

        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "side": side_int,
            "totalSize": scaled_total,
            "startPrice": scaled_start,
            "endPrice": scaled_end,
            "numberOfOrders": int(number_of_orders),
            "orderType": int(order_type),
            "reduceOnly": bool(reduce_only),
            "hasPosition": self._has_position(str(mid)),
            "collateralChange": 0.0,
            "cancelSlTp": False,
        }
        if size_skew is not None:
            body["sizeSkew"] = float(size_skew)
        if leverage is not None:
            body["leverage"] = float(leverage)
        if client_order_ids:
            body["clientOrderIds"] = [to_native_bigint(int(c)) for c in client_order_ids]
        if expiry_timestamp is not None:
            body["expiryTimestamp"] = to_native_bigint(int(expiry_timestamp))
        bc = self._builder_code()
        if bc:
            body["builderCode"] = bc

        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/place-scale-order",
            body,
            intent=f"scale ladder {number_of_orders}x {side_label(side_int)} {sym}",
            preview_path="/api/perpetuals/account/previews/place-scale-order",
            preview_body=preview_body,
        )
        return {"symbol": sym, "marketId": str(mid), "orders": int(number_of_orders),
                **self.submit_or_report(inspected)}

    def cancel_orders(self, symbol_or_id, order_ids, should_abort_on_missing_id=False):
        """Cancel specific orders in one market.

        Prefer :meth:`cancel_and_place_orders` whenever the intent is to REPLACE
        an order — a bare cancel leaves the strategy unquoted in between.
        """
        mid, sym, _ = self.resolve_market(symbol_or_id)
        wire_ids = [to_native_bigint(int(str(o).rstrip("n"))) for o in order_ids]
        if not wire_ids:
            raise ValueError("order_ids must contain at least one id")

        preview_body = {
            "accountId": self.account_id().wire(),
            "marketIdsToData": {str(mid): {"orderIds": wire_ids, "collateralChange": 0.0}},
            "shouldAbortOnMissingId": bool(should_abort_on_missing_id),
        }
        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketIdsToData": {str(mid): {"orderIds": wire_ids, "collateralChange": 0.0}},
            "shouldAbortOnMissingId": bool(should_abort_on_missing_id),
        }
        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/cancel-orders",
            body,
            intent=f"cancel {len(wire_ids)} order(s) on {sym}",
            preview_path="/api/perpetuals/account/previews/cancel-orders",
            preview_body=preview_body,
        )
        return {"symbol": sym, "marketId": str(mid), "cancelled": len(wire_ids),
                **self.submit_or_report(inspected)}

    def cancel_and_place_orders(self, symbol_or_id, order_ids_to_cancel, orders_to_place,
                                order_type=ORDER_TYPE_POST_ONLY, reduce_only=False,
                                client_order_ids_to_cancel=None, leverage=None,
                                expiry_timestamp=None,
                                should_abort_on_missing_id=False):
        """ONE tx: cancel + place. EVERY requote/reprice/modify goes through here.

        A split cancel-then-place leaves the strategy unquoted (or, if reversed,
        double-quoted) in between and can partially fail. ``orders_to_place``
        takes human values ``[{side, size, price}]`` and is scaled here.
        """
        mid, sym, market = self.resolve_market(symbol_or_id)
        params = market.get("marketParams", {})
        wire_cancel = [to_native_bigint(int(str(o).rstrip("n"))) for o in (order_ids_to_cancel or [])]

        placed = []
        for i, row in enumerate(orders_to_place or []):
            if not isinstance(row, dict):
                raise ValueError(f"orders_to_place[{i}] must be an object")
            side_int = normalize_side(row.get("side"))
            size = row.get("size")
            price = row.get("price")
            if size is None or price is None:
                raise ValueError(f"orders_to_place[{i}] requires size and price")
            placed.append({
                "side": side_int,
                "size": size if is_native_bigint(size) else scale_size(size, params["lotSize"]),
                "price": (price if is_native_bigint(price)
                          else scale_price(price, params["tickSize"], side=side_int)),
            })

        if not wire_cancel and not placed:
            raise ValueError("cancel_and_place_orders needs something to cancel or place")

        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "orderIdsToCancel": wire_cancel,
            "ordersToPlace": placed,
            "orderType": int(order_type),
            "reduceOnly": bool(reduce_only),
            "hasPosition": self._has_position(str(mid)),
            "shouldAbortOnMissingId": bool(should_abort_on_missing_id),
        }
        if client_order_ids_to_cancel:
            body["clientOrderIdsToCancel"] = [
                to_native_bigint(int(c)) for c in client_order_ids_to_cancel
            ]
        if leverage is not None:
            body["leverage"] = float(leverage)
        if expiry_timestamp is not None:
            body["expiryTimestamp"] = to_native_bigint(int(expiry_timestamp))
        bc = self._builder_code()
        if bc:
            body["builderCode"] = bc

        # No preview counterpart exists for cancel-and-place in the V2 spec, so
        # the closest gate is the cancel preview over the same order ids.
        preview_body = None
        preview_path = None
        if wire_cancel:
            preview_path = "/api/perpetuals/account/previews/cancel-orders"
            preview_body = {
                "accountId": self.account_id().wire(),
                "marketIdsToData": {str(mid): {"orderIds": wire_cancel, "collateralChange": 0.0}},
                "shouldAbortOnMissingId": bool(should_abort_on_missing_id),
            }

        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/cancel-and-place-orders",
            body,
            intent=f"requote {sym}: cancel {len(wire_cancel)}, place {len(placed)} (ONE tx)",
            preview_path=preview_path,
            preview_body=preview_body,
        )
        return {"symbol": sym, "marketId": str(mid), "cancelled": len(wire_cancel),
                "placed": len(placed), "atomic": True,
                **self.submit_or_report(inspected)}

    def move_order(self, symbol_or_id, order_id, new_price, size, side,
                   order_type=ORDER_TYPE_POST_ONLY, reduce_only=False):
        """Move my bid to X — expressed as ONE cancel-and-place transaction."""
        return self.cancel_and_place_orders(
            symbol_or_id,
            order_ids_to_cancel=[order_id],
            orders_to_place=[{"side": side, "size": size, "price": new_price}],
            order_type=order_type,
            reduce_only=reduce_only,
        )

    def place_sl_tp_orders(self, symbol_or_id, position_side, stop_loss_price=None,
                           take_profit_price=None, size=None,
                           trigger_price_type=TRIGGER_PRICE_INDEX, limit_order_id=None):
        """Attach SL/TP to an existing position — one transaction.

        v3.0.0 names: ``stopLossPrice`` / ``takeProfitPrice``.
        """
        if stop_loss_price is None and take_profit_price is None:
            raise ValueError("provide stop_loss_price and/or take_profit_price")
        mid, sym, market = self.resolve_market(symbol_or_id)
        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "positionSide": normalize_side(position_side),
            "triggerPriceType": int(trigger_price_type),
        }
        if stop_loss_price is not None:
            body["stopLossPrice"] = float(stop_loss_price)
        if take_profit_price is not None:
            body["takeProfitPrice"] = float(take_profit_price)
        if size is not None:
            body["size"] = scale_size(size, market["marketParams"]["lotSize"])
        if limit_order_id is not None:
            body["limitOrderId"] = to_native_bigint(int(str(limit_order_id).rstrip("n")))
        if self.gas_config().mode == "sponsored":
            body["isSponsoredTx"] = True
        bc = self._builder_code()
        if bc:
            body["builderCode"] = bc

        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/place-sl-tp-orders",
            body,
            intent=f"SL/TP on {sym}",
        )
        return {"symbol": sym, "marketId": str(mid), **self.submit_or_report(inspected)}

    def _sl_tp_field(self, stop_loss_price, take_profit_price, trigger_price_type):
        """The inline ``slTp`` object carried by place-limit/market-order."""
        if stop_loss_price is None and take_profit_price is None:
            return None
        sl_tp = {"triggerPriceType": int(trigger_price_type)}
        if stop_loss_price is not None:
            sl_tp["stopLossPrice"] = float(stop_loss_price)
        if take_profit_price is not None:
            sl_tp["takeProfitPrice"] = float(take_profit_price)
        if self.gas_config().mode == "sponsored":
            sl_tp["isSponsoredTx"] = True
        return sl_tp

    def cancel_all_orders(self, verify=True):
        """Cancel every resting order across every market, then VERIFY.

        This is the kill switch's cancel path. It re-reads pending orders and
        raises if any survive — a kill switch that assumes success hides the
        exposure it exists to remove.
        """
        by_market = {}
        for order in self.get_open_orders(force=True):
            oid = order.get("orderId")
            if oid is None:
                continue
            by_market.setdefault(order["marketId"], []).append(
                to_native_bigint(from_native_bigint(oid))
            )
        if not by_market:
            return {"cancelled": 0, "verified": True, "markets": 0}

        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketIdsToData": {
                mkt: {"orderIds": ids, "collateralChange": 0.0}
                for mkt, ids in by_market.items()
            },
            "shouldAbortOnMissingId": False,
        }
        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/cancel-orders",
            body,
            intent="cancel ALL open orders",
        )
        result = self.submit_or_report(inspected)

        if verify and result.get("submitted"):
            remaining = self.get_open_orders(force=True)
            if remaining:
                raise RuntimeError(
                    f"cancel-all NOT verified: {len(remaining)} order(s) still "
                    f"resting after cancellation"
                )
            result["verified"] = True
        else:
            result["verified"] = False
        result["markets"] = len(by_market)
        result["cancelled"] = sum(len(v) for v in by_market.values())
        return result

    def make_kill_switch(self, max_silence_s=60.0, log=None):
        """A kill switch wired to this adapter's VERIFIED cancel-all."""
        return KillSwitch(max_silence_s, lambda: self.cancel_all_orders(verify=True), log=log)

    def heartbeat(self, kill_switch):
        kill_switch.heartbeat()

    # -- leverage & collateral --------------------------------------------

    def set_leverage(self, symbol_or_id, leverage):
        mid, sym, _ = self.resolve_market(symbol_or_id)
        preview_body = {
            "accountId": self.account_id().wire(),
            "marketId": str(mid),
            "leverage": float(leverage),
        }
        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "leverage": float(leverage),
            "collateralChange": 0.0,
        }
        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/set-leverage",
            body,
            intent=f"set leverage {leverage}x on {sym}",
            preview_path="/api/perpetuals/account/previews/set-leverage",
            preview_body=preview_body,
        )
        return {"symbol": sym, "marketId": str(mid), **self.submit_or_report(inspected)}

    def deposit_collateral(self, amount, tx_kind=None):
        """Wallet -> account UNALLOCATED collateral.

        Serialized under the coin lock: parallel deposits race on the same Sui
        coin objects and fail with version/equivocation errors.

        Note this is only step one of the isolated-margin lifecycle —
        unallocated collateral protects no position. Call
        :meth:`allocate_collateral` next.
        """
        with _COIN_LOCK:
            body = {
                **self._account_fields(),
                "walletAddress": str(self.wallet()),
                "collateralCoinType": self.config.collateral_coin_type,
                "depositAmount": to_native_bigint(scale_collateral(amount)),
            }
            if self.gas_config().mode == "sponsored":
                body["isSponsoredTx"] = True
            inspected = self.build_gated(
                "/api/perpetuals/account/transactions/deposit-collateral",
                body,
                intent=f"deposit {amount} {short_coin(self.config.collateral_coin_type)}",
                tx_kind=tx_kind,
            )
            return self.submit_or_report(inspected)

    def withdraw_collateral(self, amount, recipient=None, tx_kind=None):
        with _COIN_LOCK:
            body = {
                "accountId": self.account_id().wire(),
                "withdrawAmount": to_native_bigint(scale_collateral(amount)),
                "recipientAddress": str(sui_address(recipient) if recipient else self.wallet()),
            }
            inspected = self.build_gated(
                "/api/perpetuals/account/transactions/withdraw-collateral",
                body,
                intent=f"withdraw {amount} {short_coin(self.config.collateral_coin_type)}",
                tx_kind=tx_kind,
            )
            return self.submit_or_report(inspected)

    def allocate_collateral(self, symbol_or_id, amount, tx_kind=None):
        """Account collateral -> per-position ISOLATED margin. Required to trade."""
        mid, sym, _ = self.resolve_market(symbol_or_id)
        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "allocateAmount": to_native_bigint(scale_collateral(amount)),
        }
        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/allocate-collateral",
            body,
            intent=f"allocate {amount} to {sym}",
            tx_kind=tx_kind,
        )
        return {"symbol": sym, "marketId": str(mid), **self.submit_or_report(inspected)}

    def deallocate_collateral(self, symbol_or_id, amount, tx_kind=None):
        mid, sym, _ = self.resolve_market(symbol_or_id)
        body = {
            **self._account_fields(),
            "walletAddress": str(self.wallet()),
            "marketId": str(mid),
            "deallocateAmount": to_native_bigint(scale_collateral(amount)),
        }
        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/deallocate-collateral",
            body,
            intent=f"deallocate {amount} from {sym}",
            tx_kind=tx_kind,
        )
        return {"symbol": sym, "marketId": str(mid), **self.submit_or_report(inspected)}

    def transfer_collateral(self, to_account_id, amount, tx_kind=None):
        body = {
            "fromAccountId": self.account_id().wire(),
            "toAccountId": native_account_id(to_account_id).wire(),
            "transferAmount": to_native_bigint(scale_collateral(amount)),
            "walletAddress": str(self.wallet()),
        }
        if self.config.account_cap_id is not None:
            body["fromAccountCapId"] = str(self.config.account_cap_id)
        inspected = self.build_gated(
            "/api/perpetuals/account/transactions/transfer-collateral",
            body,
            intent=f"transfer {amount} to account {to_account_id}",
            tx_kind=tx_kind,
        )
        return self.submit_or_report(inspected)

    def ensure_collateral_allocated(self, symbol_or_id, required_amount):
        """Isolated-margin allocation handled INSIDE the adapter.

        Strategies never have to know that Aftermath needs an explicit allocate
        step, which is exactly why this lives here and not in a strategy.
        """
        mid, sym, _ = self.resolve_market(symbol_or_id)
        allocated = 0.0
        for pos in self.get_positions(force=True):
            if pos.get("marketId") == str(mid):
                allocated = float(pos.get("collateral") or 0)
                break
        shortfall = float(required_amount) - allocated
        if shortfall <= 0:
            return {"symbol": sym, "allocated": allocated, "action": "none"}
        return {"symbol": sym, "allocated": allocated, "shortfall": shortfall,
                "action": "allocate",
                **self.allocate_collateral(symbol_or_id, shortfall)}

    # -- onboarding: ONE composed PTB -------------------------------------

    def build_onboarding(self, defer_share=True, deposit_amount=None,
                         allocate_symbol=None, allocate_amount=None):
        """Compose onboarding into a single PTB via ``txKind`` chaining.

        ``/api/perpetuals/transactions/create-account`` accepts ``txKind`` and
        ``deferShare``; every downstream builder accepts ``txKind`` to EXTEND an
        existing kind. Chaining them makes onboarding succeed or fail as a unit
        instead of stranding a user half-set-up.

        With ``deferShare = true`` the response carries **deferred PTB argument
        references** (``{accountArg, adminCapArg, sharePolicyArg,
        collateralCoinType}``) alongside ``txKind`` — both shapes are handled;
        the simple ``{txKind}`` form is never assumed.

        KNOWN LIMIT: ``deposit-collateral`` requires a *concrete numeric*
        ``accountId``, and a brand-new account only exists as a deferred PTB
        argument until the transaction executes. So deposit/allocate can only be
        chained on for an account that already exists. When they cannot be
        chained, they are reported as follow-up steps rather than silently
        dropped.
        """
        wallet = self.wallet()
        body = {
            "walletAddress": str(wallet),
            "collateralCoinType": self.config.collateral_coin_type,
            "deferShare": bool(defer_share),
        }
        sponsor = self._sponsor_field()
        if sponsor is not None:
            body["sponsor"] = sponsor

        built = self.request("/api/perpetuals/transactions/create-account", body)
        inspected = inspect_tx(built, TxExpectation(
            sender=wallet, gas=self.gas_config(), intent="create perpetuals account",
        ))

        steps = ["create-account"]
        follow_up = []
        tx_kind = inspected.tx_kind

        # Sponsored builds return full Transaction bytes, not a TransactionKind,
        # so there is nothing left to extend.
        composable = tx_kind is not None and not inspected.is_sponsored

        if deposit_amount is not None:
            if composable and self.config.account_id is not None:
                result = self.deposit_collateral(deposit_amount, tx_kind=tx_kind)
                tx_kind = (result.get("inspection") or {}).get("txKind") or tx_kind
                steps.append("deposit-collateral")
            else:
                follow_up.append(
                    "deposit-collateral requires a concrete numeric accountId; "
                    "run it after the account transaction lands"
                )
        if allocate_symbol and allocate_amount is not None:
            if composable and self.config.account_id is not None:
                self.allocate_collateral(allocate_symbol, allocate_amount, tx_kind=tx_kind)
                steps.append("allocate-collateral")
            else:
                follow_up.append(
                    "allocate-collateral requires a concrete numeric accountId; "
                    "run it after the account transaction lands"
                )

        return {
            "atomic": len(steps) > 1,
            "steps": steps,
            "followUp": follow_up,
            "deferShare": bool(defer_share),
            "deferredArgs": inspected.deferred,
            "responseShape": ("txKind+deferred" if inspected.deferred else "txKind"),
            "inspected": inspected,
        }

    def onboard(self, defer_share=True, deposit_amount=None,
                allocate_symbol=None, allocate_amount=None):
        """Build onboarding and submit it if armed; otherwise report it."""
        plan = self.build_onboarding(defer_share=defer_share,
                                     deposit_amount=deposit_amount,
                                     allocate_symbol=allocate_symbol,
                                     allocate_amount=allocate_amount)
        inspected = plan.pop("inspected")
        return {**plan, **self.submit_or_report(inspected)}

    # -- gas --------------------------------------------------------------

    def gas_pool(self, wallet=None):
        """POST /api/gas-pool/pool — requires ``{walletAddress}``.

        Returns ``{balance, gasPoolId?, walletAddress, whitelistedAddresses}``.
        """
        addr = sui_address(wallet) if wallet else self.wallet()
        return self.request("/api/gas-pool/pool", {"walletAddress": str(addr)})

    def dynamic_gas(self, serialized_tx, gas_coin_type=None, wallet=None):
        """POST /api/dynamic-gas — a TRANSFORM, not a health check.

        Requires ``{serializedTx, walletAddress, gasCoinType}`` and rewrites a
        *built* transaction to pay gas in ``gasCoinType``. There is no
        meaningful call without a real transaction, so ``doctor`` validates the
        preconditions instead of pinging it.
        """
        coin = gas_coin_type or self.config.gas_coin_type
        if not coin:
            raise ConfigError(
                "AFTERMATH_GAS_COIN_TYPE is required for dynamic gas — it names "
                "the coin that pays gas"
            )
        addr = sui_address(wallet) if wallet else self.wallet()
        return self.request("/api/dynamic-gas", {
            "serializedTx": serialized_tx,
            "walletAddress": str(addr),
            "gasCoinType": coin,
        })

    def check_gas_mode(self):
        """Validate that the ACTIVE gas mode's prerequisites actually hold.

        Each mode needs something different, and finding out at trade time is
        the failure this exists to prevent. Never silently switches modes.
        """
        mode = self.config.gas_mode
        if not self.config.wallet_address:
            return {"mode": mode, "ok": False,
                    "detail": "wallet address not configured",
                    "remedy": "Set AFTERMATH_WALLET_ADDRESS."}

        if mode == "sponsored":
            try:
                pool = self.gas_pool()
                balance = from_native_bigint(pool.get("balance", 0))
                return {"mode": mode, "ok": True,
                        "detail": f"gas pool reachable, balance {format_sui(balance)} "
                                  f"— no SUI required in your wallet",
                        "gasPoolId": pool.get("gasPoolId")}
            except Exception as exc:
                return {"mode": mode, "ok": False,
                        "detail": f"gas pool unavailable: {exc}",
                        "remedy": "Set AFTERMATH_GAS_MODE=self and fund the wallet "
                                  "with SUI, or retry when the pool is up."}

        if mode == "self":
            balance = self.sui_balance()
            if balance is None:
                return {"mode": mode, "ok": False,
                        "detail": "SUI balance could not be read",
                        "remedy": "Check the wallet address, or use "
                                  "AFTERMATH_GAS_MODE=sponsored to trade without SUI."}
            if balance < self.config.gas_budget_mist:
                return {"mode": mode, "ok": False,
                        "detail": f"wallet holds {format_sui(balance)}, below the gas "
                                  f"budget {format_sui(self.config.gas_budget_mist)}",
                        "remedy": "Add SUI, lower AFTERMATH_GAS_BUDGET_MIST, or set "
                                  "AFTERMATH_GAS_MODE=sponsored."}
            return {"mode": mode, "ok": True, "detail": f"wallet holds {format_sui(balance)}"}

        # dynamic
        if not self.config.gas_coin_type:
            return {"mode": mode, "ok": False,
                    "detail": "AFTERMATH_GAS_COIN_TYPE is not set — dynamic gas needs "
                              "the coin that pays gas",
                    "remedy": "Set AFTERMATH_GAS_COIN_TYPE (e.g. the USDC coin type), "
                              "or use AFTERMATH_GAS_MODE=sponsored."}
        return {"mode": mode, "ok": True,
                "detail": f"preconditions satisfied; gas paid in "
                          f"{short_coin(self.config.gas_coin_type)} "
                          f"(verified on the first transaction)"}

    # -- wallet (degrades gracefully: the whole family 404s live) ----------

    def wallet_balances(self, wallet=None):
        """POST /api/wallet/all_coin_balances.

        The entire ``/api/wallet/*`` family is present in the spec but 404s on
        the live host today. Returns ``None`` rather than failing the caller —
        balances are informational everywhere they are used here.
        """
        addr = sui_address(wallet) if wallet else self.wallet()
        try:
            return self.request("/api/wallet/all_coin_balances",
                                {"walletAddress": str(addr)}, retries=1)
        except AftermathApiError:
            return None

    def coin_balance(self, coin_type, wallet=None):
        payload = self.wallet_balances(wallet)
        if payload is None:
            return None
        return _extract_balance(payload, coin_type)

    def sui_balance(self, wallet=None):
        return self.coin_balance(SUI_COIN_TYPE, wallet)

    def collateral_balance(self, wallet=None):
        return self.coin_balance(self.config.collateral_coin_type, wallet)

    # -- rewards ----------------------------------------------------------

    def rewards_points(self, signed_bytes, signature, wallet=None):
        """POST /api/rewards/points -> ``{totalPoints}`` (FLOAT).

        v3.0.0 renamed ``{points}`` (int) to ``{totalPoints}`` (float).
        """
        addr = sui_address(wallet) if wallet else self.wallet()
        data = self.request("/api/rewards/points", {
            "walletAddress": str(addr),
            "bytes": signed_bytes,
            "signature": signature,
        })
        return {"totalPoints": float((data or {}).get("totalPoints", 0.0))}

    def rewards_history(self, signed_bytes, signature, wallet=None, domain=None,
                        limit=20, cursor=None):
        """POST /api/rewards/history — now requires SIGNED auth.

        v3.0.0: this route was unauthenticated before; ``bytes`` + ``signature``
        are mandatory now. This adapter never produces a signature itself.
        """
        addr = sui_address(wallet) if wallet else self.wallet()
        body = {
            "walletAddress": str(addr),
            "bytes": signed_bytes,
            "signature": signature,
            "limit": int(limit),
        }
        if domain:
            body["domain"] = domain
        if cursor is not None:
            body["cursor"] = int(cursor)
        return self.request("/api/rewards/history", body)

    # -- helpers ----------------------------------------------------------

    def _has_position(self, market_id_str):
        try:
            for pos in self.get_positions():
                if pos.get("marketId") == market_id_str:
                    return True
        except (AftermathApiError, ConfigError):
            return False
        return False


def _extract_balance(payload, coin_type):
    """Pull one coin balance out of a wallet response.

    The shape varies (map keyed by coin type, or a list of records), so both are
    handled. Returns None when absent rather than 0 — "not present" and "zero"
    mean different things to the caller.
    """
    if not isinstance(payload, (dict, list)):
        return None
    short = short_coin(coin_type)
    if isinstance(payload, dict):
        if coin_type in payload:
            return _coerce_balance(payload[coin_type])
        for k, v in payload.items():
            if k == coin_type or str(k).endswith(f"::{short}"):
                return _coerce_balance(v)
        for key in ("balances", "coinBalances", "data"):
            if key in payload:
                return _extract_balance(payload[key], coin_type)
        return None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        t = entry.get("coinType") or entry.get("coin_type") or entry.get("type")
        if t == coin_type:
            return _coerce_balance(
                entry.get("balance", entry.get("totalBalance", entry.get("amount")))
            )
    return None


def _coerce_balance(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return from_native_bigint(v)
        except TypeError:
            return None
    if isinstance(v, dict):
        for k in ("balance", "totalBalance", "amount", "value"):
            if k in v:
                return _coerce_balance(v[k])
    return None


def get_adapter(config=None):
    """Convenience constructor used by every command script."""
    return AftermathAdapter(config=config)
