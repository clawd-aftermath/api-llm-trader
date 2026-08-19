"""Behaviour tests for the Aftermath V2 adapter.

Focused on the things that are specific to this venue and easy to get wrong —
the failures that a generic "does it return 200" test would sail past.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import af_adapter as A  # noqa: E402
from af_mock import AftermathMockAdapter, MOCK_MARKET_ID  # noqa: E402


# ── native BigInt wire format ────────────────────────────────────
# Native BigInt fields require the exact "123n" string on request AND response.


def test_native_bigint_round_trip():
    assert A.to_native_bigint(123) == "123n"
    assert A.from_native_bigint("123n") == 123


def test_encoding_always_emits_the_n_suffix():
    # Strictness that matters is on ENCODE: the API rejects plain numbers on
    # native BigInt request fields. Decoding is deliberately lenient (it
    # accepts "123" as well as "123n"), which is the safe direction.
    assert A.to_native_bigint(0) == "0n"
    assert A.to_native_bigint(10**20) == f"{10**20}n"
    assert A.from_native_bigint("123") == 123  # lenient by design


def test_native_bigint_survives_beyond_float_precision():
    big = 12345678901234567890
    assert A.from_native_bigint(A.to_native_bigint(big)) == big


def test_is_native_bigint_only_matches_suffixed_form():
    assert A.is_native_bigint("42n") is True
    assert A.is_native_bigint("42") is False


# ── identifier discipline ────────────────────────────────────────
# Native numeric id vs CCXT capability object id vs account number.


def test_native_account_id_accepts_wire_and_plain_forms():
    assert str(A.native_account_id("7n")) == "7"
    assert str(A.native_account_id(7)) == "7"
    # And it round-trips back to the wire format the API demands.
    assert A.native_account_id(7).wire() == "7n"


def test_native_account_id_rejects_an_object_id():
    with pytest.raises(Exception):
        A.native_account_id("0xabc")


def test_account_cap_id_rejects_a_bare_number():
    with pytest.raises(Exception):
        A.account_cap_id("7")


def test_market_ids_are_not_tickers():
    assert A.looks_like_object_id("0x" + "ab" * 32) is True
    assert A.looks_like_object_id("BTC") is False


# ── market resolution ────────────────────────────────────────────


def test_resolves_ticker_to_object_id_from_the_api():
    adapter = AftermathMockAdapter()
    market_id, symbol, market = adapter.resolve_market("BTC")
    assert str(market_id) == MOCK_MARKET_ID
    assert symbol == "BTCUSD"
    # Never constructed — always what the API returned.
    assert market["objectId"] == MOCK_MARKET_ID


def test_unknown_ticker_raises_rather_than_guessing():
    adapter = AftermathMockAdapter()
    with pytest.raises(Exception):
        adapter.resolve_market("DOGECOIN-MOON")


def test_no_live_markets_gives_a_pre_launch_explanation():
    # Zero markets is the expected state before relaunch, and the error should
    # say so rather than implying misconfiguration.
    adapter = AftermathMockAdapter()
    adapter.transport.markets = []
    with pytest.raises(Exception) as exc:
        adapter.resolve_market("BTC")
    assert "no markets are live" in str(exc.value).lower()


# ── margin health (isolated margin) ──────────────────────────────


def test_margin_health_uses_maintenance_ratio_zones():
    # marginRatioMaintenance is 0.05 in the fixture.
    assert A.assess_margin_health(0.20, 0.05).zone == "SAFE"        # 4.0x
    assert A.assess_margin_health(0.09, 0.05).zone == "WARNING"     # 1.8x
    assert A.assess_margin_health(0.06, 0.05).zone == "DANGER"      # 1.2x
    assert A.assess_margin_health(0.04, 0.05).zone == "LIQUIDATION" # 0.8x


def test_margin_health_refuses_to_guess_on_missing_data():
    with pytest.raises(Exception):
        A.assess_margin_health(float("nan"), 0.05)


# ── position sizing ──────────────────────────────────────────────


def test_two_percent_rule():
    # $10,000 account, entry 150, stop 140 -> $200 risk / $10 = 20 units.
    assert float(A.max_size_for_risk(10_000, 150, 140, 2)) == pytest.approx(20)


def test_zero_stop_distance_is_refused():
    with pytest.raises(Exception):
        A.max_size_for_risk(10_000, 150, 150, 2)


# ── gas modes ────────────────────────────────────────────────────


def test_gas_modes_are_the_three_supported_choices():
    assert A.parse_gas_mode("sponsored") == "sponsored"
    assert A.parse_gas_mode("self") == "self"
    assert A.parse_gas_mode("dynamic") == "dynamic"


def test_unknown_gas_mode_is_rejected_with_the_valid_set():
    with pytest.raises(Exception) as exc:
        A.parse_gas_mode("free-lunch")
    assert "sponsored" in str(exc.value)


def test_gas_mode_defaults_to_sponsored_so_a_new_wallet_works():
    adapter = AftermathMockAdapter()
    assert adapter.gas_config().mode == "sponsored"


def test_sponsor_may_equal_sender():
    # A common wrong assumption is that they must differ on Sui.
    adapter = AftermathMockAdapter()
    gas = adapter.gas_config()
    assert str(gas.sponsor_wallet) == str(adapter.wallet())


# ── transaction inspection gate ──────────────────────────────────


def _expectation(adapter, **kw):
    return A.TxExpectation(
        sender=adapter.wallet(),
        gas=adapter.gas_config(),
        intent=kw.pop("intent", "test"),
        **kw,
    )


def test_inspection_rejects_a_missing_signing_digest():
    # Signing transactionBytes instead of the digest is the mistake prevented.
    adapter = AftermathMockAdapter()
    with pytest.raises(A.TxInspectionError) as exc:
        A.inspect_tx({"transactionBytes": "aGVsbG8="}, _expectation(adapter))
    assert "signingdigest" in str(exc.value).lower().replace(" ", "")


def test_inspection_rejects_a_sender_mismatch():
    adapter = AftermathMockAdapter()
    built = {
        "transactionBytes": "bW9jaw==",
        "signingDigest": "ZGlnZXN0",
        "sender": "0x" + "99" * 32,
    }
    with pytest.raises(A.TxInspectionError) as exc:
        A.inspect_tx(built, _expectation(adapter))
    assert "sender" in str(exc.value).lower()


def test_inspection_accepts_a_well_formed_transaction():
    adapter = AftermathMockAdapter()
    built = {
        "transactionBytes": "bW9jaw==",
        "signingDigest": "ZGlnZXN0",
        "sender": str(adapter.wallet()),
    }
    inspected = A.inspect_tx(built, _expectation(adapter))
    assert isinstance(inspected, A.InspectedTx)


# ── preview gating ───────────────────────────────────────────────


def test_preview_error_body_on_http_200_is_treated_as_failure():
    # Preview routes can return HTTP 200 carrying {"error": ...}. Fail closed.
    result = A.classify_payload({"error": "insufficient margin"})
    assert result.ok is False
    assert "insufficient margin" in str(result.error)


def test_x_error_message_header_marks_a_200_as_an_error():
    result = A.classify_payload({"anything": 1}, headers={"X-Error-Message": "true"})
    assert result.ok is False


def test_preview_success_is_recognised():
    result = A.classify_payload({"estimatedFee": "10"})
    assert result.ok is True


# ── safety posture ───────────────────────────────────────────────


def test_ships_disarmed():
    adapter = AftermathMockAdapter()
    assert adapter.is_armed is False


def test_host_is_the_v2_production_host():
    adapter = AftermathMockAdapter()
    assert adapter.base_url == "https://aftermath.finance"
