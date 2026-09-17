#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Development/CI entry point to the SAME validator used during experiment init."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from benchpark_container.reproducibility import main

if __name__ == '__main__':
    raise SystemExit(main())
