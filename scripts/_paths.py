"""Shared path policies for API LLM Trader."""

import os
from pathlib import Path


def _data_dir() -> Path:
    """Return the data directory for API LLM Trader state and config."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", "~"))
    else:
        base = Path.home()
    return base / ".aftermath" / "api-llm-trader"


def credentials_path() -> Path:
    return _data_dir() / "credentials"


def paper_state_path() -> Path:
    env = os.environ.get("AFTERMATH_PAPER_STATE_PATH")
    if env:
        return Path(env)
    return _data_dir() / "paper-state.json"


def symbol_cache_path(host: str = "") -> Path:
    """Return the cache path for symbol resolution, keyed by host."""
    safe = host.replace("://", "_").replace("/", "_").replace(".", "_")
    return _data_dir() / f"symbols-{safe}.json" if safe else _data_dir() / "symbols.json"
