#!/usr/bin/env python3
"""Run comprehensive live diagnostics on all lyrics providers and cascades in audiophile pipeline."""

import argparse
import sys
from pathlib import Path

# Ensure workspace is in python path
ws_root = Path(__file__).resolve().parent
if str(ws_root) not in sys.path:
    sys.path.insert(0, str(ws_root))

from lyrics_utils import run_providers_diagnostics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Output structured JSON diagnostics")
    args = parser.parse_args(argv)
    return run_providers_diagnostics(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
