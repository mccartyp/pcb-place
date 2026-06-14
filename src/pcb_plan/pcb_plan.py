#!/usr/bin/env python3
"""pcb-plan: heuristic, pin-aware placement-plan generator for pcb-place.

The planner reads KiCad board/connectivity artifacts and emits reviewable .ppl.
It never mutates .kicad_pcb files and never performs routing.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Allow this implementation file to run directly from src/pcb_plan while
# still resolving the import package used by installed console scripts.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
for _path in (_SRC_ROOT, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pcb_place import (
    AliasDiagnostics,
    BBox,
    BoardGeometry,
    DEFAULT_FOOTPRINT_MARGIN_MM,
    PlacementError,
    _find_matching_paren,
    _normalize_ref,
    _parse_fp_local_bbox,
    _rot_point,
    infer_geometry_from_footprints,
    parse_edge_cuts_geometry,
    parse_footprints,
    parse_netlist_aliases,
)

__version__ = "0.15.0"

Point = Tuple[float, float]
_POWER_RE = re.compile(r"^(?:\+?(?:1V[0-9]|1V[0-9]|[0-9]+V[0-9]*|VCC|VDD|VBAT|VIN|VBUS|AVDD|DVDD|PVDD|3V3|5V|12V))", re.I)
_GROUND_RE = re.compile(r"^(?:GND|AGND|DGND|PGND|GNDA|GNDD)$", re.I)
_HIGHSPEED_RE = re.compile(r"(?:TMDS|HDMI|USB|DP|DN|D\+|D-|SSTX|SSRX|PCIE|PCIe|LVDS|CLK|MIPI|ETH|RX|TX)", re.I)

_ALLOWED_VIA_POLICIES = {"avoid", "allow", "constrained", "forbid"}
_ALLOWED_ROUTING_MODES = {"low_speed_only", "all_nets_constrained", "experimental_high_speed"}
_HIGH_SPEED_CLASS_HINTS = ("high_speed", "diff", "rf", "clock", "hdmi", "usb", "pcie", "lvds")

# Conservative default part-to-part spacing profile (mm). Overridable via the
# board.pln `spacing:` section; never defaults to essentially-touching parts.
DEFAULT_SPACING_PROFILE: Dict[str, float] = {
    "default": 0.25,
    "passive_to_passive": 0.25,
    "passive_to_ic": 0.40,
    "ic_to_ic": 0.75,
    "connector_to_component": 1.00,
    "mechanical_to_component": 1.00,
}

_STACKUP_IMPEDANCE_WARNING = (
    "Stackup template applied: layer roles/reference planes are placement/routing intent only. "
    "Controlled impedance requires the actual fabricator stackup; do not trust template "
    "thickness/er values for impedance."
)

# Reusable conservative stackup templates. These set layer roles and likely
# reference planes for placement/routing intent; impedance always requires
# fab-specific validation.
STACKUP_PROFILES: Dict[str, Dict[str, Any]] = {
    "2_layer_basic": {
        "layers": [
            {"name": "F.Cu", "type": "signal"},
            {"name": "B.Cu", "type": "mixed"},
        ],
        "reference_planes": [],
        "high_speed_preferred_layers": ["F.Cu"],
        "power_planes": [],
        "notes": "2-layer basic: no dedicated planes; route ground pours generously; "
                 "high-speed interfaces are discouraged without careful return-path planning.",
    },
    "4_layer_signal_gnd_pwr_signal": {
        "layers": [
            {"name": "F.Cu", "type": "signal"},
            {"name": "In1.GND", "type": "plane", "net": "GND"},
            {"name": "In2.PWR", "type": "plane"},
            {"name": "B.Cu", "type": "signal"},
        ],
        "reference_planes": ["In1.GND"],
        "high_speed_preferred_layers": ["F.Cu"],
        "power_planes": ["In2.PWR"],
        "notes": "4-layer SIG/GND/PWR/SIG: route high-speed on F.Cu over In1.GND; "
                 "B.Cu references the power plane and is second choice for high-speed.",
    },
    "6_layer_high_speed": {
        "layers": [
            {"name": "F.Cu", "type": "signal"},
            {"name": "In1.GND", "type": "plane", "net": "GND"},
            {"name": "In2.PWR", "type": "plane"},
            {"name": "In3.SIG", "type": "signal"},
            {"name": "In4.GND", "type": "plane", "net": "GND"},
            {"name": "B.Cu", "type": "signal"},
        ],
        "reference_planes": ["In1.GND", "In4.GND"],
        "high_speed_preferred_layers": ["F.Cu", "In3.SIG"],
        "power_planes": ["In2.PWR"],
        "notes": "6-layer high-speed: F.Cu over In1.GND and In3.SIG between planes are the "
                 "preferred high-speed layers.",
    },
    "8_layer_high_speed": {
        "layers": [
            {"name": "F.Cu", "type": "signal"},
            {"name": "In1.GND", "type": "plane", "net": "GND"},
            {"name": "In2.SIG", "type": "signal"},
            {"name": "In3.PWR", "type": "plane"},
            {"name": "In4.GND", "type": "plane", "net": "GND"},
            {"name": "In5.SIG", "type": "signal"},
            {"name": "In6.GND", "type": "plane", "net": "GND"},
            {"name": "B.Cu", "type": "signal"},
        ],
        "reference_planes": ["In1.GND", "In4.GND", "In6.GND"],
        "high_speed_preferred_layers": ["In2.SIG", "In5.SIG"],
        "power_planes": ["In3.PWR"],
        "notes": "8-layer high-speed: stripline layers In2.SIG/In5.SIG between ground planes "
                 "are preferred for the most critical pairs.",
    },
    "10_layer_high_speed": {
        "layers": [
            {"name": "F.Cu", "type": "signal"},
            {"name": "In1.GND", "type": "plane", "net": "GND"},
            {"name": "In2.SIG", "type": "signal"},
            {"name": "In3.GND", "type": "plane", "net": "GND"},
            {"name": "In4.PWR", "type": "plane"},
            {"name": "In5.PWR", "type": "plane"},
            {"name": "In6.GND", "type": "plane", "net": "GND"},
            {"name": "In7.SIG", "type": "signal"},
            {"name": "In8.GND", "type": "plane", "net": "GND"},
            {"name": "B.Cu", "type": "signal"},
        ],
        "reference_planes": ["In1.GND", "In3.GND", "In6.GND", "In8.GND"],
        "high_speed_preferred_layers": ["In2.SIG", "In7.SIG"],
        "power_planes": ["In4.PWR", "In5.PWR"],
        "notes": "10-layer high-speed: dual stripline signal layers with adjacent ground planes; "
                 "power planes paired in the core.",
    },
}

_LAYER_COUNT_PROFILES: Dict[int, str] = {
    2: "2_layer_basic",
    4: "4_layer_signal_gnd_pwr_signal",
    6: "6_layer_high_speed",
    8: "8_layer_high_speed",
    10: "10_layer_high_speed",
}



@dataclasses.dataclass
class PlanPad:
    number: str
    name: str
    local_x: float
    local_y: float
    abs_x: float
    abs_y: float
    layers: List[str]
    shape: str
    size: Tuple[float, float]
    net: Optional[str] = None


@dataclasses.dataclass
class PlanComponent:
    ref: str
    footprint: str
    uuid: Optional[str]
    value: Optional[str]
    x: float
    y: float
    rot: float
    layer: Optional[str]
    pads: List[PlanPad]
    bbox: Optional[BBox]
    role: str = "unknown"
    role_reasons: List[str] = dataclasses.field(default_factory=list)

    @property
    def nets(self) -> List[str]:
        return sorted({p.net for p in self.pads if p.net})


@dataclasses.dataclass
class PlanNet:
    name: str
    pads: List[Tuple[str, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class DifferentialPair:
    name: str
    p: str
    n: str
    components: List[str]


@dataclasses.dataclass
class PlanRule:
    kind: str
    text: str
    refs: List[str]
    comment: Optional[str] = None


@dataclasses.dataclass
class Plan:
    board: BoardGeometry
    components: Dict[str, PlanComponent]
    nets: Dict[str, PlanNet]
    aliases: Dict[str, str]
    alias_diagnostics: AliasDiagnostics
    roles: Dict[str, str]
    regions: Dict[str, Dict[str, Any]]
    keepouts: List[Dict[str, Any]]
    clusters: List[Dict[str, Any]]
    differential_pairs: List[DifferentialPair]
    stackup: Dict[str, Any]
    routing: Dict[str, Any]
    declared_differential_pairs: Dict[str, Dict[str, Any]]
    net_classes: Dict[str, Any]
    routing_overrides: Dict[str, Dict[str, Any]]
    simulation: Dict[str, Any]
    routing_warnings: List[str]
    stackup_warnings: List[str]
    simulation_warnings: List[str]
    high_speed_constraints_complete: bool
    rules: List[PlanRule]
    warnings: List[str]
    uncertain_inferences: List[str]
    explanations: Dict[str, Dict[str, Any]]
    topology_failures: List[str]
    decoupling_groups: List[Dict[str, Any]]
    pullup_groups: List[Dict[str, Any]]
    duplicate_rules: List[str]
    spacing: Dict[str, float] = dataclasses.field(default_factory=lambda: dict(DEFAULT_SPACING_PROFILE))
    functional_paths: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    power_islands: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    mounting_hole_plan: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    edge_rotation_plan: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


def _q(value: Any) -> str:
    return json.dumps(value)


def _qn(value: Any) -> str:
    """Like _q(), but renders None as the Python literal `None` instead of `null`."""
    if value is None:
        return "None"
    return json.dumps(value)


def _q_list_with_none(items: Sequence[Any]) -> str:
    """Render a list literal where individual `None` elements stay valid Python."""
    return "[" + ", ".join(_qn(v) for v in items) + "]"


def _ref_prefix(ref: str) -> str:
    return re.match(r"[A-Za-z]+", ref).group(0).upper() if re.match(r"[A-Za-z]+", ref) else ""


def is_ground(net: Optional[str]) -> bool:
    return bool(net and _GROUND_RE.match(net))


def is_power(net: Optional[str]) -> bool:
    return bool(net and not is_ground(net) and _POWER_RE.match(net))


def is_high_speed(net: Optional[str]) -> bool:
    return bool(net and _HIGHSPEED_RE.search(net))


def _first_quoted_after_head(block: str, head: str) -> Optional[str]:
    m = re.search(r"\(" + re.escape(head) + r"\s+\"([^\"]+)\"", block)
    return m.group(1) if m else None


def _prop(block: str, name: str) -> Optional[str]:
    m = re.search(r'\(property\s+"' + re.escape(name) + r'"\s+"([^\"]*)"', block)
    return m.group(1) if m else None


def _uuid(block: str) -> Optional[str]:
    m = re.search(r'\(uuid\s+"?([^"\s)]+)"?\)', block)
    return m.group(1) if m else None


def _parse_layers(block: str) -> List[str]:
    m = re.search(r"\(layers\s+([^)]*)\)", block)
    if not m:
        return []
    return [p.strip('"') for p in m.group(1).split()]


def _parse_pads(fp: Any) -> List[PlanPad]:
    pads: List[PlanPad] = []
    for match in re.finditer(r"\(pad\b", fp.text):
        block = fp.text[match.start():_find_matching_paren(fp.text, fp.text.find("(", match.start()))]
        name_match = re.match(r'\(pad\s+("(?:[^"\\]|\\.)*"|[^\s()]+)', block)
        if not name_match:
            continue
        num = name_match.group(1).strip('"')
        at = re.search(r"\(at\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)(?:\s+([-+0-9.eE]+))?\)", block)
        size = re.search(r"\(size\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)", block)
        shape_match = re.match(r'\(pad\s+(?:"(?:[^"\\]|\\.)*"|[^\s()]+)\s+[^\s()]+\s+([^\s()]+)', block)
        net_match = re.search(r'\(net\s+\d+\s+"([^\"]+)"\)', block)
        pin_name = _prop(block, "Pin Name") or num
        lx = float(at.group(1)) if at else 0.0
        ly = float(at.group(2)) if at else 0.0
        rx, ry = _rot_point(lx, ly, fp.rot)
        pads.append(PlanPad(
            number=num,
            name=pin_name,
            local_x=lx,
            local_y=ly,
            abs_x=fp.x + rx,
            abs_y=fp.y + ry,
            layers=_parse_layers(block),
            shape=shape_match.group(1) if shape_match else "unknown",
            size=(float(size.group(1)), float(size.group(2))) if size else (0.0, 0.0),
            net=net_match.group(1) if net_match else None,
        ))
    return pads


def parse_board(path: Path) -> Tuple[BoardGeometry, Dict[str, PlanComponent], Dict[str, PlanNet], List[str]]:
    text = path.read_text(encoding="utf-8")
    fps = parse_footprints(text)
    edge_board = parse_edge_cuts_geometry(text)
    warnings: List[str] = []
    if edge_board is not None:
        board = edge_board
    else:
        board = infer_geometry_from_footprints(fps)
        warnings.append("WARNING: geometry inferred from footprint extents; review board.pln. "
                        f"A {DEFAULT_FOOTPRINT_MARGIN_MM:g} mm margin was added around footprint extents "
                        "as a last-resort board-size fallback (no board.pln geometry or Edge.Cuts rectangle "
                        "was available). Supply board.width/height/origin in board.pln for authoritative dimensions.")
    components: Dict[str, PlanComponent] = {}
    nets: Dict[str, PlanNet] = {}
    for ref, fp in sorted(fps.items()):
        pads = _parse_pads(fp)
        local_bbox = _parse_fp_local_bbox(fp)
        bbox = None
        if local_bbox:
            # Approximate at current footprint transform.
            points = []
            for x, y in ((local_bbox.min_x, local_bbox.min_y), (local_bbox.max_x, local_bbox.min_y), (local_bbox.max_x, local_bbox.max_y), (local_bbox.min_x, local_bbox.max_y)):
                rx, ry = _rot_point(x, y, fp.rot)
                points.append((fp.x + rx, fp.y + ry))
            bbox = BBox(min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points))
        footprint = _first_quoted_after_head(fp.text, "footprint") or ""
        comp = PlanComponent(ref=ref, footprint=footprint, uuid=_uuid(fp.text), value=_prop(fp.text, "Value"), x=fp.x, y=fp.y, rot=fp.rot, layer=fp.layer, pads=pads, bbox=bbox)
        components[ref] = comp
        for pad in pads:
            if pad.net:
                nets.setdefault(pad.net, PlanNet(pad.net)).pads.append((ref, pad.number))
    return board, components, nets, warnings


def _extract_connectivity_from_json_obj(obj: Any, components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> None:
    if isinstance(obj, dict):
        ref = obj.get("ref") or obj.get("reference") or obj.get("designator")
        value = obj.get("value")
        fp = obj.get("footprint")
        if ref and _normalize_ref(str(ref)) in components:
            comp = components[_normalize_ref(str(ref))]
            if value and not comp.value:
                comp.value = str(value)
            if fp and not comp.footprint:
                comp.footprint = str(fp)
            pinmap = obj.get("pins") or obj.get("pin_to_net") or obj.get("pinToNet") or obj.get("pads")
            if isinstance(pinmap, dict):
                by_num = {p.number: p for p in comp.pads}
                for pin, net in pinmap.items():
                    if str(pin) in by_num and isinstance(net, str):
                        by_num[str(pin)].net = net
                        nets.setdefault(net, PlanNet(net)).pads.append((comp.ref, str(pin)))
        # Net entries may look like {name: "3V3", nodes: [{ref,pin}]}.
        net_name = obj.get("name") or obj.get("net")
        nodes = obj.get("nodes") or obj.get("pins")
        if isinstance(net_name, str) and isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict):
                    r = node.get("ref") or node.get("reference")
                    p = node.get("pin") or node.get("pad") or node.get("number")
                    if r and p and _normalize_ref(str(r)) in components:
                        nets.setdefault(net_name, PlanNet(net_name)).pads.append((_normalize_ref(str(r)), str(p)))
                        comp = components[_normalize_ref(str(r))]
                        for pad in comp.pads:
                            if pad.number == str(p):
                                pad.net = net_name
        for value2 in obj.values():
            _extract_connectivity_from_json_obj(value2, components, nets)
    elif isinstance(obj, list):
        for item in obj:
            _extract_connectivity_from_json_obj(item, components, nets)



def _clear_imported_net_connectivity(nets: Dict[str, PlanNet]) -> None:
    """Remove duplicate net entries before an external netlist re-populates pads."""
    for net in nets.values():
        seen: set[Tuple[str, str]] = set()
        unique: List[Tuple[str, str]] = []
        for ref, pin in net.pads:
            key = (_normalize_ref(ref), str(pin))
            if key not in seen:
                seen.add(key)
                unique.append(key)
        net.pads = unique


def _connect_pin(components: Dict[str, PlanComponent], nets: Dict[str, PlanNet], net_name: str, ref: Any, pin: Any) -> bool:
    if not net_name or ref is None or pin is None:
        return False
    ref_norm = _normalize_ref(str(ref))
    pin_text = str(pin).strip('"')
    if ref_norm not in components:
        return False
    net = nets.setdefault(str(net_name), PlanNet(str(net_name)))
    node = (ref_norm, pin_text)
    if node not in net.pads:
        net.pads.append(node)
    comp = components[ref_norm]
    matched = False
    for pad in comp.pads:
        if pad.number == pin_text or pad.name == pin_text:
            pad.net = str(net_name)
            matched = True
    return matched or True


def _detect_netlist_format(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith(('{', '[')):
        return 'json'
    if stripped.startswith('<'):
        return 'xml'
    if stripped.startswith('('):
        return 'sexp'
    return 'unknown'


def _add_component_metadata(components: Dict[str, PlanComponent], ref: Any, *, value: Any = None, footprint: Any = None) -> None:
    if ref is None:
        return
    ref_norm = _normalize_ref(str(ref))
    comp = components.get(ref_norm)
    if not comp:
        return
    if value and not comp.value:
        comp.value = str(value)
    if footprint and not comp.footprint:
        comp.footprint = str(footprint)


def _extract_json_netlist(obj: Any, components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> None:
    if isinstance(obj, dict):
        ref = obj.get('ref') or obj.get('reference') or obj.get('designator')
        _add_component_metadata(components, ref, value=obj.get('value'), footprint=obj.get('footprint'))
        pinmap = obj.get('pins') or obj.get('pin_to_net') or obj.get('pinToNet') or obj.get('pads')
        if ref and isinstance(pinmap, dict):
            for pin, net in pinmap.items():
                if isinstance(net, str):
                    _connect_pin(components, nets, net, ref, pin)
        net_name = obj.get('name') or obj.get('net') or obj.get('net_name')
        nodes = obj.get('nodes') or (obj.get('pins') if isinstance(obj.get('pins'), list) else None) or obj.get('connections')
        if isinstance(net_name, str) and isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict):
                    _connect_pin(components, nets, net_name, node.get('ref') or node.get('reference') or node.get('component'), node.get('pin') or node.get('pad') or node.get('number'))
                elif isinstance(node, (list, tuple)) and len(node) >= 2:
                    _connect_pin(components, nets, net_name, node[0], node[1])
        for value2 in obj.values():
            _extract_json_netlist(value2, components, nets)
    elif isinstance(obj, list):
        for item in obj:
            _extract_json_netlist(item, components, nets)


def _xml_attr_or_child(elem: ET.Element, *names: str) -> Optional[str]:
    lowered = {k.lower(): v for k, v in elem.attrib.items()}
    for name in names:
        if name in elem.attrib:
            return elem.attrib[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    for child in list(elem):
        tag = child.tag.rsplit('}', 1)[-1].lower()
        if tag in {n.lower() for n in names}:
            return (child.text or '').strip() or child.attrib.get('value') or child.attrib.get('name')
    return None


def _extract_xml_netlist(text: str, components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> None:
    root = ET.fromstring(text)
    for elem in root.iter():
        tag = elem.tag.rsplit('}', 1)[-1].lower()
        if tag in {'comp', 'component', 'part'}:
            ref = _xml_attr_or_child(elem, 'ref', 'reference', 'designator')
            value = _xml_attr_or_child(elem, 'value')
            footprint = _xml_attr_or_child(elem, 'footprint')
            for prop in elem.findall('.//property'):
                pname = (prop.attrib.get('name') or prop.attrib.get('key') or '').lower()
                pval = prop.attrib.get('value') or (prop.text or '').strip()
                if pname in {'value'}:
                    value = value or pval
                if pname in {'footprint'}:
                    footprint = footprint or pval
            _add_component_metadata(components, ref, value=value, footprint=footprint)
        if tag == 'net':
            net_name = _xml_attr_or_child(elem, 'name', 'net')
            if not net_name:
                continue
            for node in elem.iter():
                node_tag = node.tag.rsplit('}', 1)[-1].lower()
                if node_tag in {'node', 'pin', 'pad'}:
                    _connect_pin(components, nets, net_name, _xml_attr_or_child(node, 'ref', 'reference', 'component'), _xml_attr_or_child(node, 'pin', 'pad', 'number', 'num'))


def _sexp_tokens_plan(text: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == ';':
            j = text.find('\n', i)
            i = len(text) if j < 0 else j + 1
            continue
        if c in '()':
            tokens.append(c); i += 1; continue
        if c == '"':
            j = i + 1; buf = []
            while j < len(text):
                if text[j] == '\\' and j + 1 < len(text):
                    buf.append(text[j + 1]); j += 2; continue
                if text[j] == '"':
                    break
                buf.append(text[j]); j += 1
            tokens.append(''.join(buf)); i = j + 1
            continue
        j = i
        while j < len(text) and not text[j].isspace() and text[j] not in '();':
            j += 1
        tokens.append(text[i:j]); i = j
    return tokens


def _parse_sexp_plan(tokens: List[str]) -> Any:
    def parse_at(i: int) -> Tuple[Any, int]:
        if tokens[i] != '(':
            return tokens[i], i + 1
        out: List[Any] = []
        i += 1
        while i < len(tokens) and tokens[i] != ')':
            item, i = parse_at(i)
            out.append(item)
        return out, i + 1
    items: List[Any] = []
    i = 0
    while i < len(tokens):
        item, i = parse_at(i)
        items.append(item)
    return items[0] if len(items) == 1 else items


def _walk_forms(node: Any) -> Iterable[List[Any]]:
    if isinstance(node, list):
        if node and isinstance(node[0], str):
            yield node
        for child in node:
            yield from _walk_forms(child)


def _form_values(form: List[Any]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for item in form[1:]:
        if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str) and not isinstance(item[1], list):
            values[item[0].lower()] = str(item[1])
    return values


def _extract_sexp_netlist(text: str, components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> None:
    tree = _parse_sexp_plan(_sexp_tokens_plan(text))
    for form in _walk_forms(tree):
        head = str(form[0]).lower() if form else ''
        values = _form_values(form)
        if head in {'component', 'comp', 'part'}:
            _add_component_metadata(components, values.get('ref') or values.get('reference'), value=values.get('value'), footprint=values.get('footprint'))
        if head == 'net':
            net_name = values.get('name') or values.get('net') or values.get('code')
            # Prefer the explicit (name ...) child; code is only a fallback for unusual artifacts.
            if values.get('name'):
                net_name = values['name']
            if not net_name:
                continue
            for child in form[1:]:
                if isinstance(child, list) and child and str(child[0]).lower() in {'node', 'pin', 'pad'}:
                    nv = _form_values(child)
                    _connect_pin(components, nets, net_name, nv.get('ref') or nv.get('reference') or nv.get('component'), nv.get('pin') or nv.get('pad') or nv.get('number') or nv.get('num'))


def _import_connectivity_by_format(fmt: str, text: str, components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> List[str]:
    before = sum(len(n.pads) for n in nets.values())
    warnings: List[str] = []
    try:
        if fmt == 'json':
            _extract_json_netlist(json.loads(text), components, nets)
        elif fmt == 'xml':
            _extract_xml_netlist(text, components, nets)
        elif fmt == 'sexp':
            _extract_sexp_netlist(text, components, nets)
        else:
            warnings.append('Could not detect netlist format; connectivity import skipped.')
    except Exception as exc:
        warnings.append(f'Failed to parse {fmt} netlist connectivity: {exc}')
    after = sum(len(n.pads) for n in nets.values())
    if fmt != 'unknown' and after <= before and not any(n.pads for n in nets.values()):
        warnings.append(f'{fmt} netlist parsed no pin-to-net connectivity; generated plan quality will be poor.')
    return warnings

def import_netlist(path: Optional[Path], components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> Tuple[Dict[str, str], AliasDiagnostics, List[str]]:
    if path is None:
        return {}, AliasDiagnostics(), []
    aliases, diagnostics = parse_netlist_aliases(path)
    warnings = list(diagnostics.warnings)
    text = path.read_text(encoding="utf-8")
    fmt = _detect_netlist_format(text)
    if diagnostics.parser is None and fmt != "unknown":
        diagnostics.parser = fmt
    warnings.extend(_import_connectivity_by_format(fmt, text, components, nets))
    _clear_imported_net_connectivity(nets)
    if not any(net.pads for net in nets.values()):
        warnings.append("Netlist import produced zero connected nets; check that default.net includes component ref/pin nodes.")
    return aliases, diagnostics, warnings


def load_intent(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith(("{", "[")):
        return json.loads(text)
    return _parse_simple_yaml(text)


def _parse_inline_list(value: str) -> List[Any]:
    inner = value.strip()[1:-1].strip()
    if not inner:
        return []
    return [_parse_scalar(part.strip()) for part in inner.split(",")]


def _parse_inline_map(value: str) -> Dict[str, Any]:
    inner = value.strip()[1:-1].strip()
    result: Dict[str, Any] = {}
    if not inner:
        return result
    for part in inner.split(","):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        result[key.strip().strip('"\'')] = _parse_scalar(val.strip())
    return result


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return {}
    if value.startswith("{") and value.endswith("}"):
        return _parse_inline_map(value)
    if value.startswith("[") and value.endswith("]"):
        return _parse_inline_list(value)
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value) if re.match(r"^-?\d+$", value) else float(value)
    except ValueError:
        pass
    return value

def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """Small YAML subset for .pln board-plan files; JSON remains the lossless option."""
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Any]] = [(-1, root)]
    last_key_at_indent: Dict[int, Tuple[Any, str]] = {}
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
                # A key with no scalar value defaults to a dict, but a following
                # dash means it should be a list (e.g. keepouts: / - name: ...).
                container, key = last_key_at_indent.get(indent - 2, last_key_at_indent.get(indent, (None, None)))
                new_list: List[Any] = []
                if isinstance(container, dict) and key:
                    container[key] = new_list
                    parent = new_list
                    stack.append((indent - 2, parent))
                else:
                    continue
            if not item_text:
                item = {}
                parent.append(item)
                stack.append((indent, item))
            elif ":" in item_text and not item_text.startswith(("{", "[")):
                k, v = item_text.split(":", 1)
                item = {k.strip(): _parse_scalar(v)}
                parent.append(item)
                stack.append((indent, item))
            else:
                parent.append(_parse_scalar(item_text))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        val = _parse_scalar(value)
        if isinstance(parent, dict):
            parent[key] = val
            last_key_at_indent[indent] = (parent, key)
            if isinstance(val, (dict, list)):
                stack.append((indent, val))
    return root



def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_positive_number(value: Any) -> bool:
    return _is_number(value) and float(value) > 0.0


def _field_values(spec: Mapping[str, Any], singular: str, plural: str) -> List[Any]:
    values: List[Any] = []
    if singular in spec:
        values.append(spec[singular])
    if plural in spec:
        plural_value = spec[plural]
        values.extend(plural_value if isinstance(plural_value, list) else [plural_value])
    return values


def _stackup_layer_names(stackup: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for layer in _as_list(stackup.get("layers")):
        if isinstance(layer, Mapping) and layer.get("name") is not None:
            names.add(str(layer["name"]))
    return names


def _stackup_reference_planes(stackup: Mapping[str, Any]) -> List[str]:
    planes: List[str] = []
    for layer in _as_list(stackup.get("layers")):
        if not isinstance(layer, Mapping) or layer.get("name") is None:
            continue
        layer_type = str(layer.get("type", "")).lower()
        if layer_type == "plane" or layer.get("net") is not None:
            planes.append(str(layer["name"]))
    return planes


def _routing_class_is_high_speed(name: str, spec: Mapping[str, Any]) -> bool:
    lname = name.lower()
    if any(hint in lname for hint in _HIGH_SPEED_CLASS_HINTS):
        return True
    if bool(spec.get("differential")) or spec.get("impedance_ohms") is not None:
        return True
    if spec.get("max_skew_mm") is not None or spec.get("max_length_mismatch_mm") is not None:
        return True
    return False


def _simulation_openems_enabled(openems: Mapping[str, Any]) -> bool:
    enabled = openems.get("enabled", False)
    triggers = _as_list(openems.get("trigger_on"))
    if isinstance(enabled, str) and enabled.lower() == "auto":
        return bool(triggers)
    return bool(enabled)


def _validate_layer_refs(spec: Mapping[str, Any], label: str, layer_names: set[str], warnings: List[str]) -> None:
    if not layer_names:
        return
    for layer in _field_values(spec, "preferred_layer", "preferred_layers"):
        if str(layer) not in layer_names:
            warnings.append(f"{label} references missing preferred layer {layer!r}.")
    ref_plane = spec.get("reference_plane")
    if ref_plane is not None and str(ref_plane) not in layer_names:
        warnings.append(f"{label} reference plane {ref_plane!r} is not present in stackup.")


def _guess_layer_type(name: str) -> str:
    upper = name.upper()
    if "GND" in upper or "PWR" in upper or "POWER" in upper or "PLANE" in upper:
        return "plane"
    return "signal"


def _normalize_stackup_layer(layer: Any) -> Dict[str, Any]:
    if isinstance(layer, Mapping):
        return dict(layer)
    name = str(layer)
    entry: Dict[str, Any] = {"name": name, "type": _guess_layer_type(name)}
    if "GND" in name.upper():
        entry["net"] = "GND"
    return entry


def expand_stackup_intent(stackup: Mapping[str, Any], stackup_warnings: List[str]) -> Dict[str, Any]:
    """Expand `stackup.profile`/`stackup.layers: N` into a full template stackup.

    Expanded templates are marked source=stackup_template / requires_review=true:
    they improve placement/routing intent only and never claim impedance accuracy.
    """

    if not stackup:
        return {}
    expanded = dict(stackup)
    layers = expanded.get("layers")
    profile_name = expanded.get("profile")
    if profile_name is None and isinstance(layers, int):
        profile_name = _LAYER_COUNT_PROFILES.get(int(layers))
        if profile_name is None:
            stackup_warnings.append(
                f"stackup.layers: {layers} has no built-in template (supported: "
                f"{sorted(_LAYER_COUNT_PROFILES)}); supply explicit layers.")
            expanded.pop("layers", None)
            return expanded
    if profile_name is None:
        if isinstance(layers, list):
            expanded["layers"] = [_normalize_stackup_layer(layer) for layer in layers]
        return expanded
    template = STACKUP_PROFILES.get(str(profile_name))
    if template is None:
        stackup_warnings.append(
            f"Unknown stackup profile {profile_name!r}; supported profiles: "
            f"{sorted(STACKUP_PROFILES)}.")
        return expanded
    template_layers = [dict(layer) for layer in template["layers"]]
    if isinstance(layers, list) and layers:
        user_layers = [_normalize_stackup_layer(layer) for layer in layers]
        if len(user_layers) != len(template_layers):
            stackup_warnings.append(
                f"stackup profile {profile_name!r} expects {len(template_layers)} layers but "
                f"{len(user_layers)} were listed; using the listed layers.")
        expanded["layers"] = user_layers
    else:
        expanded["layers"] = template_layers
    expanded["profile"] = str(profile_name)
    expanded.setdefault("reference_planes", list(template["reference_planes"]))
    expanded.setdefault("high_speed_preferred_layers", list(template["high_speed_preferred_layers"]))
    expanded.setdefault("power_planes", list(template["power_planes"]))
    expanded.setdefault("notes", template["notes"])
    expanded["source"] = "stackup_template"
    expanded["confidence"] = "medium"
    expanded["requires_review"] = True
    stackup_warnings.append(_STACKUP_IMPEDANCE_WARNING)
    return expanded


def spacing_profile(intent: Mapping[str, Any], warnings: Optional[List[str]] = None) -> Dict[str, float]:
    """Effective spacing profile: conservative defaults merged with board.pln overrides."""

    profile = dict(DEFAULT_SPACING_PROFILE)
    overrides = _as_mapping(intent.get("spacing"))
    for key, value in overrides.items():
        if key not in profile:
            if warnings is not None:
                warnings.append(f"Unknown spacing key {key!r}; expected one of {sorted(profile)}.")
            continue
        if not _is_number(value) or float(value) < 0:
            if warnings is not None:
                warnings.append(f"spacing.{key} must be a non-negative number.")
            continue
        profile[key] = float(value)
    return profile


def validate_routing_intent(intent: Mapping[str, Any], nets: Mapping[str, PlanNet]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any], List[str], List[str], List[str], bool]:
    """Validate optional .pln routing/SI intent without turning it into placement primitives."""

    stackup_warnings: List[str] = []
    routing_warnings: List[str] = []
    simulation_warnings: List[str] = []

    stackup = expand_stackup_intent(_as_mapping(intent.get("stackup")), stackup_warnings)
    routing = _as_mapping(intent.get("routing"))
    declared_pairs = {str(k): _as_mapping(v) for k, v in _as_mapping(intent.get("differential_pairs")).items()}
    net_classes = _as_mapping(intent.get("net_classes"))
    routing_overrides = {str(k): _as_mapping(v) for k, v in _as_mapping(intent.get("routing_overrides")).items()}
    simulation = _as_mapping(intent.get("simulation"))

    layer_names = _stackup_layer_names(stackup)
    reference_planes = set(_stackup_reference_planes(stackup))

    for layer in _as_list(stackup.get("layers")):
        if isinstance(layer, Mapping) and not layer.get("name"):
            stackup_warnings.append("Stackup layer is missing a name.")
    for dielectric in _as_list(stackup.get("dielectric")):
        if not isinstance(dielectric, Mapping):
            continue
        between = dielectric.get("between")
        if isinstance(between, list):
            for layer in between:
                if layer_names and str(layer) not in layer_names:
                    stackup_warnings.append(f"Dielectric references missing layer {layer!r}.")
        thickness = dielectric.get("thickness_mm")
        if thickness is not None and not _is_positive_number(thickness):
            stackup_warnings.append("Dielectric thickness_mm must be positive.")
        er = dielectric.get("er")
        if er is not None and not _is_positive_number(er):
            stackup_warnings.append("Dielectric er must be positive.")

    has_routing_intent = bool(routing or declared_pairs or net_classes or routing_overrides)
    mode = routing.get("mode")
    if mode is not None and str(mode) not in _ALLOWED_ROUTING_MODES:
        routing_warnings.append(f"Routing mode {mode!r} is not supported; expected one of {sorted(_ALLOWED_ROUTING_MODES)}.")

    class_specs: Dict[str, Mapping[str, Any]] = {}
    defaults = _as_mapping(routing.get("defaults"))
    if defaults:
        class_specs["routing.defaults"] = defaults
    for name, spec in _as_mapping(routing.get("classes")).items():
        class_specs[f"routing.classes.{name}"] = _as_mapping(spec)

    high_speed_requested = False
    for label, spec in class_specs.items():
        class_name = label.rsplit(".", 1)[-1]
        high_speed = _routing_class_is_high_speed(class_name, spec)
        high_speed_requested = high_speed_requested or high_speed
        _validate_layer_refs(spec, label, layer_names, routing_warnings)
        ref_plane = spec.get("reference_plane")
        if ref_plane is not None:
            if not layer_names or str(ref_plane) not in layer_names:
                routing_warnings.append(f"{label} reference plane {ref_plane!r} is missing.")
            elif str(ref_plane) not in reference_planes:
                routing_warnings.append(f"{label} reference plane {ref_plane!r} exists but is not marked as a plane layer.")
        via_policy = spec.get("via_policy")
        if via_policy is not None and str(via_policy) not in _ALLOWED_VIA_POLICIES:
            routing_warnings.append(f"{label} via_policy {via_policy!r} is not supported; expected one of {sorted(_ALLOWED_VIA_POLICIES)}.")
        for field in ("trace_width_mm", "clearance_mm", "trace_spacing_mm", "max_skew_mm", "max_length_mismatch_mm"):
            if field in spec and not _is_positive_number(spec[field]):
                routing_warnings.append(f"{label} {field} must be positive.")
        if "impedance_ohms" in spec and not _is_number(spec["impedance_ohms"]):
            routing_warnings.append(f"{label} impedance_ohms must be numeric.")
        if spec.get("impedance_ohms") is not None and (spec.get("trace_width_mm") is None or (spec.get("trace_spacing_mm") is None and bool(spec.get("differential")))):
            routing_warnings.append(f"{label} has an impedance target but no complete trace width/spacing.")
        if high_speed and spec.get("via_policy") == "allow" and spec.get("max_vias") is None:
            routing_warnings.append(f"{label} is high-speed but allows vias without explicit max_vias.")

    for net, spec in routing_overrides.items():
        _validate_layer_refs(spec, f"routing_overrides.{net}", layer_names, routing_warnings)
        via_policy = spec.get("via_policy")
        if via_policy is not None and str(via_policy) not in _ALLOWED_VIA_POLICIES:
            routing_warnings.append(f"routing_overrides.{net} via_policy {via_policy!r} is not supported.")
        for field in ("trace_width_mm", "clearance_mm", "trace_spacing_mm", "max_skew_mm", "max_length_mismatch_mm"):
            if field in spec and not _is_positive_number(spec[field]):
                routing_warnings.append(f"routing_overrides.{net} {field} must be positive.")

    if high_speed_requested and not stackup:
        routing_warnings.append("High-speed routing constraints were requested without a stackup.")

    known_nets = set(nets)
    classes = _as_mapping(routing.get("classes"))
    for pair_name, pair in declared_pairs.items():
        p_net = pair.get("p")
        n_net = pair.get("n")
        cls = pair.get("class")
        if not p_net or not n_net:
            routing_warnings.append(f"Differential pair {pair_name} has missing P/N net.")
        if known_nets:
            if p_net and str(p_net) not in known_nets:
                routing_warnings.append(f"Differential pair {pair_name} P net {p_net!r} is not present in the netlist/board connectivity.")
            if n_net and str(n_net) not in known_nets:
                routing_warnings.append(f"Differential pair {pair_name} N net {n_net!r} is not present in the netlist/board connectivity.")
        if cls and str(cls) not in classes:
            routing_warnings.append(f"Differential pair {pair_name} references unknown routing class {cls!r}.")

    openems = _as_mapping(simulation.get("openems"))
    triggers = [str(t) for t in _as_list(openems.get("trigger_on"))]
    enabled_value = openems.get("enabled")
    openems_active = _simulation_openems_enabled(openems)
    if openems_active and not any(any(token in t.lower() for token in ("high_speed", "rf", "switching")) for t in triggers):
        simulation_warnings.append("OpenEMS is enabled but no high-speed/RF/switching trigger exists.")
    if enabled_value not in (None, True, False) and not (isinstance(enabled_value, str) and enabled_value.lower() == "auto"):
        simulation_warnings.append("simulation.openems.enabled should be true, false, or auto.")

    high_speed_constraints_complete = not routing_warnings and not stackup_warnings
    if high_speed_requested:
        high_speed_constraints_complete = high_speed_constraints_complete and bool(stackup)
    return stackup, routing, declared_pairs, net_classes, routing_overrides, simulation, routing_warnings, stackup_warnings, simulation_warnings, high_speed_constraints_complete


# board.pln may use the spec-level role vocabulary; normalize to internal roles.
_ROLE_ALIASES = {
    "regulator": "power_regulator",
    "oscillator": "clock",
    "retimer_redriver": "hdmi_retimer",
    "protected_ic": "ic",
    "edge_connector": "connector",
    "hdmi_connector": "high_speed_connector",
    "usb_connector": "high_speed_connector",
    "ethernet_connector": "high_speed_connector",
    "load_decoupling": "decoupling",
    "input_cap": "decoupling",
    "output_cap": "decoupling",
    "series_resistor": "series",
    "feedback_resistor": "pullup_pulldown",
    "pullup": "pullup_pulldown",
    "strap": "pullup_pulldown",
}

_DEBUG_HEADER_TOKENS = ("SWD", "JTAG", "DEBUG", "PROG", "ISP", "UART_DEBUG", "TAG-CONNECT", "TAGCONNECT")


def _component_intent(intent: Mapping[str, Any], ref: str) -> Dict[str, Any]:
    """Per-component overrides from the board.pln `components:` section."""

    return _as_mapping(_as_mapping(intent.get("components")).get(ref))


def infer_roles(components: Dict[str, PlanComponent], intent: Mapping[str, Any]) -> None:
    intent_roles = {str(k): str(v) for k, v in (intent.get("roles") or {}).items()} if isinstance(intent.get("roles"), dict) else {}
    for ref, spec in _as_mapping(intent.get("components")).items():
        role = _as_mapping(spec).get("role") if isinstance(spec, Mapping) else None
        if role is not None and str(ref) not in intent_roles:
            intent_roles[str(ref)] = str(role)
    for comp in components.values():
        if comp.ref in intent_roles:
            supplied = intent_roles[comp.ref]
            comp.role = _ROLE_ALIASES.get(supplied, supplied)
            comp.role_reasons.append("role supplied by board intent")
            continue
        pref = _ref_prefix(comp.ref)
        value = (comp.value or "").upper()
        fp = comp.footprint.upper()
        nets = comp.nets
        haystack = f"{fp} {value} {' '.join(n.upper() for n in nets)}"
        if pref in {"H", "MH"}:
            comp.role = "mechanical"; comp.role_reasons.append("reference prefix indicates mechanical")
        elif pref in {"J", "P"} and any(token in haystack for token in _DEBUG_HEADER_TOKENS):
            comp.role = "debug_header"; comp.role_reasons.append("connector value/footprint/nets suggest debug/programming header")
        elif pref in {"J", "P"}:
            comp.role = "high_speed_connector" if ("HDMI" in fp or "USB" in fp or any(is_high_speed(n) for n in nets)) else "connector"
            comp.role_reasons.append("reference prefix indicates connector")
        elif pref == "TP":
            comp.role = "testpoint"; comp.role_reasons.append("reference prefix indicates test point")
        elif pref == "SW":
            comp.role = "switch"; comp.role_reasons.append("reference prefix indicates switch/button")
        elif pref == "LED" or (pref == "D" and ("LED" in fp or "LED" in value)):
            comp.role = "led"; comp.role_reasons.append("reference/value/footprint indicates LED")
        elif pref == "F" and ("FUSE" in fp or "FUSE" in value or "PTC" in value or not value):
            comp.role = "fuse"; comp.role_reasons.append("reference prefix indicates fuse/protection element")
        elif pref in {"C"}:
            if any(is_power(n) for n in nets) and any(is_ground(n) for n in nets):
                comp.role = "decoupling"; comp.role_reasons.append("capacitor connects a power net to ground")
            else:
                comp.role = "capacitor"; comp.role_reasons.append("reference prefix indicates capacitor")
        elif pref in {"D", "U"} and ("ESD" in value or "TVS" in value or "ESD" in fp or "TVS" in fp):
            comp.role = "esd_protection"; comp.role_reasons.append("value/footprint suggests TVS or ESD protection")
        elif pref in {"L", "FB"}:
            if "COMMON" in haystack or "CMC" in haystack or "CHOKE" in haystack:
                comp.role = "common_mode_choke"; comp.role_reasons.append("value/footprint indicates common-mode choke")
            else:
                comp.role = "inductor" if pref == "L" else "ferrite"; comp.role_reasons.append("reference prefix indicates magnetic/passive")
        elif pref in {"Y", "X"} or "CRYSTAL" in fp or "RESONATOR" in fp or "OSC" in value:
            comp.role = "clock"; comp.role_reasons.append("reference/value/footprint suggests clock source")
        elif pref == "R":
            non_ground = [n for n in nets if not is_ground(n)]
            non_power_non_ground = [n for n in non_ground if not is_power(n)]
            if len(nets) == 2 and len(non_ground) == 2 and not any(is_power(n) for n in nets):
                comp.role = "series"; comp.role_reasons.append("two-pin resistor candidate on signal path")
            elif (any(is_power(n) for n in nets) or any(is_ground(n) for n in nets)) and non_power_non_ground:
                comp.role = "pullup_pulldown"; comp.role_reasons.append("resistor connects power/ground to a signal")
            else:
                comp.role = "resistor"; comp.role_reasons.append("reference prefix indicates resistor")
        elif pref == "U":
            if "ESP32" in fp or "WIFI" in fp or "ANT" in fp or "RF" in fp or "MODULE" in fp or any("ANT" in n.upper() for n in nets):
                comp.role = "rf_module"; comp.role_reasons.append("footprint/net names suggest RF/module antenna")
            elif "BUCK" in fp or "REG" in fp or "LDO" in fp or "REG" in value or "PMIC" in value or any(n.upper() in {"SW", "FB", "VIN", "VOUT"} for n in nets):
                comp.role = "power_regulator"; comp.role_reasons.append("value/footprint/nets suggest regulator")
            elif "MCU" in fp or "MCU" in value or "STM32" in fp or "STM32" in value or "NRF" in value:
                comp.role = "mcu"; comp.role_reasons.append("value/footprint suggests MCU")
            elif "HDMI" in fp or "RETIMER" in value or any("TMDS" in n.upper() for n in nets):
                comp.role = "hdmi_retimer"; comp.role_reasons.append("high-speed TMDS/HDMI nets suggest retimer/interface IC")
            else:
                comp.role = "ic"; comp.role_reasons.append("reference prefix indicates IC")
        else:
            comp.role = "unknown"; comp.role_reasons.append("no strong role heuristic matched")


def _canonical_net(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")


def _diff_pair_key(name: str) -> Optional[Tuple[str, str]]:
    raw = name.strip()
    candidates = [(raw, raw.upper()), (_canonical_net(raw), _canonical_net(raw))]
    patterns = [
        (r"^(?P<base>.+?)(?:_|-)?P$", "p"),
        (r"^(?P<base>.+?)(?:_|-)?N$", "n"),
        (r"^(?P<base>.+?)(?:_|-)?DP$", "p"),
        (r"^(?P<base>.+?)(?:_|-)?DN$", "n"),
        (r"^(?P<base>.+?)\+$", "p"),
        (r"^(?P<base>.+?)-$", "n"),
        (r"^(?P<base>HDMI.*TMDS.*D[0-2])P$", "p"),
        (r"^(?P<base>HDMI.*TMDS.*D[0-2])N$", "n"),
        (r"^(?P<base>TMDS[0-2])P$", "p"),
        (r"^(?P<base>TMDS[0-2])N$", "n"),
        (r"^(?P<base>TMDS(?:CLK|CLOCK))P$", "p"),
        (r"^(?P<base>TMDS(?:CLK|CLOCK))N$", "n"),
    ]
    for original, upper in candidates:
        for pat, polarity in patterns:
            m = re.match(pat, upper)
            if m:
                return re.sub(r"_+$", "", m.group("base")), polarity
    return None


def detect_differential_pairs(nets: Mapping[str, PlanNet]) -> List[DifferentialPair]:
    grouped: Dict[str, Dict[str, str]] = {}
    for name in nets:
        key = _diff_pair_key(name)
        if not key:
            continue
        base, polarity = key
        grouped.setdefault(base, {})[polarity] = name
    pairs: List[DifferentialPair] = []
    used: set[str] = set()
    for base, pn in sorted(grouped.items()):
        if "p" not in pn or "n" not in pn or pn["p"] in used or pn["n"] in used:
            continue
        if not (is_high_speed(pn["p"]) or is_high_speed(pn["n"]) or any(tok in base for tok in ("TMDS", "USB", "HDMI", "DP", "LVDS", "PCIE"))):
            continue
        comps = sorted({r for r, _ in nets[pn["p"]].pads} | {r for r, _ in nets[pn["n"]].pads})
        pairs.append(DifferentialPair(base, pn["p"], pn["n"], comps))
        used.update({pn["p"], pn["n"]})
    return pairs


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _synthesized_rf_keepout(
    comp: PlanComponent,
    board: BoardGeometry,
    *,
    desired_w: float = 12.0,
    desired_h: float = 10.0,
    margin: float = 0.25,
) -> Dict[str, float]:
    """Return a board-local RF keepout adjacent to, not covering, an RF module.

    The synthesized rectangle is placed in the nearest available edge strip outside
    the module's parsed bounding box. This keeps default planner output usable by
    pcb-place validation while still reserving antenna clearance at a board edge.
    """

    if comp.bbox is None:
        cx = comp.x - board.origin_x
        cy = comp.y - board.origin_y
        min_x = max(0.0, cx - 1.0)
        max_x = min(board.width, cx + 1.0)
        min_y = max(0.0, cy - 1.0)
        max_y = min(board.height, cy + 1.0)
    else:
        min_x = comp.bbox.min_x - board.origin_x
        max_x = comp.bbox.max_x - board.origin_x
        min_y = comp.bbox.min_y - board.origin_y
        max_y = comp.bbox.max_y - board.origin_y
        cx = (min_x + max_x) / 2.0
        cy = (min_y + max_y) / 2.0

    candidates: List[Tuple[float, Dict[str, float]]] = []

    left_w = max(0.0, min(desired_w, min_x - margin))
    if left_w > 0.0:
        candidates.append((
            min_x,
            {
                "x": max(0.0, min_x - margin - left_w),
                "y": _clamp(cy - desired_h / 2.0, 0.0, max(0.0, board.height - desired_h)),
                "w": left_w,
                "h": min(desired_h, board.height),
            },
        ))

    right_x = max_x + margin
    right_w = max(0.0, min(desired_w, board.width - right_x))
    if right_w > 0.0:
        candidates.append((
            board.width - max_x,
            {
                "x": right_x,
                "y": _clamp(cy - desired_h / 2.0, 0.0, max(0.0, board.height - desired_h)),
                "w": right_w,
                "h": min(desired_h, board.height),
            },
        ))

    top_h = max(0.0, min(desired_h, min_y - margin))
    if top_h > 0.0:
        candidates.append((
            min_y,
            {
                "x": _clamp(cx - desired_w / 2.0, 0.0, max(0.0, board.width - desired_w)),
                "y": max(0.0, min_y - margin - top_h),
                "w": min(desired_w, board.width),
                "h": top_h,
            },
        ))

    bottom_y = max_y + margin
    bottom_h = max(0.0, min(desired_h, board.height - bottom_y))
    if bottom_h > 0.0:
        candidates.append((
            board.height - max_y,
            {
                "x": _clamp(cx - desired_w / 2.0, 0.0, max(0.0, board.width - desired_w)),
                "y": bottom_y,
                "w": min(desired_w, board.width),
                "h": bottom_h,
            },
        ))

    if not candidates:
        # Degenerate fallback for extremely cramped boards: keep a small corner
        # marker rather than covering the module and causing executor failure.
        return {"x": 0.0, "y": 0.0, "w": min(1.0, board.width), "h": min(1.0, board.height)}

    _distance, rect = min(candidates, key=lambda item: (item[0], -item[1]["w"] * item[1]["h"]))
    return rect



def _component_pad_side(parent: PlanComponent, pad: Optional[str]) -> Optional[str]:
    if parent.bbox is None or pad is None:
        return None
    matches = [p for p in parent.pads if p.number == pad or p.name == pad]
    if not matches:
        return None
    px = sum(p.abs_x for p in matches) / len(matches)
    py = sum(p.abs_y for p in matches) / len(matches)
    distances = {
        "left": abs(px - parent.bbox.min_x),
        "right": abs(px - parent.bbox.max_x),
        "top": abs(py - parent.bbox.min_y),
        "bottom": abs(py - parent.bbox.max_y),
    }
    return min(distances, key=distances.get)




def _effective_support_side(inferred_side: Optional[str], parent: PlanComponent, board: BoardGeometry, threshold: float = 3.0) -> Optional[str]:
    """Return an inward support side when a pad points into a nearby board edge."""

    if inferred_side not in {"left", "right", "top", "bottom"} or parent.bbox is None:
        return inferred_side
    near = {
        "left": parent.bbox.min_x - board.origin_x <= threshold,
        "right": board.origin_x + board.width - parent.bbox.max_x <= threshold,
        "top": parent.bbox.min_y - board.origin_y <= threshold,
        "bottom": board.origin_y + board.height - parent.bbox.max_y <= threshold,
    }
    opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    return opposite[inferred_side] if near.get(inferred_side, False) else inferred_side

def _component_near_board_edge(parent: PlanComponent, board: BoardGeometry, threshold: float = 3.0) -> bool:
    if parent.bbox is None:
        x = parent.x - board.origin_x
        y = parent.y - board.origin_y
        return min(x, board.width - x, y, board.height - y) <= threshold
    return min(
        parent.bbox.min_x - board.origin_x,
        board.origin_x + board.width - parent.bbox.max_x,
        parent.bbox.min_y - board.origin_y,
        board.origin_y + board.height - parent.bbox.max_y,
    ) <= threshold

def _nearest_parent(comp: PlanComponent, candidates: Iterable[PlanComponent], shared_nets: Optional[set[str]] = None) -> Optional[PlanComponent]:
    best: Tuple[float, Optional[PlanComponent]] = (1e99, None)
    for cand in candidates:
        if cand.ref == comp.ref:
            continue
        if shared_nets and not (set(cand.nets) & shared_nets):
            continue
        d = math.hypot(comp.x - cand.x, comp.y - cand.y)
        if d < best[0]:
            best = (d, cand)
    return best[1]


def _nearest_pad(parent: PlanComponent, nets: set[str]) -> Optional[str]:
    pads = [p.number for p in parent.pads if p.net in nets]
    return pads[0] if pads else None


def _connected_refs(anchor: PlanComponent, nets: Mapping[str, PlanNet]) -> set[str]:
    refs: set[str] = set()
    for net_name in anchor.nets:
        if is_ground(net_name) or is_power(net_name):
            continue
        for ref, _pin in nets.get(net_name, PlanNet(net_name)).pads:
            if ref != anchor.ref:
                refs.add(ref)
    return refs


def _alias_groups(aliases: Mapping[str, str]) -> Dict[str, set[str]]:
    groups: Dict[str, set[str]] = {}
    for alias, ref in aliases.items():
        parts = re.split(r"[/.]+", alias.strip("/"))
        if parts and ref:
            groups.setdefault(parts[0].upper(), set()).add(_normalize_ref(ref))
    return groups


# Roles whose placement is owned by a parent IC/connector rather than being a
# cluster anchor in their own right.
_CLUSTER_SUPPORT_ROLES = {
    "decoupling", "esd_protection", "pullup_pulldown", "series", "clock",
    "testpoint", "inductor", "ferrite", "capacitor", "resistor",
}

# Roles that can anchor a cluster and therefore own support parts.
_CLUSTER_ANCHOR_ROLES = {
    "ic", "mcu", "hdmi_retimer", "rf_module", "power_regulator",
    "high_speed_connector", "connector", "debug_header",
}

# Owner-selection priority for a support part's *signal* parent (lower wins):
# the part follows the signal off-board (connector) or to its controller before a
# generic peripheral or regulator.
_SUPPORT_OWNER_PRIORITY = {
    "high_speed_connector": 0, "connector": 0, "debug_header": 0,
    "mcu": 1, "hdmi_retimer": 2, "rf_module": 2, "ic": 3, "power_regulator": 4,
}


def assign_support_owners(components: Mapping[str, PlanComponent],
                          nets: Optional[Mapping[str, PlanNet]] = None) -> Dict[str, str]:
    """Assign each support part a single owning anchor by connectivity, not geometry.

    A support passive (decoupling cap, pull-up, test point, ESD, series element,
    ...) is owned by the IC/connector it is electrically tied to, so the coarse
    cluster pass does not sweep it into a physically-adjacent but unrelated IC
    (which would then drag it off-board when that IC reflows). Ownership prefers a
    real-signal parent (the connector/controller the part actually serves); a part
    that only shares power/ground — e.g. a decoupling cap — falls back to its
    nearest power-sharing anchor, matching where its decoupling array will land.
    Returns a ``ref -> owner_ref`` map for support parts only.
    """

    nets = nets or {}
    anchors = [c for c in components.values() if c.role in _CLUSTER_ANCHOR_ROLES]
    owners: Dict[str, str] = {}
    if not anchors:
        return owners
    graph = ConnectivityGraph(components, nets)
    for comp in components.values():
        if comp.role not in _CLUSTER_SUPPORT_ROLES:
            continue
        # 1) Real-signal parents: anchors sharing a non-power/ground net.
        signal_anchors: List[PlanComponent] = []
        for net_name in comp.nets:
            if is_ground(net_name) or is_power(net_name):
                continue
            for ref in graph.peers(comp.ref, net_name):
                a = components.get(ref)
                if a is not None and a.role in _CLUSTER_ANCHOR_ROLES and a not in signal_anchors:
                    signal_anchors.append(a)
        if signal_anchors:
            signal_anchors.sort(key=lambda a: (_SUPPORT_OWNER_PRIORITY.get(a.role, 9),
                                               math.hypot(comp.x - a.x, comp.y - a.y), a.ref))
            owners[comp.ref] = signal_anchors[0].ref
            continue
        # 2) Power/ground-only parts (decoupling): nearest power-sharing anchor.
        comp_nets = set(comp.nets)
        power_anchors = [a for a in anchors if comp_nets & set(a.nets)
                         and a.role not in {"high_speed_connector", "connector", "debug_header"}]
        pool = power_anchors or anchors
        pool.sort(key=lambda a: (math.hypot(comp.x - a.x, comp.y - a.y),
                                 _SUPPORT_OWNER_PRIORITY.get(a.role, 9), a.ref))
        owners[comp.ref] = pool[0].ref
    return owners


def _cluster_members(anchor: PlanComponent, components: Mapping[str, PlanComponent], nets: Optional[Mapping[str, PlanNet]] = None, aliases: Optional[Mapping[str, str]] = None, radius: float = 12.0, proximity_any_role: bool = True, support_owner: Optional[Mapping[str, str]] = None) -> List[str]:
    shared = set(anchor.nets)
    connected = _connected_refs(anchor, nets or {}) if nets else set()
    alias_refs = set()
    for refs in _alias_groups(aliases or {}).values():
        if anchor.ref in refs:
            alias_refs |= refs
    support_roles = _CLUSTER_SUPPORT_ROLES
    members = [anchor.ref]
    for comp in components.values():
        if comp.ref == anchor.ref or comp.role == "mechanical":
            continue
        dist = math.hypot(anchor.x - comp.x, anchor.y - comp.y)
        if dist > radius and comp.ref not in alias_refs:
            continue
        has_shared_signal = bool((shared & set(comp.nets)) - {n for n in shared if is_ground(n) or is_power(n)})
        is_support = comp.role in support_roles and dist <= radius
        if not proximity_any_role:
            is_support = is_support and (has_shared_signal or comp.ref in connected)
        # Connectivity-aware ownership: when a global owner map is supplied, a
        # support part only joins the cluster of its assigned owner. This stops a
        # geometrically-adjacent but electrically-unrelated passive from being
        # swept into the wrong cluster and dragged off-board on reflow.
        if support_owner is not None and comp.role in support_roles:
            is_support = is_support and support_owner.get(comp.ref) == anchor.ref
        is_nearby_any_role = (proximity_any_role and dist <= radius / 2.0
                              and comp.role != "unknown"
                              and comp.role not in support_roles)
        if comp.ref in connected or comp.ref in alias_refs or has_shared_signal or is_support or is_nearby_any_role:
            members.append(comp.ref)
    return sorted(set(members), key=lambda r: (r != anchor.ref, r))


_PASSIVE_SUPPORT_ROLES = {
    "resistor", "capacitor", "decoupling", "series", "pullup_pulldown",
    "inductor", "ferrite", "testpoint", "esd_protection",
}

class ConnectivityGraph:
    """Lightweight component/pin/net connectivity graph for topology queries.

    Built directly from parsed components and nets, this answers questions
    such as "what is this resistor between?" and "what is the owning signal
    source for this pullup?" so that primitive inference can validate
    topology instead of relying on coarse shared-net heuristics alone.
    """

    def __init__(self, components: Mapping[str, PlanComponent], nets: Mapping[str, PlanNet]) -> None:
        self.components = components
        self.nets = nets

    def net_members(self, net: str) -> List[Tuple[str, str]]:
        return list(self.nets.get(net, PlanNet(net)).pads)

    def peers(self, ref: str, net: str, exclude_roles: Optional[set] = None) -> List[str]:
        """Other components on `net`, optionally excluding certain roles."""
        exclude_roles = exclude_roles or set()
        result: List[str] = []
        for r, _pin in self.net_members(net):
            if r == ref or r not in self.components:
                continue
            if self.components[r].role in exclude_roles:
                continue
            if r not in result:
                result.append(r)
        return result

    def series_endpoints(self, ref: str) -> Optional[Tuple[str, str]]:
        """Return (a, b) if ref is a true series element A -> ref -> B, else None.

        Requires exactly two non-ground nets, each of which connects ref to
        exactly one *other* non-passive component. Shared-net relationships
        with other passives (e.g. two decoupling capacitors on the same
        power/ground nets) are not sufficient to infer a series relationship.
        """
        comp = self.components.get(ref)
        if comp is None:
            return None
        non_ground = [n for n in comp.nets if not is_ground(n)]
        if len(non_ground) != 2:
            return None
        endpoints: List[str] = []
        for net in non_ground:
            real = set(self.peers(ref, net, exclude_roles=_PASSIVE_SUPPORT_ROLES))
            if len(real) != 1:
                return None
            endpoints.append(next(iter(real)))
        if endpoints[0] == endpoints[1]:
            return None
        return endpoints[0], endpoints[1]

    def pullup_owner(self, ref: str, signal_net: Optional[str]) -> Optional[PlanComponent]:
        """Return the component that owns the signal a pullup/pulldown biases.

        Ownership preference: connectors (the signal leaves the board) first,
        then the MCU/controller, then other ICs/transceivers/peripherals.
        """
        if not signal_net:
            return None
        priority = {"high_speed_connector": 0, "connector": 0, "mcu": 1, "hdmi_retimer": 2, "rf_module": 2, "ic": 3, "power_regulator": 4}
        candidates = [self.components[r] for r in self.peers(ref, signal_net, exclude_roles=_PASSIVE_SUPPORT_ROLES)]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (priority.get(c.role, 9), c.ref))
        return candidates[0]


_SEMANTIC_CLUSTER_HINTS: Tuple[Tuple[str, str], ...] = (
    ("ESP32", "ESP32 cluster"),
    ("HDMI", "HDMI cluster"),
    ("TMDS", "HDMI cluster"),
    ("USB", "USB cluster"),
    ("WIFI", "RF cluster"),
)


def _cluster_category(comp: PlanComponent) -> str:
    """Human-readable semantic category for a cluster anchored on `comp`."""
    haystack = f"{comp.footprint.upper()} {(comp.value or '').upper()} {' '.join(comp.nets).upper()}"
    for token, label in _SEMANTIC_CLUSTER_HINTS:
        if token in haystack:
            return label
    if comp.role == "mcu":
        return "MCU cluster"
    if comp.role == "power_regulator":
        return "power cluster"
    if comp.role == "rf_module":
        return "RF cluster"
    if comp.role == "hdmi_retimer":
        return "HDMI cluster"
    if "connector" in comp.role:
        return "connector cluster"
    return f"{comp.role} cluster"


_BOUNDS_RULE_KINDS = {"region", "keepout", "corner", "fixed"}
_XY_RE = re.compile(r"\bx=(-?[0-9.eE+-]+).*?\by=(-?[0-9.eE+-]+)")
_WH_RE = re.compile(r"\bw=(-?[0-9.eE+-]+).*?\bh=(-?[0-9.eE+-]+)")


def _validate_board_bounds(rules: Sequence[PlanRule], board: BoardGeometry) -> List[str]:
    """Flag region/keepout/corner/fixed rules whose coordinates fall outside the board."""
    out: List[str] = []
    for rule in rules:
        if rule.kind not in _BOUNDS_RULE_KINDS:
            continue
        m = _XY_RE.search(rule.text)
        if not m:
            continue
        x, y = float(m.group(1)), float(m.group(2))
        w = h = 0.0
        wm = _WH_RE.search(rule.text)
        if wm:
            w, h = float(wm.group(1)), float(wm.group(2))
        if x < -1e-6 or y < -1e-6 or x + w > board.width + 1e-6 or y + h > board.height + 1e-6:
            out.append(f"{rule.kind} rule is outside board bounds ({board.width:g} x {board.height:g}): {rule.text}")
    return out


_REFINEMENT_RULE_KINDS = {
    "fixed", "corner", "satellite", "nearpad", "decoupling", "decoupling_array",
    "pullup", "pullup_array", "series", "between", "esd",
}


def _validate_rules(rules: Sequence[PlanRule], components: Mapping[str, PlanComponent]) -> Tuple[List[str], List[str]]:
    """Detect duplicate/conflicting rules and impossible topology references.

    Returns (duplicate_rule_descriptions, topology_problem_descriptions).
    """
    duplicates: List[str] = []
    problems: List[str] = []
    seen_texts: set = set()
    primary_owner: Dict[str, str] = {}
    for rule in rules:
        if rule.text in seen_texts:
            duplicates.append(f"duplicate placement rule text: {rule.text}")
        seen_texts.add(rule.text)
        if rule.kind in {"between", "series", "esd"} and len(rule.refs) >= 3:
            a_ref, b_ref = rule.refs[1], rule.refs[2]
            if a_ref == b_ref or a_ref not in components or b_ref not in components:
                problems.append(f"{rule.kind} rule for {rule.refs[0]} has an impossible Between() location: a={a_ref!r}, b={b_ref!r}")
        if rule.kind in {"nearpad", "decoupling", "pullup"} and len(rule.refs) >= 2:
            parent_ref = rule.refs[1]
            if parent_ref not in components:
                problems.append(f"{rule.kind} rule for {rule.refs[0]} has an impossible NearPad() location: parent {parent_ref!r} is missing")
        if rule.kind not in _REFINEMENT_RULE_KINDS or not rule.refs:
            continue
        targets = rule.refs if rule.kind in {"decoupling_array", "pullup_array"} else [rule.refs[0]]
        for ref in targets:
            existing = primary_owner.get(ref)
            if existing and existing != rule.kind:
                duplicates.append(f"{ref}: duplicate primary placement ownership from {existing} and {rule.kind} rules")
            else:
                primary_owner.setdefault(ref, rule.kind)
    return duplicates, problems


_EDGE_CONNECTOR_TOKENS = (
    "HDMI", "USB", "RJ45", "ETHERNET", "8P8C", "JACK", "BARREL", "TYPEC", "TYPE-C",
    "TYPE_C", "MICROSD", "SD_CARD", "DSUB", "D-SUB", "DC_IN", "POWERJACK",
)

_VIN_NET_RE = re.compile(r"(VIN|VBUS|VBAT|VDC|DCIN|DC_IN|12V|24V|V_IN)", re.I)
_SW_NET_RE = re.compile(r"^(SW|LX|PH|SWITCH)", re.I)
_FB_NET_RE = re.compile(r"(FB|FEEDBACK|ADJ|VSNS|SENSE)", re.I)

_PROTECTED_IC_PRIORITY = {"hdmi_retimer": 0, "ic": 1, "mcu": 2, "rf_module": 3}


def detect_functional_paths(components: Mapping[str, PlanComponent], nets: Mapping[str, PlanNet],
                            intent: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build functional signal paths (connector -> protection -> IC) from connectivity.

    Explicit board.pln `functional_paths:` entries win; high-speed connector
    paths are inferred from shared high-speed nets otherwise. Placement is
    derived from these paths, never from component type alone.
    """

    paths: Dict[str, Dict[str, Any]] = {}
    for name, spec in _as_mapping(intent.get("functional_paths")).items():
        spec = _as_mapping(spec)
        sequence = [str(r) for r in _as_list(spec.get("sequence"))]
        if len(sequence) < 2:
            continue
        paths[str(name)] = {
            "type": str(spec.get("type", "custom")),
            "sequence": sequence,
            "corridor_width_mm": float(spec.get("corridor_width_mm", 3.0)) if _is_number(spec.get("corridor_width_mm")) else 3.0,
            "protect_first": bool(spec.get("protect_first", True)),
            "source": "board.pln",
        }
    explicit_starts = {p["sequence"][0] for p in paths.values()}
    for comp in sorted(components.values(), key=lambda c: c.ref):
        if "connector" not in comp.role or comp.ref in explicit_starts:
            continue
        hs_nets = {n for n in comp.nets if is_high_speed(n)}
        if not hs_nets:
            continue
        protection = sorted(c.ref for c in components.values()
                            if c.role in {"esd_protection", "common_mode_choke"} and set(c.nets) & hs_nets)
        ics = sorted((c for c in components.values()
                      if c.role in _PROTECTED_IC_PRIORITY and set(c.nets) & hs_nets),
                     key=lambda c: (_PROTECTED_IC_PRIORITY[c.role], c.ref))
        if not ics:
            continue
        paths[f"HS_{comp.ref}"] = {
            "type": "high_speed_diff",
            "sequence": [comp.ref] + protection + [ics[0].ref],
            "corridor_width_mm": 3.0,
            "protect_first": True,
            "source": "inferred_from_connectivity",
        }
    return paths


def detect_power_islands(components: Mapping[str, PlanComponent], nets: Mapping[str, PlanNet],
                         intent: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Build topology-aware power islands (regulator + hot-loop parts), not power piles.

    A capacitor only joins an island when its nearest power-net parent IC is
    the regulator itself; downstream load decouplers stay owned by their loads.
    """

    explicit: Dict[str, Dict[str, Any]] = {}
    for name, spec in _as_mapping(intent.get("power_islands")).items():
        spec = _as_mapping(spec)
        regulator = spec.get("regulator")
        if regulator is None:
            continue
        explicit[str(regulator)] = {
            "name": str(name),
            "regulator": str(regulator),
            "input_caps": [str(r) for r in _as_list(spec.get("input_caps"))],
            "inductor": str(spec["inductor"]) if spec.get("inductor") else None,
            "output_caps": [str(r) for r in _as_list(spec.get("output_caps"))],
            "feedback": [str(r) for r in _as_list(spec.get("feedback"))],
            "switch_net": spec.get("switch_node") or spec.get("switch_net"),
            "source": "board.pln",
        }

    ic_candidates = [c for c in components.values()
                     if c.role in {"ic", "mcu", "hdmi_retimer", "rf_module", "power_regulator"}]
    islands: List[Dict[str, Any]] = []
    for reg in sorted((c for c in components.values() if c.role == "power_regulator"), key=lambda c: c.ref):
        if reg.ref in explicit:
            islands.append(explicit.pop(reg.ref))
            continue
        power_nets = {n for n in reg.nets if is_power(n)}
        input_nets = {n for n in power_nets if _VIN_NET_RE.search(n)}
        sw_nets = {n for n in reg.nets if not is_power(n) and not is_ground(n) and _SW_NET_RE.match(n)}
        inductor = None
        output_nets: set[str] = set()
        for ind in sorted((c for c in components.values() if c.role == "inductor"), key=lambda c: c.ref):
            shared = set(ind.nets) & set(reg.nets)
            if not shared:
                continue
            inductor = ind
            sw_nets |= {n for n in shared if not is_power(n) and not is_ground(n)}
            output_nets |= {n for n in ind.nets if is_power(n)}
            break
        if not output_nets:
            output_nets = power_nets - input_nets
        if not input_nets:
            input_nets = power_nets - output_nets

        def island_caps(target_nets: set[str]) -> List[str]:
            # A cap joins the island only when the regulator/inductor is its
            # closest plausible owner; caps nearer a downstream IC stay load
            # decouplers owned by that load.
            anchors = [reg] + ([inductor] if inductor is not None else [])
            out: List[str] = []
            for cap in components.values():
                if cap.role not in {"decoupling", "capacitor"}:
                    continue
                cap_power = set(cap.nets) & target_nets
                if not cap_power or not any(is_ground(n) for n in cap.nets):
                    continue
                parent = _nearest_parent(cap, ic_candidates, cap_power)
                if parent is not None and parent.ref == reg.ref:
                    out.append(cap.ref)
                    continue
                anchor_dist = min(math.hypot(cap.x - a.x, cap.y - a.y) for a in anchors)
                parent_dist = math.hypot(cap.x - parent.x, cap.y - parent.y) if parent is not None else float("inf")
                if anchor_dist < parent_dist:
                    out.append(cap.ref)
            return sorted(out)

        input_caps = island_caps(input_nets)
        output_caps = [r for r in island_caps(output_nets) if r not in input_caps]
        fb_nets = {n for n in reg.nets if not is_power(n) and not is_ground(n) and _FB_NET_RE.search(n)}
        feedback = sorted(c.ref for c in components.values()
                          if c.role in {"pullup_pulldown", "resistor", "series"} and set(c.nets) & fb_nets)
        if inductor is None and not input_caps and not output_caps and not feedback:
            continue
        islands.append({
            "name": f"PWR_{reg.ref}",
            "regulator": reg.ref,
            "input_caps": input_caps,
            "inductor": None if inductor is None else inductor.ref,
            "output_caps": output_caps,
            "feedback": feedback,
            "switch_net": sorted(sw_nets)[0] if sw_nets else None,
            "input_nets": sorted(input_nets),
            "output_nets": sorted(output_nets),
            "source": "inferred_from_connectivity",
        })
    # Explicit islands whose regulator was not detected as a regulator still apply.
    islands.extend(explicit[k] for k in sorted(explicit))
    return islands


_CORNER_ORDER = ("top_left", "top_right", "bottom_left", "bottom_right")


def _corner_distances(board: BoardGeometry, comp: PlanComponent) -> Dict[str, float]:
    lx = comp.x - board.origin_x
    ly = comp.y - board.origin_y
    return {
        "top_left": math.hypot(lx, ly),
        "top_right": math.hypot(board.width - lx, ly),
        "bottom_left": math.hypot(lx, board.height - ly),
        "bottom_right": math.hypot(board.width - lx, board.height - ly),
    }


def plan_mounting_holes(board: BoardGeometry, components: Mapping[str, PlanComponent],
                        intent: Mapping[str, Any], warnings: List[str]) -> List[Dict[str, Any]]:
    """Assign mounting holes to distinct corners or explicit board.pln locations.

    Mounting holes are fixed mechanical objects, never clusters. board.pln
    `mechanical.mounting_holes` wins; otherwise holes already sitting at
    distinct corners keep their positions, and clustered/colocated holes are
    redistributed so no two holes share a corner.
    """

    mech_cfg = _as_mapping(intent.get("mechanical"))
    holes_cfg = {str(k): _as_mapping(v) for k, v in _as_mapping(mech_cfg.get("mounting_holes")).items()}
    fixed = intent.get("fixed") if isinstance(intent.get("fixed"), dict) else {}
    holes = sorted((c for c in components.values() if c.role == "mechanical" and c.ref not in fixed),
                   key=lambda c: c.ref)
    if not holes:
        return []

    assignments: List[Dict[str, Any]] = []
    used_corners: set[str] = set()
    remaining: List[PlanComponent] = []
    for comp in holes:
        spec = holes_cfg.get(comp.ref)
        if spec and spec.get("corner"):
            corner = str(spec["corner"])
            assignments.append({"ref": comp.ref, "kind": "corner", "corner": corner,
                                "inset": float(spec.get("inset", 3)) if _is_number(spec.get("inset", 3)) else 3.0,
                                "source": "board.pln"})
            used_corners.add(corner)
        elif spec and _is_number(spec.get("x")) and _is_number(spec.get("y")):
            assignments.append({"ref": comp.ref, "kind": "anchor", "x": float(spec["x"]), "y": float(spec["y"]),
                                "source": "board.pln"})
        else:
            remaining.append(comp)

    # Keep existing positions when the holes are already distributed to
    # distinct corners; otherwise redistribute (the classic failure is all
    # holes colocated or piled in one area by a type-grouping pass).
    nearest: Dict[str, str] = {}
    for comp in remaining:
        distances = _corner_distances(board, comp)
        nearest[comp.ref] = min(distances, key=distances.get)
    distinct = len(set(nearest.values())) == len(remaining) and not (set(nearest.values()) & used_corners)
    span = max(1e-9, min(board.width, board.height))
    near_their_corners = all(_corner_distances(board, comp)[nearest[comp.ref]] <= 0.45 * span for comp in remaining)
    if remaining and distinct and near_their_corners:
        for comp in remaining:
            assignments.append({"ref": comp.ref, "kind": "anchor",
                                "x": comp.x - board.origin_x, "y": comp.y - board.origin_y,
                                "rot": comp.rot, "corner": nearest[comp.ref],
                                "source": "existing_distinct_corners"})
        return assignments

    for comp in remaining:
        ordered = sorted(_corner_distances(board, comp).items(), key=lambda kv: kv[1])
        corner = next((c for c, _d in ordered if c not in used_corners), None)
        if corner is None:
            assignments.append({"ref": comp.ref, "kind": "anchor",
                                "x": comp.x - board.origin_x, "y": comp.y - board.origin_y,
                                "rot": comp.rot, "source": "no_free_corner"})
            warnings.append(f"{comp.ref}: more mounting holes than corners; kept at existing location. "
                            "Define mechanical.mounting_holes in board.pln for explicit placement.")
            continue
        used_corners.add(corner)
        assignments.append({"ref": comp.ref, "kind": "corner", "corner": corner, "inset": 3.0,
                            "source": "distributed_to_distinct_corner"})
        warnings.append(f"{comp.ref}: mounting hole distributed to {corner} corner (inset 3 mm); "
                        "review against the mechanical design.")
    return assignments


# Final placement ownership priority. Lower rank wins. Groups/clusters are
# metadata: a component may belong to several semantic groups, but exactly one
# rule owns its final placement.
_OWNERSHIP_RANK: Dict[str, int] = {
    "corner": 0, "fixed": 0,                      # explicit/locked mechanical
    "edge": 1,                                     # edge-required connector
    "anchor": 2,                                   # explicit board.pln anchor
    "esd": 3, "between": 3, "series": 3,           # functional path placement
    "decoupling": 4, "decoupling_array": 4, "nearpad": 4,
    "pullup": 5, "pullup_array": 5,
    "satellite": 6,
    "cluster": 9,                                  # coarse fallback only
}


def compute_ownership(rules: Sequence[PlanRule], components: Mapping[str, PlanComponent]) -> Dict[str, Dict[str, Any]]:
    ownership: Dict[str, Dict[str, Any]] = {}
    for rule in rules:
        rank = _OWNERSHIP_RANK.get(rule.kind)
        if rank is None or not rule.refs:
            continue
        if rule.kind == "cluster":
            targets = list(rule.refs)
            anchor_rank = 1 if "edge_required=True" in rule.text else rank
        elif rule.kind in {"decoupling_array", "pullup_array"}:
            targets = list(rule.refs)
            anchor_rank = rank
        else:
            targets = rule.refs[:1]
            anchor_rank = rank
        for i, ref in enumerate(targets):
            if ref not in components:
                continue
            effective = anchor_rank if (rule.kind != "cluster" or i == 0) else rank
            current = ownership.get(ref)
            if current is None or effective < current["rank"]:
                kind = rule.kind
                if rule.kind == "cluster" and i == 0 and effective == 1:
                    kind = "edge_required_connector"
                ownership[ref] = {"owner": kind, "rank": effective, "rule": rule.text}
    return ownership


def compute_semantic_groups(clusters: Sequence[Mapping[str, Any]],
                            functional_paths: Mapping[str, Mapping[str, Any]],
                            power_islands: Sequence[Mapping[str, Any]],
                            decoupling_groups: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """Semantic group membership per ref. Groups are metadata, not placement units."""

    groups: Dict[str, List[str]] = {}

    def add(ref: str, group: str) -> None:
        groups.setdefault(ref, [])
        if group not in groups[ref]:
            groups[ref].append(group)

    for cluster in clusters:
        for ref in cluster.get("members", []):
            add(str(ref), str(cluster.get("name")))
    for name, path in functional_paths.items():
        for ref in path.get("sequence", []):
            add(str(ref), f"path:{name}")
    for island in power_islands:
        members = [island.get("regulator"), island.get("inductor")] + \
            list(island.get("input_caps", [])) + list(island.get("output_caps", [])) + \
            list(island.get("feedback", []))
        for ref in members:
            if ref:
                add(str(ref), f"power:{island.get('name')}")
    for group in decoupling_groups:
        for ref in group.get("members", []):
            add(str(ref), f"decoupling:{group.get('parent')}")
    return groups


# ---------------------------------------------------------------------------
# Constraint-driven floorplanner
#
# The planner does not copy the (often un-placed) source coordinates into the
# plan.  Instead it derives a real floorplan from connectivity and footprint
# geometry using SI/EMI best practices:
#
#   * I/O and edge connectors are distributed along their access edge so they
#     never stack, and ICs are pulled toward the board core.
#   * IC positions are solved with a deterministic force-directed model whose
#     attractive forces are pin-to-pin springs taken from the netlist and the
#     real pad coordinates of each footprint.  High-speed/differential nets are
#     weighted heavily so their endpoints sit close together (short, direct
#     traces); power feeds pull moderately; ground/plane nets are ignored
#     because they return on copper pours, not point-to-point traces.
#   * Repulsion enforces class-aware clearance plus extra isolation for RF
#     modules and switching regulators, so noisy and sensitive blocks are
#     spaced apart rather than crammed together.
#   * Weak region springs honor topology intent (regulators toward the POWER
#     region, RF toward its reserved strip), and ICs are kept inside a core
#     rectangle inset from the connector band.
# ---------------------------------------------------------------------------

# Pin-to-pin spring weights by net class (higher = shorter, more direct).
_FP_WEIGHT_HIGH_SPEED = 6.0
_FP_WEIGHT_CONTROL = 2.0
_FP_WEIGHT_POWER = 1.0
# Nets wider than this (rails, floods, big enables) are returned on planes and
# would otherwise dominate the spring model, so they are skipped for pull.
_FP_MAX_NET_FANOUT = 16
# Extra isolation clearance (mm) added on top of class clearance.
_FP_ISOLATION_RF = 4.0
_FP_ISOLATION_REGULATOR = 2.0
# Baseline breathing room added to every IC pair so the core is not packed
# shoulder-to-shoulder (helps routing/return paths and rework).
_FP_SPREAD_MARGIN = 1.2
_FP_CORE_ROLES = {"ic", "mcu", "hdmi_retimer", "rf_module", "power_regulator"}


@dataclasses.dataclass
class FloorplanResult:
    """Floorplan decisions consumed by the rule emitters."""

    edge_connectors: Dict[str, Tuple[str, float]]          # ref -> (access_side, along)
    core_xy: Dict[str, Tuple[float, float]]                # ref -> (local_x, local_y)
    notes: List[str]
    wirelength_before: float
    wirelength_after: float
    rf_keepouts: Dict[str, Dict[str, float]]               # rf ref -> board-local keepout rect


def _fp_net_weight(name: str) -> float:
    if is_ground(name):
        return 0.0
    if is_high_speed(name):
        return _FP_WEIGHT_HIGH_SPEED
    if is_power(name):
        return _FP_WEIGHT_POWER
    return _FP_WEIGHT_CONTROL


def _fp_component_extent(comp: PlanComponent) -> Tuple[float, float]:
    if comp.bbox is not None:
        w, h = comp.bbox.width, comp.bbox.height
        if w > 0 and h > 0:
            return (w, h)
    return (2.0, 2.0)


def _fp_pad_class(role: str) -> str:
    if "connector" in role or role == "debug_header":
        return "connector"
    return "ic"


def _fp_pair_clearance(role_a: str, role_b: str, spacing: Mapping[str, float]) -> float:
    ca, cb = _fp_pad_class(role_a), _fp_pad_class(role_b)
    if "connector" in (ca, cb):
        base = float(spacing.get("connector_to_component", 1.0))
    else:
        base = float(spacing.get("ic_to_ic", 0.75))
    base += _FP_SPREAD_MARGIN
    if "rf_module" in (role_a, role_b):
        base += _FP_ISOLATION_RF
    if role_a == "power_regulator" and role_b == "power_regulator":
        base += _FP_ISOLATION_REGULATOR
    return base


def _fp_connector_edge_decision(comp: PlanComponent, board: BoardGeometry,
                                intent: Mapping[str, Any]) -> Optional[Tuple[str, bool, bool]]:
    """Return (access_side, edge_required, near_edge) when a connector is edge-placed."""

    cfg = _component_intent(intent, comp.ref)
    local_x = max(0.0, min(board.width, comp.x - board.origin_x))
    local_y = max(0.0, min(board.height, comp.y - board.origin_y))
    distances = {"left": local_x, "right": board.width - local_x,
                 "top": local_y, "bottom": board.height - local_y}
    nearest_edge = min(distances, key=distances.get)
    edge_span = board.width if nearest_edge in ("left", "right") else board.height
    inset = 2.0
    near_edge = edge_span > 0 and distances[nearest_edge] / edge_span <= 0.25 and distances[nearest_edge] > inset
    haystack = f"{comp.footprint.upper()} {(comp.value or '').upper()}"
    inferred_edge_required = (comp.role == "high_speed_connector" or
                              any(token in haystack for token in _EDGE_CONNECTOR_TOKENS))
    if "edge_required" in cfg:
        edge_required = bool(cfg.get("edge_required"))
    elif cfg.get("access_side") is not None:
        edge_required = True
    elif "connector" in comp.role:
        edge_required = near_edge or inferred_edge_required
    else:
        edge_required = False
    if not (edge_required or near_edge):
        return None
    access_side = str(cfg.get("access_side") or nearest_edge)
    return access_side, edge_required, near_edge


def compute_floorplan(board: BoardGeometry, components: Mapping[str, PlanComponent],
                      nets: Mapping[str, PlanNet], intent: Mapping[str, Any],
                      regions: Mapping[str, Mapping[str, Any]],
                      mounting_hole_plan: Sequence[Mapping[str, Any]],
                      spacing: Mapping[str, float],
                      keepouts: Optional[Sequence[Mapping[str, Any]]] = None) -> FloorplanResult:
    notes: List[str] = []
    W, H = board.width, board.height
    ox, oy = board.origin_x, board.origin_y
    cx_board, cy_board = ox + W / 2.0, oy + H / 2.0

    # --- pad offsets (pin geometry) and a fast net -> pads index -------------
    pad_off: Dict[str, Dict[str, Tuple[float, float]]] = {}
    for ref, comp in components.items():
        table: Dict[str, Tuple[float, float]] = {}
        for pad in comp.pads:
            table[pad.number] = (pad.abs_x - comp.x, pad.abs_y - comp.y)
        pad_off[ref] = table

    # --- classify -----------------------------------------------------------
    edge_conn: Dict[str, Tuple[str, bool, bool]] = {}
    for comp in components.values():
        if "connector" not in comp.role and comp.role != "debug_header":
            continue
        decision = _fp_connector_edge_decision(comp, board, intent)
        if decision is not None:
            edge_conn[comp.ref] = decision

    core_refs = sorted(
        [c.ref for c in components.values()
         if c.role in _FP_CORE_ROLES or
         (("connector" in c.role or c.role == "debug_header") and c.ref not in edge_conn)],
        key=lambda r: r)

    # --- assign connectors to edges, spilling to other edges on overflow ----
    edge_along_abs: Dict[str, Tuple[str, float]] = {}        # ref -> (side, along local)
    fixed_pos: Dict[str, Tuple[float, float]] = {}           # abs anchor positions
    _gap = float(spacing.get("connector_to_component", 1.0))
    _all_edges = ("left", "right", "top", "bottom")

    # Corner mounting holes eat into both edges that meet at their corner, so
    # connectors must start beyond the hole's reach (inset + hole half-extent).
    _corner_reserve = 0.0
    for assignment in mounting_hole_plan:
        ref = assignment.get("ref")
        if assignment.get("kind") != "corner" or ref not in components:
            continue
        hw, hh = _fp_component_extent(components[ref])
        _corner_reserve = max(_corner_reserve,
                              float(assignment.get("inset", 3.0)) + 0.5 * max(hw, hh) + _gap)

    def _edge_span(side: str) -> float:
        # Connectors on a vertical (left/right) edge are distributed along y, so
        # the available length is the board height; horizontal edges use width.
        return H if side in ("left", "right") else W

    def _edge_margin(side: str) -> float:
        span = _edge_span(side)
        return min(max(max(0.1 * span, 8.0), _corner_reserve), 0.45 * span)

    def _edge_usable(side: str) -> float:
        return max(_edge_span(side) - 2.0 * _edge_margin(side), 1e-6)

    # Each connector needs roughly its footprint extent along the edge plus a
    # clearance gap.  Forced connectors (explicit access_side in intent) may not
    # be relocated; inferred ones spill to the emptiest alternate edge.
    pitch_need: Dict[str, float] = {}
    forced_side: Dict[str, bool] = {}
    for ref, (side, _req, _near) in edge_conn.items():
        w, h = _fp_component_extent(components[ref])
        pitch_need[ref] = max(w, h) + _gap
        forced_side[ref] = _component_intent(intent, ref).get("access_side") is not None

    assign: Dict[str, List[str]] = {e: [] for e in _all_edges}
    for ref, (side, _req, _near) in sorted(edge_conn.items()):
        assign[side].append(ref)

    def _load(side: str) -> float:
        return sum(pitch_need[r] for r in assign[side])

    progressed = True
    while progressed:
        progressed = False
        for side in _all_edges:
            usable = _edge_usable(side)
            movable = [r for r in assign[side] if not forced_side[r]]
            while _load(side) > usable and movable:
                victim = max(movable, key=lambda r: pitch_need[r])
                targets = sorted((t for t in _all_edges if t != side),
                                 key=lambda t: _edge_usable(t) - _load(t), reverse=True)
                relocated = False
                for t in targets:
                    if _load(t) + pitch_need[victim] <= _edge_usable(t):
                        assign[side].remove(victim)
                        assign[t].append(victim)
                        notes.append(f"connector {victim} spilled from {side} to {t} edge "
                                     "(preferred edge over capacity)")
                        progressed = True
                        relocated = True
                        break
                movable = [r for r in assign[side] if not forced_side[r]]
                if not relocated:
                    break  # no edge has room; leave it and pack tightly

    # --- distribute connectors along their (possibly reassigned) edges -------
    for side in _all_edges:
        refs = assign[side]
        if not refs:
            continue
        span, margin, usable = _edge_span(side), _edge_margin(side), _edge_usable(side)

        def _orig_along(r: str) -> float:
            comp = components[r]
            lx = max(0.0, min(W, comp.x - ox))
            ly = max(0.0, min(H, comp.y - oy))
            return ly if side in ("left", "right") else lx

        refs.sort(key=lambda r: (_orig_along(r), r))
        n = len(refs)
        max_extent = max((max(_fp_component_extent(components[r])) for r in refs), default=0.0)
        # The first/last connector's body (center +/- half its extent) must also
        # clear the corner reserve, so push the end margin out by that half.
        end_margin = max(margin, _corner_reserve + max_extent / 2.0)
        usable = max(span - 2.0 * end_margin, 1e-6)
        pitch = max(usable / n, max_extent + _gap)
        total = pitch * n
        start = (span / 2.0) - total / 2.0 + pitch / 2.0
        margin = end_margin
        for i, r in enumerate(refs):
            along = _clamp(start + i * pitch, margin, span - margin)
            edge_along_abs[r] = (side, along)
            if side == "left":
                fixed_pos[r] = (ox, oy + along)
            elif side == "right":
                fixed_pos[r] = (ox + W, oy + along)
            elif side == "top":
                fixed_pos[r] = (ox + along, oy)
            else:  # bottom
                fixed_pos[r] = (ox + along, oy + H)
        if n > 1:
            notes.append(f"distributed {n} connector(s) along {side} edge with {pitch:.2f} mm pitch")

    # mounting holes are fixed obstacles at their corners.
    for assignment in mounting_hole_plan:
        ref = assignment.get("ref")
        if not ref or ref not in components:
            continue
        if assignment.get("kind") == "corner":
            inset = float(assignment.get("inset", 3.0))
            corner = assignment.get("corner", "top_left")
            fx = ox + (W - inset if "right" in corner else inset)
            fy = oy + (H - inset if "bottom" in corner else inset)
            fixed_pos[ref] = (fx, fy)
        elif assignment.get("x") is not None:
            fixed_pos[ref] = (ox + float(assignment["x"]), oy + float(assignment["y"]))

    # --- region springs (topology intent) -----------------------------------
    def _region_center(name: Optional[str]) -> Optional[Tuple[float, float]]:
        if not name or name not in regions:
            return None
        r = regions[name]
        return (ox + float(r.get("x", 0)) + float(r.get("w", 0)) / 2.0,
                oy + float(r.get("y", 0)) + float(r.get("h", 0)) / 2.0)

    region_target: Dict[str, Tuple[float, float]] = {}
    for ref in core_refs:
        role = components[ref].role
        name = ("POWER" if role == "power_regulator" else
                "RF" if role == "rf_module" else
                "HIGH_SPEED" if role == "hdmi_retimer" else
                "CONTROL")
        center = _region_center(name) or _region_center("CONTROL")
        if center is not None:
            region_target[ref] = center

    # --- core rectangle (keep ICs off the connector band) -------------------
    core_inset = min(max(0.12 * min(W, H), 8.0), 22.0)
    core_min_x, core_max_x = ox + core_inset, ox + W - core_inset
    core_min_y, core_max_y = oy + core_inset, oy + H - core_inset

    # Reserve a halo around each IC for the support parts (decoupling caps,
    # pullups, series elements) the executor will pack against its pads.  Each
    # passive is assigned to the IC it shares the most signals with; without
    # this room the executor would reflow the IC itself to fit its array.
    ic_nets = {ref: set(components[ref].nets) for ref in core_refs}
    support_count: Dict[str, int] = {ref: 0 for ref in core_refs}
    for comp in components.values():
        if comp.ref in support_count or "connector" in comp.role or comp.role in {"mechanical", "debug_header"}:
            continue
        best, best_shared = None, 0
        for ref in core_refs:
            shared = len(ic_nets[ref] & set(comp.nets))
            if shared > best_shared:
                best, best_shared = ref, shared
        if best is not None:
            support_count[best] += 1

    # Two extent maps.  ``extent_hard`` is the real footprint that pcb-place
    # validates -- it must never overlap, so it drives clamping and the final
    # legalization.  ``extent_soft`` adds a support halo so, when the board has
    # room, ICs are spread far enough apart for the executor to pack their
    # decoupling/pullup arrays without reflowing the IC; on a tight board the
    # soft pass simply can't fully separate and the hard pass takes over.
    extent_hard: Dict[str, Tuple[float, float]] = {}
    extent_soft: Dict[str, Tuple[float, float]] = {}
    for ref in core_refs:
        w, h = _fp_component_extent(components[ref])
        extent_hard[ref] = (w, h)
        halo = min(10.0, 1.6 * math.sqrt(support_count[ref])) if support_count[ref] else 0.0
        extent_soft[ref] = (w + 2.0 * halo, h + 2.0 * halo)

    # Fixed obstacles the ICs must avoid: edge-connector bodies and mounting
    # holes.  An edge connector's footprint origin sits roughly half its body
    # inside the outline, so the body reaches inward by about its full depth; we
    # reserve an edge-aligned rectangle that deep so ICs never collide with it.
    fixed_boxes: List[Tuple[float, float, float, float, str]] = []
    for ref, (fx, fy) in fixed_pos.items():
        if ref in edge_along_abs:
            side, _along = edge_along_abs[ref]
            w, h = _fp_component_extent(components[ref])
            reach = max(w, h) + _gap + 2.0
            along_half = 0.5 * max(w, h) + _gap
            if side == "right":
                fixed_boxes.append((ox + W - reach / 2.0, fy, reach / 2.0, along_half, "connector"))
            elif side == "left":
                fixed_boxes.append((ox + reach / 2.0, fy, reach / 2.0, along_half, "connector"))
            elif side == "top":
                fixed_boxes.append((fx, oy + reach / 2.0, along_half, reach / 2.0, "connector"))
            else:  # bottom
                fixed_boxes.append((fx, oy + H - reach / 2.0, along_half, reach / 2.0, "connector"))
        else:
            fixed_boxes.append((fx, fy, 2.0, 2.0, "mechanical"))

    # Declared keepouts are hard obstacles: the solver must not pull a core part
    # into a forbidden region (strict pcb-place rejects any keepout overlap).
    for keepout in (keepouts or []):
        if not isinstance(keepout, dict):
            continue
        try:
            kx, ky = float(keepout.get("x", 0)), float(keepout.get("y", 0))
            kw, kh = float(keepout.get("w", 0)), float(keepout.get("h", 0))
        except (TypeError, ValueError):
            continue
        if kw <= 0 or kh <= 0:
            continue
        fixed_boxes.append((ox + kx + kw / 2.0, oy + ky + kh / 2.0, kw / 2.0, kh / 2.0, "keepout"))

    def _clamp_core(ref: str, x: float, y: float) -> Tuple[float, float]:
        # Clamp by the real footprint so parts can use the whole core even when
        # their soft halo would otherwise be pinned against the inset boundary.
        w, h = extent_hard[ref]
        lo_x, hi_x = core_min_x + w / 2.0, max(core_min_x + w / 2.0, core_max_x - w / 2.0)
        lo_y, hi_y = core_min_y + h / 2.0, max(core_min_y + h / 2.0, core_max_y - h / 2.0)
        return (_clamp(x, lo_x, hi_x), _clamp(y, lo_y, hi_y))

    # --- deterministic seed: centered grid ordered by ref --------------------
    pos: Dict[str, List[float]] = {}
    n_core = len(core_refs)
    if n_core:
        cols = max(1, int(math.ceil(math.sqrt(n_core))))
        rows = max(1, int(math.ceil(n_core / cols)))
        gx = (core_max_x - core_min_x) / max(1, cols)
        gy = (core_max_y - core_min_y) / max(1, rows)
        for i, ref in enumerate(core_refs):
            c, r = i % cols, i // cols
            sx = core_min_x + gx * (c + 0.5)
            sy = core_min_y + gy * (r + 0.5)
            pos[ref] = list(_clamp_core(ref, sx, sy))

    def _all_pos(ref: str) -> Optional[Tuple[float, float]]:
        if ref in pos:
            return (pos[ref][0], pos[ref][1])
        return fixed_pos.get(ref)

    # Pre-index nets that actually drive placement (small fanout, non-ground).
    spring_nets: List[Tuple[float, List[Tuple[str, str]]]] = []
    for net in nets.values():
        w = _fp_net_weight(net.name)
        if w <= 0:
            continue
        pads = [(r, p) for (r, p) in net.pads if r in components and p in pad_off.get(r, {})]
        # only nets touching at least one movable part matter
        if len(pads) < 2 or len(pads) > _FP_MAX_NET_FANOUT:
            continue
        if not any(r in pos for (r, _p) in pads):
            continue
        spring_nets.append((w / (len(pads) - 1), pads))

    def _wirelength() -> float:
        total = 0.0
        for net in nets.values():
            w = _fp_net_weight(net.name)
            if w <= 0:
                continue
            xs: List[float] = []
            ys: List[float] = []
            for (r, p) in net.pads:
                base = _all_pos(r)
                off = pad_off.get(r, {}).get(p)
                if base is None or off is None:
                    continue
                xs.append(base[0] + off[0])
                ys.append(base[1] + off[1])
            if len(xs) >= 2:
                total += w * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
        return total

    wl_before = _wirelength()

    def _resolve_overlaps(eps: float, ext: Dict[str, Tuple[float, float]],
                          boxes: Sequence[Tuple[float, float, float, float, str]],
                          movable: Sequence[str]) -> int:
        """One separation pass; pushes movable ICs apart and out of fixed bodies.

        Returns the number of pairs that were overlapping.  Separation is applied
        on the minimal-penetration axis; when that axis is pinned by the core
        boundary the orthogonal axis (which has room on a large board) takes the
        remaining push on subsequent passes.
        """

        moved = 0
        for a_i, a in enumerate(movable):
            wa, ha = ext[a]
            # movable vs movable
            for b in movable[a_i + 1:]:
                wb, hb = ext[b]
                clr = _fp_pair_clearance(components[a].role, components[b].role, spacing)
                dx = pos[b][0] - pos[a][0]
                dy = pos[b][1] - pos[a][1]
                pen_x = (wa + wb) / 2.0 + clr - abs(dx)
                pen_y = (ha + hb) / 2.0 + clr - abs(dy)
                if pen_x > eps and pen_y > eps:
                    if pen_x <= pen_y:
                        push = (pen_x / 2.0 + 1e-4) * (1.0 if dx >= 0 else -1.0)
                        pos[a][0] -= push
                        pos[b][0] += push
                    else:
                        push = (pen_y / 2.0 + 1e-4) * (1.0 if dy >= 0 else -1.0)
                        pos[a][1] -= push
                        pos[b][1] += push
                    pos[a][0], pos[a][1] = _clamp_core(a, pos[a][0], pos[a][1])
                    pos[b][0], pos[b][1] = _clamp_core(b, pos[b][0], pos[b][1])
                    moved += 1
            # movable vs fixed obstacle (connector body / mounting hole / keepout)
            for (fx, fy, fhw, fhh, frole) in boxes:
                # A keepout is a hard no-overlap zone; any small gap satisfies it,
                # so do not pile on the IC spread/isolation margins here.
                clr = (float(spacing.get("default", 0.25)) if frole == "keepout"
                       else _fp_pair_clearance(components[a].role, frole, spacing))
                dx = fx - pos[a][0]
                dy = fy - pos[a][1]
                pen_x = wa / 2.0 + fhw + clr - abs(dx)
                pen_y = ha / 2.0 + fhh + clr - abs(dy)
                if pen_x > eps and pen_y > eps:
                    if pen_x <= pen_y:
                        pos[a][0] -= (pen_x + 1e-4) * (1.0 if dx >= 0 else -1.0)
                    else:
                        pos[a][1] -= (pen_y + 1e-4) * (1.0 if dy >= 0 else -1.0)
                    pos[a][0], pos[a][1] = _clamp_core(a, pos[a][0], pos[a][1])
                    moved += 1
        return moved

    # --- force-directed solve ------------------------------------------------
    iterations = 600
    for it in range(iterations):
        cooling = 1.0 - (it / iterations) * 0.7
        force: Dict[str, List[float]] = {ref: [0.0, 0.0] for ref in pos}

        # 1) pin-to-pin springs toward each net's pad centroid
        for wp, pads in spring_nets:
            cx = cy = 0.0
            abspads: List[Tuple[str, float, float]] = []
            for (r, p) in pads:
                base = _all_pos(r)
                if base is None:
                    continue
                off = pad_off[r][p]
                px, py = base[0] + off[0], base[1] + off[1]
                abspads.append((r, px, py))
                cx += px
                cy += py
            if len(abspads) < 2:
                continue
            cx /= len(abspads)
            cy /= len(abspads)
            for (r, px, py) in abspads:
                if r in force:
                    force[r][0] += (cx - px) * wp
                    force[r][1] += (cy - py) * wp

        # 2) weak region + centering springs
        for ref in pos:
            tx, ty = region_target.get(ref, (cx_board, cy_board))
            force[ref][0] += (tx - pos[ref][0]) * 0.012
            force[ref][1] += (ty - pos[ref][1]) * 0.012

        # apply attractive step (damped, cooling)
        for ref in pos:
            pos[ref][0] += max(-6.0, min(6.0, force[ref][0] * 0.05 * cooling))
            pos[ref][1] += max(-6.0, min(6.0, force[ref][1] * 0.05 * cooling))
            pos[ref][0], pos[ref][1] = _clamp_core(ref, pos[ref][0], pos[ref][1])

        _resolve_overlaps(0.0, extent_soft, fixed_boxes, core_refs)

    # Soft spreading pass: use the halo extents while there is still room.
    for _ in range(800):
        if _resolve_overlaps(1e-6, extent_soft, fixed_boxes, core_refs) == 0:
            break

    # Now synthesize each RF antenna keepout at the module's solved location and
    # pin the module: the keepout becomes a hard obstacle so no other part is
    # legalized into it (a keepout overlap is a hard pcb-place failure).
    rf_keepouts: Dict[str, Dict[str, float]] = {}
    hard_boxes = list(fixed_boxes)
    rf_refs = [r for r in core_refs if components[r].role == "rf_module"]
    for ref in rf_refs:
        comp = components[ref]
        new_origin = (pos[ref][0], pos[ref][1])
        delta = (new_origin[0] - comp.x, new_origin[1] - comp.y)
        moved_comp = dataclasses.replace(
            comp, x=new_origin[0], y=new_origin[1],
            bbox=comp.bbox.translated(delta[0], delta[1]) if comp.bbox is not None else None)
        rect = _synthesized_rf_keepout(moved_comp, board)
        rf_keepouts[ref] = rect
        hard_boxes.append((ox + rect["x"] + rect["w"] / 2.0, oy + rect["y"] + rect["h"] / 2.0,
                           rect["w"] / 2.0, rect["h"] / 2.0, "keepout"))
        # The module itself is now fixed; treat its real body as an obstacle.
        rw, rh = extent_hard[ref]
        hard_boxes.append((pos[ref][0], pos[ref][1], rw / 2.0, rh / 2.0, "connector"))

    # --- final legalization: guarantee real-body + keepout clearance ---------
    movable_hard = [r for r in core_refs if r not in rf_refs]
    for _ in range(2000):
        if _resolve_overlaps(1e-6, extent_hard, hard_boxes, movable_hard) == 0:
            break

    # Decompaction fallback: pairwise relaxation can wedge parts against the core
    # boundary (common on small boards where wirelength concentrates parts).  Any
    # part still overlapping is relocated to the first genuinely free grid cell,
    # which guarantees a clearance-clean layout whenever the core has room.
    def _spot_free(ref: str, x: float, y: float) -> bool:
        wa, ha = extent_hard[ref]
        for o in movable_hard:
            if o == ref:
                continue
            wo, ho = extent_hard[o]
            clr = _fp_pair_clearance(components[ref].role, components[o].role, spacing)
            if ((wa + wo) / 2.0 + clr - abs(pos[o][0] - x) > 1e-6 and
                    (ha + ho) / 2.0 + clr - abs(pos[o][1] - y) > 1e-6):
                return False
        for (fx, fy, fhw, fhh, frole) in hard_boxes:
            clr = (float(spacing.get("default", 0.25)) if frole == "keepout"
                   else _fp_pair_clearance(components[ref].role, frole, spacing))
            if (wa / 2.0 + fhw + clr - abs(fx - x) > 1e-6 and
                    ha / 2.0 + fhh + clr - abs(fy - y) > 1e-6):
                return False
        return True

    for _ in range(3):
        if all(_spot_free(r, pos[r][0], pos[r][1]) for r in movable_hard):
            break
        for ref in movable_hard:
            if _spot_free(ref, pos[ref][0], pos[ref][1]):
                continue
            wa, ha = extent_hard[ref]
            step = max(1.5, 0.5 * min(wa, ha))
            placed = False
            gy = core_min_y + ha / 2.0
            while gy <= core_max_y - ha / 2.0 + 1e-9 and not placed:
                gx = core_min_x + wa / 2.0
                while gx <= core_max_x - wa / 2.0 + 1e-9:
                    if _spot_free(ref, gx, gy):
                        pos[ref] = [gx, gy]
                        placed = True
                        break
                    gx += step
                gy += step

    core_xy: Dict[str, Tuple[float, float]] = {}
    for ref in core_refs:
        core_xy[ref] = (pos[ref][0] - ox, pos[ref][1] - oy)

    wl_after = _wirelength()
    if wl_before > 0:
        notes.append(f"weighted pin-to-pin wirelength {wl_before:.0f} -> {wl_after:.0f} mm "
                     f"({100.0 * (wl_before - wl_after) / wl_before:.0f}% shorter)")

    edge_connectors = {ref: (side, along) for ref, (side, along) in edge_along_abs.items()}
    return FloorplanResult(edge_connectors=edge_connectors, core_xy=core_xy,
                           notes=notes, wirelength_before=wl_before, wirelength_after=wl_after,
                           rf_keepouts=rf_keepouts)


def generate_plan(board: BoardGeometry, components: Dict[str, PlanComponent], nets: Dict[str, PlanNet], aliases: Dict[str, str], alias_diagnostics: AliasDiagnostics, intent: Mapping[str, Any], warnings: List[str]) -> Plan:
    infer_roles(components, intent)
    pairs = detect_differential_pairs(nets)
    (
        stackup,
        routing,
        declared_pairs,
        net_classes,
        routing_overrides,
        simulation,
        routing_warnings,
        stackup_warnings,
        simulation_warnings,
        high_speed_constraints_complete,
    ) = validate_routing_intent(intent, nets)
    warnings.extend(routing_warnings)
    warnings.extend(stackup_warnings)
    warnings.extend(simulation_warnings)
    spacing = spacing_profile(intent, warnings)
    functional_paths = detect_functional_paths(components, nets, intent)
    power_islands = detect_power_islands(components, nets, intent)
    rules: List[PlanRule] = []
    explanations: Dict[str, Dict[str, Any]] = {}
    uncertain: List[str] = []
    topology_failures: List[str] = []

    regions: Dict[str, Dict[str, Any]] = {}
    if isinstance(intent.get("regions"), dict):
        regions = {str(k): dict(v) for k, v in intent["regions"].items() if isinstance(v, dict)}
    elif pairs or any(c.role in {"power_regulator", "rf_module"} for c in components.values()):
        h = board.height / 3.0
        regions = {
            "HIGH_SPEED": {"x": 0, "y": 0, "w": board.width, "h": h, "kind": "high_speed_corridor"},
            "CONTROL": {"x": 0, "y": h, "w": board.width, "h": h, "kind": "support"},
            "POWER": {"x": 0, "y": 2*h, "w": board.width, "h": board.height - 2*h, "kind": "power_island"},
        }
        warnings.append("Generated default HIGH_SPEED/CONTROL/POWER regions from board thirds; review before fabrication.")
    keepouts = list(intent.get("keepouts") or []) if isinstance(intent.get("keepouts"), list) else []

    # Region kinds that the floorplanner must treat as hard corridors/keepouts
    # (never reflowed); everything else defaults to a movable support region.
    _hard_region_kinds = {"high_speed_corridor", "rf_keepout", "mechanical", "antenna", "service"}
    for name, r in regions.items():
        kind = r.get("kind") or r.get("role")
        movable = r.get("movable")
        if movable is None:
            movable = (kind not in _hard_region_kinds) if kind is not None else True
        extra = ""
        if kind:
            extra += f', kind={_q(str(kind))}'
        extra += f', movable={bool(movable)}'
        rules.append(PlanRule("region",
            f'Region({_q(name)}, x={r.get("x",0)}, y={r.get("y",0)}, w={r.get("w",0)}, h={r.get("h",0)}{extra})',
            [], f"Region {name} ({kind or 'support'}, movable={bool(movable)}) from intent/default floorplan."))
    for k in keepouts:
        if isinstance(k, dict):
            rules.append(PlanRule("keepout", f'Keepout({_q(k.get("name", "KEEPOUT"))}, x={k.get("x",0)}, y={k.get("y",0)}, w={k.get("w",0)}, h={k.get("h",0)}, role={_q(k.get("role", "keepout"))})', [], f"Keepout {k.get('name')} from board intent."))

    # Fixed mechanical objects.
    fixed = intent.get("fixed") if isinstance(intent.get("fixed"), dict) else {}
    for ref, spec in fixed.items():
        if isinstance(spec, dict) and spec.get("type") == "corner":
            rules.append(PlanRule("corner", f'Corner({_q(ref)}, corner={_q(spec.get("corner", "top_left"))}, inset={spec.get("inset", 3)}, role="fixed_mechanical")', [str(ref)], f"{ref} fixed by board intent."))
            explanations.setdefault(str(ref), {})["generated_rule"] = rules[-1].text
    # Mounting holes are fixed mechanical objects placed first: distinct
    # corners or explicit board.pln locations, never clusters.
    mounting_hole_plan = plan_mounting_holes(board, components, intent, warnings)
    for assignment in mounting_hole_plan:
        ref = assignment["ref"]
        if assignment["kind"] == "corner":
            text = (f'Corner({_q(ref)}, corner={_q(assignment["corner"])}, '
                    f'inset={assignment.get("inset", 3.0):g}, role="mechanical")')
            rules.append(PlanRule("corner", text, [ref],
                                  f"{ref} mounting hole at {assignment['corner']} corner ({assignment['source']}); mechanical constraints first."))
        else:
            rot = assignment.get("rot", components[ref].rot if ref in components else 0.0)
            text = (f'Anchor({_q(ref)}, x={assignment["x"]:.3f}, y={assignment["y"]:.3f}, '
                    f'rot={rot:g}, lock=True, role="mechanical")')
            rules.append(PlanRule("fixed", text, [ref],
                                  f"{ref} mounting hole kept at explicit/existing mechanical location ({assignment['source']})."))
        explanations.setdefault(ref, {}).update({"role": "mechanical", "generated_rule": rules[-1].text,
                                                  "mounting_hole_assignment": assignment})

    # Solve the floorplan once (connector edge distribution + force-directed,
    # wirelength-driven IC placement) and feed its coordinates to the emitters.
    floorplan = compute_floorplan(board, components, nets, intent, regions,
                                  mounting_hole_plan, spacing, keepouts)
    for note in floorplan.notes:
        warnings.append(f"floorplan: {note}")

    clusters: List[Dict[str, Any]] = []
    edge_rotation_plan: List[Dict[str, Any]] = []
    # Resolve each support passive to one electrical owner so the coarse cluster
    # passes group by connectivity, not by where the source layout happened to
    # drop the part. Keeps one placement owner per ref and avoids off-board rides.
    support_owner = assign_support_owners(components, nets)
    # Connector/high-speed clusters. Edge-required connectors are mechanically
    # edge-locked, rotated by access side, and may extend their body outside
    # the board outline; their support parts are optimized around them later.
    for comp in components.values():
        if "connector" not in comp.role and comp.role != "debug_header":
            continue
        cfg = _component_intent(intent, comp.ref)
        members = _cluster_members(comp, components, nets, aliases, radius=18.0, proximity_any_role=False, support_owner=support_owner)
        local_x = max(0.0, min(board.width, comp.x - board.origin_x))
        local_y = max(0.0, min(board.height, comp.y - board.origin_y))
        distances = {
            "left": local_x,
            "right": board.width - local_x,
            "top": local_y,
            "bottom": board.height - local_y,
        }
        nearest_edge = min(distances, key=distances.get)
        edge_span = board.width if nearest_edge in ("left", "right") else board.height
        inset = 2.0
        near_edge = edge_span > 0 and distances[nearest_edge] / edge_span <= 0.25 and distances[nearest_edge] > inset
        name = re.sub(r"[^A-Za-z0-9_]+", "_", (("IC" if comp.role == "mcu" else comp.role.upper()) + "_" + comp.ref))
        haystack = f"{comp.footprint.upper()} {(comp.value or '').upper()}"
        inferred_edge_required = (comp.role == "high_speed_connector" or
                                  any(token in haystack for token in _EDGE_CONNECTOR_TOKENS))
        if "edge_required" in cfg:
            edge_required = bool(cfg.get("edge_required"))
        elif cfg.get("access_side") is not None:
            edge_required = True
        elif "connector" in comp.role:
            edge_required = near_edge or inferred_edge_required
        else:
            edge_required = False
        access_side = str(cfg.get("access_side") or nearest_edge)
        fp_edge = floorplan.edge_connectors.get(comp.ref)
        if fp_edge is not None:
            # Floorplan distributes connectors along their edge; use its solved
            # access side and along-edge coordinate instead of the source layout.
            access_side, along = fp_edge
            placement_kw = "y" if access_side in ("left", "right") else "x"
            allow_outside = bool(cfg.get("allow_body_outside_board", edge_required))
            rotation_cfg = cfg.get("rotation", "auto")
            if _is_number(rotation_cfg):
                rot_kw = f"rot={float(rotation_cfg):g}, "
                rotation_label = f"{float(rotation_cfg):g}"
            elif isinstance(rotation_cfg, str) and rotation_cfg.lower() == "auto":
                rot_kw = 'rot="auto", '
                rotation_label = "auto from access side"
            else:
                rot_kw = ""
                rotation_label = "keep existing"
            locked = bool(cfg.get("locked", True))
            # Edge-locked connectors are rotated to their access side, which makes
            # the source-relative geometry of their support parts invalid (a part
            # that sat to the connector's right ends up off-board after a 90/270
            # deg rotation). So the rigid edge placement carries the connector
            # alone; its support parts (ESD, pull-up arrays, decoupling, near-pad
            # passives) are placed interior by their own pad-relative rules against
            # the connector's final pads. The full neighbourhood is still recorded
            # as cluster metadata below (clusters are metadata, not atomic units).
            placement_members = [comp.ref]
            text = (f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(placement_members)}, '
                    f'placement=Edge(edge={_q(access_side)}, {placement_kw}={along:.3f}, inset={inset}, '
                    f'{rot_kw}locked={str(locked)}, edge_required={str(edge_required)}, mechanical=True, '
                    f'access_side={_q(access_side)}, allow_body_outside_board={str(allow_outside)}), '
                    f'role={_q(comp.role)})')
            rules.append(PlanRule("cluster", text, placement_members,
                                  f"{comp.ref} edge connector placed alone and rotated to {access_side}; support parts "
                                  f"placed interior by pad-relative rules so rotation cannot strand them off-board. "
                                  f"edge_required={edge_required}, access_side={access_side}, "
                                  f"rotation={rotation_label}, "
                                  f"allow_body_outside_board={allow_outside}."))
            edge_rotation_plan.append({"ref": comp.ref, "access_side": access_side,
                                       "rotation": rotation_cfg if not isinstance(rotation_cfg, str) else "auto",
                                       "allow_body_outside_board": allow_outside,
                                       "edge_required": edge_required, "locked": locked})
        else:
            anchor_x, anchor_y = floorplan.core_xy.get(comp.ref, (local_x, local_y))
            text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement=Anchor(x={anchor_x:.3f}, y={anchor_y:.3f}, rot={comp.rot:g}), role={_q(comp.role)})'
            rules.append(PlanRule("cluster", text, members, f"{comp.ref} connector placed in board core by floorplanner (not edge-required)."))
        clusters.append({"name": name, "anchor": comp.ref, "members": members, "role": comp.role,
                         "category": _cluster_category(comp), "confidence": "high" if len(members) > 1 else "low",
                         "metadata_only": True,
                         "note": "clusters are metadata: members keep this neighborhood only until a higher-priority ownership rule refines them"})
        explanations.setdefault(comp.ref, {}).update({"role": comp.role, "generated_rule": text})

    # High speed pairs/corridors and ESD chains.
    hs_connectors = [c for c in components.values() if "connector" in c.role and any(is_high_speed(n) for n in c.nets)]
    hs_ics = [c for c in components.values() if c.role in {"ic", "hdmi_retimer", "mcu"} and any(is_high_speed(n) for n in c.nets)]
    for pair in pairs:
        if len(pair.components) >= 2:
            endpoints = pair.components[:2]
            best_d = -1.0
            for a_ref in pair.components:
                for b_ref in pair.components:
                    if a_ref == b_ref or a_ref not in components or b_ref not in components:
                        continue
                    d = math.hypot(components[a_ref].x - components[b_ref].x, components[a_ref].y - components[b_ref].y)
                    if d > best_d:
                        best_d = d
                        endpoints = [a_ref, b_ref]
            rules.append(PlanRule("corridor", f'Corridor({_q(pair.name + "_CORRIDOR")}, a={_q(endpoints[0])}, b={_q(endpoints[1])}, width=3.0, role="high_speed_diff_pair")', pair.components, f"Differential pair {pair.p}/{pair.n}: reserve short symmetric routing corridor; do not route here automatically."))
    for esd in [c for c in components.values() if c.role == "esd_protection"]:
        connector = _nearest_parent(esd, hs_connectors) or _nearest_parent(esd, [c for c in components.values() if "connector" in c.role])
        protected = _nearest_parent(esd, hs_ics) or _nearest_parent(esd, [c for c in components.values() if c.role in {"ic", "mcu", "hdmi_retimer"}])
        if connector and protected:
            text = f'ESD({_q(esd.ref)}, connector={_q(connector.ref)}, protected={_q(protected.ref)}, t=0.18, offset=0, role="esd")'
            rules.append(PlanRule("esd", text, [esd.ref, connector.ref, protected.ref], f"{esd.ref} inferred as ESD/protection between {connector.ref} and {protected.ref}; place close to connector with short ground return."))
            explanations[esd.ref] = {"role": esd.role, "nets": esd.nets, "connector": connector.ref, "protected": protected.ref, "generated_rule": text}
        else:
            uncertain.append(f"{esd.ref} looks like ESD/protection but connector/protected IC was not clear.")

    # Functional signal paths: reserve direct high-speed corridors before
    # low-speed support parts are placed. Metadata for scoring + soft keepout.
    for path_name, path in sorted(functional_paths.items()):
        text = (f'HighSpeedPath({_q(path_name)}, sequence={_q(path["sequence"])}, '
                f'corridor_width={path["corridor_width_mm"]:g}, protect_first={str(path["protect_first"])}, '
                f'role={_q(path["type"])})')
        rules.append(PlanRule("high_speed_path", text, list(path["sequence"]),
                              f"Functional path {path_name} ({path['source']}): keep "
                              f"{' -> '.join(path['sequence'])} short, direct, and free of unrelated parts."))

    # IC and power clusters.
    for comp in components.values():
        if comp.role in {"ic", "mcu", "power_regulator", "rf_module", "hdmi_retimer"}:
            members = _cluster_members(comp, components, nets, aliases, radius=12.0, support_owner=support_owner)
            region = "POWER" if comp.role == "power_regulator" and "POWER" in regions else "RF" if comp.role == "rf_module" and "RF" in regions else "CONTROL" if comp.role != "rf_module" and "CONTROL" in regions else None
            name = re.sub(r"[^A-Za-z0-9_]+", "_", (("IC" if comp.role == "mcu" else comp.role.upper()) + "_" + comp.ref))
            anchor_x, anchor_y = floorplan.core_xy.get(comp.ref, (comp.x - board.origin_x, comp.y - board.origin_y))
            placement = f'Anchor(x={anchor_x:.3f}, y={anchor_y:.3f}' + (f', region={_q(region)}' if region else '') + ')'
            text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement={placement}, role={_q(comp.role)})'
            rules.append(PlanRule("cluster", text, members, f"{comp.ref} {comp.role} cluster placed by wirelength-driven floorplanner; pin-aware refinements follow."))
            clusters.append({"name": name, "anchor": comp.ref, "members": members, "role": comp.role, "category": _cluster_category(comp), "confidence": "high" if len(members) > 1 else "low"})
            explanations.setdefault(comp.ref, {}).update({"role": comp.role, "generated_rule": text})
            if comp.role == "rf_module" and not any(k.get("role") == "rf" for k in keepouts if isinstance(k, dict)):
                # Use the exact keepout rectangle the floorplanner reserved (and
                # legalized other parts out of) at the module's solved location.
                rect = floorplan.rf_keepouts.get(comp.ref)
                if rect is None:
                    new_origin = (board.origin_x + anchor_x, board.origin_y + anchor_y)
                    delta = (new_origin[0] - comp.x, new_origin[1] - comp.y)
                    moved_comp = dataclasses.replace(
                        comp, x=new_origin[0], y=new_origin[1],
                        bbox=comp.bbox.translated(delta[0], delta[1]) if comp.bbox is not None else None)
                    rect = _synthesized_rf_keepout(moved_comp, board)
                ko = f'Keepout({_q(comp.ref + "_ANTENNA")}, x={rect["x"]:.3f}, y={rect["y"]:.3f}, w={rect["w"]:.3f}, h={rect["h"]:.3f}, role="rf")'
                rules.append(PlanRule("keepout", ko, [comp.ref], f"{comp.ref} inferred RF/module; synthesized antenna keepout adjacent to module edge and requires engineering review."))

    graph = ConnectivityGraph(components, nets)
    ic_candidates = [c for c in components.values() if c.role in {"ic", "mcu", "hdmi_retimer", "rf_module"}]

    # Power islands: compact regulator topology (input cap -> regulator ->
    # inductor -> output cap, feedback at FB), not a generic power pile.
    # Members claimed here are skipped by the generic decoupling/pullup passes
    # so each ref keeps exactly one placement owner.
    power_claimed: set[str] = set()
    for island in power_islands:
        reg_ref = island["regulator"]
        reg = components.get(reg_ref)
        if reg is None:
            uncertain.append(f"power island {island['name']} regulator {reg_ref} is not on the board.")
            continue
        island_member_refs = [reg_ref]

        def _emit_island_caps(refs: List[str], target_nets: List[str], role: str, distance: float) -> None:
            refs = [r for r in refs if r in components]
            if not refs:
                return
            pad = _nearest_pad(reg, set(target_nets))
            net = target_nets[0] if target_nets else None
            if len(refs) == 1:
                pad_kw = f"pad={_q(pad)}, " if pad else ""
                text = (f'Decoupling({_q(refs[0])}, parent={_q(reg_ref)}, {pad_kw}distance={distance:g}, '
                        f'power_net={_qn(net)}, ground_net="GND", role={_q(role)})')
                rules.append(PlanRule("decoupling", text, [refs[0], reg_ref],
                                      f"{refs[0]} {role.replace('_', ' ')} for {reg_ref}: minimize the high di/dt hot loop."))
            else:
                pad_kw = f"pad={_q(pad)}, " if pad else ""
                text = (f'DecouplingArray(refs={_q(refs)}, parent={_q(reg_ref)}, {pad_kw}side="auto", '
                        f'distance={distance:g}, spacing=1.5, stagger=True, rows="auto", '
                        f'role={_q(role)}, power_net={_qn(net)}, ground_net="GND")')
                rules.append(PlanRule("decoupling_array", text, list(refs),
                                      f"{', '.join(refs)} {role.replace('_', ' ')} array for {reg_ref}: keep the converter loop compact."))
            for cref in refs:
                power_claimed.add(cref)
                island_member_refs.append(cref)
                explanations[cref] = {"role": role, "nets": components[cref].nets, "parent_candidate": reg_ref,
                                      "generated_rule": text, "primitive_selected": "Decoupling" if len(refs) == 1 else "DecouplingArray",
                                      "power_island": island["name"]}

        _emit_island_caps(list(island.get("input_caps", [])), list(island.get("input_nets", [])), "input_cap", 1.2)
        _emit_island_caps(list(island.get("output_caps", [])), list(island.get("output_nets", [])), "output_cap", 1.5)

        inductor_ref = island.get("inductor")
        if inductor_ref and inductor_ref in components:
            sw_or_out = ([island["switch_net"]] if island.get("switch_net") else []) + list(island.get("output_nets", []))
            pad = _nearest_pad(reg, set(sw_or_out))
            if pad:
                text = f'NearPad({_q(inductor_ref)}, parent={_q(reg_ref)}, pad={_q(pad)}, distance=1.5, role="inductor")'
            else:
                text = f'Satellite({_q(inductor_ref)}, parent={_q(reg_ref)}, side="auto", distance=1.5, role="inductor")'
            rules.append(PlanRule("nearpad" if pad else "satellite", text, [inductor_ref, reg_ref],
                                  f"{inductor_ref} power inductor kept tight against {reg_ref}; keep switch-node copper compact."))
            power_claimed.add(inductor_ref)
            island_member_refs.append(inductor_ref)
            explanations[inductor_ref] = {"role": "inductor", "nets": components[inductor_ref].nets,
                                          "parent_candidate": reg_ref, "generated_rule": text,
                                          "power_island": island["name"]}

        feedback_refs = [r for r in island.get("feedback", []) if r in components]
        if feedback_refs:
            fb_nets = {n for n in reg.nets if not is_power(n) and not is_ground(n) and _FB_NET_RE.search(n)}
            fb_pad = _nearest_pad(reg, fb_nets)
            for fref in feedback_refs:
                if fb_pad:
                    text = f'NearPad({_q(fref)}, parent={_q(reg_ref)}, pad={_q(fb_pad)}, distance=1.5, role="feedback")'
                else:
                    text = f'Satellite({_q(fref)}, parent={_q(reg_ref)}, side="auto", distance=2.0, role="feedback")'
                rules.append(PlanRule("nearpad" if fb_pad else "satellite", text, [fref, reg_ref],
                                      f"{fref} feedback network kept at {reg_ref} FB pin, away from the switch node."))
                power_claimed.add(fref)
                island_member_refs.append(fref)
                explanations[fref] = {"role": "feedback", "nets": components[fref].nets,
                                      "parent_candidate": reg_ref, "generated_rule": text,
                                      "power_island": island["name"]}

        island_text = (f'PowerIsland({_q(island["name"])}, regulator={_q(reg_ref)}, '
                       f'input_caps={_q(list(island.get("input_caps", [])))}, '
                       f'inductor={_qn(island.get("inductor"))}, '
                       f'output_caps={_q(list(island.get("output_caps", [])))}, '
                       f'feedback={_q(list(island.get("feedback", [])))}, '
                       f'switch_net={_qn(island.get("switch_net"))})')
        rules.append(PlanRule("power_island", island_text, island_member_refs,
                              f"Power island {island['name']} ({island['source']}): topology-aware converter "
                              "placement scored in power-placement-review."))

    # Decoupling capacitors: group caps that share the same owning power pin
    # (same parent IC + same power net/pad) and emit one primitive per group
    # instead of one Decoupling()+NearPad() pair per capacitor.
    decoupling_groups: List[Dict[str, Any]] = []
    decoupling_keys: Dict[Tuple[str, str], List[Tuple[PlanComponent, Optional[str]]]] = {}
    for cap in [c for c in components.values() if c.role == "decoupling" and c.ref not in power_claimed]:
        power_nets = {n for n in cap.nets if is_power(n)}
        parent = _nearest_parent(cap, ic_candidates, power_nets) or _nearest_parent(cap, ic_candidates)
        if not parent:
            uncertain.append(f"{cap.ref} is a power-to-ground capacitor but no parent IC candidate was found.")
            topology_failures.append(f"{cap.ref}: no parent IC candidate found for decoupling inference")
            continue
        power_net = next(iter(power_nets), None)
        pad = _nearest_pad(parent, power_nets)
        decoupling_keys.setdefault((parent.ref, pad or power_net or "UNKNOWN"), []).append((cap, power_net))

    for (parent_ref, _pad_or_net), caps in sorted(decoupling_keys.items()):
        parent = components[parent_ref]
        power_net = next((pn for _c, pn in caps if pn), None)
        pad = _nearest_pad(parent, {power_net} if power_net else set())
        members = [c.ref for c, _ in caps]
        if len(caps) == 1:
            cap = caps[0][0]
            if pad:
                text = f'Decoupling({_q(cap.ref)}, parent={_q(parent_ref)}, pad={_q(pad)}, distance=1.5, power_net={_q(power_net)}, ground_net="GND")'
            else:
                text = f'Decoupling({_q(cap.ref)}, parent={_q(parent_ref)}, distance=2.0, power_net={_q(power_net)}, ground_net="GND")'
            rules.append(PlanRule("decoupling", text, [cap.ref, parent_ref], f"{cap.ref} inferred as decoupling capacitor for {parent_ref} ({power_net or 'unknown net'}); single placement primitive."))
            explanations[cap.ref] = {"role": cap.role, "nets": cap.nets, "parent_candidate": parent_ref, "generated_rule": text, "primitive_selected": "Decoupling", "primitive_scores": {"NearPad": 70 if pad else 0, "Decoupling": 90, "Cluster": 0, "Anchor": 10}}
            decoupling_groups.append({"parent": parent_ref, "power_net": power_net, "pad": pad, "members": members, "primitive": "Decoupling", "grouped": False})
        else:
            pad_kw = f"pad={_q(pad)}, " if pad else ""
            inferred_pad_side = _component_pad_side(parent, pad)
            parent_near_edge = _component_near_board_edge(parent, board)
            stagger_recommended = len(caps) > 3
            effective_side = _effective_support_side(inferred_pad_side, parent, board)
            side_value = effective_side or ("inward" if parent_near_edge else "auto")
            array_opts = 'stagger=True, rows="auto", '
            if len(caps) > 3:
                array_opts += 'max_per_row=3, '
            text = (f'DecouplingArray(refs={_q(members)}, parent={_q(parent_ref)}, {pad_kw}side={_q(side_value)}, '
                    f'distance=2.0, spacing=1.5, {array_opts}role="decoupling", '
                    f'power_net={_qn(power_net)}, ground_net="GND")')
            rules.append(PlanRule("decoupling_array", text, list(members),
                                   f"{', '.join(members)} grouped decoupling array for {parent_ref} ({power_net or 'unknown net'}); "
                                   f"pad_side={inferred_pad_side or 'unknown'}, parent_near_board_edge={parent_near_edge}, "
                                   f"effective_side={side_value}, stagger_recommended={stagger_recommended}; replaces per-capacitor Decoupling()/NearPad() rules."))
            for cref in members:
                explanations[cref] = {"role": "decoupling", "nets": components[cref].nets, "parent_candidate": parent_ref, "generated_rule": text, "primitive_selected": "DecouplingArray", "primitive_scores": {"NearPad": 0, "Decoupling": 40, "DecouplingArray": 90, "Cluster": 80, "Anchor": 10}}
            decoupling_groups.append({"parent": parent_ref, "power_net": power_net, "pad": pad, "members": members,
                                        "primitive": "DecouplingArray", "grouped": True,
                                        "inferred_pad_side": inferred_pad_side,
                                        "parent_near_board_edge": parent_near_edge,
                                        "effective_side": side_value,
                                        "stagger_recommended": stagger_recommended})

    # Pullup/pulldown resistors: determine the true owning signal source
    # (connector/MCU/peripheral) via the connectivity graph and group
    # resistors that share an owner into a single placement primitive.
    pullup_groups: List[Dict[str, Any]] = []
    pullup_owners: Dict[str, List[Tuple[PlanComponent, Optional[str]]]] = {}
    for r in [c for c in components.values() if c.role == "pullup_pulldown" and c.ref not in power_claimed]:
        signal_nets = [n for n in r.nets if not is_power(n) and not is_ground(n)]
        signal_net = signal_nets[0] if signal_nets else None
        owner = graph.pullup_owner(r.ref, signal_net)
        if owner is None:
            owner = _nearest_parent(r, ic_candidates, set(signal_nets)) or _nearest_parent(r, ic_candidates)
        if owner is None:
            uncertain.append(f"{r.ref} looks like a pullup/pulldown but no owning connector/IC/peripheral could be determined from the connectivity graph.")
            topology_failures.append(f"{r.ref}: no owning component found for pullup/pulldown inference")
            continue
        pullup_owners.setdefault(owner.ref, []).append((r, signal_net))

    for owner_ref, items in sorted(pullup_owners.items()):
        owner = components[owner_ref]
        if len(items) == 1:
            r, signal_net = items[0]
            pad = _nearest_pad(owner, {signal_net} if signal_net else set())
            if pad:
                text = f'Pullup({_q(r.ref)}, parent={_q(owner_ref)}, net={_q(signal_net)}, pad={_q(pad)}, distance=3.0)'
            else:
                text = f'Pullup({_q(r.ref)}, parent={_q(owner_ref)}, net={_q(signal_net)}, distance=3.0)'
            rules.append(PlanRule("pullup", text, [r.ref, owner_ref], f"{r.ref} inferred as pullup/pulldown on {signal_net or 'unknown signal'}; placed near owning {owner.role} {owner_ref}."))
            explanations[r.ref] = {"role": r.role, "nets": r.nets, "parent_candidate": owner_ref, "generated_rule": text, "primitive_selected": "Pullup", "primitive_scores": {"NearPad": 70 if pad else 0, "Pullup": 90, "Cluster": 0, "Anchor": 10}}
            pullup_groups.append({"owner": owner_ref, "members": [r.ref], "primitive": "Pullup", "grouped": False})
        else:
            members = [r.ref for r, _ in items]
            signal_nets = [signal_net for _r, signal_net in items]
            text = (f'PullupArray(refs={_q(members)}, parent={_q(owner_ref)}, nets={_q_list_with_none(signal_nets)}, '
                    f'side="auto", distance=4.0, spacing=2.0, role="pullup")')
            rules.append(PlanRule("pullup_array", text, list(members),
                                   f"{', '.join(members)} grouped pullup/pulldown array near owning {owner.role} {owner_ref}; "
                                   "replaces per-resistor Pullup() rules."))
            for rref in members:
                explanations[rref] = {"role": "pullup_pulldown", "nets": components[rref].nets, "parent_candidate": owner_ref, "generated_rule": text, "primitive_selected": "PullupArray", "primitive_scores": {"NearPad": 0, "Pullup": 40, "PullupArray": 90, "Cluster": 80, "Anchor": 10}}
            pullup_groups.append({"owner": owner_ref, "members": members, "primitive": "PullupArray", "grouped": True})

    # Series passives: only infer Series() when the connectivity graph shows
    # a true A -> resistor -> B path (each non-ground net connects the
    # resistor to exactly one other non-passive component). Shared-net
    # relationships with other passives (e.g. decoupling caps) are
    # insufficient and are rejected with a warning instead.
    for r in [c for c in components.values() if c.role == "series" and c.ref not in power_claimed]:
        endpoints = graph.series_endpoints(r.ref)
        if endpoints:
            a_ref, b_ref = endpoints
            text = f'Series({_q(r.ref)}, a={_q(a_ref)}, b={_q(b_ref)}, t=0.5, offset=0)'
            rules.append(PlanRule("series", text, [r.ref, a_ref, b_ref], f"{r.ref} validated as series component between {a_ref} and {b_ref} via connectivity graph."))
            explanations[r.ref] = {"role": r.role, "nets": r.nets, "generated_rule": text, "primitive_selected": "Series"}
        else:
            msg = f"{r.ref} looks like a two-pin series component, but the connectivity graph could not find a unique A -> {r.ref} -> B topology; rejecting Series() inference."
            uncertain.append(msg)
            warnings.append("WARNING: " + msg)
            topology_failures.append(f"{r.ref}: rejected Series() inference (no validated A-resistor-B topology)")

    placed_refs = {ref for rule in rules for ref in rule.refs}
    anchor_candidates = [c for c in components.values() if c.role in {"ic", "mcu", "hdmi_retimer", "rf_module", "power_regulator", "connector", "high_speed_connector"}]
    for comp in components.values():
        if comp.ref in placed_refs or comp.role in {"mechanical", "unknown"}:
            continue
        signal_nets = {n for n in comp.nets if not is_ground(n)}
        parent = _nearest_parent(comp, anchor_candidates, signal_nets) or _nearest_parent(comp, anchor_candidates)
        if not parent:
            continue
        pad = _nearest_pad(parent, signal_nets)
        if comp.role in {"testpoint", "clock"} and pad:
            text = f'NearPad({_q(comp.ref)}, parent={_q(parent.ref)}, pad={_q(pad)}, distance=2.0, role={_q(comp.role)})'
            rules.append(PlanRule("nearpad", text, [comp.ref, parent.ref], f"{comp.ref} support component placed near {parent.ref} pad {pad} from shared connectivity."))
        elif comp.role in {"inductor", "ferrite", "common_mode_choke", "fuse"}:
            peers = [components[ref] for n in signal_nets for ref, _ in nets.get(n, PlanNet(n)).pads if ref in components and ref != comp.ref and components[ref].role not in {"inductor", "ferrite", "common_mode_choke", "fuse", "capacitor", "resistor"}]
            if len(peers) >= 2:
                text = f'Between({_q(comp.ref)}, a={_q(peers[0].ref)}, b={_q(peers[1].ref)}, t=0.5, offset=0, role={_q(comp.role)})'
                rules.append(PlanRule("between", text, [comp.ref, peers[0].ref, peers[1].ref], f"{comp.ref} inferred as inline filter/protection element between {peers[0].ref} and {peers[1].ref}."))
            else:
                text = f'Satellite({_q(comp.ref)}, parent={_q(parent.ref)}, side="auto", distance=2.0, role={_q(comp.role)})'
                rules.append(PlanRule("satellite", text, [comp.ref, parent.ref], f"{comp.ref} support magnetic/filter element kept near {parent.ref}."))
        elif comp.role in {"capacitor", "resistor"}:
            text = f'Satellite({_q(comp.ref)}, parent={_q(parent.ref)}, side="auto", distance=2.5, role={_q(comp.role)})'
            rules.append(PlanRule("satellite", text, [comp.ref, parent.ref], f"{comp.ref} generic support passive kept near connected anchor {parent.ref}."))
        elif comp.role in {"led", "switch"}:
            text = f'Satellite({_q(comp.ref)}, parent={_q(parent.ref)}, side="auto", distance=3.0, role={_q(comp.role)})'
            rules.append(PlanRule("satellite", text, [comp.ref, parent.ref], f"{comp.ref} {comp.role} placed near {parent.ref} by access/service rules; move to an edge or annotate components.{comp.ref} in board.pln if it is user-facing."))
        if comp.ref in {ref for rule in rules for ref in rule.refs}:
            explanations.setdefault(comp.ref, {"role": comp.role, "nets": comp.nets, "parent_candidate": parent.ref, "generated_rule": rules[-1].text})

    placed_refs = {ref for rule in rules for ref in rule.refs}
    unplaced_support = [c.ref for c in components.values() if c.ref not in placed_refs and c.role not in {"mechanical", "unknown"}]
    if unplaced_support:
        warnings.append("Support components without generated placement rules require review: " + ", ".join(sorted(unplaced_support)))

    warnings.extend(_validate_board_bounds(rules, board))
    duplicate_rules, rule_problems = _validate_rules(rules, components)
    warnings.extend(duplicate_rules)
    topology_failures.extend(rule_problems)

    roles = {ref: c.role for ref, c in components.items()}
    return Plan(
        board,
        components,
        nets,
        aliases,
        alias_diagnostics,
        roles,
        regions,
        keepouts,
        clusters,
        pairs,
        stackup,
        routing,
        declared_pairs,
        net_classes,
        routing_overrides,
        simulation,
        routing_warnings,
        stackup_warnings,
        simulation_warnings,
        high_speed_constraints_complete,
        rules,
        warnings,
        uncertain,
        explanations,
        topology_failures,
        decoupling_groups,
        pullup_groups,
        duplicate_rules,
        spacing,
        functional_paths,
        power_islands,
        mounting_hole_plan,
        edge_rotation_plan,
    )



def _routing_class_for_pair(plan: Plan, pair: Mapping[str, Any]) -> Dict[str, Any]:
    class_name = pair.get("class")
    classes = _as_mapping(plan.routing.get("classes"))
    return _as_mapping(classes.get(class_name)) if class_name else {}


def routing_summary_comments(plan: Plan) -> List[str]:
    comments: List[str] = []
    if not (plan.routing or plan.declared_differential_pairs):
        return comments
    comments.append("# Routing constraints from board.pln:")
    mode = plan.routing.get("mode")
    if mode:
        comments.append(f"# routing mode: {mode}")
    for name, pair in sorted(plan.declared_differential_pairs.items()):
        cls = _routing_class_for_pair(plan, pair)
        bits: List[str] = []
        impedance = cls.get("impedance_ohms") or pair.get("impedance_ohms")
        if impedance is not None:
            bits.append(f"{impedance:g} ohm differential" if _is_number(impedance) else f"{impedance} ohm differential")
        layer = cls.get("preferred_layer") or pair.get("preferred_layer")
        if layer is None:
            layers = cls.get("preferred_layers") or pair.get("preferred_layers")
            if isinstance(layers, list) and layers:
                layer = layers[0]
        ref_plane = cls.get("reference_plane") or pair.get("reference_plane")
        if layer and ref_plane:
            bits.append(f"{layer} over {ref_plane}")
        elif layer:
            bits.append(f"preferred layer {layer}")
        skew = cls.get("max_skew_mm") or pair.get("max_skew_mm")
        if skew is not None:
            bits.append(f"max skew {skew:g} mm" if _is_number(skew) else f"max skew {skew} mm")
        mismatch = cls.get("max_length_mismatch_mm") or pair.get("max_length_mismatch_mm")
        if mismatch is not None and mismatch != skew:
            bits.append(f"max mismatch {mismatch:g} mm" if _is_number(mismatch) else f"max mismatch {mismatch} mm")
        nets = f"{pair.get('p', '?')}/{pair.get('n', '?')}"
        comments.append(f"# {name}: {', '.join(bits) if bits else 'differential pair'} ({nets})")
    comments.append("# Routing performed later by orchestrator/KiCadRoutingTools.")
    comments.append("")
    return comments


def _json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    text = str(value)
    if not text or re.search(r"[:#\[\]{},&*!|>'\"%@`\s]", text):
        return json.dumps(text)
    return text


def _emit_yaml(value: Any, indent: int = 0) -> List[str]:
    sp = " " * indent
    value = _json_safe(value)
    if isinstance(value, Mapping):
        lines: List[str] = []
        if not value:
            return [sp + "{}"]
        for key, item in value.items():
            if isinstance(item, (Mapping, list)):
                lines.append(f"{sp}{key}:")
                lines.extend(_emit_yaml(item, indent + 2))
            else:
                lines.append(f"{sp}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [sp + "[]"]
        lines = []
        for item in value:
            item = _json_safe(item)
            if isinstance(item, Mapping):
                lines.append(f"{sp}-")
                lines.extend(_emit_yaml(item, indent + 2))
            elif isinstance(item, list):
                lines.append(f"{sp}-")
                lines.extend(_emit_yaml(item, indent + 2))
            else:
                lines.append(f"{sp}- {_yaml_scalar(item)}")
        return lines
    return [sp + _yaml_scalar(value)]


def emit_yaml(value: Any) -> str:
    return "\n".join(_emit_yaml(value)) + "\n"


def emit_routing_policy(plan: Plan, board_path: Path) -> str:
    payload = {
        "source": "pcb-plan .pln routing intent",
        "board_file": str(board_path),
        "routing": plan.routing,
        "net_classes": plan.net_classes,
        "routing_overrides": plan.routing_overrides,
        "differential_pairs": plan.declared_differential_pairs,
        "stackup": plan.stackup,
        "warnings": plan.routing_warnings + plan.stackup_warnings,
        "handoff": {
            "consumer": "pcb-automation-orchestrator/KiCadRoutingTools",
            "route_traces": False,
            "advisory": "pcb-plan preserves routing intent only; downstream tools perform routing.",
        },
    }
    return emit_yaml(payload)


def openems_plan_should_emit(plan: Plan) -> bool:
    openems = _as_mapping(plan.simulation.get("openems"))
    return _simulation_openems_enabled(openems)


def emit_openems_plan(plan: Plan, board_path: Path) -> str:
    openems = _as_mapping(plan.simulation.get("openems"))
    classes = _as_mapping(plan.routing.get("classes"))
    critical_nets = sorted({
        str(net)
        for pair in plan.declared_differential_pairs.values()
        for net in (pair.get("p"), pair.get("n"))
        if net
    })
    for pattern, spec in plan.net_classes.items():
        if isinstance(spec, Mapping):
            class_name = str(spec.get("class", ""))
            class_spec = _as_mapping(classes.get(class_name))
            if _routing_class_is_high_speed(class_name, class_spec) or class_name.lower() in {"rf", "switching_power"}:
                critical_nets.append(str(pattern))
    regions = sorted(set(str(region) for spec in classes.values() if isinstance(spec, Mapping) for region in _as_list(spec.get("avoid_regions"))))
    payload = {
        "board_file": str(board_path),
        "stackup_summary": plan.stackup,
        "critical_nets": sorted(set(critical_nets)),
        "differential_pairs": plan.declared_differential_pairs,
        "regions_of_interest": regions,
        "simulation_goal": "Advisory SI/EMI exploration for high-speed, RF, and switching-power constraints captured by pcb-plan.",
        "openems": {
            "enabled": openems.get("enabled"),
            "triggers": _as_list(openems.get("trigger_on")),
            "export_dir": openems.get("export_dir", "simulation/openems"),
            "notes": openems.get("notes", "advisory_only"),
        },
        "advisory_warning": "pcb-plan does not route traces or run OpenEMS; this plan is a handoff hook for simulation automation.",
        "warnings": plan.simulation_warnings + plan.routing_warnings + plan.stackup_warnings,
    }
    return emit_yaml(payload)


def emit_ppl(plan: Plan, board_path: Path, netlist_path: Optional[Path]) -> str:
    lines = [
        "# Generated by pcb-plan",
        "# Generated placement plan.",
        "# This file is intended to be edited by humans and AI assistants as part of an",
        "# iterative layout-optimization workflow: review the comments, adjust rules,",
        "# re-run pcb-place, and feed the reports back into board.pln.",
        "# Ordering model: mechanical constraints first, functional signal-path topology",
        "# second, power topology third, support passives fourth; spacing always enforced.",
        "# Each ref has exactly one placement owner: later higher-priority rules refine",
        "# earlier coarse cluster moves (clusters are metadata, not atomic units).",
        "# Requires engineering review before fabrication.",
        "# High-speed routing, impedance, return path, and EMI compliance must be verified.",
        "# Generated: not timestamped to keep planner output deterministic.",
        f"# Source board: {board_path}",
        f"# Source netlist: {netlist_path if netlist_path else 'none'}",
        "",
    ]
    confidence = compute_plan_confidence(plan)
    if confidence["level"] == "low":
        lines.append(f"# Plan confidence: {confidence['level']} (score {confidence['score']})")
        lines.append("# Reasons:")
        for reason in confidence["reasons"]:
            lines.append(f"# - {reason}")
        lines.append("")
    lines.extend(routing_summary_comments(plan))
    emit_outline = plan.board.source != "footprint_extents"
    if not emit_outline:
        lines.append("# Edge.Cuts outline not emitted: board geometry was inferred from footprint extents (low confidence).")
    sp = plan.spacing
    lines.extend([
        f"Board(width={plan.board.width:.6g}, height={plan.board.height:.6g}, origin_x={plan.board.origin_x:.6g}, origin_y={plan.board.origin_y:.6g}, emit_outline={emit_outline})",
        "# Spacing profile (mm) from board.pln spacing: section or conservative defaults;",
        "# parts never default to touching.",
        (f"Spacing(default={sp['default']:g}, passive_to_passive={sp['passive_to_passive']:g}, "
         f"passive_to_ic={sp['passive_to_ic']:g}, ic_to_ic={sp['ic_to_ic']:g}, "
         f"connector={sp['connector_to_component']:g}, mechanical={sp['mechanical_to_component']:g})"),
        "PlacementPolicy(avoid_overlap=True, allow_anchor_move=False, max_search_radius=5, search_step=0.5)",
        "",
    ])
    sections = [
        ("Mechanical constraints first: mounting holes and fixed mechanical objects", {"corner", "fixed"}),
        ("Regions and keepouts", {"region", "keepout"}),
        ("Edge connectors and coarse clusters (clusters are metadata; members may be refined below)", {"cluster"}),
        ("Functional signal paths and high-speed corridors (reserved before support parts)", {"high_speed_path", "corridor"}),
        ("Power islands: topology-aware regulator placement", {"power_island"}),
        ("Critical pin-aware refinements (these own their refs over cluster moves)", {"decoupling", "decoupling_array", "esd", "nearpad", "between"}),
        ("Low-priority refinements and support parts", {"pullup", "pullup_array", "series", "satellite"}),
    ]
    emitted: set[int] = set()
    for title, kinds in sections:
        block = [(i, r) for i, r in enumerate(plan.rules) if r.kind in kinds]
        if not block:
            continue
        lines.append(f"# {title}")
        for i, rule in block:
            if rule.comment:
                lines.append(f"# {rule.comment}")
            lines.append(rule.text)
            emitted.add(i)
        lines.append("")
    extra = [(i, r) for i, r in enumerate(plan.rules) if i not in emitted]
    if extra:
        lines.append("# Remaining inferred rules")
        for _, rule in extra:
            if rule.comment:
                lines.append(f"# {rule.comment}")
            lines.append(rule.text)
        lines.append("")
    return "\n".join(lines)


def compute_plan_confidence(plan: Plan) -> Dict[str, Any]:
    """Score how trustworthy this plan is before placement.

    Checks the conditions that most often indicate a plan was generated from
    incomplete board/netlist/intent inputs: missing nets, mostly-singleton
    clusters, mostly-unplaced components, duplicate placement rules, missing
    differential pairs despite high-speed connectors, suppressed Series()
    inferences, and footprint-extents board geometry fallback.
    """

    reasons: List[str] = []
    score = 100

    if not plan.nets:
        reasons.append("no nets were parsed from the board/netlist")
        score -= 40

    total_clusters = len(plan.clusters)
    single_clusters = sum(1 for c in plan.clusters if len(c.get("members", [])) <= 1)
    if total_clusters and single_clusters / total_clusters > 0.5:
        reasons.append(f"{single_clusters}/{total_clusters} clusters are single-member")
        score -= 15

    placed_refs = {ref for rule in plan.rules for ref in rule.refs if ref in plan.components}
    unplaced = [ref for ref in plan.components if ref not in placed_refs]
    if plan.components and len(unplaced) / len(plan.components) > 0.25:
        reasons.append(f"{len(unplaced)}/{len(plan.components)} components are unplaced")
        score -= 20

    if plan.duplicate_rules:
        reasons.append(f"{len(plan.duplicate_rules)} duplicate placement rule(s) found")
        score -= 15

    hs_connectors = any("connector" in c.role and any(is_high_speed(n) for n in c.nets) for c in plan.components.values())
    if hs_connectors and not plan.differential_pairs:
        reasons.append("high-speed connector(s) present but no differential pairs were inferred")
        score -= 15

    invalid_series = [f for f in plan.topology_failures if "rejected Series()" in f]
    if invalid_series:
        reasons.append(f"{len(invalid_series)} Series() inference(s) were rejected as invalid")
        score -= 10

    if plan.board.source == "footprint_extents":
        reasons.append("board geometry was inferred from footprint extents (no board.pln or Edge.Cuts available)")
        score -= 15

    score = max(0, min(100, score))
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "reasons": reasons}


# Map internal BoardGeometry.source strings to the stable report vocabulary.
_GEOMETRY_SOURCE_LABELS = {
    "cli": "cli",
    "board.pln": "pln",
    "placement_file": "pln",
    "edge_cuts": "edge_cuts",
    "footprint_extents": "footprint_extents",
}


def geometry_source_label(source: str) -> str:
    return _GEOMETRY_SOURCE_LABELS.get(source, source)


def _validate_board_geometry(board: BoardGeometry) -> None:
    """Reject only when the final resolved width/height is non-positive."""

    if board.width <= 0 or board.height <= 0:
        raise PlacementError(
            f"invalid board geometry: width={board.width:g} height={board.height:g} "
            f"source={geometry_source_label(board.source)}")


def board_geometry_report(board: BoardGeometry) -> Dict[str, Any]:
    """Report block describing the resolved board geometry and its provenance."""

    source = geometry_source_label(board.source)
    return {
        "source": source,
        "width": board.width,
        "height": board.height,
        "origin_x": board.origin_x,
        "origin_y": board.origin_y,
        "margin_applied": DEFAULT_FOOTPRINT_MARGIN_MM if source == "footprint_extents" else 0.0,
    }


def report(plan: Plan) -> Dict[str, Any]:
    openems = _as_mapping(plan.simulation.get("openems"))
    placed_refs = {ref for rule in plan.rules for ref in rule.refs if ref in plan.components}
    unplaced = [ref for ref in plan.components if ref not in placed_refs]
    single_clusters = sum(1 for c in plan.clusters if len(c.get("members", [])) <= 1)
    multi_clusters = sum(1 for c in plan.clusters if len(c.get("members", [])) > 1)
    rule_count = lambda kind: sum(1 for r in plan.rules if r.kind == kind)
    quality_warnings = list(plan.warnings)
    if plan.components and len(unplaced) / len(plan.components) > 0.5:
        quality_warnings.append(f"Most components are unplaced ({len(unplaced)}/{len(plan.components)}); review connectivity and semantic intent.")
    primitive_counts: Dict[str, int] = {}
    for r in plan.rules:
        primitive = r.text.split("(", 1)[0].strip()
        primitive_counts[primitive] = primitive_counts.get(primitive, 0) + 1
    total_clusters = single_clusters + multi_clusters
    cluster_quality_score = (multi_clusters / total_clusters) if total_clusters else 1.0
    ownership = compute_ownership(plan.rules, plan.components)
    semantic_groups = compute_semantic_groups(plan.clusters, plan.functional_paths,
                                              plan.power_islands, plan.decoupling_groups)
    return {
        "board_geometry": board_geometry_report(plan.board),
        "components_parsed": len(plan.components),
        "nets_parsed": len(plan.nets),
        "components_placed": len(placed_refs),
        "components_unplaced": len(unplaced),
        "spacing_profile": dict(plan.spacing),
        "functional_paths": plan.functional_paths,
        "high_speed_paths": {name: path for name, path in plan.functional_paths.items()
                             if "high_speed" in str(path.get("type", ""))},
        "power_islands": plan.power_islands,
        "mechanical_constraints": {
            "mounting_holes": plan.mounting_hole_plan,
            "keepouts": plan.keepouts,
        },
        "edge_required_components": [item["ref"] for item in plan.edge_rotation_plan if item.get("edge_required")],
        "edge_rotation_plan": plan.edge_rotation_plan,
        "ownership_model": ownership,
        "semantic_groups": semantic_groups,
        "multi_group_components": sorted(ref for ref, groups in semantic_groups.items() if len(groups) > 1),
        "clusters_are_metadata": "Clusters preserve neighborhoods only; they are not atomic placement units. "
                                 "Each ref has exactly one placement owner (see ownership_model).",
        "clusters_single_member": single_clusters,
        "clusters_multi_member": multi_clusters,
        "diff_pairs_inferred": len(plan.differential_pairs),
        "generated_decoupling_rules": rule_count("decoupling"),
        "generated_esd_rules": rule_count("esd"),
        "generated_pullup_rules": rule_count("pullup"),
        "generated_series_rules": rule_count("series"),
        "aliases_recovered": plan.aliases,
        "alias_diagnostics": dataclasses.asdict(plan.alias_diagnostics),
        "inferred_roles": plan.roles,
        "inferred_clusters": plan.clusters,
        "inferred_differential_pairs": [dataclasses.asdict(p) for p in plan.differential_pairs],
        "generated_rules": [dataclasses.asdict(r) for r in plan.rules],
        "warnings": quality_warnings,
        "unplaced_components": unplaced,
        "uncertain_inferences": plan.uncertain_inferences,
        "primitive_counts": primitive_counts,
        "duplicate_rules": plan.duplicate_rules,
        "failed_topology_inference": plan.topology_failures,
        "cluster_quality_score": cluster_quality_score,
        "decoupling_groups": plan.decoupling_groups,
        "pullup_groups": plan.pullup_groups,
        "plan_confidence": compute_plan_confidence(plan),
        "series_components_validated": rule_count("series"),
        "routing": {
            "mode": plan.routing.get("mode"),
            "classes": _as_mapping(plan.routing.get("classes")),
            "defaults": _as_mapping(plan.routing.get("defaults")),
            "differential_pairs": plan.declared_differential_pairs,
            "net_classes": plan.net_classes,
            "routing_overrides": plan.routing_overrides,
            "warnings": plan.routing_warnings,
            "high_speed_constraints_complete": plan.high_speed_constraints_complete,
        },
        "stackup": {
            "layers": _as_list(plan.stackup.get("layers")),
            "dielectric": _as_list(plan.stackup.get("dielectric")),
            "reference_planes": _stackup_reference_planes(plan.stackup),
            "warnings": plan.stackup_warnings,
        },
        "simulation": {
            "openems_enabled": _simulation_openems_enabled(openems),
            "triggers": _as_list(openems.get("trigger_on")),
            "export_dir": openems.get("export_dir"),
            "warnings": plan.simulation_warnings,
        },
    }


def build_check_report(plan: Plan) -> Dict[str, Any]:
    """Plan-quality report for `pcb-plan check`: evaluates plan quality without
    writing placement.ppl. See compute_plan_confidence() for the confidence score."""

    payload = report(plan)
    invalid_series = [f for f in plan.topology_failures if "rejected Series()" in f]
    confidence = payload["plan_confidence"]
    cluster_quality = payload["cluster_quality_score"]
    placement_quality_score = round(confidence["score"] * 0.7 + cluster_quality * 100 * 0.3, 1)
    return {
        "mechanical_constraints": payload["mechanical_constraints"],
        "edge_required_components": payload["edge_required_components"],
        "edge_rotation_plan": payload["edge_rotation_plan"],
        "functional_paths": payload["functional_paths"],
        "high_speed_paths": payload["high_speed_paths"],
        "power_islands": payload["power_islands"],
        "ownership_model": payload["ownership_model"],
        "spacing_profile": payload["spacing_profile"],
        "clusters_are_metadata": payload["clusters_are_metadata"],
        "multi_group_components": payload["multi_group_components"],
        "placement_quality_score": placement_quality_score,
        "nets_parsed": payload["nets_parsed"],
        "components_parsed": payload["components_parsed"],
        "components_planned": payload["components_placed"],
        "unplaced_components": payload["unplaced_components"],
        "clusters_total": payload["clusters_single_member"] + payload["clusters_multi_member"],
        "single_member_clusters": payload["clusters_single_member"],
        "multi_member_clusters": payload["clusters_multi_member"],
        "duplicate_rule_refs": payload["duplicate_rules"],
        "decoupling_groups": payload["decoupling_groups"],
        "pullup_groups": payload["pullup_groups"],
        "series_components_validated": payload["series_components_validated"],
        "invalid_series_candidates": invalid_series,
        "differential_pairs_inferred": payload["diff_pairs_inferred"],
        "high_speed_constraints_complete": payload["routing"]["high_speed_constraints_complete"],
        "simulation_triggers": payload["simulation"]["triggers"],
        "warnings": payload["warnings"],
        "plan_confidence": payload["plan_confidence"],
    }


def explain_text(plan: Plan, ref: str) -> str:
    ref = _normalize_ref(ref)
    comp = plan.components.get(ref)
    if not comp:
        raise PlacementError(f"unknown reference {ref!r}")
    data = plan.explanations.get(ref, {})
    rule = data.get("generated_rule") or next((r.text for r in plan.rules if ref in r.refs), "none")
    parent = data.get("parent_candidate") or data.get("protected") or "none"
    lines = [f"{ref}:", f"  role: {comp.role}", f"  nets: {', '.join(comp.nets) if comp.nets else 'none'}", f"  parent candidate: {parent}", f"  generated rule: {rule}"]
    if comp.role_reasons:
        lines.append(f"  reason: {'; '.join(comp.role_reasons)}")
    return "\n".join(lines) + "\n"



def provenance_value(value: Any, source: str, confidence: str = "medium", requires_review: bool = True) -> Dict[str, Any]:
    """Represent an inferred board.pln value with visible provenance."""
    return {
        "value": value,
        "source": source,
        "confidence": confidence,
        "requires_review": requires_review,
    }


def unwrap_provenance(value: Any) -> Any:
    """Convert board.pln provenance wrappers into plain values for planners."""
    if isinstance(value, Mapping):
        keys = set(value.keys())
        if {"value", "source", "confidence", "requires_review"}.issubset(keys):
            return unwrap_provenance(value.get("value"))
        return {str(k): unwrap_provenance(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap_provenance(v) for v in value]
    return value


def load_pln(path: Optional[Path]) -> Dict[str, Any]:
    return load_intent(path)


def serialize_pln(payload: Mapping[str, Any]) -> str:
    return emit_yaml(payload)


def validate_pln(payload: Mapping[str, Any]) -> List[str]:
    warnings: List[str] = []
    allowed = {
        "board", "regions", "keepouts", "roles", "clusters", "high_speed", "routing",
        "stackup", "differential_pairs", "net_classes", "simulation", "provenance",
        "fixed", "routing_overrides", "spacing", "components", "mechanical",
        "functional_paths", "power_islands",
    }
    for key in payload:
        if key not in allowed:
            warnings.append(f"Unknown board.pln section {key!r}.")
    board = _as_mapping(payload.get("board"))
    for field in ("width", "height"):
        value = unwrap_provenance(board.get(field))
        if value is not None and not _is_positive_number(value):
            warnings.append(f"board.{field} must be positive.")
    return warnings


def _prov_dict_from_mapping(spec: Mapping[str, Any], source: str, confidence: str = "medium", review: bool = True) -> Dict[str, Any]:
    return {str(k): provenance_value(v, source, confidence, review) for k, v in spec.items()}


def infer_stackup_candidates(plan: Plan) -> Dict[str, Any]:
    if plan.stackup:
        return plan.stackup
    has_high_speed = bool(plan.differential_pairs) or any(is_high_speed(n) for n in plan.nets)
    if not has_high_speed:
        return {
            "layers": [
                {"name": "F.Cu", "type": "signal"},
                {"name": "B.Cu", "type": "signal"},
            ],
            "notes": provenance_value("2-layer default candidate; review for impedance-controlled designs", "inferred_from_board_complexity", "low", True),
        }
    return {
        "layers": [
            {"name": "F.Cu", "type": "signal"},
            {"name": "In1.GND", "type": "plane", "net": "GND"},
            {"name": "In2.PWR", "type": "plane"},
            {"name": "B.Cu", "type": "signal"},
        ],
        "dielectric": [
            {"between": ["F.Cu", "In1.GND"], "material": "FR4", "thickness_mm": provenance_value(0.18, "candidate_for_high_speed_impedance", "low", True), "er": provenance_value(4.2, "generic_fr4_assumption", "low", True)}
        ],
        "notes": provenance_value("4-layer controlled-impedance candidate inferred from high-speed nets", "inferred_from_high_speed_nets", "medium", True),
    }


def infer_simulation_config(plan: Plan) -> Dict[str, Any]:
    existing = _as_mapping(plan.simulation)
    openems_triggers: List[str] = []
    ngspice_triggers: List[str] = []
    net_names = " ".join(plan.nets.keys()).upper()
    if any(token in net_names for token in ("HDMI", "USB", "ETH", "RF", "TMDS", "SSTX", "SSRX")) or plan.differential_pairs:
        openems_triggers.append("high_speed_or_differential_interface")
    if any(c.role in {"regulator", "power", "filter", "oscillator"} for c in plan.components.values()):
        ngspice_triggers.append("power_or_analog_network")
    if existing:
        return existing
    return {
        "openems": {
            "enabled": provenance_value("auto", "inferred_from_interfaces", "medium" if openems_triggers else "low", True),
            "trigger_on": [provenance_value(t, "inferred_from_interfaces", "medium", True) for t in openems_triggers],
        },
        "ngspice": {
            "enabled": provenance_value("auto", "inferred_from_roles", "medium" if ngspice_triggers else "low", True),
            "trigger_on": [provenance_value(t, "inferred_from_roles", "medium", True) for t in ngspice_triggers],
        },
    }


def infer_routing_constraints(plan: Plan) -> Dict[str, Any]:
    if plan.routing:
        return plan.routing
    classes: Dict[str, Any] = {
        "low_speed": {
            "trace_width_mm": provenance_value(0.15, "default_low_speed_candidate", "low", True),
            "clearance_mm": provenance_value(0.15, "default_low_speed_candidate", "low", True),
            "preferred_layers": [provenance_value("F.Cu", "default_low_speed_candidate", "low", True), provenance_value("B.Cu", "default_low_speed_candidate", "low", True)],
            "via_policy": provenance_value("allow", "default_low_speed_candidate", "low", True),
        }
    }
    if plan.differential_pairs:
        classes["high_speed_diff"] = {
            "differential": provenance_value(True, "inferred_from_differential_pairs", "medium", True),
            "impedance_ohms": provenance_value(100, "inferred_from_high_speed_diff", "medium", True),
            "trace_width_mm": provenance_value(0.12, "candidate_requires_stackup_solver", "low", True),
            "trace_spacing_mm": provenance_value(0.15, "candidate_requires_stackup_solver", "low", True),
            "reference_plane": provenance_value("In1.GND", "candidate_4_layer_stackup", "low", True),
            "preferred_layer": provenance_value("F.Cu", "candidate_4_layer_stackup", "low", True),
            "max_skew_mm": provenance_value(0.25, "inferred_from_high_speed_diff", "medium", True),
            "via_policy": provenance_value("avoid", "inferred_from_high_speed_diff", "medium", True),
        }
    return {
        "mode": provenance_value("all_nets_constrained" if plan.differential_pairs else "low_speed_only", "inferred_from_connectivity", "medium", True),
        "classes": classes,
    }


def plan_to_pln(plan: Plan) -> Dict[str, Any]:
    pairs = {
        pair.name: {
            "p": provenance_value(pair.p, "inferred_from_net_names", "medium", True),
            "n": provenance_value(pair.n, "inferred_from_net_names", "medium", True),
            "components": [provenance_value(ref, "inferred_from_pair_connectivity", "medium", True) for ref in pair.components],
            "class": provenance_value("high_speed_diff", "inferred_from_net_names", "medium", True),
        }
        for pair in plan.differential_pairs
    }
    high_speed_nets = sorted(n for n in plan.nets if is_high_speed(n))
    trusted_board_source = plan.board.source in {"edge_cuts", "cli"}
    board_source_label = "supplied_by_cli" if plan.board.source == "cli" else f"inferred_from_{plan.board.source}"
    payload: Dict[str, Any] = {
        "board": {
            "width": provenance_value(plan.board.width, board_source_label, "high" if trusted_board_source else "medium", not trusted_board_source),
            "height": provenance_value(plan.board.height, board_source_label, "high" if trusted_board_source else "medium", not trusted_board_source),
            "origin_x": provenance_value(plan.board.origin_x, board_source_label, "high" if trusted_board_source else "medium", not trusted_board_source),
            "origin_y": provenance_value(plan.board.origin_y, board_source_label, "high" if trusted_board_source else "medium", not trusted_board_source),
            "units": provenance_value("mm", "kicad_default", "high", False),
        },
        "regions": {name: _prov_dict_from_mapping(spec, "inferred_from_board_geometry", "medium", True) for name, spec in plan.regions.items()},
        "keepouts": [_prov_dict_from_mapping(k, "inferred_from_component_roles", "medium", True) for k in plan.keepouts],
        "roles": {ref: provenance_value(role, "; ".join(plan.components[ref].role_reasons) or "inferred_from_reference_and_nets", "medium" if role != "unknown" else "low", role == "unknown") for ref, role in plan.roles.items()},
        "clusters": [_prov_dict_from_mapping(c, "inferred_from_connectivity", "medium", True) for c in plan.clusters],
        "high_speed": {
            "nets": [provenance_value(n, "inferred_from_net_names", "medium", True) for n in high_speed_nets],
            "interfaces": [provenance_value(p.name, "inferred_from_differential_pairs", "medium", True) for p in plan.differential_pairs],
        },
        "routing": infer_routing_constraints(plan),
        "stackup": infer_stackup_candidates(plan),
        "differential_pairs": pairs,
        "net_classes": plan.net_classes,
        "simulation": infer_simulation_config(plan),
        "spacing": {key: provenance_value(value, "conservative_default_spacing_profile", "medium", True)
                    for key, value in plan.spacing.items()},
        "components": {
            item["ref"]: {
                "edge_required": provenance_value(item["edge_required"], "inferred_from_connector_role_and_position", "medium", True),
                "access_side": provenance_value(item["access_side"], "inferred_from_board_position", "medium", True),
                "allow_body_outside_board": provenance_value(item["allow_body_outside_board"], "edge_connector_default", "medium", True),
                "locked": provenance_value(item["locked"], "edge_connector_default", "medium", True),
                "rotation": provenance_value(item["rotation"], "auto_from_access_side", "medium", True),
            }
            for item in plan.edge_rotation_plan
        },
        "mechanical": {
            "mounting_holes": {
                item["ref"]: ({"corner": provenance_value(item["corner"], item["source"], "medium", True),
                               "inset": provenance_value(item.get("inset", 3.0), item["source"], "medium", True)}
                              if item["kind"] == "corner" else
                              {"x": provenance_value(item.get("x"), item["source"], "medium", True),
                               "y": provenance_value(item.get("y"), item["source"], "medium", True)})
                for item in plan.mounting_hole_plan
            },
        },
        "functional_paths": {
            name: {
                "type": provenance_value(path["type"], path["source"], "medium", True),
                "sequence": [provenance_value(ref, path["source"], "medium", True) for ref in path["sequence"]],
                "corridor_width_mm": provenance_value(path["corridor_width_mm"], path["source"], "medium", True),
                "protect_first": provenance_value(path["protect_first"], path["source"], "medium", True),
            }
            for name, path in plan.functional_paths.items()
        },
        "power_islands": {
            island["name"]: {
                "regulator": provenance_value(island["regulator"], island["source"], "medium", True),
                "input_caps": [provenance_value(r, island["source"], "medium", True) for r in island.get("input_caps", [])],
                "inductor": provenance_value(island.get("inductor"), island["source"], "medium", True),
                "output_caps": [provenance_value(r, island["source"], "medium", True) for r in island.get("output_caps", [])],
                "feedback": [provenance_value(r, island["source"], "medium", True) for r in island.get("feedback", [])],
                "switch_node": provenance_value(island.get("switch_net"), island["source"], "medium", True),
            }
            for island in plan.power_islands
        },
        "provenance": {
            "generator": "pcb-plan init",
            "schema_version": "0.2",
            "visibility": "inferred values include value/source/confidence/requires_review wrappers where practical",
            "ai_editing": "board.pln is intended to be reviewed and edited by humans and AI assistants; "
                          "sections are stably ordered for diffs and carry rationale via source fields",
        },
    }
    return payload


def pln_report(plan: Plan, pln_payload: Mapping[str, Any], action: str) -> Dict[str, Any]:
    base = report(plan)
    review_required: List[str] = []
    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, Mapping):
            if value.get("requires_review") is True and "value" in value:
                review_required.append(path or "<root>")
            for k, v in value.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
    walk(pln_payload)
    base.update({
        "action": action,
        "pln_schema_sections": sorted(pln_payload.keys()),
        "pln_validation_warnings": validate_pln(pln_payload),
        "review_required_items": review_required,
    })
    return base


def review_pln_text(payload: Mapping[str, Any]) -> str:
    warnings = validate_pln(payload)
    plain = unwrap_provenance(payload)
    lines = ["board.pln review", "================", ""]
    board = _as_mapping(plain.get("board"))
    if board:
        lines.append(f"Board: {board.get('width', '?')} x {board.get('height', '?')} {board.get('units', 'mm')} at origin ({board.get('origin_x', '?')}, {board.get('origin_y', '?')})")
    lines.append("")
    for title, key in (("Inferred constraints", "routing"), ("Stackup", "stackup"), ("Simulation", "simulation"), ("Differential pairs", "differential_pairs"), ("Roles", "roles"), ("Regions", "regions"), ("Keepouts", "keepouts")):
        lines.append(title + ":")
        value = plain.get(key, {})
        rendered = emit_yaml(value).rstrip().splitlines() if value else ["  none"]
        lines.extend(("  " + r if r else r) for r in rendered[:80])
        lines.append("")
    lines.append("Provenance and confidence:")
    def provenance_lines(value: Any, path: str = "") -> Iterable[str]:
        if isinstance(value, Mapping):
            if "value" in value and "source" in value:
                yield f"  {path}: source={value.get('source')}, confidence={value.get('confidence')}, requires_review={value.get('requires_review')}"
            else:
                for k, v in value.items():
                    yield from provenance_lines(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from provenance_lines(v, f"{path}[{i}]")
    prov = list(provenance_lines(payload))
    lines.extend(prov[:120] if prov else ["  none"])
    lines.append("")
    lines.append("Warnings:")
    lines.extend(f"  - {w}" for w in warnings) if warnings else lines.append("  none")
    lines.append("")
    lines.append("Missing information / review-required items:")
    review = [line.split(":", 1)[0].strip() for line in prov if "requires_review=True" in line]
    lines.extend(f"  - {item}" for item in review[:120]) if review else lines.append("  none")
    return "\n".join(lines) + "\n"


def explain_pln_text(payload: Mapping[str, Any], query: str) -> str:
    plain = unwrap_provenance(payload)
    query_norm = _normalize_ref(query) if re.match(r"^[A-Za-z]+\d+$", query) else query
    results: List[Tuple[str, Any, Any]] = []
    def walk(raw: Any, unwrapped: Any, path: str = "") -> None:
        tail = path.rsplit(".", 1)[-1]
        if path == query or tail == query or tail == query_norm or (isinstance(unwrapped, str) and unwrapped == query):
            results.append((path, raw, unwrapped))
        if isinstance(raw, Mapping):
            for k, v in raw.items():
                walk(v, unwrap_provenance(v), f"{path}.{k}" if path else str(k))
        elif isinstance(raw, list):
            for i, v in enumerate(raw):
                walk(v, unwrap_provenance(v), f"{path}[{i}]")
    walk(payload, plain)
    if not results:
        raise PlacementError(f"unknown board.pln object {query!r}")
    lines = [f"{query}:"]
    for path, raw, unwrapped in results[:12]:
        lines.append(f"  path: {path}")
        lines.append(f"  value: {json.dumps(unwrapped, sort_keys=True)}")
        if isinstance(raw, Mapping) and "source" in raw:
            lines.append(f"  why it exists: {raw.get('source')}")
            lines.append(f"  confidence: {raw.get('confidence')}")
            lines.append(f"  requires_review: {raw.get('requires_review')}")
        else:
            lines.append("  why it exists: explicit or grouped board.pln object")
        lines.append("")
    return "\n".join(lines)


def apply_feedback_updates(payload: Mapping[str, Any], reports: Mapping[str, Any]) -> Dict[str, Any]:
    updated = json.loads(json.dumps(payload))
    proposals = updated.setdefault("provenance", {}).setdefault("update_proposals", [])
    sim = updated.setdefault("simulation", {})
    if reports.get("openems"):
        proposals.append({
            "type": "openems_feedback",
            "value": "review routing corridors, reference planes, layer preferences, keepouts, and via restrictions",
            "source": "openems_report",
            "confidence": "medium",
            "requires_review": True,
        })
        sim.setdefault("openems", {}).setdefault("feedback", provenance_value("openems report supplied; inspect proposed EM/SI changes", "openems_report", "medium", True))
    if reports.get("ngspice"):
        proposals.append({
            "type": "ngspice_feedback",
            "value": "review power regions, regulator annotations, startup sequencing, filters, and analog networks",
            "source": "ngspice_report",
            "confidence": "medium",
            "requires_review": True,
        })
        sim.setdefault("ngspice", {}).setdefault("feedback", provenance_value("ngspice report supplied; inspect proposed circuit-behavior changes", "ngspice_report", "medium", True))
    if reports.get("place"):
        proposals.append({
            "type": "placement_feedback",
            "value": "review placement warnings for new keepouts, region changes, or cluster changes",
            "source": "pcb_place_report",
            "confidence": "medium",
            "requires_review": True,
        })
    if reports.get("routing"):
        proposals.append({
            "type": "routing_feedback",
            "value": "review routing DRC/congestion for class, via-policy, layer, skew, and length-tolerance adjustments",
            "source": "routing_report",
            "confidence": "medium",
            "requires_review": True,
        })
    return updated


def unified_diff_text(old: str, new: str, old_name: str = "board.pln", new_name: str = "board.updated.pln") -> str:
    import difflib
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=old_name, tofile=new_name))


def _load_optional_json(path: Optional[Path]) -> Any:
    if not path:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_plan_from_inputs(board_path: Path, netlist_path: Optional[Path], pln_path: Optional[Path],
                           *, board_override: Optional[Mapping[str, float]] = None) -> Tuple[Plan, Dict[str, Any]]:
    board, components, nets, warnings = parse_board(board_path)
    aliases, alias_diag, net_warnings = import_netlist(netlist_path, components, nets)
    warnings.extend(net_warnings)
    raw_intent = load_pln(pln_path)
    intent = unwrap_provenance(raw_intent)
    if isinstance(intent.get("board"), dict):
        b = intent["board"]
        ox = b.get("origin_x", board.origin_x)
        oy = b.get("origin_y", board.origin_y)
        if isinstance(b.get("origin"), list) and len(b["origin"]) >= 2:
            ox, oy = b["origin"][0], b["origin"][1]
        board = BoardGeometry(origin_x=float(ox), origin_y=float(oy), width=float(b.get("width", board.width)), height=float(b.get("height", board.height)), source="board.pln")
        warnings = [w for w in warnings if "footprint extents" not in w]
    if board_override is not None:
        board = BoardGeometry(
            origin_x=float(board_override.get("origin_x", board.origin_x)),
            origin_y=float(board_override.get("origin_y", board.origin_y)),
            width=float(board_override.get("width", board.width)),
            height=float(board_override.get("height", board.height)),
            source="cli",
        )
        warnings = [w for w in warnings if "footprint extents" not in w]
    _validate_board_geometry(board)
    plan = generate_plan(board, components, nets, aliases, alias_diag, intent, warnings)
    return plan, raw_intent

def _add_common_emit_args(parser: argparse.ArgumentParser, *, board_required: bool = True) -> None:
    parser.add_argument("--board", required=board_required, type=Path, help="Input KiCad .kicad_pcb board")
    parser.add_argument("--netlist", type=Path, help="Optional Zener/pcb netlist artifact")
    parser.add_argument("-o", "--output", type=Path, help="Output path")
    parser.add_argument("--report-json", type=Path, help="Write planner report JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcb-plan", description="Own board.pln lifecycle and generate reviewable pcb-place .ppl placement plans")
    parser.add_argument("--version", action="version", version=f"pcb-plan {__version__}")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Generate an initial board.pln")
    _add_common_emit_args(init)
    init.add_argument("--width", type=float, help="Override board geometry width in mm for the generated board.pln")
    init.add_argument("--height", type=float, help="Override board geometry height in mm for the generated board.pln")
    init.add_argument("--origin-x", type=float, help="Override board origin X in mm when --width/--height are supplied")
    init.add_argument("--origin-y", type=float, help="Override board origin Y in mm when --width/--height are supplied")
    init.add_argument("--summary-md", type=Path, help="Write human-readable init summary markdown")

    inspect = sub.add_parser("inspect", help="Extract board intelligence into planning-hints/ for AI board.pln generation")
    inspect.add_argument("--board", required=True, type=Path, help="Input KiCad .kicad_pcb board")
    inspect.add_argument("--netlist", type=Path, help="Optional Zener/pcb netlist artifact for connectivity")
    inspect.add_argument("--width", type=float, help="Override board width in mm")
    inspect.add_argument("--height", type=float, help="Override board height in mm")
    inspect.add_argument("--origin-x", type=float, help="Override board origin X in mm")
    inspect.add_argument("--origin-y", type=float, help="Override board origin Y in mm")
    inspect.add_argument("--stackup-layers", type=int, help="Optional known layer count to inform stackup assumptions")
    inspect.add_argument("--out", required=True, type=Path, help="Output planning-hints directory")

    update = sub.add_parser("update", help="Update board.pln from feedback reports")
    update.add_argument("--pln", required=True, type=Path, help="Existing board.pln")
    update.add_argument("--board", type=Path, help="Optional KiCad board for regenerated report context")
    update.add_argument("--place-report", type=Path, help="Optional pcb-place report JSON")
    update.add_argument("--routing-report", type=Path, help="Optional routing report JSON")
    update.add_argument("--openems-report", type=Path, help="Optional OpenEMS report JSON")
    update.add_argument("--ngspice-report", type=Path, help="Optional ngspice report JSON")
    update.add_argument("-o", "--output", required=True, type=Path, help="Updated board.pln path")
    update.add_argument("--patch", type=Path, help="Write human-reviewable unified diff patch")
    update.add_argument("--report-json", type=Path, help="Write update report JSON")
    update.add_argument("--summary-md", type=Path, help="Write update summary markdown")

    review = sub.add_parser("review", help="Review board.pln")
    review.add_argument("--pln", required=True, type=Path, help="board.pln to review")

    explain = sub.add_parser("explain", help="Explain a board.pln object")
    explain.add_argument("object", help="Reference, net, or dotted board.pln path")
    explain.add_argument("--pln", type=Path, default=Path("board.pln"), help="board.pln to explain (default: board.pln)")
    explain.add_argument("--board", type=Path, help="Optional board for legacy component explanations")
    explain.add_argument("--netlist", type=Path, help="Optional netlist for legacy component explanations")

    emit = sub.add_parser("emit", help="Generate placement.ppl from board.pln and board inputs")
    emit.add_argument("--pln", "--intent", dest="pln", type=Path, help="board.pln planning-intent file")
    _add_common_emit_args(emit)
    emit.add_argument("--emit-routing-policy", type=Path, help="Write routing-policy.yaml handoff from .pln routing constraints")
    emit.add_argument("--emit-openems-plan", type=Path, help="Write OpenEMS handoff plan when simulation.openems is enabled")
    emit.add_argument("--summary-md", type=Path, help="Write human-readable summary markdown")
    emit.add_argument("--ai-edit-hints", type=Path, help="Write ai-edit-hints.md listing uncertain inferences, ownership, and suggested .pln/.ppl edits")
    emit.add_argument("--strict-confidence", action="store_true",
                      help="Fail if the plan is low-confidence (requires --allow-low-confidence to proceed anyway)")
    emit.add_argument("--allow-low-confidence", action="store_true",
                      help="Acknowledge a low-confidence plan and proceed despite --strict-confidence")

    check = sub.add_parser("check", help="Evaluate plan quality without writing placement.ppl")
    check.add_argument("--pln", "--intent", dest="pln", type=Path, help="board.pln planning-intent file")
    check.add_argument("--board", required=True, type=Path, help="Input KiCad .kicad_pcb board")
    check.add_argument("--netlist", type=Path, help="Optional Zener/pcb netlist artifact")
    check.add_argument("--report-json", type=Path, help="Write plan quality report JSON")

    # Legacy one-shot placement generation flags kept for compatibility.
    parser.add_argument("--board", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--netlist", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--intent", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("-o", "--output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report-json", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--emit-routing-policy", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--emit-openems-plan", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--explain", help=argparse.SUPPRESS)
    return parser


def _ensure_exists(path: Optional[Path], label: str) -> None:
    if path and not path.exists():
        raise SystemExit(f"{label} file not found: {path}")


def _write_report(path: Optional[Path], payload: Mapping[str, Any]) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Optional[Path], text: str) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _summary_markdown(title: str, payload: Mapping[str, Any]) -> str:
    report_payload = _as_mapping(payload.get("report")) or payload
    warnings = _as_list(report_payload.get("warnings")) + _as_list(report_payload.get("pln_validation_warnings"))
    review_items = _as_list(report_payload.get("review_required_items"))
    lines = [f"# {title}", "", "## Inferred roles", ""]
    roles = _as_mapping(report_payload.get("inferred_roles"))
    if roles:
        lines.extend(f"- `{ref}`: `{role}`" for ref, role in sorted(roles.items()))
    else:
        lines.append("- none")
    lines.extend(["", "## Inferred constraints", ""])
    routing = _as_mapping(report_payload.get("routing"))
    if routing:
        lines.append("```yaml")
        lines.append(emit_yaml(routing).rstrip())
        lines.append("```")
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {w}" for w in warnings) if warnings else lines.append("- none")
    lines.extend(["", "## Review-required items", ""])
    lines.extend(f"- `{item}`" for item in review_items[:200]) if review_items else lines.append("- none")
    return "\n".join(lines) + "\n"


def ai_edit_hints_text(plan: Plan) -> str:
    """ai-edit-hints.md: what an AI/human editor should revisit in board.pln/placement.ppl."""

    payload = report(plan)
    lines = ["# AI edit hints (pcb-plan)", "",
             "board.pln and placement.ppl are intended to be edited by humans and AI",
             "assistants as part of an iterative layout optimization workflow. The items",
             "below are the lowest-confidence decisions and the highest-value edits.", ""]

    lines.append("## Uncertain placement choices")
    lines.append("")
    uncertain = list(plan.uncertain_inferences)
    if uncertain:
        lines.extend(f"- {item}" for item in uncertain)
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Low-confidence inferred constraints")
    lines.append("")
    confidence = payload["plan_confidence"]
    lines.append(f"- plan confidence: {confidence['level']} (score {confidence['score']})")
    lines.extend(f"- {reason}" for reason in confidence["reasons"])
    if plan.stackup.get("source") == "stackup_template":
        lines.append(f"- stackup expanded from template {plan.stackup.get('profile')!r}: layer roles are intent only; "
                     "controlled impedance requires the actual fab stackup")
    lines.append("")

    lines.append("## Components with multiple semantic groups")
    lines.append("")
    multi = payload["multi_group_components"]
    groups = payload["semantic_groups"]
    if multi:
        for ref in multi:
            owner = payload["ownership_model"].get(ref, {})
            lines.append(f"- {ref}: groups [{', '.join(groups[ref])}]; placement owner: "
                         f"{owner.get('owner', 'unowned')} (groups are metadata; ownership is unique)")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Placement owners")
    lines.append("")
    for ref in sorted(payload["ownership_model"]):
        owner = payload["ownership_model"][ref]
        lines.append(f"- {ref}: {owner['owner']} (priority rank {owner['rank']})")
    lines.append("")

    lines.append("## Suggested board.pln edits")
    lines.append("")
    suggestions: List[str] = []
    for item in plan.edge_rotation_plan:
        suggestions.append(f"- confirm components.{item['ref']}: access_side={item['access_side']}, "
                           f"allow_body_outside_board={item['allow_body_outside_board']} match the enclosure")
    for hole in plan.mounting_hole_plan:
        if hole["source"] in {"distributed_to_distinct_corner", "no_free_corner"}:
            suggestions.append(f"- confirm mechanical.mounting_holes.{hole['ref']} "
                               f"({hole.get('corner', 'explicit location')}) against the mechanical design")
    for island in plan.power_islands:
        if island["source"] != "board.pln":
            suggestions.append(f"- review inferred power island {island['name']} membership "
                               "(input_caps/inductor/output_caps/feedback) and add a power_islands: entry to pin it")
    for name, path in plan.functional_paths.items():
        if path["source"] != "board.pln":
            suggestions.append(f"- review inferred functional path {name}: {' -> '.join(path['sequence'])}; "
                               "add a functional_paths: entry to pin the sequence and corridor width")
    lines.extend(suggestions if suggestions else ["- none"])
    lines.append("")

    lines.append("## Suggested placement.ppl edits")
    lines.append("")
    ppl_suggestions = [f"- {item}" for item in plan.topology_failures]
    unplaced = payload["unplaced_components"]
    if unplaced:
        ppl_suggestions.append(f"- add explicit rules for unplaced refs: {', '.join(sorted(unplaced))}")
    lines.extend(ppl_suggestions if ppl_suggestions else ["- none"])
    lines.append("")

    lines.append("## Risks requiring engineering review")
    lines.append("")
    lines.append("- Placement heuristics cannot verify impedance, return paths, plane splits, thermal, or EMI compliance.")
    lines.append("- Stackup templates never claim impedance accuracy; validate with the fabricator.")
    lines.append("- Run pcb-place with --high-speed-review/--power-review/--mechanical-review and inspect the output.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# pcb-plan inspect: board-intelligence extraction -> planning-hints/
#
# `inspect` is a board-intelligence *extractor*, not a floorplanner. It reads
# facts (geometry, components, connectivity) and produces *candidate* hints for
# an AI (or human) to turn into an authoritative board.pln. It never performs
# placement, never creates placement ownership, and never infers a final
# floorplan. Everything it emits is explicitly marked as a hint/candidate.
# ---------------------------------------------------------------------------

# At rot=0 a connector's mating face points toward the top board edge (-y);
# auto-rotation maps the chosen access side onto that convention.
_AUTO_ROTATION_BY_SIDE: Dict[str, float] = {"top": 0.0, "right": 90.0, "bottom": 180.0, "left": 270.0}


def _candidate_role(comp: PlanComponent) -> str:
    """Refine the coarse inferred role into a more specific *candidate* role hint.

    These are hints only. They never drive placement; they help an AI map a
    component to design intent in board.pln.
    """

    haystack = f"{comp.footprint.upper()} {(comp.value or '').upper()} {' '.join(n.upper() for n in comp.nets)}"
    role = comp.role
    if "connector" in role or role == "debug_header":
        if "HDMI" in haystack:
            return "hdmi_connector"
        if "USB" in haystack:
            return "usb_connector"
        if any(tok in haystack for tok in ("RJ45", "ETHERNET", "ETH", "MAGJACK")):
            return "ethernet_connector"
        if any(tok in haystack for tok in ("BARREL", "DC_JACK", "DCJACK", "DC-JACK", "PJ-")):
            return "dc_jack"
        if role == "debug_header":
            return "debug_header"
        return "edge_connector" if role == "high_speed_connector" else "connector"
    if role == "hdmi_retimer":
        if "REDRIVER" in haystack or "REDRIVE" in haystack:
            return "redriver"
        if "RETIMER" in haystack:
            return "retimer"
        return "retimer"
    if role == "clock":
        return "oscillator"
    if role == "power_regulator":
        return "regulator"
    if role == "decoupling":
        return "decoupling_cap"
    if role == "esd_protection":
        return "esd"
    if role == "pullup_pulldown":
        return "pullup"
    if role == "mechanical":
        return "mounting_hole"
    if role == "ic":
        if "FLASH" in haystack or "EEPROM" in haystack or "NOR" in haystack:
            return "flash"
        return "ic"
    return role


def parse_kicad_groups(text: str, components: Mapping[str, PlanComponent]) -> List[Dict[str, Any]]:
    """Parse KiCad ``(group ...)`` blocks as *metadata only*.

    KiCad groups are preserved for information only and must NOT drive
    placement ownership. Member footprint UUIDs are resolved back to component
    references when possible; unresolved UUIDs are reported verbatim.
    """

    uuid_to_ref = {comp.uuid: ref for ref, comp in components.items() if comp.uuid}
    groups: List[Dict[str, Any]] = []
    idx = 0
    while True:
        start = text.find("(group", idx)
        if start == -1:
            break
        try:
            end = _find_matching_paren(text, start)
        except PlacementError:
            break
        block = text[start:end]
        idx = end
        name_match = re.search(r'\(group\s+"([^"]*)"', block)
        name = name_match.group(1) if name_match else ""
        member_ids: List[str] = []
        for chunk in re.findall(r"\(members\b([^)]*)\)", block):
            member_ids.extend(re.findall(r'"([^"]+)"', chunk))
        member_refs = sorted({uuid_to_ref[u] for u in member_ids if u in uuid_to_ref})
        unresolved = sorted({u for u in member_ids if u not in uuid_to_ref})
        groups.append({
            "name": name,
            "members": member_refs,
            "unresolved_member_uuids": unresolved,
            "metadata_only": True,
        })
    return groups


def inspect_edge_connectors(board: BoardGeometry, components: Mapping[str, PlanComponent]) -> List[Dict[str, Any]]:
    """Candidate edge connectors with access-side and auto-rotation hints.

    Hints only: edge_required/access_side/rotation require confirmation against
    the enclosure. No placement is performed.
    """

    out: List[Dict[str, Any]] = []
    for comp in sorted(components.values(), key=lambda c: c.ref):
        if "connector" not in comp.role and comp.role != "debug_header":
            continue
        local_x = max(0.0, min(board.width, comp.x - board.origin_x))
        local_y = max(0.0, min(board.height, comp.y - board.origin_y))
        distances = {
            "left": local_x,
            "right": board.width - local_x,
            "top": local_y,
            "bottom": board.height - local_y,
        }
        nearest_edge = min(distances, key=distances.get)
        edge_span = board.width if nearest_edge in ("left", "right") else board.height
        inset = 2.0
        near_edge = edge_span > 0 and distances[nearest_edge] / edge_span <= 0.25 and distances[nearest_edge] > inset
        haystack = f"{comp.footprint.upper()} {(comp.value or '').upper()}"
        inferred_edge_required = (comp.role == "high_speed_connector"
                                  or any(token in haystack for token in _EDGE_CONNECTOR_TOKENS))
        candidate_edge_required = bool(near_edge or inferred_edge_required)
        out.append({
            "ref": comp.ref,
            "role": comp.role,
            "candidate_role": _candidate_role(comp),
            "candidate_access_side": nearest_edge,
            "candidate_rotation_auto_deg": _AUTO_ROTATION_BY_SIDE[nearest_edge],
            "candidate_edge_required": candidate_edge_required,
            "near_board_edge": bool(near_edge),
            "distance_to_nearest_edge_mm": round(distances[nearest_edge], 3),
            "allow_body_outside_board_hint": candidate_edge_required,
            "nets": comp.nets,
            "high_speed_nets": [n for n in comp.nets if is_high_speed(n)],
            "metadata_only": True,
            "note": "Candidate edge connector hint; confirm access side, rotation, and "
                    "edge_required against the enclosure before committing in board.pln.",
        })
    return out


def inspect_mechanicals(board: BoardGeometry, components: Mapping[str, PlanComponent]) -> List[Dict[str, Any]]:
    """Candidate mechanical constraints (mounting holes -> distinct corners)."""

    warnings: List[str] = []
    plan_holes = plan_mounting_holes(board, components, {}, warnings)
    out: List[Dict[str, Any]] = []
    for hole in plan_holes:
        item = dict(hole)
        item["metadata_only"] = True
        out.append(item)
    return out


def inspect_rf_zones(board: BoardGeometry, components: Mapping[str, PlanComponent]) -> List[Dict[str, Any]]:
    """Candidate RF antenna keepout zones adjacent to RF modules."""

    out: List[Dict[str, Any]] = []
    for comp in sorted(components.values(), key=lambda c: c.ref):
        if comp.role != "rf_module":
            continue
        rect = _synthesized_rf_keepout(comp, board)
        out.append({
            "ref": comp.ref,
            "candidate_role": _candidate_role(comp),
            "candidate_keepout": rect,
            "openems_candidate": True,
            "metadata_only": True,
            "note": "Candidate RF antenna keepout; reserve clearance and validate detuning/EMI manually.",
        })
    return out


def inspect_connectivity_graph(components: Mapping[str, PlanComponent],
                               nets: Mapping[str, PlanNet]) -> Dict[str, Any]:
    """Component/net connectivity facts plus candidate ownership hints (no placement)."""

    graph = ConnectivityGraph(components, nets)
    ic_roles = {"ic", "mcu", "hdmi_retimer", "rf_module", "power_regulator"}
    ic_candidates = [c for c in components.values() if c.role in ic_roles]
    comp_nodes: Dict[str, Any] = {}
    for ref in sorted(components):
        comp = components[ref]
        neighbors: set[str] = set()
        for net in comp.nets:
            for peer, _pin in graph.net_members(net):
                if peer != ref and peer in components:
                    neighbors.add(peer)
        candidate_owner: Optional[str] = None
        if comp.role in _PASSIVE_SUPPORT_ROLES and comp.nets:
            parent = _nearest_parent(comp, ic_candidates, set(comp.nets))
            candidate_owner = parent.ref if parent is not None else None
        comp_nodes[ref] = {
            "role": comp.role,
            "candidate_role": _candidate_role(comp),
            "nets": comp.nets,
            "neighbors": sorted(neighbors),
            "degree": len(neighbors),
            "candidate_owner_hint": candidate_owner,
        }
    net_nodes: Dict[str, Any] = {}
    for name in sorted(nets):
        net = nets[name]
        members = sorted([list(pad) for pad in net.pads])
        net_nodes[name] = {
            "members": members,
            "degree": len({r for r, _ in net.pads}),
            "is_power": is_power(name),
            "is_ground": is_ground(name),
            "is_high_speed": is_high_speed(name),
        }
    return {
        "note": "Connectivity facts and candidate ownership hints. candidate_owner_hint is a "
                "hint only; pcb-plan inspect never assigns placement ownership.",
        "components": comp_nodes,
        "nets": net_nodes,
    }


def inspect_footprint_bboxes(board: BoardGeometry, components: Mapping[str, PlanComponent]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ref in sorted(components):
        comp = components[ref]
        bbox = comp.bbox
        bbox_block: Optional[Dict[str, float]] = None
        area = 0.0
        if bbox is not None:
            bbox_block = {
                "min_x": bbox.min_x, "min_y": bbox.min_y,
                "max_x": bbox.max_x, "max_y": bbox.max_y,
                "width": bbox.width, "height": bbox.height,
            }
            area = round(bbox.width * bbox.height, 4)
        out[ref] = {
            "footprint": comp.footprint,
            "value": comp.value,
            "layer": comp.layer,
            "x": comp.x,
            "y": comp.y,
            "rotation": comp.rot,
            "centroid": [comp.x, comp.y],
            "bbox": bbox_block,
            "footprint_area_mm2": area,
        }
    return out


def inspect_pad_locations(components: Mapping[str, PlanComponent]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ref in sorted(components):
        comp = components[ref]
        out[ref] = [
            {
                "number": pad.number,
                "name": pad.name,
                "abs_x": pad.abs_x,
                "abs_y": pad.abs_y,
                "local_x": pad.local_x,
                "local_y": pad.local_y,
                "net": pad.net,
                "shape": pad.shape,
                "size": list(pad.size),
                "layers": pad.layers,
            }
            for pad in comp.pads
        ]
    return out


def inspect_component_table_csv(components: Mapping[str, PlanComponent],
                                nets: Mapping[str, PlanNet]) -> str:
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "reference", "value", "footprint", "role", "candidate_role",
        "x", "y", "rotation", "layer", "bbox_w", "bbox_h", "area_mm2",
        "net_degree", "nets",
    ])
    for ref in sorted(components):
        comp = components[ref]
        bw = f"{comp.bbox.width:.4f}" if comp.bbox else ""
        bh = f"{comp.bbox.height:.4f}" if comp.bbox else ""
        area = f"{comp.bbox.width * comp.bbox.height:.4f}" if comp.bbox else ""
        writer.writerow([
            comp.ref, comp.value or "", comp.footprint, comp.role, _candidate_role(comp),
            f"{comp.x:g}", f"{comp.y:g}", f"{comp.rot:g}", comp.layer or "",
            bw, bh, area, len(comp.nets), ";".join(comp.nets),
        ])
    return buf.getvalue()


def inspect_routing_classes(nets: Mapping[str, PlanNet],
                            differential_pairs: Sequence[DifferentialPair]) -> Dict[str, Any]:
    """Candidate routing classes (hints only; impedance requires the fab stackup)."""

    classes: Dict[str, Any] = {
        "low_speed": {
            "trace_width_mm": 0.15,
            "clearance_mm": 0.15,
            "preferred_layers": ["F.Cu", "B.Cu"],
            "via_policy": "allow",
            "source": "candidate_default_low_speed",
        }
    }
    if differential_pairs:
        classes["high_speed_diff"] = {
            "differential": True,
            "impedance_ohms": 100,
            "trace_width_mm": 0.12,
            "trace_spacing_mm": 0.15,
            "via_policy": "avoid",
            "max_skew_mm": 0.25,
            "si_candidate": True,
            "source": "candidate_from_differential_pairs",
        }
    return {
        "note": "Candidate routing classes only. Controlled impedance always requires the "
                "actual fabricator stackup; treat trace widths/spacings as starting hints.",
        "mode": "all_nets_constrained" if differential_pairs else "low_speed_only",
        "classes": classes,
    }


def inspect_stackup_assumptions(nets: Mapping[str, PlanNet],
                                differential_pairs: Sequence[DifferentialPair],
                                layers: Optional[int]) -> Dict[str, Any]:
    """Candidate stackup assumptions (intent only; never impedance-accurate)."""

    has_high_speed = bool(differential_pairs) or any(is_high_speed(n) for n in nets)
    if layers is not None and layers in _LAYER_COUNT_PROFILES:
        profile = STACKUP_PROFILES[_LAYER_COUNT_PROFILES[layers]]
        return {
            "layers": layers,
            "profile": _LAYER_COUNT_PROFILES[layers],
            "reference_planes": list(profile.get("reference_planes", [])),
            "high_speed_preferred_layers": list(profile.get("high_speed_preferred_layers", [])),
            "power_planes": list(profile.get("power_planes", [])),
            "source": "candidate_from_requested_layer_count",
            "note": _STACKUP_IMPEDANCE_WARNING,
        }
    if layers is not None:
        # An explicit --stackup-layers value with no built-in template (e.g. a
        # 12-layer board) must be honored, not silently replaced by the 4/2-layer
        # heuristic. Preserve the requested count and flag that the layer
        # roles/planes must be defined explicitly in board.pln.
        return {
            "layers": layers,
            "profile": None,
            "reference_planes": [],
            "high_speed_preferred_layers": [],
            "power_planes": [],
            "source": "requested_layer_count_no_template",
            "requires_review": True,
            "note": f"Requested {layers}-layer stackup has no built-in template; the requested "
                    "layer count is preserved but layer roles, reference planes, and high-speed "
                    f"layers must be defined explicitly in board.pln. {_STACKUP_IMPEDANCE_WARNING}",
        }
    if has_high_speed:
        return {
            "layers": 4,
            "profile": "4_layer_signal_gnd_pwr_signal",
            "reference_planes": ["In1.GND"],
            "high_speed_preferred_layers": ["F.Cu"],
            "power_planes": ["In2.PWR"],
            "source": "candidate_from_high_speed_nets",
            "note": _STACKUP_IMPEDANCE_WARNING,
        }
    return {
        "layers": 2,
        "profile": "2_layer_basic",
        "reference_planes": [],
        "high_speed_preferred_layers": ["F.Cu"],
        "power_planes": [],
        "source": "candidate_from_board_complexity",
        "note": _STACKUP_IMPEDANCE_WARNING,
    }


def inspect_simulation_candidates(nets: Mapping[str, PlanNet],
                                  components: Mapping[str, PlanComponent],
                                  differential_pairs: Sequence[DifferentialPair]) -> Dict[str, Any]:
    """Identify simulation opportunities as hints (not simulation results)."""

    net_names = " ".join(nets.keys()).upper()
    openems = bool(differential_pairs) or any(
        token in net_names for token in ("HDMI", "USB", "ETH", "RF", "TMDS", "SSTX", "SSRX", "PCIE"))
    ngspice = any(c.role in {"power_regulator", "clock", "rf_module"} for c in components.values())
    si = bool(differential_pairs)
    return {
        "note": "Simulation opportunities are hints, not results. Confirm before triggering solvers.",
        "openems_candidate": openems,
        "ngspice_candidate": ngspice,
        "si_candidate": si,
        "differential_pairs": [p.name for p in differential_pairs],
    }


def build_planning_hints(board: BoardGeometry,
                         components: Dict[str, PlanComponent],
                         nets: Dict[str, PlanNet],
                         aliases: Mapping[str, str],
                         alias_diagnostics: AliasDiagnostics,
                         board_text: str,
                         warnings: Sequence[str],
                         *,
                         board_path: Path,
                         netlist_path: Optional[Path],
                         stackup_layers: Optional[int]) -> Dict[str, Any]:
    """Build the full board-hints.json payload from extracted facts.

    Everything here is facts + candidate hints. No placement, no ownership, no
    floorplan.
    """

    differential_pairs = detect_differential_pairs(nets)
    functional_paths = detect_functional_paths(components, nets, {})
    power_islands = detect_power_islands(components, nets, {})
    high_speed_paths = {name: path for name, path in functional_paths.items()
                        if "high_speed" in str(path.get("type", ""))}
    edge_connectors = inspect_edge_connectors(board, components)
    mechanicals = inspect_mechanicals(board, components)
    rf_zones = inspect_rf_zones(board, components)
    kicad_groups = parse_kicad_groups(board_text, components)
    role_counts: Dict[str, int] = {}
    candidate_role_counts: Dict[str, int] = {}
    for comp in components.values():
        role_counts[comp.role] = role_counts.get(comp.role, 0) + 1
        cr = _candidate_role(comp)
        candidate_role_counts[cr] = candidate_role_counts.get(cr, 0) + 1

    all_warnings = list(warnings)
    stackup_assumptions = inspect_stackup_assumptions(nets, differential_pairs, stackup_layers)
    if stackup_assumptions.get("source") == "requested_layer_count_no_template":
        all_warnings.append(
            f"WARNING: requested --stackup-layers={stackup_layers} has no built-in stackup "
            "template; the requested layer count is preserved but layer roles/planes must be "
            "defined explicitly in board.pln.")

    hints: Dict[str, Any] = {
        "schema": "pcb-plan-board-hints/0.1",
        "generator": "pcb-plan inspect",
        "intent": "Facts and candidate hints for AI-generated board.pln. Hints are NOT "
                  "authoritative and do NOT assign placement ownership or a final floorplan.",
        "inputs": {
            "board": str(board_path),
            "netlist": str(netlist_path) if netlist_path else None,
        },
        "board_geometry": {
            **board_geometry_report(board),
            "edge_cuts_available": board.source == "edge_cuts",
            "geometry_source_note": geometry_source_label(board.source),
        },
        "counts": {
            "components": len(components),
            "nets": len(nets),
            "differential_pairs": len(differential_pairs),
            "high_speed_nets": sum(1 for n in nets if is_high_speed(n)),
            "power_nets": sum(1 for n in nets if is_power(n)),
            "ground_nets": sum(1 for n in nets if is_ground(n)),
        },
        "candidate_role_counts": candidate_role_counts,
        "role_counts": role_counts,
        "candidate_roles": {ref: _candidate_role(components[ref]) for ref in sorted(components)},
        "inferred_roles": {ref: components[ref].role for ref in sorted(components)},
        "candidate_functional_paths": functional_paths,
        "candidate_high_speed_paths": high_speed_paths,
        "candidate_power_islands": power_islands,
        "candidate_edge_connectors": edge_connectors,
        "candidate_mechanicals": mechanicals,
        "candidate_rf_zones": rf_zones,
        "differential_pairs": [dataclasses.asdict(p) for p in differential_pairs],
        "routing_classes": inspect_routing_classes(nets, differential_pairs),
        "stackup_assumptions": stackup_assumptions,
        "simulation_candidates": inspect_simulation_candidates(nets, components, differential_pairs),
        "imported_kicad_groups": kicad_groups,
        "kicad_groups_note": "KiCad groups are preserved for information only and must not drive placement.",
        "aliases_recovered": dict(aliases),
        "alias_diagnostics": dataclasses.asdict(alias_diagnostics),
        "warnings": all_warnings,
    }
    return _json_safe(hints)


def board_hints_markdown(hints: Mapping[str, Any]) -> str:
    geom = _as_mapping(hints.get("board_geometry"))
    counts = _as_mapping(hints.get("counts"))
    lines = ["# Board hints", "",
             "Facts and **candidate hints** extracted by `pcb-plan inspect`. These are not",
             "authoritative: an AI or human turns them into `board.pln`. Hints never assign",
             "placement ownership or a final floorplan.", "",
             "## Board geometry", ""]
    lines.append(f"- size: {geom.get('width')} x {geom.get('height')} mm")
    lines.append(f"- origin: ({geom.get('origin_x')}, {geom.get('origin_y')})")
    lines.append(f"- source: {geom.get('source')} (Edge.Cuts available: {geom.get('edge_cuts_available')})")
    lines.extend(["", "## Counts", ""])
    for key in ("components", "nets", "differential_pairs", "high_speed_nets", "power_nets", "ground_nets"):
        lines.append(f"- {key}: {counts.get(key)}")
    lines.extend(["", "## Candidate roles", ""])
    for role, count in sorted(_as_mapping(hints.get("candidate_role_counts")).items()):
        lines.append(f"- {role}: {count}")
    lines.extend(["", "## Candidate high-speed paths", ""])
    hs = _as_mapping(hints.get("candidate_high_speed_paths"))
    if hs:
        for name, path in sorted(hs.items()):
            lines.append(f"- {name}: {' -> '.join(_as_list(_as_mapping(path).get('sequence')))}")
    else:
        lines.append("- none")
    lines.extend(["", "## Candidate power islands", ""])
    islands = _as_list(hints.get("candidate_power_islands"))
    if islands:
        for island in islands:
            island = _as_mapping(island)
            lines.append(f"- {island.get('name')}: regulator {island.get('regulator')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Candidate edge connectors", ""])
    edges = _as_list(hints.get("candidate_edge_connectors"))
    if edges:
        for item in edges:
            item = _as_mapping(item)
            lines.append(f"- {item.get('ref')}: access {item.get('candidate_access_side')}, "
                         f"edge_required hint {item.get('candidate_edge_required')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Imported KiCad groups (metadata only)", "",
                  "KiCad groups are preserved for information only and must not drive placement.", ""])
    groups = _as_list(hints.get("imported_kicad_groups"))
    if groups:
        for group in groups:
            group = _as_mapping(group)
            lines.append(f"- {group.get('name')!r}: {', '.join(_as_list(group.get('members'))) or '(no resolved members)'}")
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = _as_list(hints.get("warnings"))
    lines.extend(f"- {w}" for w in warnings) if warnings else lines.append("- none")
    return "\n".join(lines) + "\n"


def ai_pln_prompt_markdown(hints: Mapping[str, Any]) -> str:
    """ai-pln-prompt.md: a prompt to help an AI generate board.pln from hints."""

    geom = _as_mapping(hints.get("board_geometry"))
    counts = _as_mapping(hints.get("counts"))
    lines = [
        "# Generate board.pln from planning hints", "",
        "You are generating `board.pln`, the **authoritative design-intent document** for",
        "a PCB. The files in this `planning-hints/` directory are *facts and candidate",
        "hints* extracted by `pcb-plan inspect` — they are not authoritative and contain",
        "no placement ownership or final floorplan. Your job is to reason about them and",
        "produce intentional `board.pln` design decisions.", "",
        "## What board.pln must contain", "",
        "- mechanical constraints (mounting holes, board outline/keepouts)",
        "- edge-required components, connector orientation, and access sides",
        "- placement regions",
        "- power islands (electrical topology, not type piles)",
        "- high-speed corridors and functional paths (signal-flow topology)",
        "- ownership (exactly one owner per ref; groups are metadata only)",
        "- routing classes and SI constraints",
        "- stackup assumptions and simulation triggers", "",
        "## Board summary", "",
        f"- size: {geom.get('width')} x {geom.get('height')} mm (source: {geom.get('source')})",
        f"- components: {counts.get('components')}, nets: {counts.get('nets')}",
        f"- differential pairs: {counts.get('differential_pairs')}, "
        f"high-speed nets: {counts.get('high_speed_nets')}",
        "",
        "## Component counts by candidate role", "",
    ]
    for role, count in sorted(_as_mapping(hints.get("candidate_role_counts")).items()):
        lines.append(f"- {role}: {count}")
    lines.extend(["", "## High-speed interfaces / candidate paths", ""])
    hs = _as_mapping(hints.get("candidate_high_speed_paths"))
    if hs:
        for name, path in sorted(hs.items()):
            path = _as_mapping(path)
            lines.append(f"- {name} ({path.get('type')}): {' -> '.join(_as_list(path.get('sequence')))}")
    else:
        lines.append("- none detected; confirm there are no high-speed interfaces")
    lines.extend(["", "## Candidate power islands", ""])
    islands = _as_list(hints.get("candidate_power_islands"))
    if islands:
        for island in islands:
            island = _as_mapping(island)
            members = _as_list(island.get("input_caps")) + _as_list(island.get("output_caps")) + _as_list(island.get("feedback"))
            ind = island.get("inductor")
            if ind:
                members.append(str(ind))
            lines.append(f"- {island.get('name')}: regulator {island.get('regulator')}; "
                         f"members {', '.join(str(m) for m in members) or '(review)'}")
    else:
        lines.append("- none detected")
    lines.extend(["", "## Mechanical constraints", ""])
    mech = _as_list(hints.get("candidate_mechanicals"))
    if mech:
        for item in mech:
            item = _as_mapping(item)
            loc = item.get("corner") or (f"({item.get('x')}, {item.get('y')})" if item.get("x") is not None else "review")
            lines.append(f"- {item.get('ref')}: {loc}")
    else:
        lines.append("- none detected")
    lines.extend(["", "## Edge connectors to confirm", ""])
    edges = _as_list(hints.get("candidate_edge_connectors"))
    if edges:
        for item in edges:
            item = _as_mapping(item)
            lines.append(f"- {item.get('ref')}: access {item.get('candidate_access_side')}, "
                         f"rotation(auto) {item.get('candidate_rotation_auto_deg')} deg, "
                         f"edge_required hint {item.get('candidate_edge_required')}")
    else:
        lines.append("- none detected")
    stackup = _as_mapping(hints.get("stackup_assumptions"))
    lines.extend(["", "## Stackup assumptions", "",
                  f"- candidate: {stackup.get('layers')}-layer ({stackup.get('profile')})",
                  f"- {stackup.get('note')}"])
    sim = _as_mapping(hints.get("simulation_candidates"))
    lines.extend(["", "## Placement risks and simulation triggers", "",
                  f"- openems_candidate: {sim.get('openems_candidate')}",
                  f"- ngspice_candidate: {sim.get('ngspice_candidate')}",
                  f"- si_candidate: {sim.get('si_candidate')}",
                  "- Heuristic hints cannot verify impedance, return paths, plane splits, thermal, or EMI.",
                  "- KiCad groups in imported_kicad_groups are metadata only; do not use them for placement.",
                  ""])
    lines.extend(["## Output", "",
                  "Write a complete `board.pln` (YAML). Resolve every hint into an explicit",
                  "decision or drop it with a rationale. Prefer encoding intent in `board.pln`",
                  "over later `placement.ppl` overrides. Validate with `pcb-plan check`.", ""])
    return "\n".join(lines) + "\n"


def ai_placement_review_markdown(hints: Mapping[str, Any]) -> str:
    """ai-placement-review.md: a checklist for reviewing board.pln against the hints."""

    lines = [
        "# AI placement review checklist", "",
        "Use this after generating `board.pln` to confirm the design intent captures the",
        "hints from `pcb-plan inspect`. Hints are candidates; this checklist drives the",
        "review/optimization loop, not automated placement.", "",
        "## Confirm", "",
        "- [ ] Board geometry matches Edge.Cuts / mechanical drawing",
        "- [ ] Every edge connector has a confirmed access_side, rotation, and edge_required",
        "- [ ] Mounting holes are at intended corners/locations",
        "- [ ] Each high-speed path has a corridor and flow-through protection ordering",
        "- [ ] Power islands reflect electrical topology, not type piles",
        "- [ ] Every ref has exactly one placement owner (groups are metadata only)",
        "- [ ] Routing classes / SI constraints set for high-speed and differential nets",
        "- [ ] Stackup assumptions confirmed with the fabricator for controlled impedance",
        "- [ ] Simulation triggers (OpenEMS / ngspice / SI) reviewed", "",
        "## Candidate items requiring a decision", "",
    ]
    edges = _as_list(hints.get("candidate_edge_connectors"))
    for item in edges:
        item = _as_mapping(item)
        lines.append(f"- edge connector {item.get('ref')}: access {item.get('candidate_access_side')}, "
                     f"edge_required hint {item.get('candidate_edge_required')}")
    for name, path in sorted(_as_mapping(hints.get("candidate_high_speed_paths")).items()):
        path = _as_mapping(path)
        lines.append(f"- high-speed path {name}: {' -> '.join(_as_list(path.get('sequence')))}")
    for island in _as_list(hints.get("candidate_power_islands")):
        island = _as_mapping(island)
        lines.append(f"- power island {island.get('name')}: regulator {island.get('regulator')}")
    groups = _as_list(hints.get("imported_kicad_groups"))
    if groups:
        lines.append("")
        lines.append("## KiCad groups (metadata only; must not drive placement)")
        lines.append("")
        for group in groups:
            group = _as_mapping(group)
            lines.append(f"- {group.get('name')!r}: {', '.join(_as_list(group.get('members'))) or '(no resolved members)'}")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_planning_hints(out_dir: Path, hints: Mapping[str, Any],
                         components: Mapping[str, PlanComponent],
                         nets: Mapping[str, PlanNet],
                         board: BoardGeometry) -> List[str]:
    """Write every planning-hints/ artifact and return the list of files written."""

    out_dir.mkdir(parents=True, exist_ok=True)

    def _json(name: str, payload: Any) -> str:
        (out_dir / name).write_text(json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return name

    def _text(name: str, text: str) -> str:
        (out_dir / name).write_text(text, encoding="utf-8")
        return name

    written = [
        _json("board-hints.json", hints),
        _text("board-hints.md", board_hints_markdown(hints)),
        _text("component-table.csv", inspect_component_table_csv(components, nets)),
        _json("connectivity-graph.json", inspect_connectivity_graph(components, nets)),
        _json("footprint-bboxes.json", inspect_footprint_bboxes(board, components)),
        _json("pad-locations.json", inspect_pad_locations(components)),
        _json("candidate-functional-paths.json", hints.get("candidate_functional_paths")),
        _json("candidate-power-islands.json", hints.get("candidate_power_islands")),
        _json("candidate-high-speed-paths.json", hints.get("candidate_high_speed_paths")),
        _json("candidate-edge-connectors.json", hints.get("candidate_edge_connectors")),
        _json("candidate-mechanicals.json", hints.get("candidate_mechanicals")),
        _json("candidate-rf-zones.json", hints.get("candidate_rf_zones")),
        _json("routing-classes.json", hints.get("routing_classes")),
        _text("ai-pln-prompt.md", ai_pln_prompt_markdown(hints)),
        _text("ai-placement-review.md", ai_placement_review_markdown(hints)),
    ]
    return written


def run_inspect(args: argparse.Namespace) -> int:
    _ensure_exists(args.board, "Board")
    _ensure_exists(args.netlist, "Netlist")
    board_text = args.board.read_text(encoding="utf-8")
    board, components, nets, warnings = parse_board(args.board)
    aliases, alias_diag, net_warnings = import_netlist(args.netlist, components, nets)
    warnings.extend(net_warnings)
    override = _board_override_from_init_args(args)
    if override is not None:
        board = BoardGeometry(
            origin_x=float(override.get("origin_x", board.origin_x)),
            origin_y=float(override.get("origin_y", board.origin_y)),
            width=float(override["width"]),
            height=float(override["height"]),
            source="cli",
        )
        warnings = [w for w in warnings if "footprint extents" not in w]
    _validate_board_geometry(board)
    infer_roles(components, {})
    hints = build_planning_hints(
        board, components, nets, aliases, alias_diag, board_text, warnings,
        board_path=args.board, netlist_path=args.netlist,
        stackup_layers=getattr(args, "stackup_layers", None),
    )
    written = write_planning_hints(args.out, hints, components, nets, board)
    sys.stderr.write(f"pcb-plan inspect: wrote {len(written)} planning-hints files to {args.out}\n")
    return 0


def run_emit(args: argparse.Namespace, *, legacy: bool = False) -> int:
    pln_path = getattr(args, "pln", None) or getattr(args, "intent", None)
    _ensure_exists(args.board, "Board")
    _ensure_exists(args.netlist, "Netlist")
    _ensure_exists(pln_path, "board.pln")
    plan, _ = _load_plan_from_inputs(args.board, args.netlist, pln_path)
    payload = report(plan)
    _write_report(args.report_json, payload)
    confidence = payload["plan_confidence"]
    if confidence["level"] == "low":
        sys.stderr.write(f"WARNING: plan confidence is low (score {confidence['score']}):\n")
        for reason in confidence["reasons"]:
            sys.stderr.write(f"WARNING: - {reason}\n")
        if getattr(args, "strict_confidence", False) and not getattr(args, "allow_low_confidence", False):
            raise SystemExit("pcb-plan emit: plan confidence is low; re-run with --allow-low-confidence to "
                             "proceed despite --strict-confidence, or improve board.pln/netlist coverage")
    if getattr(args, "emit_routing_policy", None):
        _write_text(args.emit_routing_policy, emit_routing_policy(plan, args.board))
    if getattr(args, "emit_openems_plan", None) and openems_plan_should_emit(plan):
        _write_text(args.emit_openems_plan, emit_openems_plan(plan, args.board))
    if legacy and getattr(args, "explain", None):
        sys.stdout.write(explain_text(plan, args.explain))
        return 0
    if getattr(args, "summary_md", None):
        _write_text(args.summary_md, _summary_markdown("pcb-plan summary", {"report": report(plan)}))
    if getattr(args, "ai_edit_hints", None):
        _write_text(args.ai_edit_hints, ai_edit_hints_text(plan))
    ppl = emit_ppl(plan, args.board, args.netlist)
    _write_text(args.output, ppl)
    return 0


def _board_override_from_init_args(args: argparse.Namespace) -> Optional[Dict[str, float]]:
    width = getattr(args, "width", None)
    height = getattr(args, "height", None)
    origin_x = getattr(args, "origin_x", None)
    origin_y = getattr(args, "origin_y", None)
    if width is None and height is None and origin_x is None and origin_y is None:
        return None
    if width is None or height is None:
        raise SystemExit("pcb-plan init: --width and --height must be supplied together")
    if width <= 0 or height <= 0:
        raise SystemExit("pcb-plan init: --width and --height must be positive millimeter values")
    override = {"width": float(width), "height": float(height)}
    if origin_x is not None:
        override["origin_x"] = float(origin_x)
    if origin_y is not None:
        override["origin_y"] = float(origin_y)
    return override


def run_init(args: argparse.Namespace) -> int:
    _ensure_exists(args.board, "Board")
    _ensure_exists(args.netlist, "Netlist")
    plan, _ = _load_plan_from_inputs(args.board, args.netlist, None, board_override=_board_override_from_init_args(args))
    payload = plan_to_pln(plan)
    text = serialize_pln(payload)
    _write_text(args.output, text)
    report_payload = pln_report(plan, payload, "init")
    _write_report(args.report_json, report_payload)
    if getattr(args, "summary_md", None):
        _write_text(args.summary_md, _summary_markdown("pcb-plan init summary", {"report": report_payload}))
    return 0


def run_update(args: argparse.Namespace) -> int:
    _ensure_exists(args.pln, "board.pln")
    for path, label in ((args.board, "Board"), (args.place_report, "pcb-place report"), (args.routing_report, "Routing report"), (args.openems_report, "OpenEMS report"), (args.ngspice_report, "ngspice report")):
        _ensure_exists(path, label)
    old_text = args.pln.read_text(encoding="utf-8")
    payload = load_pln(args.pln)
    reports = {
        "place": _load_optional_json(args.place_report),
        "routing": _load_optional_json(args.routing_report),
        "openems": _load_optional_json(args.openems_report),
        "ngspice": _load_optional_json(args.ngspice_report),
    }
    updated = apply_feedback_updates(payload, reports)
    new_text = serialize_pln(updated)
    _write_text(args.output, new_text)
    if args.patch:
        _write_text(args.patch, unified_diff_text(old_text, new_text, str(args.pln), str(args.output)))
    report_payload: Dict[str, Any] = {
        "action": "update",
        "reports_consumed": sorted(k for k, v in reports.items() if v is not None),
        "pln_validation_warnings": validate_pln(updated),
        "proposed_changes": _as_list(_as_mapping(updated.get("provenance")).get("update_proposals")),
    }
    if args.board:
        plan, _ = _load_plan_from_inputs(args.board, None, args.output)
        report_payload.update(pln_report(plan, updated, "update"))
    _write_report(args.report_json, report_payload)
    if getattr(args, "summary_md", None):
        _write_text(args.summary_md, _summary_markdown("pcb-plan update summary", {"report": report_payload}))
    return 0


def run_check(args: argparse.Namespace) -> int:
    pln_path = getattr(args, "pln", None)
    _ensure_exists(args.board, "Board")
    _ensure_exists(args.netlist, "Netlist")
    _ensure_exists(pln_path, "board.pln")
    plan, _ = _load_plan_from_inputs(args.board, args.netlist, pln_path)
    payload = build_check_report(plan)
    _write_report(args.report_json, payload)
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def run_review(args: argparse.Namespace) -> int:
    _ensure_exists(args.pln, "board.pln")
    sys.stdout.write(review_pln_text(load_pln(args.pln)))
    return 0


def run_explain(args: argparse.Namespace) -> int:
    if args.board:
        _ensure_exists(args.board, "Board")
        _ensure_exists(args.netlist, "Netlist")
        plan, _ = _load_plan_from_inputs(args.board, args.netlist, args.pln if args.pln.exists() else None)
        try:
            sys.stdout.write(explain_text(plan, args.object))
            return 0
        except PlacementError:
            pass
    _ensure_exists(args.pln, "board.pln")
    sys.stdout.write(explain_pln_text(load_pln(args.pln), args.object))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "legacy_emit"
    if command == "inspect":
        return run_inspect(args)
    if command == "init":
        return run_init(args)
    if command == "update":
        return run_update(args)
    if command == "review":
        return run_review(args)
    if command == "explain":
        return run_explain(args)
    if command == "emit":
        return run_emit(args)
    if command == "check":
        return run_check(args)
    if not args.board:
        raise SystemExit("Board file not found: provide --board or use a subcommand such as 'pcb-plan init' or 'pcb-plan emit'.")
    return run_emit(args, legacy=True)


if __name__ == "__main__":
    raise SystemExit(main())
