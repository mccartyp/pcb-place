"""Initial board.pln generation helpers."""
from __future__ import annotations

from typing import Any, Mapping

import pcb_plan as _legacy


def plan_to_pln(plan: Any) -> Mapping[str, Any]:
    return _legacy.plan_to_pln(plan)
