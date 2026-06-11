import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
        [sys.executable, str(ROOT / "pcb_plan.py"), "--board", str(board_path), "-o", str(out), "--report-json", str(report)],
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
        [sys.executable, str(ROOT / "pcb_plan.py"), "--board", str(board_path), "--explain", "U2"],
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
        [sys.executable, str(ROOT / "pcb_plan.py"), "--board", str(board_path), "-o", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    ppl = out.read_text()
    assert 'Keepout("U12_ANTENNA", x=42.000, y=0.000, w=12.000, h=7.250, role="rf")' in ppl
    subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(board_path), str(out), "--dry-run", "--strict"],
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
            str(ROOT / "pcb_plan.py"),
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
