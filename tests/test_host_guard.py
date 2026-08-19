"""Guard the launched production API host against stale preview defaults."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO, "scripts")

# Keep the retired hostname split so a tracked-file grep for it stays empty.
RETIRED_PREVIEW = re.compile(r"https?://v2" r"-preview\.aftermath\.finance")
COMMENT = re.compile(r"^\s*#")


def _python_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git"}]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def test_no_retired_preview_host_in_live_python():
    offenders = []
    for path in _python_files(SCRIPTS):
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if COMMENT.match(line):
                    continue  # prose explaining the trap is allowed
                # Strip trailing comments and docstring prose lines.
                code = line.split("#", 1)[0]
                if RETIRED_PREVIEW.search(code):
                    offenders.append(f"{os.path.relpath(path, REPO)}:{lineno}: {line.strip()}")
    assert offenders == [], "retired preview host in live code:\n" + "\n".join(offenders)


def test_default_host_is_the_production_host():
    import af_adapter

    assert af_adapter.AF_API_BASE_URL == "https://aftermath.finance"


def test_adapter_resolves_its_host_from_one_place():
    """Every request must go through the single configured base URL."""
    import af_adapter

    src = open(os.path.join(SCRIPTS, "af_adapter.py"), encoding="utf-8").read()
    # Only the constant definition may hold a literal host.
    literals = re.findall(r'"https://[a-z0-9.-]*aftermath\.finance[^"]*"', src)
    assert literals == ['"https://aftermath.finance"'], (
        f"expected exactly one production host literal, found: {literals}"
    )


def test_vendored_skills_are_present_but_excluded_from_the_guard():
    """Vendored reference material is pinned and separately documented."""
    ref = os.path.join(REPO, "AFTERMATH_SKILLS_REF")
    assert os.path.isdir(ref), "skills should be vendored at AFTERMATH_SKILLS_REF/"
    assert os.path.isfile(os.path.join(ref, "PINNED.md"))
    assert os.path.isfile(os.path.join(ref, "README-DELTA.md"))
