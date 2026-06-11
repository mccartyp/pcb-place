"""board.pln parsing and serialization entry points.

The current command-line compatibility module exposes the concrete parser; this
module documents the ownership boundary for future package migration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import pcb_plan as _legacy


def load_pln(path: Optional[Path]) -> dict[str, Any]:
    return _legacy.load_pln(path)


def serialize_pln(payload: Mapping[str, Any]) -> str:
    return _legacy.serialize_pln(payload)
