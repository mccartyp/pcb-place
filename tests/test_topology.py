"""Topology-aware floorplanning and placement-ownership tests.

Covers the redesigned model: mechanical constraints first, functional signal
paths, power islands, edge-required connectors, spacing profiles, stackup
templates, and the groups-are-metadata / unique-ownership rules.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PLAN_CLI = ROOT / "src/pcb_plan/pcb_plan.py"
PLACE_CLI = ROOT / "src/pcb_place/pcb_place.py"

import pcb_plan
import pcb_place
from pcb_place import pcb_place as place_impl


def _footprint(ref: str, x: float, y: float, rot: float = 0, value: str = "", pads: str = "",
               footprint: str = "Test:Test", extra: str = "") -> str:
    return f'''  (footprint "{footprint}" (layer "F.Cu")
    (at {x} {y} {rot})
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "{value}" (at 0 1 0) (layer "F.Fab"))
{pads}{extra}  )'''


def _pad(num: str, x: float, y: float, net_id: int, net: str, size=(0.5, 0.6)) -> str:
    return f'    (pad "{num}" smd rect (at {x} {y}) (size {size[0]} {size[1]}) (layers "F.Cu") (net {net_id} "{net}"))\n'


def _board(width: float, height: float, footprints: str) -> str:
    return f'''(kicad_pcb (version 20240108) (generator "pcb-plan-test")
  (gr_rect (start 0 0) (end {width} {height}) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{footprints}
)
'''


# Four mounting holes piled in one area, an MCU, and an HDMI connector.
_MECHANICAL_BOARD = _board(60, 40, "\n".join([
    _footprint("H1", 30, 20, footprint="MountingHole:M3"),
    _footprint("H2", 31, 20, footprint="MountingHole:M3"),
    _footprint("H3", 30, 21, footprint="MountingHole:M3"),
    _footprint("H4", 31, 21, footprint="MountingHole:M3"),
    _footprint("U1", 30, 28, value="MCU", footprint="Package_QFP:TQFP-32",
               pads=_pad("1", -2, 0, 1, "3V3") + _pad("2", 2, 0, 2, "GND")),
    _footprint("J1", 4, 12, rot=270, value="HDMI_IN", footprint="Connector_HDMI:HDMI_A",
               pads=_pad("1", -1, 0, 3, "TMDS0_P") + _pad("2", 1, 0, 4, "TMDS0_N")),
]))


def _plan_for(board_text: str, tmp_path, intent_text: str = ""):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(board_text, encoding="utf-8")
    board, components, nets, warnings = pcb_plan.parse_board(board_path)
    intent = {}
    if intent_text:
        pln = tmp_path / "board.pln"
        pln.write_text(intent_text, encoding="utf-8")
        intent = pcb_plan.load_intent(pln)
    return pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), intent, warnings)


# ---------------------------------------------------------------------------
# Mechanical
# ---------------------------------------------------------------------------


def test_clustered_mounting_holes_distributed_to_distinct_corners(tmp_path):
    plan = _plan_for(_MECHANICAL_BOARD, tmp_path)
    corner_rules = [r for r in plan.rules if r.kind == "corner"]
    assert len(corner_rules) == 4
    corners = {a["corner"] for a in plan.mounting_hole_plan}
    assert corners == {"top_left", "top_right", "bottom_left", "bottom_right"}
    # Holes never become cluster members.
    for cluster in plan.clusters:
        assert not set(cluster["members"]) & {"H1", "H2", "H3", "H4"}


def test_distinct_corner_holes_keep_existing_positions(tmp_path):
    board = _board(60, 40, "\n".join([
        _footprint("H1", 3, 3, footprint="MountingHole:M3"),
        _footprint("H2", 57, 3, footprint="MountingHole:M3"),
        _footprint("H3", 3, 37, footprint="MountingHole:M3"),
        _footprint("H4", 57, 37, footprint="MountingHole:M3"),
    ]))
    plan = _plan_for(board, tmp_path)
    assert all(a["kind"] == "anchor" and a["source"] == "existing_distinct_corners"
               for a in plan.mounting_hole_plan)


def test_board_pln_mechanical_mounting_holes_win(tmp_path):
    intent = """
mechanical:
  mounting_holes:
    H1: { corner: bottom_right, inset: 4 }
"""
    plan = _plan_for(_MECHANICAL_BOARD, tmp_path, intent)
    h1 = next(a for a in plan.mounting_hole_plan if a["ref"] == "H1")
    assert h1 == {"ref": "H1", "kind": "corner", "corner": "bottom_right", "inset": 4.0, "source": "board.pln"}
    others = {a["corner"] for a in plan.mounting_hole_plan if a["ref"] != "H1"}
    assert "bottom_right" not in others


def test_mounting_holes_placed_apart_after_pcb_place(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_MECHANICAL_BOARD, encoding="utf-8")
    ppl = tmp_path / "placement.ppl"
    subprocess.run([sys.executable, str(PLAN_CLI), "--board", str(board_path), "-o", str(ppl)],
                   check=True, capture_output=True, text=True)
    report = tmp_path / "report.json"
    subprocess.run([sys.executable, str(PLACE_CLI), str(board_path), str(ppl), "-o",
                    str(tmp_path / "out.kicad_pcb"), "--report-json", str(report)],
                   check=True, capture_output=True, text=True)
    payload = json.loads(report.read_text())
    holes = {p["ref"]: (p["x"], p["y"]) for p in payload["placements"] if p["ref"].startswith("H")}
    assert len(holes) == 4
    positions = list(holes.values())
    for i, a in enumerate(positions):
        for b in positions[i + 1:]:
            assert abs(a[0] - b[0]) + abs(a[1] - b[1]) > 20  # distinct corners, not clustered
    mech = payload["mechanical_review"]
    assert not any("share nearest corner" in w for w in mech["warnings"])


# ---------------------------------------------------------------------------
# Edge connectors
# ---------------------------------------------------------------------------


def test_hdmi_connector_edge_required_with_rotation_and_body_extension(tmp_path):
    plan = _plan_for(_MECHANICAL_BOARD, tmp_path)
    cluster = next(r for r in plan.rules if r.kind == "cluster" and "J1" in r.refs)
    assert "edge_required=True" in cluster.text
    assert 'rot="auto"' in cluster.text
    assert "allow_body_outside_board=True" in cluster.text
    assert 'access_side="left"' in cluster.text


def test_board_pln_components_section_overrides_edge_side(tmp_path):
    intent = """
components:
  J1:
    edge_required: true
    access_side: top
    allow_body_outside_board: false
    rotation: 90
"""
    plan = _plan_for(_MECHANICAL_BOARD, tmp_path, intent)
    cluster = next(r for r in plan.rules if r.kind == "cluster" and "J1" in r.refs)
    assert 'edge="top"' in cluster.text
    assert 'access_side="top"' in cluster.text
    assert "allow_body_outside_board=False" in cluster.text
    assert "rot=90" in cluster.text


def test_edge_required_connector_forced_to_edge_from_mid_board(tmp_path):
    board = _board(60, 40, _footprint("J2", 30, 20, value="USB-C", footprint="Connector_USB:USB_C",
                                      pads=_pad("1", -1, 0, 1, "VBUS")))
    plan = _plan_for(board, tmp_path)
    cluster = next(r for r in plan.rules if r.kind == "cluster" and "J2" in r.refs)
    assert "placement=Edge(" in cluster.text
    assert "edge_required=True" in cluster.text


def test_access_side_rotation_convention_applied_by_pcb_place(tmp_path):
    assert place_impl.access_side_rotation("left") == 270.0
    assert place_impl.access_side_rotation("right") == 90.0
    assert place_impl.access_side_rotation("top") == 0.0
    assert place_impl.access_side_rotation("bottom") == 180.0

    pcb = _board(50, 30, _footprint("J1", 25, 15, rot=0, value="CONN",
                                    pads=_pad("1", 0, 0, 1, "SIG")))
    ppl = tmp_path / "rot.ppl"
    ppl.write_text('Board(width=50, height=30)\n'
                   'Edge("J1", edge="left", y=15, inset=2, rot="auto", access_side="left", '
                   'edge_required=True, allow_body_outside_board=True)\n', encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    _out, _messages, report = pcb_place.apply_placements(pcb, model)
    placement = next(p for p in report["placements"] if p["ref"] == "J1")
    assert placement["final_rot"] == 270.0
    assert "J1" in report["allowed_outside_board_refs"]


def test_cluster_edge_rotation_rotates_members_rigidly(tmp_path):
    pcb = _board(50, 30,
                 _footprint("J1", 25, 15, rot=0, value="CONN", pads=_pad("1", 0, 0, 1, "SIG")) + "\n" +
                 _footprint("C9", 27, 15, rot=0, value="100nF", pads=_pad("1", 0, 0, 1, "SIG")))
    ppl = tmp_path / "cluster.ppl"
    ppl.write_text('Board(width=50, height=30)\n'
                   'Cluster("CONN", anchor="J1", members=["J1", "C9"], '
                   'placement=Edge(edge="left", y=15, inset=2, rot="auto", access_side="left", '
                   'edge_required=True, allow_body_outside_board=True))\n', encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    _out, _messages, report = pcb_place.apply_placements(pcb, model)
    placements = {p["ref"]: p for p in report["placements"]}
    assert placements["J1"]["final_rot"] == 270.0
    assert placements["C9"]["final_rot"] == 270.0
    # Member offset (+2, 0) from anchor rotates by 270 deg to (0, -2).
    assert round(placements["C9"]["x"] - placements["J1"]["x"], 6) == 0.0
    assert round(placements["C9"]["y"] - placements["J1"]["y"], 6) == -2.0


def test_edge_connector_cluster_places_connector_alone(tmp_path):
    # An edge-locked, rotated connector must carry only itself in its rigid Edge
    # placement; its nearby support passive is placed interior by its own
    # pad-relative rule, never rotated off-board inside the connector cluster.
    board = _board(50, 30, "\n".join([
        _footprint("J1", 4, 15, rot=0, value="HDMI_IN", footprint="Connector_HDMI:HDMI_A",
                   pads=_pad("1", -1, 0, 1, "DDC_SCL") + _pad("2", 1, 0, 2, "GND")),
        _footprint("R1", 9, 15, rot=0, value="2k2",
                   pads=_pad("1", -0.4, 0, 1, "DDC_SCL") + _pad("2", 0.4, 0, 3, "3V3")),
    ]))
    intent = (
        "board: { width: 50, height: 30, origin_x: 0, origin_y: 0 }\n"
        "components:\n"
        "  J1: { role: hdmi_connector, edge_required: true, access_side: left, rotation: auto }\n"
    )
    plan = _plan_for(board, tmp_path, intent)
    edge_clusters = [r for r in plan.rules if r.kind == "cluster" and "placement=Edge(" in r.text
                     and 'anchor="J1"' in r.text]
    assert edge_clusters, "expected an edge cluster for J1"
    rule = edge_clusters[0]
    assert 'members=["J1"]' in rule.text  # connector alone; R1 is not rigidly carried
    assert "R1" not in rule.refs
    # The support resistor still has its own placement rule (pad-relative).
    assert any("R1" in r.refs and r.kind != "cluster" for r in plan.rules)


def test_connector_body_outside_board_flagged_when_not_allowed(tmp_path):
    wide_pads = _pad("1", -4, 0, 1, "SIG") + _pad("2", 4, 0, 1, "SIG")
    pcb = _board(50, 30, _footprint("J1", 2, 15, rot=0, value="CONN", pads=wide_pads))
    ppl = tmp_path / "edge.ppl"
    ppl.write_text('Board(width=50, height=30)\n'
                   'Edge("J1", edge="left", y=15, inset=2, edge_required=True)\n', encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    _out, _messages, report = pcb_place.apply_placements(pcb, model)
    mech = report["mechanical_review"]
    item = next(e for e in mech["edge_components"] if e["ref"] == "J1")
    assert item["body_outside_board"] is True
    assert item["allow_body_outside_board"] is False
    assert any("allow_body_outside_board" in w for w in mech["warnings"])


def test_non_edge_ic_is_not_placed_at_edge(tmp_path):
    plan = _plan_for(_MECHANICAL_BOARD, tmp_path)
    ic_clusters = [r for r in plan.rules if r.kind == "cluster" and "U1" in r.refs]
    assert ic_clusters
    assert all("placement=Anchor(" in r.text for r in ic_clusters)
    assert "U1" not in [item["ref"] for item in plan.edge_rotation_plan]


# ---------------------------------------------------------------------------
# Clusters are metadata / ownership
# ---------------------------------------------------------------------------

_OWNERSHIP_BOARD = _board(60, 40, "\n".join([
    _footprint("U1", 30, 20, value="MCU", footprint="Package_QFP:TQFP-32",
               pads=_pad("1", -2, 0, 1, "3V3") + _pad("2", 2, 0, 2, "GND") + _pad("3", 0, 2, 5, "SCL")),
    _footprint("C1", 28, 18, value="100nF", pads=_pad("1", -0.4, 0, 1, "3V3") + _pad("2", 0.4, 0, 2, "GND")),
    _footprint("R1", 32, 18, value="10k", pads=_pad("1", -0.4, 0, 1, "3V3") + _pad("2", 0.4, 0, 5, "SCL")),
]))


def test_cluster_membership_does_not_imply_placement_ownership(tmp_path):
    plan = _plan_for(_OWNERSHIP_BOARD, tmp_path)
    payload = pcb_plan.report(plan)
    cluster = next(c for c in plan.clusters if c["anchor"] == "U1")
    assert "C1" in cluster["members"]  # semantic group membership stays
    ownership = payload["ownership_model"]
    assert ownership["C1"]["owner"] == "decoupling"  # but placement owner is the refinement
    assert ownership["R1"]["owner"] == "pullup"
    assert ownership["U1"]["owner"] == "cluster"
    assert "clusters_are_metadata" in payload
    assert payload["duplicate_rules"] == []


def test_power_domain_group_does_not_pile_parts_at_regulator(tmp_path):
    # Two load ICs each with a local 3V3 cap, plus a regulator: load decouplers
    # must stay owned by their loads, not the regulator.
    board = _board(80, 40, "\n".join([
        _footprint("U7", 10, 30, value="Buck Reg", footprint="Package_TO_SOT_SMD:SOT-23-6_Regulator",
                   pads=_pad("1", -1, 0, 1, "VIN") + _pad("2", 1, 0, 2, "3V3") + _pad("3", 0, 1, 3, "GND")),
        _footprint("U1", 50, 10, value="MCU", footprint="Package_QFP:TQFP-32",
                   pads=_pad("1", -2, 0, 2, "3V3") + _pad("2", 2, 0, 3, "GND")),
        _footprint("U2", 70, 30, value="RETIMER", footprint="Package_QFN:QFN-40",
                   pads=_pad("1", -2, 0, 2, "3V3") + _pad("2", 2, 0, 3, "GND")),
        _footprint("C1", 48, 8, value="100nF", pads=_pad("1", -0.4, 0, 2, "3V3") + _pad("2", 0.4, 0, 3, "GND")),
        _footprint("C2", 68, 28, value="100nF", pads=_pad("1", -0.4, 0, 2, "3V3") + _pad("2", 0.4, 0, 3, "GND")),
    ]))
    plan = _plan_for(board, tmp_path)
    parents = {g["members"][0]: g["parent"] for g in plan.decoupling_groups}
    assert parents["C1"] == "U1"
    assert parents["C2"] == "U2"


def test_multi_group_membership_reported_without_conflicts(tmp_path):
    plan = _plan_for(_OWNERSHIP_BOARD, tmp_path)
    payload = pcb_plan.report(plan)
    assert "C1" in payload["multi_group_components"]
    groups = payload["semantic_groups"]["C1"]
    assert len(groups) > 1
    assert payload["duplicate_rules"] == []


# ---------------------------------------------------------------------------
# Spacing
# ---------------------------------------------------------------------------


def test_default_spacing_is_realistic_not_touching():
    rules = pcb_place.ClearanceRules()
    assert rules.passive_to_passive == 0.25
    assert rules.passive_to_ic == 0.40
    assert rules.ic_to_ic == 0.75
    assert rules.connector == 1.00
    assert rules.mechanical == 1.00


def test_spacing_profile_emitted_and_overridable(tmp_path):
    plan = _plan_for(_OWNERSHIP_BOARD, tmp_path)
    ppl = pcb_plan.emit_ppl(plan, tmp_path / "layout.kicad_pcb", None)
    assert "Spacing(default=0.25, passive_to_passive=0.25, passive_to_ic=0.4, ic_to_ic=0.75, connector=1, mechanical=1)" in ppl

    plan = _plan_for(_OWNERSHIP_BOARD, tmp_path, "spacing:\n  passive_to_passive: 0.5\n  ic_to_ic: 2.0\n")
    assert plan.spacing["passive_to_passive"] == 0.5
    assert plan.spacing["ic_to_ic"] == 2.0
    ppl = pcb_plan.emit_ppl(plan, tmp_path / "layout.kicad_pcb", None)
    assert "passive_to_passive=0.5" in ppl
    assert "ic_to_ic=2" in ppl


def test_spacing_connector_aliases_accepted_by_pcb_place(tmp_path):
    ppl = tmp_path / "spacing.ppl"
    ppl.write_text("Board(width=50, height=30)\n"
                   "Spacing(connector_to_component=1.5, mechanical_to_component=2.0)\n", encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    assert model.clearance.connector == 1.5
    assert model.clearance.mechanical == 2.0


def test_decoupling_array_escalates_spacing_instead_of_touching(tmp_path):
    caps = "\n".join(_footprint(f"C{i}", 40 + i, 25, pads=_pad("1", 0, 0, 1, "3V3", size=(1, 1)))
                     for i in range(1, 4))
    pcb = _board(50, 40, _footprint("U1", 20, 20, value="IC", footprint="Package_QFN:QFN-40",
                                    pads=_pad("1", -2, 0, 1, "3V3"),
                                    extra="    (fp_rect (start -2 -3) (end 2 3) (stroke (width 0.1) (type solid)) (fill none) (layer \"F.CrtYd\"))\n")
                 + "\n" + caps)
    ppl = tmp_path / "array.ppl"
    # 1.1 mm pitch on ~1 mm wide caps leaves < 0.25 mm clearance: the engine
    # must widen the array, not emit touching parts or fail.
    ppl.write_text('Board(width=50, height=40)\n'
                   'DecouplingArray(refs=["C1", "C2", "C3"], parent="U1", pad="1", spacing=1.1)\n',
                   encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    _out, _messages, report = pcb_place.apply_placements(pcb, model, strict=True)
    assert report["spacing_violation_count"] == 0
    assert report["collision_count"] == 0


# ---------------------------------------------------------------------------
# High-speed paths
# ---------------------------------------------------------------------------


def test_functional_path_inferred_connector_esd_ic(tmp_path):
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    board, components, nets, warnings = pcb_plan.parse_board(board_path)
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), {}, warnings)
    assert plan.functional_paths["HS_J1"]["sequence"] == ["J1", "U2", "U10"]
    assert plan.functional_paths["HS_J1"]["type"] == "high_speed_diff"
    ppl = pcb_plan.emit_ppl(plan, board_path, None)
    assert 'HighSpeedPath("HS_J1", sequence=["J1", "U2", "U10"]' in ppl


def test_explicit_functional_path_from_board_pln(tmp_path):
    intent = """
functional_paths:
  HDMI_IN:
    type: high_speed_diff
    sequence: [J1, U2, U10]
    corridor_width_mm: 8
    protect_first: true
"""
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    board, components, nets, warnings = pcb_plan.parse_board(board_path)
    pln = tmp_path / "board.pln"
    pln.write_text(intent, encoding="utf-8")
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(),
                                  pcb_plan.load_intent(pln), warnings)
    assert plan.functional_paths["HDMI_IN"]["corridor_width_mm"] == 8.0
    assert plan.functional_paths["HDMI_IN"]["source"] == "board.pln"
    assert "HS_J1" not in plan.functional_paths  # explicit path suppresses inference


def test_high_speed_review_flags_corridor_intruder_and_scores_esd(tmp_path):
    pcb = _board(60, 30, "\n".join([
        _footprint("J1", 4, 15, value="HDMI", pads=_pad("1", 0, 0, 1, "TMDS0_P")),
        _footprint("U2", 10, 15, value="ESD", pads=_pad("1", 0, 0, 1, "TMDS0_P")),
        _footprint("U10", 50, 15, value="RETIMER", pads=_pad("1", 0, 0, 1, "TMDS0_P")),
        _footprint("R5", 30, 15, value="10k", pads=_pad("1", 0, 0, 2, "OTHER")),
    ]))
    ppl = tmp_path / "hs.ppl"
    ppl.write_text('Board(width=60, height=30)\n'
                   'HighSpeedPath("HDMI_IN", sequence=["J1", "U2", "U10"], corridor_width=4)\n',
                   encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    _out, _messages, report = pcb_place.apply_placements(pcb, model)
    review = report["high_speed_paths"][0]
    assert review["straightness"] == 1.0
    assert review["protection"]["protection_position_ratio"] < 0.5  # ESD near connector
    assert "R5" in review["corridor_intruders"]
    assert any("intrude" in w for w in review["warnings"])


def test_unrelated_passive_steered_out_of_high_speed_corridor(tmp_path):
    # A satellite passive whose nominal slot is inside a declared high-speed
    # corridor should be scored away from it.
    pcb = _board(60, 30, "\n".join([
        _footprint("J1", 4, 15, value="HDMI", pads=_pad("1", 0, 0, 1, "TMDS0_P")),
        _footprint("U10", 50, 15, value="RETIMER", pads=_pad("1", 0, 0, 1, "TMDS0_P")),
        _footprint("R5", 30, 14, value="10k", pads=_pad("1", 0, 0, 2, "OTHER")),
    ]))
    ppl = tmp_path / "corridor.ppl"
    ppl.write_text('Board(width=60, height=30)\n'
                   'HighSpeedPath("HDMI_IN", sequence=["J1", "U10"], corridor_width=4)\n'
                   'Satellite("R5", parent="U10", side="left", distance=10)\n', encoding="utf-8")
    model = pcb_place.load_ppl(ppl)
    _out, _messages, report = pcb_place.apply_placements(pcb, model)
    review = report["high_speed_paths"][0]
    assert "R5" not in review["corridor_intruders"]


# ---------------------------------------------------------------------------
# Power islands
# ---------------------------------------------------------------------------

_BUCK_BOARD = _board(60, 40, "\n".join([
    _footprint("U7", 30, 30, value="Buck Reg", footprint="Package_TO_SOT_SMD:SOT-23-6_Regulator",
               pads=_pad("1", -1, 0, 1, "VIN") + _pad("2", 1, 0, 2, "SW") +
                    _pad("3", 0, 1, 3, "GND") + _pad("4", 1, 1, 6, "FB")),
    _footprint("L1", 36, 30, value="4.7uH", footprint="Inductor_SMD:L",
               pads=_pad("1", -0.5, 0, 2, "SW") + _pad("2", 0.5, 0, 4, "3V3")),
    _footprint("C11", 26, 28, value="10uF", pads=_pad("1", -0.4, 0, 1, "VIN") + _pad("2", 0.4, 0, 3, "GND")),
    _footprint("C12", 40, 28, value="22uF", pads=_pad("1", -0.4, 0, 4, "3V3") + _pad("2", 0.4, 0, 3, "GND")),
    _footprint("R13", 32, 33, value="100k", pads=_pad("1", -0.4, 0, 6, "FB") + _pad("2", 0.4, 0, 4, "3V3")),
    _footprint("U1", 15, 10, value="MCU", footprint="Package_QFP:TQFP-32",
               pads=_pad("1", -2, 0, 4, "3V3") + _pad("2", 2, 0, 3, "GND")),
    _footprint("C1", 13, 8, value="100nF", pads=_pad("1", -0.4, 0, 4, "3V3") + _pad("2", 0.4, 0, 3, "GND")),
]))


def test_power_island_topology_detected_and_emitted(tmp_path):
    plan = _plan_for(_BUCK_BOARD, tmp_path)
    island = next(i for i in plan.power_islands if i["regulator"] == "U7")
    assert island["inductor"] == "L1"
    assert island["input_caps"] == ["C11"]
    assert island["output_caps"] == ["C12"]
    assert island["feedback"] == ["R13"]
    assert island["switch_net"] == "SW"
    texts = "\n".join(r.text for r in plan.rules)
    assert 'PowerIsland("PWR_U7"' in texts
    assert 'role="input_cap"' in texts
    assert 'role="output_cap"' in texts
    assert 'role="feedback"' in texts
    assert 'role="inductor"' in texts
    # Load decoupler C1 belongs to the MCU, not the regulator island.
    assert "C1" not in island["input_caps"] + island["output_caps"]
    parents = {g["members"][0]: g["parent"] for g in plan.decoupling_groups}
    assert parents["C1"] == "U1"


def test_feedback_placed_near_fb_pin(tmp_path):
    plan = _plan_for(_BUCK_BOARD, tmp_path)
    fb_rule = next(r for r in plan.rules if r.refs and r.refs[0] == "R13")
    assert 'role="feedback"' in fb_rule.text
    assert 'pad="4"' in fb_rule.text  # the regulator's FB pad


def test_explicit_power_island_from_board_pln(tmp_path):
    intent = """
power_islands:
  BUCK_3V3:
    regulator: U7
    input_caps: [C11]
    inductor: L1
    output_caps: [C12]
    feedback: [R13]
    switch_node: SW
"""
    plan = _plan_for(_BUCK_BOARD, tmp_path, intent)
    island = next(i for i in plan.power_islands if i["regulator"] == "U7")
    assert island["name"] == "BUCK_3V3"
    assert island["source"] == "board.pln"


def test_power_island_placed_and_reviewed_end_to_end(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_BUCK_BOARD, encoding="utf-8")
    ppl = tmp_path / "placement.ppl"
    subprocess.run([sys.executable, str(PLAN_CLI), "--board", str(board_path), "-o", str(ppl)],
                   check=True, capture_output=True, text=True)
    report_path = tmp_path / "report.json"
    review_path = tmp_path / "power-placement-review.md"
    subprocess.run([sys.executable, str(PLACE_CLI), str(board_path), str(ppl), "-o",
                    str(tmp_path / "out.kicad_pcb"), "--report-json", str(report_path),
                    "--power-review", str(review_path)],
                   check=True, capture_output=True, text=True)
    payload = json.loads(report_path.read_text())
    island = payload["power_islands"][0]
    assert island["regulator"] == "U7"
    for item in island["input_caps"]:
        assert item["distance_mm"] is not None and item["distance_mm"] < 5.0
    assert island["inductor"]["distance_mm"] < 6.0
    assert island["estimated_hot_loop_area_mm2"] is not None
    assert "Power placement review" in review_path.read_text()


# ---------------------------------------------------------------------------
# Stackup profiles
# ---------------------------------------------------------------------------


def test_stackup_profile_expansion_six_layer():
    warnings = []
    stackup = pcb_plan.expand_stackup_intent({"profile": "6_layer_high_speed"}, warnings)
    assert [layer["name"] for layer in stackup["layers"]] == [
        "F.Cu", "In1.GND", "In2.PWR", "In3.SIG", "In4.GND", "B.Cu"]
    assert stackup["source"] == "stackup_template"
    assert stackup["confidence"] == "medium"
    assert stackup["requires_review"] is True
    assert stackup["reference_planes"] == ["In1.GND", "In4.GND"]
    assert any("impedance" in w for w in warnings)


def test_stackup_layer_count_expands_to_conservative_profile():
    warnings = []
    stackup = pcb_plan.expand_stackup_intent({"layers": 6}, warnings)
    assert stackup["profile"] == "6_layer_high_speed"
    assert len(stackup["layers"]) == 6
    assert stackup["requires_review"] is True


def test_stackup_unsupported_layer_count_warns():
    warnings = []
    pcb_plan.expand_stackup_intent({"layers": 7}, warnings)
    assert any("no built-in template" in w for w in warnings)


def test_stackup_profile_with_layer_names_list():
    warnings = []
    stackup = pcb_plan.expand_stackup_intent({
        "profile": "6_layer_high_speed",
        "layers": ["F.Cu", "In1.GND", "In2.PWR", "In3.SIG", "In4.GND", "B.Cu"],
    }, warnings)
    assert stackup["layers"][1] == {"name": "In1.GND", "type": "plane", "net": "GND"}


def test_all_supported_profiles_have_expected_layer_counts():
    for count, name in ((2, "2_layer_basic"), (4, "4_layer_signal_gnd_pwr_signal"),
                        (6, "6_layer_high_speed"), (8, "8_layer_high_speed"),
                        (10, "10_layer_high_speed")):
        profile = pcb_plan.STACKUP_PROFILES[name]
        assert len(profile["layers"]) == count
        assert profile["notes"]


def test_stackup_template_flows_into_plan_warnings(tmp_path):
    plan = _plan_for(_OWNERSHIP_BOARD, tmp_path, "stackup:\n  layers: 6\n")
    assert plan.stackup.get("profile") == "6_layer_high_speed"
    assert any("impedance" in w for w in plan.stackup_warnings)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_check_report_includes_topology_sections(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_BUCK_BOARD, encoding="utf-8")
    report_path = tmp_path / "pcb-plan-check-report.json"
    result = subprocess.run([sys.executable, str(PLAN_CLI), "check", "--board", str(board_path),
                             "--report-json", str(report_path)],
                            check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    expected = {"mechanical_constraints", "edge_required_components", "edge_rotation_plan",
                "functional_paths", "high_speed_paths", "power_islands", "ownership_model",
                "spacing_profile", "clusters_are_metadata", "placement_quality_score"}
    assert expected <= set(payload)
    assert payload["power_islands"][0]["regulator"] == "U7"
    assert payload["spacing_profile"]["passive_to_ic"] == 0.40


def test_ai_edit_hints_emitted_by_plan_cli(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_MECHANICAL_BOARD, encoding="utf-8")
    hints_path = tmp_path / "ai-edit-hints.md"
    subprocess.run([sys.executable, str(PLAN_CLI), "emit", "--board", str(board_path),
                    "-o", str(tmp_path / "placement.ppl"), "--ai-edit-hints", str(hints_path)],
                   check=True, capture_output=True, text=True)
    text = hints_path.read_text()
    assert "# AI edit hints" in text
    assert "Placement owners" in text
    assert "Suggested board.pln edits" in text
    assert "Risks requiring engineering review" in text


def test_place_reviews_and_hints_emitted_by_place_cli(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_MECHANICAL_BOARD, encoding="utf-8")
    ppl = tmp_path / "placement.ppl"
    subprocess.run([sys.executable, str(PLAN_CLI), "--board", str(board_path), "-o", str(ppl)],
                   check=True, capture_output=True, text=True)
    hs = tmp_path / "high-speed-placement-review.md"
    mech = tmp_path / "mechanical-placement-review.md"
    hints = tmp_path / "ai-edit-hints.md"
    subprocess.run([sys.executable, str(PLACE_CLI), str(board_path), str(ppl),
                    "-o", str(tmp_path / "out.kicad_pcb"), "--high-speed-review", str(hs),
                    "--mechanical-review", str(mech), "--ai-edit-hints", str(hints)],
                   check=True, capture_output=True, text=True)
    assert "High-speed placement review" in hs.read_text()
    mech_text = mech.read_text()
    assert "Mounting holes" in mech_text
    assert "Edge components" in mech_text
    hints_text = hints.read_text()
    assert "Placement ownership" in hints_text


def test_pln_init_includes_new_sections(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_BUCK_BOARD, encoding="utf-8")
    pln = tmp_path / "board.pln"
    subprocess.run([sys.executable, str(PLAN_CLI), "init", "--board", str(board_path), "-o", str(pln)],
                   check=True, capture_output=True, text=True)
    payload = pcb_plan.load_intent(pln)
    plain = pcb_plan.unwrap_provenance(payload)
    assert "spacing" in plain
    assert plain["spacing"]["passive_to_ic"] == 0.4
    assert "power_islands" in plain
    assert plain["power_islands"]["PWR_U7"]["regulator"] == "U7"
    assert pcb_plan.validate_pln(payload) == []
