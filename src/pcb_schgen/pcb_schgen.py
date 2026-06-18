#!/usr/bin/env python3
"""
pcb-schgen: deterministic KiCad schematic (.kicad_sch) generator.

pcb-schgen closes the gap where Zener/pcb can generate a netlist and a placed
KiCad board but cannot emit a human-readable schematic.  It reads:

* a Zener/pcb netlist (e.g. ``default.net``) -- the authoritative connectivity,
* a placed KiCad board (``*.kicad_pcb``) -- recovered placement for ordering,
* a ``board.pln`` design-intent file -- functional grouping/intent,
* optional ``planning-hints/`` and a ``pcb-place`` report,

and writes a valid ``.kicad_sch`` plus optional generation/symbol-mapping
reports.

Design goals (mirrors the pcb-place / pcb-plan family):

* Deterministic output -- stable ordering and stable (uuid5-derived) UUIDs so the
  schematic diffs cleanly in version control.
* Standalone -- it does not import, modify, or depend on pcb-plan / pcb-place and
  never alters the input PCB.
* Connectivity-correct first -- every netlist net is reproduced exactly in the
  generated schematic.  Exact symbol fidelity is best-effort: when a precise
  KiCad symbol is unknown a generic rectangular symbol is generated that
  preserves pin names/numbers and is flagged ``requires_review``.

Schematic philosophy: this is not a netlist dump.  Local circuits are drawn with
real wires; global rails (GND / power) and high-fanout nets use power symbols and
labels.  Components are grouped by ``board.pln`` functional intent.

Security note: the netlist, board, and board.pln are parsed as data, never
executed.  Treat generated artifacts like any other build output.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__version__ = "0.1.0"

Point = Tuple[float, float]

# Deterministic UUID namespace so re-runs produce byte-identical schematics.
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "pcb-schgen.kicad")

# Geometry constants (millimetres, KiCad schematic units).
GRID = 1.27
PIN_PITCH = 2.54
STUB = 2.54


class SchgenError(RuntimeError):
    """Raised for invalid input or an unrecoverable generation failure."""


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


def _fmt(value: float) -> str:
    """Format a coordinate the way KiCad does: trimmed, grid-snapped decimals."""

    v = round(float(value) + 0.0, 4)
    if v == 0:
        v = 0.0
    text = f"{v:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _key(point: Point) -> Tuple[int, int]:
    """Snap a point to an integer key for exact endpoint coincidence tests."""

    return (int(round(point[0] * 1000)), int(round(point[1] * 1000)))


def _det_uuid(seed: str, random: bool = False) -> str:
    if random:
        return str(uuid.uuid4())
    return str(uuid.uuid5(_NS, seed))


# ---------------------------------------------------------------------------
# S-expression parsing (netlist + board)
# ---------------------------------------------------------------------------


def _sexp_tokens(text: str) -> List[str]:
    return re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)


def _parse_sexp(tokens: List[str]) -> Any:
    """Parse a single s-expression into nested lists; strings keep their quotes."""

    if not tokens:
        raise SchgenError("Empty s-expression")
    token = tokens.pop(0)
    if token == "(":
        node: List[Any] = []
        while tokens and tokens[0] != ")":
            node.append(_parse_sexp(tokens))
        if not tokens:
            raise SchgenError("Unbalanced parentheses in s-expression")
        tokens.pop(0)  # discard ")"
        return node
    if token == ")":
        raise SchgenError("Unexpected ')' in s-expression")
    return token


def _unquote(token: Any) -> str:
    if isinstance(token, str) and len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return token if isinstance(token, str) else str(token)


def _children(node: Any, key: str) -> Iterable[List[Any]]:
    if not isinstance(node, list):
        return
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            yield child


def _first(node: Any, key: str) -> Optional[List[Any]]:
    for child in _children(node, key):
        return child
    return None


def _atom(node: Any, key: str) -> Optional[str]:
    child = _first(node, key)
    if child is not None and len(child) >= 2:
        return _unquote(child[1])
    return None


# ---------------------------------------------------------------------------
# Netlist model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class LibPin:
    number: str
    name: str


@dataclasses.dataclass
class Component:
    ref: str
    value: str
    footprint: str
    part: str
    sheetpath: str
    properties: Dict[str, str]
    pins: List[LibPin] = dataclasses.field(default_factory=list)

    @property
    def prefix(self) -> str:
        m = re.match(r"^([A-Za-z]+)", self.ref)
        return m.group(1).upper() if m else ""

    @property
    def number(self) -> int:
        m = re.search(r"(\d+)", self.ref)
        return int(m.group(1)) if m else 0

    @property
    def display_value(self) -> str:
        for key in ("value", "resistance", "capacitance", "inductance"):
            if self.properties.get(key):
                return self.properties[key]
        return self.value


@dataclasses.dataclass
class Net:
    code: int
    name: str
    nodes: List[Tuple[str, str]]  # (ref, pin)


@dataclasses.dataclass
class Netlist:
    components: Dict[str, Component]
    nets: List[Net]
    libparts: Dict[str, List[LibPin]]


def parse_netlist(path: Path) -> Netlist:
    text = path.read_text(encoding="utf-8", errors="replace")
    tree = _parse_sexp(_sexp_tokens(text))
    if not isinstance(tree, list) or not tree or tree[0] != "export":
        raise SchgenError(f"{path} is not a recognised Zener/pcb netlist (export ...)")

    # libparts: part name -> ordered pins
    libparts: Dict[str, List[LibPin]] = {}
    libparts_node = _first(tree, "libparts")
    if libparts_node is not None:
        for libpart in _children(libparts_node, "libpart"):
            part = _atom(libpart, "part") or "?"
            pins: List[LibPin] = []
            pins_node = _first(libpart, "pins")
            if pins_node is not None:
                for pin in _children(pins_node, "pin"):
                    pins.append(LibPin(_atom(pin, "num") or "", _atom(pin, "name") or ""))
            libparts[part] = pins

    # components
    components: Dict[str, Component] = {}
    comps_node = _first(tree, "components")
    if comps_node is not None:
        for comp in _children(comps_node, "comp"):
            ref = _atom(comp, "ref")
            if not ref:
                continue
            props: Dict[str, str] = {}
            for prop in _children(comp, "property"):
                name = _atom(prop, "name")
                val = _atom(prop, "value")
                if name is not None:
                    props[name] = val or ""
            libsource = _first(comp, "libsource")
            part = _atom(libsource, "part") if libsource is not None else None
            sheet = _first(comp, "sheetpath")
            sheetpath = _atom(sheet, "names") if sheet is not None else ""
            components[ref] = Component(
                ref=ref,
                value=_atom(comp, "value") or "",
                footprint=_atom(comp, "footprint") or "",
                part=part or "?",
                sheetpath=sheetpath or "",
                properties=props,
                pins=list(libparts.get(part or "", [])),
            )

    # nets
    nets: List[Net] = []
    nets_node = _first(tree, "nets")
    if nets_node is not None:
        for net in _children(nets_node, "net"):
            code = _atom(net, "code") or "0"
            name = _atom(net, "name") or ""
            nodes: List[Tuple[str, str]] = []
            for node in _children(net, "node"):
                nref = _atom(node, "ref")
                npin = _atom(node, "pin")
                if nref and npin is not None:
                    nodes.append((nref, npin))
            try:
                code_int = int(code)
            except ValueError:
                code_int = len(nets) + 1
            nets.append(Net(code=code_int, name=name, nodes=nodes))

    if not components:
        raise SchgenError(f"{path} contained no components")
    return Netlist(components=components, nets=nets, libparts=libparts)


# ---------------------------------------------------------------------------
# Placed board parsing (positions only -- never modified)
# ---------------------------------------------------------------------------


def parse_board_positions(path: Optional[Path]) -> Dict[str, Point]:
    """Recover footprint reference -> (x, y) from a placed .kicad_pcb."""

    if path is None:
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    positions: Dict[str, Point] = {}
    for match in re.finditer(r"\(footprint\b", text):
        start = match.start()
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        block = text[start:end]
        ref_m = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
        at_m = re.search(r"\(at\s+(-?[0-9.eE]+)\s+(-?[0-9.eE]+)", block)
        if ref_m and at_m:
            positions[ref_m.group(1)] = (float(at_m.group(1)), float(at_m.group(2)))
    return positions


# ---------------------------------------------------------------------------
# board.pln parsing (small dependency-free YAML subset)
# ---------------------------------------------------------------------------


def _pln_split_top(inner: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    quote: Optional[str] = None
    buf: List[str] = []
    for ch in inner:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _pln_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return {}
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        result: Dict[str, Any] = {}
        for part in _pln_split_top(inner):
            if ":" in part:
                k, v = part.split(":", 1)
                result[k.strip().strip("\"'")] = _pln_scalar(v.strip())
        return result
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_pln_scalar(p.strip()) for p in _pln_split_top(inner) if p.strip()]
    if value[0] in "\"'" and value[-1:] == value[0]:
        return value[1:-1]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.match(r"^-?\d+$", value):
        return int(value)
    if re.match(r"^-?\d*\.\d+$", value):
        return float(value)
    return value


def parse_pln(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Any]] = [(-1, root)]
    last_key: Dict[int, Tuple[Any, str]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if stripped == "-" or stripped.startswith("- "):
            item_text = stripped[1:].strip()
            if not isinstance(parent, list):
                container, key = last_key.get(indent - 2, last_key.get(indent, (None, None)))
                if isinstance(container, dict) and key:
                    new_list: List[Any] = []
                    container[key] = new_list
                    parent = new_list
                    stack.append((indent - 2, parent))
                else:
                    continue
            if not item_text:
                item: Dict[str, Any] = {}
                parent.append(item)
                stack.append((indent, item))
            elif ":" in item_text and not item_text.startswith(("{", "[")):
                k, v = item_text.split(":", 1)
                item = {k.strip(): _pln_scalar(v)}
                parent.append(item)
                stack.append((indent, item))
            else:
                parent.append(_pln_scalar(item_text))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        val = _pln_scalar(value)
        if isinstance(parent, dict):
            parent[key] = val
            last_key[indent] = (parent, key)
            if isinstance(val, (dict, list)):
                stack.append((indent, val))
    return root


# ---------------------------------------------------------------------------
# Symbol library
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SymPin:
    number: str
    name: str
    x: float
    y: float
    angle: int
    length: float = PIN_PITCH
    etype: str = "passive"


@dataclasses.dataclass
class SymbolDef:
    name: str  # lib_id without the "schgen:" prefix
    ref_prefix: str
    pins: List[SymPin]
    graphics: List[str]
    power: bool = False
    pin_names_hide: bool = True
    pin_numbers_hide: bool = True

    @property
    def lib_id(self) -> str:
        return f"schgen:{self.name}"


def _rect(x0: float, y0: float, x1: float, y1: float) -> str:
    return (
        f"(rectangle (start {_fmt(x0)} {_fmt(y0)}) (end {_fmt(x1)} {_fmt(y1)}) "
        f"(stroke (width 0.254) (type default)) (fill (type none)))"
    )


def _two_pin(name: str, prefix: str, graphics: List[str]) -> SymbolDef:
    pins = [
        SymPin("1", "~", 0, 3.81, 270, 1.27),
        SymPin("2", "~", 0, -3.81, 90, 1.27),
    ]
    return SymbolDef(name=name, ref_prefix=prefix, pins=pins, graphics=graphics)


def _builtin_symbols() -> Dict[str, SymbolDef]:
    syms: Dict[str, SymbolDef] = {}
    syms["R"] = _two_pin("R", "R", [_rect(-1.016, -2.54, 1.016, 2.54)])
    syms["C"] = _two_pin(
        "C",
        "C",
        [
            "(polyline (pts (xy -1.778 0.508) (xy 1.778 0.508)) (stroke (width 0.508) (type default)) (fill (type none)))",
            "(polyline (pts (xy -1.778 -0.508) (xy 1.778 -0.508)) (stroke (width 0.508) (type default)) (fill (type none)))",
        ],
    )
    syms["L"] = _two_pin("L", "L", [_rect(-0.762, -2.54, 0.762, 2.54)])
    syms["FB"] = _two_pin("FB", "FB", [_rect(-1.016, -2.54, 1.016, 2.54)])
    syms["F"] = _two_pin("F", "F", [_rect(-1.016, -2.54, 1.016, 2.54)])
    syms["D"] = _two_pin(
        "D",
        "D",
        [
            "(polyline (pts (xy 1.27 1.27) (xy 1.27 -1.27)) (stroke (width 0.254) (type default)) (fill (type none)))",
            "(polyline (pts (xy -1.27 1.27) (xy 1.27 0) (xy -1.27 -1.27) (xy -1.27 1.27)) (stroke (width 0.254) (type default)) (fill (type none)))",
        ],
    )
    # diode pins: anode (1) top, cathode (2) bottom -- keep generic 1/2 mapping.
    syms["LED"] = _two_pin(
        "LED",
        "D",
        [
            "(polyline (pts (xy 1.27 1.27) (xy 1.27 -1.27)) (stroke (width 0.254) (type default)) (fill (type none)))",
            "(polyline (pts (xy -1.27 1.27) (xy 1.27 0) (xy -1.27 -1.27) (xy -1.27 1.27)) (stroke (width 0.254) (type default)) (fill (type none)))",
            "(polyline (pts (xy 2.0 1.9) (xy 3.2 0.7)) (stroke (width 0.2) (type default)) (fill (type none)))",
        ],
    )
    # Test point: single pin.
    syms["TestPoint"] = SymbolDef(
        name="TestPoint",
        ref_prefix="TP",
        pins=[SymPin("1", "1", 0, -2.54, 90, 0)],
        graphics=["(circle (center 0 0.762) (radius 0.762) (stroke (width 0.254) (type default)) (fill (type none)))"],
    )
    # Push switch: 2 pins (simplified SPST).
    syms["SW_Push"] = SymbolDef(
        name="SW_Push",
        ref_prefix="SW",
        pins=[SymPin("1", "1", -3.81, 0, 0, 2.0), SymPin("2", "2", 3.81, 0, 180, 2.0)],
        graphics=[
            "(circle (center -1.524 0) (radius 0.254) (stroke (width 0) (type default)) (fill (type none)))",
            "(circle (center 1.524 0) (radius 0.254) (stroke (width 0) (type default)) (fill (type none)))",
            "(polyline (pts (xy -1.27 0.508) (xy 1.27 1.778)) (stroke (width 0.254) (type default)) (fill (type none)))",
        ],
    )
    return syms


def _generic_symbol(name: str, ref_prefix: str, pins: Sequence[LibPin]) -> SymbolDef:
    """Build a deterministic rectangular symbol that preserves pin names/numbers."""

    ordered = _order_pins(pins)
    count = len(ordered)
    left_count = (count + 1) // 2
    left = ordered[:left_count]
    right = ordered[left_count:]
    rows = max(left_count, len(right), 1)
    half_h = (rows + 1) * PIN_PITCH / 2
    half_w = 7.62
    sym_pins: List[SymPin] = []
    for i, lp in enumerate(left):
        y = half_h - PIN_PITCH - i * PIN_PITCH
        sym_pins.append(SymPin(lp.number, lp.name or "~", -half_w - PIN_PITCH, y, 0, PIN_PITCH, "passive"))
    for i, lp in enumerate(right):
        y = half_h - PIN_PITCH - i * PIN_PITCH
        sym_pins.append(SymPin(lp.number, lp.name or "~", half_w + PIN_PITCH, y, 180, PIN_PITCH, "passive"))
    graphics = [_rect(-half_w, -half_h, half_w, half_h)]
    return SymbolDef(
        name=name,
        ref_prefix=ref_prefix,
        pins=sym_pins,
        graphics=graphics,
        pin_names_hide=False,
        pin_numbers_hide=False,
    )


def _order_pins(pins: Sequence[LibPin]) -> List[LibPin]:
    def sort_key(lp: LibPin) -> Tuple[int, int, str]:
        if lp.number.isdigit():
            return (0, int(lp.number), "")
        return (1, 0, lp.number)

    return sorted(pins, key=sort_key)


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

_GROUND_RE = re.compile(r"^(GND|AGND|DGND|GNDA|GNDD|PGND|EGND|VSS|VSSA)$", re.I)
_POWER_RE = re.compile(
    r"^(\+?\d+V\d*([A-Z0-9_]*)?|VBUS|VCC|VDD|VIN|VDDA|VDDIO|\+?\d+V|[+-]\d.*)$",
    re.I,
)

_DEFAULT_LABEL_NETS = {
    "GND", "AGND", "DGND", "GNDA", "+3V3", "+3V3_SYS", "+1V2_CORE", "+5V",
    "+5V_SYS", "VIN", "VBUS", "VCC", "VDD",
}


@dataclasses.dataclass
class SymbolMapping:
    ref: str
    lib_id: str
    symbol: SymbolDef
    fallback: bool
    requires_review: bool
    reason: str


def _role_for(ref: str, pln: Mapping[str, Any]) -> str:
    roles = pln.get("roles") if isinstance(pln.get("roles"), dict) else {}
    if ref in roles:
        return str(roles[ref])
    components = pln.get("components") if isinstance(pln.get("components"), dict) else {}
    spec = components.get(ref)
    if isinstance(spec, dict) and spec.get("role"):
        return str(spec["role"])
    return ""


def map_symbols(netlist: Netlist, pln: Mapping[str, Any]) -> Dict[str, SymbolMapping]:
    builtin = _builtin_symbols()
    mappings: Dict[str, SymbolMapping] = {}
    generic_cache: Dict[str, SymbolDef] = {}

    # Pins actually referenced per ref, so a chosen symbol always covers them.
    used_pins: Dict[str, set] = {}
    for net in netlist.nets:
        for ref, pin in net.nodes:
            used_pins.setdefault(ref, set()).add(pin)

    for ref, comp in netlist.components.items():
        prefix = comp.prefix
        role = _role_for(ref, pln).lower()
        value_l = comp.display_value.lower()
        part_l = comp.part.lower()
        chosen: Optional[SymbolDef] = None
        fallback = False
        requires_review = False
        reason = "exact"

        is_led = prefix == "D" and ("led" in role or "led" in value_l or "led" in part_l)
        if prefix == "R" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["R"]
        elif prefix == "C" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["C"]
        elif prefix == "L" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["L"]
        elif prefix == "FB" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["FB"]
        elif prefix == "F" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["F"]
        elif is_led and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["LED"]
        elif prefix == "D" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["D"]
        elif prefix == "TP" and _pins_ok(comp, used_pins.get(ref), {"1"}):
            chosen = builtin["TestPoint"]
        elif prefix == "SW" and _pins_ok(comp, used_pins.get(ref), {"1", "2"}):
            chosen = builtin["SW_Push"]

        if chosen is None:
            # Generic rectangular symbol built from the libpart / referenced pins.
            pins = _symbol_pins_for(comp, used_pins.get(ref))
            if not pins:
                # Mechanical / no-pin part: a single anchor pin keeps it placeable.
                pins = [LibPin("1", "1")]
            sig = ref + "|" + ",".join(f"{p.number}:{p.name}" for p in pins)
            cache_key = comp.part + "|" + sig if comp.part not in ("?", "") else sig
            sym = generic_cache.get(cache_key)
            if sym is None:
                gname = _safe_symbol_name(comp.part if comp.part not in ("?", "") else f"GEN_{ref}")
                if gname in {s.name for s in generic_cache.values()}:
                    gname = f"{gname}_{ref}"
                sym = _generic_symbol(gname, prefix or "U", pins)
                generic_cache[cache_key] = sym
            chosen = sym
            fallback = True
            requires_review = True
            reason = f"generic symbol for {prefix or 'part'} {comp.part!r} (exact KiCad symbol unknown)"

        mappings[ref] = SymbolMapping(
            ref=ref,
            lib_id=chosen.lib_id,
            symbol=chosen,
            fallback=fallback,
            requires_review=requires_review,
            reason=reason,
        )
    return mappings


def _pins_ok(comp: Component, used: Optional[set], provided: set) -> bool:
    """True when a builtin symbol's pin set covers every referenced pin."""

    if not used:
        return True
    return used.issubset(provided)


def _symbol_pins_for(comp: Component, used: Optional[set]) -> List[LibPin]:
    pins = list(comp.pins)
    have = {p.number for p in pins}
    for pin in sorted(used or []):
        if pin not in have:
            pins.append(LibPin(pin, pin))
            have.add(pin)
    return pins


def _safe_symbol_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.+-]", "_", raw)
    return name or "GEN"


# ---------------------------------------------------------------------------
# Functional grouping (board.pln intent)
# ---------------------------------------------------------------------------

# Canonical block ordering left-to-right / top-to-bottom on the sheet.
_BLOCK_ORDER = [
    "HDMI_IN", "RETIMER", "HDMI_OUT", "MCU", "WIFI", "POWER", "TELEMETRY",
    "FLASH", "USB", "DEBUG", "USER_IO", "MISC",
]

_ROLE_BLOCK = {
    "hdmi_connector": "HDMI_IN",
    "hdmi_retimer": "RETIMER",
    "hdmi_redriver": "RETIMER",
    "mcu": "MCU",
    "rf_module": "WIFI",
    "regulator": "POWER",
    "usb_connector": "USB",
    "debug_header": "DEBUG",
    "esd_protection": "MISC",  # refined to its functional path below
}

_SHEET_BLOCK = {
    "PWR": "POWER",
    "TMDS181": "RETIMER",
    "MCU": "MCU",
    "HDMI_IN": "HDMI_IN",
    "HDMI_OUT": "HDMI_OUT",
    "WIFI": "WIFI",
    "TELEMETRY": "TELEMETRY",
    "FLASH": "FLASH",
    "USER_IO": "USER_IO",
}


@dataclasses.dataclass
class Grouping:
    block_of: Dict[str, str]
    order_in_block: Dict[str, float]  # ref -> sort key within its block
    path_index: Dict[str, int]  # ref -> position in a functional path (for ESD checks)


def group_components(netlist: Netlist, pln: Mapping[str, Any]) -> Grouping:
    block_of: Dict[str, str] = {}
    path_index: Dict[str, int] = {}

    # 1) functional_paths give the strongest signal AND a left-to-right order.
    paths = pln.get("functional_paths") if isinstance(pln.get("functional_paths"), dict) else {}
    for pname, spec in paths.items():
        if not isinstance(spec, dict):
            continue
        seq = spec.get("sequence") or []
        block = _path_block(pname)
        for idx, ref in enumerate(seq):
            if not isinstance(ref, str) or ref not in netlist.components:
                continue
            path_index.setdefault(ref, idx)
            # The connector (first) anchors the path's block and the ESD parts in
            # the middle move next to it (so they sit between connector and IC).
            # The protected IC endpoint (last) keeps its own role block so it can
            # sit between the in/out connector blocks (e.g. a retimer).
            is_endpoint_ic = idx == len(seq) - 1 and len(seq) > 1
            role = _role_for(ref, pln).lower()
            if not (is_endpoint_ic and role and "connector" not in role and "esd" not in role):
                block_of.setdefault(ref, block)

    # 2) power islands.
    islands = pln.get("power_islands") if isinstance(pln.get("power_islands"), dict) else {}
    for spec in islands.values():
        if not isinstance(spec, dict):
            continue
        for value in spec.values():
            for ref in _as_refs(value):
                if ref in netlist.components:
                    block_of.setdefault(ref, "POWER")

    # 3) explicit roles.
    for ref, comp in netlist.components.items():
        if ref in block_of:
            continue
        role = _role_for(ref, pln).lower()
        if role in _ROLE_BLOCK and _ROLE_BLOCK[role] != "MISC":
            block_of[ref] = _ROLE_BLOCK[role]

    # 4) netlist sheetpath top segment.
    for ref, comp in netlist.components.items():
        if ref in block_of:
            continue
        top = comp.sheetpath.split(".")[0] if comp.sheetpath else ""
        if top in _SHEET_BLOCK:
            block_of[ref] = _SHEET_BLOCK[top]

    # 5) ref-prefix heuristic fallback.
    for ref, comp in netlist.components.items():
        if ref in block_of:
            continue
        block_of[ref] = _prefix_block(comp)

    order_in_block = _order_within_blocks(netlist, block_of, path_index, pln)
    return Grouping(block_of=block_of, order_in_block=order_in_block, path_index=path_index)


def _path_block(pname: str) -> str:
    name = pname.upper()
    if "IN" in name:
        return "HDMI_IN"
    if "OUT" in name:
        return "HDMI_OUT"
    if "USB" in name:
        return "USB"
    return "MISC"


def _as_refs(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if re.match(r"^[A-Za-z]+\d", value) else []
    if isinstance(value, list):
        refs: List[str] = []
        for item in value:
            refs.extend(_as_refs(item))
        return refs
    return []


def _prefix_block(comp: Component) -> str:
    prefix = comp.prefix
    if prefix in ("J",):
        return "USER_IO"
    if prefix in ("TP",):
        return "DEBUG"
    if prefix in ("SW", "LED"):
        return "USER_IO"
    if prefix in ("H", "MK"):
        return "MISC"
    if prefix == "U":
        return "MCU"
    return "MISC"


_ROLE_RANK = {"connector": 0, "esd": 1, "ic": 2, "passive": 3, "default": 3}


def _priority_key(ref: str, comp: Component, path_index: Mapping[str, int], pln: Mapping[str, Any]) -> Tuple[float, int, str]:
    if ref in path_index:
        return (float(path_index[ref]), comp.number, ref)
    role = _role_for(ref, pln).lower()
    prefix = comp.prefix
    if "connector" in role or prefix == "J":
        rank = 0
    elif "esd" in role:
        rank = 1
    elif prefix == "U" or "retimer" in role or "mcu" in role or "regulator" in role:
        rank = 2
    else:
        rank = 3
    return (10.0 + rank, comp.number, ref)


def _nets_of(netlist: Netlist) -> Dict[str, frozenset]:
    acc: Dict[str, set] = {}
    for net in netlist.nets:
        for ref, _ in net.nodes:
            acc.setdefault(ref, set()).add(net.name)
    return {ref: frozenset(names) for ref, names in acc.items()}


def _is_anchor(comp: Component, nets: frozenset) -> bool:
    """Anchors own a locality: ICs, connectors, regulators, multi-pin parts."""

    if comp.prefix in ("U", "J", "Y", "X"):
        return True
    return len(comp.pins) > 2 or len(nets) > 2


def _sheet_prefix_match(a: str, b: str) -> int:
    ta = a.split(".")
    tb = b.split(".")
    n = 0
    for x, y in zip(ta, tb):
        if x == y:
            n += 1
        else:
            break
    return n


def _parent_anchor(
    ref: str,
    comp: Component,
    anchors: Sequence[str],
    netlist: Netlist,
    nets_of: Mapping[str, frozenset],
) -> Optional[str]:
    """Pick the anchor a support part belongs to (shared sheet path, then nets)."""

    if not anchors:
        return None
    my_nets = nets_of.get(ref, frozenset())
    my_sheet = comp.sheetpath
    best = None
    best_key: Tuple[int, int, int] = (-1, -1, 1 << 30)
    for a in anchors:
        if a == ref:
            continue
        ac = netlist.components[a]
        shared = len(my_nets & nets_of.get(a, frozenset()))
        if shared == 0:
            continue
        key = (
            _sheet_prefix_match(my_sheet, ac.sheetpath),
            shared,
            -abs(comp.number - ac.number),
        )
        if key > best_key:
            best_key = key
            best = a
    return best


def _order_within_blocks(
    netlist: Netlist,
    block_of: Mapping[str, str],
    path_index: Mapping[str, int],
    pln: Mapping[str, Any],
) -> Dict[str, float]:
    """Order a block so components sharing a local net are placed adjacently.

    A greedy traversal seeded by functional priority keeps connectors first and
    pulls each component's local-net neighbours next to it, which is what makes
    short, collision-free local wiring (feedback dividers, LED chains, reset
    circuits) possible.
    """

    # Local-net adjacency within each block.
    adj: Dict[str, set] = {ref: set() for ref in netlist.components}
    for net in netlist.nets:
        refs = [r for r, _ in net.nodes if r in netlist.components]
        if net_is_ground(net.name) or net_is_power(net.name) or not (2 <= len(refs) <= 5):
            continue
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                if block_of.get(a) == block_of.get(b):
                    adj[a].add(b)
                    adj[b].add(a)

    # Locality: attach each support passive to its parent anchor (the IC/
    # connector it belongs to) so decoupling caps and support resistors cluster
    # next to their owner instead of forming an anonymous "line of caps".
    nets_of = _nets_of(netlist)
    anchors_by_block: Dict[str, List[str]] = {}
    for ref, comp in netlist.components.items():
        if _is_anchor(comp, nets_of.get(ref, frozenset())):
            anchors_by_block.setdefault(block_of[ref], []).append(ref)
    for ref, comp in netlist.components.items():
        if _is_anchor(comp, nets_of.get(ref, frozenset())):
            continue
        parent = _parent_anchor(ref, comp, anchors_by_block.get(block_of[ref], []), netlist, nets_of)
        if parent is not None:
            adj[ref].add(parent)
            adj[parent].add(ref)

    blocks: Dict[str, List[str]] = {}
    for ref in netlist.components:
        blocks.setdefault(block_of[ref], []).append(ref)

    order: Dict[str, float] = {}
    for block, refs in blocks.items():
        pkey = {r: _priority_key(r, netlist.components[r], path_index, pln) for r in refs}
        seeds = sorted(refs, key=lambda r: pkey[r])
        visited: set = set()
        seq: List[str] = []
        for seed in seeds:
            if seed in visited:
                continue
            stack = [seed]
            visited.add(seed)
            cluster: List[str] = []
            while stack:
                cur = stack.pop()
                cluster.append(cur)
                for nb in sorted(adj[cur], key=lambda r: pkey.get(r, (99.0, 0, r))):
                    if nb not in visited and block_of.get(nb) == block:
                        visited.add(nb)
                        stack.append(nb)
            cluster.sort(key=lambda r: pkey[r])
            seq.extend(cluster)
        for i, ref in enumerate(seq):
            order[ref] = float(i)
    return order


# ---------------------------------------------------------------------------
# Net policy
# ---------------------------------------------------------------------------


def net_is_ground(name: str) -> bool:
    return bool(_GROUND_RE.match(name.strip()))


def _is_nc_net(name: str) -> bool:
    """True when a net name denotes a deliberate no-connect / unnamed pin.

    Zener/pcb emits explicit no-connects as ``<sheet>.NC_<...>`` (and KiCad uses
    ``unconnected-...``).  Any other named single-node net (a test point, a spare
    connector signal) is a real net whose name must be preserved with a label.
    """

    n = name.strip()
    if not n:
        return True
    if n.lower().startswith("unconnected-"):
        return True
    last = n.split(".")[-1]
    return bool(re.search(r"(^|[._])NC(_|$)", n, re.I)) or last.upper() == "NC"


def net_is_power(name: str) -> bool:
    n = name.strip()
    if net_is_ground(n):
        return False
    if n in _DEFAULT_LABEL_NETS:
        return True
    return bool(_POWER_RE.match(n))


@dataclasses.dataclass
class NetPolicy:
    name: str
    kind: str  # "ground", "power", "label", "wire"


def classify_net(net: Net, block_of: Mapping[str, str], fanout_threshold: int) -> NetPolicy:
    name = net.name
    fanout = len(net.nodes)
    if net_is_ground(name):
        return NetPolicy(name, "ground")
    if net_is_power(name):
        return NetPolicy(name, "power")
    blocks = {block_of.get(ref, "MISC") for ref, _ in net.nodes}
    # global if high fanout, named like a reset/bus rail, or spanning >1 block.
    if name in _DEFAULT_LABEL_NETS:
        return NetPolicy(name, "label")
    if fanout >= fanout_threshold:
        return NetPolicy(name, "label")
    if re.match(r".*(RESET|RST|_N$|I2C|SPI|UART|SCL|SDA|CEC|HPD)", name, re.I) and len(blocks) > 1:
        return NetPolicy(name, "label")
    if len(blocks) > 1:
        return NetPolicy(name, "label")
    return NetPolicy(name, "wire")


# ---------------------------------------------------------------------------
# Schematic model + layout
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PlacedSymbol:
    ref: str
    mapping: SymbolMapping
    x: float
    y: float
    pin_points: Dict[str, Point]  # pin number -> abs connection point
    sheet: str = ""


@dataclasses.dataclass
class Wire:
    a: Point
    b: Point
    seed: str
    sheet: str = ""


@dataclasses.dataclass
class LabelObj:
    name: str
    at: Point
    justify: str
    glob: bool
    seed: str
    sheet: str = ""


@dataclasses.dataclass
class PowerObj:
    net: str
    at: Point
    kind: str  # "ground" or "power"
    direction: str  # "up" / "down"
    seed: str
    sheet: str = ""


@dataclasses.dataclass
class JunctionObj:
    at: Point
    seed: str
    sheet: str = ""


@dataclasses.dataclass
class NoConnectObj:
    at: Point
    seed: str
    sheet: str = ""


@dataclasses.dataclass
class SchModel:
    symbols: List[PlacedSymbol]
    wires: List[Wire]
    labels: List[LabelObj]
    powers: List[PowerObj]
    junctions: List[JunctionObj]
    power_symbol_names: Dict[str, str]  # net -> schgen power symbol name
    no_connects: List[NoConnectObj] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class LayoutConfig:
    block_cols: int = 3          # blocks packed per meta-row
    block_max_w: float = 190.0   # shelf wrap width inside a block (mm)
    block_gap_x: float = 25.4    # horizontal gap between blocks
    block_gap_y: float = 25.4    # vertical gap between meta-rows of blocks
    gutter: float = 7.62         # gap between components inside a block
    origin_x: float = 25.4
    origin_y: float = 25.4


def _pin_abs(px: float, py: float, pin: SymPin) -> Point:
    # rotation 0, no mirror: schematic = (px + lx, py - ly).
    return (round(px + pin.x, 4), round(py - pin.y, 4))


def _outward(center: Point, point: Point) -> Point:
    dx = point[0] - center[0]
    dy = point[1] - center[1]
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0)


def _symbol_half_extents(sym: SymbolDef) -> Point:
    """Half width/height of a symbol about its origin, padded for stubs/labels."""

    xs = [p.x for p in sym.pins] or [0.0]
    ys = [p.y for p in sym.pins] or [0.0]
    # Pin connection points already sit at the body edge; pad for the stub plus
    # label/power-symbol text room so neighbours never share a coordinate.
    hx = max(abs(min(xs)), abs(max(xs))) + STUB + 6.0
    hy = max(abs(min(ys)), abs(max(ys))) + STUB + 4.0
    return (max(hx, 7.62), max(hy, 7.62))


def _pack_block(
    refs: Sequence[str],
    mappings: Mapping[str, SymbolMapping],
    cfg: LayoutConfig,
) -> Tuple[float, float, List[Tuple[str, float, float]]]:
    """Shelf-pack a block's components; return (width, height, [(ref, rx, ry)]).

    Components on a shelf share a common centerline (rather than a common top
    edge) so their pin rows rarely coincide by accident -- which keeps local
    wiring short and collision-free.
    """

    shelves: List[List[Tuple[str, float, float]]] = []  # (ref, rx, half_height)
    cursor_x = 0.0
    shelf: List[Tuple[str, float, float]] = []
    block_w = 0.0
    for ref in refs:
        hx, hy = _symbol_half_extents(mappings[ref].symbol)
        w = 2 * hx
        if cursor_x > 0 and cursor_x + w > cfg.block_max_w:
            shelves.append(shelf)
            shelf = []
            cursor_x = 0.0
        shelf.append((ref, cursor_x + hx, hy))
        cursor_x += w + cfg.gutter
        block_w = max(block_w, cursor_x - cfg.gutter)
    if shelf:
        shelves.append(shelf)

    placed: List[Tuple[str, float, float]] = []
    shelf_y = 0.0
    for members in shelves:
        shelf_h = max((2 * hy for _, _, hy in members), default=0.0)
        center_y = shelf_y + shelf_h / 2
        for ref, rx, _hy in members:
            placed.append((ref, rx, center_y))
        shelf_y += shelf_h + cfg.gutter
    block_h = shelf_y - cfg.gutter if shelves else 0.0
    return block_w, block_h, placed


def layout(
    netlist: Netlist,
    grouping: Grouping,
    mappings: Mapping[str, SymbolMapping],
    positions: Mapping[str, Point],
    cfg: LayoutConfig,
    per_sheet: bool = False,
) -> List[PlacedSymbol]:
    """Place every component.

    per_sheet=False packs all functional blocks onto one sheet in a meta-grid.
    per_sheet=True lays each block out at the sheet origin independently (each
    block becomes its own hierarchical sub-sheet), which keeps each page small
    and inside the page outline.
    """

    # Bucket refs by block, ordered deterministically.
    buckets: Dict[str, List[str]] = {}
    for ref in netlist.components:
        buckets.setdefault(grouping.block_of[ref], []).append(ref)

    blocks_present = [b for b in _BLOCK_ORDER if b in buckets]
    blocks_present += sorted(b for b in buckets if b not in _BLOCK_ORDER)

    # First pass: pack each block independently and record its size.
    packed: Dict[str, Tuple[float, float, List[Tuple[str, float, float]]]] = {}
    for block in blocks_present:
        refs = buckets[block]
        refs.sort(key=lambda r: (grouping.order_in_block[r], netlist.components[r].number, r))
        packed[block] = _pack_block(refs, mappings, cfg)

    placed: List[PlacedSymbol] = []
    meta_x = cfg.origin_x
    meta_y = cfg.origin_y
    row_h = 0.0
    in_row = 0
    for block in blocks_present:
        block_w, block_h, comps = packed[block]
        if per_sheet:
            bx, by = cfg.origin_x, cfg.origin_y
        else:
            if in_row >= cfg.block_cols and in_row > 0:
                meta_x = cfg.origin_x
                meta_y += row_h + cfg.block_gap_y
                row_h = 0.0
                in_row = 0
            bx, by = meta_x, meta_y
        for ref, rx, ry in comps:
            px = _snap(bx + rx)
            py = _snap(by + ry)
            mapping = mappings[ref]
            pin_points: Dict[str, Point] = {}
            for pin in mapping.symbol.pins:
                pin_points[pin.number] = _pin_abs(px, py, pin)
            placed.append(PlacedSymbol(ref=ref, mapping=mapping, x=px, y=py, pin_points=pin_points, sheet=block))
        if not per_sheet:
            meta_x += block_w + cfg.block_gap_x
            row_h = max(row_h, block_h)
            in_row += 1
    return placed


def _snap(value: float) -> float:
    return round(round(value / GRID) * GRID, 4)


@dataclasses.dataclass
class _NetGeom:
    net: Net
    policy: NetPolicy
    endpoints: List[Tuple[str, str, Point, Point]]  # ref, pin, pin_pt, outward


class _Occupancy:
    """Records emitted points/segments so wiring never touches a foreign net.

    Keyed per sheet: objects on different hierarchical sub-sheets may share XY
    without colliding, so collision checks are scoped to one sheet.
    """

    def __init__(self) -> None:
        self.points: Dict[str, List[Tuple[Tuple[int, int], str]]] = {}
        self.segments: Dict[str, List[Tuple[Tuple[int, int], Tuple[int, int], str]]] = {}

    def add_point(self, pt: Point, owner: str, sheet: str = "") -> None:
        self.points.setdefault(sheet, []).append((_key(pt), owner))

    def add_segment(self, a: Point, b: Point, owner: str, sheet: str = "") -> None:
        self.segments.setdefault(sheet, []).append((_key(a), _key(b), owner))

    def conflicts(self, cand_points: Sequence[Point], cand_segments: Sequence[Tuple[Point, Point]],
                  owner: str, sheet: str = "") -> bool:
        cpts = [_key(p) for p in cand_points]
        csegs = [(_key(a), _key(b)) for a, b in cand_segments]
        points = self.points.get(sheet, ())
        segments = self.segments.get(sheet, ())
        # A candidate point landing on a foreign point or foreign segment connects.
        for cp in cpts:
            for op, oown in points:
                if oown != owner and op == cp:
                    return True
            for sa, sb, oown in segments:
                if oown != owner and _on_seg(cp, sa, sb):
                    return True
        # A foreign point landing on a candidate segment connects (T-junction).
        for ca, cb in csegs:
            for op, oown in points:
                if oown != owner and _on_seg(op, ca, cb):
                    return True
        return False


def _on_seg(p: Tuple[int, int], a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    if a[0] == b[0]:
        return p[0] == a[0] and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    if a[1] == b[1]:
        return p[1] == a[1] and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
    return False


def build_model(
    netlist: Netlist,
    grouping: Grouping,
    mappings: Mapping[str, SymbolMapping],
    placed: List[PlacedSymbol],
    fanout_threshold: int,
    random_uuids: bool,
) -> Tuple[SchModel, List[NetPolicy]]:
    by_ref = {p.ref: p for p in placed}
    block_of = grouping.block_of
    wires: List[Wire] = []
    labels: List[LabelObj] = []
    powers: List[PowerObj] = []
    junctions: List[JunctionObj] = []
    no_connects: List[NoConnectObj] = []
    power_symbol_names: Dict[str, str] = {}
    occ = _Occupancy()

    # Resolve every net's geometry up front so we can order emission.
    geoms: List[_NetGeom] = []
    for net in netlist.nets:
        policy = classify_net(net, grouping.block_of, fanout_threshold)
        endpoints: List[Tuple[str, str, Point, Point]] = []
        for ref, pin in net.nodes:
            sym = by_ref.get(ref)
            if sym is None or pin not in sym.pin_points:
                continue
            pin_pt = sym.pin_points[pin]
            out = _outward((sym.x, sym.y), pin_pt)
            endpoints.append((ref, pin, pin_pt, out))
        geoms.append(_NetGeom(net=net, policy=policy, endpoints=endpoints))

    policies = [g.policy for g in geoms]

    # Register every pin coordinate first so stubs never land on a foreign pin.
    for g in geoms:
        for ref, pin, pin_pt, out in g.endpoints:
            occ.add_point(pin_pt, g.net.name, block_of.get(ref, "MISC"))

    def alloc_stub(pin_pt: Point, out: Point, owner: str, sheet: str) -> Point:
        """Return a collision-free stub endpoint and register the stub wire."""

        for length, direction in _stub_candidates(out):
            cand = (_snap(pin_pt[0] + direction[0] * STUB * length),
                    _snap(pin_pt[1] + direction[1] * STUB * length))
            if cand == pin_pt:
                continue
            if not occ.conflicts([cand], [(pin_pt, cand)], owner, sheet):
                wires.append(Wire(pin_pt, cand, f"stub:{owner}:{_key(pin_pt)}", sheet))
                occ.add_point(cand, owner, sheet)
                occ.add_segment(pin_pt, cand, owner, sheet)
                return cand
        # Fallback: accept the natural stub even if imperfect (validation reports).
        cand = (_snap(pin_pt[0] + out[0] * STUB), _snap(pin_pt[1] + out[1] * STUB))
        wires.append(Wire(pin_pt, cand, f"stub:{owner}:{_key(pin_pt)}", sheet))
        occ.add_point(cand, owner, sheet)
        occ.add_segment(pin_pt, cand, owner, sheet)
        return cand

    def emit_global(g: _NetGeom) -> None:
        net, policy = g.net, g.policy
        for ref, pin, pin_pt, out in g.endpoints:
            sheet = block_of.get(ref, "MISC")
            stub_end = alloc_stub(pin_pt, out, net.name, sheet)
            sdir = _outward(pin_pt, stub_end)
            if policy.kind == "ground":
                powers.append(PowerObj(net.name, stub_end, "ground", _vdir(sdir), f"pwr:{net.name}:{ref}:{pin}", sheet))
                power_symbol_names[net.name] = _power_symbol_name(net.name)
            elif policy.kind == "power":
                powers.append(PowerObj(net.name, stub_end, "power", _vdir(sdir), f"pwr:{net.name}:{ref}:{pin}", sheet))
                power_symbol_names[net.name] = _power_symbol_name(net.name)
            else:
                justify = "left" if sdir[0] >= 0 else "right"
                labels.append(LabelObj(net.name, stub_end, justify, True, f"glabel:{net.name}:{ref}:{pin}", sheet))

    # Phase 1: global rails / labels (stable backbone), registered first.
    for g in geoms:
        if g.endpoints and g.policy.kind in ("ground", "power", "label"):
            emit_global(g)

    # Phase 2: local nets are drawn with real wires when a collision-free route
    # exists; otherwise the net is demoted to a (still-correct) label.
    for g in geoms:
        if not g.endpoints or g.policy.kind != "wire":
            continue
        net = g.net
        sheet = block_of.get(g.endpoints[0][0], "MISC")
        if len(g.endpoints) == 1:
            ref, pin, pin_pt, out = g.endpoints[0]
            if _is_nc_net(net.name):
                # Truly unconnected/spare pin: mark it no-connect (suppress ERC).
                no_connects.append(NoConnectObj(pin_pt, f"nc:{net.name}:{ref}:{pin}", sheet))
                g.policy.kind = "noconnect"
            else:
                # A named one-node net (test point, spare signal) must keep its
                # name so re-exporting from KiCad reproduces the source net.
                stub_end = alloc_stub(pin_pt, out, net.name, sheet)
                sdir = _outward(pin_pt, stub_end)
                justify = "left" if sdir[0] >= 0 else "right"
                labels.append(LabelObj(net.name, stub_end, justify, True, f"glabel:{net.name}:{ref}:{pin}", sheet))
                g.policy.kind = "label"
            continue
        stub_ends = [alloc_stub(pin_pt, out, net.name, sheet) for _, _, pin_pt, out in g.endpoints]
        chosen = _route_net(net, stub_ends, occ, sheet)
        if chosen is None:
            g.policy.kind = "label"  # demote -> drawn as labels for correctness
            for (ref, pin, pin_pt, out), stub_end in zip(g.endpoints, stub_ends):
                sdir = _outward(pin_pt, stub_end)
                justify = "left" if sdir[0] >= 0 else "right"
                labels.append(LabelObj(net.name, stub_end, justify, True, f"glabel:{net.name}:{ref}:{pin}", sheet))
            continue
        cw, cj, cp, cs = chosen
        for w in cw:
            w.sheet = sheet
        for j in cj:
            j.sheet = sheet
        wires.extend(cw)
        junctions.extend(cj)
        for pt in cp:
            occ.add_point(pt, net.name, sheet)
        for a, b in cs:
            occ.add_segment(a, b, net.name, sheet)

    return (
        SchModel(
            symbols=placed,
            wires=wires,
            labels=labels,
            powers=powers,
            junctions=junctions,
            power_symbol_names=power_symbol_names,
            no_connects=no_connects,
        ),
        policies,
    )


_RouteCand = Tuple[List["Wire"], List["JunctionObj"], List[Point], List[Tuple[Point, Point]]]


def _route_net(net: Net, stub_ends: List[Point], occ: "_Occupancy", sheet: str = "") -> Optional[_RouteCand]:
    """Return the first collision-free local route for a net, or None to demote."""

    owner = net.name
    if len(stub_ends) == 2:
        for vi, segs in enumerate(_two_pin_routes(stub_ends[0], stub_ends[1])):
            cw = [Wire(s[0], s[1], f"link:{owner}:{vi}:{i}") for i, s in enumerate(segs)]
            pts = list(stub_ends) + [p for s in segs for p in s]
            if not occ.conflicts(pts, segs, owner, sheet):
                return cw, [], pts, list(segs)
        return None
    # Multi-pin: prefer a daisy chain (handles dense dividers), then fall back to
    # a single trunk/bus line.
    daisy = _daisy_chain(net, stub_ends, occ, sheet)
    if daisy is not None:
        return daisy
    for cand in _trunk_bus_variants(net, stub_ends):
        cw, cj, cp, cs = cand
        if not occ.conflicts(cp, cs, owner, sheet):
            return cand
    return None


def _daisy_chain(net: Net, stub_ends: List[Point], occ: "_Occupancy", sheet: str = "") -> Optional[_RouteCand]:
    """Connect pins in spatial order, routing each hop with a clear dogleg."""

    owner = net.name
    order = sorted(range(len(stub_ends)), key=lambda i: (stub_ends[i][0], stub_ends[i][1]))
    chain = [stub_ends[i] for i in order]
    cw: List[Wire] = []
    cj: List[JunctionObj] = []
    pts: List[Point] = list(stub_ends)
    segs: List[Tuple[Point, Point]] = []
    for hop, (a, b) in enumerate(zip(chain, chain[1:])):
        routed = None
        for segvariant in _two_pin_routes(a, b):
            if occ.conflicts([p for s in segvariant for p in s], segvariant, owner, sheet):
                continue
            routed = segvariant
            break
        if routed is None:
            return None
        for i, s in enumerate(routed):
            cw.append(Wire(s[0], s[1], f"chain:{owner}:{hop}:{i}"))
            segs.append(s)
            pts.append(s[0])
            pts.append(s[1])
    # Interior chain nodes carry the pin stub plus two hop wires -> need junctions.
    for node in chain[1:-1]:
        cj.append(JunctionObj(node, f"cjunc:{owner}:{_key(node)}"))
    return cw, cj, pts, segs


def _trunk_bus_variants(net: Net, stub_ends: List[Point]) -> List[_RouteCand]:
    xs = sorted({se[0] for se in stub_ends})
    ys = sorted({se[1] for se in stub_ends})
    min_x, max_x = xs[0], xs[-1]
    min_y, max_y = ys[0], ys[-1]
    columns = [
        _snap(max_x + STUB), _snap(max_x + 2 * STUB), _snap(max_x + 3 * STUB),
        _snap(min_x - STUB), _snap(min_x - 2 * STUB), _snap(min_x - 3 * STUB),
    ]
    rows = [
        _snap(min_y - STUB), _snap(min_y - 2 * STUB), _snap(min_y - 3 * STUB),
        _snap(max_y + STUB), _snap(max_y + 2 * STUB), _snap(max_y + 3 * STUB),
    ]
    variants: List[_RouteCand] = []
    vi = 0
    for sx in columns:
        variants.append(_trunk_route(net, stub_ends, ys, sx, vi, vertical=True))
        vi += 1
    for sy in rows:
        variants.append(_trunk_route(net, stub_ends, xs, sy, vi, vertical=False))
        vi += 1
    return variants


def _two_pin_routes(a: Point, b: Point) -> List[List[Tuple[Point, Point]]]:
    routes: List[List[Tuple[Point, Point]]] = []
    routes.append(_manhattan(a, b))
    routes.append(_manhattan_alt(a, b))
    midx = _snap((a[0] + b[0]) / 2)
    if midx not in (a[0], b[0]):
        routes.append([(a, (midx, a[1])), ((midx, a[1]), (midx, b[1])), ((midx, b[1]), b)])
    midy = _snap((a[1] + b[1]) / 2)
    if midy not in (a[1], b[1]):
        routes.append([(a, (a[0], midy)), ((a[0], midy), (b[0], midy)), ((b[0], midy), b)])
    return [r for r in routes if r]


def _trunk_route(net: Net, stub_ends: List[Point], spine: List[float], coord: float, vi: int, vertical: bool) -> _RouteCand:
    """Route a multi-pin net via a single trunk line.

    vertical=True: trunk is a vertical line at x=coord with horizontal taps.
    vertical=False: trunk is a horizontal line at y=coord with vertical taps.
    """

    cw: List[Wire] = []
    cj: List[JunctionObj] = []
    pts: List[Point] = list(stub_ends)
    segs: List[Tuple[Point, Point]] = []

    def trunk_pt(v: float) -> Point:
        return (coord, v) if vertical else (v, coord)

    for i, stub_end in enumerate(stub_ends):
        along = stub_end[1] if vertical else stub_end[0]
        target = trunk_pt(along)
        if target != stub_end:
            cw.append(Wire(stub_end, target, f"tap:{net.name}:{vi}:{i}"))
            segs.append((stub_end, target))
            pts.append(target)
    for i in range(len(spine) - 1):
        a, b = trunk_pt(spine[i]), trunk_pt(spine[i + 1])
        cw.append(Wire(a, b, f"trunk:{net.name}:{vi}:{i}"))
        segs.append((a, b))
        pts.append(a)
        pts.append(b)
    for v in spine[1:-1]:
        cj.append(JunctionObj(trunk_pt(v), f"junc:{net.name}:{vi}:{v}"))
    return cw, cj, pts, segs


def _stub_candidates(out: Point) -> List[Tuple[int, Point]]:
    """Ordered (length, direction) stub attempts: natural first, then alternates."""

    if abs(out[0]) >= abs(out[1]):
        primary = (1.0 if out[0] >= 0 else -1.0, 0.0)
        alts = [(0.0, -1.0), (0.0, 1.0), (-primary[0], 0.0)]
    else:
        primary = (0.0, 1.0 if out[1] >= 0 else -1.0)
        alts = [(1.0, 0.0), (-1.0, 0.0), (0.0, -primary[1])]
    order: List[Tuple[int, Point]] = [(1, primary)]
    for d in alts:
        order.append((1, d))
    for length in (2, 3):
        order.append((length, primary))
        for d in alts:
            order.append((length, d))
    return order


def _vdir(out: Point) -> str:
    return "down" if out[1] > 0 else "up"


def _manhattan(a: Point, b: Point) -> List[Tuple[Point, Point]]:
    """Two-segment orthogonal route a -> (b.x, a.y) -> b (shared endpoints)."""

    if a == b:
        return []
    if a[0] == b[0] or a[1] == b[1]:
        return [(a, b)]
    corner = (b[0], a[1])
    return [(a, corner), (corner, b)]


def _manhattan_alt(a: Point, b: Point) -> List[Tuple[Point, Point]]:
    """Mirror two-segment route a -> (a.x, b.y) -> b."""

    if a == b or a[0] == b[0] or a[1] == b[1]:
        return []
    corner = (a[0], b[1])
    return [(a, corner), (corner, b)]


def _power_symbol_name(net: str) -> str:
    return _safe_symbol_name("PWR_" + net)


# ---------------------------------------------------------------------------
# Connectivity extraction + self-validation
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def extract_connectivity(model: SchModel) -> Dict[Tuple[str, str], Any]:
    """Reconstruct net groups the way KiCad would.

    Coordinate-based connections (wires, junctions, pins) only join objects on
    the SAME sheet -- two sub-sheets may reuse the same XY without touching.
    Global labels and power symbols connect by name across the whole hierarchy.
    """

    uf = _UnionFind()

    def cnode(sheet: str, pt: Point) -> Tuple[str, Tuple[int, int]]:
        return (sheet, _key(pt))

    # Wire endpoints share a coordinate on the same sheet -> same node.
    for wire in model.wires:
        uf.union(cnode(wire.sheet, wire.a), cnode(wire.sheet, wire.b))
    # Pins anchor to their (sheet, coordinate) node.
    pin_node: Dict[Tuple[str, str], Any] = {}
    for sym in model.symbols:
        for pin, pt in sym.pin_points.items():
            uf.union((sym.ref, pin), cnode(sym.sheet, pt))
            pin_node[(sym.ref, pin)] = (sym.ref, pin)
    # Labels and power symbols bind a coordinate to a hierarchy-global named node.
    for label in model.labels:
        uf.union(cnode(label.sheet, label.at), ("LABEL", label.name))
    for power in model.powers:
        uf.union(cnode(power.sheet, power.at), ("NET", power.net))
    groups: Dict[Tuple[str, str], Any] = {}
    for key in pin_node:
        groups[key] = uf.find(key)
    return groups


def validate(model: SchModel, netlist: Netlist) -> Dict[str, Any]:
    groups = extract_connectivity(model)
    # netlist pin -> netname for pins that were actually placed.
    placed_refs = {s.ref for s in model.symbols}
    expected: Dict[Tuple[str, str], str] = {}
    for net in netlist.nets:
        for ref, pin in net.nodes:
            expected[(ref, pin)] = net.name

    mismatches: List[str] = []
    # Two placed pins must share a group iff they share a netlist net.
    by_group: Dict[Any, List[Tuple[str, str]]] = {}
    for key, root in groups.items():
        by_group.setdefault(root, []).append(key)
    # Build expected groups (only placed pins).
    exp_groups: Dict[str, List[Tuple[str, str]]] = {}
    for (ref, pin), name in expected.items():
        if ref in placed_refs:
            exp_groups.setdefault(name, []).append((ref, pin))

    pin_to_actual = groups
    for name, members in exp_groups.items():
        roots = {pin_to_actual.get(m) for m in members if m in pin_to_actual}
        present = [m for m in members if m in pin_to_actual]
        if len(present) != len(members):
            missing = [m for m in members if m not in pin_to_actual]
            mismatches.append(f"net {name}: pins not emitted {missing}")
        if len(roots) > 1:
            mismatches.append(f"net {name}: split across {len(roots)} groups")
    # No group should mix two different expected nets.
    for root, members in by_group.items():
        names = {expected.get(m) for m in members if m in expected}
        names.discard(None)
        if len(names) > 1:
            mismatches.append(f"group merges nets {sorted(n for n in names if n)}")
    return {"ok": not mismatches, "mismatches": mismatches}


# ---------------------------------------------------------------------------
# .kicad_sch emission
# ---------------------------------------------------------------------------

_FONT = "(effects (font (size 1.27 1.27)))"


def _emit_lib_symbol(sym: SymbolDef) -> List[str]:
    out: List[str] = []
    pn_hide = " hide" if sym.pin_numbers_hide else ""
    nm_hide = " hide" if sym.pin_names_hide else ""
    out.append(
        f'\t\t(symbol "{sym.lib_id}" '
        f"(pin_numbers{' hide' if sym.pin_numbers_hide else ''}) "
        f"(pin_names (offset 0.254){nm_hide}) "
        f"(exclude_from_sim no) (in_bom yes) (on_board yes)"
        + (" (power)" if sym.power else "")
    )
    out.append(f'\t\t\t(property "Reference" "{sym.ref_prefix}" (at 0 5.08 0) {_FONT})')
    out.append(f'\t\t\t(property "Value" "{sym.name}" (at 0 -5.08 0) {_FONT})')
    out.append(f'\t\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))')
    out.append(f'\t\t\t(property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))')
    out.append(f'\t\t\t(symbol "{sym.name}_0_1"')
    for g in sym.graphics:
        out.append(f"\t\t\t\t{g}")
    out.append("\t\t\t)")
    out.append(f'\t\t\t(symbol "{sym.name}_1_1"')
    for pin in sym.pins:
        out.append(
            f"\t\t\t\t(pin {pin.etype} line (at {_fmt(pin.x)} {_fmt(pin.y)} {pin.angle}) "
            f"(length {_fmt(pin.length)}) "
            f'(name "{pin.name}" {_FONT}) (number "{pin.number}" {_FONT}))'
        )
    out.append("\t\t\t)")
    out.append("\t\t)")
    return out


def _power_symbol_def(name: str, net: str, ground: bool) -> SymbolDef:
    if ground:
        graphics = [
            "(polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0) (type default)) (fill (type none)))",
            "(polyline (pts (xy -1.27 -1.27) (xy 1.27 -1.27)) (stroke (width 0) (type default)) (fill (type none)))",
            "(polyline (pts (xy -0.762 -1.778) (xy 0.762 -1.778)) (stroke (width 0) (type default)) (fill (type none)))",
            "(polyline (pts (xy -0.254 -2.286) (xy 0.254 -2.286)) (stroke (width 0) (type default)) (fill (type none)))",
        ]
        pin = SymPin("1", net, 0, 0, 90, 0, "power_in")
    else:
        graphics = [
            "(polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))",
            "(polyline (pts (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27)) (stroke (width 0) (type default)) (fill (type none)))",
        ]
        pin = SymPin("1", net, 0, 0, 270, 0, "power_in")
    return SymbolDef(
        name=name,
        ref_prefix="#PWR",
        pins=[pin],
        graphics=graphics,
        power=True,
    )


_PAPER_DIMS = {
    "A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0),
    "A1": (841.0, 594.0), "A0": (1189.0, 841.0),
}
_PAPER_ORDER = ["A4", "A3", "A2", "A1", "A0"]


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "sheet"


def _content_extent(model: SchModel, sheet: Optional[str]) -> Tuple[float, float]:
    """Right/bottom extent of a sheet's content, for paper sizing."""

    max_x = max_y = 0.0
    for sym in model.symbols:
        if sheet is None or sym.sheet == sheet:
            hx, hy = _symbol_half_extents(sym.mapping.symbol)
            max_x = max(max_x, sym.x + hx)
            max_y = max(max_y, sym.y + hy)
    for coll in (model.labels, model.powers, model.no_connects):
        for obj in coll:
            if sheet is None or obj.sheet == sheet:
                max_x = max(max_x, obj.at[0] + 12.7)
                max_y = max(max_y, obj.at[1] + 6.35)
    for w in model.wires:
        if sheet is None or w.sheet == sheet:
            max_x = max(max_x, w.a[0], w.b[0])
            max_y = max(max_y, w.a[1], w.b[1])
    return max_x, max_y


def _choose_paper(max_x: float, max_y: float, preferred: str) -> str:
    need_x, need_y = max_x + 15.0, max_y + 15.0
    start = _PAPER_ORDER.index(preferred) if preferred in _PAPER_ORDER else 0
    for name in _PAPER_ORDER[start:]:
        w, h = _PAPER_DIMS[name]
        if need_x <= w and need_y <= h:
            return name
    return "A0"


def render(
    model: SchModel,
    netlist: Netlist,
    mappings: Mapping[str, SymbolMapping],
    project: str,
    title: str,
    paper: str,
    random_uuids: bool,
    multi_sheet: bool,
    root_name: str,
) -> Dict[str, str]:
    """Render the schematic. Returns {filename: text}; root_name is the -o file."""

    root_uuid = _det_uuid("root", random_uuids)
    sheets = _ordered_sheets(model)
    pwr_counter = [0]

    if not multi_sheet or len(sheets) <= 1:
        # Flat single sheet: everything on the root file.
        mx, my = _content_extent(model, None)
        chosen = _choose_paper(mx, my, paper)
        body = _emit_sheet_body(model, None, netlist, mappings, project,
                                f"/{root_uuid}", pwr_counter, random_uuids)
        text = _wrap_sheet(root_uuid, title, chosen, _all_lib_symbols(model, netlist, mappings, None),
                           body, sheet_instances=[("/", "1")])
        return {root_name: text}

    # Hierarchical: root sheet of sub-sheet symbols + one file per block.
    root_stem = Path(root_name).stem
    sheet_uuid = {s: _det_uuid(f"sheet:{s}", random_uuids) for s in sheets}
    file_uuid = {s: _det_uuid(f"sheetfile:{s}", random_uuids) for s in sheets}
    file_name = {s: f"{root_stem}-{_slug(s)}.kicad_sch" for s in sheets}

    files: Dict[str, str] = {}
    # Sub-sheet files (pages 2..N).
    sheet_instances = [("/", "1")]
    for i, s in enumerate(sheets):
        page = i + 2
        inst_path = f"/{root_uuid}/{sheet_uuid[s]}"
        mx, my = _content_extent(model, s)
        chosen = _choose_paper(mx, my, paper)
        body = _emit_sheet_body(model, s, netlist, mappings, project, inst_path, pwr_counter, random_uuids)
        files[file_name[s]] = _wrap_sheet(file_uuid[s], f"{title} — {s}", chosen,
                                          _all_lib_symbols(model, netlist, mappings, s), body,
                                          sheet_instances=None)
        sheet_instances.append((f"/{sheet_uuid[s]}", str(page)))

    # Root file (page 1): sheet symbols only.
    files[root_name] = _emit_root(root_uuid, project, title, paper, sheets,
                                  sheet_uuid, file_name, sheet_instances, random_uuids)
    return files


def _ordered_sheets(model: SchModel) -> List[str]:
    present = {s.sheet for s in model.symbols}
    ordered = [b for b in _BLOCK_ORDER if b in present]
    ordered += sorted(b for b in present if b not in _BLOCK_ORDER)
    return ordered


def _all_lib_symbols(model: SchModel, netlist: Netlist, mappings: Mapping[str, SymbolMapping],
                     sheet: Optional[str]) -> List[str]:
    out: List[str] = ["\t(lib_symbols"]
    seen: Dict[str, SymbolDef] = {}
    for sym in model.symbols:
        if sheet is None or sym.sheet == sheet:
            d = sym.mapping.symbol
            seen.setdefault(d.lib_id, d)
    for lib_id in sorted(seen):
        out.extend(_emit_lib_symbol(seen[lib_id]))
    power_defs: Dict[str, SymbolDef] = {}
    used_power_nets = {p.net for p in model.powers if sheet is None or p.sheet == sheet}
    for net in used_power_nets:
        pname = model.power_symbol_names.get(net)
        if pname:
            power_defs[pname] = _power_symbol_def(pname, net, net_is_ground(net))
    for pname in sorted(power_defs):
        out.extend(_emit_lib_symbol(power_defs[pname]))
    out.append("\t)")
    return out


def _emit_sheet_body(model: SchModel, sheet: Optional[str], netlist: Netlist,
                     mappings: Mapping[str, SymbolMapping], project: str, inst_path: str,
                     pwr_counter: List[int], random_uuids: bool) -> List[str]:
    out: List[str] = []
    syms = [s for s in model.symbols if sheet is None or s.sheet == sheet]
    for sym in sorted(syms, key=lambda s: (netlist.components[s.ref].prefix, netlist.components[s.ref].number, s.ref)):
        out.extend(_emit_instance(sym, netlist.components[sym.ref], project, inst_path, random_uuids))
    powers = [p for p in model.powers if sheet is None or p.sheet == sheet]
    for power in sorted(powers, key=lambda p: (p.net, _key(p.at))):
        out.extend(_emit_power(power, model.power_symbol_names[power.net], project, inst_path, pwr_counter, random_uuids))
    for wire in sorted((w for w in model.wires if sheet is None or w.sheet == sheet), key=lambda w: (_key(w.a), _key(w.b))):
        if _key(wire.a) == _key(wire.b):
            continue
        out.append(
            f"\t(wire (pts (xy {_fmt(wire.a[0])} {_fmt(wire.a[1])}) (xy {_fmt(wire.b[0])} {_fmt(wire.b[1])})) "
            f'(stroke (width 0) (type default)) (uuid "{_det_uuid("wire:" + wire.seed, random_uuids)}"))'
        )
    for junc in sorted((j for j in model.junctions if sheet is None or j.sheet == sheet), key=lambda j: _key(j.at)):
        out.append(
            f"\t(junction (at {_fmt(junc.at[0])} {_fmt(junc.at[1])}) (diameter 0) (color 0 0 0 0) "
            f'(uuid "{_det_uuid("junc:" + junc.seed, random_uuids)}"))'
        )
    for nc in sorted((n for n in model.no_connects if sheet is None or n.sheet == sheet), key=lambda n: _key(n.at)):
        out.append(
            f"\t(no_connect (at {_fmt(nc.at[0])} {_fmt(nc.at[1])}) "
            f'(uuid "{_det_uuid("nc:" + nc.seed, random_uuids)}"))'
        )
    for label in sorted((l for l in model.labels if sheet is None or l.sheet == sheet), key=lambda l: (l.name, _key(l.at))):
        angle = 0 if label.justify == "left" else 180
        out.append(
            f'\t(global_label "{_esc(label.name)}" (shape bidirectional) (at {_fmt(label.at[0])} {_fmt(label.at[1])} {angle}) '
            f"(fields_autoplaced) (effects (font (size 1.27 1.27)) (justify {label.justify})) "
            f'(uuid "{_det_uuid("glabel:" + label.seed, random_uuids)}"))'
        )
    return out


def _wrap_sheet(file_uuid: str, title: str, paper: str, lib_symbols: List[str],
                body: List[str], sheet_instances: Optional[List[Tuple[str, str]]]) -> str:
    out: List[str] = ["(kicad_sch", "\t(version 20231120)", '\t(generator "pcb-schgen")',
                      f'\t(generator_version "{__version__}")', f'\t(uuid "{file_uuid}")',
                      f'\t(paper "{paper}")', "\t(title_block", f'\t\t(title "{_esc(title)}")',
                      '\t\t(comment 1 "Generated by pcb-schgen from netlist + board.pln + placed PCB")', "\t)"]
    out.extend(lib_symbols)
    out.extend(body)
    if sheet_instances is not None:
        out.append("\t(sheet_instances")
        for path, page in sheet_instances:
            out.append(f'\t\t(path "{path}" (page "{page}"))')
        out.append("\t)")
    out.append(")")
    return "\n".join(out) + "\n"


def _emit_root(root_uuid: str, project: str, title: str, paper: str, sheets: List[str],
               sheet_uuid: Mapping[str, str], file_name: Mapping[str, str],
               sheet_instances: List[Tuple[str, str]], random_uuids: bool) -> str:
    out: List[str] = ["(kicad_sch", "\t(version 20231120)", '\t(generator "pcb-schgen")',
                      f'\t(generator_version "{__version__}")', f'\t(uuid "{root_uuid}")',
                      f'\t(paper "{paper}")', "\t(title_block", f'\t\t(title "{_esc(title)}")',
                      '\t\t(comment 1 "Generated by pcb-schgen — hierarchical root")', "\t)",
                      "\t(lib_symbols)"]
    # Lay sheet symbols out in a tidy grid.
    per_row = 3
    sw, sh = 60.0, 30.0
    gx, gy = 25.4, 22.86
    x0, y0 = 25.4, 31.75
    for i, s in enumerate(sheets):
        col, row = i % per_row, i // per_row
        x = x0 + col * (sw + gx)
        y = y0 + row * (sh + gy)
        out.append(f"\t(sheet (at {_fmt(x)} {_fmt(y)}) (size {_fmt(sw)} {_fmt(sh)}) (fields_autoplaced)")
        out.append("\t\t(stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0))")
        out.append(f'\t\t(uuid "{sheet_uuid[s]}")')
        out.append(f'\t\t(property "Sheetname" "{_esc(s)}" (at {_fmt(x)} {_fmt(y - 1.0)} 0) (effects (font (size 1.6 1.6)) (justify left bottom)))')
        out.append(f'\t\t(property "Sheetfile" "{_esc(file_name[s])}" (at {_fmt(x)} {_fmt(y + sh + 1.0)} 0) (effects (font (size 1.2 1.2)) (justify left top)))')
        out.append(f'\t\t(instances (project "{_esc(project)}" (path "/{root_uuid}" (page "{i + 2}")))))')
    out.append("\t(sheet_instances")
    for path, page in sheet_instances:
        out.append(f'\t\t(path "{path}" (page "{page}"))')
    out.append("\t)")
    out.append(")")
    return "\n".join(out) + "\n"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _emit_instance(sym: PlacedSymbol, comp: Component, project: str, inst_path: str, random_uuids: bool) -> List[str]:
    suid = _det_uuid(f"sym:{sym.ref}", random_uuids)
    lib_id = sym.mapping.lib_id
    out: List[str] = []
    out.append(
        f'\t(symbol (lib_id "{lib_id}") (at {_fmt(sym.x)} {_fmt(sym.y)} 0) (unit 1) '
        f"(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (fields_autoplaced)"
    )
    out.append(f'\t\t(uuid "{suid}")')
    ry = sym.y - 6.35
    vy = sym.y + 6.35
    out.append(f'\t\t(property "Reference" "{_esc(sym.ref)}" (at {_fmt(sym.x)} {_fmt(ry)} 0) {_FONT})')
    out.append(f'\t\t(property "Value" "{_esc(comp.display_value)}" (at {_fmt(sym.x)} {_fmt(vy)} 0) {_FONT})')
    out.append(
        f'\t\t(property "Footprint" "{_esc(comp.footprint)}" (at {_fmt(sym.x)} {_fmt(sym.y)} 0) '
        f"(effects (font (size 1.27 1.27)) hide))"
    )
    out.append(
        f'\t\t(property "Datasheet" "{_esc(comp.properties.get("datasheet", "~") or "~")}" '
        f"(at {_fmt(sym.x)} {_fmt(sym.y)} 0) (effects (font (size 1.27 1.27)) hide))"
    )
    if sym.mapping.requires_review:
        out.append(
            f'\t\t(property "schgen_review" "generic symbol; verify pinout" '
            f"(at {_fmt(sym.x)} {_fmt(sym.y)} 0) (effects (font (size 1.27 1.27)) hide))"
        )
    for pin in sym.mapping.symbol.pins:
        out.append(f'\t\t(pin "{pin.number}" (uuid "{_det_uuid(f"pin:{sym.ref}:{pin.number}", random_uuids)}"))')
    out.append(
        f'\t\t(instances (project "{_esc(project)}" (path "{inst_path}" (reference "{_esc(sym.ref)}") (unit 1))))'
    )
    out.append("\t)")
    return out


def _emit_power(power: PowerObj, pname: str, project: str, inst_path: str, counter: List[int], random_uuids: bool) -> List[str]:
    counter[0] += 1
    ref = f"#PWR{counter[0]:04d}"
    suid = _det_uuid("power:" + power.seed, random_uuids)
    out: List[str] = []
    out.append(
        f'\t(symbol (lib_id "schgen:{pname}") (at {_fmt(power.at[0])} {_fmt(power.at[1])} 0) (unit 1) '
        f"(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (fields_autoplaced)"
    )
    out.append(f'\t\t(uuid "{suid}")')
    out.append(f'\t\t(property "Reference" "{ref}" (at {_fmt(power.at[0])} {_fmt(power.at[1] - 3.81)} 0) (effects (font (size 1.27 1.27)) hide))')
    out.append(f'\t\t(property "Value" "{_esc(power.net)}" (at {_fmt(power.at[0])} {_fmt(power.at[1] + 3.81)} 0) {_FONT})')
    out.append(f'\t\t(property "Footprint" "" (at {_fmt(power.at[0])} {_fmt(power.at[1])} 0) (effects (font (size 1.27 1.27)) hide))')
    out.append(f'\t\t(property "Datasheet" "" (at {_fmt(power.at[0])} {_fmt(power.at[1])} 0) (effects (font (size 1.27 1.27)) hide))')
    out.append(f'\t\t(pin "1" (uuid "{_det_uuid("powerpin:" + power.seed, random_uuids)}"))')
    out.append(f'\t\t(instances (project "{_esc(project)}" (path "{inst_path}" (reference "{ref}") (unit 1))))')
    out.append("\t)")
    return out


# ---------------------------------------------------------------------------
# symbol-map.yaml + report
# ---------------------------------------------------------------------------


def write_symbol_map(path: Path, netlist: Netlist, mappings: Mapping[str, SymbolMapping]) -> None:
    lines = ["# pcb-schgen symbol mapping (footprint/ref/value/role -> KiCad symbol)", "symbols:"]
    for ref in sorted(mappings, key=lambda r: (netlist.components[r].prefix, netlist.components[r].number)):
        m = mappings[ref]
        comp = netlist.components[ref]
        lines.append(f"  {ref}:")
        lines.append(f"    lib_id: {m.lib_id}")
        lines.append(f"    value: {_yaml_str(comp.display_value)}")
        lines.append(f"    part: {_yaml_str(comp.part)}")
        lines.append(f"    footprint: {_yaml_str(comp.footprint)}")
        lines.append(f"    fallback: {str(m.fallback).lower()}")
        lines.append(f"    requires_review: {str(m.requires_review).lower()}")
        lines.append(f"    reason: {_yaml_str(m.reason)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _yaml_str(text: str) -> str:
    if text == "":
        return '""'
    if re.search(r"[:#\{\}\[\],&*?|<>=!%@`\"']", text) or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def build_report(
    netlist: Netlist,
    mappings: Mapping[str, SymbolMapping],
    model: SchModel,
    policies: Sequence[NetPolicy],
    validation: Mapping[str, Any],
) -> Dict[str, Any]:
    placed_refs = {s.ref for s in model.symbols}
    refs_total = len(netlist.components)
    missing_refs = sorted(set(netlist.components) - placed_refs, key=lambda r: r)
    nets_total = len(netlist.nets)

    groups = extract_connectivity(model)
    nets_matched = 0
    missing_nets: List[str] = []
    for net in netlist.nets:
        members = [(r, p) for r, p in net.nodes if r in placed_refs]
        roots = {groups.get(m) for m in members if m in groups}
        if not members:
            # Empty net (no nodes) or all members unplaced: nothing to verify.
            nets_matched += 1
        elif len(roots) == 1 and all(m in groups for m in members):
            nets_matched += 1
        else:
            missing_nets.append(net.name)

    global_label_nets = sorted({p.name for p in policies if p.kind in ("ground", "power", "label")})
    wired_nets = sorted({p.name for p in policies if p.kind == "wire"})
    symbol_fallbacks = sorted(r for r, m in mappings.items() if m.fallback)
    requires_review = sorted(r for r, m in mappings.items() if m.requires_review)

    return {
        "tool": "pcb-schgen",
        "version": __version__,
        "refs_total": refs_total,
        "refs_emitted": len(placed_refs),
        "nets_total": nets_total,
        "nets_matched": nets_matched,
        "missing_refs": missing_refs,
        "missing_nets": missing_nets,
        "global_label_nets": global_label_nets,
        "wired_nets": wired_nets,
        "symbol_fallbacks": symbol_fallbacks,
        "requires_review": requires_review,
        "connectivity_ok": bool(validation.get("ok")),
        "connectivity_mismatches": list(validation.get("mismatches", [])),
    }


# ---------------------------------------------------------------------------
# Top-level generate
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GenResult:
    files: Dict[str, str]          # filename -> .kicad_sch text (one key is the root)
    root_name: str                 # filename of the root sheet (the -o file)
    report: Dict[str, Any]
    model: SchModel
    mappings: Dict[str, SymbolMapping]
    netlist: Netlist

    @property
    def sch_text(self) -> str:
        """Text of the root sheet (the -o file)."""

        return self.files[self.root_name]

    @property
    def all_text(self) -> str:
        """Concatenation of every generated sheet (handy for tests/search)."""

        return "\n".join(self.files[k] for k in sorted(self.files))


def generate(
    netlist_path: Path,
    board_path: Optional[Path],
    pln_path: Optional[Path],
    *,
    project: str = "schgen",
    title: str = "pcb-schgen schematic",
    paper: str = "A3",
    fanout_threshold: int = 6,
    random_uuids: bool = False,
    multi_sheet: bool = True,
    root_name: str = "schematic.kicad_sch",
    cfg: Optional[LayoutConfig] = None,
) -> GenResult:
    cfg = cfg or LayoutConfig()
    netlist = parse_netlist(netlist_path)
    positions = parse_board_positions(board_path)
    pln = parse_pln(pln_path)

    mappings = map_symbols(netlist, pln)
    grouping = group_components(netlist, pln)
    placed = layout(netlist, grouping, mappings, positions, cfg, per_sheet=multi_sheet)
    model, policies = build_model(netlist, grouping, mappings, placed, fanout_threshold, random_uuids)

    validation = validate(model, netlist)
    files = render(model, netlist, mappings, project, title, paper, random_uuids, multi_sheet, root_name)
    report = build_report(netlist, mappings, model, policies, validation)
    report["sheets"] = sorted(f for f in files)
    return GenResult(files=files, root_name=root_name, report=report, model=model,
                     mappings=mappings, netlist=netlist)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcb-schgen",
        description="Generate a readable KiCad .kicad_sch from a netlist, board.pln, and placed KiCad board.",
    )
    parser.add_argument("--netlist", type=Path, required=True, help="Zener/pcb netlist, e.g. default.net")
    parser.add_argument("--board", type=Path, help="Placed KiCad board (*.kicad_pcb)")
    parser.add_argument("--pln", type=Path, help="board.pln design-intent file")
    parser.add_argument("--hints", type=Path, help="Optional planning-hints/ directory (advisory)")
    parser.add_argument("--place-report", type=Path, help="Optional pcb-place report JSON (advisory)")
    parser.add_argument("--project", type=Path, help="Optional KiCad project (*.kicad_pro) for naming")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output .kicad_sch path")
    parser.add_argument("--report-json", type=Path, help="Write schematic generation report JSON")
    parser.add_argument("--symbol-map", type=Path, help="Write symbol mapping report YAML")
    parser.add_argument("--title", default=None, help="Schematic title block title")
    parser.add_argument("--paper", default="A3", choices=["A0", "A1", "A2", "A3", "A4"], help="Sheet size")
    parser.add_argument("--label-fanout", type=int, default=6, help="Fanout at/above which a net uses labels instead of wires")
    parser.add_argument("--single-sheet", action="store_true", help="Emit one flat sheet instead of hierarchical per-block sheets")
    parser.add_argument("--random-uuids", action="store_true", help="Use random uuid4 instead of deterministic uuid5")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if connectivity validation fails")
    parser.add_argument("--quiet", action="store_true", help="Suppress the human-readable summary")
    return parser


def _project_name(args: argparse.Namespace) -> str:
    if args.project is not None:
        return args.project.stem
    return args.output.stem


def _main(argv: Optional[List[str]]) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.netlist.exists():
        raise SchgenError(f"netlist not found: {args.netlist}")
    project = _project_name(args)
    title = args.title or project
    result = generate(
        args.netlist,
        args.board,
        args.pln,
        project=project,
        title=title,
        paper=args.paper,
        fanout_threshold=args.label_fanout,
        random_uuids=args.random_uuids,
        multi_sheet=not args.single_sheet,
        root_name=args.output.name,
    )

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in result.files.items():
        path = args.output if name == result.root_name else out_dir / name
        path.write_text(text, encoding="utf-8")

    if args.symbol_map is not None:
        args.symbol_map.parent.mkdir(parents=True, exist_ok=True)
        write_symbol_map(args.symbol_map, result.netlist, result.mappings)

    if args.report_json is not None:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(result.report, indent=2) + "\n", encoding="utf-8")

    if not args.quiet:
        r = result.report
        nsheets = len(result.files)
        suffix = f" (+{nsheets - 1} sub-sheet{'s' if nsheets - 1 != 1 else ''})" if nsheets > 1 else ""
        print(f"pcb-schgen: wrote {args.output}{suffix}")
        print(f"  refs:  {r['refs_emitted']}/{r['refs_total']} emitted")
        print(f"  nets:  {r['nets_matched']}/{r['nets_total']} connectivity-matched")
        print(f"  wired: {len(r['wired_nets'])}  labeled/power: {len(r['global_label_nets'])}")
        print(f"  symbol fallbacks: {len(r['symbol_fallbacks'])}  requires_review: {len(r['requires_review'])}")
        if not r["connectivity_ok"]:
            print(f"  WARNING: connectivity mismatches: {len(r['connectivity_mismatches'])}", file=sys.stderr)
            for m in r["connectivity_mismatches"][:10]:
                print(f"    - {m}", file=sys.stderr)

    if args.strict and not result.report["connectivity_ok"]:
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    except SchgenError as exc:
        print(f"pcb-schgen error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
