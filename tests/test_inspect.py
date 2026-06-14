import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PLAN_CLI = ROOT / "src/pcb_plan/pcb_plan.py"

import pcb_plan


EXPECTED_FILES = {
    "board-hints.json",
    "board-hints.md",
    "component-table.csv",
    "connectivity-graph.json",
    "footprint-bboxes.json",
    "pad-locations.json",
    "candidate-functional-paths.json",
    "candidate-power-islands.json",
    "candidate-high-speed-paths.json",
    "candidate-edge-connectors.json",
    "candidate-mechanicals.json",
    "candidate-rf-zones.json",
    "routing-classes.json",
    "ai-pln-prompt.md",
    "ai-placement-review.md",
}


def _run_inspect(tmp_path, board, extra=None):
    out = tmp_path / "planning-hints"
    cmd = [sys.executable, str(PLAN_CLI), "inspect", "--board", str(board), "--out", str(out)]
    cmd.extend(extra or [])
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def test_inspect_emits_all_planning_hints(tmp_path):
    board = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = _run_inspect(tmp_path, board, ["--width", "75", "--height", "75"])
    written = {p.name for p in out.iterdir()}
    assert EXPECTED_FILES <= written

    hints = json.loads((out / "board-hints.json").read_text())
    assert hints["board_geometry"]["width"] == 75.0
    assert hints["counts"]["components"] == 3
    # candidate roles refine coarse roles into AI-friendly hints
    assert hints["candidate_roles"]["J1"] == "hdmi_connector"
    assert hints["candidate_roles"]["U2"] == "esd"
    assert hints["candidate_high_speed_paths"], "expected a high-speed path"


def test_inspect_edge_connectors_and_rotation_hints(tmp_path):
    board = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = _run_inspect(tmp_path, board, ["--width", "75", "--height", "75"])
    edges = json.loads((out / "candidate-edge-connectors.json").read_text())
    j1 = next(e for e in edges if e["ref"] == "J1")
    assert j1["candidate_edge_required"] is True
    assert j1["candidate_access_side"] in {"left", "right", "top", "bottom"}
    # auto rotation must follow top->0, right->90, bottom->180, left->270
    expected = {"top": 0.0, "right": 90.0, "bottom": 180.0, "left": 270.0}
    assert j1["candidate_rotation_auto_deg"] == expected[j1["candidate_access_side"]]
    assert j1["metadata_only"] is True


def test_inspect_power_island_is_candidate_only(tmp_path):
    board = ROOT / "tests/fixtures/buck_regulator/layout.kicad_pcb"
    out = _run_inspect(tmp_path, board, ["--width", "40", "--height", "40"])
    islands = json.loads((out / "candidate-power-islands.json").read_text())
    assert islands, "expected at least one candidate power island"
    assert all(island["source"].startswith("inferred") or island["source"] == "board.pln"
               for island in islands)


def test_inspect_kicad_groups_are_metadata_only(tmp_path):
    board = tmp_path / "grouped.kicad_pcb"
    board.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (footprint "Test:U" (layer "F.Cu")\n'
        '    (uuid "uuid-u1")\n'
        '    (at 20 20)\n'
        '    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at -1 -1) (size 1 1) (layers "F.Cu") (net 1 "GND"))\n'
        '  )\n'
        '  (footprint "Test:C" (layer "F.Cu")\n'
        '    (uuid "uuid-c1")\n'
        '    (at 24 20)\n'
        '    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at -1 -1) (size 1 1) (layers "F.Cu") (net 1 "GND"))\n'
        '  )\n'
        '  (group "support-cluster" (uuid "group-uuid-1")\n'
        '    (members "uuid-u1" "uuid-c1")\n'
        '  )\n'
        ')\n',
        encoding="utf-8",
    )
    out = _run_inspect(tmp_path, board, ["--width", "40", "--height", "40"])
    hints = json.loads((out / "board-hints.json").read_text())
    groups = hints["imported_kicad_groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["name"] == "support-cluster"
    assert group["members"] == ["C1", "U1"]
    assert group["metadata_only"] is True
    assert "must not drive placement" in hints["kicad_groups_note"]


def test_inspect_parse_kicad_groups_unit():
    text = (
        '(group "g1" (uuid "x") (members "uuid-a" "uuid-missing"))\n'
    )
    comp = pcb_plan.PlanComponent(
        ref="A1", footprint="fp", uuid="uuid-a", value=None, x=0, y=0, rot=0,
        layer="F.Cu", pads=[], bbox=None,
    )
    groups = pcb_plan.parse_kicad_groups(text, {"A1": comp})
    assert groups[0]["members"] == ["A1"]
    assert groups[0]["unresolved_member_uuids"] == ["uuid-missing"]


def test_inspect_honors_unsupported_stackup_layers(tmp_path):
    board = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = _run_inspect(tmp_path, board, ["--width", "75", "--height", "75", "--stackup-layers", "12"])
    hints = json.loads((out / "board-hints.json").read_text())
    stackup = hints["stackup_assumptions"]
    # The requested layer count must be preserved, not replaced by 4/2-layer heuristics.
    assert stackup["layers"] == 12
    assert stackup["source"] == "requested_layer_count_no_template"
    assert stackup.get("requires_review") is True
    assert any("--stackup-layers=12" in w for w in hints["warnings"])


def test_inspect_supported_stackup_layers_uses_template(tmp_path):
    board = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = _run_inspect(tmp_path, board, ["--width", "75", "--height", "75", "--stackup-layers", "6"])
    stackup = json.loads((out / "board-hints.json").read_text())["stackup_assumptions"]
    assert stackup["layers"] == 6
    assert stackup["profile"] == "6_layer_high_speed"
    assert stackup["source"] == "candidate_from_requested_layer_count"


def test_inspect_ai_pln_prompt_mentions_board_pln(tmp_path):
    board = ROOT / "tests/fixtures/high_speed_connector/layout.kicad_pcb"
    out = _run_inspect(tmp_path, board, ["--width", "75", "--height", "75"])
    prompt = (out / "ai-pln-prompt.md").read_text()
    assert "board.pln" in prompt
    assert "authoritative" in prompt
    review = (out / "ai-placement-review.md").read_text()
    assert "placement owner" in review
