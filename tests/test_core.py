from pathlib import Path
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pcb_place
from pcb_place import apply_placements, import_netlist_aliases, is_valid_uuid, load_ppl, parse_footprints, parse_netlist_aliases


def test_parse_footprints():
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    fps = parse_footprints(text)
    assert set(fps) >= {"U1", "C1", "J1", "H1"}
    assert fps["U1"].x == 0


def test_apply_basic_rules():
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    model = load_ppl(ROOT / "tests/fixtures/simple.ppl")
    out, messages, report = apply_placements(text, model, strict=True)
    assert report["placements_applied"] == 8
    assert '(property "Reference" "U1"' in out
    assert '(at 25 15 0)' in out
    assert '(at 25 13 0)' in out  # C1 satellite above U1


def test_cli_list_refs_json():
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(ROOT / "tests/fixtures/simple.kicad_pcb"), "--list-refs", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    refs = [item["ref"] for item in json.loads(result.stdout)]
    assert "U1" in refs


def test_cli_check_fails_when_stale(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    ppl = ROOT / "tests/fixtures/simple.ppl"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    result = subprocess.run([sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--check"], capture_output=True, text=True)
    assert result.returncode == 1

from pcb_place import PlacementError


def test_region_relative_component_and_lock(tmp_path):
    ppl = tmp_path / "region.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Region("CONTROL", x=20, y=10, w=10, h=10)
Component("U1").anchor(region="CONTROL").lock(reason="primary anchor")
Anchor("C1", relative_to="U1", dx=2, dy=-2, rot=0)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    out, messages, report = apply_placements(text, load_ppl(ppl), strict=True, validate=True)
    assert report["regions"]["CONTROL"]["x"] == 20.0
    assert "U1" in report["locked_refs"]
    assert '(at 25 15 0)' in out
    assert '(at 27 13 0)' in out


def test_locked_component_violation_raises(tmp_path):
    ppl = tmp_path / "locked.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U1", x=10, y=10, lock=True)
Anchor("U1", x=20, y=20)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    with pytest.raises(PlacementError):
        apply_placements(text, load_ppl(ppl), strict=True)


def test_cli_validate(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    ppl = ROOT / "tests/fixtures/simple.ppl"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    result = subprocess.run([sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--validate"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "validate:" in result.stdout


def test_parse_json_netlist_aliases():
    aliases, diagnostics = parse_netlist_aliases(ROOT / "tests/fixtures/zener.json")
    assert diagnostics.parser == "json"
    assert aliases["MCU.U_MCU"] == "U1"
    assert aliases["MCU.C_VDD_1.C"] == "C1"


def test_parse_xml_and_sexp_netlist_aliases():
    xml_aliases, xml_diagnostics = parse_netlist_aliases(ROOT / "tests/fixtures/zener.xml")
    sexp_aliases, sexp_diagnostics = parse_netlist_aliases(ROOT / "tests/fixtures/zener.sexp")
    assert xml_diagnostics.parser == "xml"
    assert sexp_diagnostics.parser == "sexp"
    assert xml_aliases["MCU.U_MCU"] == "U1"
    assert sexp_aliases["MCU.C_VDD_1.C"] == "C1"


def test_imported_aliases_place_semantic_refs(tmp_path):
    ppl = tmp_path / "semantic.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("MCU.U_MCU", x=25, y=15, rot=0)
Satellite("MCU.C_VDD_1.C", parent="MCU.U_MCU", side="top", distance=2, rot=0)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    model = load_ppl(ppl)
    import_netlist_aliases(model, ROOT / "tests/fixtures/zener.json")
    out, messages, report = apply_placements(text, model, strict=True)
    assert '(property "Reference" "U1"' in out
    assert '(at 25 15 0)' in out
    assert '(at 25 13 0)' in out
    assert report["imported_aliases"]["MCU.U_MCU"] == "U1"
    assert report["effective_aliases"]["MCU.C_VDD_1.C"] == "C1"


def test_explicit_alias_overrides_imported_alias(tmp_path):
    ppl = tmp_path / "override.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Alias("MCU.U_MCU", "J1")
Anchor("MCU.U_MCU", x=11, y=12, rot=90)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    model = load_ppl(ppl)
    import_netlist_aliases(model, ROOT / "tests/fixtures/zener.json")
    out, messages, report = apply_placements(text, model, strict=True)
    assert '(property "Reference" "J1"' in out
    assert '(at 11 12 90)' in out
    assert report["effective_aliases"]["MCU.U_MCU"] == "J1"


def test_ambiguous_suffixes_fail(tmp_path):
    netlist = tmp_path / "ambiguous.json"
    netlist.write_text(json.dumps({"components": [
        {"path": "MCU.U_SHARED", "ref": "U1"},
        {"path": "PWR.U_SHARED", "ref": "J1"},
    ]}))
    ppl = tmp_path / "ambiguous.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U_SHARED", x=1, y=2)
''')
    model = load_ppl(ppl)
    import_netlist_aliases(model, netlist)
    with pytest.raises(PlacementError, match="ambiguous"):
        apply_placements((ROOT / "tests/fixtures/simple.kicad_pcb").read_text(), model, strict=True)


def test_missing_semantic_alias_fails_cleanly(tmp_path):
    ppl = tmp_path / "missing.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("MCU.U_DOES_NOT_EXIST", x=1, y=2)
''')
    with pytest.raises(PlacementError, match="missing footprint"):
        apply_placements((ROOT / "tests/fixtures/simple.kicad_pcb").read_text(), load_ppl(ppl), strict=True)


def test_cli_list_aliases_json():
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), "--netlist", str(ROOT / "tests/fixtures/zener.json"), "--list-aliases", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["aliases"]["MCU.U_MCU"] == "U1"


def test_cli_report_json_includes_alias_data(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    report = tmp_path / "report.json"
    ppl = tmp_path / "semantic.ppl"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    ppl.write_text('''
Board(width=50, height=30)
Anchor("MCU.U_MCU", x=25, y=15, rot=0)
''')
    subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--netlist", str(ROOT / "tests/fixtures/zener.json"), "--report-json", str(report), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(report.read_text())
    assert payload["imported_aliases"]["MCU.U_MCU"] == "U1"
    assert payload["alias_diagnostics"]["parser"] == "json"


def test_suffix_alias_target_missing_fails_cleanly(tmp_path):
    netlist = tmp_path / "missing-target.json"
    netlist.write_text(json.dumps({"components": [
        {"path": "MCU.U_MISSING", "ref": "U99"},
    ]}))
    ppl = tmp_path / "missing-target.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U_MISSING", x=1, y=2)
''')
    model = load_ppl(ppl)
    import_netlist_aliases(model, netlist)
    with pytest.raises(PlacementError, match="missing footprint"):
        apply_placements((ROOT / "tests/fixtures/simple.kicad_pcb").read_text(), model, strict=True)


def _pcb_with_at(at_line: str = "(at 140 60)") -> str:
    return f'''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (footprint "Test:U" (layer "F.Cu")
    {at_line}
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:C" (layer "F.Cu")
    (at 150 70 45)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))
  )
)
'''


def test_board_origin_mapping_and_default_origin(tmp_path):
    with_origin = tmp_path / "with_origin.ppl"
    with_origin.write_text('''
Board(width=70, height=40, origin_x=140, origin_y=60)
Anchor("U1", x=10, y=5)
''')
    out, _messages, report = apply_placements(_pcb_with_at(), load_ppl(with_origin), strict=True)
    assert '(at 150 65)' in out
    assert report["board"]["origin_x"] == 140.0

    default_origin = tmp_path / "default_origin.ppl"
    default_origin.write_text('''
Board(width=70, height=40)
Anchor("U1", x=10, y=5)
''')
    out, _messages, report = apply_placements(_pcb_with_at(), load_ppl(default_origin), strict=True)
    assert '(at 10 5)' in out
    assert report["board"]["origin_x"] == 0.0


def test_cli_infer_origin_and_print_bounds(tmp_path):
    pcb = tmp_path / "origin.kicad_pcb"
    ppl = tmp_path / "origin.ppl"
    report = tmp_path / "report.json"
    pcb.write_text(_pcb_with_at())
    ppl.write_text('''
Board(width=70, height=40)
Anchor("U1", x=10, y=5)
''')
    bounds = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), "--print-bounds"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "min_x=140" in bounds.stdout
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--infer-origin", "--dry-run", "--report-json", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "origin source: board_definition" in result.stdout
    assert "origin_x=0" in result.stdout
    assert "U1:" in result.stdout
    payload = json.loads(report.read_text())
    assert payload["board"]["origin_x"] == 0.0
    assert payload["footprint_bounds"]["min_x"] == 140.0
    assert payload["placements"][0]["delta_x"] == -130.0


def test_rotation_form_preserved_and_explicit_rot_changes(tmp_path):
    no_rot = tmp_path / "no_rot.ppl"
    no_rot.write_text('''
Board(width=300, height=200)
Anchor("U1", x=10, y=20)
Anchor("C1", x=30, y=40)
''')
    out, _messages, _report = apply_placements(_pcb_with_at(), load_ppl(no_rot), strict=True)
    assert '(at 10 20)' in out
    assert '(at 30 40 45)' in out

    explicit = tmp_path / "explicit.ppl"
    explicit.write_text('''
Board(width=300, height=200)
Anchor("U1", x=10, y=20, rot=90)
''')
    out, _messages, _report = apply_placements(_pcb_with_at(), load_ppl(explicit), strict=True)
    assert '(at 10 20 90)' in out


def test_path_rotation_opt_in_and_cardinal_rounding(tmp_path):
    preserve = tmp_path / "preserve.ppl"
    preserve.write_text('''
Board(width=300, height=200)
Inline("U1", a="U1", b="C1", t=0.5)
''')
    out, _messages, _report = apply_placements(_pcb_with_at("(at 140 60 12)"), load_ppl(preserve), strict=True)
    assert '(at 145 65 12)' in out

    path = tmp_path / "path.ppl"
    path.write_text('''
Board(width=300, height=200)
Inline("U1", a="U1", b="C1", t=0.5, rot="path")
''')
    out, _messages, _report = apply_placements(_pcb_with_at("(at 140 60 12)"), load_ppl(path), strict=True)
    assert '(at 145 65 45)' in out

    arbitrary = tmp_path / "arbitrary.ppl"
    arbitrary.write_text('''
Board(width=300, height=200)
Anchor("U1", x=10, y=20, rot=44)
Anchor("C1", x=30, y=40, rot=44, allow_arbitrary_rotation=True)
''')
    out, _messages, _report = apply_placements(_pcb_with_at(), load_ppl(arbitrary), strict=True, cardinal_rotations=True)
    assert '(at 10 20 0)' in out
    assert '(at 30 40 44)' in out


def test_safety_rejects_nonfinite_outside_huge_duplicate_and_locked(tmp_path):
    text = _pcb_with_at()
    inf_ppl = tmp_path / "inf.ppl"
    inf_ppl.write_text('''
Board(width=70, height=40, origin_x=140, origin_y=60)
Anchor("U1", x=1e999, y=0)
''')
    with pytest.raises(PlacementError, match="non-finite|outside"):
        apply_placements(text, load_ppl(inf_ppl), strict=True, safe=True)

    outside_ppl = tmp_path / "outside.ppl"
    outside_ppl.write_text('''
Board(width=70, height=40, origin_x=140, origin_y=60)
Anchor("U1", x=80, y=5)
''')
    with pytest.raises(PlacementError, match="outside Board"):
        apply_placements(text, load_ppl(outside_ppl), strict=True, safe=True)

    huge_ppl = tmp_path / "huge.ppl"
    huge_ppl.write_text('''
Board(width=70, height=40, origin_x=140, origin_y=60)
Anchor("U1", x=10000, y=5)
''')
    with pytest.raises(PlacementError, match="far outside"):
        apply_placements(text, load_ppl(huge_ppl), strict=True, safe=True, allow_outside_board=True)

    dup_ppl = tmp_path / "dup.ppl"
    dup_ppl.write_text('''
Board(width=300, height=200)
Anchor("U1", x=1, y=2)
Anchor("U1", x=3, y=4)
''')
    _out, messages, report = apply_placements(text, load_ppl(dup_ppl), strict=False, safe=True)
    assert report["priority_conflicts"]
    assert any("same-priority placement conflict" in m.text for m in messages)

    locked_ppl = tmp_path / "locked-safe.ppl"
    locked_ppl.write_text('''
Board(width=300, height=200)
Lock("U1")
Anchor("U1", x=3, y=4)
''')
    with pytest.raises(PlacementError, match="locked footprint"):
        apply_placements(text, load_ppl(locked_ppl), strict=False, safe=True)


def test_atomic_write_and_output_sanity_preserves_footprint_count(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    out = tmp_path / "simple.placed.kicad_pcb"
    ppl = tmp_path / "simple.ppl"
    pcb.write_text(_pcb_with_at())
    ppl.write_text('''
Board(width=300, height=200)
Anchor("U1", x=10, y=20)
''')
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "-o", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "wrote:" in result.stdout
    assert out.exists()
    assert len(parse_footprints(out.read_text())) == len(parse_footprints(pcb.read_text()))


def test_cli_dry_run_reports_unsafe_placement_without_failing(tmp_path):
    pcb = tmp_path / "unsafe.kicad_pcb"
    ppl = tmp_path / "unsafe.ppl"
    report = tmp_path / "unsafe-report.json"
    pcb.write_text(_pcb_with_at("(at 0 0)"))
    ppl.write_text('''
Board(width=10, height=10)
Anchor("U1", x=20, y=0)
''')
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--dry-run", "--report-json", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "outside_board=yes" in result.stdout
    assert "would be outside Board bounds" in result.stdout
    payload = json.loads(report.read_text())
    assert payload["validation_errors"] >= 1
    assert payload["placements"][0]["outside_board"] is True

from pcb_place import BoardGeometry, emit_board_outline


def _pcb_with_edge_rect() -> str:
    return _pcb_with_at()[:-2] + '''  (gr_rect (start 140 52) (end 210 122) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge-rect"))
)
'''


def _pcb_with_edge_lines() -> str:
    return _pcb_with_at()[:-2] + '''  (gr_line (start 140 52) (end 210 52) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e1"))
  (gr_line (start 210 52) (end 210 122) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e2"))
  (gr_line (start 210 122) (end 140 122) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e3"))
  (gr_line (start 140 122) (end 140 52) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "e4"))
)
'''


def test_edge_cuts_gr_rect_parsing():
    geometry = BoardGeometry.from_edge_cuts(_pcb_with_edge_rect())
    assert geometry is not None
    assert geometry.source == "edge_cuts"
    assert geometry.origin_x == 140.0
    assert geometry.origin_y == 52.0
    assert geometry.width == 70.0
    assert geometry.height == 70.0


def test_edge_cuts_gr_line_rectangle_parsing_and_missing():
    geometry = BoardGeometry.from_edge_cuts(_pcb_with_edge_lines())
    assert geometry is not None
    assert geometry.origin_x == 140.0
    assert geometry.height == 70.0
    assert BoardGeometry.from_edge_cuts(_pcb_with_at()) is None


def test_emit_outline_true_generates_outline_and_conflict(tmp_path):
    ppl = tmp_path / "outline.ppl"
    ppl.write_text('''
Board(width=70, height=70, origin_x=140, origin_y=52, emit_outline=True)
Anchor("U1", x=10, y=8)
''')
    out, _messages, report = apply_placements(_pcb_with_at(), load_ppl(ppl), strict=True)
    assert 'layer "Edge.Cuts"' in out
    assert BoardGeometry.from_edge_cuts(out).width == 70.0
    assert report["board_geometry"]["source"] == "placement_file"

    conflict = tmp_path / "conflict.ppl"
    conflict.write_text('''
Board(width=80, height=70, origin_x=140, origin_y=52, emit_outline=True)
Anchor("U1", x=10, y=8)
''')
    with pytest.raises(PlacementError, match="conflicts"):
        apply_placements(_pcb_with_edge_rect(), load_ppl(conflict), strict=True)


def test_generated_edge_cuts_lines_receive_unique_uuidv4(tmp_path):
    ppl = tmp_path / "outline-uuid.ppl"
    ppl.write_text('''
Board(width=70, height=70, origin_x=140, origin_y=52, emit_outline=True)
Anchor("U1", x=10, y=8)
''')
    out, _messages, report = apply_placements(_pcb_with_at(), load_ppl(ppl), strict=True)
    generated = report["generated_uuids"]
    assert len(generated) == 4
    uuid_values = [item["uuid"] for item in generated]
    assert [item["object"] for item in generated] == ["gr_line"] * 4
    assert len(set(uuid_values)) == 4
    assert all(is_valid_uuid(value) for value in uuid_values)
    assert all(f'(uuid "{value}")' in out for value in uuid_values)


def test_existing_footprint_group_and_pad_uuid_text_preserved(tmp_path):
    ppl = tmp_path / "preserve-uuid.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U1", x=10, y=20)
''')
    footprint_uuid = "footprint-existing-uuid-text"
    pad_uuid = "pad-existing-uuid-text"
    group_uuid = "group-existing-uuid-text"
    text = f'''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (footprint "Test:U" (layer "F.Cu")
    (uuid "{footprint_uuid}")
    (at 140 60)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 -1) (size 1 1) (layers "F.Cu") (uuid "{pad_uuid}"))
  )
  (group "existing-group" (uuid "{group_uuid}")
    (members "{footprint_uuid}")
  )
)
'''
    out, _messages, report = apply_placements(text, load_ppl(ppl), strict=True)
    assert f'(uuid "{footprint_uuid}")' in out
    assert f'(uuid "{pad_uuid}")' in out
    assert f'(uuid "{group_uuid}")' in out
    assert out.count(f'(uuid "{footprint_uuid}")') == 1
    assert out.count(f'(uuid "{pad_uuid}")') == 1
    assert out.count(f'(uuid "{group_uuid}")') == 1
    assert report["generated_uuids"] == []


def test_malformed_generated_uuid_rejected(monkeypatch, tmp_path):
    ppl = tmp_path / "bad-uuid.ppl"
    ppl.write_text('''
Board(width=70, height=70, origin_x=140, origin_y=52, emit_outline=True)
Anchor("U1", x=10, y=8)
''')
    monkeypatch.setattr(pcb_place.uuid, "uuid4", lambda: "not-a-uuid")
    with pytest.raises(PlacementError, match="valid UUIDv4"):
        apply_placements(_pcb_with_at(), load_ppl(ppl), strict=True)


def test_duplicate_generated_uuid_rejected(monkeypatch, tmp_path):
    ppl = tmp_path / "duplicate-uuid.ppl"
    ppl.write_text('''
Board(width=70, height=70, origin_x=140, origin_y=52, emit_outline=True)
Anchor("U1", x=10, y=8)
''')
    monkeypatch.setattr(pcb_place.uuid, "uuid4", lambda: "550e8400-e29b-41d4-a716-446655440000")
    with pytest.raises(PlacementError, match="not unique within output file|duplicate generated UUID"):
        apply_placements(_pcb_with_at(), load_ppl(ppl), strict=True)


def test_uuid_validation_helper():
    assert is_valid_uuid("550e8400-e29b-41d4-a716-446655440000")
    assert not is_valid_uuid("550e8400-e29b-11d4-a716-446655440000")
    assert not is_valid_uuid("pcb-place-edge-1")
    assert not is_valid_uuid("550E8400-E29B-41D4-A716-446655440000")


def test_geometry_aware_validation_prefers_edge_cuts(tmp_path):
    ppl = tmp_path / "edge-authoritative.ppl"
    ppl.write_text('''
Board(width=300, height=300)
Anchor("U1", x=5, y=5)
''')
    with pytest.raises(PlacementError, match="outside Board"):
        apply_placements(_pcb_with_edge_rect(), load_ppl(ppl), strict=True, safe=True)


def test_cli_print_board_and_improved_print_bounds(tmp_path):
    pcb = tmp_path / "edge.kicad_pcb"
    pcb.write_text(_pcb_with_edge_rect())
    board = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), "--print-board"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "source=edge_cuts" in board.stdout
    assert "width=70" in board.stdout
    bounds = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), "--print-bounds"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "footprint_bounds:" in bounds.stdout
    assert "board_bounds:" in bounds.stdout
    assert "source=edge_cuts" in bounds.stdout


def test_cli_emit_outline_only(tmp_path):
    pcb = tmp_path / "no-edge.kicad_pcb"
    ppl = tmp_path / "outline.ppl"
    out = tmp_path / "outline.kicad_pcb"
    pcb.write_text(_pcb_with_at())
    ppl.write_text('''
Board(width=70, height=70, origin_x=140, origin_y=52, emit_outline=True)
''')
    subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--emit-outline-only", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    geometry = BoardGeometry.from_edge_cuts(out.read_text())
    assert geometry is not None
    assert geometry.origin_x == 140.0


def test_cli_debug_write_reports_generated_uuids(tmp_path):
    pcb = tmp_path / "no-edge-debug.kicad_pcb"
    ppl = tmp_path / "outline-debug.ppl"
    out = tmp_path / "outline-debug.kicad_pcb"
    pcb.write_text(_pcb_with_at())
    ppl.write_text('''
Board(width=70, height=70, origin_x=140, origin_y=52, emit_outline=True)
''')
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--emit-outline-only", str(out), "--debug-write"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "generated UUID:" in result.stdout
    assert "object=gr_line" in result.stdout


def _pcb_with_l_shaped_edge_lines() -> str:
    return _pcb_with_at()[:-2] + '''  (gr_line (start 140 52) (end 210 52) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "l1"))
  (gr_line (start 210 52) (end 210 82) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "l2"))
  (gr_line (start 210 82) (end 170 82) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "l3"))
  (gr_line (start 170 82) (end 170 122) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "l4"))
  (gr_line (start 170 122) (end 140 122) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "l5"))
  (gr_line (start 140 122) (end 140 52) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "l6"))
)
'''


def _pcb_with_chamfered_edge_lines() -> str:
    return _pcb_with_at()[:-2] + '''  (gr_line (start 140 52) (end 200 52) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "c1"))
  (gr_line (start 200 52) (end 210 62) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "c2"))
  (gr_line (start 210 62) (end 210 122) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "c3"))
  (gr_line (start 210 122) (end 140 122) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "c4"))
  (gr_line (start 140 122) (end 140 52) (stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "c5"))
)
'''


def test_edge_cuts_rejects_non_rectangular_gr_line_outlines():
    with pytest.raises(PlacementError, match="Unsupported Edge.Cuts geometry"):
        BoardGeometry.from_edge_cuts(_pcb_with_l_shaped_edge_lines())
    with pytest.raises(PlacementError, match="Unsupported Edge.Cuts geometry"):
        BoardGeometry.from_edge_cuts(_pcb_with_chamfered_edge_lines())


def _safety_pcb() -> str:
    return '''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (footprint "Pkg:SOIC" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -2 -1) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 2 1) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Pkg:R" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.8) (layers "F.Cu"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.8) (layers "F.Cu"))
  )
  (footprint "Pkg:J" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Pkg:H" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "H1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (layers "*.Cu"))
  )
)
'''


def test_direct_overlap_detection_fails(tmp_path):
    ppl = tmp_path / "overlap.ppl"
    ppl.write_text('''
Board(width=30, height=30)
AvoidOverlap(False)
Anchor("U1", x=10, y=10)
Anchor("R1", x=10, y=10)
''')
    with pytest.raises(PlacementError, match="overlaps"):
        apply_placements(_safety_pcb(), load_ppl(ppl), strict=True, safe=True)


def test_spacing_violation_and_spacing_dsl(tmp_path):
    ppl = tmp_path / "spacing.ppl"
    ppl.write_text('''
Board(width=30, height=30)
Spacing(default=0.25, passive_to_ic=2.0)
PartClass("U1", "ic")
PartClass("R1", "passive")
Anchor("U1", x=10, y=10)
Anchor("R1", x=13.8, y=10)
''')
    with pytest.raises(PlacementError, match="passive to ic|ic to passive|required 2"):
        apply_placements(_safety_pcb(), load_ppl(ppl), strict=True, safe=True)
    _out, _messages, report = apply_placements(_safety_pcb(), load_ppl(ppl), strict=True, safe=True, warn_overlap=True)
    assert report["clearance_rules"]["passive_to_ic"] == 2.0
    assert report["spacing_violations"]


def test_partclass_inference(tmp_path):
    ppl = tmp_path / "classes.ppl"
    ppl.write_text('''
Board(width=30, height=30)
Anchor("U1", x=5, y=5)
Anchor("R1", x=10, y=5)
Anchor("J1", x=15, y=5)
Anchor("H1", x=20, y=5)
''')
    _out, _messages, report = apply_placements(_safety_pcb(), load_ppl(ppl), strict=True, safe=True, warn_overlap=True)
    assert report["part_classes"]["R1"] == "passive"
    assert report["part_classes"]["U1"] == "ic"
    assert report["part_classes"]["J1"] == "connector"
    assert report["part_classes"]["H1"] == "mechanical"


def test_satellite_avoidance_auto_adjusts_and_respects_bounds(tmp_path):
    ppl = tmp_path / "avoid.ppl"
    ppl.write_text('''
Board(width=30, height=30)
PlacementPolicy(avoid_overlap=True, max_search_radius=5, search_step=0.5)
Anchor("U1", x=10, y=10)
Satellite("R1", parent="U1", side="top", distance=0.2, clearance=0.5)
''')
    _out, _messages, report = apply_placements(_safety_pcb(), load_ppl(ppl), strict=True, safe=True, warn_overlap=True)
    assert report["auto_adjustments"]
    placed = report["placements"][0] if report["placements"][0]["ref"] == "R1" else report["placements"][1]
    assert 0 <= placed["x"] <= 30 and 0 <= placed["y"] <= 30


def test_locked_anchor_not_moved_by_avoidance(tmp_path):
    ppl = tmp_path / "locked-avoid.ppl"
    ppl.write_text('''
Board(width=30, height=30)
Anchor("U1", x=10, y=10, lock=True)
AvoidOverlap(False)
Anchor("R1", x=10, y=10)
''')
    with pytest.raises(PlacementError, match="overlaps"):
        apply_placements(_safety_pcb(), load_ppl(ppl), strict=True, safe=True)


def test_fallback_bbox_warning_report_json_and_dry_run(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    ppl = tmp_path / "simple.ppl"
    report = tmp_path / "report.json"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U1", x=10, y=10)
Satellite("C1", parent="U1", side="top", distance=0.1)
''')
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--dry-run", "--report-json", str(report), "--warn-overlap"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(report.read_text())
    assert "collisions" in payload and "spacing_violations" in payload and "bbox_warnings" in payload and "auto_adjustments" in payload
    assert payload["bbox_warnings"]
    assert "auto_adjustments=" in result.stdout


def _two_footprint_pcb(*, r_layer: str = "F.Cu", r_at: str = "10 10 0") -> str:
    return f'''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (footprint "Pkg:SOIC" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 -1) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 1 1) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Pkg:R" (layer "{r_layer}")
    (at {r_at})
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.8) (layers "{r_layer}"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.8) (layers "{r_layer}"))
  )
)
'''


def test_collision_validation_checks_updated_against_existing_footprints(tmp_path):
    ppl = tmp_path / "subset-overlap.ppl"
    ppl.write_text('''
Board(width=30, height=30)
Anchor("U1", x=10, y=10)
''')
    with pytest.raises(PlacementError, match="U1 overlaps R1|R1 overlaps U1"):
        apply_placements(_two_footprint_pcb(), load_ppl(ppl), strict=True, safe=True)


def test_collision_validation_ignores_opposite_board_sides(tmp_path):
    ppl = tmp_path / "back-to-back.ppl"
    ppl.write_text('''
Board(width=30, height=30)
Anchor("U1", x=10, y=10)
''')
    _out, _messages, report = apply_placements(
        _two_footprint_pcb(r_layer="B.Cu"), load_ppl(ppl), strict=True, safe=True
    )
    assert report["collisions"] == []
    assert report["spacing_violations"] == []


def _cluster_pcb() -> str:
    return '''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (footprint "Test:U" (layer "F.Cu")
    (at 10 10 90)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
    (property "Reference" "U6" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:C" (layer "F.Cu")
    (at 12 10 45)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
    (property "Reference" "C5" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:R" (layer "F.Cu")
    (at 10 13 180)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:J" (layer "F.Cu")
    (at 40 10 0)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
  )
)
'''


def _load_inline_ppl(tmp_path, text):
    ppl = tmp_path / "placement.ppl"
    ppl.write_text(text)
    return load_ppl(ppl)


def test_cluster_anchor_placement_preserves_relative_geometry_and_rotations(tmp_path):
    model = _load_inline_ppl(tmp_path, '''
Board(width=80, height=50)
Cluster("MCU", anchor="U6", members=["U6", "C5", "R1"], placement=Anchor(x=30, y=20))
''')
    out, messages, report = apply_placements(_cluster_pcb(), model, strict=True, allow_overlap=True)
    fps = parse_footprints(out)
    assert (fps["U6"].x, fps["U6"].y, fps["U6"].rot) == (30, 20, 90)
    assert (fps["C5"].x, fps["C5"].y, fps["C5"].rot) == (32, 20, 45)
    assert (fps["R1"].x, fps["R1"].y, fps["R1"].rot) == (30, 23, 180)
    assert report["clusters"][0]["delta"] == [20.0, 10.0]


def test_cluster_edge_and_corner_placement(tmp_path):
    edge_model = _load_inline_ppl(tmp_path, '''
Board(width=80, height=50)
Cluster("IO", anchor="U6", members=["C5"], placement=Edge(edge="left", y=25, inset=3))
''')
    out, _messages, report = apply_placements(_cluster_pcb(), edge_model, strict=True, allow_overlap=True)
    fps = parse_footprints(out)
    assert (fps["U6"].x, fps["U6"].y) == (3, 25)
    assert (fps["C5"].x, fps["C5"].y) == (5, 25)
    assert report["clusters"][0]["member_count"] == 2  # anchor auto-added

    corner_model = _load_inline_ppl(tmp_path, '''
Board(width=80, height=50)
Cluster("IO", anchor="U6", members=["U6", "C5"], placement=Corner(corner="bottom_right", inset=5))
''')
    out, _messages, _report = apply_placements(_cluster_pcb(), corner_model, strict=True, allow_overlap=True)
    fps = parse_footprints(out)
    assert (fps["U6"].x, fps["U6"].y) == (75, 45)
    assert (fps["C5"].x, fps["C5"].y) == (77, 45)


def test_cluster_duplicate_missing_and_ignore_missing(tmp_path):
    model = _load_inline_ppl(tmp_path, '''
Board(width=80, height=50)
Cluster("MCU", anchor="U6", members=["U6", "C5", "C5", "R99"], placement=Anchor(x=30, y=20), ignore_missing=True)
''')
    _out, messages, report = apply_placements(_cluster_pcb(), model, strict=True, allow_overlap=True)
    texts = [m.text for m in messages]
    assert any("duplicate member 'C5'" in text for text in texts)
    assert any("missing member 'R99'" in text for text in texts)
    assert report["clusters"][0]["member_count"] == 2

    missing = _load_inline_ppl(tmp_path, '''
Board(width=80, height=50)
Cluster("MCU", anchor="U6", members=["R99"], placement=Anchor(x=30, y=20))
''')
    with pytest.raises(pcb_place.PlacementError, match="missing member"):
        apply_placements(_cluster_pcb(), missing, strict=True)


def test_cluster_deduplicates_members_after_alias_resolution(tmp_path):
    model = _load_inline_ppl(tmp_path, """
Board(width=80, height=50)
Alias("MCU.U6", "U6")
Cluster("MCU", anchor="U6", members=["MCU.U6", "U6", "C5"], placement=Anchor(x=30, y=20))
""")
    out, messages, report = apply_placements(_cluster_pcb(), model, strict=True, allow_overlap=True)
    fps = parse_footprints(out)
    assert (fps["U6"].x, fps["U6"].y) == (30, 20)
    assert (fps["C5"].x, fps["C5"].y) == (32, 20)
    assert report["clusters"][0]["member_count"] == 2
    assert any("resolves to duplicate footprint 'U6'" in m.text for m in messages)


def test_cluster_satellite_refinement_warns_and_report_json_cli(tmp_path):
    pcb = tmp_path / "cluster.kicad_pcb"
    ppl = tmp_path / "cluster.ppl"
    report = tmp_path / "report.json"
    pcb.write_text(_cluster_pcb())
    ppl.write_text('''
Board(width=80, height=50)
Cluster("MCU", anchor="U6", members=["U6", "C5"], placement=Anchor(x=30, y=20))
Satellite("C5", parent="U6", side="top", distance=2)
''')
    out, messages, _report = apply_placements(pcb.read_text(), load_ppl(ppl), strict=True, allow_overlap=True)
    fps = parse_footprints(out)
    assert (fps["C5"].x, fps["C5"].y) == (30, 18)
    assert any("C5 moved by cluster MCU later refined by satellite" in m.text for m in messages)

    clusters = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--print-clusters"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Cluster MCU" in clusters.stdout
    assert "members: 2" in clusters.stdout

    subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--report-json", str(report), "--dry-run", "--allow-overlap"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(report.read_text())
    assert payload["clusters"][0]["name"] == "MCU"
    assert payload["clusters"][0]["new_anchor"] == [30.0, 20.0]


def test_cluster_collision_validation(tmp_path):
    model = _load_inline_ppl(tmp_path, '''
Board(width=80, height=50)
Cluster("MCU", anchor="U6", members=["U6", "C5"], placement=Anchor(x=40, y=10))
''')
    _out, _messages, report = apply_placements(_cluster_pcb(), model, strict=False, validate=True)
    assert any(item["ref_a"] == "J1" or item["ref_b"] == "J1" for item in report["collisions"])


def _pad_pcb(rot: float = 0) -> str:
    return f'''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (footprint "Test:U" (layer "F.Cu")
    (at 10 10 {rot})
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 2 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 4 0) (size 1 1) (layers "F.Cu"))
    (pad "SCL" smd rect (at 0 3) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Test:C" (layer "F.Cu")
    (at 30 30 45)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Test:R" (layer "F.Cu")
    (at 35 35 0)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Test:D" (layer "F.Cu")
    (at 40 40 0)
    (property "Reference" "D1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Test:J" (layer "F.Cu")
    (at 2 2 0)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
)'''


def _load_inline(tmp_path, body: str):
    ppl = tmp_path / "inline.ppl"
    ppl.write_text(body)
    return load_ppl(ppl)


def test_nearpad_places_pad_centroid_and_rotation(tmp_path):
    model = _load_inline(tmp_path, '''
Board(width=100, height=100)
NearPad("C1", parent="U1", pad="1", distance=6, side="right")
NearPad("R1", parent="U1", pad=["1", "2"], distance=6, side="bottom")
''')
    out, _messages, _report = apply_placements(_pad_pcb(), model, strict=True)
    assert '(at 18 10 45)' in out  # pad 1 absolute x=12 plus 6; C1 rotation preserved
    assert '(at 13 16 0)' in out   # centroid x=13, y=10 plus bottom distance

    model = _load_inline(tmp_path, '''
Board(width=100, height=100)
NearPad("C1", parent="U1", pad="1", distance=6, side="right")
''')
    out, _messages, _report = apply_placements(_pad_pcb(90), model, strict=True)
    assert '(at 16 12 45)' in out  # local pad (2,0) rotates to (0,2), then right 6


def test_nearpad_missing_pad_fails_clearly(tmp_path):
    model = _load_inline(tmp_path, 'Board(width=100, height=100)\nNearPad("C1", parent="U1", pad="99")\n')
    with pytest.raises(PlacementError, match="no pad '99'"):
        apply_placements(_pad_pcb(), model, strict=True)


def test_semantic_helpers_expand(tmp_path):
    model = _load_inline(tmp_path, '''
Board(width=100, height=100)
Decoupling("C1", parent="U1", pad="1", side="right")
Pullup("R1", parent="U1", pad="SCL", side="bottom")
Series("D1", a="J1", b="U1", t=0.5, offset=0)
ESD("J1", connector="U1", protected="C1", t=0.2, offset=-1)
''')
    _out, _messages, report = apply_placements(_pad_pcb(), model, strict=True)
    assert report["placements_applied"] == 4
    assert any(p["why"] == "near_pad" and p["ref"] == "C1" for p in report["placements"])
    assert any(p["why"] == "between" and p["ref"] == "D1" for p in report["placements"])


def test_keepout_region_priority_soft_and_cli(tmp_path):
    pcb = tmp_path / "pad.kicad_pcb"
    pcb.write_text(_pad_pcb())
    ppl = tmp_path / "rules.ppl"
    ppl.write_text('''
Board(width=100, height=100)
Region("CONTROL", x=0, y=0, w=20, h=20)
Keepout("ANT", x=13.5, y=9.5, w=3, h=3, role="rf")
Anchor("C1", x=14, y=10, region="CONTROL", soft=True)
Anchor("R1", x=80, y=80, priority=1)
Anchor("R1", x=5, y=5, priority=2)
Anchor("D1", x=3, y=3, priority=2)
Anchor("D1", x=4, y=4, priority=2)
''')
    out, messages, report = apply_placements(pcb.read_text(), load_ppl(ppl), strict=True)
    assert report["keepouts"] and report["regions"]["CONTROL"]
    assert report["auto_adjustments"]  # soft C1 is moved away from keepout
    assert any(item["reason"] == "higher_priority" for item in report["overridden_rules"])
    assert report["priority_conflicts"]
    assert '(at 5 5 0)' in out
    result = subprocess.run([sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--print-regions"], check=True, capture_output=True, text=True)
    assert "Region CONTROL" in result.stdout


def test_keepout_and_region_violations_reported(tmp_path):
    model = _load_inline(tmp_path, '''
Board(width=100, height=100)
Region("CONTROL", x=0, y=0, w=5, h=5)
Keepout("ANT", x=13.5, y=9.5, w=3, h=3)
Anchor("C1", x=14, y=10, region="CONTROL")
''')
    _out, messages, report = apply_placements(_pad_pcb(), model, strict=False, validate=True)
    assert report["keepout_violations"]
    assert report["region_violations"]
    assert any(m.level == "error" and "keepout" in m.text for m in messages)


def test_lock_and_hard_anchor_semantics(tmp_path):
    locked = _load_inline(tmp_path, '''
Board(width=100, height=100)
Anchor("C1", x=10, y=10, lock=True)
Anchor("C1", x=12, y=12, priority=1)
''')
    with pytest.raises(PlacementError, match="locked footprint"):
        apply_placements(_pad_pcb(), locked, strict=True)

    hard = _load_inline(tmp_path, '''
Board(width=100, height=100)
Keepout("ANT", x=13.5, y=9.5, w=3, h=3)
Anchor("C1", x=14, y=10)
''')
    _out, _messages, report = apply_placements(_pad_pcb(), hard, strict=False, validate=True)
    assert not report["auto_adjustments"]
    assert report["keepout_violations"]

    allowed = _load_inline(tmp_path, '''
Board(width=100, height=100)
Keepout("ANT", x=13.5, y=9.5, w=3, h=3)
Anchor("C1", x=14, y=10, allow_keepout_overlap=True)
''')
    _out, _messages, report = apply_placements(_pad_pcb(), allowed, strict=True, validate=True, allow_keepout_overlap=True, allow_overlap=True)
    assert report["keepout_violations"] == []
