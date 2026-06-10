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
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__version__ = "0.6.0"

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

    def add(self, rule_type: str, **kwargs: Any) -> None:
        self.rules.append({"type": rule_type, **kwargs})

    def alias(self, name: str, ref: str) -> None:
        if not name or not ref:
            raise PlacementError("Alias(name, ref) requires non-empty strings")
        self.aliases[name] = ref

    def region(self, name: str, *, x: Number, y: Number, w: Number, h: Number,
               role: Optional[str] = None, note: Optional[str] = None) -> None:
        if not name:
            raise PlacementError("Region(name, ...) requires a non-empty name")
        if w <= 0 or h <= 0:
            raise PlacementError("Region(w=..., h=...) must be positive")
        self.regions[name] = {
            "name": name, "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "role": role, "note": note,
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
               role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Region() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.region(str(name), x=x, y=y, w=w, h=h, role=role, note=note)

    def Lock(ref: str, *, reason: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if kwargs:
            raise PlacementError(f"Lock() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.add("lock", ref=_normalize_ref(ref), note=note or reason)

    def Anchor(ref: str, *, x: Optional[Number] = None, y: Optional[Number] = None,
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
        model.add(
            "anchor",
            ref=_normalize_ref(ref),
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

    def Fixed(ref: str, **kwargs: Any) -> None:
        kwargs.setdefault("lock", True)
        Anchor(ref, **kwargs)
        model.rules[-1]["type"] = "fixed"

    def Corner(ref: str, *, corner: str, inset: Number | Point = 3,
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
        model.add("corner", ref=_normalize_ref(ref), x=x, y=y, rot=_rot_or_none(rot),
                  role=role, lock=bool(lock), corner=c, note=note, extra=dict(kwargs))

    def Edge(ref: str, *, edge: str, offset: Optional[Number] = None, inset: Number = 0,
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
        model.add("edge", ref=_normalize_ref(ref), x=px, y=py,
                  rot=_rot_or_none(rot), role=role,
                  lock=bool(lock), edge=e, note=note, extra=dict(kwargs))

    def Between(ref: str, *, a: str, b: str, t: Number = 0.5,
                dx: Number = 0, dy: Number = 0, offset: Optional[Number] = None,
                rot: Optional[Number | str] = None, align: Optional[str] = None,
                role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if align is not None and rot is None:
            rot = align
        model.add("between", ref=_normalize_ref(ref), a=_normalize_ref(a), b=_normalize_ref(b),
                  t=float(t), dx=float(dx), dy=float(dy),
                  offset=None if offset is None else float(offset), rot=_rot_or_none(rot),
                  role=role, note=note, extra=dict(kwargs))

    def Inline(ref: str, *, a: str, b: str, t: Number = 0.5,
               rot: Optional[Number | str] = None, align: Optional[str] = None,
               role: Optional[str] = None, note: Optional[str] = None, **kwargs: Any) -> None:
        if align is not None and rot is None:
            rot = align
        model.add("inline", ref=_normalize_ref(ref), a=_normalize_ref(a), b=_normalize_ref(b),
                  t=float(t), rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

    def Satellite(ref: str, *, parent: str, side: str = "right", distance: Number = 2,
                  index: int = 0, pitch: Number = 1.5, dx: Number = 0, dy: Number = 0,
                  rot: Optional[Number | str] = None, role: Optional[str] = None,
                  note: Optional[str] = None, **kwargs: Any) -> None:
        model.add("satellite", ref=_normalize_ref(ref), parent=_normalize_ref(parent),
                  side=side.lower().replace("-", "_"), distance=float(distance),
                  index=int(index), pitch=float(pitch), dx=float(dx), dy=float(dy),
                  rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

    def Orbit(*, refs: Sequence[str], parent: str, radius: Number = 3,
              start_angle: Number = 0, step_angle: Optional[Number] = None,
              rot: Optional[Number | str] = None, role: Optional[str] = None,
              note: Optional[str] = None, **kwargs: Any) -> None:
        """Distribute support parts around an anchor in polar coordinates."""
        refs = list(refs)
        if not refs:
            return
        if step_angle is None:
            step_angle = 360.0 / len(refs)
        model.add("orbit", refs=[_normalize_ref(r) for r in refs], parent=_normalize_ref(parent),
                  radius=float(radius), start_angle=float(start_angle), step_angle=float(step_angle),
                  rot=_rot_or_none(rot), role=role, note=note, extra=dict(kwargs))

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
                **kwargs: Any) -> None:
        model.add("keepout", name=str(name), x=float(x), y=float(y), w=float(w), h=float(h),
                  layers=str(layers), role=role, note=note, extra=dict(kwargs))

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
        "Lock": Lock,
        "Component": Component,
        "Anchor": Anchor,
        "Fixed": Fixed,
        "Corner": Corner,
        "Edge": Edge,
        "Between": Between,
        "Inline": Inline,
        "Satellite": Satellite,
        "Orbit": Orbit,
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




def _edge_cuts_points(text: str) -> List[Point]:
    """Return points from supported Edge.Cuts drawings.

    KiCad stores board outlines as general graphics in the root board file.
    This intentionally supports rectangular geometry first while keeping the
    parser as a simple drawing-to-point collector for future shapes.
    """

    points: List[Point] = []
    for match in re.finditer(r'(?m)^\s*\((gr_rect|gr_line)\b', text):
        block = text[match.start():_find_matching_paren(text, text.find("(", match.start()))]
        if '(layer "Edge.Cuts")' not in block:
            continue
        if match.group(1) == "gr_rect":
            point_match = re.search(
                r'\(start\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\).*?\(end\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)',
                block,
                flags=re.S,
            )
            if point_match:
                x0, y0, x1, y1 = map(float, point_match.groups())
                points.extend([(x0, y0), (x1, y1)])
        elif match.group(1) == "gr_line":
            point_match = re.search(
                r'\(start\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\).*?\(end\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)',
                block,
                flags=re.S,
            )
            if point_match:
                x0, y0, x1, y1 = map(float, point_match.groups())
                points.extend([(x0, y0), (x1, y1)])
    return points

def parse_edge_cuts_geometry(text: str) -> Optional[BoardGeometry]:
    points = _edge_cuts_points(text)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    origin_x, origin_y = min(xs), min(ys)
    width, height = max(xs) - origin_x, max(ys) - origin_y
    if width <= 0 or height <= 0:
        raise PlacementError("Edge.Cuts geometry has non-positive width or height")
    # For now pcb-place only understands rectangular board geometry.
    return BoardGeometry(origin_x, origin_y, width, height, "edge_cuts")


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


def _outline_text(geometry: BoardGeometry) -> str:
    x0, y0, x1, y1 = geometry.min_x, geometry.min_y, geometry.max_x, geometry.max_y
    return (
        f'  (gr_line (start {_fmt_num(x0)} {_fmt_num(y0)}) (end {_fmt_num(x1)} {_fmt_num(y0)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "pcb-place-edge-1"))\n'
        f'  (gr_line (start {_fmt_num(x1)} {_fmt_num(y0)}) (end {_fmt_num(x1)} {_fmt_num(y1)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "pcb-place-edge-2"))\n'
        f'  (gr_line (start {_fmt_num(x1)} {_fmt_num(y1)}) (end {_fmt_num(x0)} {_fmt_num(y1)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "pcb-place-edge-3"))\n'
        f'  (gr_line (start {_fmt_num(x0)} {_fmt_num(y1)}) (end {_fmt_num(x0)} {_fmt_num(y0)}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "pcb-place-edge-4"))\n'
    )


def emit_board_outline(text: str, geometry: BoardGeometry) -> str:
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
    return text[:insert_at] + _outline_text(geometry) + text[insert_at:]

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
              *, allow_arbitrary_rotation: bool = False) -> None:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {ref!r} for {why}")
            return
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
                self._warn_or_raise(f"locked footprint {actual_ref!r} cannot be moved by {why}; locked by {self.locked[actual_ref]}")
                return
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
        self.positions[actual_ref] = (float(x), float(y), float(new_rot))
        suffix = f"  # {note}" if note else ""
        rot_label = _fmt_num(new_rot) if explicit_rot else "preserve"
        self.messages.append(
            Message("place", f"place {actual_ref:>16s} -> x={_fmt_num(x):>8s} y={_fmt_num(y):>8s} "
                             f"rot={rot_label:>8s}  {why}{suffix}")
        )

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

    def _allow_arbitrary_rotation(self, rule: Dict[str, Any]) -> bool:
        return bool(rule.get("allow_arbitrary_rotation") or rule.get("extra", {}).get("allow_arbitrary_rotation"))

    def _apply_linear_collection(self, rule: Dict[str, Any], why: str) -> None:
        sx, sy = rule["start"]
        px, py = rule["pitch"]
        for i, ref in enumerate(rule["refs"]):
            x, y = _board_to_abs(self.model, sx + px * i, sy + py * i)
            self.place(ref, x, y, self.resolve_rot(rule.get("rot")), why, rule.get("note"),
                       allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

    def apply(self) -> None:
        """Apply rules in deterministic passes.

        Absolute rules run first; relationship rules then reference the updated
        coordinates. Documentation-only rules are reported last.
        """

        # Absolute pass.
        for rule in self.model.rules:
            typ = rule["type"]
            if typ in {"anchor", "fixed", "corner", "edge"}:
                if rule.get("relative_to") is not None:
                    bx, by = self.get_pos(rule["relative_to"])
                    x = bx + float(rule.get("dx", 0.0))
                    y = by + float(rule.get("dy", 0.0))
                else:
                    x, y = _board_to_abs(self.model, float(rule["x"]), float(rule["y"]))
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot")), typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))
                if rule.get("lock"):
                    self.lock(rule["ref"], f"{typ} rule")
            elif typ == "lock":
                self.lock(rule["ref"], rule.get("note") or "Lock rule")
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
                               allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

        # Relational pass.
        for rule in self.model.rules:
            typ = rule["type"]
            if typ in {"between", "inline"}:
                a = self.get_pos(rule["a"])
                b = self.get_pos(rule["b"])
                t = float(rule.get("t", 0.5))
                x = a[0] + (b[0] - a[0]) * t + float(rule.get("dx", 0.0))
                y = a[1] + (b[1] - a[1]) * t + float(rule.get("dy", 0.0))
                if rule.get("offset") is not None:
                    vx, vy = b[0] - a[0], b[1] - a[1]
                    length = math.hypot(vx, vy) or 1.0
                    x += (-vy / length) * float(rule["offset"])
                    y += (vx / length) * float(rule["offset"])
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot"), a, b), typ, rule.get("note"),
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

            elif typ == "satellite":
                px, py = self.get_pos(rule["parent"])
                side = rule.get("side", "right")
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
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

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
                               allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

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
                           allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

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
                               allow_arbitrary_rotation=self._allow_arbitrary_rotation(rule))

        # Documentation/reporting pass.
        for rule in self.model.rules:
            typ = rule["type"]
            if typ == "keepout":
                self.messages.append(Message("note", f"keepout {rule['name']!r}: x={_fmt_num(rule['x'])} "
                                                   f"y={_fmt_num(rule['y'])} w={_fmt_num(rule['w'])} "
                                                   f"h={_fmt_num(rule['h'])} layers={rule['layers']!r} "
                                                   "parsed but not emitted yet"))
            elif typ == "corridor":
                self.messages.append(Message("note", f"corridor {rule['name']!r}: {rule['a']} -> {rule['b']} "
                                                   f"width={_fmt_num(rule['width'])} "
                                                   f"clearance={_fmt_num(rule['clearance'])} parsed but not emitted yet"))


def validate_placements(engine: PlacementEngine, *, min_spacing: float = 0.25) -> List[Message]:
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
            if not geometry.contains(x, y):
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
                messages.append(Message("error", f"{a!r} and {b!r} have overlapping/near-coincident origins"))

    for rule in engine.model.rules:
        if rule["type"] != "keepout":
            continue
        x0, y0 = float(rule["x"]), float(rule["y"])
        x1, y1 = x0 + float(rule["w"]), y0 + float(rule["h"])
        for ref in sorted(engine.updates):
            x, y, _rot = engine.positions[ref]
            if x0 <= x <= x1 and y0 <= y <= y1:
                messages.append(Message("error", f"{ref!r} origin lies inside keepout {rule['name']!r}"))

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
            messages.append(Message("error", f"duplicate resolved placement for {ref!r}; {count} rules target the same footprint"))

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
                     cardinal_rotations: bool = False, safety_fatal: bool = True) -> Tuple[str, List[Message], Dict[str, Any]]:
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
    validation_messages = validate_placements(engine) if validate else []
    safety_messages = validate_safe_placements(engine, allow_large_move=allow_large_move,
                                               allow_outside_board=allow_outside_board) if safe else []
    engine.messages.extend(validation_messages)
    if strict and any(m.level == "error" for m in engine.messages):
        raise PlacementError("validation failed: " + "; ".join(m.text for m in engine.messages if m.level == "error"))
    engine.messages.extend(safety_messages)
    if safe and safety_fatal and any(m.level == "error" for m in engine.messages):
        raise PlacementError("validation failed: " + "; ".join(m.text for m in engine.messages if m.level == "error"))
    rewritten = text
    if model.board.emit_outline and defined_geometry is not None:
        rewritten = emit_board_outline(rewritten, defined_geometry)
    for ref, update in sorted(engine.updates.items(), key=lambda kv: footprints[kv[0]].start, reverse=True):
        fp = footprints[ref]
        rewritten = rewritten[:fp.start] + _replace_at(fp.text, update.x, update.y, update.write_rot) + rewritten[fp.end:]
    report = {
        "version": __version__,
        "footprints_total": len(footprints),
        "rules_total": len(model.rules),
        "placements_applied": len(engine.updates),
        "aliases": dict(model.aliases),
        "imported_aliases": dict(model.imported_aliases),
        "effective_aliases": engine.alias_map(),
        "alias_diagnostics": dataclasses.asdict(model.alias_diagnostics),
        "regions": dict(model.regions),
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


def emit_outline_only(pcb_path: Path, model: PlacementModel, output_path: Path) -> int:
    geometry = board_geometry_from_definition(model.board)
    if geometry is None:
        raise PlacementError("--emit-outline-only requires Board(width=..., height=...) in the placement file")
    text = pcb_path.read_text(encoding="utf-8")
    out = emit_board_outline(text, geometry)
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
    parser.add_argument("--print-bounds", action="store_true", help="Print input footprint coordinate bounds and board geometry and exit")
    parser.add_argument("--print-board", action="store_true", help="Print authoritative board geometry and exit")
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
        parser.error("ppl file is required unless --list-refs, --print-bounds, --print-board, or --list-aliases is used")
    if not args.ppl.exists():
        raise SystemExit(f"PPL file not found: {args.ppl}")
    if args.output and args.in_place:
        parser.error("--output and --in-place are mutually exclusive")

    model = load_ppl(args.ppl)
    if args.print_board:
        return print_board(args.pcb, model, fmt=args.format)
    if args.emit_outline_only is not None:
        return emit_outline_only(args.pcb, model, args.emit_outline_only)
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
                                                  safety_fatal=not bool(args.dry_run))
    for message in messages:
        print(message)
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
