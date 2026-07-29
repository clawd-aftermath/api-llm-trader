"""HTTP clients for Aftermath Finance REST API and Sui JSON-RPC.

No SDK vendoring — pure requests-based HTTP calls against:
- Aftermath V2 perpetuals API: https://v2-preview.aftermath.finance/api/perpetuals/*
- Sui fullnode JSON-RPC: https://fullnode.mainnet.sui.io:443
"""

import json
import os
import sys

from _paths import credentials_path

DEFAULT_HOST = "https://v2-preview.aftermath.finance"
DEFAULT_SUI_RPC = "https://fullnode.mainnet.sui.io:443"
TESTNET_HOST = "https://testnet.aftermath.finance"
TESTNET_SUI_RPC = "https://fullnode.testnet.sui.io:443"

# USDC collateral coin type on Sui mainnet
USDC_COIN_TYPE = (
    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"
    "::usdc::USDC"
)

# Keys whose values must never appear in logs, tracebacks, or agent output.
_SECRET_NAMES = frozenset({"AFTERMATH_PRIVATE_KEY"})
_CREDENTIALS = None


class SecretValue:
    """Opaque wrapper that keeps secret strings out of Debug/Display output."""

    __slots__ = ("_val",)

    def __init__(self, val: str):
        self._val = val

    def expose(self) -> str:
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
                    f"(mode {stat.filemode(mode)}). "
                    f"Run: chmod 600 {path}",
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
    """Like get_config_value but also report where the value came from.

    Returns (value, source) where source is "env", "credentials_file", or None.
    """
    value = os.environ.get(name)
    if value:
        return (
            SecretValue(value) if name in _SECRET_NAMES else value,
            "env",
        )
    value = _load_credentials().get(name)
    if value:
        return value, "credentials_file"
    return None, None


def get_aftermath_host():
    """Resolve the Aftermath API host."""
    return str(get_config_value("AFTERMATH_HOST", DEFAULT_HOST)).strip().rstrip("/")


def get_sui_rpc_url():
    """Resolve the Sui fullnode RPC URL based on the configured host."""
    host = get_aftermath_host()
    if "testnet" in host.lower():
        return get_config_value("SUI_RPC_URL", TESTNET_SUI_RPC)
    return get_config_value("SUI_RPC_URL", DEFAULT_SUI_RPC)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

try:
    import requests as _requests
except ImportError:
    _requests = None


def _ensure_requests():
    if _requests is None:
        print(json.dumps({
            "error": "requests library not installed; run: pip install requests"
        }))
        sys.exit(1)


def _extract_error_message(payload):
    """Extract a best-effort error message from an API payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("message", "msg", "reason", "details"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        return json.dumps(payload, default=str)
    return str(payload)


def _parse_http_json_response(resp, endpoint):
    """Parse HTTP JSON and raise a useful RuntimeError on malformed responses."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Aftermath API returned non-JSON response for {endpoint}"
        ) from exc

    # Aftermath preview-style endpoints can return HTTP 200 with an error payload.
    if isinstance(data, dict):
        if data.get("success") is False or data.get("ok") is False:
            err = data.get("error") or data.get("message") or data
            raise RuntimeError(f"Aftermath API error for {endpoint}: {_extract_error_message(err)}")
        if "error" in data and data.get("error") not in (None, False, ""):
            raise RuntimeError(
                f"Aftermath API error for {endpoint}: "
                f"{_extract_error_message(data.get('error'))}"
            )

    return data


def _request_json(method, url, endpoint, body=None):
    """Run an HTTP request and return validated JSON payload."""
    try:
        if method == "POST":
            resp = _requests.post(url, json=body or {}, timeout=30)
        else:
            resp = _requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                detail = _extract_error_message(resp.json())
            except ValueError:
                detail = resp.text.strip() or str(exc)
            raise RuntimeError(
                f"Aftermath API request failed for {endpoint}: HTTP {resp.status_code}: {detail}"
            ) from exc
        raise RuntimeError(f"Aftermath API request failed for {endpoint}: {exc}") from exc

    return _parse_http_json_response(resp, endpoint)


def af_post(path, body=None, host=None):
    """POST to an Aftermath API endpoint. Returns parsed JSON or raises."""
    _ensure_requests()
    base_url = str(host or get_aftermath_host()).strip().rstrip("/")
    url = base_url + path
    return _request_json("POST", url, path, body=body)


def af_get(path, host=None):
    """GET from an Aftermath API endpoint. Returns parsed JSON."""
    _ensure_requests()
    base_url = str(host or get_aftermath_host()).strip().rstrip("/")
    url = base_url + path
    return _request_json("GET", url, path)


def sui_rpc(method, params=None, rpc_url=None):
    """Call a Sui JSON-RPC method. Returns the 'result' field."""
    _ensure_requests()
    url = rpc_url or get_sui_rpc_url()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or [],
    }
    resp = _requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(
            f"Sui RPC error: {data['error'].get('message', data['error'])}"
        )
    return data.get("result")


def sui_get_coins(address, coin_type="0x2::sui::SUI", limit=5, rpc_url=None):
    """Fetch SUI coins owned by an address for gas payment."""
    result = sui_rpc(
        "suix_getCoins",
        [address, coin_type, None, limit],
        rpc_url=rpc_url,
    )
    return result.get("data", [])


def sui_get_reference_gas_price(rpc_url=None):
    """Get the current reference gas price from the Sui network."""
    result = sui_rpc("suix_getReferenceGasPrice", rpc_url=rpc_url)
    return int(result)


def sui_execute_transaction(tx_bytes_b64, signatures, rpc_url=None):
    """Submit a signed transaction to the Sui fullnode.

    tx_bytes_b64: base64 BCS-encoded TransactionData
    signatures: list of base64 Sui UserSignature bytes
    """
    return sui_rpc(
        "sui_executeTransactionBlock",
        [tx_bytes_b64, signatures, {"showEffects": True, "showEvents": True}, "WaitForLocalExecution"],
        rpc_url=rpc_url,
    )


def sui_dry_run(tx_bytes_b64, rpc_url=None):
    """Dry-run a transaction to estimate gas budget."""
    return sui_rpc(
        "sui_dryRunTransactionBlock",
        [tx_bytes_b64],
        rpc_url=rpc_url,
    )
