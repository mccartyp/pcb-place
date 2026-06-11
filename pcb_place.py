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
import json
import math
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__version__ = "0.9.0"

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

    def as_report(self) -> Dict[str, float]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class BBoxInfo:
    ref: str
    bbox: BBox
    fallback: bool = False
    warning: Optional[str] = None


@dataclasses.dataclass
class ClearanceRules:
    """Configurable minimum clearance rules in millimeters."""

    default: float = 0.25
    passive_to_passive: float = 0.20
    passive_to_ic: float = 0.50
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
        return dataclasses.asdict(self) | self.bounds()

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
               role: Optional[str] = None, note: Optional[str] = None, priority: Optional[Number] = None) -> None:
        if not name:
            raise PlacementError("Region(name, ...) requires a non-empty name")
        if w <= 0 or h <= 0:
            raise PlacementError("Region(w=..., h=...) must be positive")
        self.regions[name] = {
            "name": name, "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "role": role, "note": note, "priority": None if priority is None else float(priority),
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


def infer_geometry_from_footprints(footprints: Mapping[str, Footprint]) -> BoardGeometry:
    bounds = footprint_bounds(footprints)
    return BoardGeometry(bounds["min_x"], bounds["min_y"],
                         bounds["max_x"] - bounds["min_x"], bounds["max_y"] - bounds["min_y"],
                         "inferred_from_footprints")


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


def _parse_sexp_netlist(text: str) -> Tuple[Dict[str, str], List[str]]:
    tree = _parse_sexp(_sexp_tokens(text))
    aliases: Dict[str, str] = {}
    conflicts: Dict[str, Set[str]] = {}
    path_names = {k.lower() for k in _PATH_KEYS}
    ref_names = {k.lower() for k in _REF_KEYS}
    for form in _walk_sexp(tree):
        values: Dict[str, str] = {}
        for item in form[1:]:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str):
                key = item[0].lower()
                if key in path_names | ref_names and not isinstance(item[1], list):
                    values[key] = str(item[1])
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
            raise PlacementError("Board(width=..., height=...) dimensions must be positive")
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
               priority: Optional[Number] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Region() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.region(str(name), x=x, y=y, w=w, h=h, role=role, note=note, priority=priority)

    def Spacing(*, default: Number = 0.25, passive_to_passive: Number = 0.20,
                passive_to_ic: Number = 0.50, ic_to_ic: Number = 0.75,
                connector: Number = 1.00, mechanical: Number = 1.00, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Spacing() unknown parameter(s): {', '.join(sorted(kwargs))}")
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
             lock: bool = False, note: Optional[str] = None, **kwargs: Any) -> None:
        w, h, _ox, _oy = _require_board_size(model, "Edge")
        e = edge.lower().replace("-", "_")
        if e in ("left", "right"):
            px = float(inset) if e == "left" else w - float(inset)
            py = float(y) if y is not None else float(offset if offset is not None else h / 2.0)
        elif e in ("top", "bottom"):
            px = float(x) if x is not None else float(offset if offset is not None else w / 2.0)
            py = float(inset) if e == "top" else h - float(inset)
        else:
            raise PlacementError(f"Unknown edge {edge!r}")
        rule = dict(type="edge", ref=None if ref is None else _normalize_ref(ref), x=px, y=py,
                    rot=_rot_or_none(rot), role=role,
                    lock=bool(lock), edge=e, note=note, extra=dict(kwargs))
        if ref is None:
            return rule
        model.rules.append(rule)
        return None

    def Between(ref: Optional[str] = None, *, a: str, b: str, t: Number = 0.5,
                dx: Number = 0, dy: Number = 0, offset: Optional[Number] = None,
                rot: Optional[Number | str] = None, align: Optional[str] = None,
                role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if align is not None and rot is None:
            rot = align
        rule = dict(type="between", ref=None if ref is None else _normalize_ref(ref), a=_normalize_ref(a), b=_normalize_ref(b),
                    t=float(t), dx=float(dx), dy=float(dy),
                    offset=None if offset is None else float(offset), rot=_rot_or_none(rot),
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
        "Pullup": Pullup,
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


def footprint_bbox_at(fp: Footprint, x: float, y: float, rot: float, model: PlacementModel) -> BBoxInfo:
    cls = _part_class_for(model, fp.ref)
    local = _parse_fp_local_bbox(fp)
    fallback = False
    warning = None
    if local is None:
        local, warning = _fallback_local_bbox(fp, cls)
        fallback = True
    return BBoxInfo(fp.ref, _transform_local_bbox(local, x, y, rot), fallback, warning)


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
        if rule.get("region") and rule.get("ref"):
            actual = engine.resolve_ref(str(rule["ref"]))
            if actual:
                rule_region_by_ref[actual] = str(rule["region"])
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
                 cardinal_rotations: bool = False, board_geometry: Optional[BoardGeometry] = None) -> None:
        self.footprints = dict(footprints)
        self.model = model
        self.strict = strict
        self.allow_suffix_match = allow_suffix_match
        self.cardinal_rotations = cardinal_rotations
        self.board_geometry = board_geometry
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
        for error in model.alias_diagnostics.errors:
            self.messages.append(Message("error", error))
        for warning in model.alias_diagnostics.warnings:
            self.messages.append(Message("warn", warning))
        if self.strict and model.alias_diagnostics.errors:
            raise PlacementError("netlist alias validation failed: " + "; ".join(model.alias_diagnostics.errors))

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
              rule: Optional[Mapping[str, Any]] = None) -> None:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {ref!r} for {why}")
            return
        rule = rule or {}
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
            placed = self._find_non_overlapping_position(actual_ref, float(x), float(y), float(new_rot),
                                                         clearance_override=clearance_override,
                                                         candidate_sides=candidate_sides,
                                                         region=self._region_bbox(rule.get("region")),
                                                         allow_keepout_overlap=self._rule_flag(rule, "allow_keepout_overlap", False))
            if placed is None:
                raise PlacementError(f"{actual_ref!r} could not be placed by {why} without violating clearance or board bounds; requested x={_fmt_num(x)} y={_fmt_num(y)}")
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
        self.positions[actual_ref] = (float(x), float(y), float(new_rot))
        suffix = f"  # {note}" if note else ""
        rot_label = _fmt_num(new_rot) if explicit_rot else "preserve"
        self.messages.append(
            Message("place", f"place {actual_ref:>16s} -> x={_fmt_num(x):>8s} y={_fmt_num(y):>8s} "
                             f"rot={rot_label:>8s}  {why}{suffix}")
        )

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
        for actual in members:
            x, y, rot = self.positions[actual]
            self.place(actual, x + dx, y + dy, None, f"cluster {name}", rule.get("note"))
            self.last_cluster_by_ref[actual] = name
        self.clusters.append({
            "name": name,
            "anchor": anchor_ref,
            "member_count": len(members),
            "delta": [dx, dy],
            "old_anchor": [old_anchor[0], old_anchor[1]],
            "new_anchor": [new_anchor[0], new_anchor[1]],
        })

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

    def _rule_flag(self, rule: Mapping[str, Any], name: str, default: bool = False) -> bool:
        return bool(rule.get(name, rule.get("extra", {}).get(name, default)))

    def _rule_priority(self, rule: Mapping[str, Any], actual_ref: str) -> float:
        value = rule.get("priority")
        if value is None:
            value = rule.get("extra", {}).get("priority")
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

    def _near_point_candidates(self, x: float, y: float, distance: float, side: str) -> List[Tuple[str, float, float]]:
        vectors = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
        sides = ["top", "right", "bottom", "left"] if side == "auto" else [side]
        bad = [s for s in sides if s not in vectors]
        if bad:
            raise PlacementError(f"Unknown side {bad[0]!r}")
        return [(s, x + vectors[s][0] * distance, y + vectors[s][1] * distance) for s in sides]

    def _pad_centroid(self, parent_ref: str, pads: str | Sequence[str]) -> Point:
        actual_parent = self.resolve_ref(parent_ref)
        if actual_parent is None or actual_parent not in self.footprints:
            raise PlacementError(f"NearPad parent {parent_ref!r} is missing")
        names = [str(pads)] if isinstance(pads, str) else [str(p) for p in pads]
        if not names:
            raise PlacementError("NearPad(pad=...) requires at least one pad")
        centers = _parse_pad_local_centers(self.footprints[actual_parent])
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

    def _place_near_point(self, rule: Mapping[str, Any], origin: Point, why: str) -> None:
        side = str(rule.get("side", "auto"))
        distance = float(rule.get("distance", 1.5))
        last_error: Optional[Exception] = None
        for candidate_side, x, y in self._near_point_candidates(origin[0], origin[1], distance, side):
            try:
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot")), why, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(dict(rule)),
                           avoid_overlap=True, clearance_override=self._clearance_override(dict(rule)),
                           candidate_sides=[candidate_side], rule=rule)
                return
            except PlacementError as exc:
                last_error = exc
                if side != "auto":
                    raise
        if last_error is not None:
            raise last_error

    def _collides_at(self, ref: str, x: float, y: float, rot: float, *, clearance_override: Optional[float] = None,
                    region: Optional[BBox] = None, allow_keepout_overlap: bool = False) -> Optional[str]:
        if ref not in self.footprints:
            return None
        test_info = footprint_bbox_at(self.footprints[ref], x, y, rot, self.model)
        if not _board_contains_bbox(self.board_geometry, test_info.bbox):
            return "would leave board bounds"
        if region is not None and not region.contains_bbox(test_info.bbox):
            return "would leave region"
        if not allow_keepout_overlap:
            for keepout in keepout_rules(self.model):
                if bool(keepout.get("extra", {}).get("allow_keepout_overlap", False)):
                    continue
                if test_info.bbox.overlaps(_rect_to_abs_bbox(self.model, keepout)):
                    return f"avoided keepout {keepout['name']!r}"
        obstacle_refs = set(self.positions)
        for other in sorted(obstacle_refs):
            if other == ref or other not in self.footprints or not _same_physical_side(self.footprints[ref], self.footprints[other]):
                continue
            ox, oy, orot = self.positions[other]
            other_info = footprint_bbox_at(self.footprints[other], ox, oy, orot, self.model)
            required = clearance_override
            if required is None:
                required = self.model.clearance.required_for(_part_class_for(self.model, ref), _part_class_for(self.model, other))
            if test_info.bbox.expanded(required).overlaps(other_info.bbox):
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
        vectors = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
        candidates: List[Tuple[float, float]] = []
        n = int(max_r / step)
        # Slide along the requested side first, then search outward in side directions.
        for side in sides:
            if side in {"top", "bottom"}:
                for k in range(1, n + 1):
                    d = k * step
                    candidates.extend([(x + d, y), (x - d, y)])
            elif side in {"left", "right"}:
                for k in range(1, n + 1):
                    d = k * step
                    candidates.extend([(x, y + d), (x, y - d)])
        for k in range(1, n + 1):
            d = k * step
            for side in sides:
                vx, vy = vectors.get(side, (0.0, 0.0))
                candidates.append((x + vx * d, y + vy * d))
        seen: Set[Tuple[int, int]] = set()
        for cx, cy in candidates:
            key = (round(cx / 1e-6), round(cy / 1e-6))
            if key in seen:
                continue
            seen.add(key)
            if self._collides_at(ref, cx, cy, rot, clearance_override=clearance_override,
                                 region=region, allow_keepout_overlap=allow_keepout_overlap) is None:
                return (cx, cy, first)
        return None

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

        for rule_index, rule in enumerate(self.model.rules):
            rule["_index"] = rule_index
            typ = rule["type"]
            if typ in {"anchor", "fixed", "corner", "edge"}:
                x, y = self._placement_target(rule)
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot")), typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule), rule=rule)
                if rule.get("lock") or self._rule_flag(rule, "locked", False):
                    self.lock(rule["ref"], f"{typ} rule")

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
                x, y = self._placement_target(rule)
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot"), a, b), typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                           avoid_overlap=True, clearance_override=self._clearance_override(rule), rule=rule)

            elif typ == "satellite":
                px, py = self.get_pos(rule["parent"])
                side = rule.get("side", "right")
                if side == "auto":
                    self._place_near_point(rule, (px, py), typ)
                    continue
                dist = float(rule.get("distance", 2.0))
                idx = int(rule.get("index", 0))
                pitch = float(rule.get("pitch", 1.5))
                dx = float(rule.get("dx", 0.0))
                dy = float(rule.get("dy", 0.0))
                if side == "right":
                    x, y = px + dist, py + idx * pitch
                elif side == "left":
                    x, y = px - dist, py + idx * pitch
                elif side == "top":
                    x, y = px + idx * pitch, py - dist
                elif side == "bottom":
                    x, y = px + idx * pitch, py + dist
                else:
                    raise PlacementError(f"Unknown satellite side {side!r}")
                self.place(rule["ref"], x + dx, y + dy, self.resolve_rot(rule.get("rot")), typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule),
                           avoid_overlap=True, clearance_override=self._clearance_override(rule),
                           candidate_sides=[side] if side in {"top", "right", "bottom", "left"} else None, rule=rule)

            elif typ == "near_pad":
                origin = self._pad_centroid(str(rule["parent"]), rule["pad"])
                self._place_near_point(rule, origin, "near_pad")

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


def validate_placements(engine: PlacementEngine, *, min_spacing: float = 0.25,
                        allow_overlap: bool = False, warn_overlap: bool = False,
                        allow_outside_board: bool = False,
                        allow_keepout_overlap: bool = False,
                        allow_outside_region: bool = False) -> List[Message]:
    """Run lightweight, CI-friendly placement validation.

    KiCad footprints can have complex outlines.  This first validator uses
    footprint origins as conservative proxies: it detects duplicate/near-
    duplicate placement, board-boundary violations, point-in-keepout violations,
    and unresolved relationship problems that were already emitted as warnings.
    Future versions can add true courtyard/bounding-box collision checks.
    """

    messages: List[Message] = []
    geometry = engine.board_geometry
    if geometry is not None and geometry.source != "inferred_from_footprints":
        if geometry.width <= 0 or geometry.height <= 0:
            messages.append(Message("error", "Board geometry dimensions must be positive"))
        for ref in sorted(engine.updates):
            x, y, _rot = engine.positions[ref]
            if not geometry.contains(x, y) and not allow_outside_board:
                messages.append(Message("error", f"{ref!r} is outside Board geometry ({geometry.source}): x={_fmt_num(x)} y={_fmt_num(y)}"))

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
                messages.append(Message("warn" if (allow_overlap or warn_overlap) else "error",
                                        f"{a!r} and {b!r} have overlapping/near-coincident origins"))

    for item in keepout_violations(engine, refs=engine.updates, allow_keepout_overlap=allow_keepout_overlap):
        messages.append(Message("error", f"{item['ref']!r} bbox overlaps keepout {item['keepout']!r}"))
    for item in region_violations(engine, refs=engine.updates, allow_outside_region=allow_outside_region):
        messages.append(Message("error", f"{item['ref']!r} bbox is outside region {item['region']!r}"))

    collisions, violations, bbox_warnings = spacing_analysis(engine, refs=engine.updates)
    for warning in bbox_warnings:
        messages.append(Message("warn", warning["message"]))
    overlap_level = "warn" if (allow_overlap or warn_overlap) else "error"
    for item in collisions:
        messages.append(Message(overlap_level,
            f"ERROR: {item['ref_a']} overlaps {item['ref_b']}\n"
            f"  {item['ref_a']} bbox: {item['bbox_a']}\n"
            f"  {item['ref_b']} bbox: {item['bbox_b']}\n"
            f"  required clearance: {_fmt_num(item['required_clearance'])} mm"))
    for item in violations:
        level = "warn" if (allow_overlap or warn_overlap) else "error"
        messages.append(Message(level,
            f"{item['ref_a']} and {item['ref_b']} spacing {_fmt_num(item['actual_clearance'])} mm is below "
            f"required {_fmt_num(item['required_clearance'])} mm ({item['class_a']} to {item['class_b']})"))

    return messages


def validate_safe_placements(engine: PlacementEngine, *, allow_large_move: bool = False,
                             allow_outside_board: bool = False) -> List[Message]:
    """Pre-write safety checks intended to prevent dangerous KiCad output."""

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
        if not all(math.isfinite(v) for v in (update.x, update.y, update.final_rot)):
            messages.append(Message("error", f"{update.ref!r} has non-finite placement coordinate or rotation"))
        if update.outside_board and not allow_outside_board:
            messages.append(Message("error", f"{update.ref!r} would be outside Board bounds at x={_fmt_num(update.x)} y={_fmt_num(update.y)}; use --allow-outside-board to override"))
        far_x = update.x < safe_bounds["min_x"] - margin_x or update.x > safe_bounds["max_x"] + margin_x
        far_y = update.y < safe_bounds["min_y"] - margin_y or update.y > safe_bounds["max_y"] + margin_y
        if (far_x or far_y) and not allow_large_move:
            messages.append(Message("error", f"{update.ref!r} would move far outside BoardGeometry bounds; use --allow-large-move to override"))
    return messages


def apply_placements(text: str, model: PlacementModel, *, strict: bool = False,
                     allow_suffix_match: bool = True, validate: bool = False, safe: bool = False,
                     allow_large_move: bool = False, allow_outside_board: bool = False,
                     cardinal_rotations: bool = False, safety_fatal: bool = True,
                     allow_overlap: bool = False, warn_overlap: bool = False,
                     allow_keepout_overlap: bool = False,
                     allow_outside_region: bool = False) -> Tuple[str, List[Message], Dict[str, Any]]:
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
    engine = PlacementEngine(footprints, model, strict=strict, allow_suffix_match=allow_suffix_match,
                             cardinal_rotations=cardinal_rotations, board_geometry=board_geometry)
    engine.apply()
    collision_messages = validate_placements(engine, allow_overlap=allow_overlap, warn_overlap=warn_overlap,
                                             allow_outside_board=allow_outside_board,
                                             allow_keepout_overlap=allow_keepout_overlap,
                                             allow_outside_region=allow_outside_region)
    validation_messages = collision_messages if (validate or safe) else []
    safety_messages = validate_safe_placements(engine, allow_large_move=allow_large_move,
                                               allow_outside_board=allow_outside_board) if safe else []
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
    parser.add_argument("--strict", action="store_true", help="Treat missing/ambiguous refs as errors")
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
    parser.add_argument("--version", action="version", version=f"pcb-place {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
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
                        "inferred_from_footprints": "footprint_bounds_fallback"}.get(inferred_geometry.source, inferred_geometry.source)
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
                                                  allow_outside_region=bool(args.allow_outside_region))
    for message in messages:
        print(message)
    if args.debug_write:
        print_generated_uuid_debug(report.get("generated_uuids", []))
    if args.report_json:
        args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"report: {args.report_json}")

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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlacementError as exc:
        print(f"pcb-place error: {exc}", file=sys.stderr)
        raise SystemExit(2)
