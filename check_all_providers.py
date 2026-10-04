#!/usr/bin/env python3
"""Run comprehensive live diagnostics on all lyrics providers and cascades in audiophile pipeline."""

import sys
from pathlib import Path

# Ensure workspace is in python path
ws_root = Path(__file__).resolve().parent
if str(ws_root) not in sys.path:
    sys.path.insert(0, str(ws_root))

from lyrics_utils import run_providers_diagnostics

if __name__ == "__main__":
    as_json = "--json" in sys.argv
    sys.exit(run_providers_diagnostics(as_json=as_json))
