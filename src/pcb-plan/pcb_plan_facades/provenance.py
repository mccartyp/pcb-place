"""Provenance helpers for visible board.pln inference assumptions."""
from __future__ import annotations

from typing import Any, Dict, Mapping


def provenance_value(value: Any, source: str, confidence: str = "medium", requires_review: bool = True) -> Dict[str, Any]:
    return {"value": value, "source": source, "confidence": confidence, "requires_review": requires_review}


def unwrap_provenance(value: Any) -> Any:
    if isinstance(value, Mapping):
        if {"value", "source", "confidence", "requires_review"}.issubset(value.keys()):
            return unwrap_provenance(value.get("value"))
        return {str(k): unwrap_provenance(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap_provenance(v) for v in value]
    return value
