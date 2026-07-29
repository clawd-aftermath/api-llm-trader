#!/usr/bin/env python3
"""``doctor`` — preflight for API LLM Trader.

The point of turnkey is that "clone, add wallet, go" either works or tells you
exactly why not. Without this, a misconfiguration surfaces later as an opaque
API rejection mid-trade. Doctor moves every knowable failure to second zero and
states the remedy in plain language.

Exits non-zero when a check fails, so it doubles as a CI/liveness gate.

    python3 scripts/doctor.py
    python3 scripts/doctor.py --json
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from af_adapter import (  # noqa: E402
    AdapterConfig,
    AftermathAdapter,
    ConfigError,
    format_sui,
    short_coin,
)

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"

_ICON = {PASS: "✓", FAIL: "✗", WARN: "!", SKIP: "–"}


class Report:
    def __init__(self):
        self.checks = []

    def add(self, group, name, status, detail, remedy=None):
        self.checks.append(
            {"group": group, "name": name, "status": status, "detail": detail, "remedy": remedy}
        )

    @property
    def failures(self):
        return [c for c in self.checks if c["status"] == FAIL]

    @property
    def warnings(self):
        return [c for c in self.checks if c["status"] == WARN]


def _err(exc):
    return str(exc) or type(exc).__name__


def run_checks(adapter):
    r = Report()
    cfg = adapter.config

    # ── API ──────────────────────────────────────────────────────
    base = adapter.base_url
    # The apex host is retired; subdomains (v2-preview, testnet) are live.
    # Built from parts so this check itself does not embed a retired-host
    # literal that the host guard would then flag.
    retired_apex = "https://" + "aftermath" + ".finance"
    if base.startswith(retired_apex):
        r.add("API", "base url", FAIL, base,
              "That is the retired v1 host. Unset AFTERMATH_HOST to use the v2 default.")
    else:
        r.add("API", "base url", PASS, base)

    reachable = False
    try:
        started = time.time()
        markets = adapter.get_all_markets(force=True)
        ms = int((time.time() - started) * 1000)
        reachable = True
        r.add("API", "reachable", PASS, f"{ms}ms")
        # Zero markets is EXPECTED before relaunch — never a failure.
        r.add(
            "Markets", "resolved",
            PASS if markets else WARN,
            f"{len(markets)} market(s) for {short_coin(cfg.collateral_coin_type)}"
            if markets else f"none live yet for {short_coin(cfg.collateral_coin_type)}",
            None if markets else
            "Expected before launch — markets appear here once listed.",
        )
    except Exception as exc:  # noqa: BLE001
        r.add("API", "reachable", FAIL, _err(exc), f"Check network access to {base}.")
        r.add("Markets", "resolved", SKIP, "API unreachable")

    # ── Wallet ───────────────────────────────────────────────────
    wallet = None
    try:
        wallet = adapter.wallet()
        r.add("Wallet", "address", PASS, _abbrev(str(wallet)))
    except ConfigError as exc:
        r.add("Wallet", "address", FAIL, _err(exc),
              "Set AFTERMATH_WALLET_ADDRESS=0x... (the only required setting).")

    sui_balance = None
    if wallet and reachable:
        try:
            sui_balance = adapter.sui_balance()
            r.add("Wallet", "SUI balance", PASS,
                  format_sui(sui_balance) if sui_balance is not None else "unknown")
            collat = adapter.collateral_balance()
            r.add("Wallet", f"{short_coin(cfg.collateral_coin_type)} balance",
                  PASS if collat else WARN,
                  str(collat) if collat is not None else "unknown",
                  None if collat else "No collateral — deposit before trading.")
        except Exception as exc:  # noqa: BLE001
            # /api/wallet/* is in the spec but 404s live; degrade, do not fail.
            r.add("Wallet", "balances", WARN, _err(exc),
                  "Wallet balance routes are unavailable on this deployment.")

    # ── Account ──────────────────────────────────────────────────
    if wallet and reachable:
        try:
            caps = adapter.discover_accounts()
            r.add("Account", "perps account",
                  PASS if caps else WARN,
                  f"{len(caps)} account(s) owned" if caps else "none yet",
                  None if caps else
                  "Run onboarding — it is a single atomic transaction.")
        except Exception as exc:  # noqa: BLE001
            r.add("Account", "perps account", WARN, _err(exc))
    else:
        r.add("Account", "perps account", SKIP, "needs wallet + API")

    # ── Gas ──────────────────────────────────────────────────────
    try:
        gas = adapter.check_gas_mode()
        # check_gas_mode returns a mapping; accept an object too so this keeps
        # working if it is ever promoted to a dataclass.
        if isinstance(gas, dict):
            ok, detail, remedy = gas.get("ok"), gas.get("detail", ""), gas.get("remedy")
        else:
            ok = getattr(gas, "ok", None)
            detail = getattr(gas, "detail", str(gas))
            remedy = getattr(gas, "remedy", None)
        r.add("Gas", f"mode: {cfg.gas_mode}", PASS if ok else FAIL, detail, remedy)
    except Exception as exc:  # noqa: BLE001
        r.add("Gas", f"mode: {cfg.gas_mode}", WARN, _err(exc))
    r.add("Gas", "budget", PASS,
          f"{format_sui(cfg.gas_budget_mist)} (explicit, never auto-estimated)")
    if cfg.gas_mode == "dynamic":
        r.add("Gas", "gas coin",
              PASS if cfg.gas_coin_type else FAIL,
              short_coin(cfg.gas_coin_type) if cfg.gas_coin_type else "not set",
              None if cfg.gas_coin_type else
              "Set AFTERMATH_GAS_COIN_TYPE, or use AFTERMATH_GAS_MODE=sponsored.")

    # ── Safety posture ───────────────────────────────────────────
    r.add("Safety", "trading",
          PASS,
          "ARMED — live orders enabled" if adapter.is_armed
          else "dry-run — nothing can be signed or submitted")

    return r


def _abbrev(addr):
    return f"{addr[:8]}…{addr[-6:]}" if len(addr) > 14 else addr


def render(report):
    print()
    print("  doctor — API LLM Trader")
    print()
    last = None
    for c in report.checks:
        if c["group"] != last:
            print(f"  {c['group']}")
            last = c["group"]
        print(f"  {_ICON[c['status']]}  {c['name']:<22} {c['detail']}")
    print()
    for c in report.failures + report.warnings:
        if c["remedy"]:
            tag = "fix " if c["status"] == FAIL else "note"
            print(f"  {tag} {c['name']}: {c['remedy']}")
    if report.failures:
        print(f"\n  {len(report.failures)} check(s) failed.")
    elif report.warnings:
        print(f"\n  {len(report.warnings)} warning(s).")
    else:
        print("  All checks passed.")
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preflight checks for API LLM Trader")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        adapter = AftermathAdapter(config=AdapterConfig.from_env())
    except Exception as exc:  # noqa: BLE001
        print(f"doctor: cannot construct adapter: {_err(exc)}", file=sys.stderr)
        return 1

    report = run_checks(adapter)

    if args.json:
        import json

        print(json.dumps({"checks": report.checks,
                          "failed": len(report.failures),
                          "warnings": len(report.warnings)}, indent=2))
    else:
        render(report)

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
