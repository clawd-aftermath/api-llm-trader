#!/usr/bin/env python3
"""Verify dependencies are installed for API LLM Trader."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import output, error

req_ver = nacl_ver = None
try:
    import requests; req_ver = requests.__version__
except ImportError:
    pass
try:
    import nacl; nacl_ver = nacl.__version__
except ImportError:
    pass

if not req_ver or not nacl_ver:
    missing = []
    if not req_ver: missing.append("requests")
    if not nacl_ver: missing.append("PyNaCl")
    error(f"missing dependencies: {', '.join(missing)}; run: pip install {' '.join(missing)}")
output({"status": "ok", "requests": req_ver, "nacl": nacl_ver})
