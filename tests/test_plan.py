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
