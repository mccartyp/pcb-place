#!/usr/bin/env python3
"""
pcb-place: deterministic footprint placement for KiCad .kicad_pcb files.

pcb-place is a small sidecar tool that applies a Python/Starlark-like placement
DSL (.ppl) to an existing KiCad board. It is intentionally independent of any
schematic capture system: use it after Zener/pcb layout generation, after a
KiCad netlist import, or against any board that already contains footprints.

Typical workflow:

    pcb-place board.kicad_pcb placement.ppl -o board.placed.kicad_pcb
    pcb-place board.kicad_pcb placement.ppl --in-place
    pcb-place board.kicad_pcb placement.ppl --check

Design goals:

* Separate electrical intent from physical placement intent.
* Make board floorplanning deterministic and reviewable in version control.
* Express reusable physical relationships: anchors, satellites, rows, columns,
  grids, mirrored parts, repeated channels, between/inline placement, corners,
  edges, keepouts, and corridors.
* Avoid dependencies on KiCad's Python runtime so the tool works in lightweight
  CI, Codex, and containerized environments.

Current scope:

* Updates footprint-level `(at x y rot)` records in .kicad_pcb files.
* Parses Keepout() and Corridor() rules and reports them for review.
* Does not yet emit KiCad keepout zones, drawings, tracks, vias, or locked flags.

Security note:

The .ppl file is executed in a restricted namespace containing only DSL
functions and constants. It is still code. Treat placement files like source
code from a trusted repository.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__version__ = "0.14.0"

Number = float | int
Point = Tuple[float, float]


# ---------------------------------------------------------------------------
# Errors and diagnostics
# ---------------------------------------------------------------------------


class PlacementError(RuntimeError):
    """Raised for invalid DSL input, invalid KiCad input, or failed placement."""


@dataclasses.dataclass(frozen=True)
class Message:
    """A structured diagnostic emitted while applying placement rules."""

    level: str  # "info", "warn", "error", "note", "place"
    text: str

    def __str__(self) -> str:
        return self.text if self.level == "place" else f"{self.level}: {self.text}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Footprint:
    """A parsed footprint block from a KiCad .kicad_pcb file.

    start/end are offsets into the original file. Text rewriting keeps the tool
    small and portable; users do not need KiCad installed in CI just to run a
    placement check.
    """

    ref: str
    start: int
    end: int
    text: str
    x: float
    y: float
    rot: float
    at_has_rot: bool = False
    at_rot_text: Optional[str] = None
    layer: Optional[str] = None



@dataclasses.dataclass(frozen=True)
class BBox:
    """Axis-aligned footprint bounding box in board coordinates."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def expanded(self, amount: float) -> "BBox":
        return BBox(self.min_x - amount, self.min_y - amount, self.max_x + amount, self.max_y + amount)

    def overlaps(self, other: "BBox", *, eps: float = 1e-9) -> bool:
        return not (self.max_x <= other.min_x + eps or other.max_x <= self.min_x + eps or
                    self.max_y <= other.min_y + eps or other.max_y <= self.min_y + eps)

    def clearance_to(self, other: "BBox") -> float:
        dx = max(other.min_x - self.max_x, self.min_x - other.max_x, 0.0)
        dy = max(other.min_y - self.max_y, self.min_y - other.max_y, 0.0)
        if dx == 0.0 and dy == 0.0:
            overlap_x = min(self.max_x, other.max_x) - max(self.min_x, other.min_x)
            overlap_y = min(self.max_y, other.max_y) - max(self.min_y, other.min_y)
            return -max(0.0, min(overlap_x, overlap_y))
        return math.hypot(dx, dy)

    def contains_bbox(self, other: "BBox", *, eps: float = 1e-9) -> bool:
        return (self.min_x - eps <= other.min_x and other.max_x <= self.max_x + eps and
                self.min_y - eps <= other.min_y and other.max_y <= self.max_y + eps)

    def translated(self, dx: float, dy: float) -> "BBox":
        return BBox(self.min_x + dx, self.min_y + dy, self.max_x + dx, self.max_y + dy)

    def as_report(self) -> Dict[str, float]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class BBoxInfo:
    ref: str
    bbox: BBox
    fallback: bool = False
    warning: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PlacementRegion:
    """Legal strip used for grouped support-part placement around a parent."""

    owner_ref: str
    owner_bbox: BBox
    expanded_owner_bbox: BBox
    side: str
    legal_bbox: BBox
    preferred_axis: str
    cross_axis: str
    capacity_length: float
    capacity_area: float
    role: str = "support"

    def as_report(self) -> Dict[str, Any]:
        return {
            "owner_ref": self.owner_ref,
            "owner_bbox": self.owner_bbox.as_report(),
            "expanded_owner_bbox": self.expanded_owner_bbox.as_report(),
            "side": self.side,
            "legal_bbox": self.legal_bbox.as_report(),
            "preferred_axis": self.preferred_axis,
            "cross_axis": self.cross_axis,
            "capacity_length": round(self.capacity_length, 6),
            "capacity_area": round(self.capacity_area, 6),
            "role": self.role,
        }


@dataclasses.dataclass
class ClearanceRules:
    """Configurable minimum clearance rules in millimeters.

    Defaults are deliberately conservative so generated placements never
    default to essentially-touching parts; override via Spacing() in .ppl.
    """

    default: float = 0.25
    passive_to_passive: float = 0.25
    passive_to_ic: float = 0.40
    ic_to_ic: float = 0.75
    connector: float = 1.00
    mechanical: float = 1.00

    def required_for(self, class_a: str, class_b: str) -> float:
        pair = {class_a, class_b}
        if "mechanical" in pair:
            return self.mechanical
        if "connector" in pair:
            return self.connector
        if pair == {"passive"}:
            return self.passive_to_passive
        if pair == {"ic"}:
            return self.ic_to_ic
        if pair == {"passive", "ic"}:
            return self.passive_to_ic
        return self.default


@dataclasses.dataclass
class PlacementPolicy:
    avoid_overlap: bool = True
    allow_anchor_move: bool = False
    max_search_radius: float = 5.0
    search_step: float = 0.5


@dataclasses.dataclass
class AutoAdjustment:
    ref: str
    requested_x: float
    requested_y: float
    placed_x: float
    placed_y: float
    reason: str


@dataclasses.dataclass(frozen=True)
class SearchCandidate:
    """A single candidate location evaluated by the placement search engine."""

    x: float
    y: float
    label: str
    legal: bool
    reason: Optional[str]
    distance: float
    clearance_margin: Optional[float]
    edge_distance: Optional[float]
    score: float
    routing_penalty: float = 0.0
    region_penalty: float = 0.0
    shape_penalty: float = 0.0
    high_speed_penalty: float = 0.0
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_report(self) -> Dict[str, Any]:
        payload = {
            "x": round(self.x, 6),
            "y": round(self.y, 6),
            "label": self.label,
            "legal": self.legal,
            "reason": self.reason,
            "distance": round(self.distance, 6),
            "clearance_margin": None if self.clearance_margin is None else round(self.clearance_margin, 6),
            "edge_distance": None if self.edge_distance is None else round(self.edge_distance, 6),
            "routing_penalty": round(self.routing_penalty, 6),
            "region_penalty": round(self.region_penalty, 6),
            "shape_penalty": round(self.shape_penalty, 6),
            "high_speed_penalty": round(self.high_speed_penalty, 6),
            "score": round(self.score, 6),
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclasses.dataclass
class SearchOutcome:
    """The result of searching for a legal placement near a target coordinate."""

    ref: str
    target: Point
    attempted: List[SearchCandidate]
    chosen: Optional[SearchCandidate]
    search_radius_used: float
    fallback_used: bool

    @property
    def rejected(self) -> List[SearchCandidate]:
        return [c for c in self.attempted if not c.legal]

    @property
    def best_candidate(self) -> Optional[SearchCandidate]:
        return min(self.attempted, key=lambda c: c.score) if self.attempted else None

    @property
    def nearest_legal(self) -> Optional[SearchCandidate]:
        legal = [c for c in self.attempted if c.legal]
        return min(legal, key=lambda c: c.distance) if legal else None

    def as_report(self) -> Dict[str, Any]:
        best = self.best_candidate
        nearest = self.nearest_legal
        return {
            "target": {"x": self.target[0], "y": self.target[1]},
            "chosen": None if self.chosen is None else self.chosen.as_report(),
            "winning_score": None if self.chosen is None else round(self.chosen.score, 6),
            "attempted_candidates": [c.as_report() for c in self.attempted],
            "rejected_candidates": [c.as_report() for c in self.rejected],
            "top_rejected_candidates": [c.as_report() for c in sorted(self.rejected, key=lambda c: c.score)[:5]],
            "best_candidate": None if best is None else best.as_report(),
            "nearest_legal_location": None if nearest is None else nearest.as_report(),
            "search_radius_used": self.search_radius_used,
            "fallback_used": self.fallback_used,
        }


@dataclasses.dataclass
class ReflowAttempt:
    """A record of one reflow attempt triggered by a local placement failure.

    The floorplanner treats a local placement failure as evidence that the
    surrounding floorplan is suboptimal, not that the component is at fault.
    Each attempt records which level of the reflow ladder was reached, what was
    moved, and whether the conflict was resolved.
    """

    trigger_ref: str
    why: str
    level: int
    strategy: str
    moved_components: List[str] = dataclasses.field(default_factory=list)
    moved_parents: List[str] = dataclasses.field(default_factory=list)
    score_before: Optional[float] = None
    score_after: Optional[float] = None
    resolved: bool = False

    @property
    def score_delta(self) -> Optional[float]:
        if self.score_before is None or self.score_after is None:
            return None
        return round(self.score_after - self.score_before, 6)

    def as_report(self) -> Dict[str, Any]:
        return {
            "trigger_ref": self.trigger_ref,
            "why": self.why,
            "level": self.level,
            "strategy": self.strategy,
            "moved_components": list(self.moved_components),
            "moved_parents": list(self.moved_parents),
            "score_before": None if self.score_before is None else round(self.score_before, 6),
            "score_after": None if self.score_after is None else round(self.score_after, 6),
            "score_delta": self.score_delta,
            "resolved": self.resolved,
        }


@dataclasses.dataclass
class ParkedPlacement:
    """A component the floorplanner could not place legally after reflow.

    Parking is the last resort.  The component is still placed (at its best
    effort position) so downstream tools have a coordinate, but it is flagged as
    degraded and carries a quality penalty plus a review-required marker.
    """

    ref: str
    why: str
    reason: str
    x: float
    y: float
    penalty: float
    review_required: bool = True

    def as_report(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "why": self.why,
            "reason": self.reason,
            "x": round(self.x, 6),
            "y": round(self.y, 6),
            "penalty": round(self.penalty, 6),
            "review_required": self.review_required,
        }


@dataclasses.dataclass
class ArraySearch:
    """The outcome of searching for a legal grouped-array layout.

    Separated from committing/parking so the reflow loop can re-run the search
    after relocating obstacles or nudging a movable parent.
    """

    refs: List[str]
    why: str
    rot: float
    requested_spacing: float
    clearance_override: Optional[float]
    allow_arbitrary_rotation: bool
    succeeded: Optional[List[Tuple[float, float]]]
    chosen_attempt: Optional[Dict[str, Any]]
    attempts: List[Dict[str, Any]]
    side_origin: Point
    sides: List[str]
    region: Optional[BBox]
    parent_ref: Optional[str]
    parent_bbox: Optional[BBox]
    parent_expanded: Optional[BBox]
    inferred_side: Optional[str]
    pad_origin: Optional[Point]
    detail: str = ""


@dataclasses.dataclass
class PlacementUpdate:
    """A requested footprint placement and how its rotation should be written."""

    ref: str
    x: float
    y: float
    final_rot: float
    write_rot: Optional[float]
    why: str
    note: Optional[str]
    original_x: float
    original_y: float
    original_rot: float
    rotation_changed: bool
    outside_board: bool
    delta_x: float
    delta_y: float


@dataclasses.dataclass(frozen=True)
class GeneratedUUID:
    """A UUID assigned to an object newly created by pcb-place."""

    object: str
    uuid: str


@dataclasses.dataclass(frozen=True)
class BoardGeometry:
    """Authoritative rectangular board geometry used for validation and reporting."""

    origin_x: float
    origin_y: float
    width: float
    height: float
    source: str = "unknown"

    @property
    def min_x(self) -> float:
        return self.origin_x

    @property
    def min_y(self) -> float:
        return self.origin_y

    @property
    def max_x(self) -> float:
        return self.origin_x + self.width

    @property
    def max_y(self) -> float:
        return self.origin_y + self.height

    def contains(self, x: float, y: float, *, eps: float = 1e-9) -> bool:
        return self.min_x - eps <= x <= self.max_x + eps and self.min_y - eps <= y <= self.max_y + eps

    def bounds(self) -> Dict[str, float]:
        return {"min_x": self.min_x, "min_y": self.min_y, "max_x": self.max_x, "max_y": self.max_y}

    def as_report(self) -> Dict[str, Any]:
        report = dataclasses.asdict(self) | self.bounds()
        report["margin_applied"] = DEFAULT_FOOTPRINT_MARGIN_MM if self.source == "footprint_extents" else 0.0
        return report

    def nearly_equals(self, other: "BoardGeometry", *, eps: float = 1e-6) -> bool:
        return (abs(self.origin_x - other.origin_x) <= eps and abs(self.origin_y - other.origin_y) <= eps
                and abs(self.width - other.width) <= eps and abs(self.height - other.height) <= eps)

    @classmethod
    def from_edge_cuts(cls, text: str) -> Optional["BoardGeometry"]:
        return parse_edge_cuts_geometry(text)


@dataclasses.dataclass
class BoardInfo:
    """Board metadata declared in placement.ppl."""

    width: Optional[float] = None
    height: Optional[float] = None
    units: str = "mm"
    origin: Point = (0.0, 0.0)
    name: Optional[str] = None
    emit_outline: bool = False

    @property
    def origin_x(self) -> float:
        return self.origin[0]

    @property
    def origin_y(self) -> float:
        return self.origin[1]


@dataclasses.dataclass
class AliasDiagnostics:
    """Diagnostics and metadata for imported netlist aliases."""

    warnings: List[str] = dataclasses.field(default_factory=list)
    errors: List[str] = dataclasses.field(default_factory=list)
    source: Optional[str] = None
    parser: Optional[str] = None


@dataclasses.dataclass
class PlacementModel:
    """In-memory representation of all placement rules declared in .ppl.

    The model deliberately separates *what the user wrote* from KiCad file
    details.  Rules are ordered, regions are named rectangles, and locks are
    semantic constraints that the placement engine enforces while it applies
    rules.  This keeps the public DSL small while leaving room for a future
    constraint-solver backend.
    """

    board: BoardInfo = dataclasses.field(default_factory=BoardInfo)
    rules: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    aliases: Dict[str, str] = dataclasses.field(default_factory=dict)
    imported_aliases: Dict[str, str] = dataclasses.field(default_factory=dict)
    alias_diagnostics: AliasDiagnostics = dataclasses.field(default_factory=AliasDiagnostics)
    regions: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    priority_refs: Dict[str, float] = dataclasses.field(default_factory=dict)
    soft_refs: Set[str] = dataclasses.field(default_factory=set)
    clearance: ClearanceRules = dataclasses.field(default_factory=ClearanceRules)
    policy: PlacementPolicy = dataclasses.field(default_factory=PlacementPolicy)
    part_classes: Dict[str, str] = dataclasses.field(default_factory=dict)

    def add(self, rule_type: str, **kwargs: Any) -> None:
        self.rules.append({"type": rule_type, **kwargs})

    def alias(self, name: str, ref: str) -> None:
        if not name or not ref:
            raise PlacementError("Alias(name, ref) requires non-empty strings")
        self.aliases[name] = ref

    def region(self, name: str, *, x: Number, y: Number, w: Number, h: Number,
               role: Optional[str] = None, note: Optional[str] = None, priority: Optional[Number] = None,
               kind: Optional[str] = None, movable: Optional[bool] = None) -> None:
        if not name:
            raise PlacementError("Region(name, ...) requires a non-empty name")
        if w <= 0 or h <= 0:
            raise PlacementError("Region(w=..., h=...) must be positive")
        self.regions[name] = {
            "name": name, "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "role": role, "note": note, "priority": None if priority is None else float(priority),
            # Region typing + a movable flag let the floorplanner decide whether a
            # region may be reflowed (decoupling/support/power island) or is a hard
            # keepout it must respect (RF, high-speed corridor, mechanical).
            "kind": kind, "movable": True if movable is None else bool(movable),
        }


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _as_point(value: Any, *, field: str = "point") -> Point:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise PlacementError(f"Expected {field}=(x, y), got {value!r}")
    return float(value[0]), float(value[1])


def _fmt_num(value: float) -> str:
    """Format a KiCad numeric field deterministically."""

    if not math.isfinite(value):
        return str(value)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _angle_between(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _normalize_rotation(value: float) -> float:
    """Normalize KiCad rotation values into [0, 360)."""

    value = float(value) % 360.0
    return 0.0 if abs(value) < 1e-9 or abs(value - 360.0) < 1e-9 else value


def _cardinal_rotation(value: float) -> float:
    return _normalize_rotation(round(_normalize_rotation(value) / 90.0) * 90.0)


def _board_to_abs(model: PlacementModel, x: float, y: float) -> Point:
    ox, oy = model.board.origin
    return ox + float(x), oy + float(y)


def board_geometry_from_definition(board: BoardInfo) -> Optional[BoardGeometry]:
    if board.width is None or board.height is None:
        return None
    return BoardGeometry(board.origin_x, board.origin_y, float(board.width), float(board.height), "placement_file")


def board_bounds(board: BoardInfo) -> Optional[Dict[str, float]]:
    geometry = board_geometry_from_definition(board)
    return None if geometry is None else geometry.bounds()


# Last-resort margin (mm) added around footprint extents when neither board.pln
# geometry nor an Edge.Cuts rectangle is available. Keeps the inferred board from
# clipping pads/courtyards and guarantees a non-zero size for valid footprints.
DEFAULT_FOOTPRINT_MARGIN_MM = 5.0


def _footprint_abs_bbox(fp: Footprint) -> BBox:
    """Absolute-coordinate bounding box for a single footprint.

    Uses the footprint's parsed pad/graphic extents when available; if no bbox
    can be parsed, falls back to the footprint at-position as a zero-size point so
    a board with a single placed part still yields a sane extent after margin.
    """

    local = _parse_fp_local_bbox(fp)
    if local is not None:
        return _transform_local_bbox(local, fp.x, fp.y, fp.rot)
    return BBox(fp.x, fp.y, fp.x, fp.y)


def footprint_extents_bounds(footprints: Mapping[str, Footprint]) -> Dict[str, float]:
    """Min/max over footprint *bounding boxes* (not just at-positions)."""

    if not footprints:
        return {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0}
    boxes = [_footprint_abs_bbox(fp) for fp in footprints.values()]
    return {
        "min_x": min(b.min_x for b in boxes),
        "min_y": min(b.min_y for b in boxes),
        "max_x": max(b.max_x for b in boxes),
        "max_y": max(b.max_y for b in boxes),
    }


def infer_geometry_from_footprints(footprints: Mapping[str, Footprint],
                                   *, margin: float = DEFAULT_FOOTPRINT_MARGIN_MM) -> BoardGeometry:
    """Last-resort board geometry from footprint extents.

    Uses footprint bounding boxes (with a per-footprint at-position fallback) plus
    a fixed margin so valid footprints never collapse to a zero-size board. This is
    a low-confidence fallback: callers should warn and ask the user to review.
    """

    bounds = footprint_extents_bounds(footprints)
    if not footprints:
        # Nothing to infer from: stay invalid (zero size) so callers reject it with
        # a clear, sourced error instead of inventing a 2*margin square board.
        return BoardGeometry(bounds["min_x"], bounds["min_y"], 0.0, 0.0, "footprint_extents")
    margin = max(0.0, float(margin))
    width = bounds["max_x"] - bounds["min_x"] + 2.0 * margin
    height = bounds["max_y"] - bounds["min_y"] + 2.0 * margin
    return BoardGeometry(bounds["min_x"] - margin, bounds["min_y"] - margin,
                         width, height, "footprint_extents")


def _normalize_ref(ref: str) -> str:
    return str(ref).strip()


def _require_board_size(model: PlacementModel, rule_name: str) -> Tuple[float, float, float, float]:
    if model.board.width is None or model.board.height is None:
        raise PlacementError(f"Board(width=..., height=...) must be declared before {rule_name}(...)")
    ox, oy = model.board.origin
    return model.board.width, model.board.height, ox, oy


def _rot_or_none(rot: Optional[Number | str]) -> Optional[Number | str]:
    return None if rot is None else rot


def footprint_bounds(footprints: Mapping[str, Footprint]) -> Dict[str, float]:
    if not footprints:
        return {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0}
    xs = [fp.x for fp in footprints.values()]
    ys = [fp.y for fp in footprints.values()]
    return {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)}


def infer_origin_from_footprints(footprints: Mapping[str, Footprint]) -> Point:
    bounds = footprint_bounds(footprints)
    return bounds["min_x"], bounds["min_y"]


def parentheses_balanced(text: str) -> bool:
    depth = 0
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_str


def normalize_alias_path(value: Any) -> str:
    """Normalize a Zener/pcb semantic instance path into a DSL alias.

    Normalization is intentionally conservative: it strips whitespace, removes
    leading separators, converts slash-separated hierarchy to dot-separated
    hierarchy, and collapses duplicate dots.  The result is suitable for
    matching placement DSL references such as ``MCU.U_MCU``.
    """

    text = str(value).strip()
    text = re.sub(r"\s+", "", text)
    text = text.strip("/.")
    text = text.replace("/", ".")
    text = re.sub(r"\.+", ".", text)
    return text


def _looks_like_ref(value: Any) -> bool:
    text = str(value).strip()
    return bool(re.fullmatch(r"[A-Za-z]+[A-Za-z0-9_.:-]*\d+[A-Za-z0-9_.:-]*", text))


# ---------------------------------------------------------------------------
# Netlist alias parsing
# ---------------------------------------------------------------------------


_PATH_KEYS = {"path", "instance", "hierarchical_path", "hierarchicalPath", "instance_path", "instancePath"}
_REF_KEYS = {"ref", "reference", "designator"}


def _add_imported_alias(aliases: Dict[str, str], conflicts: Dict[str, Set[str]], semantic: Any, ref: Any) -> None:
    alias = normalize_alias_path(semantic)
    raw_ref = _normalize_ref(str(ref))
    if not alias or not raw_ref or alias == raw_ref:
        return
    if alias in aliases and aliases[alias] != raw_ref:
        conflicts.setdefault(alias, {aliases[alias]}).add(raw_ref)
        return
    aliases[alias] = raw_ref


def _extract_aliases_from_obj(obj: Any, aliases: Dict[str, str], conflicts: Dict[str, Set[str]]) -> None:
    """Recursively find likely semantic-path/reference pairs in JSON-like data."""

    if isinstance(obj, dict):
        found_ref: Optional[Any] = None
        found_path: Optional[Any] = None
        lower_to_key = {str(k).lower(): k for k in obj}
        for key in _REF_KEYS:
            actual = lower_to_key.get(key.lower())
            if actual is not None and _looks_like_ref(obj[actual]):
                found_ref = obj[actual]
                break
        for key in _PATH_KEYS:
            actual = lower_to_key.get(key.lower())
            if actual is not None:
                candidate = normalize_alias_path(obj[actual])
                if candidate:
                    found_path = obj[actual]
                    break
        if found_path is not None and found_ref is not None:
            _add_imported_alias(aliases, conflicts, found_path, found_ref)
        # Some artifacts use arbitrary component IDs whose nested value contains
        # either a reference or a path.  Recurse regardless of whether this level
        # yielded a pair.
        for value in obj.values():
            _extract_aliases_from_obj(value, aliases, conflicts)
    elif isinstance(obj, list):
        for item in obj:
            _extract_aliases_from_obj(item, aliases, conflicts)


def _parse_json_netlist(text: str) -> Tuple[Dict[str, str], List[str]]:
    data = json.loads(text)
    aliases: Dict[str, str] = {}
    conflicts: Dict[str, Set[str]] = {}
    _extract_aliases_from_obj(data, aliases, conflicts)
    errors = [f"netlist alias {alias!r} maps to multiple refs: {', '.join(sorted(refs))}"
              for alias, refs in sorted(conflicts.items())]
    return aliases, errors


def _parse_xml_netlist(text: str) -> Tuple[Dict[str, str], List[str]]:
    root = ET.fromstring(text)
    aliases: Dict[str, str] = {}
    conflicts: Dict[str, Set[str]] = {}
    for elem in root.iter():
        values: Dict[str, str] = {k: v for k, v in elem.attrib.items()}
        for child in list(elem):
            tag = child.tag.split("}", 1)[-1]
            if child.text and child.text.strip():
                values.setdefault(tag, child.text.strip())
        ref = None
        semantic = None
        for key, value in values.items():
            if key.lower() in {k.lower() for k in _REF_KEYS} and _looks_like_ref(value):
                ref = value
                break
        for key, value in values.items():
            if key.lower() in {k.lower() for k in _PATH_KEYS}:
                semantic = value
                break
        # KiCad XML commonly has <comp ref="U1"><property name="path" value="/MCU/U_MCU"/></comp>.
        if elem.tag.split("}", 1)[-1] == "comp" and ref is None:
            ref = elem.attrib.get("ref")
        if semantic is None:
            for child in elem:
                tag = child.tag.split("}", 1)[-1].lower()
                name = (child.attrib.get("name") or child.attrib.get("key") or "").lower()
                if tag in {"property", "field"} and name in {k.lower() for k in _PATH_KEYS}:
                    semantic = child.attrib.get("value") or child.text
                    break
        if semantic is not None and ref is not None:
            _add_imported_alias(aliases, conflicts, semantic, ref)
    errors = [f"netlist alias {alias!r} maps to multiple refs: {', '.join(sorted(refs))}"
              for alias, refs in sorted(conflicts.items())]
    return aliases, errors


def _sexp_tokens(text: str) -> List[str]:
    return re.findall(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()]+', text)


def _parse_sexp(tokens: List[str]) -> Any:
    def parse_at(index: int) -> Tuple[Any, int]:
        if index >= len(tokens):
            raise ValueError("unexpected end of S-expression")
        token = tokens[index]
        if token == "(":
            result = []
            index += 1
            while index < len(tokens) and tokens[index] != ")":
                item, index = parse_at(index)
                result.append(item)
            if index >= len(tokens):
                raise ValueError("unbalanced S-expression")
            return result, index + 1
        if token == ")":
            raise ValueError("unexpected ')' in S-expression")
        if token.startswith('"') and token.endswith('"'):
            return bytes(token[1:-1], "utf-8").decode("unicode_escape"), index + 1
        return token, index + 1

    parsed, next_index = parse_at(0)
    if next_index != len(tokens):
        # Multiple top-level forms: keep them all under one synthetic root.
        items = [parsed]
        while next_index < len(tokens):
            item, next_index = parse_at(next_index)
            items.append(item)
        return items
    return parsed


def _walk_sexp(node: Any) -> Iterable[List[Any]]:
    if isinstance(node, list):
        yield node
        for item in node:
            yield from _walk_sexp(item)


def _sexp_child_atom(form: List[Any], key: str) -> Optional[str]:
    """Return the first scalar child value with the requested S-expression key."""

    key = key.lower()
    for item in form[1:]:
        if (
            isinstance(item, list)
            and len(item) >= 2
            and isinstance(item[0], str)
            and item[0].lower() == key
            and not isinstance(item[1], list)
        ):
            return str(item[1])
    return None


def _parse_sexp_netlist(text: str) -> Tuple[Dict[str, str], List[str]]:
    tree = _parse_sexp(_sexp_tokens(text))
    aliases: Dict[str, str] = {}
    conflicts: Dict[str, Set[str]] = {}
    path_names = {k.lower() for k in _PATH_KEYS}
    ref_names = {k.lower() for k in _REF_KEYS}
    for form in _walk_sexp(tree):
        head = str(form[0]).lower() if form and isinstance(form[0], str) else ""
        values: Dict[str, str] = {}
        for item in form[1:]:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str):
                key = item[0].lower()
                if key in path_names | ref_names and not isinstance(item[1], list):
                    values[key] = str(item[1])
                # KiCad netlist exports store hierarchical component names as
                # (comp (ref "C1") ... (sheetpath (names "FLASH.C") ...)).
                # Treat that sheet path as the semantic alias for the comp ref.
                elif head in {"component", "comp"} and key == "sheetpath":
                    names = _sexp_child_atom(item, "names")
                    if names:
                        values.setdefault("path", names)
        ref = next((values[k] for k in ref_names if k in values and _looks_like_ref(values[k])), None)
        semantic = next((values[k] for k in path_names if k in values), None)
        if ref is not None and semantic is not None:
            _add_imported_alias(aliases, conflicts, semantic, ref)
    errors = [f"netlist alias {alias!r} maps to multiple refs: {', '.join(sorted(refs))}"
              for alias, refs in sorted(conflicts.items())]
    return aliases, errors


def parse_netlist_aliases(path: Path) -> Tuple[Dict[str, str], AliasDiagnostics]:
    """Parse a Zener/pcb netlist artifact and return semantic aliases.

    The parser is deliberately dependency-free and defensive.  It first tries
    JSON, then KiCad-style XML, then an S-expression-like fallback.  Only clear
    ``semantic_path -> raw_ref`` pairs are imported; raw-reference identity
    aliases are added later by the resolver so existing placement files remain
    unchanged.
    """

    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    attempts: List[Tuple[str, Any]] = []
    if stripped.startswith("{") or stripped.startswith("["):
        attempts.append(("json", _parse_json_netlist))
    if stripped.startswith("<"):
        attempts.append(("xml", _parse_xml_netlist))
    if stripped.startswith("("):
        attempts.append(("sexp", _parse_sexp_netlist))
    for name, func in (("json", _parse_json_netlist), ("xml", _parse_xml_netlist), ("sexp", _parse_sexp_netlist)):
        if name not in {attempt[0] for attempt in attempts}:
            attempts.append((name, func))

    failures: List[str] = []
    for name, func in attempts:
        try:
            aliases, errors = func(text)
        except Exception as exc:  # defensive format probing
            failures.append(f"{name}: {exc}")
            continue
        diagnostics = AliasDiagnostics(source=str(path), parser=name, errors=errors)
        if aliases or errors:
            return aliases, diagnostics
    return {}, AliasDiagnostics(source=str(path), parser=None,
                                warnings=["no semantic aliases found in netlist; tried " + "; ".join(failures)])


def import_netlist_aliases(model: PlacementModel, path: Path) -> None:
    aliases, diagnostics = parse_netlist_aliases(path)
    model.imported_aliases = aliases
    model.alias_diagnostics = diagnostics


# ---------------------------------------------------------------------------
# Placement DSL loader
# ---------------------------------------------------------------------------


def load_ppl(path: Path) -> PlacementModel:
    """Load a placement.ppl file into a PlacementModel.

    The DSL intentionally looks like Zener/Starlark-style function calls but is
    implemented as a restricted Python namespace. This keeps the first versions
    easy to review, test, and extend.
    """

    model = PlacementModel()

    def Board(*, width: Number, height: Number, units: str = "mm", origin: Point = (0, 0),
              origin_x: Optional[Number] = None, origin_y: Optional[Number] = None,
              name: Optional[str] = None, emit_outline: bool = False, **kwargs: Any) -> None:
        if units != "mm":
            raise PlacementError("Only units='mm' is currently supported")
        if kwargs:
            raise PlacementError(f"Board() unknown parameter(s): {', '.join(sorted(kwargs))}")
        if float(width) <= 0 or float(height) <= 0:
            raise PlacementError(f"invalid board geometry: width={_fmt_num(float(width))} "
                                 f"height={_fmt_num(float(height))} source=placement_file "
                                 "(Board(width=..., height=...) dimensions must be positive)")
        if origin_x is not None or origin_y is not None:
            base_x, base_y = _as_point(origin, field="origin")
            origin = (base_x if origin_x is None else float(origin_x),
                      base_y if origin_y is None else float(origin_y))
        model.board = BoardInfo(
            width=float(width),
            height=float(height),
            units=units,
            origin=_as_point(origin, field="origin"),
            name=name,
            emit_outline=bool(emit_outline),
        )

    def Alias(name: str, ref: str) -> None:
        model.alias(str(name), str(ref))

    def Region(name: str, *, x: Number, y: Number, w: Number, h: Number,
               role: Optional[str] = None, note: Optional[str] = None,
               priority: Optional[Number] = None, kind: Optional[str] = None,
               movable: Optional[bool] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Region() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.region(str(name), x=x, y=y, w=w, h=h, role=role, note=note, priority=priority,
                     kind=kind, movable=movable)

    def Spacing(*, default: Number = 0.25, passive_to_passive: Number = 0.25,
                passive_to_ic: Number = 0.40, ic_to_ic: Number = 0.75,
                connector: Number = 1.00, mechanical: Number = 1.00,
                connector_to_component: Optional[Number] = None,
                mechanical_to_component: Optional[Number] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Spacing() unknown parameter(s): {', '.join(sorted(kwargs))}")
        if connector_to_component is not None:
            connector = connector_to_component
        if mechanical_to_component is not None:
            mechanical = mechanical_to_component
        model.clearance = ClearanceRules(float(default), float(passive_to_passive), float(passive_to_ic),
                                         float(ic_to_ic), float(connector), float(mechanical))

    def AvoidOverlap(enabled: bool = True, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"AvoidOverlap() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.policy.avoid_overlap = bool(enabled)

    def PlacementPolicyDsl(*, avoid_overlap: bool = True, allow_anchor_move: bool = False,
                           max_search_radius: Number = 5, search_step: Number = 0.5, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"PlacementPolicy() unknown parameter(s): {', '.join(sorted(kwargs))}")
        if float(max_search_radius) < 0 or float(search_step) <= 0:
            raise PlacementError("PlacementPolicy(max_search_radius=..., search_step=...) must be positive")
        model.policy = PlacementPolicy(bool(avoid_overlap), bool(allow_anchor_move),
                                       float(max_search_radius), float(search_step))

    def PartClass(ref: str, cls: str, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"PartClass() unknown parameter(s): {', '.join(sorted(kwargs))}")
        normalized = str(cls).lower().replace("-", "_")
        aliases = {"passives": "passive", "ics": "ic", "mounting_hole": "mechanical", "mounting_holes": "mechanical"}
        normalized = aliases.get(normalized, normalized)
        allowed = {"passive", "ic", "connector", "mechanical", "testpoint", "switch", "default"}
        if normalized not in allowed:
            raise PlacementError(f"PartClass({ref!r}, {cls!r}) has unsupported class")
        model.part_classes[_normalize_ref(ref)] = normalized

    def Lock(ref: str, *, reason: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Lock() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.add("lock", ref=_normalize_ref(ref), note=note or reason)

    def Priority(ref: str, value: Number, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Priority() unknown parameter(s): {', '.join(sorted(kwargs))}")
        normalized = _normalize_ref(ref)
        model.priority_refs[normalized] = float(value)
        model.add("priority", ref=normalized, value=float(value))

    def Soft(ref: str, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Soft() unknown parameter(s): {', '.join(sorted(kwargs))}")
        normalized = _normalize_ref(ref)
        model.soft_refs.add(normalized)
        model.add("soft", ref=normalized)

    def Anchor(ref: Optional[str] = None, *, x: Optional[Number] = None, y: Optional[Number] = None,
               at: Optional[Point] = None, rot: Optional[Number | str] = None,
               role: Optional[str] = None, side: str = "F", lock: bool = False,
               relative_to: Optional[str] = None, dx: Number = 0, dy: Number = 0,
               region: Optional[str] = None, note: Optional[str] = None,
               comment: Optional[str] = None, **kwargs: Any) -> None:
        if at is not None:
            x, y = _as_point(at, field="at")
        if region is not None and region not in model.regions:
            raise PlacementError(f"Anchor({ref!r}) references unknown region {region!r}")
        if x is None and y is None and region is not None and relative_to is None:
            r = model.regions[region]
            x = r["x"] + r["w"] / 2.0
            y = r["y"] + r["h"] / 2.0
        if relative_to is None and (x is None or y is None):
            raise PlacementError(f"Anchor({ref!r}) requires x/y, at=(x,y), relative_to=..., or region=...")
        rule = dict(
            type="anchor",
            ref=None if ref is None else _normalize_ref(ref),
            x=None if x is None else float(x),
            y=None if y is None else float(y),
            relative_to=None if relative_to is None else _normalize_ref(relative_to),
            dx=float(dx),
            dy=float(dy),
            region=region,
            rot=_rot_or_none(rot),
            role=role,
            side=side,
            lock=bool(lock),
            note=note or comment,
            extra=dict(kwargs),
        )
        if ref is None:
            return rule
        model.rules.append(rule)
        return None

    def Fixed(ref: str, **kwargs: Any) -> None:
        kwargs.setdefault("lock", True)
        Anchor(ref, **kwargs)
        model.rules[-1]["type"] = "fixed"

    def Corner(ref: Optional[str] = None, *, corner: str, inset: Number | Point = 3,
               rot: Optional[Number | str] = None, role: Optional[str] = "mounting_hole",
               lock: bool = True, note: Optional[str] = None, **kwargs: Any) -> None:
        w, h, _ox, _oy = _require_board_size(model, "Corner")
        if isinstance(inset, (tuple, list)):
            ix, iy = _as_point(inset, field="inset")
        else:
            ix = iy = float(inset)
        c = corner.lower().replace("-", "_")
        aliases = {
            "tl": "top_left", "tr": "top_right", "bl": "bottom_left", "br": "bottom_right",
            "upper_left": "top_left", "upper_right": "top_right",
            "lower_left": "bottom_left", "lower_right": "bottom_right",
        }
        c = aliases.get(c, c)
        if c == "top_left":
            x, y = ix, iy
        elif c == "top_right":
            x, y = w - ix, iy
        elif c == "bottom_left":
            x, y = ix, h - iy
        elif c == "bottom_right":
            x, y = w - ix, h - iy
        else:
            raise PlacementError(f"Unknown corner {corner!r}")
        rule = dict(type="corner", ref=None if ref is None else _normalize_ref(ref), x=x, y=y, rot=_rot_or_none(rot),
                    role=role, lock=bool(lock), corner=c, note=note, extra=dict(kwargs))
        if ref is None:
            return rule
        model.rules.append(rule)
        return None

    def Edge(ref: Optional[str] = None, *, edge: str, offset: Optional[Number] = None, inset: Number = 0,
             x: Optional[Number] = None, y: Optional[Number] = None,
             rot: Optional[Number | str] = None, role: Optional[str] = None,
             lock: bool = False, locked: bool = False, edge_required: bool = False,
             mechanical: bool = False, access_side: Optional[str] = None,
             allow_body_outside_board: bool = False,
             note: Optional[str] = None, **kwargs: Any) -> None:
        w, h, _ox, _oy = _require_board_size(model, "Edge")
        e = edge.lower().replace("-", "_")
        if e in ("left", "right"):
            px = float(inset) if e == "left" else w - float(inset)
            py = float(y) if y is not None else float(offset if offset is not None else h / 2.0)
        elif e in ("top", "bottom"):
            if x is not None:
                px = float(x)
            elif offset is not None:
                px = float(offset)
            elif y is not None:
                px = float(y)
            else:
                px = w / 2.0
            py = float(inset) if e == "top" else h - float(inset)
        else:
            raise PlacementError(f"Unknown edge {edge!r}")
        access = access_side.lower().replace("-", "_") if access_side is not None else e
        must_lock = bool(lock) or bool(locked) or bool(edge_required)
        rule = dict(type="edge", ref=None if ref is None else _normalize_ref(ref), x=px, y=py,
                    rot=_rot_or_none(rot), role=role,
                    lock=must_lock, locked=must_lock, edge_required=bool(edge_required),
                    mechanical=bool(mechanical), access_side=access,
                    allow_body_outside_board=bool(allow_body_outside_board),
                    edge=e, note=note, extra=dict(kwargs))
        if ref is None:
            return rule
        model.rules.append(rule)
        return None

    def Between(ref: Optional[str] = None, *, a: str, b: str, t: Number = 0.5,
                dx: Number = 0, dy: Number = 0, offset: Optional[Number] = None,
                rot: Optional[Number | str] = None, align: Optional[str] = None,
                auto_spread: bool = False,
                role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if align is not None and rot is None:
            rot = align
        rule = dict(type="between", ref=None if ref is None else _normalize_ref(ref), a=_normalize_ref(a), b=_normalize_ref(b),
                    t=float(t), dx=float(dx), dy=float(dy),
                    offset=None if offset is None else float(offset), rot=_rot_or_none(rot),
                    auto_spread=bool(auto_spread),
                    role=role, note=note, extra=dict(kwargs))
        if ref is None:
            return rule
        model.rules.append(rule)
        return None

    def Inline(ref: Optional[str] = None, *, a: str, b: str, t: Number = 0.5,
               rot: Optional[Number | str] = None, align: Optional[str] = None,
               role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if align is not None and rot is None:
            rot = align
        rule = dict(type="inline", ref=None if ref is None else _normalize_ref(ref), a=_normalize_ref(a), b=_normalize_ref(b),
                    t=float(t), rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))
        if ref is None:
            return rule
        model.rules.append(rule)
        return None

    def Satellite(ref: str, *, parent: str, side: str = "right", distance: Number = 2,
                  index: int = 0, pitch: Number = 1.5, dx: Number = 0, dy: Number = 0,
                  rot: Optional[Number | str] = None, role: Optional[str] = None,
                  note: Optional[str] = None, **kwargs: Any) -> None:
        model.add("satellite", ref=_normalize_ref(ref), parent=_normalize_ref(parent),
                  side=side.lower().replace("-", "_"), distance=float(distance),
                  index=int(index), pitch=float(pitch), dx=float(dx), dy=float(dy),
                  rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

    def NearPad(ref: str, *, parent: str, pad: str | Sequence[str], distance: Number = 1.5,
                side: str = "auto", clearance: Optional[Number] = None, role: Optional[str] = None,
                priority: Optional[Number] = None, rot: Optional[Number | str] = None,
                note: Optional[str] = None, **kwargs: Any) -> None:
        model.add("near_pad", ref=_normalize_ref(ref), parent=_normalize_ref(parent), pad=pad,
                  distance=float(distance), side=side.lower().replace("-", "_"),
                  clearance=None if clearance is None else float(clearance), role=role,
                  priority=None if priority is None else float(priority), rot=_rot_or_none(rot),
                  note=note, extra=dict(kwargs))

    def Decoupling(ref: str, *, parent: str, power_net: Optional[str] = None, ground_net: Optional[str] = None,
                   pad: Optional[str | Sequence[str]] = None, distance: Number = 1.5,
                   role: str = "decoupling", priority: Number = 90, **kwargs: Any) -> None:
        kwargs.setdefault("power_net", power_net)
        kwargs.setdefault("ground_net", ground_net)
        if pad is not None:
            NearPad(ref, parent=parent, pad=pad, distance=distance, role=role, priority=priority, **kwargs)
        else:
            Satellite(ref, parent=parent, side="auto", distance=distance, role=role, priority=priority, **kwargs)

    def Pullup(ref: str, *, parent: str, net: Optional[str] = None, pad: Optional[str | Sequence[str]] = None,
               distance: Number = 3.0, role: str = "pullup", priority: Number = 60, **kwargs: Any) -> None:
        kwargs.setdefault("net", net)
        if pad is not None:
            NearPad(ref, parent=parent, pad=pad, distance=distance, role=role, priority=priority, **kwargs)
        else:
            Satellite(ref, parent=parent, side="auto", distance=distance, role=role, priority=priority, **kwargs)

    def DecouplingArray(refs: Sequence[str], *, parent: str, pad: Optional[str | Sequence[str]] = None,
                        side: str = "auto", distance: Number = 2.0, spacing: Number = 1.5,
                        stagger: bool = True, rows: int | str = "auto", max_per_row: Optional[int] = None,
                        role: str = "decoupling", priority: Number = 90,
                        power_net: Optional[str] = None, ground_net: Optional[str] = None,
                        rot: Optional[Number | str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        """Place a group of decoupling capacitors outside the parent footprint body,
        near `pad` when supplied, spreading and staggering the array as needed."""

        refs = list(refs)
        if not refs:
            return
        kwargs.setdefault("power_net", power_net)
        kwargs.setdefault("ground_net", ground_net)
        model.add("decoupling_array", refs=[_normalize_ref(r) for r in refs], parent=_normalize_ref(parent),
                  pad=pad, side=side.lower().replace("-", "_"), distance=float(distance),
                  spacing=float(spacing), stagger=bool(stagger), rows=rows, max_per_row=max_per_row,
                  role=role, priority=float(priority),
                  rot=_rot_or_none(rot), note=note, extra=dict(kwargs))

    def PullupArray(refs: Sequence[str], *, parent: str, nets: Optional[Sequence[str]] = None,
                    pad: Optional[str | Sequence[str]] = None, side: str = "auto",
                    distance: Number = 4.0, spacing: Number = 2.0, role: str = "pullup",
                    priority: Number = 60, rot: Optional[Number | str] = None,
                    note: Optional[str] = None, **kwargs: Any) -> None:
        """Place a group of pull-up/pull-down resistors (or similar support parts) as a
        single row near `parent` (or near `pad` on `parent` if given), spread by `spacing`
        along the chosen `side`. The whole group is kept together when possible; if it does
        not fit, members fall back to individual search-based placement."""

        refs = list(refs)
        if not refs:
            return
        kwargs.setdefault("nets", list(nets) if nets is not None else None)
        model.add("pullup_array", refs=[_normalize_ref(r) for r in refs], parent=_normalize_ref(parent),
                  pad=pad, side=side.lower().replace("-", "_"), distance=float(distance),
                  spacing=float(spacing), role=role, priority=float(priority),
                  rot=_rot_or_none(rot), note=note, extra=dict(kwargs))

    def Series(ref: str, *, a: str, b: str, t: Number = 0.5, offset: Number = 0,
               clearance: Optional[Number] = None, role: str = "series", priority: Number = 70,
               **kwargs: Any) -> None:
        Between(ref, a=a, b=b, t=t, offset=offset, role=role, priority=priority, clearance=clearance, **kwargs)

    def ESD(ref: str, *, connector: str, protected: str, t: Number = 0.2, offset: Number = 0,
            clearance: Optional[Number] = None, role: str = "esd", priority: Number = 80,
            **kwargs: Any) -> None:
        Between(ref, a=connector, b=protected, t=t, dy=offset, role=role, priority=priority, clearance=clearance, **kwargs)

    def Orbit(*, refs: Optional[Sequence[str]] = None, parent: str, radius: Number = 3,
              start_angle: Number = 0, step_angle: Optional[Number] = None,
              index: int = 0, angle: Optional[Number] = None,
              rot: Optional[Number | str] = None, role: Optional[str] = None,
              note: Optional[str] = None, **kwargs: Any) -> None:
        """Distribute support parts around an anchor in polar coordinates."""
        if refs is None:
            return dict(type="orbit", refs=None, parent=_normalize_ref(parent), radius=float(radius),
                        start_angle=float(start_angle if angle is None else angle), step_angle=0.0 if step_angle is None else float(step_angle),
                        index=int(index), rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))
        refs = list(refs)
        if not refs:
            return
        if step_angle is None:
            step_angle = 360.0 / len(refs)
        model.add("orbit", refs=[_normalize_ref(r) for r in refs], parent=_normalize_ref(parent),
                  radius=float(radius), start_angle=float(start_angle), step_angle=float(step_angle),
                  rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

    def Cluster(name: str, *, anchor: str, members: Sequence[str], placement: Mapping[str, Any],
                ignore_missing: bool = False, role: Optional[str] = None,
                note: Optional[str] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Cluster() unknown parameter(s): {', '.join(sorted(kwargs))}")
        if not name:
            raise PlacementError("Cluster(name, ...) requires a non-empty name")
        if not isinstance(placement, Mapping) or not placement.get("type"):
            raise PlacementError("Cluster(placement=...) must be an unbound placement primitive such as Anchor(...)")
        supported = {"anchor", "fixed", "edge", "corner", "between", "inline", "orbit"}
        if placement["type"] not in supported:
            raise PlacementError(f"Cluster({name!r}) does not support placement={placement['type']!r}")
        raw_members = [_normalize_ref(m) for m in members]
        anchor_ref = _normalize_ref(anchor)
        if anchor_ref not in raw_members:
            raw_members.insert(0, anchor_ref)
        seen: Set[str] = set()
        duplicate_members: List[str] = []
        unique_members: List[str] = []
        for member in raw_members:
            if member in seen:
                duplicate_members.append(member)
                continue
            seen.add(member)
            unique_members.append(member)
        model.add("cluster", name=str(name), anchor=anchor_ref, members=unique_members,
                  duplicate_members=duplicate_members, placement=dict(placement),
                  ignore_missing=bool(ignore_missing), role=role, note=note)

    def Array(*, refs: Iterable[str], start: Point, pitch: Point = (2, 0),
              rot: Optional[Number | str] = None, role: Optional[str] = None,
              note: Optional[str] = None, **kwargs: Any) -> None:
        model.add("array", refs=[_normalize_ref(r) for r in refs], start=_as_point(start, field="start"),
                  pitch=_as_point(pitch, field="pitch"), rot=_rot_or_none(rot),
                  role=role, note=note, extra=dict(kwargs))

    def Row(refs: Sequence[str], *, start: Point, pitch: Number = 2,
            rot: Optional[Number | str] = None, role: Optional[str] = None,
            note: Optional[str] = None, **kwargs: Any) -> None:
        Array(refs=refs, start=start, pitch=(float(pitch), 0.0), rot=rot, role=role, note=note, **kwargs)
        model.rules[-1]["type"] = "row"

    def Column(refs: Sequence[str], *, start: Point, pitch: Number = 2,
               rot: Optional[Number | str] = None, role: Optional[str] = None,
               note: Optional[str] = None, **kwargs: Any) -> None:
        Array(refs=refs, start=start, pitch=(0.0, float(pitch)), rot=rot, role=role, note=note, **kwargs)
        model.rules[-1]["type"] = "column"

    def Grid(*, refs: Sequence[str], start: Point, columns: int, pitch: Point = (2, 2),
             rot: Optional[Number | str] = None, role: Optional[str] = None,
             note: Optional[str] = None, **kwargs: Any) -> None:
        if columns <= 0:
            raise PlacementError("Grid(columns=...) must be positive")
        model.add("grid", refs=[_normalize_ref(r) for r in refs], start=_as_point(start, field="start"),
                  columns=int(columns), pitch=_as_point(pitch, field="pitch"),
                  rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

    def Mirror(ref: str, *, source: str, axis: str = "vertical", about: Optional[Number] = None,
               rot: Optional[Number | str] = None, role: Optional[str] = None,
               note: Optional[str] = None, **kwargs: Any) -> None:
        """Place ref as a mirror of source across a board or explicit axis."""
        model.add("mirror", ref=_normalize_ref(ref), source=_normalize_ref(source), axis=axis,
                  about=None if about is None else float(about), rot=_rot_or_none(rot),
                  role=role, note=note, extra=dict(kwargs))

    def CopyPlacement(*, source_prefix: str, target_prefix: str, dx: Number = 0, dy: Number = 0,
                      refs: Optional[Sequence[str]] = None, rot: Optional[Number | str] = None,
                      role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        """Copy all or selected placements from one hierarchical prefix to another."""
        model.add("copy_placement", source_prefix=str(source_prefix), target_prefix=str(target_prefix),
                  dx=float(dx), dy=float(dy), refs=None if refs is None else [_normalize_ref(r) for r in refs],
                  rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

    def Keepout(name: str, *, x: Number, y: Number, w: Number, h: Number,
                layers: str = "all", role: Optional[str] = None, note: Optional[str] = None,
                emit: bool = False,
                **kwargs: Any) -> None:
        model.add("keepout", name=str(name), x=float(x), y=float(y), w=float(w), h=float(h),
                  layers=str(layers), role=role, note=note, emit=bool(emit), extra=dict(kwargs))

    def Corridor(name: str, *, a: str, b: str, width: Number,
                 clearance: Number = 0, role: Optional[str] = None,
                 note: Optional[str] = None, **kwargs: Any) -> None:
        model.add("corridor", name=str(name), a=_normalize_ref(a), b=_normalize_ref(b),
                  width=float(width), clearance=float(clearance), role=role, note=note, extra=dict(kwargs))

    def HighSpeedPath(name: str, *, sequence: Sequence[str], corridor_width: Number = 3.0,
                      protect_first: bool = True, role: str = "high_speed",
                      note: Optional[str] = None, **kwargs: Any) -> None:
        """Declare a high-speed signal path (connector -> protection -> IC).

        Metadata only: it moves nothing by itself, but the path is scored in the
        high-speed placement review (directness, ESD position, corridor
        obstruction) and its corridor contributes soft scoring penalties.
        """
        seq = [_normalize_ref(r) for r in sequence]
        if len(seq) < 2:
            raise PlacementError(f"HighSpeedPath({name!r}) requires at least two sequence members")
        model.add("high_speed_path", name=str(name), sequence=seq, corridor_width=float(corridor_width),
                  protect_first=bool(protect_first), role=role, note=note, extra=dict(kwargs))

    def PowerIsland(name: str, *, regulator: str, input_caps: Sequence[str] = (),
                    inductor: Optional[str] = None, output_caps: Sequence[str] = (),
                    feedback: Sequence[str] = (), switch_net: Optional[str] = None,
                    role: str = "power_island", note: Optional[str] = None, **kwargs: Any) -> None:
        """Declare a regulator power-island topology.

        Metadata only: members are placed by their own rules; the island is
        scored in the power placement review (hot-loop area, cap/inductor
        compactness, feedback proximity, separation from high-speed paths).
        """
        model.add("power_island", name=str(name), regulator=_normalize_ref(regulator),
                  input_caps=[_normalize_ref(r) for r in input_caps],
                  inductor=None if inductor is None else _normalize_ref(inductor),
                  output_caps=[_normalize_ref(r) for r in output_caps],
                  feedback=[_normalize_ref(r) for r in feedback],
                  switch_net=switch_net, role=role, note=note, extra=dict(kwargs))

    class ComponentBuilder:
        """Fluent API for users who prefer Component("U1").anchor(...).

        The simple top-level DSL remains the primary API.  ComponentBuilder is
        intentionally thin: every method delegates to the same rule functions so
        both styles produce identical PlacementModel entries.
        """
        def __init__(self, ref: str) -> None:
            self.ref = _normalize_ref(ref)

        def anchor(self, **kwargs: Any) -> "ComponentBuilder":
            Anchor(self.ref, **kwargs)
            return self

        def fixed(self, **kwargs: Any) -> "ComponentBuilder":
            Fixed(self.ref, **kwargs)
            return self

        def satellite(self, *, parent: str, **kwargs: Any) -> "ComponentBuilder":
            Satellite(self.ref, parent=parent, **kwargs)
            return self

        def between(self, *, a: str, b: str, **kwargs: Any) -> "ComponentBuilder":
            Between(self.ref, a=a, b=b, **kwargs)
            return self

        def inline(self, *, a: str, b: str, **kwargs: Any) -> "ComponentBuilder":
            Inline(self.ref, a=a, b=b, **kwargs)
            return self

        def edge(self, *, edge: str, **kwargs: Any) -> "ComponentBuilder":
            Edge(self.ref, edge=edge, **kwargs)
            return self

        def corner(self, *, corner: str, **kwargs: Any) -> "ComponentBuilder":
            Corner(self.ref, corner=corner, **kwargs)
            return self

        def lock(self, *, reason: Optional[str] = None, note: Optional[str] = None) -> "ComponentBuilder":
            Lock(self.ref, reason=reason, note=note)
            return self

    def Component(ref: str) -> ComponentBuilder:
        return ComponentBuilder(ref)

    namespace: Dict[str, Any] = {
        "Board": Board,
        "Alias": Alias,
        "Region": Region,
        "Spacing": Spacing,
        "ClearanceRules": Spacing,
        "AvoidOverlap": AvoidOverlap,
        "PlacementPolicy": PlacementPolicyDsl,
        "PartClass": PartClass,
        "Lock": Lock,
        "Priority": Priority,
        "Soft": Soft,
        "Component": Component,
        "Anchor": Anchor,
        "Fixed": Fixed,
        "Corner": Corner,
        "Edge": Edge,
        "Between": Between,
        "Inline": Inline,
        "Satellite": Satellite,
        "NearPad": NearPad,
        "Decoupling": Decoupling,
        "DecouplingArray": DecouplingArray,
        "Pullup": Pullup,
        "PullupArray": PullupArray,
        "Series": Series,
        "ESD": ESD,
        "Orbit": Orbit,
        "Cluster": Cluster,
        "Array": Array,
        "Row": Row,
        "Column": Column,
        "Grid": Grid,
        "Mirror": Mirror,
        "CopyPlacement": CopyPlacement,
        "Keepout": Keepout,
        "Corridor": Corridor,
        "HighSpeedPath": HighSpeedPath,
        "PowerIsland": PowerIsland,
        "mm": lambda v: float(v),
        "True": True,
        "False": False,
        "None": None,
        "__builtins__": {},
    }

    try:
        code = path.read_text(encoding="utf-8")
        exec(compile(code, str(path), "exec"), namespace, {})
    except PlacementError:
        raise
    except Exception as exc:
        raise PlacementError(f"Failed loading {path}: {exc}") from exc

    return model


# ---------------------------------------------------------------------------
# KiCad S-expression parsing and rewriting
# ---------------------------------------------------------------------------


def _find_matching_paren(text: str, open_idx: int) -> int:
    """Return the index just after the matching ')' for text[open_idx] == '('."""

    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "(":
        raise PlacementError(f"Expected '(' at offset {open_idx}")
    depth = 0
    in_str = False
    escape = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise PlacementError(f"Unbalanced parentheses near offset {open_idx}")


def parse_footprints(text: str) -> Dict[str, Footprint]:
    """Extract footprints keyed by their Reference property."""

    footprints: Dict[str, Footprint] = {}
    for match in re.finditer(r'(?m)^\s*\(footprint\s+"', text):
        start = match.start()
        open_idx = text.find("(", start)
        end = _find_matching_paren(text, open_idx)
        block = text[start:end]
        ref_match = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
        at_match = re.search(r'(?m)^(\s*)\(at\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)(?:\s+([-+0-9.eE]+))?\)', block)
        layer_match = re.search(r'\(layer\s+"([^"]+)"\)', block)
        if not ref_match or not at_match:
            continue
        ref = ref_match.group(1)
        if ref in footprints:
            raise PlacementError(f"Duplicate footprint reference {ref!r} in PCB file")
        footprints[ref] = Footprint(
            ref=ref,
            start=start,
            end=end,
            text=block,
            x=float(at_match.group(2)),
            y=float(at_match.group(3)),
            rot=float(at_match.group(4)) if at_match.group(4) is not None else 0.0,
            at_has_rot=at_match.group(4) is not None,
            at_rot_text=at_match.group(4),
            layer=layer_match.group(1) if layer_match else None,
        )
    return footprints




def _edge_cuts_primitives(text: str) -> Tuple[List[Tuple[Point, Point]], List[Tuple[Point, Point]]]:
    """Return Edge.Cuts rectangles and line segments from supported KiCad drawings."""

    rects: List[Tuple[Point, Point]] = []
    lines: List[Tuple[Point, Point]] = []
    for match in re.finditer(r'(?m)^\s*\((gr_rect|gr_line)\b', text):
        block = text[match.start():_find_matching_paren(text, text.find("(", match.start()))]
        if '(layer "Edge.Cuts")' not in block:
            continue
        point_match = re.search(
            r'\(start\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\).*?\(end\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)',
            block,
            flags=re.S,
        )
        if not point_match:
            continue
        x0, y0, x1, y1 = map(float, point_match.groups())
        if match.group(1) == "gr_rect":
            rects.append(((x0, y0), (x1, y1)))
        else:
            lines.append(((x0, y0), (x1, y1)))
    return rects, lines


def _geometry_from_corners(a: Point, b: Point, *, source: str = "edge_cuts") -> BoardGeometry:
    origin_x, origin_y = min(a[0], b[0]), min(a[1], b[1])
    width, height = max(a[0], b[0]) - origin_x, max(a[1], b[1]) - origin_y
    if width <= 0 or height <= 0:
        raise PlacementError("Edge.Cuts geometry has non-positive width or height")
    return BoardGeometry(origin_x, origin_y, width, height, source)


def _line_rectangle_geometry(lines: List[Tuple[Point, Point]], *, eps: float = 1e-6) -> Optional[BoardGeometry]:
    """Return geometry only when four Edge.Cuts lines form one axis-aligned rectangle."""

    if not lines:
        return None
    if len(lines) != 4:
        raise PlacementError("Unsupported Edge.Cuts geometry: rectangular gr_line boards must have exactly four line segments")

    points = [point for line in lines for point in line]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 - x0 <= eps or y1 - y0 <= eps:
        raise PlacementError("Edge.Cuts geometry has non-positive width or height")

    def point_key(point: Point) -> Tuple[int, int]:
        return (round(point[0] / eps), round(point[1] / eps))

    def segment_key(a: Point, b: Point) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        ka, kb = point_key(a), point_key(b)
        return (ka, kb) if ka <= kb else (kb, ka)

    expected = {
        segment_key((x0, y0), (x1, y0)),
        segment_key((x1, y0), (x1, y1)),
        segment_key((x1, y1), (x0, y1)),
        segment_key((x0, y1), (x0, y0)),
    }
    actual: Set[Tuple[Tuple[int, int], Tuple[int, int]]] = set()
    for a, b in lines:
        horizontal = abs(a[1] - b[1]) <= eps and abs(a[0] - b[0]) > eps
        vertical = abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) > eps
        if not (horizontal or vertical):
            raise PlacementError("Unsupported Edge.Cuts geometry: only axis-aligned rectangular gr_line outlines are supported")
        actual.add(segment_key(a, b))
    if actual != expected:
        raise PlacementError("Unsupported Edge.Cuts geometry: gr_line segments do not form a single rectangle")
    return BoardGeometry(x0, y0, x1 - x0, y1 - y0, "edge_cuts")


def parse_edge_cuts_geometry(text: str) -> Optional[BoardGeometry]:
    rects, lines = _edge_cuts_primitives(text)
    if not rects and not lines:
        return None
    if rects:
        if len(rects) != 1 or lines:
            raise PlacementError("Unsupported Edge.Cuts geometry: expected one gr_rect or one four-line rectangle")
        return _geometry_from_corners(rects[0][0], rects[0][1])
    return _line_rectangle_geometry(lines)

def resolve_board_geometry(text: str, model: Optional[PlacementModel], footprints: Mapping[str, Footprint],
                           *, allow_footprint_fallback: bool = True) -> Optional[BoardGeometry]:
    edge = BoardGeometry.from_edge_cuts(text)
    if edge is not None:
        return edge
    if model is not None:
        defined = board_geometry_from_definition(model.board)
        if defined is not None:
            return defined
    return infer_geometry_from_footprints(footprints) if allow_footprint_fallback else None


def is_valid_uuid(text: str) -> bool:
    """Return True only for canonical UUIDv4 strings generated by pcb-place."""

    try:
        parsed = uuid.UUID(text)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 4 and str(parsed) == text


def _new_generated_uuid(object_name: str, generated_uuids: Optional[List[GeneratedUUID]] = None) -> str:
    uuid_text = str(uuid.uuid4())
    if not is_valid_uuid(uuid_text):
        raise PlacementError(f"generated {object_name} UUID is not a valid UUIDv4: {uuid_text!r}")
    if generated_uuids is not None:
        generated_uuids.append(GeneratedUUID(object_name, uuid_text))
    return uuid_text


def validate_generated_uuids(output_text: str, generated_uuids: Sequence[GeneratedUUID]) -> None:
    """Validate only UUIDs that pcb-place created during this write."""

    generated_seen: Set[str] = set()
    file_counts: Dict[str, int] = {}
    for uuid_text in re.findall(r'\(uuid\s+"([^"]+)"\)', output_text):
        file_counts[uuid_text] = file_counts.get(uuid_text, 0) + 1

    for item in generated_uuids:
        if not is_valid_uuid(item.uuid):
            raise PlacementError(f"generated {item.object} UUID is not a valid UUIDv4: {item.uuid!r}")
        if item.uuid in generated_seen:
            raise PlacementError(f"duplicate generated UUID: {item.uuid}")
        generated_seen.add(item.uuid)
        if file_counts.get(item.uuid, 0) != 1:
            raise PlacementError(f"generated UUID {item.uuid} is not unique within output file")


def _uuid_attr(object_name: str, generated_uuids: Optional[List[GeneratedUUID]] = None) -> str:
    uuid_text = _new_generated_uuid(object_name, generated_uuids)
    assert is_valid_uuid(uuid_text)
    return f'(uuid "{uuid_text}")'


def _outline_text(geometry: BoardGeometry, *, generated_uuids: Optional[List[GeneratedUUID]] = None) -> str:
    x0, y0, x1, y1 = geometry.min_x, geometry.min_y, geometry.max_x, geometry.max_y
    uuids = [_uuid_attr("gr_line", generated_uuids) for _ in range(4)]
    return (
        f'  (gr_line (start {_fmt_num(x0)} {_fmt_num(y0)}) (end {_fmt_num(x1)} {_fmt_num(y0)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") {uuids[0]})\n'
        f'  (gr_line (start {_fmt_num(x1)} {_fmt_num(y0)}) (end {_fmt_num(x1)} {_fmt_num(y1)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") {uuids[1]})\n'
        f'  (gr_line (start {_fmt_num(x1)} {_fmt_num(y1)}) (end {_fmt_num(x0)} {_fmt_num(y1)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") {uuids[2]})\n'
        f'  (gr_line (start {_fmt_num(x0)} {_fmt_num(y1)}) (end {_fmt_num(x0)} {_fmt_num(y0)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") {uuids[3]})\n'
    )


def emit_board_outline(text: str, geometry: BoardGeometry, *, generated_uuids: Optional[List[GeneratedUUID]] = None) -> str:
    existing = BoardGeometry.from_edge_cuts(text)
    if existing is not None:
        if not existing.nearly_equals(geometry):
            raise PlacementError(
                "Board emit_outline=True conflicts with existing Edge.Cuts geometry: "
                f"existing origin=({_fmt_num(existing.origin_x)}, {_fmt_num(existing.origin_y)}) "
                f"size={_fmt_num(existing.width)}x{_fmt_num(existing.height)}, "
                f"requested origin=({_fmt_num(geometry.origin_x)}, {_fmt_num(geometry.origin_y)}) "
                f"size={_fmt_num(geometry.width)}x{_fmt_num(geometry.height)}"
            )
        return text
    insert_at = text.rfind(")")
    if insert_at < 0:
        raise PlacementError("Cannot emit board outline: PCB file is not a KiCad S-expression")
    return text[:insert_at] + _outline_text(geometry, generated_uuids=generated_uuids) + text[insert_at:]

def _replace_at(block: str, x: float, y: float, rot: Optional[float]) -> str:
    """Replace only the footprint-level (at ...) field, preserving indentation and rotation form."""

    def repl(match: re.Match[str]) -> str:
        indent = match.group(1)
        old_rot = match.group(4)
        if rot is None:
            suffix = f" {old_rot}" if old_rot is not None else ""
            return f"{indent}(at {_fmt_num(x)} {_fmt_num(y)}{suffix})"
        return f"{indent}(at {_fmt_num(x)} {_fmt_num(y)} {_fmt_num(rot)})"

    new_block, count = re.subn(
        r'(?m)^(\s*)\(at\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)(?:\s+([-+0-9.eE]+))?\)',
        repl,
        block,
        count=1,
    )
    if count != 1:
        raise PlacementError("Could not replace footprint-level (at ...) field")
    return new_block



# ---------------------------------------------------------------------------
# Footprint bounding boxes and spacing
# ---------------------------------------------------------------------------

_NUM = r"([-+0-9.eE]+)"


def _fp_kind_name(fp: Footprint) -> str:
    match = re.match(r'\s*\(footprint\s+"([^"]+)"', fp.text)
    return match.group(1) if match else "unknown"


@functools.lru_cache(maxsize=None)
def infer_part_class(ref: str) -> str:
    upper = ref.upper()
    for prefix, cls in (("MH", "mechanical"), ("TP", "testpoint"), ("SW", "switch"),
                        ("FB", "passive"), ("R", "passive"), ("C", "passive"),
                        ("L", "passive"), ("F", "passive"), ("D", "passive"),
                        ("U", "ic"), ("J", "connector"), ("P", "connector"),
                        ("H", "mechanical")):
        if upper.startswith(prefix):
            return cls
    return "default"


def _part_class_for(model: PlacementModel, ref: str) -> str:
    return model.part_classes.get(ref, infer_part_class(ref))


def _physical_side(fp: Footprint) -> str:
    layer = (fp.layer or "").lower()
    if layer.startswith("f."):
        return "front"
    if layer.startswith("b."):
        return "back"
    return "both"


def _same_physical_side(a: Footprint, b: Footprint) -> bool:
    side_a = _physical_side(a)
    side_b = _physical_side(b)
    return side_a == "both" or side_b == "both" or side_a == side_b


# Rotation applied for rot="auto" on Edge() rules, by access side. Convention:
# at rot=0 the footprint's mating face points toward -y (the top board edge).
# Override with an explicit rot=... when a footprint uses a different convention.
ACCESS_SIDE_ROTATIONS: Dict[str, float] = {"top": 0.0, "right": 90.0, "bottom": 180.0, "left": 270.0}


def access_side_rotation(access_side: Optional[str]) -> Optional[float]:
    if access_side is None:
        return None
    return ACCESS_SIDE_ROTATIONS.get(str(access_side).lower().replace("-", "_"))


def _rot_point(x: float, y: float, deg: float) -> Point:
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return x * c - y * s, x * s + y * c


def _bbox_from_points(points: Sequence[Point]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def _transform_local_bbox(local: BBox, x: float, y: float, rot: float) -> BBox:
    corners = [(local.min_x, local.min_y), (local.min_x, local.max_y), (local.max_x, local.min_y), (local.max_x, local.max_y)]
    return _bbox_from_points([(x + rx, y + ry) for rx, ry in (_rot_point(cx, cy, rot) for cx, cy in corners)])


def _parse_fp_local_bbox(fp: Footprint) -> Optional[BBox]:
    points: List[Point] = []
    text = fp.text
    # Pads: use pad-local at/size rectangles. Rotation inside the footprint is approximated by its extents.
    for match in re.finditer(r'\(pad\b', text):
        block = text[match.start():_find_matching_paren(text, text.find("(", match.start()))]
        at = re.search(r'\(at\s+' + _NUM + r'\s+' + _NUM + r'(?:\s+' + _NUM + r')?\)', block)
        size = re.search(r'\(size\s+' + _NUM + r'\s+' + _NUM + r'\)', block)
        if at and size:
            cx, cy = float(at.group(1)), float(at.group(2))
            w, h = float(size.group(1)), float(size.group(2))
            points.extend([(cx - w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2), (cx + w / 2, cy - h / 2)])
    for pat in (r'\((?:fp_line|gr_line)\b[^)]*?\(start\s+' + _NUM + r'\s+' + _NUM + r'\).*?\(end\s+' + _NUM + r'\s+' + _NUM + r'\)',):
        for m in re.finditer(pat, text, flags=re.S):
            points.extend([(float(m.group(1)), float(m.group(2))), (float(m.group(3)), float(m.group(4)))])
    for m in re.finditer(r'\((?:fp_rect|gr_rect)\b[^)]*?\(start\s+' + _NUM + r'\s+' + _NUM + r'\).*?\(end\s+' + _NUM + r'\s+' + _NUM + r'\)', text, flags=re.S):
        x0, y0, x1, y1 = map(float, m.groups())
        points.extend([(x0, y0), (x1, y1), (x0, y1), (x1, y0)])
    for m in re.finditer(r'\((?:fp_circle|gr_circle)\b[^)]*?\(center\s+' + _NUM + r'\s+' + _NUM + r'\).*?\(end\s+' + _NUM + r'\s+' + _NUM + r'\)', text, flags=re.S):
        cx, cy, ex, ey = map(float, m.groups())
        r = math.hypot(ex - cx, ey - cy)
        points.extend([(cx - r, cy - r), (cx + r, cy + r)])
    for m in re.finditer(r'\((?:fp_poly|gr_poly)\b.*?\(pts\s+(.*?)\)\s*\)', text, flags=re.S):
        for p in re.finditer(r'\(xy\s+' + _NUM + r'\s+' + _NUM + r'\)', m.group(1)):
            points.append((float(p.group(1)), float(p.group(2))))
    return _bbox_from_points(points) if points else None


def _parse_pad_local_centers(fp: Footprint) -> Dict[str, Point]:
    """Return KiCad pad names/numbers mapped to footprint-local pad centers."""

    pads: Dict[str, Point] = {}
    for match in re.finditer(r'\(pad\b', fp.text):
        block = fp.text[match.start():_find_matching_paren(fp.text, fp.text.find("(", match.start()))]
        name_match = re.match(r'\(pad\s+("(?:[^"\\]|\\.)*"|[^\s()]+)', block)
        at = re.search(r'\(at\s+' + _NUM + r'\s+' + _NUM + r'(?:\s+' + _NUM + r')?\)', block)
        if not name_match or not at:
            continue
        name = name_match.group(1)
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        pads[name] = (float(at.group(1)), float(at.group(2)))
    return pads


def _fallback_local_bbox(fp: Footprint, cls: str) -> Tuple[BBox, str]:
    sizes = {
        "passive": (1.6, 0.8),
        "ic": (1.0, 1.0),
        "connector": (2.0, 2.0),
        "mechanical": (3.0, 3.0),
        "testpoint": (1.0, 1.0),
        "switch": (4.0, 4.0),
        "default": (1.0, 1.0),
    }
    w, h = sizes.get(cls, sizes["default"])
    kind = _fp_kind_name(fp)
    warning = f"{fp.ref} footprint {kind!r} has no parsed pad/graphic geometry; using {w}x{h} mm {cls} fallback bbox"
    return BBox(-w / 2, -h / 2, w / 2, h / 2), warning


# Footprint-local geometry (pad/graphic extents and pad centers) is independent
# of where the part is placed, so it is parsed once and cached.  The cache is
# keyed by object identity and validated against the footprint text so it is
# safe to reuse across multiple apply_placements() runs in the same process.
_LOCAL_BBOX_CACHE: Dict[int, Tuple[str, BBox, bool, Optional[str]]] = {}
_PAD_CENTER_CACHE: Dict[int, Tuple[str, Dict[str, Point]]] = {}
# Process-wide geometry cache hit/miss counters surfaced in the runtime report.
_GEOMETRY_CACHE_STATS = {"hits": 0, "misses": 0}


def _cached_local_bbox(fp: Footprint, cls: str) -> Tuple[BBox, bool, Optional[str]]:
    """Return the cached footprint-local bbox plus fallback flag/warning."""

    hit = _LOCAL_BBOX_CACHE.get(id(fp))
    if hit is not None and hit[0] is fp.text:
        _GEOMETRY_CACHE_STATS["hits"] += 1
        return hit[1], hit[2], hit[3]
    _GEOMETRY_CACHE_STATS["misses"] += 1
    local = _parse_fp_local_bbox(fp)
    fallback = False
    warning = None
    if local is None:
        local, warning = _fallback_local_bbox(fp, cls)
        fallback = True
    _LOCAL_BBOX_CACHE[id(fp)] = (fp.text, local, fallback, warning)
    return local, fallback, warning


def _cached_pad_centers(fp: Footprint) -> Dict[str, Point]:
    """Return cached footprint-local pad centers."""

    hit = _PAD_CENTER_CACHE.get(id(fp))
    if hit is not None and hit[0] is fp.text:
        return hit[1]
    centers = _parse_pad_local_centers(fp)
    _PAD_CENTER_CACHE[id(fp)] = (fp.text, centers)
    return centers


def footprint_bbox_at(fp: Footprint, x: float, y: float, rot: float, model: PlacementModel) -> BBoxInfo:
    cls = _part_class_for(model, fp.ref)
    local, fallback, warning = _cached_local_bbox(fp, cls)
    return BBoxInfo(fp.ref, _transform_local_bbox(local, x, y, rot), fallback, warning)


class SpatialIndex:
    """Uniform-grid spatial index over placed footprint bounding boxes.

    Collision checks only need to consider footprints whose bbox is near the
    candidate.  Rather than scanning every footprint for every candidate, parts
    are bucketed into fixed-size grid cells and ``query(bbox)`` returns just the
    refs occupying the overlapping cells.  Stdlib only; no R-tree dependency.
    """

    def __init__(self, cell_size: float = 5.0) -> None:
        self.cell_size = max(0.5, float(cell_size))
        self._cells: Dict[Tuple[int, int], Set[str]] = {}
        self._ref_cells: Dict[str, Tuple[Tuple[int, int], ...]] = {}
        # Absolute bbox of each indexed footprint at its current position, so
        # collision/clearance queries reuse it instead of recomputing transforms.
        self._bboxes: Dict[str, BBox] = {}
        # Diagnostics for the spatial_index report block.
        self.queries = 0
        self.candidates_checked = 0
        self.rebuilds = 0

    def _cell_range(self, bbox: BBox) -> Tuple[int, int, int, int]:
        cs = self.cell_size
        return (int(math.floor(bbox.min_x / cs)), int(math.floor(bbox.min_y / cs)),
                int(math.floor(bbox.max_x / cs)), int(math.floor(bbox.max_y / cs)))

    def _cells_for(self, bbox: BBox) -> Tuple[Tuple[int, int], ...]:
        x0, y0, x1, y1 = self._cell_range(bbox)
        return tuple((cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1))

    def insert(self, ref: str, bbox: BBox) -> None:
        """Insert/replace ``ref`` at the cells covered by ``bbox``."""

        self.remove(ref)
        cells = self._cells_for(bbox)
        for cell in cells:
            self._cells.setdefault(cell, set()).add(ref)
        self._ref_cells[ref] = cells
        self._bboxes[ref] = bbox

    def remove(self, ref: str) -> None:
        self._bboxes.pop(ref, None)
        for cell in self._ref_cells.pop(ref, ()):  # type: ignore[arg-type]
            bucket = self._cells.get(cell)
            if bucket is not None:
                bucket.discard(ref)
                if not bucket:
                    del self._cells[cell]

    def bbox_of(self, ref: str) -> Optional[BBox]:
        return self._bboxes.get(ref)

    def query(self, bbox: BBox) -> Set[str]:
        """Return refs whose cells overlap ``bbox`` (a superset of true overlaps)."""

        self.queries += 1
        found: Set[str] = set()
        x0, y0, x1, y1 = self._cell_range(bbox)
        cells = self._cells
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                bucket = cells.get((cx, cy))
                if bucket:
                    found |= bucket
        self.candidates_checked += len(found)
        return found

    @property
    def cell_count(self) -> int:
        return len(self._cells)

    def average_candidates_checked(self) -> float:
        return (self.candidates_checked / self.queries) if self.queries else 0.0

    def report(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "cell_size_mm": round(self.cell_size, 3),
            "cells": self.cell_count,
            "indexed_refs": len(self._ref_cells),
            "queries": self.queries,
            "candidates_checked": self.candidates_checked,
            "average_candidates_checked": round(self.average_candidates_checked(), 3),
            "rebuilds": self.rebuilds,
        }


class PlacementProfiler:
    """Lightweight counters and per-rule timing for placement profiling."""

    def __init__(self) -> None:
        self.counters: Dict[str, int] = {
            "candidates_generated": 0,
            "candidates_evaluated": 0,
            "candidates_pruned": 0,
            "candidates_early_accepted": 0,
            "collision_checks": 0,
            "spatial_index_queries": 0,
            "geometry_cache_hits": 0,
            "geometry_cache_misses": 0,
            "reflow_attempts": 0,
        }
        self.rule_runtime: Dict[str, float] = {}
        self.rule_counts: Dict[str, int] = {}

    def incr(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def record_rule(self, label: str, seconds: float) -> None:
        self.rule_runtime[label] = self.rule_runtime.get(label, 0.0) + seconds
        self.rule_counts[label] = self.rule_counts.get(label, 0) + 1

    def slowest_rules(self, n: int = 10) -> List[Dict[str, Any]]:
        items = sorted(self.rule_runtime.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return [{"rule": label, "seconds": round(secs, 4), "invocations": self.rule_counts.get(label, 0)}
                for label, secs in items]

    def report(self, *, total_runtime: float) -> Dict[str, Any]:
        return {
            "total_runtime_seconds": round(total_runtime, 4),
            "counters": dict(self.counters),
            "per_rule_runtime": {k: round(v, 4) for k, v in sorted(self.rule_runtime.items())},
            "slowest_rules": self.slowest_rules(),
        }


# Optimization-level presets controlling search breadth, reflow depth, and the
# early-success threshold.  fast trades quality for speed; deep does the reverse.
# ``post_legal_candidates`` bounds how many more layouts an array/group search
# explores after a legal one is found, looking for a marginally better score.
# fast accepts the first legal layout; deep keeps searching up to the full cap.
OPTIMIZATION_LEVELS: Dict[str, Dict[str, Any]] = {
    "fast": {"max_candidates_per_rule": 150, "excellent_threshold": 3.0,
             "max_floorplan_iterations": 2, "reflow_max_level": 2, "parent_reflow": False,
             "post_legal_candidates": 0},
    "normal": {"max_candidates_per_rule": 500, "excellent_threshold": 1.0,
               "max_floorplan_iterations": 5, "reflow_max_level": 3, "parent_reflow": "if_needed",
               "post_legal_candidates": 48},
    "deep": {"max_candidates_per_rule": 2000, "excellent_threshold": 0.25,
             "max_floorplan_iterations": 12, "reflow_max_level": 5, "parent_reflow": True,
             "post_legal_candidates": 2000},
}


class RuntimeBudget:
    """Wall-clock budget and optimization-level knobs for a placement run.

    When the budget expires the floorplanner stops generating new search work
    and falls back to best-known placements (marked degraded/requires_review)
    instead of aborting -- unless ``strict`` forces a hard failure.
    """

    def __init__(self, *, level: str = "normal", time_budget_seconds: float = 60.0,
                 max_candidates_per_rule: Optional[int] = None,
                 max_floorplan_iterations: Optional[int] = None,
                 strict: bool = False) -> None:
        self.level = level if level in OPTIMIZATION_LEVELS else "normal"
        preset = OPTIMIZATION_LEVELS[self.level]
        self.time_budget_seconds = float(time_budget_seconds)
        self.max_candidates_per_rule = int(max_candidates_per_rule
                                            if max_candidates_per_rule is not None
                                            else preset["max_candidates_per_rule"])
        self.excellent_threshold = float(preset["excellent_threshold"])
        self.reflow_max_level = int(preset["reflow_max_level"])
        self.parent_reflow = preset["parent_reflow"]
        self.post_legal_candidates = int(preset["post_legal_candidates"])
        self.max_floorplan_iterations = int(max_floorplan_iterations
                                            if max_floorplan_iterations is not None
                                            else preset["max_floorplan_iterations"])
        self.strict = strict
        self.start_time = time.perf_counter()
        self.timed_out = False
        self.degraded_refs: Set[str] = set()

    def elapsed(self) -> float:
        return time.perf_counter() - self.start_time

    def expired(self) -> bool:
        if self.time_budget_seconds <= 0:
            return False
        if self.elapsed() >= self.time_budget_seconds:
            self.timed_out = True
            return True
        return False

    def report(self, *, candidates_evaluated: int, cache_hits: int) -> Dict[str, Any]:
        return {
            "optimization_level": self.level,
            "elapsed_seconds": round(self.elapsed(), 4),
            "budget_seconds": self.time_budget_seconds,
            "timed_out": self.timed_out,
            "candidates_evaluated": candidates_evaluated,
            "cache_hits": cache_hits,
            "max_candidates_per_rule": self.max_candidates_per_rule,
            "degraded_refs": sorted(self.degraded_refs),
        }


def all_bbox_infos(engine: "PlacementEngine", *, refs: Optional[Iterable[str]] = None) -> Dict[str, BBoxInfo]:
    selected = set(refs) if refs is not None else set(engine.positions)
    infos: Dict[str, BBoxInfo] = {}
    for ref in selected:
        if ref not in engine.footprints:
            continue
        x, y, rot = engine.positions[ref]
        infos[ref] = footprint_bbox_at(engine.footprints[ref], x, y, rot, engine.model)
    return infos


def _board_contains_bbox(geometry: Optional[BoardGeometry], bbox: BBox, *, eps: float = 1e-9) -> bool:
    if geometry is None:
        return True
    return (geometry.min_x - eps <= bbox.min_x and bbox.max_x <= geometry.max_x + eps and
            geometry.min_y - eps <= bbox.min_y and bbox.max_y <= geometry.max_y + eps)


def _geometry_bbox(geometry: Optional[BoardGeometry]) -> Optional[BBox]:
    if geometry is None:
        return None
    return BBox(geometry.min_x, geometry.min_y, geometry.max_x, geometry.max_y)


def _union_bbox(boxes: Sequence[BBox]) -> Optional[BBox]:
    if not boxes:
        return None
    return BBox(min(b.min_x for b in boxes), min(b.min_y for b in boxes),
                max(b.max_x for b in boxes), max(b.max_y for b in boxes))


def slide_array_bbox_into_bounds(items: Sequence[Tuple[float, float]], side: str, board_bbox: Optional[BBox],
                                 margin: float = 0.0, *, item_bboxes: Optional[Sequence[BBox]] = None) -> Dict[str, Any]:
    """Slide a complete array along its side tangent so the full bbox fits board bounds."""

    if board_bbox is None:
        return {"items": list(items), "slide_applied": False, "slide_dx": 0.0, "slide_dy": 0.0,
                "original_array_bbox": None, "slid_array_bbox": None}
    boxes = list(item_bboxes or [BBox(x, y, x, y) for x, y in items])
    original = _union_bbox(boxes)
    if original is None:
        return {"items": list(items), "slide_applied": False, "slide_dx": 0.0, "slide_dy": 0.0,
                "original_array_bbox": None, "slid_array_bbox": None}
    allowed = BBox(board_bbox.min_x + margin, board_bbox.min_y + margin,
                   board_bbox.max_x - margin, board_bbox.max_y - margin)
    dx = dy = 0.0
    if side in {"top", "bottom"}:
        if original.width > allowed.width + 1e-9:
            return {"items": list(items), "slide_applied": False, "slide_dx": 0.0, "slide_dy": 0.0,
                    "original_array_bbox": original, "slid_array_bbox": original, "slide_rejected": "array wider than board"}
        if original.min_x < allowed.min_x:
            dx = allowed.min_x - original.min_x
        elif original.max_x > allowed.max_x:
            dx = allowed.max_x - original.max_x
    elif side in {"left", "right"}:
        if original.height > allowed.height + 1e-9:
            return {"items": list(items), "slide_applied": False, "slide_dx": 0.0, "slide_dy": 0.0,
                    "original_array_bbox": original, "slid_array_bbox": original, "slide_rejected": "array taller than board"}
        if original.min_y < allowed.min_y:
            dy = allowed.min_y - original.min_y
        elif original.max_y > allowed.max_y:
            dy = allowed.max_y - original.max_y
    slid = original.translated(dx, dy)
    return {"items": [(x + dx, y + dy) for x, y in items], "slide_applied": abs(dx) > 1e-9 or abs(dy) > 1e-9,
            "slide_dx": dx, "slide_dy": dy, "original_array_bbox": original, "slid_array_bbox": slid}


def _point_segment_distance(point: Point, a: Point, b: Point) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    length_sq = vx * vx + vy * vy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / length_sq))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _bbox_intrudes_corridor(bbox: BBox, a: Point, b: Point, width: float) -> bool:
    """Conservative bbox-vs-segment corridor test used as a soft routing penalty."""

    if width <= 0:
        return False
    half = width / 2.0
    corners = [(bbox.min_x, bbox.min_y), (bbox.min_x, bbox.max_y),
               (bbox.max_x, bbox.min_y), (bbox.max_x, bbox.max_y)]
    if any(_point_segment_distance(corner, a, b) <= half for corner in corners):
        return True
    if bbox.min_x <= a[0] <= bbox.max_x and bbox.min_y <= a[1] <= bbox.max_y:
        return True
    if bbox.min_x <= b[0] <= bbox.max_x and bbox.min_y <= b[1] <= bbox.max_y:
        return True
    return False


def _rect_to_abs_bbox(model: PlacementModel, item: Mapping[str, Any]) -> BBox:
    x0, y0 = _board_to_abs(model, float(item["x"]), float(item["y"]))
    return BBox(x0, y0, x0 + float(item["w"]), y0 + float(item["h"]))


def keepout_rules(model: PlacementModel) -> List[Dict[str, Any]]:
    return [rule for rule in model.rules if rule.get("type") == "keepout"]


def keepout_report(model: PlacementModel) -> List[Dict[str, Any]]:
    out = []
    for rule in keepout_rules(model):
        bbox = _rect_to_abs_bbox(model, rule)
        out.append({k: v for k, v in rule.items() if k != "extra"} | {"bbox": bbox.as_report(), "emit": bool(rule.get("emit", False))})
    return out


def regions_report(model: PlacementModel) -> Dict[str, Dict[str, Any]]:
    return {name: dict(region) | {"bbox": _rect_to_abs_bbox(model, region).as_report()} for name, region in model.regions.items()}


def spacing_analysis(engine: "PlacementEngine", *, refs: Optional[Iterable[str]] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    subject_refs = set(refs) if refs is not None else set(engine.positions)
    considered_refs = set(engine.positions) if refs is not None and subject_refs else subject_refs
    infos = all_bbox_infos(engine, refs=considered_refs)
    bbox_warnings = []
    seen_warnings: Set[str] = set()
    for ref in sorted(subject_refs | set(infos)):
        info = infos.get(ref)
        if info is not None and info.warning and info.warning not in seen_warnings:
            bbox_warnings.append({"ref": info.ref, "message": info.warning})
            seen_warnings.add(info.warning)
    collisions: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []
    refs_sorted = sorted(infos)
    for i, a in enumerate(refs_sorted):
        for b in refs_sorted[i + 1:]:
            if refs is not None and a not in subject_refs and b not in subject_refs:
                continue
            if not _same_physical_side(engine.footprints[a], engine.footprints[b]):
                continue
            ia, ib = infos[a], infos[b]
            ca, cb = _part_class_for(engine.model, a), _part_class_for(engine.model, b)
            required = engine.model.clearance.required_for(ca, cb)
            actual = ia.bbox.clearance_to(ib.bbox)
            item = {"ref_a": a, "ref_b": b, "bbox_a": ia.bbox.as_report(), "bbox_b": ib.bbox.as_report(),
                    "class_a": ca, "class_b": cb, "layer_a": engine.footprints[a].layer, "layer_b": engine.footprints[b].layer,
                    "side_a": _physical_side(engine.footprints[a]), "side_b": _physical_side(engine.footprints[b]),
                    "required_clearance": required, "actual_clearance": actual}
            if ia.bbox.overlaps(ib.bbox):
                collisions.append(item)
            elif actual < required - 1e-9:
                violations.append(item)
    return collisions, violations, bbox_warnings


def keepout_violations(engine: "PlacementEngine", *, refs: Optional[Iterable[str]] = None,
                       allow_keepout_overlap: bool = False) -> List[Dict[str, Any]]:
    if allow_keepout_overlap:
        return []
    subject_refs = set(refs) if refs is not None else set(engine.positions)
    infos = all_bbox_infos(engine, refs=subject_refs)
    allowed_refs: Set[str] = set()
    for rule in engine.model.rules:
        if rule.get("ref") and bool(rule.get("allow_keepout_overlap", rule.get("extra", {}).get("allow_keepout_overlap", False))):
            actual = engine.resolve_ref(str(rule["ref"]))
            if actual:
                allowed_refs.add(actual)
    out: List[Dict[str, Any]] = []
    for keepout in keepout_rules(engine.model):
        if bool(keepout.get("extra", {}).get("allow_keepout_overlap", False)):
            continue
        kb = _rect_to_abs_bbox(engine.model, keepout)
        for ref, info in sorted(infos.items()):
            if ref in allowed_refs:
                continue
            if info.bbox.overlaps(kb):
                out.append({"ref": ref, "keepout": keepout["name"], "bbox": info.bbox.as_report(),
                            "keepout_bbox": kb.as_report(), "role": keepout.get("role")})
    return out


def region_violations(engine: "PlacementEngine", *, refs: Optional[Iterable[str]] = None,
                      allow_outside_region: bool = False) -> List[Dict[str, Any]]:
    if allow_outside_region:
        return []
    subject_refs = set(refs) if refs is not None else set(engine.updates)
    infos = all_bbox_infos(engine, refs=subject_refs)
    out: List[Dict[str, Any]] = []
    rule_region_by_ref: Dict[str, str] = {}
    allowed_refs: Set[str] = set()
    for rule in engine.model.rules:
        if rule.get("ref") and bool(rule.get("allow_outside_region", rule.get("extra", {}).get("allow_outside_region", False))):
            actual = engine.resolve_ref(str(rule["ref"]))
            if actual:
                allowed_refs.add(actual)
        region = rule.get("region")
        if region is None:
            region = rule.get("extra", {}).get("region")
        if region and rule.get("ref"):
            actual = engine.resolve_ref(str(rule["ref"]))
            if actual:
                rule_region_by_ref[actual] = str(region)
    for ref, region_name in sorted(rule_region_by_ref.items()):
        if ref in allowed_refs:
            continue
        if ref not in infos or region_name not in engine.model.regions:
            continue
        rb = _rect_to_abs_bbox(engine.model, engine.model.regions[region_name])
        if not rb.contains_bbox(infos[ref].bbox):
            out.append({"ref": ref, "region": region_name, "bbox": infos[ref].bbox.as_report(),
                        "region_bbox": rb.as_report()})
    return out

# ---------------------------------------------------------------------------
# Placement engine
# ---------------------------------------------------------------------------


class PlacementEngine:
    """Applies a PlacementModel to parsed KiCad footprint positions."""

    def __init__(self, footprints: Mapping[str, Footprint], model: PlacementModel,
                 *, strict: bool = False, allow_suffix_match: bool = True,
                 cardinal_rotations: bool = False, board_geometry: Optional[BoardGeometry] = None,
                 allow_keepout_overlap: bool = False, allow_outside_region: bool = False,
                 best_effort: bool = True, fail_fast: bool = False,
                 max_floorplan_iterations: int = 10,
                 runtime: Optional[RuntimeBudget] = None,
                 progress: bool = False) -> None:
        self.footprints = dict(footprints)
        self.model = model
        self.strict = strict
        self.runtime = runtime if runtime is not None else RuntimeBudget(strict=strict)
        self.profiler = PlacementProfiler()
        self.progress = progress
        self._cache_stats_baseline = dict(_GEOMETRY_CACHE_STATS)
        # max_floorplan_iterations from the runtime budget/optimization level
        # takes precedence over the legacy default when one was not explicitly set.
        if runtime is not None:
            max_floorplan_iterations = runtime.max_floorplan_iterations
        # No-abort placement: by default the floorplanner never aborts.  A local
        # placement failure triggers reflow and, as a last resort, parking.
        # Only --strict or --fail-fast turn unplaceable components into errors.
        self.fail_fast = fail_fast
        self.best_effort = best_effort and not strict and not fail_fast
        self.max_floorplan_iterations = max(1, int(max_floorplan_iterations))
        self.allow_suffix_match = allow_suffix_match
        self.cardinal_rotations = cardinal_rotations
        self.board_geometry = board_geometry
        self.allow_keepout_overlap = allow_keepout_overlap
        self.allow_outside_region = allow_outside_region
        self.messages: List[Message] = []
        self.positions: Dict[str, Tuple[float, float, float]] = {
            ref: (fp.x, fp.y, fp.rot) for ref, fp in self.footprints.items()
        }
        self.updates: Dict[str, PlacementUpdate] = {}
        self.placement_attempts: Dict[str, int] = {}
        self.ref_cache: Dict[str, str] = {}
        self.locked: Dict[str, str] = {}
        self.applied_priority: Dict[str, float] = {}
        self.applied_rule_index: Dict[str, int] = {}
        self.priority_conflicts: List[Dict[str, Any]] = []
        self.overridden_rules: List[Dict[str, Any]] = []
        self.locked_move_attempts: List[Dict[str, Any]] = []
        self.auto_adjustments: List[AutoAdjustment] = []
        self.clusters: List[Dict[str, Any]] = []
        self.last_cluster_by_ref: Dict[str, str] = {}
        self.search_log: Dict[str, SearchOutcome] = {}
        # Floorplanner state: reflow history, parked components, and the
        # per-iteration score trajectory used by the iterative optimizer.
        self.reflow_attempts: List[ReflowAttempt] = []
        self.parked: Dict[str, ParkedPlacement] = {}
        self.degraded_warnings: List[str] = []
        self.iteration_scores: List[Dict[str, Any]] = []
        self.requires_review: Set[str] = set()
        self._degrade_announced = False
        # Spatial index over placed footprints for fast collision queries.  It is
        # rebuilt lazily whenever positions change (tracked by _index_version) so
        # that the many collision checks within a single candidate search reuse it.
        self._spatial_index: Optional[SpatialIndex] = None
        self._index_version = 0
        self._spatial_index_built_version = -1
        self._index_cell_size = self._compute_index_cell_size()
        self._max_clearance = self._compute_max_clearance()
        self.allow_body_outside_refs: Set[str] = self._collect_allow_body_outside_refs()
        for error in model.alias_diagnostics.errors:
            self.messages.append(Message("error", error))
        for warning in model.alias_diagnostics.warnings:
            self.messages.append(Message("warn", warning))
        if self.strict and model.alias_diagnostics.errors:
            raise PlacementError("netlist alias validation failed: " + "; ".join(model.alias_diagnostics.errors))

    def _collect_allow_body_outside_refs(self) -> Set[str]:
        """Refs whose footprint body/courtyard may extend past the board outline.

        Granted by Edge(allow_body_outside_board=True) directly or via a
        Cluster() whose Edge placement carries the flag (the grant applies to
        the cluster anchor, typically the connector itself, not its support
        parts). The footprint origin must still land inside the board.
        """

        allowed: Set[str] = set()

        def flagged(spec: Mapping[str, Any]) -> bool:
            return bool(spec.get("allow_body_outside_board") or
                        (spec.get("extra") or {}).get("allow_body_outside_board"))

        for rule in self.model.rules:
            if rule.get("type") == "cluster":
                placement = rule.get("placement") or {}
                if isinstance(placement, Mapping) and flagged(placement):
                    anchor = self.resolve_ref(str(rule.get("anchor")))
                    if anchor is not None:
                        allowed.add(anchor)
                continue
            if rule.get("ref") is not None and flagged(rule):
                actual = self.resolve_ref(str(rule["ref"]))
                if actual is not None:
                    allowed.add(actual)
        return allowed

    def _compute_index_cell_size(self) -> float:
        """Pick a grid cell size from typical footprint extents.

        A cell roughly the size of the largest footprint keeps each part in a
        handful of cells while bounding the number of refs returned per query.
        """

        extents: List[float] = []
        for ref, fp in self.footprints.items():
            cls = _part_class_for(self.model, ref)
            local, _fb, _w = _cached_local_bbox(fp, cls)
            extents.append(max(local.width, local.height))
        if not extents:
            return 5.0
        extents.sort()
        # Use a high-percentile extent (robust to a few huge parts) plus clearance.
        idx = min(len(extents) - 1, int(len(extents) * 0.9))
        return max(2.0, min(20.0, extents[idx] + 2.0))

    def _compute_max_clearance(self) -> float:
        """Largest clearance any part-class pair can require (query margin)."""

        clearance = self.model.clearance
        values = [clearance.default]
        for attr in ("passive_to_passive", "passive_to_ic", "ic_to_ic", "connector", "mechanical"):
            val = getattr(clearance, attr, None)
            if isinstance(val, (int, float)):
                values.append(float(val))
        return max(values) if values else 0.25

    def _touch_positions(self) -> None:
        """Mark the spatial index stale after a position change."""

        self._index_version += 1

    def _set_position(self, ref: str, x: float, y: float, rot: float) -> None:
        """Update a footprint position and invalidate the spatial index."""

        self.positions[ref] = (float(x), float(y), float(rot))
        self._touch_positions()

    def _ensure_spatial_index(self) -> SpatialIndex:
        """Return the spatial index, rebuilding it if positions changed."""

        if self._spatial_index is None:
            self._spatial_index = SpatialIndex(self._index_cell_size)
        if self._spatial_index_built_version != self._index_version:
            index = self._spatial_index
            index._cells.clear()
            index._ref_cells.clear()
            index._bboxes.clear()
            for ref, (x, y, rot) in self.positions.items():
                if ref not in self.footprints:
                    continue
                bbox = footprint_bbox_at(self.footprints[ref], x, y, rot, self.model).bbox
                index.insert(ref, bbox)
            index.rebuilds += 1
            self._spatial_index_built_version = self._index_version
        return self._spatial_index

    def _raise_strict_timeout(self) -> None:
        """Raise the strict-mode timeout error after the budget has expired."""

        if self.runtime.strict:
            raise PlacementError(
                f"placement time budget of {self.runtime.time_budget_seconds:g}s exceeded "
                f"after {self.runtime.elapsed():.1f}s (strict mode)")

    def _maybe_degrade_on_timeout(self) -> None:
        """If the wall-clock budget is spent, enter best-effort degrade mode.

        Degrade mode keeps placing remaining components with the best known
        candidate but flags them requires_review instead of aborting.  --strict
        turns the timeout into a hard error.
        """

        if not self.runtime.expired():
            return
        # expired() set timed_out=True.  Check strict before the once-only guard
        # so a timeout first observed on a non-strict search path still aborts.
        self._raise_strict_timeout()
        if self._degrade_announced:
            return
        self._degrade_announced = True
        # Shrink the per-rule search to a minimal bounded pass so the run drains
        # quickly while still placing every remaining component.
        self.runtime.max_candidates_per_rule = min(self.runtime.max_candidates_per_rule, 24)
        self.degraded_warnings.append(
            f"time budget {self.runtime.time_budget_seconds:g}s exceeded at "
            f"{self.runtime.elapsed():.1f}s; remaining placements are best-effort and require review")
        self.messages.append(Message("warn", self.degraded_warnings[-1]))

    def _maybe_progress(self, rule_index: int, total: int, rule: Mapping[str, Any], typ: str) -> None:
        """Emit concise, throttled progress for long runs (>=0.5s between lines)."""

        if not self.progress:
            return
        now = time.perf_counter()
        last = getattr(self, "_last_progress_time", 0.0)
        if rule_index != 0 and now - last < 0.5:
            return
        self._last_progress_time = now
        ref = rule.get("ref")
        if ref is None:
            refs = rule.get("refs") or []
            ref = (refs[0] if refs else rule.get("anchor") or rule.get("name") or "")
        count = len(rule.get("refs") or []) or 1
        label = "".join(part.capitalize() for part in str(typ).split("_"))
        print(f"placing {rule_index + 1}/{total} {ref} {label} candidates={count} "
              f"elapsed={self.runtime.elapsed():.1f}s", file=sys.stderr)

    def alias_map(self) -> Dict[str, str]:
        """Return the effective alias map used by the resolver."""

        aliases = {ref: ref for ref in self.footprints}
        aliases.update(self.model.imported_aliases)
        aliases.update(self.model.aliases)
        return aliases

    def resolve_ref(self, ref: str) -> Optional[str]:
        """Resolve a DSL ref/alias to an actual KiCad footprint reference."""

        original_ref = _normalize_ref(ref)
        if original_ref in self.ref_cache:
            return self.ref_cache[original_ref]
        alias_map = self.alias_map()
        target = alias_map.get(original_ref, original_ref)
        if target in self.footprints:
            self.ref_cache[original_ref] = target
            return target

        if self.allow_suffix_match:
            matches: Set[str] = set()
            normalized_query = normalize_alias_path(original_ref)
            for alias, candidate_ref in alias_map.items():
                if alias == original_ref:
                    continue
                normalized_alias = normalize_alias_path(alias)
                if candidate_ref not in self.footprints:
                    continue
                if normalized_alias.endswith(f".{normalized_query}") or normalized_alias.endswith(f":{original_ref}"):
                    matches.add(candidate_ref)
            suffixes = [f".{original_ref}", f"/{original_ref}", f":{original_ref}"]
            for candidate in self.footprints:
                if any(candidate.endswith(s) for s in suffixes):
                    matches.add(candidate)
            sorted_matches = sorted(matches)
            if len(sorted_matches) == 1:
                self.messages.append(Message("note", f"resolved {original_ref!r} to {sorted_matches[0]!r} by unambiguous suffix match"))
                self.ref_cache[original_ref] = sorted_matches[0]
                return sorted_matches[0]
            if len(sorted_matches) > 1:
                self._warn_or_raise(f"ambiguous alias/reference {original_ref!r}; matches: {', '.join(sorted_matches)}")
                return None
        return None

    def _warn_or_raise(self, msg: str) -> None:
        if self.strict:
            raise PlacementError(msg)
        self.messages.append(Message("warn", msg))

    def place(self, ref: str, x: float, y: float, rot: Optional[float], why: str, note: Optional[str] = None,
              *, allow_arbitrary_rotation: bool = False, avoid_overlap: bool = False,
              clearance_override: Optional[float] = None, candidate_sides: Optional[Sequence[str]] = None,
              region_override: Optional[BBox] = None,
              rule: Optional[Mapping[str, Any]] = None) -> None:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {ref!r} for {why}")
            return
        rule = rule or {}
        if actual_ref in self.locked:
            old_x, old_y, old_rot = self.positions.get(actual_ref, (0.0, 0.0, self.footprints[actual_ref].rot))
            pre_new_rot = old_rot if rot is None else _normalize_rotation(float(rot))
            if self.cardinal_rotations and rot is not None and not allow_arbitrary_rotation:
                pre_new_rot = _cardinal_rotation(pre_new_rot)
            changed = (abs(old_x - float(x)) > 1e-9 or abs(old_y - float(y)) > 1e-9 or abs(old_rot - pre_new_rot) > 1e-9)
            if changed:
                self.locked_move_attempts.append({"ref": actual_ref, "by": why, "locked_by": self.locked[actual_ref]})
                self._warn_or_raise(f"locked footprint {actual_ref!r} cannot be moved by {why}; locked by {self.locked[actual_ref]}")
                return
        priority = self._rule_priority(rule, actual_ref)
        index = int(rule.get("_index", -1))
        if actual_ref in self.updates:
            prev_priority = self.applied_priority.get(actual_ref, 0.0)
            prev_index = self.applied_rule_index.get(actual_ref, -1)
            if priority < prev_priority:
                self.overridden_rules.append({"ref": actual_ref, "ignored_by": why, "ignored_priority": priority,
                                              "winning_priority": prev_priority, "reason": "lower_priority"})
                self.messages.append(Message("warn", f"{actual_ref} placement by {why} ignored: priority {_fmt_num(priority)} is below existing {_fmt_num(prev_priority)}"))
                return
            if abs(priority - prev_priority) <= 1e-9:
                self.priority_conflicts.append({"ref": actual_ref, "previous_rule_index": prev_index,
                                                "new_rule_index": index, "priority": priority, "winner": "later"})
                self.messages.append(Message("warn", f"{actual_ref} has same-priority placement conflict at {_fmt_num(priority)}; later rule wins"))
            else:
                self.overridden_rules.append({"ref": actual_ref, "overridden_priority": prev_priority,
                                              "winning_priority": priority, "reason": "higher_priority"})
        previous_cluster = self.last_cluster_by_ref.get(actual_ref)
        if previous_cluster is not None and not why.startswith("cluster "):
            self.messages.append(Message("warn", f"{actual_ref} moved by cluster {previous_cluster} later refined by {why}"))
        self.placement_attempts[actual_ref] = self.placement_attempts.get(actual_ref, 0) + 1
        old_x, old_y, old_rot = self.positions.get(actual_ref, (0.0, 0.0, self.footprints[actual_ref].rot))
        explicit_rot = rot is not None
        write_rot: Optional[float] = None
        if explicit_rot:
            write_rot = _normalize_rotation(float(rot))
            if self.cardinal_rotations and not allow_arbitrary_rotation:
                write_rot = _cardinal_rotation(write_rot)
        new_rot = old_rot if write_rot is None else write_rot
        if actual_ref in self.locked:
            changed = (abs(old_x - x) > 1e-9 or abs(old_y - y) > 1e-9 or abs(old_rot - new_rot) > 1e-9)
            if changed:
                self.locked_move_attempts.append({"ref": actual_ref, "by": why, "locked_by": self.locked[actual_ref]})
                self._warn_or_raise(f"locked footprint {actual_ref!r} cannot be moved by {why}; locked by {self.locked[actual_ref]}")
                return
        soft = self._rule_soft(rule, actual_ref)
        if (avoid_overlap or soft) and self.model.policy.avoid_overlap:
            region = region_override if region_override is not None else (
                None if self.allow_outside_region else self._region_bbox(self._rule_value(rule, "region")))
            placed = self._find_non_overlapping_position(actual_ref, float(x), float(y), float(new_rot),
                                                         clearance_override=clearance_override,
                                                         candidate_sides=candidate_sides,
                                                         region=region,
                                                         allow_keepout_overlap=(self.allow_keepout_overlap or
                                                                                self._rule_flag(rule, "allow_keepout_overlap", False)))
            if placed is None and self.best_effort:
                placed = self._reflow_single(
                    actual_ref, float(x), float(y), float(new_rot),
                    clearance_override=clearance_override, region=region,
                    allow_keepout_overlap=(self.allow_keepout_overlap or
                                           self._rule_flag(rule, "allow_keepout_overlap", False)),
                    why=why)
            if placed is None:
                outcome = self.search_log.get(actual_ref)
                detail = (self._format_search_failure(actual_ref, why, outcome, requested=(float(x), float(y)))
                          if outcome is not None else
                          f"{actual_ref!r} could not be placed by {why} without violating clearance or board bounds; "
                          f"requested x={_fmt_num(x)} y={_fmt_num(y)}")
                if not self.best_effort:
                    raise PlacementError(detail)
                # No-abort: park at the requested location with a degraded warning.
                self._park(actual_ref, float(x), float(y), why, reason=detail, penalty=200.0)
                placed = (float(x), float(y), "parked (best-effort; no legal location after reflow)")
            px, py, reason = placed
            if abs(px - x) > 1e-9 or abs(py - y) > 1e-9:
                self.auto_adjustments.append(AutoAdjustment(actual_ref, float(x), float(y), px, py, reason))
                self.messages.append(Message("note", f"adjusted {actual_ref}: requested x={_fmt_num(x)} y={_fmt_num(y)} placed x={_fmt_num(px)} y={_fmt_num(py)}; {reason}"))
                x, y = px, py
        fp = self.footprints[actual_ref]
        outside = False
        if self.board_geometry is not None:
            outside = not self.board_geometry.contains(x, y)
        update = PlacementUpdate(
            ref=actual_ref, x=float(x), y=float(y), final_rot=float(new_rot), write_rot=write_rot, why=why, note=note,
            original_x=fp.x, original_y=fp.y, original_rot=fp.rot,
            rotation_changed=abs(fp.rot - new_rot) > 1e-9, outside_board=outside,
            delta_x=float(x) - fp.x, delta_y=float(y) - fp.y,
        )
        self.updates[actual_ref] = update
        self.applied_priority[actual_ref] = priority
        self.applied_rule_index[actual_ref] = index
        self._set_position(actual_ref, float(x), float(y), float(new_rot))
        if self.runtime.timed_out:
            self.requires_review.add(actual_ref)
            self.runtime.degraded_refs.add(actual_ref)
        suffix = f"  # {note}" if note else ""
        rot_label = _fmt_num(new_rot) if explicit_rot else "preserve"
        self.messages.append(
            Message("place", f"place {actual_ref:>16s} -> x={_fmt_num(x):>8s} y={_fmt_num(y):>8s} "
                             f"rot={rot_label:>8s}  {why}{suffix}")
        )
        if self._rule_flag(rule, "lock", False) or self._rule_flag(rule, "locked", False):
            self.lock(actual_ref, f"{why} rule")

    def _placement_target(self, rule: Mapping[str, Any]) -> Point:
        typ = str(rule["type"])
        if typ in {"anchor", "fixed", "corner", "edge"}:
            if rule.get("relative_to") is not None:
                bx, by = self.get_pos(str(rule["relative_to"]))
                return bx + float(rule.get("dx", 0.0)), by + float(rule.get("dy", 0.0))
            return _board_to_abs(self.model, float(rule["x"]), float(rule["y"]))
        if typ in {"between", "inline"}:
            a = self.get_pos(str(rule["a"]))
            b = self.get_pos(str(rule["b"]))
            t = float(rule.get("t", 0.5))
            x = a[0] + (b[0] - a[0]) * t + float(rule.get("dx", 0.0))
            y = a[1] + (b[1] - a[1]) * t + float(rule.get("dy", 0.0))
            if rule.get("offset") is not None:
                vx, vy = b[0] - a[0], b[1] - a[1]
                length = math.hypot(vx, vy) or 1.0
                x += (-vy / length) * float(rule["offset"])
                y += (vx / length) * float(rule["offset"])
            return x, y
        if typ == "orbit":
            cx, cy = self.get_pos(str(rule["parent"]))
            radius = float(rule["radius"])
            theta = math.radians(float(rule.get("start_angle", 0.0)) + float(rule.get("step_angle", 0.0)) * int(rule.get("index", 0)))
            return cx + radius * math.cos(theta), cy + radius * math.sin(theta)
        raise PlacementError(f"Cluster placement type {typ!r} cannot resolve a coordinate")

    def place_cluster(self, rule: Mapping[str, Any]) -> None:
        name = str(rule["name"])
        anchor_ref = self.resolve_ref(str(rule["anchor"]))
        if anchor_ref is None:
            raise PlacementError(f"Cluster {name} anchor {rule['anchor']!r} is missing")
        members: List[str] = []
        seen_members: Set[str] = set()
        for duplicate in rule.get("duplicate_members", []):
            self.messages.append(Message("warn", f"Cluster {name} duplicate member {duplicate!r}; de-duplicated"))
        for member in rule.get("members", []):
            actual = self.resolve_ref(str(member))
            if actual is None:
                msg = f"Cluster {name} missing member {member!r}"
                if rule.get("ignore_missing"):
                    self.messages.append(Message("warn", msg))
                    continue
                raise PlacementError(msg)
            if actual in seen_members:
                self.messages.append(Message("warn", f"Cluster {name} member {member!r} resolves to duplicate footprint {actual!r}; de-duplicated"))
                continue
            seen_members.add(actual)
            members.append(actual)
        if anchor_ref not in seen_members:
            members.insert(0, anchor_ref)
        old_anchor = self.get_pos(anchor_ref)
        new_anchor = self._placement_target(rule["placement"])
        dx = new_anchor[0] - old_anchor[0]
        dy = new_anchor[1] - old_anchor[1]
        rot_delta = self._cluster_rotation_delta(rule["placement"], anchor_ref)
        if abs(rot_delta) > 1e-9:
            self.messages.append(Message("note",
                f"cluster {name} rotated rigidly by {_fmt_num(rot_delta)} deg to honor access_side/rotation"))
        targets = []
        for actual in members:
            mx, my, _ = self.positions[actual]
            ox, oy = _rot_point(mx - old_anchor[0], my - old_anchor[1], rot_delta)
            targets.append((new_anchor[0] + ox, new_anchor[1] + oy))
        if self.board_geometry is not None:
            clamped_targets = []
            for tx, ty in targets:
                cx = min(max(tx, self.board_geometry.min_x), self.board_geometry.max_x)
                cy = min(max(ty, self.board_geometry.min_y), self.board_geometry.max_y)
                clamped_targets.append((cx, cy))
            if clamped_targets != targets:
                outside_count = sum(1 for (tx, ty), (cx, cy) in zip(targets, clamped_targets)
                                    if abs(tx - cx) > 1e-9 or abs(ty - cy) > 1e-9)
                self.messages.append(Message(
                    "warn",
                    f"cluster {name} had {outside_count} member target(s) outside board bounds; "
                    "clamped those anchors inside the board for best-effort placement"))
                targets = clamped_targets
        margin = self.model.policy.max_search_radius + 5.0
        cluster_region = BBox(
            min(t[0] for t in targets) - margin, min(t[1] for t in targets) - margin,
            max(t[0] for t in targets) + margin, max(t[1] for t in targets) + margin,
        )
        for actual, (tx, ty) in zip(members, targets):
            _x, _y, rot = self.positions[actual]
            new_rot = None if abs(rot_delta) <= 1e-9 else _normalize_rotation(rot + rot_delta)
            self.place(actual, tx, ty, new_rot, f"cluster {name}", rule.get("note"),
                       region_override=cluster_region)
            self.last_cluster_by_ref[actual] = name
        if self._rule_flag(rule.get("placement", {}), "edge_required", False):
            self.lock(anchor_ref, f"edge_required cluster {name}")
        self.clusters.append({
            "name": name,
            "anchor": anchor_ref,
            "member_count": len(members),
            "delta": [dx, dy],
            "old_anchor": [old_anchor[0], old_anchor[1]],
            "new_anchor": [new_anchor[0], new_anchor[1]],
        })

    def _cluster_rotation_delta(self, placement: Mapping[str, Any], anchor_ref: str) -> float:
        """Rigid rotation to apply to a cluster so its anchor honors the Edge rotation.

        Only Edge placements rotate clusters: rot="auto" uses the access-side
        convention, an explicit numeric rot is honored directly. The whole
        cluster rotates around the anchor so relative geometry is preserved.
        """

        if placement.get("type") != "edge":
            return 0.0
        rot_spec = placement.get("rot")
        target_rot: Optional[float] = None
        if isinstance(rot_spec, str) and rot_spec.lower() == "auto":
            target_rot = access_side_rotation(placement.get("access_side") or placement.get("edge"))
        elif rot_spec is not None:
            target_rot = _normalize_rotation(float(rot_spec))
        if target_rot is None:
            return 0.0
        current = self.positions[anchor_ref][2]
        return _normalize_rotation(target_rot - current)

    def lock(self, ref: str, why: str = "Lock") -> None:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {ref!r} for Lock")
            return
        self.locked[actual_ref] = why
        self.messages.append(Message("note", f"locked {actual_ref!r}: {why}"))

    def get_pos(self, ref: str) -> Point:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None or actual_ref not in self.positions:
            raise PlacementError(f"Rule references unknown footprint {ref!r}")
        x, y, _ = self.positions[actual_ref]
        return x, y

    def get_rot(self, ref: str) -> float:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None or actual_ref not in self.positions:
            raise PlacementError(f"Rule references unknown footprint {ref!r}")
        return self.positions[actual_ref][2]

    @staticmethod
    def resolve_rot(rot_spec: Any, a: Optional[Point] = None, b: Optional[Point] = None,
                    *, source_rot: Optional[float] = None, mirror_axis: Optional[str] = None) -> Optional[float]:
        if rot_spec is None:
            return None
        if isinstance(rot_spec, str):
            spec = rot_spec.lower()
            if spec in ("path", "along", "auto"):
                return None if a is None or b is None else _angle_between(a, b)
            if spec in ("perpendicular", "normal"):
                return None if a is None or b is None else _angle_between(a, b) + 90.0
            if spec == "mirror":
                if source_rot is None:
                    return None
                if mirror_axis in ("vertical", "x"):
                    return (180.0 - source_rot) % 360.0
                if mirror_axis in ("horizontal", "y"):
                    return (-source_rot) % 360.0
                return source_rot
            if spec in ("keep", "same"):
                return source_rot
            raise PlacementError(f"Unknown rotation spec {rot_spec!r}")
        return float(rot_spec)


    def _clearance_override(self, rule: Dict[str, Any]) -> Optional[float]:
        value = rule.get("clearance")
        if value is None:
            value = rule.get("extra", {}).get("clearance")
        return None if value is None else float(value)

    def _rule_value(self, rule: Mapping[str, Any], name: str, default: Any = None) -> Any:
        value = rule.get(name)
        if value is None:
            value = rule.get("extra", {}).get(name, default)
        return value

    def _rule_flag(self, rule: Mapping[str, Any], name: str, default: bool = False) -> bool:
        return bool(self._rule_value(rule, name, default))

    def _rule_priority(self, rule: Mapping[str, Any], actual_ref: str) -> float:
        if self._rule_flag(rule, "edge_required", False) or self._rule_flag(rule, "locked", False) or self._rule_flag(rule, "lock", False):
            return max(10000.0, float(self.model.priority_refs.get(actual_ref, 0.0)))
        value = self._rule_value(rule, "priority")
        if value is None:
            value = self.model.priority_refs.get(actual_ref)
        return 0.0 if value is None else float(value)

    def _rule_soft(self, rule: Mapping[str, Any], actual_ref: str) -> bool:
        return self._rule_flag(rule, "soft", False) or actual_ref in {self.resolve_ref(r) or r for r in self.model.soft_refs}

    def _region_bbox(self, name: Optional[str]) -> Optional[BBox]:
        if name is None:
            return None
        if name not in self.model.regions:
            raise PlacementError(f"unknown region {name!r}")
        return _rect_to_abs_bbox(self.model, self.model.regions[name])

    # Unit vectors for the four cardinal placement sides. "top"/"bottom" point toward
    # decreasing/increasing y because KiCad y increases downward on the board.
    _SIDE_VECTORS: Dict[str, Point] = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
    _NEARPAD_OFFSETS: Tuple[float, ...] = (0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.0, -2.0, 3.0, -3.0)

    def _edge_distance(self, bbox: BBox) -> Optional[float]:
        """Distance from bbox to the nearest board edge, or None if board geometry is unknown."""

        if self.board_geometry is None:
            return None
        g = self.board_geometry
        return min(bbox.min_x - g.min_x, g.max_x - bbox.max_x, bbox.min_y - g.min_y, g.max_y - bbox.max_y)

    def _clearance_margin(self, ref: str, x: float, y: float, rot: float,
                          clearance_override: Optional[float], allow_keepout_overlap: bool,
                          forbidden_bboxes: Optional[Sequence[Tuple[str, BBox]]] = None) -> Optional[float]:
        """Smallest clearance margin to obstacles, keepouts, and the board edge (positive is safe)."""

        if ref not in self.footprints:
            return None
        info = footprint_bbox_at(self.footprints[ref], x, y, rot, self.model)
        margins: List[float] = []
        edge = self._edge_distance(info.bbox)
        if edge is not None:
            margins.append(edge)
        # The clearance margin only feeds a score reward clamped to +/-5 mm, so
        # only footprints within that window (plus the largest clearance) can
        # affect the result.  Query the spatial index for those neighbours.
        index = self._ensure_spatial_index()
        window = self._max_clearance + 6.0
        ref_fp = self.footprints[ref]
        ref_class = _part_class_for(self.model, ref)
        for other in index.query(info.bbox.expanded(window)):
            if other == ref or other not in self.footprints or not _same_physical_side(ref_fp, self.footprints[other]):
                continue
            other_bbox = index.bbox_of(other)
            if other_bbox is None:
                ox, oy, orot = self.positions[other]
                other_bbox = footprint_bbox_at(self.footprints[other], ox, oy, orot, self.model).bbox
            required = clearance_override
            if required is None:
                required = self.model.clearance.required_for(ref_class, _part_class_for(self.model, other))
            margins.append(info.bbox.clearance_to(other_bbox) - required)
        if forbidden_bboxes:
            for _name, bbox in forbidden_bboxes:
                margins.append(info.bbox.clearance_to(bbox))
        if not allow_keepout_overlap:
            for keepout in keepout_rules(self.model):
                if bool(keepout.get("extra", {}).get("allow_keepout_overlap", False)):
                    continue
                margins.append(info.bbox.clearance_to(_rect_to_abs_bbox(self.model, keepout)))
        return min(margins) if margins else None

    def _corridor_penalty(self, ref: str, bbox: BBox) -> float:
        """Soft penalty for entering declared high-speed corridors unless this part is protection."""

        roles = {_part_class_for(self.model, ref).lower()}
        for rule in self.model.rules:
            rule_refs: List[str] = []
            if rule.get("ref") is not None:
                rule_refs.append(str(rule.get("ref")))
            rule_refs.extend(str(r) for r in rule.get("refs", []) or [])
            if ref in {self.resolve_ref(r) or r for r in rule_refs}:
                role_value = rule.get("role") or rule.get("extra", {}).get("role")
                if role_value is not None:
                    roles.add(str(role_value).lower().replace("-", "_"))
        if roles & {"esd", "protection", "tvs", "surge_protection"}:
            return 0.0
        ref_upper = ref.upper()
        if ref_upper.startswith(("D", "TVS", "ESD")):
            return 0.0
        penalty = 0.0
        for rule in self.model.rules:
            if rule.get("type") == "high_speed_path":
                members = {self.resolve_ref(r) or r for r in rule.get("sequence", [])}
                if ref in members:
                    continue
                width = float(rule.get("corridor_width", 3.0))
                for a_name, b_name in zip(rule.get("sequence", []), rule.get("sequence", [])[1:]):
                    a_ref = self.resolve_ref(a_name)
                    b_ref = self.resolve_ref(b_name)
                    if a_ref not in self.positions or b_ref not in self.positions:
                        continue
                    ax, ay, _ = self.positions[a_ref]
                    bx, by, _ = self.positions[b_ref]
                    if _bbox_intrudes_corridor(bbox, (ax, ay), (bx, by), width):
                        penalty += 25.0
                continue
            if rule.get("type") != "corridor":
                continue
            marker = " ".join(str(rule.get(k, "")) for k in ("name", "role", "note")).lower()
            if not any(token in marker for token in ("high", "hs", "usb", "diff", "rf")):
                continue
            a_ref = self.resolve_ref(str(rule.get("a")))
            b_ref = self.resolve_ref(str(rule.get("b")))
            if a_ref not in self.positions or b_ref not in self.positions:
                continue
            ax, ay, _ = self.positions[a_ref]
            bx, by, _ = self.positions[b_ref]
            width = float(rule.get("width", 0.0)) + 2.0 * float(rule.get("clearance", 0.0))
            if _bbox_intrudes_corridor(bbox, (ax, ay), (bx, by), width):
                penalty += 25.0
        return penalty

    def _shape_penalty_for_label(self, label: str) -> float:
        penalty = 0.0
        if "stagger" in label and "stagger=False" not in label:
            penalty += 0.35
        if "row" in label or "column" in label:
            penalty += 0.05
        if "fallback" in label:
            penalty += 5.0
        if "slide" in label or "radial" in label:
            penalty += 0.25
        return penalty

    def _evaluate_candidate(self, ref: str, x: float, y: float, rot: float, target: Point, label: str,
                            *, clearance_override: Optional[float], region: Optional[BBox],
                            allow_keepout_overlap: bool,
                            forbidden_bboxes: Optional[Sequence[Tuple[str, BBox]]] = None,
                            metadata: Optional[Dict[str, Any]] = None) -> SearchCandidate:
        """Score a single candidate location for legality, distance, clearance, routing, and simplicity."""

        reason = self._collides_at(ref, x, y, rot, clearance_override=clearance_override,
                                   region=region, allow_keepout_overlap=allow_keepout_overlap,
                                   forbidden_bboxes=forbidden_bboxes)
        legal = reason is None
        distance = math.hypot(x - target[0], y - target[1])
        margin = self._clearance_margin(ref, x, y, rot, clearance_override, allow_keepout_overlap, forbidden_bboxes)
        edge = None
        routing_penalty = 0.0
        region_penalty = 0.0
        high_speed_penalty = 0.0
        if ref in self.footprints:
            info = footprint_bbox_at(self.footprints[ref], x, y, rot, self.model)
            edge = self._edge_distance(info.bbox)
            # Prefer orthogonal/simple routing from the requested target over diagonal detours.
            dx, dy = abs(x - target[0]), abs(y - target[1])
            routing_penalty = min(dx, dy) * 0.15
            if region is not None and region.contains_bbox(info.bbox):
                region_penalty = 0.0
            elif region is not None:
                region_penalty = 100.0
            high_speed_penalty = self._corridor_penalty(ref, info.bbox)
        shape_penalty = self._shape_penalty_for_label(label)
        clearance_reward = min(max(margin if margin is not None else 0.0, -5.0), 5.0) * 0.35
        edge_reward = min(max(edge if edge is not None else 0.0, -5.0), 5.0) * 0.05
        score = distance + routing_penalty + region_penalty + shape_penalty + high_speed_penalty - clearance_reward - edge_reward
        if not legal:
            score += 1000.0
        return SearchCandidate(x, y, label, legal, reason, distance, margin, edge, score,
                               routing_penalty, region_penalty, shape_penalty, high_speed_penalty, metadata or {})

    def _search_candidates(self, ref: str, target: Point, rot: float, points: Sequence[Tuple[float, float, str]],
                           *, clearance_override: Optional[float], region: Optional[BBox],
                           allow_keepout_overlap: bool, search_radius_used: float,
                           fallback_used: bool = False,
                           forbidden_bboxes: Optional[Sequence[Tuple[str, BBox]]] = None) -> SearchOutcome:
        """Evaluate candidate points and choose the lowest-cost legal one. Records diagnostics on self.search_log."""

        attempted: List[SearchCandidate] = []
        seen: Set[Tuple[int, int]] = set()
        budget = self.runtime
        max_candidates = max(1, budget.max_candidates_per_rule)
        excellent = budget.excellent_threshold
        best_legal: Optional[SearchCandidate] = None
        self.profiler.incr("candidates_generated", len(points))
        evaluated = 0
        for x, y, label in points:
            key = (round(x / 1e-6), round(y / 1e-6))
            if key in seen:
                continue
            seen.add(key)
            # Bound the search: cap evaluations per rule so a single primitive
            # cannot explode into thousands of expensive collision checks.
            if evaluated >= max_candidates:
                break
            candidate = self._evaluate_candidate(ref, x, y, rot, target, label,
                                                 clearance_override=clearance_override, region=region,
                                                 allow_keepout_overlap=allow_keepout_overlap,
                                                 forbidden_bboxes=forbidden_bboxes)
            attempted.append(candidate)
            evaluated += 1
            self.profiler.incr("candidates_evaluated")
            if candidate.legal and (best_legal is None or candidate.score < best_legal.score):
                best_legal = candidate
                # Early success: once an excellent legal candidate is found, stop
                # searching for a marginally better one.
                if candidate.score <= excellent:
                    self.profiler.incr("candidates_early_accepted")
                    break
            # Honour the wall-clock budget mid-search; keep the best so far.
            if evaluated % 64 == 0 and budget.expired():
                self._raise_strict_timeout()
                break
        chosen = best_legal
        outcome = SearchOutcome(ref, target, attempted, chosen, search_radius_used, fallback_used)
        self.search_log[ref] = outcome
        return outcome

    def _mark_fallback(self, outcome: SearchOutcome, ref: str, x: float, y: float, rot: float,
                       *, clearance_override: Optional[float], region: Optional[BBox],
                       allow_keepout_overlap: bool,
                       forbidden_bboxes: Optional[Sequence[Tuple[str, BBox]]] = None) -> SearchOutcome:
        """Append a generic grid-search fallback candidate and mark the outcome as having used it."""

        candidate = self._evaluate_candidate(ref, x, y, rot, outcome.target, "fallback_grid_search",
                                             clearance_override=clearance_override, region=region,
                                             allow_keepout_overlap=allow_keepout_overlap,
                                             forbidden_bboxes=forbidden_bboxes)
        attempted = outcome.attempted + [candidate]
        chosen = candidate if candidate.legal else outcome.chosen
        new_outcome = SearchOutcome(ref, outcome.target, attempted, chosen, outcome.search_radius_used, True)
        self.search_log[ref] = new_outcome
        return new_outcome

    def _format_search_failure(self, ref: str, why: str, outcome: SearchOutcome, *, requested: Point) -> str:
        parts = [
            f"{ref!r} could not be placed by {why} without violating clearance or board bounds; "
            f"requested x={_fmt_num(requested[0])} y={_fmt_num(requested[1])}",
            f"attempted {len(outcome.attempted)} candidate(s)",
        ]
        best = outcome.best_candidate
        if best is not None:
            if best.legal:
                parts.append(f"best candidate: x={_fmt_num(best.x)} y={_fmt_num(best.y)} ({best.label})")
            else:
                parts.append(f"best candidate: x={_fmt_num(best.x)} y={_fmt_num(best.y)} ({best.label}); rejected: {best.reason}")
        nearest = outcome.nearest_legal
        if nearest is not None:
            parts.append(f"nearest legal location: x={_fmt_num(nearest.x)} y={_fmt_num(nearest.y)} ({nearest.label})")
        else:
            parts.append("no legal location found")
        parts.append(f"search_radius_used={_fmt_num(outcome.search_radius_used)}")
        parts.append(f"fallback_used={outcome.fallback_used}")
        return "; ".join(parts)

    def _resolve_unplaceable_search(self, actual_ref: str, why: str, outcome: SearchOutcome,
                                    requested: Point, rot: Optional[float], *,
                                    region: Optional[BBox], clearance_override: Optional[float],
                                    allow_keepout_overlap: bool) -> Tuple[float, float]:
        """No-abort handling when a single-part candidate search finds nothing.

        Shared by NearPad / Satellite / Between (and their auto-side variants):
        try a Level-2 reflow that frees movable support near the requested
        point, otherwise park.  Only strict / fail-fast actually raise.
        """

        primary_x, primary_y = requested
        eval_rot = rot if rot is not None else self.positions[actual_ref][2]
        if self.best_effort:
            placed = self._reflow_single(actual_ref, primary_x, primary_y, eval_rot,
                                         clearance_override=clearance_override, region=region,
                                         allow_keepout_overlap=allow_keepout_overlap, why=why)
            if placed is not None:
                return (placed[0], placed[1])
        detail = self._format_search_failure(actual_ref, why, outcome, requested=requested)
        if not self.best_effort:
            raise PlacementError(detail)
        self._park(actual_ref, primary_x, primary_y, why, reason=detail, penalty=200.0)
        return (primary_x, primary_y)

    def _board_inward_sides(self, x: float, y: float) -> List[str]:
        """Order cardinal sides preferring the direction toward the board interior."""

        if self.board_geometry is None:
            return ["top", "right", "bottom", "left"]
        g = self.board_geometry
        cx, cy = (g.min_x + g.max_x) / 2.0, (g.min_y + g.max_y) / 2.0
        horizontal = "right" if x <= cx else "left"
        vertical = "top" if y >= cy else "bottom"
        dist_x = min(x - g.min_x, g.max_x - x)
        dist_y = min(y - g.min_y, g.max_y - y)
        primary = [horizontal, vertical] if dist_x <= dist_y else [vertical, horizontal]
        remaining = [s for s in ("top", "right", "bottom", "left") if s not in primary]
        return primary + remaining

    def _near_pad_points(self, origin: Point, sides: Sequence[str], distance: float) -> List[Tuple[float, float, str]]:
        """Candidate points for NearPad/Satellite("auto"/"inward"): each side at `distance`, with small
        perpendicular offsets searched before falling back to the next side."""

        points: List[Tuple[float, float, str]] = []
        for side in sides:
            if side not in self._SIDE_VECTORS:
                raise PlacementError(f"Unknown side {side!r}")
            vx, vy = self._SIDE_VECTORS[side]
            tx, ty = -vy, vx
            bx, by = origin[0] + vx * distance, origin[1] + vy * distance
            for off in self._NEARPAD_OFFSETS:
                label = side if off == 0 else f"{side}{off:+g}"
                points.append((bx + tx * off, by + ty * off, label))
        return points

    def _satellite_base(self, px: float, py: float, side: str, dist: float, idx: int, pitch: float) -> Point:
        if side == "right":
            return px + dist, py + idx * pitch
        if side == "left":
            return px - dist, py + idx * pitch
        if side == "top":
            return px + idx * pitch, py - dist
        if side == "bottom":
            return px + idx * pitch, py + dist
        raise PlacementError(f"Unknown satellite side {side!r}")

    def _satellite_points(self, px: float, py: float, side: str, dist: float, idx: int, pitch: float,
                          dx: float, dy: float) -> List[Tuple[float, float, str]]:
        """Candidate points for a Satellite() with an explicit side: neighboring slots along the
        requested side, the same slot on alternate sides, and small perimeter offsets."""

        points: List[Tuple[float, float, str]] = []
        sides_order = [side] + [s for s in ("right", "left", "top", "bottom") if s != side]
        for s_index, s in enumerate(sides_order):
            slots = [0, -1, 1, -2, 2, -3, 3] if s_index == 0 else (0, -1, 1)
            for k in slots:
                bx, by = self._satellite_base(px, py, s, dist, idx + k, pitch)
                label = s if k == 0 else f"{s}:idx{idx + k}"
                points.append((bx + dx, by + dy, label))
        for s in ("right", "left", "top", "bottom"):
            vx, vy = self._SIDE_VECTORS[s]
            tx, ty = -vy, vx
            bx, by = self._satellite_base(px, py, s, dist, 0, pitch)
            for off in (0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.0, -2.0, 3.0, -3.0):
                points.append((bx + tx * off + dx, by + ty * off + dy, f"{s}{off:+g}"))
        return points

    def _between_points(self, rule: Mapping[str, Any], a: Point, b: Point) -> List[Tuple[float, float, str]]:
        """Candidate points for a Between() rule: normal-offset variations in both directions, and
        (with auto_spread=True) small shifts along the a->b line to spread grouped members out."""

        t0 = float(rule.get("t", 0.5))
        dx = float(rule.get("dx", 0.0))
        dy = float(rule.get("dy", 0.0))
        base_offset = rule.get("offset")
        base_offset = 0.0 if base_offset is None else float(base_offset)
        auto_spread = self._rule_flag(rule, "auto_spread", False)
        vx, vy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(vx, vy) or 1.0
        nx, ny = -vy / length, vx / length
        offsets = [base_offset]
        for d in (0.25, -0.25, 0.5, -0.5, 0.75, -0.75, 1.0, -1.0, 1.5, -1.5, 2.0, -2.0, 3.0, -3.0):
            offsets.append(base_offset + d)
        t_values = [t0]
        if auto_spread:
            for dt in (0.04, -0.04, 0.08, -0.08, 0.12, -0.12, 0.16, -0.16, 0.24, -0.24, 0.32, -0.32):
                t_values.append(max(0.0, min(1.0, t0 + dt)))
        points: List[Tuple[float, float, str]] = []
        for ti in t_values:
            bx = a[0] + (b[0] - a[0]) * ti + dx
            by = a[1] + (b[1] - a[1]) * ti + dy
            for off in offsets:
                label = f"t={ti:.3g},offset={off:+.3g}"
                points.append((bx + nx * off, by + ny * off, label))
        return points

    def _pad_centroid(self, parent_ref: str, pads: str | Sequence[str]) -> Point:
        actual_parent = self.resolve_ref(parent_ref)
        if actual_parent is None or actual_parent not in self.footprints:
            raise PlacementError(f"NearPad parent {parent_ref!r} is missing")
        names = [str(pads)] if isinstance(pads, str) else [str(p) for p in pads]
        if not names:
            raise PlacementError("NearPad(pad=...) requires at least one pad")
        centers = _cached_pad_centers(self.footprints[actual_parent])
        missing = [name for name in names if name not in centers]
        if missing:
            available = ", ".join(sorted(centers)) or "none"
            raise PlacementError(f"NearPad parent {actual_parent!r} has no pad {missing[0]!r}; available pads: {available}")
        px, py, prot = self.positions[actual_parent]
        abs_centers = []
        for name in names:
            rx, ry = _rot_point(centers[name][0], centers[name][1], prot)
            abs_centers.append((px + rx, py + ry))
        return (sum(p[0] for p in abs_centers) / len(abs_centers),
                sum(p[1] for p in abs_centers) / len(abs_centers))


    def _parent_forbidden_bboxes(self, parent_ref: Optional[str], *, clearance: float = 0.0) -> List[Tuple[str, BBox]]:
        if parent_ref is None:
            return []
        actual_parent = self.resolve_ref(parent_ref) or parent_ref
        if actual_parent not in self.footprints or actual_parent not in self.positions:
            return []
        px, py, prot = self.positions[actual_parent]
        bbox = footprint_bbox_at(self.footprints[actual_parent], px, py, prot, self.model).bbox.expanded(clearance)
        return [(f"parent bbox {actual_parent}", bbox)]

    def _place_near_point(self, rule: Mapping[str, Any], origin: Point, why: str) -> None:
        actual_ref = self.resolve_ref(rule["ref"])
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {rule['ref']!r} for {why}")
            return
        side = str(rule.get("side", "auto")).lower()
        distance = float(rule.get("distance", 1.5))
        if side in ("auto", "inward"):
            sides = self._board_inward_sides(origin[0], origin[1])
        elif side in self._SIDE_VECTORS:
            sides = [side] + [s for s in self._board_inward_sides(origin[0], origin[1]) if s != side]
        else:
            raise PlacementError(f"Unknown side {side!r}")

        rot = self.resolve_rot(rule.get("rot"))
        eval_rot = rot if rot is not None else self.positions[actual_ref][2]
        points = self._near_pad_points(origin, sides, distance)
        primary_x, primary_y, primary_label = points[0]

        if not self.model.policy.avoid_overlap:
            self.place(rule["ref"], primary_x, primary_y, rot, why, rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                       clearance_override=self._clearance_override(dict(rule)), rule=rule)
            return

        region = None if self.allow_outside_region else self._region_bbox(self._rule_value(rule, "region"))
        clearance_override = self._clearance_override(dict(rule))
        allow_keepout_overlap = self.allow_keepout_overlap or self._rule_flag(rule, "allow_keepout_overlap", False)
        forbidden_bboxes = self._parent_forbidden_bboxes(str(rule.get("parent")) if rule.get("parent") else None)
        search_radius = distance + max(abs(o) for o in self._NEARPAD_OFFSETS)

        outcome = self._search_candidates(actual_ref, (primary_x, primary_y), eval_rot, points,
                                          clearance_override=clearance_override, region=region,
                                          allow_keepout_overlap=allow_keepout_overlap,
                                          search_radius_used=search_radius,
                                          forbidden_bboxes=forbidden_bboxes)
        chosen = outcome.chosen
        if chosen is None:
            fallback = self._find_non_overlapping_position(actual_ref, primary_x, primary_y, eval_rot,
                                                            clearance_override=clearance_override,
                                                            candidate_sides=sides, region=region,
                                                            allow_keepout_overlap=allow_keepout_overlap)
            if fallback is not None:
                outcome = self._mark_fallback(outcome, actual_ref, fallback[0], fallback[1], eval_rot,
                                              clearance_override=clearance_override, region=region,
                                              allow_keepout_overlap=allow_keepout_overlap,
                                              forbidden_bboxes=forbidden_bboxes)
                chosen = outcome.chosen
        if chosen is None:
            px, py = self._resolve_unplaceable_search(
                actual_ref, why, outcome, (primary_x, primary_y), rot,
                region=region, clearance_override=clearance_override,
                allow_keepout_overlap=allow_keepout_overlap)
            self.place(rule["ref"], px, py, rot, why, rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                       clearance_override=clearance_override, rule=rule)
            return

        self.place(rule["ref"], chosen.x, chosen.y, rot, why, rule.get("note"),
                   allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                   clearance_override=clearance_override, rule=rule)
        if chosen.label != primary_label:
            self.auto_adjustments.append(AutoAdjustment(actual_ref, primary_x, primary_y, chosen.x, chosen.y,
                                                         f"search candidate {chosen.label}"))
            self.messages.append(Message("note", f"adjusted {actual_ref}: requested x={_fmt_num(primary_x)} "
                                         f"y={_fmt_num(primary_y)} placed x={_fmt_num(chosen.x)} y={_fmt_num(chosen.y)}; "
                                         f"search candidate {chosen.label}"))

    def _place_satellite(self, rule: Mapping[str, Any], px: float, py: float, side: str, dist: float,
                         idx: int, pitch: float, dx: float, dy: float) -> None:
        actual_ref = self.resolve_ref(rule["ref"])
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {rule['ref']!r} for satellite")
            return
        rot = self.resolve_rot(rule.get("rot"))
        eval_rot = rot if rot is not None else self.positions[actual_ref][2]
        points = self._satellite_points(px, py, side, dist, idx, pitch, dx, dy)
        primary_x, primary_y, primary_label = points[0]

        if not self.model.policy.avoid_overlap:
            self.place(rule["ref"], primary_x, primary_y, rot, "satellite", rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                       clearance_override=self._clearance_override(dict(rule)), rule=rule)
            return

        region = None if self.allow_outside_region else self._region_bbox(self._rule_value(rule, "region"))
        clearance_override = self._clearance_override(dict(rule))
        allow_keepout_overlap = self.allow_keepout_overlap or self._rule_flag(rule, "allow_keepout_overlap", False)
        forbidden_bboxes = self._parent_forbidden_bboxes(str(rule.get("parent")) if rule.get("parent") else None)
        search_radius = dist + abs(idx) * pitch + 2.0

        outcome = self._search_candidates(actual_ref, (primary_x, primary_y), eval_rot, points,
                                          clearance_override=clearance_override, region=region,
                                          allow_keepout_overlap=allow_keepout_overlap,
                                          search_radius_used=search_radius,
                                          forbidden_bboxes=forbidden_bboxes)
        chosen = outcome.chosen
        if chosen is None:
            fallback = self._find_non_overlapping_position(actual_ref, primary_x, primary_y, eval_rot,
                                                            clearance_override=clearance_override,
                                                            candidate_sides=[side], region=region,
                                                            allow_keepout_overlap=allow_keepout_overlap)
            if fallback is not None:
                outcome = self._mark_fallback(outcome, actual_ref, fallback[0], fallback[1], eval_rot,
                                              clearance_override=clearance_override, region=region,
                                              allow_keepout_overlap=allow_keepout_overlap,
                                              forbidden_bboxes=forbidden_bboxes)
                chosen = outcome.chosen
        if chosen is None:
            sx, sy = self._resolve_unplaceable_search(
                actual_ref, "satellite", outcome, (primary_x, primary_y), rot,
                region=region, clearance_override=clearance_override,
                allow_keepout_overlap=allow_keepout_overlap)
            self.place(rule["ref"], sx, sy, rot, "satellite", rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                       clearance_override=clearance_override, rule=rule)
            return

        self.place(rule["ref"], chosen.x, chosen.y, rot, "satellite", rule.get("note"),
                   allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                   clearance_override=clearance_override, rule=rule)
        if chosen.label != primary_label:
            self.auto_adjustments.append(AutoAdjustment(actual_ref, primary_x, primary_y, chosen.x, chosen.y,
                                                         f"search candidate {chosen.label}"))
            self.messages.append(Message("note", f"adjusted {actual_ref}: requested x={_fmt_num(primary_x)} "
                                         f"y={_fmt_num(primary_y)} placed x={_fmt_num(chosen.x)} y={_fmt_num(chosen.y)}; "
                                         f"search candidate {chosen.label}"))

    def _place_between(self, rule: Mapping[str, Any], a: Point, b: Point) -> None:
        actual_ref = self.resolve_ref(rule["ref"])
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {rule['ref']!r} for between")
            return
        rot = self.resolve_rot(rule.get("rot"), a, b)
        eval_rot = rot if rot is not None else self.positions[actual_ref][2]
        points = self._between_points(rule, a, b)
        primary_x, primary_y, primary_label = points[0]

        if not self.model.policy.avoid_overlap:
            self.place(rule["ref"], primary_x, primary_y, rot, "between", rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                       clearance_override=self._clearance_override(rule), rule=rule)
            return

        region = None if self.allow_outside_region else self._region_bbox(self._rule_value(rule, "region"))
        clearance_override = self._clearance_override(rule)
        allow_keepout_overlap = self.allow_keepout_overlap or self._rule_flag(rule, "allow_keepout_overlap", False)
        search_radius = self.model.policy.max_search_radius

        outcome = self._search_candidates(actual_ref, (primary_x, primary_y), eval_rot, points,
                                          clearance_override=clearance_override, region=region,
                                          allow_keepout_overlap=allow_keepout_overlap,
                                          search_radius_used=search_radius)
        chosen = outcome.chosen
        if chosen is None:
            fallback = self._find_non_overlapping_position(actual_ref, primary_x, primary_y, eval_rot,
                                                            clearance_override=clearance_override,
                                                            candidate_sides=None, region=region,
                                                            allow_keepout_overlap=allow_keepout_overlap)
            if fallback is not None:
                outcome = self._mark_fallback(outcome, actual_ref, fallback[0], fallback[1], eval_rot,
                                              clearance_override=clearance_override, region=region,
                                              allow_keepout_overlap=allow_keepout_overlap)
                chosen = outcome.chosen
        if chosen is None:
            bx2, by2 = self._resolve_unplaceable_search(
                actual_ref, "between", outcome, (primary_x, primary_y), rot,
                region=region, clearance_override=clearance_override,
                allow_keepout_overlap=allow_keepout_overlap)
            self.place(rule["ref"], bx2, by2, rot, "between", rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                       clearance_override=clearance_override, rule=rule)
            return

        self.place(rule["ref"], chosen.x, chosen.y, rot, "between", rule.get("note"),
                   allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                   clearance_override=clearance_override, rule=rule)
        if chosen.label != primary_label:
            self.auto_adjustments.append(AutoAdjustment(actual_ref, primary_x, primary_y, chosen.x, chosen.y,
                                                         f"search candidate {chosen.label}"))
            self.messages.append(Message("note", f"adjusted {actual_ref}: requested x={_fmt_num(primary_x)} "
                                         f"y={_fmt_num(primary_y)} placed x={_fmt_num(chosen.x)} y={_fmt_num(chosen.y)}; "
                                         f"search candidate {chosen.label}"))

    def _fmt_bbox(self, bbox: BBox) -> str:
        return (f"min=({_fmt_num(bbox.min_x)}, {_fmt_num(bbox.min_y)}) "
                f"max=({_fmt_num(bbox.max_x)}, {_fmt_num(bbox.max_y)})")

    def _nearest_bbox_side(self, point: Point, bbox: BBox) -> str:
        x, y = point
        distances = {
            "left": abs(x - bbox.min_x),
            "right": abs(x - bbox.max_x),
            "top": abs(y - bbox.min_y),
            "bottom": abs(y - bbox.max_y),
        }
        return min(distances, key=distances.get)

    def _ordered_array_sides(self, side: str, origin: Point, parent_bbox: Optional[BBox],
                             pad_side: Optional[str]) -> List[str]:
        side = side.lower().replace("-", "_")
        if side == "auto":
            preferred: List[str] = []
            if pad_side is not None:
                preferred.append(pad_side)
            preferred.extend(self._board_inward_sides(origin[0], origin[1]))
        elif side == "inward":
            if parent_bbox is not None:
                cx = (parent_bbox.min_x + parent_bbox.max_x) / 2.0
                cy = (parent_bbox.min_y + parent_bbox.max_y) / 2.0
                preferred = self._board_inward_sides(cx, cy)
            else:
                preferred = self._board_inward_sides(origin[0], origin[1])
        elif side in self._SIDE_VECTORS:
            preferred = [side] + [s for s in self._board_inward_sides(origin[0], origin[1]) if s != side]
        else:
            raise PlacementError(f"Unknown side {side!r}")
        out: List[str] = []
        for s in preferred + ["top", "right", "bottom", "left"]:
            if s not in out:
                out.append(s)
        return out

    def _part_half_size_at_rot(self, ref: str, rot: float) -> Tuple[float, float]:
        fp = self.footprints[ref]
        info = footprint_bbox_at(fp, 0.0, 0.0, rot, self.model)
        return info.bbox.width / 2.0, info.bbox.height / 2.0

    def _array_candidates(self, origin: Point, side: str, distance: float, spacing: float,
                          n: int) -> List[Tuple[float, float]]:
        """Backward-compatible centered row of `n` candidate points near `origin`."""

        vx, vy = self._SIDE_VECTORS[side]
        tx, ty = -vy, vx
        anchor = (origin[0] + vx * distance, origin[1] + vy * distance)
        return [(anchor[0] + tx * spacing * (i - (n - 1) / 2.0),
                 anchor[1] + ty * spacing * (i - (n - 1) / 2.0)) for i in range(n)]


    def _array_item_bboxes(self, actual_refs: Sequence[Optional[str]], candidates: Sequence[Tuple[float, float]],
                           rot: Optional[float]) -> List[BBox]:
        boxes: List[BBox] = []
        for actual, (x, y) in zip(actual_refs, candidates):
            if actual is None or actual not in self.footprints:
                boxes.append(BBox(x, y, x, y))
                continue
            eval_rot = rot if rot is not None else self.positions[actual][2]
            boxes.append(footprint_bbox_at(self.footprints[actual], x, y, eval_rot, self.model).bbox)
        return boxes

    def _placement_region_for_side(self, owner_ref: str, owner_bbox: Optional[BBox], expanded: Optional[BBox],
                                   side: str, requested_region: Optional[BBox], role: str) -> Optional[PlacementRegion]:
        board = _geometry_bbox(self.board_geometry)
        if owner_bbox is None or expanded is None or board is None:
            return None
        if side == "left":
            raw = BBox(board.min_x, board.min_y, expanded.min_x, board.max_y)
            preferred_axis, cross_axis = "y", "x"
        elif side == "right":
            raw = BBox(expanded.max_x, board.min_y, board.max_x, board.max_y)
            preferred_axis, cross_axis = "y", "x"
        elif side == "top":
            raw = BBox(board.min_x, board.min_y, board.max_x, expanded.min_y)
            preferred_axis, cross_axis = "x", "y"
        elif side == "bottom":
            raw = BBox(board.min_x, expanded.max_y, board.max_x, board.max_y)
            preferred_axis, cross_axis = "x", "y"
        else:
            return None
        legal = BBox(max(raw.min_x, board.min_x), max(raw.min_y, board.min_y),
                     min(raw.max_x, board.max_x), min(raw.max_y, board.max_y))
        if requested_region is not None:
            legal = BBox(max(legal.min_x, requested_region.min_x), max(legal.min_y, requested_region.min_y),
                         min(legal.max_x, requested_region.max_x), min(legal.max_y, requested_region.max_y))
        if legal.max_x < legal.min_x:
            legal = BBox(legal.min_x, legal.min_y, legal.min_x, legal.max_y)
        if legal.max_y < legal.min_y:
            legal = BBox(legal.min_x, legal.min_y, legal.max_x, legal.min_y)
        capacity_length = legal.width if preferred_axis == "x" else legal.height
        return PlacementRegion(owner_ref, owner_bbox, expanded, side, legal, preferred_axis, cross_axis,
                               capacity_length, max(0.0, legal.width) * max(0.0, legal.height), role)

    def _capacity_metadata(self, n: int, side: str, spacing: float, region: Optional[PlacementRegion],
                           staggered: bool, max_per_row: Optional[int]) -> Dict[str, Any]:
        if n <= 0:
            single = 0
        elif region is None:
            single = n
        else:
            single = max(1, int(math.floor((region.capacity_length + 1e-9) / max(spacing, 1e-9))) + 1)
        per_row = max_per_row or (min(3, max(1, math.ceil(n / 2))) if staggered else n)
        rows = max(1, math.ceil(n / max(1, per_row)))
        return {"single_row_capacity": single, "required_refs": n, "rows": rows,
                "capacity_sufficient": single >= n if not staggered else single * rows >= n,
                "region_capacity_length": None if region is None else round(region.capacity_length, 6),
                "region_capacity_area": None if region is None else round(region.capacity_area, 6)}

    def _array_layout_candidates(self, refs: Sequence[str], actual_refs: Sequence[Optional[str]], origin: Point,
                                 side: str, distance: float, spacing: float, *, shift: float,
                                 staggered: bool, parent_expanded: Optional[BBox], rot: Optional[float],
                                 max_per_row: Optional[int]) -> List[Tuple[float, float]]:
        """Generate a complete DecouplingArray candidate outside the parent keepout.

        `origin` supplies the tangent coordinate (usually the selected pad). The normal
        coordinate is anchored from the expanded parent bbox so item bodies remain
        outside the IC footprint instead of centered on the pad.
        """

        n = len(refs)
        if n == 0:
            return []
        horizontal_edge = side in {"top", "bottom"}
        outward = -1.0 if side in {"left", "top"} else 1.0
        if staggered:
            per_row = max_per_row or min(3, max(1, math.ceil(n / 2)))
            per_row = max(1, min(per_row, n))
        else:
            per_row = n

        points: List[Tuple[float, float]] = []
        for idx, actual in enumerate(actual_refs):
            row = idx // per_row
            col = idx % per_row
            row_count = min(per_row, n - row * per_row)
            tangent = spacing * (col - (row_count - 1) / 2.0) + shift
            if staggered and row % 2 == 1:
                tangent += spacing / 2.0
            eval_rot = rot if rot is not None else (self.positions[actual][2] if actual is not None else 0.0)
            half_w = half_h = 0.5
            if actual is not None and actual in self.footprints:
                half_w, half_h = self._part_half_size_at_rot(actual, eval_rot)
            row_gap = row * spacing
            if parent_expanded is None:
                vx, vy = self._SIDE_VECTORS[side]
                tx, ty = -vy, vx
                anchor = (origin[0] + vx * (distance + row_gap), origin[1] + vy * (distance + row_gap))
                points.append((anchor[0] + tx * tangent, anchor[1] + ty * tangent))
            elif horizontal_edge:
                y = (parent_expanded.min_y - distance - row_gap - half_h
                     if side == "top" else parent_expanded.max_y + distance + row_gap + half_h)
                x = origin[0] + tangent
                points.append((x, y))
            else:
                x = (parent_expanded.min_x - distance - row_gap - half_w
                     if side == "left" else parent_expanded.max_x + distance + row_gap + half_w)
                y = origin[1] + tangent
                points.append((x, y))
        return points

    def _try_grouped_spread(self, refs: Sequence[str], actual_refs: Sequence[Optional[str]],
                            candidates: Sequence[Tuple[float, float]], rot: Optional[float],
                            *, clearance_override: Optional[float], region: Optional[BBox],
                            allow_keepout_overlap: bool, parent_expanded: Optional[BBox] = None) -> Dict[str, Any]:
        """Tentatively place each member of a group and reject any illegal candidate."""

        saved: Dict[str, Tuple[float, float, float]] = {}
        try:
            for ref, actual, (x, y) in zip(refs, actual_refs, candidates):
                if actual is None:
                    continue
                eval_rot = rot if rot is not None else self.positions[actual][2]
                if parent_expanded is not None and actual in self.footprints:
                    test_info = footprint_bbox_at(self.footprints[actual], x, y, eval_rot, self.model)
                    if test_info.bbox.overlaps(parent_expanded):
                        return {"ok": False, "failed_ref": ref,
                                "reason": "would overlap expanded parent bbox"}
                reason = self._collides_at(actual, x, y, eval_rot, clearance_override=clearance_override,
                                           region=region, allow_keepout_overlap=allow_keepout_overlap)
                if reason is not None:
                    return {"ok": False, "failed_ref": ref, "reason": reason}
                saved[actual] = self.positions[actual]
                self._set_position(actual, x, y, eval_rot)
            return {"ok": True}
        finally:
            for actual, pos in saved.items():
                self._set_position(actual, *pos)


    def _score_grouped_array_attempt(self, refs: Sequence[str], actual_refs: Sequence[Optional[str]],
                                     candidates: Sequence[Tuple[float, float]], rot: Optional[float],
                                     target: Point, label: str, result: Mapping[str, Any],
                                     metadata: Dict[str, Any]) -> SearchCandidate:
        anchor = candidates[len(candidates) // 2] if candidates else target
        legal = bool(result.get("ok"))
        distances = [math.hypot(x - target[0], y - target[1]) for x, y in candidates] or [0.0]
        distance = sum(distances) / len(distances)
        margins: List[float] = []
        edge_values: List[float] = []
        hs_penalty = 0.0
        for actual, (x, y) in zip(actual_refs, candidates):
            if actual is None or actual not in self.footprints:
                continue
            eval_rot = rot if rot is not None else self.positions[actual][2]
            margin = self._clearance_margin(actual, x, y, eval_rot, None, False)
            if margin is not None:
                margins.append(margin)
            info = footprint_bbox_at(self.footprints[actual], x, y, eval_rot, self.model)
            edge = self._edge_distance(info.bbox)
            if edge is not None:
                edge_values.append(edge)
            hs_penalty += self._corridor_penalty(actual, info.bbox)
        margin = min(margins) if margins else None
        edge = min(edge_values) if edge_values else None
        shape_penalty = self._shape_penalty_for_label(label)
        if metadata.get("shape") == "stagger":
            shape_penalty += 0.35
        if metadata.get("shape") in {"row", "column"}:
            shape_penalty += 0.05
        shape_penalty += float(metadata.get("side_rank", 0)) * 2.0
        clearance_reward = min(max(margin if margin is not None else 0.0, -5.0), 5.0) * 0.35
        edge_reward = min(max(edge if edge is not None else 0.0, -5.0), 5.0) * 0.05
        score = distance + shape_penalty + hs_penalty - clearance_reward - edge_reward
        if not legal:
            score += 1000.0
        return SearchCandidate(anchor[0], anchor[1], label, legal, None if legal else str(result.get("reason")),
                               distance, margin, edge, score, 0.0, 0.0, shape_penalty, hs_penalty, metadata)

    # ------------------------------------------------------------------
    # Reflow + floorplanning
    #
    # A local placement failure is treated as evidence that the surrounding
    # floorplan is suboptimal, not that the failing component is at fault.  The
    # reflow ladder progressively relaxes the neighborhood -- relocating movable
    # support passives, then nudging a movable parent -- before parking as a
    # last resort.  Mechanical anchors, locked parts, and edge-required
    # connectors are never moved.
    # ------------------------------------------------------------------

    def _role_index(self) -> Tuple[Dict[str, str], Set[str]]:
        """Map each resolved ref to its declared role and collect edge-required refs."""

        cached = getattr(self, "_role_index_cache", None)
        if cached is not None:
            return cached
        roles: Dict[str, str] = {}
        edge_required: Set[str] = set()
        for rule in self.model.rules:
            ref = rule.get("ref")
            if rule.get("type") == "cluster":
                ref = rule.get("anchor")
                placement = rule.get("placement") or {}
                if isinstance(placement, Mapping) and self._rule_flag(placement, "edge_required", False):
                    anchor = self.resolve_ref(str(ref)) if ref else None
                    if anchor is not None:
                        edge_required.add(anchor)
                role = rule.get("role")
                actual = self.resolve_ref(str(ref)) if ref else None
                if actual is not None and role:
                    roles.setdefault(actual, str(role))
                continue
            if ref is None:
                continue
            actual = self.resolve_ref(str(ref))
            if actual is None:
                continue
            role = rule.get("role")
            if role:
                roles[actual] = str(role)
            if self._rule_flag(rule, "edge_required", False):
                edge_required.add(actual)
        self._role_index_cache = (roles, edge_required)
        return self._role_index_cache

    _IMMOVABLE_ROLES = {"mechanical", "connector", "high_speed_connector", "rf", "rf_module", "antenna"}

    def _is_movable_support(self, ref: str) -> bool:
        """True if ref is a support passive the reflow engine may relocate freely."""

        if ref in self.locked or ref not in self.footprints:
            return False
        roles, edge_required = self._role_index()
        if ref in edge_required:
            return False
        if roles.get(ref) in self._IMMOVABLE_ROLES:
            return False
        # Movable support: noncritical passives, testpoints, LEDs, pullups, straps.
        return _part_class_for(self.model, ref) in {"passive", "testpoint"}

    def _parent_movable(self, parent_ref: str) -> bool:
        """True if a parent may be nudged during reflow (Level 5)."""

        actual = self.resolve_ref(parent_ref) or parent_ref
        if actual in self.locked or actual not in self.footprints:
            return False
        roles, edge_required = self._role_index()
        if actual in edge_required or roles.get(actual) in {"mechanical", "connector", "high_speed_connector", "rf", "antenna"}:
            return False
        # Only move a parent when the floorplan policy explicitly allows anchor
        # movement, or the parent was marked soft/movable.
        return bool(self.model.policy.allow_anchor_move) or actual in self.model.soft_refs

    def _snapshot_positions(self) -> Tuple[Dict[str, Tuple[float, float, float]], Dict[str, PlacementUpdate]]:
        return (dict(self.positions), dict(self.updates))

    def _restore_positions(self, snapshot: Tuple[Dict[str, Tuple[float, float, float]], Dict[str, PlacementUpdate]]) -> None:
        self.positions = dict(snapshot[0])
        self.updates = dict(snapshot[1])
        self._touch_positions()

    def _cur_bbox(self, ref: str) -> BBox:
        x, y, rot = self.positions[ref]
        return footprint_bbox_at(self.footprints[ref], x, y, rot, self.model).bbox

    def _apply_reflow_move(self, ref: str, x: float, y: float, why: str) -> None:
        """Record a reflow relocation as a placement update (without overlap search)."""

        fp = self.footprints[ref]
        _, _, rot = self.positions.get(ref, (fp.x, fp.y, fp.rot))
        self._set_position(ref, float(x), float(y), float(rot))
        outside = self.board_geometry is not None and not self.board_geometry.contains(x, y)
        self.updates[ref] = PlacementUpdate(
            ref=ref, x=float(x), y=float(y), final_rot=float(rot),
            write_rot=None if abs(rot - fp.rot) < 1e-9 else float(rot),
            why=why, note="reflow", original_x=fp.x, original_y=fp.y, original_rot=fp.rot,
            rotation_changed=abs(fp.rot - rot) > 1e-9, outside_board=outside,
            delta_x=float(x) - fp.x, delta_y=float(y) - fp.y)
        self.messages.append(Message("note", f"reflow moved {ref} -> x={_fmt_num(x)} y={_fmt_num(y)}  {why}"))

    def _relocate_out_of(self, ref: str, area: BBox, rot: float) -> Optional[Point]:
        """Find a legal location for ref whose body no longer overlaps `area`."""

        x, y, _ = self.positions[ref]
        acx = (area.min_x + area.max_x) / 2.0
        acy = (area.min_y + area.max_y) / 2.0
        away = (1.0 if x >= acx else -1.0, 1.0 if y >= acy else -1.0)
        directions = sorted(
            [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0), (1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0)],
            key=lambda v: -(v[0] * away[0] + v[1] * away[1]))
        step = max(self.model.policy.search_step, 0.25)
        reach = max(area.width, area.height, 1.0) + self.model.policy.max_search_radius
        k = 1
        while k * step <= reach:
            d = k * step
            for vx, vy in directions:
                tx, ty = x + vx * d, y + vy * d
                fp = self.footprints[ref]
                bbox = footprint_bbox_at(fp, tx, ty, rot, self.model).bbox
                if bbox.overlaps(area):
                    continue
                if self._collides_at(ref, tx, ty, rot) is None:
                    return (tx, ty)
            k += 1
        return None

    def _free_area(self, area: BBox, exclude: Set[str]) -> List[str]:
        """Relocate movable support passives whose body overlaps `area`."""

        moved: List[str] = []
        for ref in sorted(self.positions):
            if ref in exclude or not self._is_movable_support(ref):
                continue
            if not self._cur_bbox(ref).overlaps(area):
                continue
            dest = self._relocate_out_of(ref, area, self.positions[ref][2])
            if dest is not None:
                self._apply_reflow_move(ref, dest[0], dest[1], "reflow: clear placement region")
                moved.append(ref)
        return moved

    def _array_clear_area(self, search: ArraySearch) -> BBox:
        """The region that must be freed for the array's best candidate to land.

        Targets the footprint area of the best-scoring (but blocked) attempt so
        reflow relocates exactly the obstructing support parts, rather than
        clearing the whole neighborhood.
        """

        best: Optional[Dict[str, Any]] = None
        for attempt in search.attempts:
            if attempt.get("candidates") and (best is None or attempt["score"] < best["score"]):
                best = attempt
        if best is not None:
            actual_refs = [self.resolve_ref(r) or r for r in search.refs]
            boxes = self._array_item_bboxes(actual_refs, best["candidates"], search.rot)
            union = _union_bbox([b for b in boxes if b is not None])
            if union is not None:
                return union.expanded(self.model.clearance.default + 0.5)
        if search.parent_expanded is not None:
            return search.parent_expanded
        if search.region is not None:
            return search.region
        sx, sy = search.side_origin
        reach = max(2.0, search.requested_spacing * max(1, len(search.refs)))
        return BBox(sx - reach, sy - reach, sx + reach, sy + reach)

    def _reflow_array(self, rule: Mapping[str, Any], origin: Point, search: ArraySearch) -> bool:
        """Reflow the neighborhood so a failed array can place. Returns True on success."""

        trigger = search.refs[0]
        why = search.why
        score_before = self.floorplan_score()["score"]
        exclude = {self.resolve_ref(r) or r for r in search.refs}
        if search.parent_ref is not None:
            exclude.add(self.resolve_ref(search.parent_ref) or search.parent_ref)

        # Levels 2-4: relocate movable support parts out of the placement region.
        snapshot = self._snapshot_positions()
        moved = self._free_area(self._array_clear_area(search), exclude)
        if moved:
            retry = self._attempt_array_placement(rule, search.refs, origin, why,
                                                  parent_ref=search.parent_ref, pad_origin=search.pad_origin)
            if retry.succeeded is not None:
                self._commit_array(rule, retry)
                self.reflow_attempts.append(ReflowAttempt(
                    trigger, why, level=3,
                    strategy="relocated movable support parts to free the placement region",
                    moved_components=moved, score_before=score_before,
                    score_after=self.floorplan_score()["score"], resolved=True))
                return True
            self._restore_positions(snapshot)

        # Level 5: nudge a movable parent if policy allows.
        if search.parent_ref is not None and self._parent_movable(search.parent_ref):
            parent, retry = self._reflow_move_parent(rule, origin, search)
            if retry is not None and retry.succeeded is not None:
                self._commit_array(rule, retry)
                self.reflow_attempts.append(ReflowAttempt(
                    trigger, why, level=5, strategy=f"moved movable parent {parent}",
                    moved_parents=[parent], score_before=score_before,
                    score_after=self.floorplan_score()["score"], resolved=True))
                return True
            # Record that the floorplanner considered (but could not achieve) a
            # parent move, so "considers moving the parent if allowed" is visible.
            self.reflow_attempts.append(ReflowAttempt(
                trigger, why, level=5,
                strategy=f"considered moving movable parent {parent}; no legal layout found",
                score_before=score_before, score_after=score_before, resolved=False))

        self.reflow_attempts.append(ReflowAttempt(
            trigger, why, level=7, strategy="reflow ladder exhausted; parking",
            score_before=score_before, score_after=score_before, resolved=False))
        return False

    def _reflow_move_parent(self, rule: Mapping[str, Any], origin: Point,
                            search: ArraySearch) -> Tuple[str, Optional[ArraySearch]]:
        parent = self.resolve_ref(search.parent_ref) or str(search.parent_ref)
        if parent not in self.positions:
            return parent, None
        px, py, prot = self.positions[parent]
        snapshot = self._snapshot_positions()
        # Bounded trial grid: re-running the full array search per parent
        # position is expensive, so keep the candidate set small.
        for vx, vy in [(0.0, -1.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0)]:
            for d in (2.0, 4.0, 6.0):
                nx, ny = px + vx * d, py + vy * d
                if self._collides_at(parent, nx, ny, prot) is not None:
                    continue
                self._apply_reflow_move(parent, nx, ny, f"reflow: move parent for {search.why}")
                new_origin = (origin[0] + (nx - px), origin[1] + (ny - py))
                new_pad = None if search.pad_origin is None else (search.pad_origin[0] + (nx - px), search.pad_origin[1] + (ny - py))
                retry = self._attempt_array_placement(rule, search.refs, new_origin, search.why,
                                                      parent_ref=search.parent_ref, pad_origin=new_pad, quick=True)
                if retry.succeeded is not None:
                    return parent, retry
                self._restore_positions(snapshot)
        return parent, None

    def _park_array(self, rule: Mapping[str, Any], search: ArraySearch) -> None:
        """Last-resort parking for an unplaceable array (no-abort behavior)."""

        self._emit_array_side_notes(search)
        refs = search.refs
        coords: Optional[List[Tuple[float, float]]] = None
        failed = [a for a in search.attempts if not a.get("ok") and a.get("candidates")]
        if failed:
            best = min(failed, key=lambda a: math.hypot(a["anchor"][0] - search.side_origin[0],
                                                         a["anchor"][1] - search.side_origin[1]))
            coords = list(best.get("candidates") or [])
        if not coords:
            sx, sy = search.side_origin
            coords = [(sx + i * max(search.requested_spacing, 1.0), sy) for i in range(len(refs))]
        for ref, (x, y) in zip(refs, coords):
            actual = self.resolve_ref(ref) or ref
            self.place(ref, x, y, search.rot, search.why + " (parked)", rule.get("note"),
                       allow_arbitrary_rotation=search.allow_arbitrary_rotation, rule=rule)
            if actual in self.footprints:
                self.parked[actual] = ParkedPlacement(
                    ref=actual, why=search.why,
                    reason="no legal grouped-array layout after reflow", x=float(x), y=float(y),
                    penalty=250.0)
        warn = (f"{refs[0]} could not be placed legally by {search.why} after reflow; "
                f"parked {len(refs)} part(s) at best-effort locations (review required)")
        self.degraded_warnings.append(warn)
        self.messages.append(Message("warn", warn))

    def _reflow_single(self, ref: str, x: float, y: float, rot: float, *,
                       clearance_override: Optional[float], region: Optional[BBox],
                       allow_keepout_overlap: bool, why: str) -> Optional[Tuple[float, float, str]]:
        """Relocate movable support parts near (x, y) so a single part can place."""

        fp = self.footprints[ref]
        area = footprint_bbox_at(fp, x, y, rot, self.model).bbox.expanded(max(1.0, self.model.clearance.default))
        score_before = self.floorplan_score()["score"]
        snapshot = self._snapshot_positions()
        moved = self._free_area(area, exclude={ref})
        if not moved:
            return None
        placed = self._find_non_overlapping_position(ref, x, y, rot, clearance_override=clearance_override,
                                                     candidate_sides=None, region=region,
                                                     allow_keepout_overlap=allow_keepout_overlap)
        if placed is not None:
            self.reflow_attempts.append(ReflowAttempt(
                ref, why, level=2, strategy="relocated movable support parts near requested location",
                moved_components=moved, score_before=score_before,
                score_after=self.floorplan_score()["score"], resolved=True))
            return placed
        self._restore_positions(snapshot)
        return None

    def _park(self, ref: str, x: float, y: float, why: str, *, reason: str, penalty: float) -> None:
        self.parked[ref] = ParkedPlacement(ref=ref, why=why, reason=reason, x=float(x), y=float(y), penalty=penalty)
        warn = (f"{ref} could not be placed legally by {why} after reflow; parked at best-effort "
                f"location x={_fmt_num(x)} y={_fmt_num(y)} (review required)")
        self.degraded_warnings.append(warn)
        self.messages.append(Message("warn", warn))

    # ------------------------------------------------------------------
    # Global floorplan scoring + iterative optimization
    # ------------------------------------------------------------------

    def congestion_map(self, *, cells: int = 6) -> Dict[str, Any]:
        """Coarse occupancy grid over the board used as a congestion proxy."""

        geom = self.board_geometry
        if geom is not None:
            min_x, min_y, max_x, max_y = geom.min_x, geom.min_y, geom.max_x, geom.max_y
        else:
            b = footprint_bounds({ref: dataclasses.replace(self.footprints[ref], x=self.positions[ref][0], y=self.positions[ref][1])
                                  for ref in self.updates}) if self.updates else None
            if not b:
                return {"cells": cells, "grid": [], "peak": 0, "peak_penalty": 0.0}
            min_x, min_y, max_x, max_y = b["min_x"], b["min_y"], b["max_x"], b["max_y"]
        if not all(math.isfinite(v) for v in (min_x, min_y, max_x, max_y)):
            return {"cells": cells, "grid": [], "peak": 0, "peak_penalty": 0.0}
        w = max(max_x - min_x, 1e-6)
        h = max(max_y - min_y, 1e-6)
        grid = [[0 for _ in range(cells)] for _ in range(cells)]
        for ref in self.updates:
            if ref not in self.footprints:
                continue
            x, y, _ = self.positions[ref]
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            cx = min(cells - 1, max(0, int((x - min_x) / w * cells)))
            cy = min(cells - 1, max(0, int((y - min_y) / h * cells)))
            grid[cy][cx] += 1
        counts = [grid[r][c] for r in range(cells) for c in range(cells)]
        total = sum(counts)
        cap = max(1, math.ceil(total / max(1, cells * cells)) + 1)
        peak = max(counts) if counts else 0
        peak_penalty = float(sum(max(0, c - cap) ** 2 for c in counts))
        return {"cells": cells, "grid": grid, "capacity_per_cell": cap, "peak": peak,
                "peak_penalty": round(peak_penalty, 4)}

    def floorplan_score(self) -> Dict[str, Any]:
        """Aggregate global placement-quality score (lower is better)."""

        collisions, spacing_violations, _ = spacing_analysis(self, refs=list(self.updates))
        out_of_bounds = sum(1 for ref in self.updates if self.updates[ref].outside_board)
        parked_penalty = float(sum(p.penalty for p in self.parked.values()))
        congestion = self.congestion_map()
        quality = (1000.0 * len(collisions)
                   + 1000.0 * out_of_bounds
                   + parked_penalty
                   + 50.0 * len(spacing_violations)
                   + congestion["peak_penalty"])
        return {
            "score": round(quality, 4),
            "collisions": len(collisions),
            "spacing_violations": len(spacing_violations),
            "out_of_bounds": out_of_bounds,
            "parked": len(self.parked),
            "parked_penalty": round(parked_penalty, 4),
            "congestion_peak": congestion["peak"],
            "congestion_penalty": congestion["peak_penalty"],
        }

    def _separate_overlaps(self) -> bool:
        """Push movable support passives apart to relieve overlaps/congestion."""

        refs = [r for r in sorted(self.updates) if r in self.footprints]
        moved = False
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                if not _same_physical_side(self.footprints[a], self.footprints[b]):
                    continue
                ba, bb = self._cur_bbox(a), self._cur_bbox(b)
                required = self.model.clearance.required_for(_part_class_for(self.model, a), _part_class_for(self.model, b))
                if not ba.expanded(required).overlaps(bb):
                    continue
                target = b if self._is_movable_support(b) else (a if self._is_movable_support(a) else None)
                if target is None:
                    continue
                keep_area = (ba if target == b else bb).expanded(required)
                dest = self._relocate_out_of(target, keep_area, self.positions[target][2])
                if dest is not None:
                    self._apply_reflow_move(target, dest[0], dest[1], "reflow: separate overlap")
                    moved = True
        return moved

    def run_floorplan(self) -> None:
        """Iterative floorplanner: place, score, reflow, re-score until convergence.

        The first pass already performs inline reflow/parking, so subsequent
        iterations focus on relieving residual overlaps and congestion.  Strict /
        fail-fast mode runs a single deterministic pass.
        """

        self.apply()
        self.iteration_scores.append({"iteration": 0, **self.floorplan_score()})
        if not self.best_effort:
            return
        for iteration in range(1, self.max_floorplan_iterations):
            prev = self.iteration_scores[-1]
            if prev["collisions"] == 0 and prev["congestion_penalty"] == 0.0:
                break
            # Stop optimizing once the wall-clock budget is spent; the current
            # best-known floorplan is kept rather than aborting.
            if self.runtime.expired():
                self.messages.append(Message("warn",
                    f"time budget {self.runtime.time_budget_seconds:g}s exceeded during optimization "
                    f"(iteration {iteration}); keeping best-known floorplan"))
                break
            improved = self._separate_overlaps()
            score = self.floorplan_score()
            self.iteration_scores.append({"iteration": iteration, **score})
            if not improved or (prev["score"] - score["score"]) < 1e-6:
                break

    def reflow_report(self) -> Dict[str, Any]:
        """Structured floorplanner diagnostics for the placement report."""

        return {
            "best_effort": self.best_effort,
            "max_iterations": self.max_floorplan_iterations,
            "iterations": list(self.iteration_scores),
            "reflow_attempts": [a.as_report() for a in self.reflow_attempts],
            "moved_components": sorted({m for a in self.reflow_attempts for m in a.moved_components}),
            "moved_parents": sorted({m for a in self.reflow_attempts for m in a.moved_parents}),
            "parked": [p.as_report() for p in self.parked.values()],
            "parking_fallback_used": len(self.parked),
            "degraded_warnings": list(self.degraded_warnings),
            "congestion_map": self.congestion_map(),
            "placement_quality_score": self.floorplan_score(),
        }

    def _cache_hits(self) -> int:
        return _GEOMETRY_CACHE_STATS["hits"] - self._cache_stats_baseline["hits"]

    def runtime_report(self) -> Dict[str, Any]:
        """Best-effort/timeout diagnostics required by the runtime spec."""

        return self.runtime.report(
            candidates_evaluated=self.profiler.counters.get("candidates_evaluated", 0),
            cache_hits=self._cache_hits())

    def spatial_index_report(self) -> Dict[str, Any]:
        index = self._ensure_spatial_index()
        return index.report()

    def profile_report(self) -> Dict[str, Any]:
        report = self.profiler.report(total_runtime=self.runtime.elapsed())
        report["counters"]["geometry_cache_hits"] = self._cache_hits()
        report["counters"]["geometry_cache_misses"] = (
            _GEOMETRY_CACHE_STATS["misses"] - self._cache_stats_baseline["misses"])
        report["counters"]["reflow_attempts"] = len(self.reflow_attempts)
        report["candidates_evaluated"] = self.profiler.counters.get("candidates_evaluated", 0)
        report["candidates_generated"] = self.profiler.counters.get("candidates_generated", 0)
        report["candidates_pruned"] = self.profiler.counters.get("candidates_pruned", 0)
        report["spatial_index"] = self.spatial_index_report()
        return report

    def place_array_near_target(self, rule: Mapping[str, Any], refs: Sequence[str], origin: Point,
                                why: str, *, parent_ref: Optional[str] = None,
                                pad_origin: Optional[Point] = None) -> None:
        """Place an array as a group, reflowing the floorplan before giving up.

        A grouped-array placement failure is not treated as a component problem;
        it is evidence the surrounding floorplan is too tight.  When the local
        search cannot find a legal layout we (1) reflow nearby movable support
        parts and a movable parent, then (2) park as a last resort.  Only strict
        / fail-fast mode aborts.
        """

        refs = list(refs)
        if not refs:
            return
        search = self._attempt_array_placement(rule, refs, origin, why,
                                               parent_ref=parent_ref, pad_origin=pad_origin)
        if search.succeeded is not None:
            self._commit_array(rule, search)
            return
        if not self.best_effort:
            raise PlacementError(search.detail)
        if self._reflow_array(rule, origin, search):
            return
        self._park_array(rule, search)

    def _commit_array(self, rule: Mapping[str, Any], search: ArraySearch) -> None:
        """Place every member of a resolved array layout."""

        self._emit_array_side_notes(search)
        chosen = search.chosen_attempt or {}
        chosen_spacing = float(chosen.get("spacing", search.requested_spacing))
        if abs(chosen_spacing - search.requested_spacing) > 1e-9:
            self.messages.append(Message("note",
                f"{search.why}: array spacing increased from {_fmt_num(search.requested_spacing)} to "
                f"{_fmt_num(chosen_spacing)} mm to satisfy clearance rules"))
        for ref, (x, y) in zip(search.refs, search.succeeded or []):
            self.place(ref, x, y, search.rot, search.why, rule.get("note"),
                       allow_arbitrary_rotation=search.allow_arbitrary_rotation,
                       clearance_override=search.clearance_override, rule=rule)

    def _emit_array_side_notes(self, search: ArraySearch) -> None:
        rejected_by_side: Set[str] = set()
        for attempt in search.attempts:
            if attempt.get("ok") or attempt["side"] in rejected_by_side:
                continue
            rejected_by_side.add(attempt["side"])
            anchor = attempt["anchor"]
            self.messages.append(Message("note",
                f"{search.why}: side {attempt['side']!r} (anchor ~ x={_fmt_num(anchor[0])} y={_fmt_num(anchor[1])}, "
                f"distance={_fmt_num(attempt['distance'])}, shift={_fmt_num(attempt['shift'])}, "
                f"stagger={attempt['staggered']}) rejected: {attempt.get('failed_ref')!r} {attempt.get('reason')}"))

    def _attempt_array_placement(self, rule: Mapping[str, Any], refs: Sequence[str], origin: Point,
                                 why: str, *, parent_ref: Optional[str] = None,
                                 pad_origin: Optional[Point] = None, quick: bool = False) -> ArraySearch:
        """Search for a legal grouped-array layout without committing or aborting.

        ``quick`` runs a coarse search (single spacing, fewer distances/shifts)
        used by the reflow loop, where re-running the full ladder per trial would
        be prohibitively expensive.
        """

        refs = list(refs)
        n = len(refs)
        # fast optimization level uses the coarse (quick) ladder everywhere.
        if self.runtime.level == "fast":
            quick = True

        parent_bbox: Optional[BBox] = None
        parent_expanded: Optional[BBox] = None
        inferred_side: Optional[str] = None
        if parent_ref is not None:
            actual_parent = self.resolve_ref(parent_ref) or parent_ref
            if actual_parent in self.footprints and actual_parent in self.positions:
                px, py, prot = self.positions[actual_parent]
                parent_bbox = footprint_bbox_at(self.footprints[actual_parent], px, py, prot, self.model).bbox
                clearance = self._clearance_override(dict(rule))
                if clearance is None:
                    clearance = self.model.clearance.default
                array_margin = float(self._rule_value(rule, "array_margin", 0.5))
                parent_expanded = parent_bbox.expanded(clearance + array_margin)
                if pad_origin is not None:
                    inferred_side = self._nearest_bbox_side(pad_origin, parent_bbox)

        side = str(rule.get("side", "auto")).lower().replace("-", "_")
        side_origin = pad_origin or origin
        sides = self._ordered_array_sides(side, side_origin, parent_bbox, inferred_side)

        distance = float(rule.get("distance", 2.0))
        spacing = float(rule.get("spacing", 1.5))
        stagger_requested = self._rule_flag(rule, "stagger", False)
        max_per_row_value = rule.get("max_per_row")
        max_per_row = None if max_per_row_value is None else int(max_per_row_value)
        rot = self.resolve_rot(rule.get("rot"))
        region = None if self.allow_outside_region else self._region_bbox(self._rule_value(rule, "region"))
        clearance_override = self._clearance_override(dict(rule))
        allow_keepout_overlap = self.allow_keepout_overlap or self._rule_flag(rule, "allow_keepout_overlap", False)
        allow_arbitrary_rotation = self._allow_arbitrary_rotation(dict(rule))

        actual_refs: List[Optional[str]] = []
        for ref in refs:
            actual = self.resolve_ref(ref)
            if actual is None:
                self._warn_or_raise(f"missing footprint {ref!r} for {why}")
            actual_refs.append(actual)

        attempts: List[Dict[str, Any]] = []
        scored_attempts: List[SearchCandidate] = []
        chosen_attempt: Optional[Dict[str, Any]] = None
        # Bound the array search: an excellent layout ends the search early, and
        # no more than max_candidates_per_rule layouts are evaluated overall.
        attempt_cap = max(1, self.runtime.max_candidates_per_rule)
        excellent = self.runtime.excellent_threshold
        post_legal_budget = self.runtime.post_legal_candidates
        attempts_at_first_legal: Optional[int] = None
        distances = ([distance + delta for delta in (0.0, 1.0, 2.0, 3.0)] if quick
                     else [distance + delta for delta in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0)])
        stagger_options = [False, True] if stagger_requested else [False]
        # If the requested spacing cannot satisfy clearance between array members,
        # escalate spacing rather than failing or placing touching parts.
        requested_spacing = spacing
        spacing_ladder = ([requested_spacing] if quick
                          else [requested_spacing] + [requested_spacing * mult for mult in (1.25, 1.5, 2.0)])
        search_done = False
        for spacing in spacing_ladder:
          if chosen_attempt is not None or search_done:
            break
          shifts = ([0.0, spacing, -spacing] if quick else
                    [0.0, spacing / 2.0, -spacing / 2.0, spacing, -spacing, 1.5 * spacing, -1.5 * spacing, 2.0 * spacing, -2.0 * spacing])
          for dist in distances:
            for side_name in sides:
                for staggered in stagger_options:
                    for shift in shifts:
                        original_candidates = self._array_layout_candidates(
                            refs, actual_refs, side_origin, side_name, dist, spacing, shift=shift,
                            staggered=staggered, parent_expanded=parent_expanded, rot=rot,
                            max_per_row=max_per_row)
                        placement_region = self._placement_region_for_side(
                            parent_ref or "", parent_bbox, parent_expanded, side_name, region, str(rule.get("role", why)))
                        original_boxes = self._array_item_bboxes(actual_refs, original_candidates, rot)
                        slide_bounds = placement_region.legal_bbox if placement_region is not None else _geometry_bbox(self.board_geometry)
                        slide = slide_array_bbox_into_bounds(
                            original_candidates, side_name, slide_bounds, 0.0, item_bboxes=original_boxes)
                        candidates = list(slide["items"])
                        result = self._try_grouped_spread(
                            refs, actual_refs, candidates, rot, clearance_override=clearance_override,
                            region=region, allow_keepout_overlap=allow_keepout_overlap,
                            parent_expanded=parent_expanded)
                        rows = max(1, math.ceil(n / (max_per_row or min(3, max(1, math.ceil(n / 2))) if staggered else n)))
                        shape = "stagger" if staggered else ("column" if side_name in {"left", "right"} else "row")
                        metadata = {"side": side_name, "effective_side": side_name, "distance": round(dist, 6), "shift": round(shift, 6),
                                    "spacing": round(spacing, 6), "requested_spacing": round(requested_spacing, 6),
                                    "staggered": staggered, "shape": shape, "side_rank": sides.index(side_name), "rows": rows,
                                    "columns": max_per_row or (min(3, max(1, math.ceil(n / 2))) if staggered else n),
                                    "candidate_count": len(candidates),
                                    "slide_applied": bool(slide["slide_applied"]),
                                    "slide_dx": round(float(slide["slide_dx"]), 6),
                                    "slide_dy": round(float(slide["slide_dy"]), 6),
                                    "original_array_bbox": None if slide["original_array_bbox"] is None else slide["original_array_bbox"].as_report(),
                                    "slid_array_bbox": None if slide["slid_array_bbox"] is None else slide["slid_array_bbox"].as_report(),
                                    "slide_bounds_bbox": None if slide_bounds is None else slide_bounds.as_report(),
                                    "placement_region": None if placement_region is None else placement_region.as_report(),
                                    **self._capacity_metadata(n, side_name, spacing, placement_region, staggered, max_per_row)}
                        label = (f"side={side_name},distance={dist:g},shape={shape},"
                                 f"stagger={staggered},shift={shift:g}")
                        scored = self._score_grouped_array_attempt(refs, actual_refs, candidates, rot,
                                                                   side_origin, label, result, metadata)
                        attempt = {"side": side_name, "distance": dist, "shift": shift,
                                   "spacing": spacing, "staggered": staggered, "shape": shape,
                                   "anchor": candidates[len(candidates) // 2] if candidates else side_origin,
                                   "original_candidates": original_candidates, "candidates": candidates,
                                   "placement_region": None if placement_region is None else placement_region.as_report(),
                                   "slide": {k: (v.as_report() if isinstance(v, BBox) else v) for k, v in slide.items() if k != "items"},
                                   "score": scored.score, **result}
                        attempts.append(attempt)
                        scored_attempts.append(scored)
                        self.profiler.incr("candidates_generated")
                        self.profiler.incr("candidates_evaluated")
                        if scored.legal:
                            if attempts_at_first_legal is None:
                                attempts_at_first_legal = len(attempts)
                            if chosen_attempt is None or scored.score < float(chosen_attempt["score"]):
                                chosen_attempt = attempt
                            # Early success: an excellent legal layout stops the search.
                            if scored.score <= excellent:
                                self.profiler.incr("candidates_early_accepted")
                                search_done = True
                        # Honour the wall-clock budget even before a legal layout
                        # exists; strict mode must fail promptly and best-effort mode
                        # keeps the best attempt seen so far.  Candidate-count bounding
                        # still kicks in only once a legal layout exists, so the spacing
                        # ladder is not capped prematurely when time remains.
                        if len(attempts) % 32 == 0 and self.runtime.expired():
                            self._raise_strict_timeout()
                            search_done = True
                        elif chosen_attempt is not None:
                            since_legal = len(attempts) - (attempts_at_first_legal or 0)
                            if len(attempts) >= attempt_cap or since_legal >= post_legal_budget:
                                search_done = True
                        if search_done:
                            break
                    if search_done:
                        break
                if search_done:
                    break
            if search_done:
                break

        chosen_score = min((c for c in scored_attempts if c.legal), key=lambda c: c.score, default=None)
        if scored_attempts:
            outcome = SearchOutcome(refs[0], side_origin, scored_attempts, chosen_score, max(distances), False)
            for ref in refs:
                actual = self.resolve_ref(ref) or ref
                self.search_log[actual] = outcome
        succeeded: Optional[List[Tuple[float, float]]] = None if chosen_attempt is None else list(chosen_attempt["candidates"])

        failed = [a for a in attempts if not a.get("ok")]
        best = min(failed, key=lambda a: math.hypot(a["anchor"][0] - side_origin[0], a["anchor"][1] - side_origin[1])) if failed else None
        detail = [f"{refs[0]} could not be placed by {why}"]
        if parent_ref is not None:
            detail.append(f"parent {parent_ref} bbox: {self._fmt_bbox(parent_bbox)}" if parent_bbox else f"parent {parent_ref} bbox: unavailable")
            detail.append(f"expanded parent bbox: {self._fmt_bbox(parent_expanded)}" if parent_expanded else "expanded parent bbox: unavailable")
        if pad_origin is not None:
            detail.append(f"pad at x={_fmt_num(pad_origin[0])} y={_fmt_num(pad_origin[1])}")
        if inferred_side is not None:
            detail.append(f"inferred side: {inferred_side}")
        detail.append(f"effective side: {sides[0] if sides else 'unknown'}")
        if self.board_geometry is not None:
            detail.append(f"board bbox: {self._fmt_bbox(_geometry_bbox(self.board_geometry))}")
        if region is not None:
            detail.append(f"requested region bbox: {self._fmt_bbox(region)}")
        detail.append(f"sides tried: {', '.join(sides)}")
        detail.append(f"candidate arrays tried: {len(attempts)}")
        detail.append("sliding attempted: true")
        if best is not None:
            bx, by = best["anchor"]
            if best.get("placement_region"):
                pr = best["placement_region"]["legal_bbox"]
                detail.append(f"placement region bbox: min=({_fmt_num(pr['min_x'])}, {_fmt_num(pr['min_y'])}) max=({_fmt_num(pr['max_x'])}, {_fmt_num(pr['max_y'])})")
            slide = best.get("slide") or {}
            detail.append(f"slide_applied: {str(bool(slide.get('slide_applied'))).lower()}")
            detail.append(f"slide_dx: {_fmt_num(float(slide.get('slide_dx', 0.0)))}")
            detail.append(f"slide_dy: {_fmt_num(float(slide.get('slide_dy', 0.0)))}")
            if slide.get("original_array_bbox"):
                b = slide["original_array_bbox"]
                detail.append(f"original_array_bbox: min=({_fmt_num(b['min_x'])}, {_fmt_num(b['min_y'])}) max=({_fmt_num(b['max_x'])}, {_fmt_num(b['max_y'])})")
            if slide.get("slid_array_bbox"):
                b = slide["slid_array_bbox"]
                detail.append(f"slid_array_bbox: min=({_fmt_num(b['min_x'])}, {_fmt_num(b['min_y'])}) max=({_fmt_num(b['max_x'])}, {_fmt_num(b['max_y'])})")
            detail.append(f"best candidate after slide: side={best['side']} x={_fmt_num(bx)} y={_fmt_num(by)}; rejected: {best.get('failed_ref')!r} {best.get('reason')}")

        return ArraySearch(
            refs=refs, why=why, rot=rot, requested_spacing=requested_spacing,
            clearance_override=clearance_override, allow_arbitrary_rotation=allow_arbitrary_rotation,
            succeeded=succeeded, chosen_attempt=chosen_attempt, attempts=attempts,
            side_origin=side_origin, sides=sides, region=region, parent_ref=parent_ref,
            parent_bbox=parent_bbox, parent_expanded=parent_expanded, inferred_side=inferred_side,
            pad_origin=pad_origin, detail="; ".join(detail))

    def _place_array_rule(self, rule: Mapping[str, Any], why: str) -> None:
        refs = rule.get("refs") or []
        if not refs:
            return
        pad = rule.get("pad")
        pad_origin = None
        if pad is not None:
            origin = self._pad_centroid(str(rule["parent"]), pad)
            pad_origin = origin
        else:
            origin = self.get_pos(rule["parent"])
        self.place_array_near_target(rule, refs, origin, why, parent_ref=str(rule.get("parent")), pad_origin=pad_origin)

    def _collides_at(self, ref: str, x: float, y: float, rot: float, *, clearance_override: Optional[float] = None,
                    region: Optional[BBox] = None, allow_keepout_overlap: bool = False,
                    forbidden_bboxes: Optional[Sequence[Tuple[str, BBox]]] = None) -> Optional[str]:
        if ref not in self.footprints:
            return None
        test_info = footprint_bbox_at(self.footprints[ref], x, y, rot, self.model)
        if not _board_contains_bbox(self.board_geometry, test_info.bbox):
            if ref not in self.allow_body_outside_refs:
                return "would leave board bounds"
            if self.board_geometry is not None and not self.board_geometry.contains(x, y):
                return "anchor would leave board bounds (body extension allowed, anchor is not)"
        if region is not None and not region.contains_bbox(test_info.bbox):
            return "would leave region"
        if forbidden_bboxes:
            for name, bbox in forbidden_bboxes:
                if test_info.bbox.overlaps(bbox):
                    return f"would overlap {name}"
        if not allow_keepout_overlap:
            for keepout in keepout_rules(self.model):
                if bool(keepout.get("extra", {}).get("allow_keepout_overlap", False)):
                    continue
                if test_info.bbox.overlaps(_rect_to_abs_bbox(self.model, keepout)):
                    return f"avoided keepout {keepout['name']!r}"
        self.profiler.incr("collision_checks")
        # Query the spatial index for nearby footprints only.  The query margin
        # is the largest clearance any pair can require so no real overlap is
        # missed; precise per-pair clearance is still checked below.
        index = self._ensure_spatial_index()
        margin = clearance_override if clearance_override is not None else self._max_clearance
        nearby = index.query(test_info.bbox.expanded(margin))
        self.profiler.incr("spatial_index_queries")
        ref_fp = self.footprints[ref]
        ref_class = _part_class_for(self.model, ref)
        for other in sorted(nearby):
            if other == ref or other not in self.footprints or not _same_physical_side(ref_fp, self.footprints[other]):
                continue
            other_bbox = index.bbox_of(other)
            if other_bbox is None:
                ox, oy, orot = self.positions[other]
                other_bbox = footprint_bbox_at(self.footprints[other], ox, oy, orot, self.model).bbox
            required = clearance_override
            if required is None:
                required = self.model.clearance.required_for(ref_class, _part_class_for(self.model, other))
            if test_info.bbox.expanded(required).overlaps(other_bbox):
                return f"avoided collision with {other}"
        return None

    def _find_non_overlapping_position(self, ref: str, x: float, y: float, rot: float,
                                       *, clearance_override: Optional[float],
                                       candidate_sides: Optional[Sequence[str]], region: Optional[BBox] = None,
                                       allow_keepout_overlap: bool = False) -> Optional[Tuple[float, float, str]]:
        first = self._collides_at(ref, x, y, rot, clearance_override=clearance_override,
                                  region=region, allow_keepout_overlap=allow_keepout_overlap)
        if first is None:
            return (x, y, "requested location is legal")
        step = self.model.policy.search_step
        max_r = self.model.policy.max_search_radius
        sides = list(candidate_sides or ["top", "right", "bottom", "left"])
        vectors = self._SIDE_VECTORS
        points: List[Tuple[float, float, str]] = [(x, y, "requested")]
        n = int(max_r / step)
        # Slide along the requested side first, then search outward in side directions.
        for side in sides:
            if side in {"top", "bottom"}:
                for k in range(1, n + 1):
                    d = k * step
                    points.append((x + d, y, f"slide{side}+{d:g}"))
                    points.append((x - d, y, f"slide{side}-{d:g}"))
            elif side in {"left", "right"}:
                for k in range(1, n + 1):
                    d = k * step
                    points.append((x, y + d, f"slide{side}+{d:g}"))
                    points.append((x, y - d, f"slide{side}-{d:g}"))
        for k in range(1, n + 1):
            d = k * step
            for side in sides:
                vx, vy = vectors.get(side, (0.0, 0.0))
                points.append((x + vx * d, y + vy * d, f"radial{side}{d:g}"))
        outcome = self._search_candidates(ref, (x, y), rot, points, clearance_override=clearance_override,
                                          region=region, allow_keepout_overlap=allow_keepout_overlap,
                                          search_radius_used=max_r)
        chosen = outcome.chosen
        if chosen is None:
            return None
        if chosen.label == "requested":
            return (x, y, "requested location is legal")
        return (chosen.x, chosen.y, first)

    def _allow_arbitrary_rotation(self, rule: Dict[str, Any]) -> bool:
        return bool(rule.get("allow_arbitrary_rotation") or rule.get("extra", {}).get("allow_arbitrary_rotation"))

    def _apply_linear_collection(self, rule: Dict[str, Any], why: str) -> None:
        sx, sy = rule["start"]
        px, py = rule["pitch"]
        for i, ref in enumerate(rule["refs"]):
            x, y = _board_to_abs(self.model, sx + px * i, sy + py * i)
            self.place(ref, x, y, self.resolve_rot(rule.get("rot")), why, rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                       avoid_overlap=True, clearance_override=self._clearance_override(rule), rule=rule)

    def apply(self) -> None:
        """Apply rules in placement-file order."""

        total = len(self.model.rules)
        prev_label: Optional[str] = None
        prev_start = 0.0
        for rule_index, rule in enumerate(self.model.rules):
            rule["_index"] = rule_index
            typ = rule["type"]
            now = time.perf_counter()
            # Attribute the previous rule's wall time now that it has finished
            # (rule bodies use `continue`, so timing is closed at the loop top).
            if prev_label is not None:
                self.profiler.record_rule(prev_label, now - prev_start)
            prev_label = typ
            prev_start = now
            self._maybe_degrade_on_timeout()
            self._maybe_progress(rule_index, total, rule, typ)
            if typ in {"anchor", "fixed", "corner", "edge"}:
                x, y = self._placement_target(rule)
                rot_spec = rule.get("rot")
                if typ == "edge" and isinstance(rot_spec, str) and rot_spec.lower() == "auto":
                    rot = access_side_rotation(rule.get("access_side") or rule.get("edge"))
                    if rot is not None:
                        self.messages.append(Message("note",
                            f"{rule['ref']} edge rotation auto: access_side={rule.get('access_side') or rule.get('edge')!r} -> rot={_fmt_num(rot)}"))
                else:
                    rot = self.resolve_rot(rot_spec)
                self.place(rule["ref"], x, y, rot, typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule), rule=rule)

            elif typ == "cluster":
                self.place_cluster(rule)

            elif typ == "lock":
                self.lock(rule["ref"], rule.get("note") or "Lock rule")

            elif typ == "priority":
                actual = self.resolve_ref(rule["ref"])
                if actual is not None:
                    self.model.priority_refs[actual] = float(rule["value"])
                self.messages.append(Message("note", f"priority {rule['ref']!r}={_fmt_num(rule['value'])}"))

            elif typ == "soft":
                actual = self.resolve_ref(rule["ref"])
                if actual is not None:
                    self.model.soft_refs.add(actual)
                self.messages.append(Message("note", f"soft placement enabled for {rule['ref']!r}"))

            elif typ in {"array", "row", "column"}:
                self._apply_linear_collection(rule, typ)

            elif typ == "grid":
                sx, sy = rule["start"]
                px, py = rule["pitch"]
                cols = int(rule["columns"])
                for i, ref in enumerate(rule["refs"]):
                    col = i % cols
                    row = i // cols
                    x, y = _board_to_abs(self.model, sx + col * px, sy + row * py)
                    self.place(ref, x, y,
                               self.resolve_rot(rule.get("rot")), "grid", rule.get("note"),
                               allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                               avoid_overlap=True, clearance_override=self._clearance_override(rule), rule=rule)

            elif typ in {"between", "inline"}:
                a = self.get_pos(rule["a"])
                b = self.get_pos(rule["b"])
                if typ == "between":
                    self._place_between(rule, a, b)
                    continue
                x, y = self._placement_target(rule)
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot"), a, b), typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                           avoid_overlap=True, clearance_override=self._clearance_override(rule), rule=rule)

            elif typ == "satellite":
                px, py = self.get_pos(rule["parent"])
                side = str(rule.get("side", "right")).lower()
                if side in ("auto", "inward"):
                    self._place_near_point(rule, (px, py), typ)
                    continue
                if side not in self._SIDE_VECTORS:
                    raise PlacementError(f"Unknown satellite side {side!r}")
                dist = float(rule.get("distance", 2.0))
                idx = int(rule.get("index", 0))
                pitch = float(rule.get("pitch", 1.5))
                dx = float(rule.get("dx", 0.0))
                dy = float(rule.get("dy", 0.0))
                self._place_satellite(rule, px, py, side, dist, idx, pitch, dx, dy)

            elif typ == "near_pad":
                origin = self._pad_centroid(str(rule["parent"]), rule["pad"])
                self._place_near_point(rule, origin, "near_pad")

            elif typ in {"decoupling_array", "pullup_array"}:
                self._place_array_rule(rule, typ)

            elif typ == "orbit":
                cx, cy = self.get_pos(rule["parent"])
                radius = float(rule["radius"])
                start = float(rule["start_angle"])
                step = float(rule["step_angle"])
                for i, ref in enumerate(rule["refs"]):
                    theta = math.radians(start + step * i)
                    x = cx + radius * math.cos(theta)
                    y = cy + radius * math.sin(theta)
                    self.place(ref, x, y, self.resolve_rot(rule.get("rot")), "orbit", rule.get("note"),
                               allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                               avoid_overlap=True, clearance_override=self._clearance_override(rule), rule=rule)

            elif typ == "mirror":
                sx, sy = self.get_pos(rule["source"])
                source_rot = self.get_rot(rule["source"])
                axis = str(rule.get("axis", "vertical")).lower()
                w, h, ox, oy = _require_board_size(self.model, "Mirror")
                if rule.get("about") is not None:
                    about = float(rule["about"])
                elif axis in ("vertical", "x"):
                    about = ox + w / 2.0
                elif axis in ("horizontal", "y"):
                    about = oy + h / 2.0
                else:
                    raise PlacementError(f"Unknown mirror axis {axis!r}")
                if axis in ("vertical", "x"):
                    x, y = about + (about - sx), sy
                else:
                    x, y = sx, about + (about - sy)
                self.place(rule["ref"], x, y,
                           self.resolve_rot(rule.get("rot"), source_rot=source_rot, mirror_axis=axis),
                           "mirror", rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule), rule=rule)

            elif typ == "copy_placement":
                source_prefix = rule["source_prefix"]
                target_prefix = rule["target_prefix"]
                dx = float(rule.get("dx", 0.0))
                dy = float(rule.get("dy", 0.0))
                selected = rule.get("refs")
                if selected is None:
                    candidates = list(self.alias_map())
                    suffixes = [alias[len(source_prefix):] for alias in candidates if alias.startswith(source_prefix)]
                else:
                    suffixes = list(selected)
                if not suffixes:
                    self._warn_or_raise(f"CopyPlacement found no source refs under {source_prefix!r}")
                    continue
                for suffix in suffixes:
                    src = source_prefix + suffix
                    dst = target_prefix + suffix
                    actual_dst = self.resolve_ref(dst)
                    if actual_dst is None:
                        self._warn_or_raise(f"missing CopyPlacement target {dst!r}")
                        continue
                    x, y = self.get_pos(src)
                    source_rot = self.get_rot(src)
                    rot = self.resolve_rot(rule.get("rot"), source_rot=source_rot)
                    self.place(dst, x + dx, y + dy, rot, "copy_placement", rule.get("note"),
                               allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule), rule=rule)

            elif typ == "keepout":
                self.messages.append(Message("note", f"keepout {rule['name']!r}: x={_fmt_num(rule['x'])} "
                                                   f"y={_fmt_num(rule['y'])} w={_fmt_num(rule['w'])} "
                                                   f"h={_fmt_num(rule['h'])} layers={rule['layers']!r} "
                                                   "parsed but not emitted yet"))
                if rule.get("emit"):
                    self.messages.append(Message("warn", f"keepout {rule['name']!r} emit=True is not implemented yet"))
            elif typ == "corridor":
                self.messages.append(Message("note", f"corridor {rule['name']!r}: {rule['a']} -> {rule['b']} "
                                                   f"width={_fmt_num(rule['width'])} "
                                                   f"clearance={_fmt_num(rule['clearance'])} parsed but not emitted yet"))

            elif typ == "high_speed_path":
                self.messages.append(Message("note", f"high-speed path {rule['name']!r}: "
                                                   f"{' -> '.join(rule['sequence'])} "
                                                   f"corridor_width={_fmt_num(rule['corridor_width'])} (scored in review)"))

            elif typ == "power_island":
                self.messages.append(Message("note", f"power island {rule['name']!r}: regulator {rule['regulator']} "
                                                   f"(scored in review)"))
        if prev_label is not None:
            self.profiler.record_rule(prev_label, time.perf_counter() - prev_start)


def validate_placements(engine: PlacementEngine, *, min_spacing: float = 0.25,
                        allow_overlap: bool = False, warn_overlap: bool = False,
                        allow_outside_board: bool = False,
                        allow_keepout_overlap: bool = False,
                        allow_outside_region: bool = False,
                        degraded_refs: Optional[Set[str]] = None) -> List[Message]:
    """Run lightweight, CI-friendly placement validation.

    KiCad footprints can have complex outlines.  This first validator uses
    footprint origins as conservative proxies: it detects duplicate/near-
    duplicate placement, board-boundary violations, point-in-keepout violations,
    and unresolved relationship problems that were already emitted as warnings.
    Future versions can add true courtyard/bounding-box collision checks.
    """

    messages: List[Message] = []
    # Parked (degraded) footprints are already reported as no-abort warnings; do
    # not let them re-trip fatal validation in best-effort mode.
    degraded = degraded_refs or set()

    def _level(*refs: str, base: str = "error") -> str:
        return "warn" if any(r in degraded for r in refs) else base

    geometry = engine.board_geometry
    if geometry is not None:
        if geometry.width <= 0 or geometry.height <= 0:
            # Non-positive dimensions are always invalid, even for the low-confidence
            # footprint_extents fallback (e.g. a board with no parseable footprints).
            messages.append(Message("error", f"invalid board geometry: width={_fmt_num(geometry.width)} "
                                              f"height={_fmt_num(geometry.height)} source={geometry.source}"))
        elif geometry.source != "footprint_extents":
            for ref in sorted(engine.updates):
                x, y, _rot = engine.positions[ref]
                if not geometry.contains(x, y) and not allow_outside_board:
                    messages.append(Message(_level(ref), f"{ref!r} is outside Board geometry ({geometry.source}): x={_fmt_num(x)} y={_fmt_num(y)}"))

    # Near-coincident origins are usually accidental overlaps in generated placements.
    # Limit this initial check to footprints touched by this run; otherwise an
    # imported board with many unplaced footprints at (0, 0) would fail before a
    # partial placement file has a chance to mature.
    refs = sorted(engine.updates)
    for i, a in enumerate(refs):
        ax, ay, _ = engine.positions[a]
        for b in refs[i + 1:]:
            bx, by, _ = engine.positions[b]
            if math.hypot(ax - bx, ay - by) < min_spacing:
                messages.append(Message("warn" if (allow_overlap or warn_overlap) else _level(a, b),
                                        f"{a!r} and {b!r} have overlapping/near-coincident origins"))

    for item in keepout_violations(engine, refs=engine.updates, allow_keepout_overlap=allow_keepout_overlap):
        messages.append(Message(_level(item['ref']), f"{item['ref']!r} bbox overlaps keepout {item['keepout']!r}"))
    for item in region_violations(engine, refs=engine.updates, allow_outside_region=allow_outside_region):
        messages.append(Message(_level(item['ref']), f"{item['ref']!r} bbox is outside region {item['region']!r}"))

    collisions, violations, bbox_warnings = spacing_analysis(engine, refs=engine.updates)
    for warning in bbox_warnings:
        messages.append(Message("warn", warning["message"]))
    for item in collisions:
        overlap_level = "warn" if (allow_overlap or warn_overlap) else _level(item['ref_a'], item['ref_b'])
        messages.append(Message(overlap_level,
            f"ERROR: {item['ref_a']} overlaps {item['ref_b']}\n"
            f"  {item['ref_a']} bbox: {item['bbox_a']}\n"
            f"  {item['ref_b']} bbox: {item['bbox_b']}\n"
            f"  required clearance: {_fmt_num(item['required_clearance'])} mm"))
    for item in violations:
        level = "warn" if (allow_overlap or warn_overlap) else _level(item['ref_a'], item['ref_b'])
        messages.append(Message(level,
            f"{item['ref_a']} and {item['ref_b']} spacing {_fmt_num(item['actual_clearance'])} mm is below "
            f"required {_fmt_num(item['required_clearance'])} mm ({item['class_a']} to {item['class_b']})"))

    return messages


def validate_safe_placements(engine: PlacementEngine, *, allow_large_move: bool = False,
                             allow_outside_board: bool = False,
                             degraded_refs: Optional[Set[str]] = None) -> List[Message]:
    """Pre-write safety checks intended to prevent dangerous KiCad output."""

    degraded = degraded_refs or set()

    messages: List[Message] = []
    original_bounds = footprint_bounds(engine.footprints)
    geometry = engine.board_geometry
    safe_bounds = geometry.bounds() if geometry is not None else original_bounds
    board_w = geometry.width if geometry is not None else max(1.0, original_bounds["max_x"] - original_bounds["min_x"])
    board_h = geometry.height if geometry is not None else max(1.0, original_bounds["max_y"] - original_bounds["min_y"])
    margin_x = max(1.0, 5.0 * float(board_w))
    margin_y = max(1.0, 5.0 * float(board_h))

    for ref, count in sorted(engine.placement_attempts.items()):
        if count > 1:
            messages.append(Message("warn", f"duplicate resolved placement for {ref!r}; {count} rules target the same footprint; priority/later-rule semantics selected the winner"))

    for message in engine.messages:
        lower = message.text.lower()
        if message.level == "warn" and any(key in lower for key in ("missing footprint", "ambiguous", "locked footprint")):
            messages.append(Message("error", message.text))

    for update in engine.updates.values():
        # Non-finite coordinates are always fatal (they corrupt KiCad output),
        # even for parked parts.  Geometric degradations for parked parts are
        # demoted to warnings so the no-abort default does not abort on write.
        degraded_level = "warn" if update.ref in degraded else "error"
        if not all(math.isfinite(v) for v in (update.x, update.y, update.final_rot)):
            messages.append(Message("error", f"{update.ref!r} has non-finite placement coordinate or rotation"))
        if update.outside_board and not allow_outside_board:
            messages.append(Message(degraded_level, f"{update.ref!r} would be outside Board bounds at x={_fmt_num(update.x)} y={_fmt_num(update.y)}; use --allow-outside-board to override"))
        far_x = update.x < safe_bounds["min_x"] - margin_x or update.x > safe_bounds["max_x"] + margin_x
        far_y = update.y < safe_bounds["min_y"] - margin_y or update.y > safe_bounds["max_y"] + margin_y
        if (far_x or far_y) and not allow_large_move:
            messages.append(Message(degraded_level, f"{update.ref!r} would move far outside BoardGeometry bounds; use --allow-large-move to override"))
    return messages


def ownership_report(engine: PlacementEngine) -> Dict[str, Dict[str, Any]]:
    """Final placement ownership per ref: exactly one winning rule per footprint.

    Groups/clusters are metadata; this records which rule actually owns the
    final coordinates, and whether a coarse cluster move was later refined by
    a higher-priority rule.
    """

    ownership: Dict[str, Dict[str, Any]] = {}
    for ref, update in engine.updates.items():
        cluster = engine.last_cluster_by_ref.get(ref)
        refined = cluster is not None and not update.why.startswith("cluster ")
        ownership[ref] = {
            "owner_rule": update.why,
            "priority": engine.applied_priority.get(ref, 0.0),
            "rule_index": engine.applied_rule_index.get(ref, -1),
            "cluster": cluster,
            "refined_from_cluster": refined,
            "locked": ref in engine.locked,
            "allow_body_outside_board": ref in engine.allow_body_outside_refs,
        }
    return ownership


def _resolved_position(engine: PlacementEngine, ref: str) -> Optional[Point]:
    actual = engine.resolve_ref(ref)
    if actual is None or actual not in engine.positions:
        return None
    x, y, _ = engine.positions[actual]
    return x, y


def _bbox_at_current(engine: PlacementEngine, ref: str) -> Optional[BBox]:
    actual = engine.resolve_ref(ref)
    if actual is None or actual not in engine.footprints:
        return None
    x, y, rot = engine.positions[actual]
    return footprint_bbox_at(engine.footprints[actual], x, y, rot, engine.model).bbox


def high_speed_path_review(engine: PlacementEngine) -> List[Dict[str, Any]]:
    """Score each declared HighSpeedPath: directness, protection position, corridor obstruction."""

    reviews: List[Dict[str, Any]] = []
    for rule in engine.model.rules:
        if rule.get("type") != "high_speed_path":
            continue
        sequence = [str(r) for r in rule.get("sequence", [])]
        width = float(rule.get("corridor_width", 3.0))
        positions = [(name, _resolved_position(engine, name)) for name in sequence]
        missing = [name for name, pos in positions if pos is None]
        located = [(name, pos) for name, pos in positions if pos is not None]
        warnings: List[str] = []
        if missing:
            warnings.append(f"path members missing from board: {', '.join(missing)}")
        segments: List[Dict[str, Any]] = []
        total = 0.0
        for (a_name, a), (b_name, b) in zip(located, located[1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            total += length
            segments.append({"from": a_name, "to": b_name, "length_mm": round(length, 3)})
        direct = math.hypot(located[-1][1][0] - located[0][1][0],
                            located[-1][1][1] - located[0][1][1]) if len(located) >= 2 else 0.0
        straightness = (direct / total) if total > 1e-9 else 1.0
        if straightness < 0.85 and len(located) >= 3:
            warnings.append(f"path detours: straightness {straightness:.2f} (1.0 is a straight flow-through path)")
        protection_quality = None
        if rule.get("protect_first") and len(located) >= 3:
            conn_to_prot = segments[0]["length_mm"]
            ratio = conn_to_prot / total if total > 1e-9 else 0.0
            protection_quality = {
                "protection_ref": located[1][0],
                "connector_to_protection_mm": conn_to_prot,
                "protection_position_ratio": round(ratio, 3),
            }
            if ratio > 0.5:
                warnings.append(f"protection {located[1][0]} sits closer to the IC than the connector; "
                                "ESD should be placed near the connector for flow-through protection")
        intruders: List[str] = []
        members = {engine.resolve_ref(name) or name for name in sequence}
        for (a_name, a), (b_name, b) in zip(located, located[1:]):
            for other in sorted(engine.positions):
                if other in members or other not in engine.footprints:
                    continue
                bbox = _bbox_at_current(engine, other)
                if bbox is not None and _bbox_intrudes_corridor(bbox, a, b, width) and other not in intruders:
                    intruders.append(other)
        if intruders:
            warnings.append("unrelated components intrude the high-speed corridor: " + ", ".join(intruders))
        reviews.append({
            "name": str(rule.get("name")),
            "sequence": sequence,
            "corridor_width_mm": width,
            "segments": segments,
            "total_path_length_mm": round(total, 3),
            "direct_distance_mm": round(direct, 3),
            "straightness": round(straightness, 3),
            "protection": protection_quality,
            "corridor_intruders": intruders,
            "warnings": warnings,
        })
    return reviews


def power_island_review(engine: PlacementEngine) -> List[Dict[str, Any]]:
    """Score each declared PowerIsland: compactness, hot-loop area, feedback proximity, HS separation."""

    hs_segments: List[Tuple[Point, Point]] = []
    for rule in engine.model.rules:
        if rule.get("type") != "high_speed_path":
            continue
        pts = [p for p in (_resolved_position(engine, r) for r in rule.get("sequence", [])) if p is not None]
        hs_segments.extend(zip(pts, pts[1:]))

    reviews: List[Dict[str, Any]] = []
    for rule in engine.model.rules:
        if rule.get("type") != "power_island":
            continue
        regulator = str(rule.get("regulator"))
        reg_pos = _resolved_position(engine, regulator)
        warnings: List[str] = []

        def group_distances(refs: Sequence[str]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for ref in refs:
                pos = _resolved_position(engine, ref)
                if pos is None or reg_pos is None:
                    out.append({"ref": ref, "distance_mm": None})
                    continue
                out.append({"ref": ref, "distance_mm": round(math.hypot(pos[0] - reg_pos[0], pos[1] - reg_pos[1]), 3)})
            return out

        input_caps = group_distances([str(r) for r in rule.get("input_caps", [])])
        output_caps = group_distances([str(r) for r in rule.get("output_caps", [])])
        feedback = group_distances([str(r) for r in rule.get("feedback", [])])
        inductor_ref = rule.get("inductor")
        inductor = group_distances([str(inductor_ref)])[0] if inductor_ref else None

        for item in input_caps:
            if item["distance_mm"] is not None and item["distance_mm"] > 5.0:
                warnings.append(f"input cap {item['ref']} is {item['distance_mm']} mm from {regulator}; "
                                "keep input capacitance close to regulator power pins")
        if inductor and inductor["distance_mm"] is not None and inductor["distance_mm"] > 6.0:
            warnings.append(f"inductor {inductor['ref']} is {inductor['distance_mm']} mm from {regulator}")
        for item in feedback:
            if item["distance_mm"] is not None and item["distance_mm"] > 6.0:
                warnings.append(f"feedback part {item['ref']} is far from {regulator} FB pin")

        hot_loop_refs = [regulator] + [str(r) for r in rule.get("input_caps", [])]
        if inductor_ref:
            hot_loop_refs.append(str(inductor_ref))
        hot_loop_refs.extend(str(r) for r in rule.get("output_caps", []))
        boxes = [b for b in (_bbox_at_current(engine, r) for r in hot_loop_refs) if b is not None]
        island_bbox = _union_bbox(boxes)
        hot_loop_area = None if island_bbox is None else round(island_bbox.width * island_bbox.height, 3)

        hs_separation = None
        if island_bbox is not None and hs_segments:
            cx = (island_bbox.min_x + island_bbox.max_x) / 2.0
            cy = (island_bbox.min_y + island_bbox.max_y) / 2.0
            hs_separation = round(min(_point_segment_distance((cx, cy), a, b) for a, b in hs_segments), 3)
            if hs_separation < 5.0:
                warnings.append(f"power island {rule.get('name')!r} is {hs_separation} mm from a high-speed path; "
                                "keep switching power away from high-speed corridors")

        reviews.append({
            "name": str(rule.get("name")),
            "regulator": regulator,
            "input_caps": input_caps,
            "inductor": inductor,
            "output_caps": output_caps,
            "feedback": feedback,
            "switch_net": rule.get("switch_net"),
            "island_bbox": None if island_bbox is None else island_bbox.as_report(),
            "estimated_hot_loop_area_mm2": hot_loop_area,
            "high_speed_separation_mm": hs_separation,
            "warnings": warnings,
        })
    return reviews


_CORNER_NAMES = ("top_left", "top_right", "bottom_left", "bottom_right")


def _nearest_board_corner(geometry: BoardGeometry, point: Point) -> str:
    corners = {
        "top_left": (geometry.min_x, geometry.min_y),
        "top_right": (geometry.max_x, geometry.min_y),
        "bottom_left": (geometry.min_x, geometry.max_y),
        "bottom_right": (geometry.max_x, geometry.max_y),
    }
    return min(corners, key=lambda name: math.hypot(point[0] - corners[name][0], point[1] - corners[name][1]))


def mechanical_review(engine: PlacementEngine) -> Dict[str, Any]:
    """Review mechanical placement: mounting-hole distribution and edge-connector handling."""

    geometry = engine.board_geometry
    holes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for ref in sorted(engine.footprints):
        if _part_class_for(engine.model, ref) != "mechanical":
            continue
        x, y, _ = engine.positions[ref]
        corner = None if geometry is None else _nearest_board_corner(geometry, (x, y))
        holes.append({"ref": ref, "x": round(x, 3), "y": round(y, 3), "nearest_corner": corner})
    by_corner: Dict[str, List[str]] = {}
    for hole in holes:
        if hole["nearest_corner"]:
            by_corner.setdefault(hole["nearest_corner"], []).append(hole["ref"])
    for corner, refs in sorted(by_corner.items()):
        if len(refs) > 1:
            warnings.append(f"mounting holes {', '.join(refs)} share nearest corner {corner}; "
                            "holes should be distributed to distinct corners or explicit locations")
    for i, a in enumerate(holes):
        for b in holes[i + 1:]:
            d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            if d < 5.0:
                warnings.append(f"mounting holes {a['ref']} and {b['ref']} are clustered ({d:.2f} mm apart)")

    edge_components: List[Dict[str, Any]] = []
    for rule in engine.model.rules:
        placement = rule.get("placement") if rule.get("type") == "cluster" else None
        edge_rule = placement if isinstance(placement, Mapping) and placement.get("type") == "edge" else (
            rule if rule.get("type") == "edge" else None)
        if edge_rule is None:
            continue
        ref_name = str(rule.get("anchor") if rule.get("type") == "cluster" else rule.get("ref"))
        actual = engine.resolve_ref(ref_name)
        if actual is None:
            continue
        bbox = _bbox_at_current(engine, actual)
        body_outside = (geometry is not None and bbox is not None and
                        not _board_contains_bbox(geometry, bbox))
        allowed = actual in engine.allow_body_outside_refs
        if body_outside and not allowed:
            warnings.append(f"edge component {actual} body extends outside the board without "
                            "allow_body_outside_board=True")
        edge_components.append({
            "ref": actual,
            "edge": edge_rule.get("edge"),
            "access_side": edge_rule.get("access_side"),
            "edge_required": bool(edge_rule.get("edge_required")),
            "rotation": engine.positions[actual][2],
            "rotation_spec": edge_rule.get("rot"),
            "allow_body_outside_board": allowed,
            "body_outside_board": body_outside,
            "locked": actual in engine.locked,
        })
    return {"mounting_holes": holes, "holes_by_corner": by_corner,
            "edge_components": edge_components, "warnings": warnings}


def _md_warnings(lines: List[str], warnings: Sequence[str]) -> None:
    lines.append("")
    lines.append("## Warnings requiring human review")
    lines.append("")
    if warnings:
        lines.extend(f"- {w}" for w in warnings)
    else:
        lines.append("- none")
    lines.append("")


def high_speed_review_md(reviews: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# High-speed placement review", "",
             "Scores declared high-speed paths (connector -> protection -> IC) for directness,",
             "flow-through ESD placement, and corridor obstruction. Placement-level review only:",
             "impedance, return paths, and reference-plane continuity must be verified during routing.", ""]
    all_warnings: List[str] = []
    for review in reviews:
        lines.append(f"## {review['name']}")
        lines.append("")
        lines.append(f"- path: {' -> '.join(review['sequence'])}")
        lines.append(f"- corridor width: {review['corridor_width_mm']} mm")
        lines.append(f"- total path length: {review['total_path_length_mm']} mm "
                     f"(direct {review['direct_distance_mm']} mm, straightness {review['straightness']})")
        for segment in review["segments"]:
            lines.append(f"- segment {segment['from']} -> {segment['to']}: {segment['length_mm']} mm")
        if review.get("protection"):
            p = review["protection"]
            lines.append(f"- protection {p['protection_ref']}: {p['connector_to_protection_mm']} mm from connector "
                         f"(position ratio {p['protection_position_ratio']}; lower is closer to connector)")
        if review["corridor_intruders"]:
            lines.append(f"- corridor intruders: {', '.join(review['corridor_intruders'])}")
        else:
            lines.append("- corridor intruders: none")
        lines.append("")
        all_warnings.extend(review["warnings"])
    if not reviews:
        lines.append("No high-speed paths declared (HighSpeedPath() in placement.ppl).")
    _md_warnings(lines, all_warnings)
    return "\n".join(lines)


def power_review_md(reviews: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# Power placement review", "",
             "Scores declared power islands (regulator topology) for compactness, estimated",
             "hot-loop area, feedback proximity, and separation from high-speed paths.", ""]
    all_warnings: List[str] = []
    for review in reviews:
        lines.append(f"## {review['name']} (regulator {review['regulator']})")
        lines.append("")
        for label, items in (("input caps", review["input_caps"]), ("output caps", review["output_caps"]),
                             ("feedback", review["feedback"])):
            if items:
                rendered = ", ".join(f"{i['ref']} ({i['distance_mm']} mm)" for i in items)
                lines.append(f"- {label}: {rendered}")
        if review.get("inductor"):
            lines.append(f"- inductor: {review['inductor']['ref']} ({review['inductor']['distance_mm']} mm)")
        if review.get("switch_net"):
            lines.append(f"- switch node: {review['switch_net']} (keep copper compact; keep away from "
                         "high-speed, RF, and crystals)")
        if review.get("estimated_hot_loop_area_mm2") is not None:
            lines.append(f"- estimated hot-loop bounding area: {review['estimated_hot_loop_area_mm2']} mm^2")
        if review.get("high_speed_separation_mm") is not None:
            lines.append(f"- separation from nearest high-speed path: {review['high_speed_separation_mm']} mm")
        lines.append("")
        all_warnings.extend(review["warnings"])
    if not reviews:
        lines.append("No power islands declared (PowerIsland() in placement.ppl).")
    _md_warnings(lines, all_warnings)
    return "\n".join(lines)


def mechanical_review_md(review: Mapping[str, Any]) -> str:
    lines = ["# Mechanical placement review", "",
             "Mechanical constraints have highest priority: mounting holes, board outline,",
             "edge connectors, and keepouts are placed before everything else.", "",
             "## Mounting holes", ""]
    holes = review.get("mounting_holes", [])
    if holes:
        for hole in holes:
            lines.append(f"- {hole['ref']}: x={hole['x']} y={hole['y']} nearest corner: {hole['nearest_corner']}")
    else:
        lines.append("- none detected")
    lines.append("")
    lines.append("## Edge components")
    lines.append("")
    edge_components = review.get("edge_components", [])
    if edge_components:
        for item in edge_components:
            outside = " (body extends outside board: allowed)" if item["body_outside_board"] and item["allow_body_outside_board"] else (
                " (body extends outside board: NOT allowed)" if item["body_outside_board"] else "")
            lines.append(f"- {item['ref']}: edge={item['edge']} access_side={item['access_side']} "
                         f"rotation={item['rotation']:g} edge_required={item['edge_required']} "
                         f"locked={item['locked']}{outside}")
    else:
        lines.append("- none declared")
    _md_warnings(lines, review.get("warnings", []))
    return "\n".join(lines)


def ai_edit_hints_md(report: Mapping[str, Any]) -> str:
    """AI adjustment hints: uncertain placements and suggested .ppl edits.

    placement.ppl is intended to be edited by humans and AI assistants as part
    of an iterative layout-optimization loop; this report points editors at the
    placements most worth revisiting.
    """

    lines = ["# AI edit hints (pcb-place)", "",
             "placement.ppl and board.pln are designed to be edited by humans and AI",
             "assistants iteratively. The items below are the placements most worth",
             "revisiting, with suggested edits.", ""]

    lines.append("## Placement ownership")
    lines.append("")
    ownership = report.get("ownership", {})
    refined = {ref: o for ref, o in ownership.items() if o.get("refined_from_cluster")}
    for ref, owner in sorted(ownership.items()):
        cluster = f" (cluster {owner['cluster']} refined)" if owner.get("refined_from_cluster") else ""
        lines.append(f"- {ref}: owned by `{owner['owner_rule']}` priority {owner['priority']:g}{cluster}")
    if not ownership:
        lines.append("- no placements applied")
    lines.append("")

    lines.append("## Uncertain or adjusted placements")
    lines.append("")
    flagged = False
    for adj in report.get("auto_adjustments", []):
        flagged = True
        lines.append(f"- {adj['ref']} moved from requested ({adj['requested_x']:g}, {adj['requested_y']:g}) to "
                     f"({adj['placed_x']:g}, {adj['placed_y']:g}): {adj['reason']}. "
                     "If this is wrong, add an explicit Anchor()/NearPad() with the intended location.")
    for ref, search in (report.get("placement_search") or {}).items():
        if search.get("fallback_used"):
            flagged = True
            lines.append(f"- {ref} used grid-search fallback; consider widening its region, increasing distance, "
                         "or relaxing spacing for this ref.")
    if not flagged:
        lines.append("- none")
    lines.append("")

    floorplan = report.get("floorplan", {})
    quality = floorplan.get("placement_quality_score", {})
    quality_total = quality.get("score") if isinstance(quality, Mapping) else quality
    lines.append("## Floorplan health")
    lines.append("")
    if quality_total is not None:
        lines.append(f"- placement quality score: {quality_total:g} (lower is better)")
    iterations = floorplan.get("iterations", [])
    lines.append(f"- floorplan iterations run: {len(iterations)} of {floorplan.get('max_iterations', 0)} max")
    reflow_attempts = floorplan.get("reflow_attempts", [])
    moved_parents = floorplan.get("moved_parents", [])
    moved_components = floorplan.get("moved_components", [])
    lines.append(f"- reflow attempts: {len(reflow_attempts)}; parents moved: {len(moved_parents)}; "
                 f"support parts repacked: {len(moved_components)}")
    for ref in moved_parents:
        lines.append(f"- [review-required] movable parent {ref} was reflowed to satisfy a neighbour; "
                     "confirm this is acceptable, or Lock() / Anchor() it to pin it down.")
    parked = floorplan.get("parked", [])
    if parked:
        lines.append("")
        lines.append("### Parked components (degraded placement — edit the floorplan, not just the rule)")
        lines.append("")
        lines.append("Prefer editing regions, power islands, path definitions, edge-required")
        lines.append("constraints, stackup, and routing constraints before tweaking individual")
        lines.append("placement rules. A parked part usually means the surrounding floorplan is")
        lines.append("over-constrained:")
        for entry in parked:
            reason = entry.get("reason", "no legal location after reflow")
            lines.append(f"- [review-required] {entry['ref']} parked by {entry.get('why', 'placement')}: "
                         f"{reason}. Enlarge its Region(), free space near its parent, or relax spacing "
                         "for the neighbourhood.")
    lines.append("")

    lines.append("## Suggested placement.ppl edits")
    lines.append("")
    suggestions = False
    for item in report.get("spacing_violations", []):
        suggestions = True
        lines.append(f"- {item['ref_a']}/{item['ref_b']} spacing {item['actual_clearance']:g} mm is below "
                     f"{item['required_clearance']:g} mm: move one ref or override Spacing() deliberately.")
    for review in report.get("high_speed_paths", []):
        for warning in review.get("warnings", []):
            suggestions = True
            lines.append(f"- {review['name']}: {warning}")
    for review in report.get("power_islands", []):
        for warning in review.get("warnings", []):
            suggestions = True
            lines.append(f"- {review['name']}: {warning}")
    if refined:
        suggestions = True
        lines.append(f"- {len(refined)} ref(s) had cluster moves refined by higher-priority rules "
                     "(expected: clusters are metadata, not atomic placement units).")
    if not suggestions:
        lines.append("- none")
    lines.append("")
    lines.append("## Risks requiring engineering review")
    lines.append("")
    lines.append("- Placement-level scoring cannot verify impedance, return paths, plane splits, or EMI compliance.")
    lines.append("- Review high-speed-placement-review.md and power-placement-review.md before routing.")
    return "\n".join(lines) + "\n"


def apply_placements(text: str, model: PlacementModel, *, strict: bool = False,
                     allow_suffix_match: bool = True, validate: bool = False, safe: bool = False,
                     allow_large_move: bool = False, allow_outside_board: bool = False,
                     cardinal_rotations: bool = False, safety_fatal: bool = True,
                     allow_overlap: bool = False, warn_overlap: bool = False,
                     allow_keepout_overlap: bool = False,
                     allow_outside_region: bool = False,
                     best_effort: bool = True, fail_fast: bool = False,
                     max_floorplan_iterations: Optional[int] = None,
                     optimization_level: str = "normal",
                     time_budget_seconds: float = 60.0,
                     max_candidates_per_rule: Optional[int] = None,
                     progress: bool = False) -> Tuple[str, List[Message], Dict[str, Any]]:
    """Apply placement rules and return rewritten KiCad text plus diagnostics."""

    footprints = parse_footprints(text)
    if not footprints:
        raise PlacementError("No footprints found in PCB file")
    board_geometry = resolve_board_geometry(text, model, footprints, allow_footprint_fallback=True)
    defined_geometry = board_geometry_from_definition(model.board)
    if model.board.emit_outline:
        if defined_geometry is None:
            raise PlacementError("Board(emit_outline=True) requires width and height")
        existing_geometry = BoardGeometry.from_edge_cuts(text)
        if existing_geometry is not None and not existing_geometry.nearly_equals(defined_geometry):
            raise PlacementError("Board emit_outline=True conflicts with existing Edge.Cuts geometry")
    runtime = RuntimeBudget(level=optimization_level, time_budget_seconds=time_budget_seconds,
                            max_candidates_per_rule=max_candidates_per_rule,
                            max_floorplan_iterations=max_floorplan_iterations, strict=strict)
    engine = PlacementEngine(footprints, model, strict=strict, allow_suffix_match=allow_suffix_match,
                             cardinal_rotations=cardinal_rotations, board_geometry=board_geometry,
                             allow_keepout_overlap=allow_keepout_overlap,
                             allow_outside_region=allow_outside_region,
                             best_effort=best_effort, fail_fast=fail_fast,
                             max_floorplan_iterations=max_floorplan_iterations or runtime.max_floorplan_iterations,
                             runtime=runtime, progress=progress)
    engine.run_floorplan()
    # In no-abort mode, parked footprints are intentionally degraded; their
    # geometric violations are reported as warnings, not fatal errors.
    degraded_refs = set(engine.parked)
    collision_messages = validate_placements(engine, allow_overlap=allow_overlap, warn_overlap=warn_overlap,
                                             allow_outside_board=allow_outside_board,
                                             allow_keepout_overlap=allow_keepout_overlap,
                                             allow_outside_region=allow_outside_region,
                                             degraded_refs=degraded_refs)
    validation_messages = collision_messages if (validate or safe) else []
    safety_messages = validate_safe_placements(engine, allow_large_move=allow_large_move,
                                               allow_outside_board=allow_outside_board,
                                               degraded_refs=degraded_refs) if safe else []
    engine.messages.extend(validation_messages)
    if strict and any(m.level == "error" for m in engine.messages):
        raise PlacementError("validation failed: " + "; ".join(m.text for m in engine.messages if m.level == "error"))
    engine.messages.extend(safety_messages)
    if safe and safety_fatal and any(m.level == "error" for m in engine.messages):
        raise PlacementError("validation failed: " + "; ".join(m.text for m in engine.messages if m.level == "error"))
    collisions, spacing_violations, bbox_warnings = spacing_analysis(engine, refs=engine.updates)
    ko_violations = keepout_violations(engine, refs=engine.updates, allow_keepout_overlap=allow_keepout_overlap)
    reg_violations = region_violations(engine, refs=engine.updates, allow_outside_region=allow_outside_region)
    generated_uuids: List[GeneratedUUID] = []
    rewritten = text
    if model.board.emit_outline and defined_geometry is not None:
        rewritten = emit_board_outline(rewritten, defined_geometry, generated_uuids=generated_uuids)
    for ref, update in sorted(engine.updates.items(), key=lambda kv: footprints[kv[0]].start, reverse=True):
        fp = footprints[ref]
        rewritten = rewritten[:fp.start] + _replace_at(fp.text, update.x, update.y, update.write_rot) + rewritten[fp.end:]
    validate_generated_uuids(rewritten, generated_uuids)
    edge_required_refs = {
        ref
        for ref, rule in ((engine.resolve_ref(str(r.get("ref"))) or str(r.get("ref")), r)
                          for r in model.rules if r.get("ref") is not None)
        if engine._rule_flag(rule, "edge_required", False)
    }
    edge_required_refs.update(
        engine.resolve_ref(str(r.get("anchor"))) or str(r.get("anchor"))
        for r in model.rules
        if r.get("type") == "cluster" and engine._rule_flag(r.get("placement", {}), "edge_required", False)
    )
    edge_required_validation: List[Dict[str, Any]] = []
    for ref in sorted(edge_required_refs):
        if ref not in engine.footprints:
            continue
        bbox = _bbox_at_current(engine, ref)
        edge_distance = None if bbox is None else engine._edge_distance(bbox)
        on_edge = edge_distance is not None and edge_distance <= 5.0
        edge_required_validation.append({
            "ref": ref,
            "edge_distance_mm": None if edge_distance is None else round(edge_distance, 3),
            "on_board_edge": on_edge,
            "locked": ref in engine.locked,
        })
        if edge_distance is not None and not on_edge:
            engine.messages.append(Message("warn", f"edge-required {ref!r} sits {_fmt_num(edge_distance)} mm "
                                           "from the nearest board edge; optimization must not pull edge "
                                           "connectors inward"))
    hs_reviews = high_speed_path_review(engine)
    power_reviews = power_island_review(engine)
    mech_review = mechanical_review(engine)
    report = {
        "version": __version__,
        "footprints_total": len(footprints),
        "rules_total": len(model.rules),
        "placements_applied": len(engine.updates),
        "aliases": dict(model.aliases),
        "imported_aliases": dict(model.imported_aliases),
        "effective_aliases": engine.alias_map(),
        "alias_diagnostics": dataclasses.asdict(model.alias_diagnostics),
        "regions": regions_report(model),
        "clearance_rules": dataclasses.asdict(model.clearance),
        "placement_policy": dataclasses.asdict(model.policy),
        "part_classes": {ref: _part_class_for(model, ref) for ref in sorted(footprints)},
        "locked_refs": sorted(engine.locked),
        "edge_required_refs": sorted(edge_required_refs),
        "edge_required_validation": edge_required_validation,
        "allowed_outside_board_refs": sorted(engine.allow_body_outside_refs),
        "ownership": ownership_report(engine),
        "high_speed_paths": hs_reviews,
        "power_islands": power_reviews,
        "mechanical_review": mech_review,
        "floorplan": engine.reflow_report(),
        "runtime": engine.runtime_report(),
        "spatial_index": engine.spatial_index_report(),
        "profile": engine.profile_report(),
        "requires_review": sorted(engine.requires_review),
        "spacing_profile": dataclasses.asdict(model.clearance),
        "validation_errors": sum(1 for m in engine.messages if m.level == "error"),
        "board": dataclasses.asdict(model.board) | {"origin_x": model.board.origin_x, "origin_y": model.board.origin_y},
        "bounds": footprint_bounds(footprints),
        "footprint_bounds": footprint_bounds(footprints),
        "placement_bounds": footprint_bounds({ref: dataclasses.replace(footprints[ref], x=engine.updates[ref].x, y=engine.updates[ref].y) for ref in engine.updates}) if engine.updates else footprint_bounds(footprints),
        "board_bounds": None if board_geometry is None else board_geometry.bounds(),
        "board_geometry": None if board_geometry is None else board_geometry.as_report(),
        "geometry_source": None if board_geometry is None else board_geometry.source,
        "updated_refs": sorted(engine.updates),
        "placements": [dataclasses.asdict(engine.updates[ref]) for ref in sorted(engine.updates)],
        "clusters": list(engine.clusters),
        "keepouts": keepout_report(model),
        "keepout_violations": ko_violations,
        "region_violations": reg_violations,
        "priority_conflicts": list(engine.priority_conflicts),
        "overridden_rules": list(engine.overridden_rules),
        "locked_move_attempts": list(engine.locked_move_attempts),
        "collisions": collisions,
        "spacing_violations": spacing_violations,
        "bbox_warnings": bbox_warnings,
        "auto_adjustments": [dataclasses.asdict(a) for a in engine.auto_adjustments],
        "placement_search": {ref: outcome.as_report() for ref, outcome in engine.search_log.items()},
        "generated_uuids": [dataclasses.asdict(item) for item in generated_uuids],
        "collision_count": len(collisions),
        "spacing_violation_count": len(spacing_violations),
        "messages": [dataclasses.asdict(m) for m in engine.messages],
    }
    return rewritten, engine.messages, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def list_refs(pcb_path: Path, *, fmt: str = "text") -> int:
    text = pcb_path.read_text(encoding="utf-8")
    footprints = parse_footprints(text)
    if fmt == "json":
        payload = [dataclasses.asdict(fp) | {"text": None, "start": None, "end": None} for fp in sorted(footprints.values(), key=lambda fp: fp.ref)]
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for ref in sorted(footprints):
            fp = footprints[ref]
            layer = f" layer={fp.layer}" if fp.layer else ""
            print(f"{ref}\tx={_fmt_num(fp.x)}\ty={_fmt_num(fp.y)}\trot={_fmt_num(fp.rot)}{layer}")
        print(f"\n{len(footprints)} footprint(s)")
    return 0



def list_aliases(netlist_path: Path, *, fmt: str = "text") -> int:
    """List semantic aliases parsed from a Zener/pcb netlist artifact."""

    aliases, diagnostics = parse_netlist_aliases(netlist_path)
    if fmt == "json":
        print(json.dumps({
            "aliases": aliases,
            "diagnostics": dataclasses.asdict(diagnostics),
        }, indent=2, sort_keys=True))
    else:
        for alias, ref in sorted(aliases.items()):
            print(f"{alias} -> {ref}")
        print(f"\n{len(aliases)} alias(es)")
        for error in diagnostics.errors:
            print(f"error: {error}", file=sys.stderr)
        for warning in diagnostics.warnings:
            print(f"warn: {warning}", file=sys.stderr)
    return 1 if diagnostics.errors else 0

def default_output_path(pcb_path: Path) -> Path:
    if pcb_path.name.endswith(".kicad_pcb"):
        return pcb_path.with_name(pcb_path.name[:-10] + ".placed.kicad_pcb")
    return pcb_path.with_suffix(pcb_path.suffix + ".placed")


def _print_geometry(geometry: Optional[BoardGeometry], *, unavailable: bool = True) -> None:
    if geometry is None:
        if unavailable:
            print("board_bounds:")
            print("  unavailable")
        return
    print("board_bounds:")
    print(f"  source={geometry.source}")
    print(f"  origin_x={_fmt_num(geometry.origin_x)}")
    print(f"  origin_y={_fmt_num(geometry.origin_y)}")
    print(f"  width={_fmt_num(geometry.width)}")
    print(f"  height={_fmt_num(geometry.height)}")


def print_bounds(pcb_path: Path, *, fmt: str = "text") -> int:
    text = pcb_path.read_text(encoding="utf-8")
    footprints = parse_footprints(text)
    bounds = footprint_bounds(footprints)
    geometry = BoardGeometry.from_edge_cuts(text)
    if fmt == "json":
        print(json.dumps({"footprints_total": len(footprints), "footprint_bounds": bounds,
                          "board_bounds": None if geometry is None else geometry.as_report()}, indent=2, sort_keys=True))
    else:
        print("footprint_bounds:")
        print(f"  min_x={_fmt_num(bounds['min_x'])}")
        print(f"  min_y={_fmt_num(bounds['min_y'])}")
        print(f"  max_x={_fmt_num(bounds['max_x'])}")
        print(f"  max_y={_fmt_num(bounds['max_y'])}")
        _print_geometry(geometry)
        print(f"{len(footprints)} footprint(s)")
    return 0


def print_board(pcb_path: Path, model: Optional[PlacementModel] = None, *, fmt: str = "text") -> int:
    text = pcb_path.read_text(encoding="utf-8")
    footprints = parse_footprints(text)
    geometry = resolve_board_geometry(text, model, footprints, allow_footprint_fallback=False)
    if fmt == "json":
        print(json.dumps({"board_geometry": None if geometry is None else geometry.as_report()}, indent=2, sort_keys=True))
    else:
        if geometry is None:
            print("board: unavailable")
        else:
            print(f"source={geometry.source}")
            print(f"origin_x={_fmt_num(geometry.origin_x)}")
            print(f"origin_y={_fmt_num(geometry.origin_y)}")
            print(f"width={_fmt_num(geometry.width)}")
            print(f"height={_fmt_num(geometry.height)}")
    return 0


def print_clusters(model: PlacementModel, *, fmt: str = "text") -> int:
    clusters = [rule for rule in model.rules if rule.get("type") == "cluster"]
    payload = [{"name": rule["name"], "anchor": rule["anchor"], "members": list(rule["members"]),
                "member_count": len(rule["members"])} for rule in clusters]
    if fmt == "json":
        print(json.dumps({"clusters": payload}, indent=2, sort_keys=True))
    else:
        for item in payload:
            print(f"Cluster {item['name']}")
            print(f"  anchor: {item['anchor']}")
            print(f"  members: {item['member_count']}")
        if not payload:
            print("no clusters")
    return 0


def print_regions(model: PlacementModel, *, fmt: str = "text") -> int:
    payload = regions_report(model)
    if fmt == "json":
        print(json.dumps({"regions": payload}, indent=2, sort_keys=True))
    else:
        for name, item in payload.items():
            print(f"Region {name}: x={_fmt_num(item['x'])} y={_fmt_num(item['y'])} w={_fmt_num(item['w'])} h={_fmt_num(item['h'])}")
        if not payload:
            print("no regions")
    return 0


def print_generated_uuid_debug(generated_uuids: Sequence[Mapping[str, str] | GeneratedUUID]) -> None:
    for item in generated_uuids:
        if isinstance(item, GeneratedUUID):
            object_name = item.object
            uuid_text = item.uuid
        else:
            object_name = item["object"]
            uuid_text = item["uuid"]
        print("generated UUID:")
        print(f"  object={object_name}")
        print(f"  uuid={uuid_text}")


def emit_outline_only(pcb_path: Path, model: PlacementModel, output_path: Path, *, debug_write: bool = False) -> int:
    geometry = board_geometry_from_definition(model.board)
    if geometry is None:
        raise PlacementError("--emit-outline-only requires Board(width=..., height=...) in the placement file")
    text = pcb_path.read_text(encoding="utf-8")
    generated_uuids: List[GeneratedUUID] = []
    out = emit_board_outline(text, geometry, generated_uuids=generated_uuids)
    validate_generated_uuids(out, generated_uuids)
    if debug_write:
        print_generated_uuid_debug(generated_uuids)
    atomic_write_text(output_path, out)
    print(f"wrote outline: {output_path}")
    return 0


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def sanity_check_output(input_text: str, output_text: str) -> None:
    if not parentheses_balanced(output_text):
        raise PlacementError("sanity check failed: output parentheses are not balanced")
    input_fps = parse_footprints(input_text)
    output_fps = parse_footprints(output_text)
    if len(output_fps) != len(input_fps):
        raise PlacementError(f"sanity check failed: footprint count changed from {len(input_fps)} to {len(output_fps)}")
    missing = sorted(set(input_fps) - set(output_fps))
    if missing:
        raise PlacementError("sanity check failed: missing footprint refs: " + ", ".join(missing))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcb-place", description="Apply placement.ppl rules to a KiCad .kicad_pcb file")
    parser.add_argument("pcb", type=Path, nargs="?", help="Path to input .kicad_pcb file")
    parser.add_argument("ppl", type=Path, nargs="?", help="Path to placement .ppl file")
    parser.add_argument("-o", "--output", type=Path, help="Output .kicad_pcb path. Defaults to <input>.placed.kicad_pcb")
    parser.add_argument("--in-place", action="store_true", help="Edit the input PCB in place")
    parser.add_argument("--dry-run", action="store_true", help="Report placements without writing output")
    parser.add_argument("--debug-write", action="store_true", help="Print details about generated output objects before writing")
    parser.add_argument("--print-bounds", action="store_true", help="Print input footprint coordinate bounds and board geometry and exit")
    parser.add_argument("--print-board", action="store_true", help="Print authoritative board geometry and exit")
    parser.add_argument("--print-clusters", action="store_true", help="Print Cluster() declarations from the placement file and exit")
    parser.add_argument("--print-regions", action="store_true", help="Print Region() declarations from the placement file and exit")
    parser.add_argument("--emit-outline-only", type=Path, metavar="OUTPUT", help="Write only the rectangular Board outline to OUTPUT and exit")
    parser.add_argument("--infer-origin", action="store_true", help="Infer Board origin from input footprint minimum x/y before applying rules")
    parser.add_argument("--validate", action="store_true", help="Run placement validation without writing output")
    parser.add_argument("--check", action="store_true", help="Fail if applying placement would change the input file")
    parser.add_argument("--no-backup", action="store_true", help="Do not write .bak when editing in-place")
    parser.add_argument("--strict", action="store_true", help="Treat missing/ambiguous refs and unplaceable parts as errors (disables no-abort)")
    parser.add_argument("--fail-fast", action="store_true", help="Abort on the first unplaceable component instead of reflowing/parking")
    parser.add_argument("--best-effort", dest="best_effort", action="store_true", default=True,
                        help="No-abort floorplanning: reflow then park instead of failing (default)")
    parser.add_argument("--no-best-effort", dest="best_effort", action="store_false",
                        help="Disable no-abort floorplanning (equivalent to --fail-fast)")
    parser.add_argument("--max-floorplan-iterations", type=int, default=None,
                        help="Maximum iterative floorplan optimization passes (default from --optimization-level)")
    parser.add_argument("--optimization-level", choices=["fast", "normal", "deep"], default="normal",
                        help="Search breadth/quality trade-off: fast (fewer candidates, no parent reflow), "
                             "normal (balanced, default), deep (larger search, more reflow)")
    parser.add_argument("--time-budget-seconds", type=float, default=60.0,
                        help="Wall-clock placement budget in seconds (default 60). 0 disables. When it expires "
                             "the best-known placement is kept and marked requires_review unless --strict")
    parser.add_argument("--max-candidates-per-rule", type=int, default=None,
                        help="Cap candidate evaluations per placement primitive (default from --optimization-level)")
    parser.add_argument("--profile-placement", type=Path, metavar="JSON",
                        help="Write a placement profiling report (per-rule runtime, candidate/collision counts) to JSON")
    parser.add_argument("--progress", action="store_true",
                        help="Print concise per-rule progress to stderr for long runs")
    parser.add_argument("--floorplan-report", type=Path, metavar="JSON",
                        help="Write the floorplanner/reflow report as standalone JSON")
    parser.add_argument("--safe", dest="safe", action="store_true", default=True, help="Run pre-write safety checks (default)")
    parser.add_argument("--no-safe", dest="safe", action="store_false", help="Disable pre-write safety checks")
    parser.add_argument("--allow-large-move", action="store_true", help="Allow placements far outside original footprint bounds")
    parser.add_argument("--allow-outside-board", action="store_true", help="Allow placements outside declared Board bounds")
    parser.add_argument("--allow-overlap", action="store_true", help="Allow footprint overlaps and spacing violations after reporting them")
    parser.add_argument("--allow-keepout-overlap", action="store_true", help="Allow footprint bounding boxes to overlap Keepout() rectangles")
    parser.add_argument("--allow-outside-region", action="store_true", help="Allow placements with region=... to leave that Region() rectangle")
    parser.add_argument("--warn-overlap", action="store_true", help="Report footprint overlaps and spacing violations as warnings")
    parser.add_argument("--cardinal-rotations", action="store_true", help="Round explicit rotations to nearest 0/90/180/270 unless a rule allows arbitrary rotation")
    parser.add_argument("--no-suffix-match", action="store_true", help="Disable hierarchical suffix reference matching")
    parser.add_argument("--netlist", type=Path, help="Optional Zener/pcb netlist artifact used to import semantic aliases")
    parser.add_argument("--list-refs", action="store_true", help="List footprint references in the PCB and exit")
    parser.add_argument("--list-aliases", action="store_true", help="List semantic aliases parsed from --netlist and exit")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format for --list-refs or --list-aliases")
    parser.add_argument("--report-json", type=Path, help="Write machine-readable placement report JSON")
    parser.add_argument("--high-speed-review", type=Path, metavar="MD",
                        help="Write high-speed-placement-review.md scoring HighSpeedPath() declarations")
    parser.add_argument("--power-review", type=Path, metavar="MD",
                        help="Write power-placement-review.md scoring PowerIsland() declarations")
    parser.add_argument("--mechanical-review", type=Path, metavar="MD",
                        help="Write mechanical-placement-review.md for holes and edge connectors")
    parser.add_argument("--ai-edit-hints", type=Path, metavar="MD",
                        help="Write ai-edit-hints.md with uncertain placements and suggested .ppl edits")
    parser.add_argument("--explain-placement", metavar="REF", help="Print scored placement-candidate explanation for REF")
    parser.add_argument("--version", action="version", version=f"pcb-place {__version__}")
    return parser


def _main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.list_aliases:
        if args.netlist is None:
            parser.error("--netlist is required with --list-aliases")
        if not args.netlist.exists():
            raise SystemExit(f"Netlist file not found: {args.netlist}")
        return list_aliases(args.netlist, fmt=args.format)
    if args.pcb is None:
        parser.error("pcb file is required")
    if not args.pcb.exists():
        raise SystemExit(f"PCB file not found: {args.pcb}")
    if args.list_refs:
        return list_refs(args.pcb, fmt=args.format)
    if args.print_bounds:
        return print_bounds(args.pcb, fmt=args.format)
    if args.print_board and args.ppl is None:
        return print_board(args.pcb, None, fmt=args.format)
    if args.ppl is None:
        parser.error("ppl file is required unless --list-refs, --print-bounds, --print-board, --print-clusters, --print-regions, or --list-aliases is used")
    if not args.ppl.exists():
        raise SystemExit(f"PPL file not found: {args.ppl}")
    if args.output and args.in_place:
        parser.error("--output and --in-place are mutually exclusive")

    model = load_ppl(args.ppl)
    if args.print_clusters:
        return print_clusters(model, fmt=args.format)
    if args.print_regions:
        return print_regions(model, fmt=args.format)
    if args.print_board:
        return print_board(args.pcb, model, fmt=args.format)
    if args.emit_outline_only is not None:
        return emit_outline_only(args.pcb, model, args.emit_outline_only, debug_write=bool(args.debug_write))
    if args.netlist is not None:
        if not args.netlist.exists():
            raise SystemExit(f"Netlist file not found: {args.netlist}")
        import_netlist_aliases(model, args.netlist)
    source_text = args.pcb.read_text(encoding="utf-8")
    source_footprints = parse_footprints(source_text)
    if args.infer_origin:
        inferred_geometry = resolve_board_geometry(source_text, model, source_footprints, allow_footprint_fallback=True)
        if inferred_geometry is None:
            raise PlacementError("Could not infer origin: no Edge.Cuts, Board(...), or footprint bounds available")
        model.board.origin = (inferred_geometry.origin_x, inferred_geometry.origin_y)
        source_label = {"edge_cuts": "edge_cuts", "placement_file": "board_definition",
                        "footprint_extents": "footprint_bounds_fallback"}.get(inferred_geometry.source, inferred_geometry.source)
        print(f"origin source: {source_label}")
        print(f"origin: inferred origin_x={_fmt_num(model.board.origin_x)} origin_y={_fmt_num(model.board.origin_y)}")
    run_validation = bool(args.validate or args.check)
    new_text, messages, report = apply_placements(source_text, model, strict=args.strict,
                                                  allow_suffix_match=not args.no_suffix_match,
                                                  validate=run_validation, safe=bool(args.safe),
                                                  allow_large_move=bool(args.allow_large_move),
                                                  allow_outside_board=bool(args.allow_outside_board),
                                                  cardinal_rotations=bool(args.cardinal_rotations),
                                                  safety_fatal=not bool(args.dry_run),
                                                  allow_overlap=bool(args.allow_overlap),
                                                  warn_overlap=bool(args.warn_overlap),
                                                  allow_keepout_overlap=bool(args.allow_keepout_overlap),
                                                  allow_outside_region=bool(args.allow_outside_region),
                                                  best_effort=bool(args.best_effort) and not bool(args.fail_fast),
                                                  fail_fast=bool(args.fail_fast),
                                                  max_floorplan_iterations=(None if args.max_floorplan_iterations is None
                                                                            else int(args.max_floorplan_iterations)),
                                                  optimization_level=args.optimization_level,
                                                  time_budget_seconds=float(args.time_budget_seconds),
                                                  max_candidates_per_rule=(None if args.max_candidates_per_rule is None
                                                                           else int(args.max_candidates_per_rule)),
                                                  progress=bool(args.progress))
    for message in messages:
        print(message)
    if args.debug_write:
        print_generated_uuid_debug(report.get("generated_uuids", []))
    if args.report_json:
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"report: {args.report_json}")
    if args.floorplan_report:
        args.floorplan_report.parent.mkdir(parents=True, exist_ok=True)
        args.floorplan_report.write_text(json.dumps(report.get("floorplan", {}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"floorplan-report: {args.floorplan_report}")
    if args.profile_placement:
        args.profile_placement.parent.mkdir(parents=True, exist_ok=True)
        profile_payload = {
            "runtime": report.get("runtime", {}),
            "spatial_index": report.get("spatial_index", {}),
            "profile": report.get("profile", {}),
        }
        args.profile_placement.write_text(json.dumps(profile_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"profile: {args.profile_placement}")
    for path, text_out in ((args.high_speed_review, lambda: high_speed_review_md(report["high_speed_paths"])),
                           (args.power_review, lambda: power_review_md(report["power_islands"])),
                           (args.mechanical_review, lambda: mechanical_review_md(report["mechanical_review"])),
                           (args.ai_edit_hints, lambda: ai_edit_hints_md(report))):
        if path is not None:
            content = text_out()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
            print(f"review: {path}")
    if args.explain_placement:
        ref = report.get("effective_aliases", {}).get(args.explain_placement, args.explain_placement)
        explanation = report.get("placement_search", {}).get(ref)
        if explanation is None:
            print(json.dumps({"ref": ref, "error": "no scored placement search recorded"}, indent=2, sort_keys=True))
        else:
            print(json.dumps({"ref": ref, **explanation}, indent=2, sort_keys=True))

    changed = new_text != source_text
    if args.validate:
        errors = int(report.get("validation_errors", 0))
        print(f"validate: {errors} error(s), {report['placements_applied']} placement(s), changed={changed}")
        return 1 if errors else 0
    if args.check:
        if changed:
            print("check: placement changes are pending", file=sys.stderr)
            return 1
        print("check: placement is up to date")
        return 0
    if args.dry_run:
        print(f"dry-run: {report['placements_applied']} placement(s) would be applied to "
              f"{report['footprints_total']} footprint(s); changed={changed}")
        print(f"  collisions={report.get('collision_count', 0)} spacing_violations={report.get('spacing_violation_count', 0)} "
              f"fallback_bboxes={len(report.get('bbox_warnings', []))} auto_adjustments={len(report.get('auto_adjustments', []))}")
        for adj in report.get("auto_adjustments", []):
            print(f"  adjusted {adj['ref']}: requested=({_fmt_num(adj['requested_x'])}, {_fmt_num(adj['requested_y'])}) "
                  f"placed=({_fmt_num(adj['placed_x'])}, {_fmt_num(adj['placed_y'])}) reason={adj['reason']}")
        for item in report.get("placements", []):
            rot_change = "yes" if item["rotation_changed"] else "no"
            outside = "yes" if item["outside_board"] else "no"
            print(f"  {item['ref']}: "
                  f"old=({_fmt_num(item['original_x'])}, {_fmt_num(item['original_y'])}, {_fmt_num(item['original_rot'])}) "
                  f"new=({_fmt_num(item['x'])}, {_fmt_num(item['y'])}, {_fmt_num(item['final_rot'])}) "
                  f"delta=({_fmt_num(item['delta_x'])}, {_fmt_num(item['delta_y'])}) "
                  f"rot_changed={rot_change} outside_board={outside}")
        return 0

    destination = args.pcb if args.in_place else (args.output or default_output_path(args.pcb))
    sanity_check_output(source_text, new_text)
    if destination == args.pcb and not args.no_backup:
        backup = args.pcb.with_suffix(args.pcb.suffix + ".bak")
        shutil.copy2(args.pcb, backup)
        print(f"backup: {backup}")
    atomic_write_text(destination, new_text)
    sanity_check_output(source_text, destination.read_text(encoding="utf-8"))
    print(f"wrote: {destination}")
    print(f"summary: {report['placements_applied']} placement(s), {report['rules_total']} rule(s), "
          f"{report['footprints_total']} footprint(s)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point that reports placement failures without a Python traceback."""

    try:
        return _main(argv)
    except PlacementError as exc:
        print(f"pcb-place error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
