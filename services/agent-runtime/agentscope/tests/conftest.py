from __future__ import annotations

import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
runtime_root = str(RUNTIME_ROOT)
if runtime_root in sys.path:
    sys.path.remove(runtime_root)
sys.path.insert(0, runtime_root)
