"""board.pln feedback update and patch helpers."""
from __future__ import annotations

from typing import Any, Mapping

import pcb_plan as _legacy


def apply_feedback_updates(payload: Mapping[str, Any], reports: Mapping[str, Any]) -> dict[str, Any]:
    return _legacy.apply_feedback_updates(payload, reports)


def unified_diff_text(old: str, new: str, old_name: str = "board.pln", new_name: str = "board.updated.pln") -> str:
    return _legacy.unified_diff_text(old, new, old_name, new_name)
