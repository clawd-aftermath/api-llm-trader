"""Shared CLI helpers for API LLM Trader scripts."""

import json
import sys
from argparse import ArgumentParser


def output(data):
    """Print structured JSON to stdout."""
    print(json.dumps(data, default=str, indent=2))


def error(message):
    """Print JSON error and exit 1."""
    print(json.dumps({"error": message}))
    sys.exit(1)


class JsonArgumentParser(ArgumentParser):
    """ArgumentParser that emits errors as JSON instead of plain text."""

    def error(self, message):
        print(json.dumps({"error": message}))
        sys.exit(2)
