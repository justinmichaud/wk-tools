#!/usr/bin/env python3
"""The path bash callers (lib/bench.sh, image/*.sh, cmd/pi) run the slot CLI by; it is lib/wk/slot.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wk import slot  # noqa: E402

if __name__ == "__main__":
    slot.main(sys.argv[1:])
