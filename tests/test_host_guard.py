"""Guard: the retired v1 host must never reappear in live code.

Three sources keep trying to reintroduce it:
  1. the vendored skills' examples (`BASE_URL = "https://aftermath.finance"`)
  2. the OpenAPI spec's `servers` block, which labels it "Production server"
  3. anything generated from that spec

All three are stale. `v2-preview.aftermath.finance` is production mainnet.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO, "scripts")

# Only the APEX host is retired. Subdomains are live and legitimate:
#   v2-preview.aftermath.finance  -> production mainnet (251 paths)
#   testnet.aftermath.finance     -> V2 testnet         (250 paths, verified)
# So match the scheme immediately followed by the apex, nothing else.
RETIRED = re.compile(r"https?://aftermath\.finance")
COMMENT = re.compile(r"^\s*#")


def _python_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git"}]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def test_no_retired_host_in_live_python():
    offenders = []
    for path in _python_files(SCRIPTS):
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if COMMENT.match(line):
                    continue  # prose explaining the trap is allowed
                # Strip trailing comments and docstring prose lines.
                code = line.split("#", 1)[0]
                if RETIRED.search(code):
                    offenders.append(f"{os.path.relpath(path, REPO)}:{lineno}: {line.strip()}")
    assert offenders == [], "retired v1 host in live code:\n" + "\n".join(offenders)


def test_default_host_is_the_v2_production_host():
    import af_adapter

    assert af_adapter.AF_API_BASE_URL.startswith("https://v2-preview.aftermath.finance")


def test_adapter_resolves_its_host_from_one_place():
    """Every request must go through the single configured base URL."""
    import af_adapter

    src = open(os.path.join(SCRIPTS, "af_adapter.py"), encoding="utf-8").read()
    # Only the constant definition may hold a literal host.
    literals = re.findall(r'"https://[a-z0-9.-]*aftermath\.finance[^"]*"', src)
    assert literals, "expected the host constant to be defined here"
    for lit in literals:
        assert "v2-preview.aftermath.finance" in lit or "testnet.aftermath.finance" in lit, (
            f"non-v2 host literal: {lit}"
        )


def test_vendored_skills_are_present_but_excluded_from_the_guard():
    """The vendored skills DO contain the retired host — that is expected.

    They are pinned verbatim; the discrepancy is documented in README-DELTA.md
    rather than edited away. This test asserts the vendoring exists so the guard
    above is meaningfully scoped, and that the delta note is present.
    """
    ref = os.path.join(REPO, "AFTERMATH_SKILLS_REF")
    assert os.path.isdir(ref), "skills should be vendored at AFTERMATH_SKILLS_REF/"
    assert os.path.isfile(os.path.join(ref, "PINNED.md"))
    assert os.path.isfile(os.path.join(ref, "README-DELTA.md"))
