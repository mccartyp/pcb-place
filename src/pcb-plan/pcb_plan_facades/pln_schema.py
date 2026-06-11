"""Authoritative board.pln schema section names and lightweight validation."""
from __future__ import annotations

from typing import Any, List, Mapping

from .provenance import unwrap_provenance

SCHEMA_VERSION = "0.1"
SECTIONS = {
    "board", "regions", "keepouts", "roles", "clusters", "high_speed", "routing",
    "stackup", "differential_pairs", "net_classes", "simulation", "provenance",
    "fixed", "routing_overrides",
}


def validate_pln(payload: Mapping[str, Any]) -> List[str]:
    warnings: List[str] = []
    for key in payload:
        if key not in SECTIONS:
            warnings.append(f"Unknown board.pln section {key!r}.")
    board = payload.get("board")
    if isinstance(board, Mapping):
        for field in ("width", "height"):
            value = unwrap_provenance(board.get(field))
            if value is not None and not (isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0):
                warnings.append(f"board.{field} must be positive.")
    return warnings
