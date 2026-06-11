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

__version__ = "0.10.0-experimental"

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


def _q(value: Any) -> str:
    return json.dumps(value)


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
    board = parse_edge_cuts_geometry(text) or infer_geometry_from_footprints(fps)
    warnings: List[str] = []
    if parse_edge_cuts_geometry(text) is None:
        warnings.append("No Edge.Cuts rectangle found; inferred board geometry from footprint extents.")
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


def import_netlist(path: Optional[Path], components: Dict[str, PlanComponent], nets: Dict[str, PlanNet]) -> Tuple[Dict[str, str], AliasDiagnostics, List[str]]:
    if path is None:
        return {}, AliasDiagnostics(), []
    aliases, diagnostics = parse_netlist_aliases(path)
    warnings = list(diagnostics.warnings)
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith(("{", "[")):
        data = json.loads(text)
        _extract_connectivity_from_json_obj(data, components, nets)
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
            if any(is_power(n) for n in nets) and len(non_ground) >= 2:
                comp.role = "pullup_pulldown"; comp.role_reasons.append("resistor connects power to a signal")
            elif len(non_ground) == 1 and len(nets) == 2:
                comp.role = "series"; comp.role_reasons.append("two-pin resistor candidate on signal path")
            else:
                comp.role = "resistor"; comp.role_reasons.append("reference prefix indicates resistor")
        elif pref == "U":
            if "ESP32" in fp or "WIFI" in fp or "ANT" in fp:
                comp.role = "rf_module"; comp.role_reasons.append("footprint suggests RF/module antenna")
            elif "BUCK" in fp or "REG" in fp or "LDO" in fp or "REG" in value:
                comp.role = "power_regulator"; comp.role_reasons.append("value/footprint suggests regulator")
            else:
                comp.role = "ic"; comp.role_reasons.append("reference prefix indicates IC")
        else:
            comp.role = "unknown"; comp.role_reasons.append("no strong role heuristic matched")


def detect_differential_pairs(nets: Mapping[str, PlanNet]) -> List[DifferentialPair]:
    names = set(nets)
    pairs: List[DifferentialPair] = []
    suffixes = [("_P", "_N"), ("+", "-"), ("P", "N"), ("DP", "DN")]
    used: set[str] = set()
    for name in sorted(names):
        if name in used:
            continue
        for ps, ns in suffixes:
            if not name.upper().endswith(ps.upper()):
                continue
            base = name[:-len(ps)]
            candidates = [base + ns, base + ns.lower()]
            mate = next((c for c in candidates if c in names), None)
            if mate and (is_high_speed(name) or is_high_speed(mate)):
                comps = sorted({r for r, _ in nets[name].pads} | {r for r, _ in nets[mate].pads})
                pairs.append(DifferentialPair(base.rstrip("_+-"), name, mate, comps))
                used.update({name, mate})
                break
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


def _cluster_members(anchor: PlanComponent, components: Mapping[str, PlanComponent], radius: float = 12.0) -> List[str]:
    shared = set(anchor.nets)
    members = [anchor.ref]
    for comp in components.values():
        if comp.ref == anchor.ref or comp.role == "mechanical":
            continue
        dist = math.hypot(anchor.x - comp.x, anchor.y - comp.y)
        if dist <= radius and (shared & set(comp.nets) or comp.role in {"decoupling", "esd_protection", "pullup_pulldown", "series"}):
            members.append(comp.ref)
    return sorted(set(members), key=lambda r: (r != anchor.ref, r))


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
            members = _cluster_members(comp, components, radius=18.0)
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
            name = re.sub(r"[^A-Za-z0-9_]+", "_", comp.role.upper() + "_" + comp.ref)
            if edge_span > 0 and distances[edge] / edge_span <= 0.25 and distances[edge] > inset:
                along = local_y if edge in ("left", "right") else local_x
                placement_kw = "y" if edge in ("left", "right") else "x"
                text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement=Edge(edge={_q(edge)}, {placement_kw}={along:.3f}, inset={inset}), role={_q(comp.role)})'
                rules.append(PlanRule("cluster", text, members, f"{comp.ref} connector cluster preserves local geometry and reserves edge access."))
            else:
                text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement=Anchor(x={local_x:.3f}, y={local_y:.3f}, rot={comp.rot:g}), role={_q(comp.role)})'
                rules.append(PlanRule("cluster", text, members, f"{comp.ref} connector cluster kept at existing location; not adjacent to a board edge."))
            clusters.append({"name": name, "anchor": comp.ref, "members": members, "role": comp.role})
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
            members = _cluster_members(comp, components, radius=12.0)
            region = "POWER" if comp.role == "power_regulator" and "POWER" in regions else "RF" if comp.role == "rf_module" and "RF" in regions else "CONTROL" if comp.role != "rf_module" and "CONTROL" in regions else None
            name = re.sub(r"[^A-Za-z0-9_]+", "_", comp.role.upper() + "_" + comp.ref)
            placement = f'Anchor(x={comp.x - board.origin_x:.3f}, y={comp.y - board.origin_y:.3f}' + (f', region={_q(region)}' if region else '') + ')'
            text = f'Cluster({_q(name)}, anchor={_q(comp.ref)}, members={_q(members)}, placement={placement}, role={_q(comp.role)})'
            rules.append(PlanRule("cluster", text, members, f"{comp.ref} {comp.role} support cluster preserves coarse neighborhood before pin-aware refinements."))
            clusters.append({"name": name, "anchor": comp.ref, "members": members, "role": comp.role})
            explanations.setdefault(comp.ref, {}).update({"role": comp.role, "generated_rule": text})
            if comp.role == "rf_module" and not any(k.get("role") == "rf" for k in keepouts if isinstance(k, dict)):
                rect = _synthesized_rf_keepout(comp, board)
                ko = f'Keepout({_q(comp.ref + "_ANTENNA")}, x={rect["x"]:.3f}, y={rect["y"]:.3f}, w={rect["w"]:.3f}, h={rect["h"]:.3f}, role="rf")'
                rules.append(PlanRule("keepout", ko, [comp.ref], f"{comp.ref} inferred RF/module; synthesized antenna keepout adjacent to module edge and requires engineering review."))

    ic_candidates = [c for c in components.values() if c.role in {"ic", "mcu", "hdmi_retimer", "rf_module"}]
    for cap in [c for c in components.values() if c.role == "decoupling"]:
        power_nets = {n for n in cap.nets if is_power(n)}
        parent = _nearest_parent(cap, ic_candidates, power_nets) or _nearest_parent(cap, ic_candidates)
        if parent:
            pad = _nearest_pad(parent, power_nets)
            if pad:
                text = f'Decoupling({_q(cap.ref)}, parent={_q(parent.ref)}, pad={_q(pad)}, distance=1.5, power_net={_q(next(iter(power_nets), None))}, ground_net="GND")'
            else:
                text = f'Decoupling({_q(cap.ref)}, parent={_q(parent.ref)}, distance=2.0, power_net={_q(next(iter(power_nets), None))}, ground_net="GND")'
            rules.append(PlanRule("decoupling", text, [cap.ref, parent.ref], f"{cap.ref} inferred as decoupling: connects {', '.join(cap.nets)} near {parent.ref}."))
            explanations[cap.ref] = {"role": cap.role, "nets": cap.nets, "parent_candidate": parent.ref, "generated_rule": text}
        else:
            uncertain.append(f"{cap.ref} is power-to-ground capacitor but no parent IC candidate was found.")

    for r in [c for c in components.values() if c.role == "pullup_pulldown"]:
        signal_nets = [n for n in r.nets if not is_power(n) and not is_ground(n)]
        parent = _nearest_parent(r, ic_candidates, set(signal_nets)) or _nearest_parent(r, ic_candidates)
        if parent:
            text = f'Pullup({_q(r.ref)}, parent={_q(parent.ref)}, net={_q(signal_nets[0] if signal_nets else None)}, distance=3.0)'
            rules.append(PlanRule("pullup", text, [r.ref, parent.ref], f"{r.ref} inferred as pullup/pulldown on {signal_nets[0] if signal_nets else 'unknown signal'} near {parent.ref}."))
            explanations[r.ref] = {"role": r.role, "nets": r.nets, "parent_candidate": parent.ref, "generated_rule": text}
    for r in [c for c in components.values() if c.role == "series"]:
        peers = [components[ref] for n in r.nets for ref, _ in nets.get(n, PlanNet(n)).pads if ref in components and ref != r.ref and components[ref].role not in {"resistor", "capacitor"}]
        if len(peers) >= 2:
            text = f'Series({_q(r.ref)}, a={_q(peers[0].ref)}, b={_q(peers[1].ref)}, t=0.5, offset=0)'
            rules.append(PlanRule("series", text, [r.ref, peers[0].ref, peers[1].ref], f"{r.ref} inferred as series component between {peers[0].ref} and {peers[1].ref}."))
            explanations[r.ref] = {"role": r.role, "nets": r.nets, "generated_rule": text}

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
    lines.extend(routing_summary_comments(plan))
    lines.extend([
        f"Board(width={plan.board.width:.6g}, height={plan.board.height:.6g}, origin_x={plan.board.origin_x:.6g}, origin_y={plan.board.origin_y:.6g})",
        "Spacing(default=0.25, passive_to_ic=0.50, connector=1.00)",
        "PlacementPolicy(avoid_overlap=True, allow_anchor_move=False, max_search_radius=5, search_step=0.5)",
        "",
    ])
    sections = [
        ("Regions and keepouts", {"region", "keepout"}),
        ("Fixed mechanical placement", {"corner", "fixed"}),
        ("Clusters and routing corridors", {"cluster", "corridor"}),
        ("Critical pin-aware refinements", {"decoupling", "esd"}),
        ("Low-priority refinements", {"pullup", "series"}),
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


def report(plan: Plan) -> Dict[str, Any]:
    openems = _as_mapping(plan.simulation.get("openems"))
    return {
        "components_parsed": len(plan.components),
        "nets_parsed": len(plan.nets),
        "aliases_recovered": plan.aliases,
        "alias_diagnostics": dataclasses.asdict(plan.alias_diagnostics),
        "inferred_roles": plan.roles,
        "inferred_clusters": plan.clusters,
        "inferred_differential_pairs": [dataclasses.asdict(p) for p in plan.differential_pairs],
        "generated_rules": [dataclasses.asdict(r) for r in plan.rules],
        "warnings": plan.warnings,
        "unplaced_components": [ref for ref in plan.components if not any(ref in r.refs for r in plan.rules)],
        "uncertain_inferences": plan.uncertain_inferences,
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
    payload: Dict[str, Any] = {
        "board": {
            "width": provenance_value(plan.board.width, f"inferred_from_{plan.board.source}", "high" if plan.board.source == "edge_cuts" else "medium", plan.board.source != "edge_cuts"),
            "height": provenance_value(plan.board.height, f"inferred_from_{plan.board.source}", "high" if plan.board.source == "edge_cuts" else "medium", plan.board.source != "edge_cuts"),
            "origin_x": provenance_value(plan.board.origin_x, f"inferred_from_{plan.board.source}", "high" if plan.board.source == "edge_cuts" else "medium", plan.board.source != "edge_cuts"),
            "origin_y": provenance_value(plan.board.origin_y, f"inferred_from_{plan.board.source}", "high" if plan.board.source == "edge_cuts" else "medium", plan.board.source != "edge_cuts"),
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


def _load_plan_from_inputs(board_path: Path, netlist_path: Optional[Path], pln_path: Optional[Path]) -> Tuple[Plan, Dict[str, Any]]:
    board, components, nets, warnings = parse_board(board_path)
    aliases, alias_diag, net_warnings = import_netlist(netlist_path, components, nets)
    warnings.extend(net_warnings)
    raw_intent = load_pln(pln_path)
    intent = unwrap_provenance(raw_intent)
    if isinstance(intent.get("board"), dict):
        b = intent["board"]
        board = BoardGeometry(width=float(b.get("width", board.width)), height=float(b.get("height", board.height)), origin_x=float(b.get("origin_x", board.origin_x)), origin_y=float(b.get("origin_y", board.origin_y)), source="intent")
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
    _write_report(args.report_json, report(plan))
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


def run_init(args: argparse.Namespace) -> int:
    _ensure_exists(args.board, "Board")
    _ensure_exists(args.netlist, "Netlist")
    plan, _ = _load_plan_from_inputs(args.board, args.netlist, None)
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
    if not args.board:
        raise SystemExit("Board file not found: provide --board or use a subcommand such as 'pcb-plan init' or 'pcb-plan emit'.")
    return run_emit(args, legacy=True)


if __name__ == "__main__":
    raise SystemExit(main())
