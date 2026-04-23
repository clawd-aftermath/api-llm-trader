"""Sui transaction signing for API LLM Trader.

Handles: Bech32 key decode, Ed25519 signing, BCS TransactionData wrapping,
full build→sign→submit flow against the Sui JSON-RPC.
"""

import base64
import hashlib
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _api import (
    get_config_value,
    sui_execute_transaction,
    sui_get_coins,
    sui_get_reference_gas_price,
    get_sui_rpc_url,
)
from _cli import error

DEFAULT_GAS_BUDGET = 50_000_000  # 0.05 SUI

# ---------------------------------------------------------------------------
# Base58
# ---------------------------------------------------------------------------

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_MAP = {ch: i for i, ch in enumerate(_B58_ALPHABET)}


def base58_decode(s):
    """Decode a Base58-encoded string to bytes."""
    n = 0
    for ch in s.encode("ascii"):
        if ch not in _B58_MAP:
            raise ValueError(f"invalid base58 character: {chr(ch)}")
        n = n * 58 + _B58_MAP[ch]
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


# ---------------------------------------------------------------------------
# Bech32 (standard, NOT Bech32m)
# ---------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify(hrp, data):
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def bech32_decode(bech):
    """Decode a Bech32 string. Returns (hrp, data_bytes)."""
    bech = bech.strip()
    if any(ord(c) < 33 or ord(c) > 126 for c in bech):
        raise ValueError("invalid bech32 character")
    if bech != bech.lower() and bech != bech.upper():
        raise ValueError("mixed-case bech32 strings are invalid")
    bech_lower = bech.lower()
    pos = bech_lower.rfind("1")
    if pos < 1 or pos + 7 > len(bech_lower):
        raise ValueError("invalid bech32 separator position")
    hrp = bech_lower[:pos]
    data_part = bech_lower[pos + 1 :]
    values = []
    for c in data_part:
        idx = _BECH32_CHARSET.find(c)
        if idx == -1:
            raise ValueError(f"invalid bech32 character: {c}")
        values.append(idx)
    if not _bech32_verify(hrp, values):
        raise ValueError("bech32 checksum failed")
    # Strip 6-char checksum, convert 5-bit groups to 8-bit
    five_bit = values[:-6]
    acc = 0
    bits = 0
    result = []
    for v in five_bit:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            result.append((acc >> bits) & 0xFF)
    if bits >= 5 or ((acc << (8 - bits)) & 0xFF):
        raise ValueError("invalid bech32 padding")
    return hrp, bytes(result)


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def load_keypair():
    """Load Ed25519 keypair from AFTERMATH_PRIVATE_KEY config.

    Expects suiprivkey1... Bech32 format. First data byte is scheme
    (0x00 = Ed25519), remaining 32 bytes are the seed.

    Returns (nacl.signing.SigningKey, nacl.signing.VerifyKey).
    """
    try:
        import nacl.signing
    except ImportError:
        error("PyNaCl not installed; run: pip install PyNaCl")

    raw = get_config_value("AFTERMATH_PRIVATE_KEY")
    if not raw:
        error(
            "missing AFTERMATH_PRIVATE_KEY; set it as an env var "
            "or in your API LLM Trader credentials file"
        )
    key_str = raw.expose() if hasattr(raw, "expose") else str(raw)

    try:
        hrp, data = bech32_decode(key_str)
    except ValueError as e:
        error(f"invalid AFTERMATH_PRIVATE_KEY: {e}")

    if hrp != "suiprivkey":
        error(f"unexpected key prefix '{hrp}'; expected 'suiprivkey'")
    if len(data) != 33:
        error("private key data must be exactly 33 bytes (scheme + 32-byte seed)")

    scheme = data[0]
    if scheme != 0x00:
        error(f"unsupported key scheme {scheme:#x}; only Ed25519 (0x00) is supported")

    seed = data[1:33]
    signing_key = nacl.signing.SigningKey(seed)
    return signing_key, signing_key.verify_key


def derive_address(verify_key):
    """Derive Sui address from Ed25519 public key.

    address = blake2b_256([0x00] + pubkey_bytes) → 0x-prefixed hex
    """
    pubkey_bytes = bytes(verify_key)
    digest = hashlib.blake2b(b"\x00" + pubkey_bytes, digest_size=32).digest()
    return "0x" + digest.hex()


def get_wallet_address():
    """Get wallet address from config or derive from private key."""
    addr = get_config_value("AFTERMATH_WALLET_ADDRESS")
    if addr:
        return str(addr)
    # Derive from private key
    _, verify_key = load_keypair()
    return derive_address(verify_key)


# ---------------------------------------------------------------------------
# BCS helpers
# ---------------------------------------------------------------------------


def _write_uleb128(buf, value):
    """Write ULEB128-encoded unsigned integer into bytearray."""
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)


def _addr_bytes(hex_addr):
    """Convert 0x-prefixed hex address to 32 bytes, zero-padded."""
    raw = hex_addr.replace("0x", "").replace("0X", "")
    return bytes.fromhex(raw.zfill(64))


def wrap_tx_kind(tx_kind_b64, sender_hex, gas_coins, gas_owner_hex, gas_price, gas_budget):
    """Wrap a base64 TransactionKind into full BCS TransactionData V1.

    gas_coins: list of dicts with keys objectId (0x hex), version (int),
               digest (base58 string).
    Returns raw bytes of BCS-encoded TransactionData.
    """
    tx_kind_bytes = base64.b64decode(tx_kind_b64)
    sender = _addr_bytes(sender_hex)
    gas_owner = _addr_bytes(gas_owner_hex)

    buf = bytearray()
    # TransactionData::V1 enum tag
    buf.append(0)
    # TransactionKind (already BCS-encoded)
    buf.extend(tx_kind_bytes)
    # sender: SuiAddress (32 bytes)
    buf.extend(sender)
    # GasData.payment: Vec<ObjectRef>
    _write_uleb128(buf, len(gas_coins))
    for coin in gas_coins:
        # ObjectID: 32 bytes
        buf.extend(_addr_bytes(coin["objectId"]))
        # SequenceNumber: u64 little-endian
        buf.extend(struct.pack("<Q", int(coin["version"])))
        # ObjectDigest: 32 bytes (base58-decoded)
        digest_bytes = base58_decode(coin["digest"])
        if len(digest_bytes) != 32:
            raise ValueError(
                f"invalid gas coin digest length for {coin.get('objectId')}: "
                f"expected 32 bytes, got {len(digest_bytes)}"
            )
        buf.extend(digest_bytes)
    # GasData.owner: SuiAddress (32 bytes)
    buf.extend(gas_owner)
    # GasData.price: u64 little-endian
    buf.extend(struct.pack("<Q", int(gas_price)))
    # GasData.budget: u64 little-endian
    buf.extend(struct.pack("<Q", int(gas_budget)))
    # TransactionExpiration::None (enum tag 0)
    buf.append(0)

    return bytes(buf)


def sign_transaction(tx_data_bytes, signing_key):
    """Sign BCS TransactionData with Ed25519.

    Returns base64-encoded Sui UserSignature:
    [0x00 scheme byte] + [64-byte Ed25519 signature] + [32-byte public key]
    """
    import nacl.signing

    # Intent message: scope=0 (TransactionData), version=0, app_id=0
    intent_message = bytes([0, 0, 0]) + tx_data_bytes
    digest = hashlib.blake2b(intent_message, digest_size=32).digest()

    # Sign the digest
    signed = signing_key.sign(digest)
    signature = signed.signature  # 64 bytes
    pubkey = signing_key.verify_key.encode()  # 32 bytes

    # Sui signature: [scheme_byte] + [signature] + [public_key]
    sui_sig = bytes([0x00]) + signature + pubkey  # 97 bytes
    return base64.b64encode(sui_sig).decode("ascii")


# ---------------------------------------------------------------------------
# Full build → sign → submit
# ---------------------------------------------------------------------------


def build_sign_submit(tx_kind_b64, wallet_address=None, gas_budget=None, sponsor_signature=None):
    """Complete flow: wrap txKind → fetch gas → sign → submit to Sui RPC.

    Returns the Sui execution result dict.
    """
    signing_key, verify_key = load_keypair()
    sender = wallet_address or get_wallet_address()
    rpc_url = get_sui_rpc_url()

    # Fetch gas coins (SUI for gas)
    coins = sui_get_coins(sender, "0x2::sui::SUI", limit=3, rpc_url=rpc_url)
    if not coins:
        error(f"no SUI gas coins found for address {sender}")

    gas_coins = [
        {
            "objectId": c["coinObjectId"],
            "version": int(c["version"]),
            "digest": c["digest"],
        }
        for c in coins
    ]

    gas_price = sui_get_reference_gas_price(rpc_url=rpc_url)
    budget = gas_budget or DEFAULT_GAS_BUDGET

    # Wrap txKind into full TransactionData
    tx_data = wrap_tx_kind(
        tx_kind_b64=tx_kind_b64,
        sender_hex=sender,
        gas_coins=gas_coins,
        gas_owner_hex=sender,
        gas_price=gas_price,
        gas_budget=budget,
    )

    # Sign
    signature = sign_transaction(tx_data, signing_key)

    # Submit
    tx_bytes_b64 = base64.b64encode(tx_data).decode("ascii")
    signatures = [signature]
    if sponsor_signature:
        signatures.append(sponsor_signature)
    result = sui_execute_transaction(tx_bytes_b64, signatures, rpc_url=rpc_url)
    return result
