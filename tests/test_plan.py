import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PLAN_CLI = ROOT / "src/pcb_plan/pcb_plan.py"
PLACE_CLI = ROOT / "src/pcb_place/pcb_place.py"

import pcb_plan


def test_kicad_board_parser_pads_geometry_and_transform():
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/simple_mcu/layout.kicad_pcb")
    assert board.width == 50
    assert board.height == 30
    assert set(components) == {"U1", "C1", "R1"}
    c1 = components["C1"]
    assert c1.pads[0].number == "1"
    assert c1.pads[0].shape == "rect"
    assert c1.pads[0].size == (0.5, 0.6)
    # C1 has footprint rotation 90 degrees, so local (-0.4, 0) rotates to approximately (0, -0.4).
    assert round(c1.pads[0].abs_x, 3) == 21.0
    assert round(c1.pads[0].abs_y, 3) == 11.6
    assert nets["3V3"].pads


def test_netlist_alias_import_and_connectivity_graph():
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/zener_generated_board/layout.kicad_pcb")
    aliases, diagnostics, net_warnings = pcb_plan.import_netlist(ROOT / "tests/fixtures/zener_generated_board/default.net", components, nets)
    assert aliases["HDMI.J_IN"] == "J1"
    assert diagnostics.parser == "json"
    assert ("J1", "1") in nets["TMDS0_P"].pads


def test_inference_diff_pair_decoupling_esd_pullup_series_cluster_region_keepout():
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/simple_mcu/layout.kicad_pcb")
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), {}, warnings)
    texts = "\n".join(rule.text for rule in plan.rules)
    assert 'Decoupling("C1"' in texts
    assert 'Pullup("R1"' in texts
    assert any(c["anchor"] == "U1" for c in plan.clusters)

    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb")
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), {}, warnings)
    texts = "\n".join(rule.text for rule in plan.rules)
    assert plan.differential_pairs[0].p == "TMDS0_P"
    assert 'Corridor("TMDS0_CORRIDOR"' in texts
    assert 'ESD("U2"' in texts
    assert "HIGH_SPEED" in plan.regions

    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/rf_module/layout.kicad_pcb")
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), {}, warnings)
    assert any(rule.kind == "keepout" and "ANTENNA" in rule.text for rule in plan.rules)


def test_ppl_emission_report_json_and_explain(tmp_path):
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = tmp_path / "placement.ppl"
    report = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(PLAN_CLI), "--board", str(board_path), "-o", str(out), "--report-json", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    ppl = out.read_text()
    assert "Generated placement plan" in ppl
    assert "Requires engineering review before fabrication" in ppl
    assert 'Cluster(' in ppl
    payload = json.loads(report.read_text())
    assert payload["components_parsed"] == 3
    assert payload["inferred_differential_pairs"]
    assert any(rule["kind"] == "esd" for rule in payload["generated_rules"])

    explain = subprocess.run(
        [sys.executable, str(PLAN_CLI), "--board", str(board_path), "--explain", "U2"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "U2:" in explain.stdout
    assert "role: esd_protection" in explain.stdout


def test_rf_module_synthesized_keepout_does_not_reject_generated_plan(tmp_path):
    board_path = ROOT / "tests/fixtures/rf_module/layout.kicad_pcb"
    out = tmp_path / "placement.ppl"
    subprocess.run(
        [sys.executable, str(PLAN_CLI), "--board", str(board_path), "-o", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    ppl = out.read_text()
    assert 'Keepout("U12_ANTENNA", x=42.000, y=0.000, w=12.000, h=7.250, role="rf")' in ppl
    subprocess.run(
        [sys.executable, str(PLACE_CLI), str(board_path), str(out), "--dry-run", "--strict"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_json_and_simple_yaml_intent_are_optional_and_honored(tmp_path):
    intent = tmp_path / "board.pln"
    intent.write_text(
        """
board:
  width: 74
  height: 40
  origin_x: 0
  origin_y: 0
fixed:
  H1: { type: corner, corner: top_left, inset: 3 }
roles:
  U10: hdmi_retimer
regions:
  HIGH_SPEED: { x: 0, y: 0, w: 74, h: 20 }
keepouts:
  - name: WIFI_ANTENNA
    x: 55
    y: 5
    w: 14
    h: 18
    role: rf
"""
    )
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb")
    parsed = pcb_plan.load_intent(intent)
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), parsed, warnings)
    texts = "\n".join(rule.text for rule in plan.rules)
    assert 'Region("HIGH_SPEED", x=0, y=0, w=74, h=20)' in texts
    assert 'Keepout("WIFI_ANTENNA"' in texts
    assert plan.roles["U10"] == "hdmi_retimer"


def test_fixture_expected_fragments_are_documented(tmp_path):
    cases = [
        ("simple_mcu", None),
        ("high_speed_connector", None),
        ("buck_regulator", None),
        ("rf_module", None),
        ("zener_generated_board", "default.net"),
    ]
    for fixture, netlist_name in cases:
        fixture_dir = ROOT / "tests/fixtures" / fixture
        board_path = fixture_dir / "layout.kicad_pcb"
        board, components, nets, warnings = pcb_plan.parse_board(board_path)
        aliases = {}
        diagnostics = pcb_plan.AliasDiagnostics()
        if netlist_name:
            aliases, diagnostics, net_warnings = pcb_plan.import_netlist(fixture_dir / netlist_name, components, nets)
            warnings.extend(net_warnings)
        plan = pcb_plan.generate_plan(board, components, nets, aliases, diagnostics, {}, warnings)
        ppl = pcb_plan.emit_ppl(plan, board_path, fixture_dir / netlist_name if netlist_name else None)
        for fragment in (fixture_dir / "expected.ppl-fragments").read_text().splitlines():
            assert fragment in ppl
        expected_fields = json.loads((fixture_dir / "expected-report-fields.json").read_text())
        payload = pcb_plan.report(plan)
        for field in expected_fields["required_fields"]:
            assert field in payload
        if "aliases_recovered" in expected_fields:
            for alias, ref in expected_fields["aliases_recovered"].items():
                assert payload["aliases_recovered"][alias] == ref


def _routing_intent_text(**overrides):
    p_net = overrides.get("p_net", "TMDS0_P")
    n_net = overrides.get("n_net", "TMDS0_N")
    ref_plane = overrides.get("ref_plane", "In1.GND")
    impedance = overrides.get("impedance", "100")
    width = overrides.get("width", "0.12")
    spacing = overrides.get("spacing", "0.15")
    return f"""
board:
  width: 74
  height: 74
  origin_x: 140.23
  origin_y: 52.465
  units: mm
stackup:
  layers:
    - name: F.Cu
      type: signal
      copper_oz: 1
    - name: In1.GND
      type: plane
      net: GND
    - name: In2.PWR
      type: plane
      net: 3V3
    - name: B.Cu
      type: signal
  dielectric:
    - between: [F.Cu, In1.GND]
      material: FR4
      thickness_mm: 0.18
      er: 4.2
routing:
  mode: all_nets_constrained
  defaults:
    trace_width_mm: 0.15
    clearance_mm: 0.15
    via_policy: allow
    preferred_layers: [F.Cu, B.Cu]
  classes:
    low_speed:
      trace_width_mm: 0.15
      clearance_mm: 0.15
      preferred_layers: [F.Cu, B.Cu]
      via_policy: allow
    high_speed_diff:
      differential: true
      impedance_ohms: {impedance}
      trace_width_mm: {width}
      trace_spacing_mm: {spacing}
      preferred_layer: F.Cu
      reference_plane: {ref_plane}
      max_skew_mm: 0.25
      max_length_mismatch_mm: 0.25
      via_policy: avoid
      max_vias: 0
    switching_power:
      route: constrained
      keep_short: true
      avoid_regions: [HIGH_SPEED, RF]
      via_policy: avoid
differential_pairs:
  HDMI_TMDS0:
    p: {p_net}
    n: {n_net}
    class: high_speed_diff
net_classes:
  TMDS*:
    class: high_speed_diff
  SW_NODE*:
    class: switching_power
routing_overrides:
  TMDS0_P:
    preferred_layer: F.Cu
    max_vias: 0
simulation:
  openems:
    enabled: auto
    trigger_on:
      - high_speed_diff
      - switching_power_near_high_speed
    export_dir: simulation/openems
    notes: advisory_only
"""


def _plan_from_intent(tmp_path, text):
    intent_path = tmp_path / "board.pln"
    intent_path.write_text(text)
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb")
    intent = pcb_plan.load_intent(intent_path)
    return pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), intent, warnings)


def test_pln_parses_stackup_routing_classes_and_differential_pairs(tmp_path):
    plan = _plan_from_intent(tmp_path, _routing_intent_text())
    assert [layer["name"] for layer in plan.stackup["layers"]] == ["F.Cu", "In1.GND", "In2.PWR", "B.Cu"]
    assert plan.routing["mode"] == "all_nets_constrained"
    assert plan.routing["classes"]["high_speed_diff"]["impedance_ohms"] == 100
    assert plan.declared_differential_pairs["HDMI_TMDS0"]["p"] == "TMDS0_P"
    assert plan.high_speed_constraints_complete is True


def test_pln_warns_for_missing_reference_plane_and_missing_pair_net(tmp_path):
    plan = _plan_from_intent(tmp_path, _routing_intent_text(ref_plane="In9.MISSING", n_net="TMDS9_N"))
    warnings = "\n".join(plan.warnings)
    assert "reference plane 'In9.MISSING'" in warnings
    assert "N net 'TMDS9_N'" in warnings
    assert plan.high_speed_constraints_complete is False


def test_pln_validates_impedance_width_spacing(tmp_path):
    plan = _plan_from_intent(tmp_path, _routing_intent_text(impedance="fast", width="0", spacing="-0.1"))
    warnings = "\n".join(plan.warnings)
    assert "impedance_ohms must be numeric" in warnings
    assert "trace_width_mm must be positive" in warnings
    assert "trace_spacing_mm must be positive" in warnings


def test_routing_report_json_and_ppl_comments(tmp_path):
    plan = _plan_from_intent(tmp_path, _routing_intent_text())
    payload = pcb_plan.report(plan)
    assert payload["routing"]["mode"] == "all_nets_constrained"
    assert payload["routing"]["differential_pairs"]["HDMI_TMDS0"]["class"] == "high_speed_diff"
    assert payload["stackup"]["reference_planes"] == ["In1.GND", "In2.PWR"]
    assert payload["simulation"]["openems_enabled"] is True
    ppl = pcb_plan.emit_ppl(plan, ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb", None)
    assert "# Routing constraints from board.pln:" in ppl
    assert "# HDMI_TMDS0: 100 ohm differential, F.Cu over In1.GND, max skew 0.25 mm" in ppl
    assert "Routing performed later by orchestrator/KiCadRoutingTools." in ppl


def test_emit_routing_policy_and_openems_plan_auto_mode(tmp_path):
    intent_path = tmp_path / "board.pln"
    intent_path.write_text(_routing_intent_text())
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = tmp_path / "placement.ppl"
    report = tmp_path / "pcb-plan-report.json"
    policy = tmp_path / "routing-policy.yaml"
    openems = tmp_path / "simulation/openems/openems-plan.yaml"
    subprocess.run(
        [
            sys.executable,
            str(PLAN_CLI),
            "--board",
            str(board_path),
            "--intent",
            str(intent_path),
            "-o",
            str(out),
            "--report-json",
            str(report),
            "--emit-routing-policy",
            str(policy),
            "--emit-openems-plan",
            str(openems),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "route_traces: false" in policy.read_text()
    assert "KiCadRoutingTools" in policy.read_text()
    openems_text = openems.read_text()
    assert "critical_nets:" in openems_text
    assert "TMDS0_P" in openems_text
    assert "advisory_warning:" in openems_text
    assert json.loads(report.read_text())["routing"]["high_speed_constraints_complete"] is True


def test_plan_init_review_explain_emit_lifecycle(tmp_path):
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    pln = tmp_path / "board.pln"
    init_report = tmp_path / "pcb-plan-init-report.json"
    subprocess.run(
        [sys.executable, str(PLAN_CLI), "init", "--board", str(board_path), "-o", str(pln), "--report-json", str(init_report)],
        check=True,
        capture_output=True,
        text=True,
    )
    pln_text = pln.read_text()
    assert "differential_pairs:" in pln_text
    assert "requires_review:" in pln_text
    init_payload = json.loads(init_report.read_text())
    assert init_payload["action"] == "init"
    assert "routing" in init_payload
    assert init_payload["review_required_items"]

    review = subprocess.run(
        [sys.executable, str(PLAN_CLI), "review", "--pln", str(pln)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "board.pln review" in review.stdout
    assert "Provenance and confidence" in review.stdout

    explain = subprocess.run(
        [sys.executable, str(PLAN_CLI), "explain", "routing.classes.high_speed_diff", "--pln", str(pln)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "routing.classes.high_speed_diff" in explain.stdout
    assert "why it exists" in explain.stdout

    ppl = tmp_path / "placement.ppl"
    subprocess.run(
        [sys.executable, str(PLAN_CLI), "emit", "--pln", str(pln), "--board", str(board_path), "-o", str(ppl)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Generated placement plan" in ppl.read_text()


def test_plan_update_generates_reviewable_patch(tmp_path):
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    pln = tmp_path / "board.pln"
    subprocess.run(
        [sys.executable, str(PLAN_CLI), "init", "--board", str(board_path), "-o", str(pln)],
        check=True,
        capture_output=True,
        text=True,
    )
    openems_report = tmp_path / "openems-report.json"
    openems_report.write_text(json.dumps({"suggestion": "increase antenna keepout"}))
    updated = tmp_path / "board.updated.pln"
    patch = tmp_path / "board.pln.patch"
    update_report = tmp_path / "pcb-plan-update-report.json"
    subprocess.run(
        [
            sys.executable,
            str(PLAN_CLI),
            "update",
            "--pln",
            str(pln),
            "--openems-report",
            str(openems_report),
            "-o",
            str(updated),
            "--patch",
            str(patch),
            "--report-json",
            str(update_report),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "openems_feedback" in updated.read_text()
    assert "--- " in patch.read_text()
    assert "+++ " in patch.read_text()
    payload = json.loads(update_report.read_text())
    assert "openems" in payload["reports_consumed"]


def test_zener_sexp_default_net_connectivity_and_aliases(tmp_path):
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/zener_generated_board/layout.kicad_pcb")
    netlist = tmp_path / "default.net"
    netlist.write_text(
        '''(netlist
  (components
    (component (path "HDMI.J_IN") (ref "J1") (value "HDMI_IN"))
    (component (path "HDMI.U_ESD") (ref "U2") (value "ESD"))
    (component (path "HDMI.U_RETIMER") (ref "U10") (value "HDMI Retimer")))
  (nets
    (net (code "1") (name "TMDS0_P")
      (node (ref "J1") (pin "1"))
      (node (ref "U2") (pin "1"))
      (node (ref "U10") (pin "1")))
    (net (code "2") (name "TMDS0_N")
      (node (ref "J1") (pin "2"))
      (node (ref "U2") (pin "2"))
      (node (ref "U10") (pin "2")))))
''',
        encoding="utf-8",
    )
    aliases, diagnostics, net_warnings = pcb_plan.import_netlist(netlist, components, nets)
    warnings.extend(net_warnings)
    plan = pcb_plan.generate_plan(board, components, nets, aliases, diagnostics, {}, warnings)
    payload = pcb_plan.report(plan)
    assert diagnostics.parser == "sexp"
    assert aliases["HDMI.J_IN"] == "J1"
    assert payload["nets_parsed"] > 0
    assert payload["clusters_multi_member"] >= 1
    assert payload["diff_pairs_inferred"] == 1


def test_board_pln_geometry_overrides_edge_cuts_and_footprint_fallback(tmp_path):
    source = (ROOT / "tests/fixtures/simple_mcu/layout.kicad_pcb").read_text(encoding="utf-8")
    no_edge = tmp_path / "layout.kicad_pcb"
    no_edge.write_text("\n".join(line for line in source.splitlines() if "Edge.Cuts" not in line), encoding="utf-8")
    pln = tmp_path / "board.pln"
    pln.write_text("""
board:
  width: 74
  height: 74
  origin_x: 0
  origin_y: 0
""", encoding="utf-8")
    plan, _ = pcb_plan._load_plan_from_inputs(no_edge, None, pln)
    payload = pcb_plan.report(plan)
    assert plan.board.width == 74
    assert plan.board.height == 74
    assert plan.board.source == "board.pln"
    assert not any("footprint extents" in warning for warning in payload["warnings"])


def test_report_quality_metrics_and_nearpad_rule():
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/simple_mcu/layout.kicad_pcb")
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), {}, warnings)
    payload = pcb_plan.report(plan)
    assert payload["components_placed"] == 3
    assert payload["components_unplaced"] == 0
    assert payload["clusters_multi_member"] == 1
    assert payload["generated_decoupling_rules"] == 1
    assert payload["generated_pullup_rules"] == 1
    # A single decoupling cap per IC power pin uses Decoupling() directly;
    # no redundant NearPad() rule is emitted for the same component.
    assert not any(rule.kind == "nearpad" and rule.refs and rule.refs[0] == "C1" for rule in plan.rules)
    assert payload["decoupling_groups"] == [
        {"parent": "U1", "power_net": "3V3", "pad": "1", "members": ["C1"], "primitive": "Decoupling", "grouped": False}
    ]
    assert payload["pullup_groups"] == [
        {"owner": "U1", "members": ["R1"], "primitive": "Pullup", "grouped": False}
    ]
    assert payload["duplicate_rules"] == []
    assert payload["failed_topology_inference"] == []
    assert payload["cluster_quality_score"] == 1.0
    assert payload["series_components_validated"] == 0
    assert payload["primitive_counts"]["Decoupling"] == 1
    assert payload["primitive_counts"]["Pullup"] == 1


def test_grouped_decoupling_and_pullup_arrays_and_series_validation():
    board, components, nets, warnings = pcb_plan.parse_board(ROOT / "tests/fixtures/grouped_support/layout.kicad_pcb")
    plan = pcb_plan.generate_plan(board, components, nets, {}, pcb_plan.AliasDiagnostics(), {}, warnings)
    payload = pcb_plan.report(plan)

    # Three decoupling caps sharing U10's 3V3 pad are grouped into a single
    # DecouplingArray(), not per-cap Decoupling()/NearPad() rules.
    assert len(plan.decoupling_groups) == 1
    decoupling_group = plan.decoupling_groups[0]
    assert decoupling_group["parent"] == "U10"
    assert decoupling_group["primitive"] == "DecouplingArray"
    assert decoupling_group["grouped"] is True
    assert sorted(decoupling_group["members"]) == ["C31", "C32", "C33"]
    assert not any(rule.kind in {"decoupling", "nearpad"} for rule in plan.rules if rule.kind != "decoupling_array")
    assert all(rule.text.startswith("DecouplingArray(") for rule in plan.rules if rule.kind == "decoupling_array")
    decoupling_array_rules = [rule for rule in plan.rules if rule.kind == "decoupling_array"]
    assert len(decoupling_array_rules) == 1  # one rule covers all members, no duplicates
    assert 'stagger=True' in decoupling_array_rules[0].text
    assert 'rows="auto"' in decoupling_array_rules[0].text
    assert "inferred_pad_side" in decoupling_group
    assert "parent_near_board_edge" in decoupling_group
    assert "stagger_recommended" in decoupling_group

    # Two pullups on different signal nets owned by the same MCU are grouped
    # by ownership rather than emitted as context-less Pullup() rules.
    assert len(plan.pullup_groups) == 1
    pullup_group = plan.pullup_groups[0]
    assert pullup_group["owner"] == "U10"
    assert pullup_group["primitive"] == "PullupArray"
    assert pullup_group["grouped"] is True
    assert sorted(pullup_group["members"]) == ["R21", "R22"]
    assert all(rule.text.startswith("PullupArray(") for rule in plan.rules if rule.kind == "pullup_array")
    pullup_array_rules = [rule for rule in plan.rules if rule.kind == "pullup_array"]
    assert len(pullup_array_rules) == 1  # one rule covers all members, no duplicates

    # R10 sits on a true A -> resistor -> B signal path (J1 -> R10 -> U10)
    # and is validated as a Series() component.
    assert any(rule.kind == "series" and 'Series("R10", a="J1", b="U10"' in rule.text for rule in plan.rules)
    assert payload["series_components_validated"] == 1

    # R11 only shares its two nets with other passive support components, so
    # the connectivity graph rejects the false Series() inference.
    assert not any(rule.kind == "series" and "R11" in rule.refs for rule in plan.rules)
    assert any("R11" in failure and "rejected Series()" in failure for failure in payload["failed_topology_inference"])

    assert payload["duplicate_rules"] == []


def test_pcb_plan_check_report(tmp_path):
    board_path = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    report_path = tmp_path / "pcb-plan-check-report.json"
    result = subprocess.run(
        [sys.executable, str(PLAN_CLI), "check", "--board", str(board_path), "--report-json", str(report_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload == json.loads(report_path.read_text())

    expected_fields = {
        "nets_parsed", "components_parsed", "components_planned", "unplaced_components",
        "clusters_total", "single_member_clusters", "multi_member_clusters", "duplicate_rule_refs",
        "decoupling_groups", "pullup_groups", "series_components_validated", "invalid_series_candidates",
        "differential_pairs_inferred", "high_speed_constraints_complete", "simulation_triggers",
        "warnings", "plan_confidence",
    }
    assert expected_fields <= set(payload)
    assert payload["nets_parsed"] > 0
    assert payload["components_parsed"] == 3
    assert payload["differential_pairs_inferred"] == 1
    assert payload["plan_confidence"]["level"] in {"high", "medium", "low"}
    assert isinstance(payload["plan_confidence"]["score"], int)
    assert isinstance(payload["plan_confidence"]["reasons"], list)


_LOW_CONFIDENCE_BOARD = '''(kicad_pcb (version 20240108) (generator "pcb-plan-test")
  (footprint "Package_QFP:TQFP-32" (layer "F.Cu")
    (at 25 15 0)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "MCU" (at 0 1 0) (layer "F.Fab"))
    (pad "1" smd rect (at -2 -2) (size 0.4 1.2) (layers "F.Cu"))
    (pad "2" smd rect (at 2 -2) (size 0.4 1.2) (layers "F.Cu"))
  )
  (footprint "Capacitor_SMD:C_0402" (layer "F.Cu")
    (at 21 12 90)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "100nF" (at 0 1 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.4 0) (size 0.5 0.6) (layers "F.Cu"))
    (pad "2" smd rect (at 0.4 0) (size 0.5 0.6) (layers "F.Cu"))
  )
)
'''


def test_pcb_plan_emit_warns_on_low_confidence_plan(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_LOW_CONFIDENCE_BOARD, encoding="utf-8")
    out = tmp_path / "placement.ppl"
    report_path = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(PLAN_CLI), "emit", "--board", str(board_path), "-o", str(out), "--report-json", str(report_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "WARNING" in result.stderr
    assert "plan confidence is low" in result.stderr
    payload = json.loads(report_path.read_text())
    assert payload["plan_confidence"]["level"] == "low"
    assert "# Plan confidence: low" in out.read_text()


def test_pcb_plan_emit_strict_confidence_fails(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_LOW_CONFIDENCE_BOARD, encoding="utf-8")
    out = tmp_path / "placement.ppl"
    result = subprocess.run(
        [sys.executable, str(PLAN_CLI), "emit", "--board", str(board_path), "-o", str(out), "--strict-confidence"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "plan confidence is low" in result.stderr
    assert "--allow-low-confidence" in result.stderr


def test_pcb_plan_emit_allow_low_confidence_override(tmp_path):
    board_path = tmp_path / "layout.kicad_pcb"
    board_path.write_text(_LOW_CONFIDENCE_BOARD, encoding="utf-8")
    out = tmp_path / "placement.ppl"
    result = subprocess.run(
        [sys.executable, str(PLAN_CLI), "emit", "--board", str(board_path), "-o", str(out),
         "--strict-confidence", "--allow-low-confidence"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert out.exists()
    assert "# Plan confidence: low" in out.read_text()
