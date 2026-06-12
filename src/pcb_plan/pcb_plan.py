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

__version__ = "0.12.0"

Point = Tuple[float, float]
_POWER_RE = re.compile(r"^(?:\+?(?:1V[0-9]|1V[0-9]|[0-9]+V[0-9]*|VCC|VDD|VBAT|VIN|VBUS|AVDD|DVDD|PVDD|3V3|5V|12V))", re.I)
_GROUND_RE = re.compile(r"^(?:GND|AGND|DGND|PGND|GNDA|GNDD)$", re.I)
_HIGHSPEED_RE = re.compile(r"(?:TMDS|HDMI|USB|DP|DN|D\+|D-|SSTX|SSRX|PCIE|PCIe|LVDS|CLK|MIPI|ETH|RX|TX)", re.I)

_ALLOWED_VIA_POLICIES = {"avoid", "allow", "constrained", "forbid"}
_ALLOWED_ROUTING_MODES = {"low_speed_only", "all_nets_constrained", "experimental_high_speed"}
_HIGH_SPEED_CLASS_HINTS = ("high_speed", "diff", "rf", "clock", "hdmi", "usb", "pcie", "lvds")



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
        warnings.append("WARNING: No board.pln geometry or Edge.Cuts rectangle was available while parsing the board; using footprint extents only as a last-resort board-size fallback. Supply board.width/height/origin in board.pln for authoritative dimensions.")
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


def validate_routing_intent(intent: Mapping[str, Any], nets: Mapping[str, PlanNet]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any], List[str], List[str], List[str], bool]:
    """Validate optional .pln routing/SI intent without turning it into placement primitives."""

    stackup = _as_mapping(intent.get("stackup"))
    routing = _as_mapping(intent.get("routing"))
    declared_pairs = {str(k): _as_mapping(v) for k, v in _as_mapping(intent.get("differential_pairs")).items()}
    net_classes = _as_mapping(intent.get("net_classes"))
    routing_overrides = {str(k): _as_mapping(v) for k, v in _as_mapping(intent.get("routing_overrides")).items()}
    simulation = _as_mapping(intent.get("simulation"))

    stackup_warnings: List[str] = []
    routing_warnings: List[str] = []
    simulation_warnings: List[str] = []

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


def infer_roles(components: Dict[str, PlanComponent], intent: Mapping[str, Any]) -> None:
    intent_roles = {str(k): str(v) for k, v in (intent.get("roles") or {}).items()} if isinstance(intent.get("roles"), dict) else {}
    for comp in components.values():
        if comp.ref in intent_roles:
            comp.role = intent_roles[comp.ref]
            comp.role_reasons.append("role supplied by board intent")
            continue
        pref = _ref_prefix(comp.ref)
        value = (comp.value or "").upper()
        fp = comp.footprint.upper()
        nets = comp.nets
        if pref in {"H", "MH"}:
            comp.role = "mechanical"; comp.role_reasons.append("reference prefix indicates mechanical")
        elif pref in {"J", "P"}:
            comp.role = "high_speed_connector" if ("HDMI" in fp or "USB" in fp or any(is_high_speed(n) for n in nets)) else "connector"
            comp.role_reasons.append("reference prefix indicates connector")
        elif pref == "TP":
            comp.role = "testpoint"; comp.role_reasons.append("reference prefix indicates test point")
        elif pref in {"C"}:
            if any(is_power(n) for n in nets) and any(is_ground(n) for n in nets):
                comp.role = "decoupling"; comp.role_reasons.append("capacitor connects a power net to ground")
            else:
                comp.role = "capacitor"; comp.role_reasons.append("reference prefix indicates capacitor")
        elif pref in {"D", "U"} and ("ESD" in value or "TVS" in value or "ESD" in fp or "TVS" in fp):
            comp.role = "esd_protection"; comp.role_reasons.append("value/footprint suggests TVS or ESD protection")
        elif pref in {"L", "FB"}:
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


def _cluster_members(anchor: PlanComponent, components: Mapping[str, PlanComponent], nets: Optional[Mapping[str, PlanNet]] = None, aliases: Optional[Mapping[str, str]] = None, radius: float = 12.0, proximity_any_role: bool = True) -> List[str]:
    shared = set(anchor.nets)
    connected = _connected_refs(anchor, nets or {}) if nets else set()
    alias_refs = set()
    for refs in _alias_groups(aliases or {}).values():
        if anchor.ref in refs:
            alias_refs |= refs
    support_roles = {"decoupling", "esd_protection", "pullup_pulldown", "series", "clock", "testpoint", "inductor", "ferrite", "capacitor", "resistor"}
    members = [anchor.ref]
    for comp in components.values():
        if comp.ref == anchor.ref or comp.role == "mechanical":
            continue
        dist = math.hypot(anchor.x - comp.x, anchor.y - comp.y)
        if dist > radius and comp.ref not in alias_refs:
            continue
        has_shared_signal = bool((shared & set(comp.nets)) - {n for n in shared if is_ground(n) or is_power(n)})
        is_support = comp.role in support_roles and dist <= radius
        is_nearby_any_role = proximity_any_role and dist <= radius / 2.0 and comp.role != "unknown"
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
            "HIGH_SPEED": {"x": 0, "y": 0, "w": board.width, "h": h},
            "CONTROL": {"x": 0, "y": h, "w": board.width, "h": h},
            "POWER": {"x": 0, "y": 2*h, "w": board.width, "h": board.height - 2*h},
        }
        warnings.append("Generated default HIGH_SPEED/CONTROL/POWER regions from board thirds; review before fabrication.")
    keepouts = list(intent.get("keepouts") or []) if isinstance(intent.get("keepouts"), list) else []

    for name, r in regions.items():
        rules.append(PlanRule("region", f'Region({_q(name)}, x={r.get("x",0)}, y={r.get("y",0)}, w={r.get("w",0)}, h={r.get("h",0)})', [], f"Region {name} from intent/default floorplan."))
    for k in keepouts:
        if isinstance(k, dict):
            rules.append(PlanRule("keepout", f'Keepout({_q(k.get("name", "KEEPOUT"))}, x={k.get("x",0)}, y={k.get("y",0)}, w={k.get("w",0)}, h={k.get("h",0)}, role={_q(k.get("role", "keepout"))})', [], f"Keepout {k.get('name')} from board intent."))

    # Fixed mechanical objects.
    fixed = intent.get("fixed") if isinstance(intent.get("fixed"), dict) else {}
    for ref, spec in fixed.items():
        if isinstance(spec, dict) and spec.get("type") == "corner":
            rules.append(PlanRule("corner", f'Corner({_q(ref)}, corner={_q(spec.get("corner", "top_left"))}, inset={spec.get("inset", 3)}, role="fixed_mechanical")', [str(ref)], f"{ref} fixed by board intent."))
            explanations.setdefault(str(ref), {})["generated_rule"] = rules[-1].text
    for comp in components.values():
        if comp.role == "mechanical" and comp.ref not in fixed:
            rules.append(PlanRule("fixed", f'Anchor({_q(comp.ref)}, x={comp.x - board.origin_x:.3f}, y={comp.y - board.origin_y:.3f}, rot={comp.rot:g}, role="mechanical")', [comp.ref], f"{comp.ref} kept at existing mechanical location."))

    clusters: List[Dict[str, Any]] = []
    # Connector/high-speed clusters.
    for comp in components.values():
        if "connector" in comp.role:
            members = _cluster_members(comp, components, nets, aliases, radius=18.0, proximity_any_role=False)
            local_x = max(0.0, min(board.width, comp.x - board.origin_x))
            local_y = max(0.0, min(board.height, comp.y - board.origin_y))
            distances = {
                "left": local_x,
                "right": board.width - local_x,
                "top": local_y,
                "bottom": board.height - local_y,
            }
            edge = min(distances, key=distances.get)
            edge_span = board.width if edge in ("left", "right") else board.height
            inset = 2.0
            name = re.sub(r"[^A-Za-z0-9_]+", "_", (("IC" if comp.role == "mcu" else comp.role.upper()) + "_" + comp.ref))
            if edge_span > 0 and distances[edge] / edge_span <= 0.25 and distances[edge] > inset:
                along = local_y if edge in ("left", "right") else local_x
                placement_kw = "y" if edge in ("left", "right") else "x"
                edge_required = comp.role in {"connector", "high_speed_connector"}
                text = (f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, '
                        f'placement=Edge(edge={_q(edge)}, {placement_kw}={along:.3f}, inset={inset}, '
                        f'locked=True, edge_required={str(edge_required)}, mechanical=True, access_side={_q(edge)}), '
                        f'role={_q(comp.role)})')
                rules.append(PlanRule("cluster", text, members, f"{comp.ref} connector cluster preserves local geometry and reserves edge access; edge_required={edge_required}, access_side={edge}."))
            else:
                text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement=Anchor(x={local_x:.3f}, y={local_y:.3f}, rot={comp.rot:g}), role={_q(comp.role)})'
                rules.append(PlanRule("cluster", text, members, f"{comp.ref} connector cluster kept at existing location; not adjacent to a board edge."))
            clusters.append({"name": name, "anchor": comp.ref, "members": members, "role": comp.role, "category": _cluster_category(comp), "confidence": "high" if len(members) > 1 else "low"})
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

    # IC and power clusters.
    for comp in components.values():
        if comp.role in {"ic", "mcu", "power_regulator", "rf_module", "hdmi_retimer"}:
            members = _cluster_members(comp, components, nets, aliases, radius=12.0)
            region = "POWER" if comp.role == "power_regulator" and "POWER" in regions else "RF" if comp.role == "rf_module" and "RF" in regions else "CONTROL" if comp.role != "rf_module" and "CONTROL" in regions else None
            name = re.sub(r"[^A-Za-z0-9_]+", "_", (("IC" if comp.role == "mcu" else comp.role.upper()) + "_" + comp.ref))
            placement = f'Anchor(x={comp.x - board.origin_x:.3f}, y={comp.y - board.origin_y:.3f}' + (f', region={_q(region)}' if region else '') + ')'
            text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement={placement}, role={_q(comp.role)})'
            rules.append(PlanRule("cluster", text, members, f"{comp.ref} {comp.role} support cluster preserves coarse neighborhood before pin-aware refinements."))
            clusters.append({"name": name, "anchor": comp.ref, "members": members, "role": comp.role, "category": _cluster_category(comp), "confidence": "high" if len(members) > 1 else "low"})
            explanations.setdefault(comp.ref, {}).update({"role": comp.role, "generated_rule": text})
            if comp.role == "rf_module" and not any(k.get("role") == "rf" for k in keepouts if isinstance(k, dict)):
                rect = _synthesized_rf_keepout(comp, board)
                ko = f'Keepout({_q(comp.ref + "_ANTENNA")}, x={rect["x"]:.3f}, y={rect["y"]:.3f}, w={rect["w"]:.3f}, h={rect["h"]:.3f}, role="rf")'
                rules.append(PlanRule("keepout", ko, [comp.ref], f"{comp.ref} inferred RF/module; synthesized antenna keepout adjacent to module edge and requires engineering review."))

    graph = ConnectivityGraph(components, nets)
    ic_candidates = [c for c in components.values() if c.role in {"ic", "mcu", "hdmi_retimer", "rf_module"}]

    # Decoupling capacitors: group caps that share the same owning power pin
    # (same parent IC + same power net/pad) and emit one primitive per group
    # instead of one Decoupling()+NearPad() pair per capacitor.
    decoupling_groups: List[Dict[str, Any]] = []
    decoupling_keys: Dict[Tuple[str, str], List[Tuple[PlanComponent, Optional[str]]]] = {}
    for cap in [c for c in components.values() if c.role == "decoupling"]:
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
    for r in [c for c in components.values() if c.role == "pullup_pulldown"]:
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
    for r in [c for c in components.values() if c.role == "series"]:
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
        elif comp.role in {"inductor", "ferrite"}:
            peers = [components[ref] for n in signal_nets for ref, _ in nets.get(n, PlanNet(n)).pads if ref in components and ref != comp.ref and components[ref].role not in {"inductor", "ferrite", "capacitor", "resistor"}]
            if len(peers) >= 2:
                text = f'Between({_q(comp.ref)}, a={_q(peers[0].ref)}, b={_q(peers[1].ref)}, t=0.5, offset=0, role={_q(comp.role)})'
                rules.append(PlanRule("between", text, [comp.ref, peers[0].ref, peers[1].ref], f"{comp.ref} inferred as inline magnetic/filter element."))
            else:
                text = f'Satellite({_q(comp.ref)}, parent={_q(parent.ref)}, side="auto", distance=2.0, role={_q(comp.role)})'
                rules.append(PlanRule("satellite", text, [comp.ref, parent.ref], f"{comp.ref} support magnetic/filter element kept near {parent.ref}."))
        elif comp.role in {"capacitor", "resistor"}:
            text = f'Satellite({_q(comp.ref)}, parent={_q(parent.ref)}, side="auto", distance=2.5, role={_q(comp.role)})'
            rules.append(PlanRule("satellite", text, [comp.ref, parent.ref], f"{comp.ref} generic support passive kept near connected anchor {parent.ref}."))
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
    lines.extend([
        f"Board(width={plan.board.width:.6g}, height={plan.board.height:.6g}, origin_x={plan.board.origin_x:.6g}, origin_y={plan.board.origin_y:.6g}, emit_outline=True)",
        "Spacing(default=0.25, passive_to_ic=0.50, connector=1.00)",
        "PlacementPolicy(avoid_overlap=True, allow_anchor_move=False, max_search_radius=5, search_step=0.5)",
        "",
    ])
    sections = [
        ("Regions and keepouts", {"region", "keepout"}),
        ("Fixed mechanical placement", {"corner", "fixed"}),
        ("Clusters and routing corridors", {"cluster", "corridor"}),
        ("Critical pin-aware refinements", {"decoupling", "decoupling_array", "esd", "nearpad", "between"}),
        ("Low-priority refinements", {"pullup", "pullup_array", "series", "satellite"}),
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

    if plan.board.source == "inferred_from_footprints":
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
    return {
        "components_parsed": len(plan.components),
        "nets_parsed": len(plan.nets),
        "components_placed": len(placed_refs),
        "components_unplaced": len(unplaced),
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
    return {
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
        "fixed", "routing_overrides",
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
        "provenance": {
            "generator": "pcb-plan init",
            "schema_version": "0.1",
            "visibility": "inferred values include value/source/confidence/requires_review wrappers where practical",
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
