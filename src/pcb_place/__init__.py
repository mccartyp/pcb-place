"""Importable package for the canonical pcb-place implementation.

The executable implementation is kept with its tool README at
``src/pcb-place/pcb_place.py``. This package exists so installed console scripts
can use a normal Python import target without keeping root-level compatibility
entry point files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_IMPL_PATH = Path(__file__).resolve().parents[1] / "pcb-place" / "pcb_place.py"
_SPEC = importlib.util.spec_from_file_location("_pcb_place_impl", _IMPL_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - defensive import guard
    raise ImportError(f"Unable to load pcb-place implementation from {_IMPL_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

for _name, _value in vars(_MODULE).items():
    if _name.startswith("__") and _name not in {"__version__"}:
        continue
    globals()[_name] = _value
