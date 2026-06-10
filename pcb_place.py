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
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__version__ = "0.3.0"

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
    layer: Optional[str] = None


@dataclasses.dataclass
class BoardInfo:
    """Board metadata declared in placement.ppl."""

    width: Optional[float] = None
    height: Optional[float] = None
    units: str = "mm"
    origin: Point = (0.0, 0.0)
    name: Optional[str] = None


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

    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _angle_between(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _normalize_ref(ref: str) -> str:
    return str(ref).strip()


def _require_board_size(model: PlacementModel, rule_name: str) -> Tuple[float, float, float, float]:
    if model.board.width is None or model.board.height is None:
        raise PlacementError(f"Board(width=..., height=...) must be declared before {rule_name}(...)")
    ox, oy = model.board.origin
    return model.board.width, model.board.height, ox, oy


def _rot_or_none(rot: Optional[Number | str]) -> Optional[Number | str]:
    return None if rot is None else rot


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
              name: Optional[str] = None, **kwargs: Any) -> None:
        if units != "mm":
            raise PlacementError("Only units='mm' is currently supported")
        if kwargs:
            raise PlacementError(f"Board() unknown parameter(s): {', '.join(sorted(kwargs))}")
        model.board = BoardInfo(
            width=float(width),
            height=float(height),
            units=units,
            origin=_as_point(origin, field="origin"),
            name=name,
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
               rot: Optional[Number | str] = 0, role: Optional[str] = "mounting_hole",
               lock: bool = True, note: Optional[str] = None, **kwargs: Any) -> None:
        w, h, ox, oy = _require_board_size(model, "Corner")
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
            x, y = ox + ix, oy + iy
        elif c == "top_right":
            x, y = ox + w - ix, oy + iy
        elif c == "bottom_left":
            x, y = ox + ix, oy + h - iy
        elif c == "bottom_right":
            x, y = ox + w - ix, oy + h - iy
        else:
            raise PlacementError(f"Unknown corner {corner!r}")
        model.add("corner", ref=_normalize_ref(ref), x=x, y=y, rot=_rot_or_none(rot),
                  role=role, lock=bool(lock), corner=c, note=note, extra=dict(kwargs))

    def Edge(ref: str, *, edge: str, offset: Optional[Number] = None, inset: Number = 0,
             x: Optional[Number] = None, y: Optional[Number] = None,
             rot: Optional[Number | str] = None, role: Optional[str] = None,
             lock: bool = False, note: Optional[str] = None, **kwargs: Any) -> None:
        w, h, ox, oy = _require_board_size(model, "Edge")
        e = edge.lower().replace("-", "_")
        if e in ("left", "right"):
            px = ox + (float(inset) if e == "left" else w - float(inset))
            py = oy + (float(y) if y is not None else float(offset if offset is not None else h / 2.0))
            default_rot = 270 if e == "left" else 90
        elif e in ("top", "bottom"):
            px = ox + (float(x) if x is not None else float(offset if offset is not None else w / 2.0))
            py = oy + (float(inset) if e == "top" else h - float(inset))
            default_rot = 0 if e == "top" else 180
        else:
            raise PlacementError(f"Unknown edge {edge!r}")
        model.add("edge", ref=_normalize_ref(ref), x=px, y=py,
                  rot=_rot_or_none(default_rot if rot is None else rot), role=role,
                  lock=bool(lock), edge=e, note=note, extra=dict(kwargs))

    def Between(ref: str, *, a: str, b: str, t: Number = 0.5,
                dx: Number = 0, dy: Number = 0, offset: Optional[Number] = None,
                rot: Optional[Number | str] = None, role: Optional[str] = None,
                note: Optional[str] = None, **kwargs: Any) -> None:
        model.add("between", ref=_normalize_ref(ref), a=_normalize_ref(a), b=_normalize_ref(b),
                  t=float(t), dx=float(dx), dy=float(dy),
                  offset=None if offset is None else float(offset), rot=_rot_or_none(rot),
                  role=role, note=note, extra=dict(kwargs))

    def Inline(ref: str, *, a: str, b: str, t: Number = 0.5,
               rot: Optional[Number | str] = "path", role: Optional[str] = None,
               note: Optional[str] = None, **kwargs: Any) -> None:
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
               rot: Optional[Number | str] = "mirror", role: Optional[str] = None,
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
            layer=layer_match.group(1) if layer_match else None,
        )
    return footprints


def _replace_at(block: str, x: float, y: float, rot: Optional[float]) -> str:
    """Replace only the footprint-level (at ...) field, preserving indentation."""

    def repl(match: re.Match[str]) -> str:
        indent = match.group(1)
        if rot is None:
            return f"{indent}(at {_fmt_num(x)} {_fmt_num(y)})"
        return f"{indent}(at {_fmt_num(x)} {_fmt_num(y)} {_fmt_num(rot)})"

    new_block, count = re.subn(
        r'(?m)^(\s*)\(at\s+[-+0-9.eE]+\s+[-+0-9.eE]+(?:\s+[-+0-9.eE]+)?\)',
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
                 *, strict: bool = False, allow_suffix_match: bool = True) -> None:
        self.footprints = dict(footprints)
        self.model = model
        self.strict = strict
        self.allow_suffix_match = allow_suffix_match
        self.messages: List[Message] = []
        self.positions: Dict[str, Tuple[float, float, float]] = {
            ref: (fp.x, fp.y, fp.rot) for ref, fp in self.footprints.items()
        }
        self.updates: Dict[str, Tuple[float, float, Optional[float]]] = {}
        self.ref_cache: Dict[str, str] = {}
        self.locked: Dict[str, str] = {}

    def resolve_ref(self, ref: str) -> Optional[str]:
        """Resolve a DSL ref/alias to an actual KiCad footprint reference."""

        ref = self.model.aliases.get(ref, ref)
        if ref in self.ref_cache:
            return self.ref_cache[ref]
        if ref in self.footprints:
            self.ref_cache[ref] = ref
            return ref
        if self.allow_suffix_match:
            suffixes = [f".{ref}", f"/{ref}", f":{ref}"]
            matches = [candidate for candidate in self.footprints if any(candidate.endswith(s) for s in suffixes)]
            if len(matches) == 1:
                self.messages.append(Message("note", f"resolved {ref!r} to hierarchical footprint {matches[0]!r}"))
                self.ref_cache[ref] = matches[0]
                return matches[0]
            if len(matches) > 1:
                self._warn_or_raise(f"ambiguous footprint reference {ref!r}; matches: {', '.join(matches)}")
                return None
        return None

    def _warn_or_raise(self, msg: str) -> None:
        if self.strict:
            raise PlacementError(msg)
        self.messages.append(Message("warn", msg))

    def place(self, ref: str, x: float, y: float, rot: Optional[float], why: str, note: Optional[str] = None) -> None:
        actual_ref = self.resolve_ref(ref)
        if actual_ref is None:
            self._warn_or_raise(f"missing footprint {ref!r} for {why}")
            return
        old_x, old_y, old_rot = self.positions.get(actual_ref, (0.0, 0.0, self.footprints[actual_ref].rot))
        new_rot = old_rot if rot is None else float(rot)
        if actual_ref in self.locked:
            changed = (abs(old_x - x) > 1e-9 or abs(old_y - y) > 1e-9 or abs(old_rot - new_rot) > 1e-9)
            if changed:
                self._warn_or_raise(f"locked footprint {actual_ref!r} cannot be moved by {why}; locked by {self.locked[actual_ref]}")
                return
        self.updates[actual_ref] = (x, y, new_rot)
        self.positions[actual_ref] = (x, y, new_rot)
        suffix = f"  # {note}" if note else ""
        self.messages.append(
            Message("place", f"place {actual_ref:>16s} -> x={_fmt_num(x):>8s} y={_fmt_num(y):>8s} "
                             f"rot={_fmt_num(new_rot):>7s}  {why}{suffix}")
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

    def _apply_linear_collection(self, rule: Dict[str, Any], why: str) -> None:
        sx, sy = rule["start"]
        px, py = rule["pitch"]
        for i, ref in enumerate(rule["refs"]):
            self.place(ref, sx + px * i, sy + py * i, self.resolve_rot(rule.get("rot")), why, rule.get("note"))

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
                    x = float(rule["x"])
                    y = float(rule["y"])
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot")), typ, rule.get("note"))
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
                    self.place(ref, sx + col * px, sy + row * py,
                               self.resolve_rot(rule.get("rot")), "grid", rule.get("note"))

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
                self.place(rule["ref"], x, y, self.resolve_rot(rule.get("rot"), a, b), typ, rule.get("note"))

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
                self.place(rule["ref"], x + dx, y + dy, self.resolve_rot(rule.get("rot")), typ, rule.get("note"))

            elif typ == "orbit":
                cx, cy = self.get_pos(rule["parent"])
                radius = float(rule["radius"])
                start = float(rule["start_angle"])
                step = float(rule["step_angle"])
                for i, ref in enumerate(rule["refs"]):
                    theta = math.radians(start + step * i)
                    x = cx + radius * math.cos(theta)
                    y = cy + radius * math.sin(theta)
                    self.place(ref, x, y, self.resolve_rot(rule.get("rot")), "orbit", rule.get("note"))

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
                           "mirror", rule.get("note"))

            elif typ == "copy_placement":
                source_prefix = rule["source_prefix"]
                target_prefix = rule["target_prefix"]
                dx = float(rule.get("dx", 0.0))
                dy = float(rule.get("dy", 0.0))
                selected = rule.get("refs")
                if selected is None:
                    suffixes = [ref[len(source_prefix):] for ref in self.footprints if ref.startswith(source_prefix)]
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
                    self.place(dst, x + dx, y + dy, rot, "copy_placement", rule.get("note"))

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
    board = engine.model.board
    if board.width is not None and board.height is not None:
        ox, oy = board.origin
        for ref in sorted(engine.updates):
            x, y, _rot = engine.positions[ref]
            if x < ox or y < oy or x > ox + board.width or y > oy + board.height:
                messages.append(Message("error", f"{ref!r} is outside Board bounds: x={_fmt_num(x)} y={_fmt_num(y)}"))

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


def apply_placements(text: str, model: PlacementModel, *, strict: bool = False,
                     allow_suffix_match: bool = True, validate: bool = False) -> Tuple[str, List[Message], Dict[str, Any]]:
    """Apply placement rules and return rewritten KiCad text plus diagnostics."""

    footprints = parse_footprints(text)
    if not footprints:
        raise PlacementError("No footprints found in PCB file")
    engine = PlacementEngine(footprints, model, strict=strict, allow_suffix_match=allow_suffix_match)
    engine.apply()
    validation_messages = validate_placements(engine) if validate else []
    engine.messages.extend(validation_messages)
    if strict and any(m.level == "error" for m in validation_messages):
        raise PlacementError("validation failed: " + "; ".join(m.text for m in validation_messages if m.level == "error"))
    rewritten = text
    for ref, (x, y, rot) in sorted(engine.updates.items(), key=lambda kv: footprints[kv[0]].start, reverse=True):
        fp = footprints[ref]
        rewritten = rewritten[:fp.start] + _replace_at(fp.text, x, y, rot) + rewritten[fp.end:]
    report = {
        "version": __version__,
        "footprints_total": len(footprints),
        "rules_total": len(model.rules),
        "placements_applied": len(engine.updates),
        "aliases": dict(model.aliases),
        "regions": dict(model.regions),
        "locked_refs": sorted(engine.locked),
        "validation_errors": sum(1 for m in engine.messages if m.level == "error"),
        "board": dataclasses.asdict(model.board),
        "updated_refs": sorted(engine.updates),
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


def default_output_path(pcb_path: Path) -> Path:
    if pcb_path.name.endswith(".kicad_pcb"):
        return pcb_path.with_name(pcb_path.name[:-10] + ".placed.kicad_pcb")
    return pcb_path.with_suffix(pcb_path.suffix + ".placed")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcb-place", description="Apply placement.ppl rules to a KiCad .kicad_pcb file")
    parser.add_argument("pcb", type=Path, nargs="?", help="Path to input .kicad_pcb file")
    parser.add_argument("ppl", type=Path, nargs="?", help="Path to placement .ppl file")
    parser.add_argument("-o", "--output", type=Path, help="Output .kicad_pcb path. Defaults to <input>.placed.kicad_pcb")
    parser.add_argument("--in-place", action="store_true", help="Edit the input PCB in place")
    parser.add_argument("--dry-run", action="store_true", help="Report placements without writing output")
    parser.add_argument("--validate", action="store_true", help="Run placement validation without writing output")
    parser.add_argument("--check", action="store_true", help="Fail if applying placement would change the input file")
    parser.add_argument("--no-backup", action="store_true", help="Do not write .bak when editing in-place")
    parser.add_argument("--strict", action="store_true", help="Treat missing/ambiguous refs as errors")
    parser.add_argument("--no-suffix-match", action="store_true", help="Disable hierarchical suffix reference matching")
    parser.add_argument("--list-refs", action="store_true", help="List footprint references in the PCB and exit")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format for --list-refs")
    parser.add_argument("--report-json", type=Path, help="Write machine-readable placement report JSON")
    parser.add_argument("--version", action="version", version=f"pcb-place {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.pcb is None:
        parser.error("pcb file is required")
    if not args.pcb.exists():
        raise SystemExit(f"PCB file not found: {args.pcb}")
    if args.list_refs:
        return list_refs(args.pcb, fmt=args.format)
    if args.ppl is None:
        parser.error("ppl file is required unless --list-refs is used")
    if not args.ppl.exists():
        raise SystemExit(f"PPL file not found: {args.ppl}")
    if args.output and args.in_place:
        parser.error("--output and --in-place are mutually exclusive")

    model = load_ppl(args.ppl)
    source_text = args.pcb.read_text(encoding="utf-8")
    run_validation = bool(args.validate or args.check)
    new_text, messages, report = apply_placements(source_text, model, strict=args.strict,
                                                  allow_suffix_match=not args.no_suffix_match,
                                                  validate=run_validation)
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
        return 0

    destination = args.pcb if args.in_place else (args.output or default_output_path(args.pcb))
    if destination == args.pcb and not args.no_backup:
        backup = args.pcb.with_suffix(args.pcb.suffix + ".bak")
        shutil.copy2(args.pcb, backup)
        print(f"backup: {backup}")
    destination.write_text(new_text, encoding="utf-8")
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
