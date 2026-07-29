"""The mock twin must stay interface-identical to the real adapter.

A mock that drifts is worse than no mock: tests keep passing while the thing
they stand in for has moved on. These tests make the parity structural rather
than a matter of remembering.
"""

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from af_adapter import AftermathAdapter  # noqa: E402
from af_mock import AftermathMockAdapter, MockTransport  # noqa: E402


def _public_methods(cls):
    return {
        name: obj
        for name, obj in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_mock_exposes_every_public_adapter_method():
    real = _public_methods(AftermathAdapter)
    mock = _public_methods(AftermathMockAdapter)
    missing = sorted(set(real) - set(mock))
    assert missing == [], f"mock is missing adapter methods: {missing}"


def test_signatures_match_for_inherited_and_overridden_methods():
    real = _public_methods(AftermathAdapter)
    mock = _public_methods(AftermathMockAdapter)
    mismatched = []
    for name, fn in real.items():
        if name not in mock:
            continue
        if inspect.signature(fn) != inspect.signature(mock[name]):
            mismatched.append(
                f"{name}: real{inspect.signature(fn)} != mock{inspect.signature(mock[name])}"
            )
    assert mismatched == [], "signature drift:\n" + "\n".join(mismatched)


def test_mock_is_a_real_adapter():
    # Subclassing is what makes the parity structural: the mock runs the real
    # market resolution, margin maths, and inspection code.
    assert issubclass(AftermathMockAdapter, AftermathAdapter)


def test_mock_needs_no_environment():
    # Must construct with a completely empty environment — no wallet, no keys.
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith("AFTERMATH") or k.startswith("AF_"):
                del os.environ[k]
        adapter = AftermathMockAdapter()
        assert adapter.wallet()
        assert adapter.is_armed is False
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_mock_never_signs_or_submits():
    adapter = AftermathMockAdapter()
    with pytest.raises(RuntimeError, match="cannot sign"):
        adapter.set_signer(lambda digest: "signature")
    with pytest.raises(RuntimeError, match="cannot submit"):
        adapter.sign_and_submit(object())


def test_mock_makes_no_network_calls():
    # The transport is the only seam; if anything reached the network it would
    # not be recorded here.
    adapter = AftermathMockAdapter()
    adapter.get_all_markets()
    adapter.get_all_mids()
    assert adapter.paths_called(), "expected the mock transport to record calls"
    assert all(p.startswith("/api/") for p in adapter.paths_called())


def test_unknown_route_fails_loudly():
    # A missing fixture must break the test, not silently return None.
    transport = MockTransport()
    with pytest.raises(RuntimeError, match="no fixture"):
        transport.handle("/api/perpetuals/not-a-real-route", {})
